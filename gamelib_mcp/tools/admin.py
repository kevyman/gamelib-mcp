"""refresh_library, detect_farmed_games, and set_nintendo_session admin tools."""

import asyncio
import json
import logging
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

from fastmcp.exceptions import ToolError

from ..data.content import (
    CONTENT_BASE_GAME,
    CONTENT_DLC,
    CONTENT_UNKNOWN_ADDON,
    NESTED_CONTENT_TYPES,
)
from ..data.db import (
    ACQUISITION_FIELDS,
    STEAM_APP_ID,
    clear_fulfilled_wishlist_entries,
    default_data_dir,
    get_db,
    record_play_history_snapshots,
)
from ..data.title_normalization import normalize_search_text
from ..data.enrich_bg import pause_background_enrichment, resume_background_enrichment

# The platform sync dicts are built from platforms_registry at call time; the
# imports below keep the functions bound on this module so existing tests can
# patch gamelib_mcp.tools.admin.<sync_fn> (resolve_platform_functions checks
# this namespace first). F401: referenced via getattr, not by name.
from ..data.epic import sync_epic  # noqa: F401
from ..data.gog import sync_gog  # noqa: F401
from ..data.nintendo import sync_nintendo  # noqa: F401
from ..data.psn import sync_psn  # noqa: F401
from ..data.steam_xml import fetch_library  # noqa: F401
from ..data.xbox import sync_xbox  # noqa: F401
from ..lifecycle import _schedule_background_enrich, get_startup_refresh_task
from ..platforms_registry import WISHLIST_SYNCABLE_PLATFORMS, resolve_platform_functions
from .common import PLATFORM_ALIASES, SYNCABLE_PLATFORMS, info as _info, report_progress

logger = logging.getLogger(__name__)


async def _mark_sync_started(targets: set[str]) -> None:
    """Mark the overall sync in-progress and each selected platform running."""
    from ..data.db import set_meta_many

    updates: dict[str, str | None] = {
        "library_sync_status": "in_progress",
        "library_sync_started_at": datetime.now(timezone.utc).isoformat(),
        "library_sync_finished_at": None,
    }
    for name in targets:
        updates[f"sync_platform_state_{name}"] = "running"
    await set_meta_many(updates)


async def _mark_platform_state(name: str, state: str) -> None:
    from ..data.db import set_meta
    await set_meta(f"sync_platform_state_{name}", state)


async def run_library_sync(
    platforms: list[str] | None = None,
    ctx=None,
) -> dict:
    """
    Re-sync game library. Defaults to all configured platforms.
    platforms: optional subset, e.g. ["steam", "epic"]. If omitted, syncs all.
    """
    def _resolve(p: str) -> str:
        return PLATFORM_ALIASES.get(p.lower(), p.lower())

    requested_targets = list(platforms) if platforms else sorted(SYNCABLE_PLATFORMS)
    unknown_platforms = [p for p in requested_targets if _resolve(p) not in SYNCABLE_PLATFORMS]
    if unknown_platforms:
        valid = sorted(SYNCABLE_PLATFORMS | set(PLATFORM_ALIASES))
        unknown = "', '".join(unknown_platforms)
        raise ToolError(f"Unknown platform '{unknown}'. Valid: {valid}")

    targets = {_resolve(p) for p in requested_targets}

    if targets == SYNCABLE_PLATFORMS:
        startup_task = get_startup_refresh_task()
        current_task = asyncio.current_task()
        if startup_task is not None and not startup_task.done() and startup_task is not current_task:
            await _info(ctx, "Waiting for running startup library refresh")
            result = await asyncio.shield(startup_task)
            if isinstance(result, dict):
                return result

    # Derived from the platform registry; resolution prefers names bound on
    # THIS module (the imports above), so tests patching e.g.
    # gamelib_mcp.tools.admin.sync_epic keep intercepting the sync.
    platform_syncs = resolve_platform_functions("sync", namespace=sys.modules[__name__])

    result_names = {name: name for name in targets}
    for requested in requested_targets:
        result_names[_resolve(requested)] = requested

    async def run_platform(_name: str, fn) -> dict:
        return await fn()

    selected = [(name, fn) for name, fn in platform_syncs.items() if name in targets]
    pause_background_enrichment()
    try:
        # Mark started here too (not only in the refresh_library tool): the startup and
        # periodic paths reach this worker via _run_startup_refresh without going through
        # the tool, so this is what records per-platform "running" state on those paths.
        # On the tool path it's an idempotent re-write of state the tool already set.
        await _mark_sync_started(targets)
        await report_progress(ctx, 0, len(selected))
        await _info(ctx, f"Refreshing {len(selected)} platform(s)")
        outcomes = await asyncio.gather(
            *(run_platform(name, fn) for name, fn in selected),
            return_exceptions=True,
        )

        results: dict = {}
        for index, ((name, _), outcome) in enumerate(zip(selected, outcomes, strict=True), start=1):
            result_name = result_names.get(name, name)
            if isinstance(outcome, BaseException):
                results[result_name] = {"error": str(outcome)}
                await _mark_platform_state(name, "error")
                await _info(ctx, f"Failed {result_name} refresh: {outcome}")
            else:
                results[result_name] = outcome
                await _mark_platform_state(name, "done")
                try:
                    history_rows = await record_play_history_snapshots(name)
                except Exception:
                    logger.warning(
                        "play_history snapshot failed for %s", name, exc_info=True
                    )
                    history_rows = None
                if isinstance(outcome, dict) and history_rows is not None:
                    outcome["play_history_rows"] = history_rows
                await _info(ctx, f"Finished {result_name} refresh")
            await report_progress(ctx, index, len(selected))

        steam_result = results.get("steam")
        steam_synced = (
            "steam" in targets
            and isinstance(steam_result, dict)
            and not steam_result.get("error")
        )
        if steam_synced:
            try:
                await detect_farmed_games(dry_run=False)
            except Exception:
                logger.exception("Farmed-game detection failed after Steam refresh")

            # GetOwnedGames silently omits some retired/delisted apps the
            # account still holds licenses for — the audit heals those from
            # the account's own license list. Incremental (per-appid outcomes
            # persist), capped per refresh, and a no-op without a stored Steam
            # store session. Never fails the refresh.
            try:
                from ..data.steam_licenses import (
                    audit_steam_licenses as _audit_steam_licenses,
                    is_license_audit_configured,
                )

                if is_license_audit_configured():
                    audit = await _audit_steam_licenses()
                    if isinstance(steam_result, dict):
                        steam_result["license_audit"] = {
                            "minted": len(audit.get("minted", [])),
                            "minted_delisted": len(audit.get("minted_delisted", [])),
                            "skipped_non_game": len(audit.get("skipped_non_game", [])),
                            "unresolved": len(audit.get("unresolved", [])),
                            "remaining": audit.get("remaining", 0),
                        }
            except Exception:
                logger.exception("Steam license audit failed after Steam refresh")

        # A refresh may have just established ownership of a previously-wishlisted
        # game (e.g. bought it on Steam) — clear it the same way storefronts do.
        try:
            await clear_fulfilled_wishlist_entries()
        except Exception:
            logger.exception("Wishlist fulfillment cleanup failed after library refresh")
    finally:
        resume_background_enrichment()

    try:
        await _schedule_background_enrich()
    except Exception:
        logger.exception("Failed to schedule background enrichment after library refresh")

    from ..data.db import set_meta_many
    await set_meta_many({
        "library_sync_status": "idle",
        "library_sync_finished_at": datetime.now(timezone.utc).isoformat(),
    })
    return results


async def refresh_library(
    platforms: list[str] | None = None,
    ctx=None,
) -> dict:
    """
    Schedule a library re-sync and return immediately (non-blocking).

    Starts a background sync of the owned game library from configured
    platforms and returns an acknowledgement. Poll get_sync_status to follow
    progress. platforms can be omitted (all configured platforms) or a subset.
    """
    from ..lifecycle import _ensure_startup_refresh, get_startup_refresh_task

    def _resolve(p: str) -> str:
        return PLATFORM_ALIASES.get(p.lower(), p.lower())

    requested_targets = list(platforms) if platforms else sorted(SYNCABLE_PLATFORMS)
    unknown = [p for p in requested_targets if _resolve(p) not in SYNCABLE_PLATFORMS]
    if unknown:
        valid = sorted(SYNCABLE_PLATFORMS | set(PLATFORM_ALIASES))
        raise ToolError(f"Unknown platform '{', '.join(unknown)}'. Valid: {valid}")

    targets = {_resolve(p) for p in requested_targets}

    existing = get_startup_refresh_task()
    if existing is not None and not existing.done():
        return {
            "status": "already_running",
            "platforms": sorted(targets),
            "already_running": True,
        }

    await _mark_sync_started(targets)
    await _ensure_startup_refresh(sorted(targets))
    return {
        "status": "started",
        "platforms": sorted(targets),
        "already_running": False,
    }


async def get_sync_status() -> dict:
    """
    Report the current/last library sync: overall state plus per-platform state.

    status is "in_progress" while a sync runs, else "idle". Each syncable
    platform reports state (pending/running/done/error), its last success time,
    and the last error summary if any. Poll this after calling refresh_library.
    """
    from ..data.db import get_meta, get_meta_prefix

    overall = await get_meta("library_sync_status") or "idle"
    started_at = await get_meta("library_sync_started_at")
    finished_at = await get_meta("library_sync_finished_at")

    state_keys = await get_meta_prefix("sync_platform_state_")
    integ = await get_meta_prefix("integration_sync_")

    platforms: dict[str, dict] = {}
    for name in sorted(SYNCABLE_PLATFORMS):
        platforms[name] = {
            "state": state_keys.get(f"sync_platform_state_{name}", "pending"),
            "last_success_at": integ.get(f"integration_sync_{name}_last_success_at"),
            "error": integ.get(f"integration_sync_{name}_last_error_summary"),
        }

    return {
        "status": overall,
        "started_at": started_at,
        "finished_at": finished_at,
        "platforms": platforms,
    }


# WISHLIST_SYNCABLE_PLATFORMS comes from platforms_registry (specs carrying a
# wishlist_sync ref). PSN has no public wishlist API (confirmed: no community
# library exposes one) — use add_game_to_platform(owned=False) for it instead.


async def sync_wishlist(
    platforms: list[str] | None = None,
    ctx=None,
) -> dict:
    """
    Sync wishlists from configured automated sources: Steam (official wishlist
    API) and Nintendo/switch2 (via a DekuDeals shared wishlist export, since
    Nintendo has no wishlist API). Defaults to both.

    platforms: optional subset, e.g. ["steam"]. PSN is not included — it has no
    wishlist API; record PSN wishlist items with
    add_game_to_platform(name, "ps5", owned=False) instead.

    A platform whose required config (STEAM_API_KEY/STEAM_ID or
    DEKUDEALS_WISHLIST_URL) isn't set returns sync_status="unconfigured" instead
    of erroring.
    """
    def _resolve(p: str) -> str:
        return PLATFORM_ALIASES.get(p.lower(), p.lower())

    requested = list(platforms) if platforms else sorted(WISHLIST_SYNCABLE_PLATFORMS)
    unknown = [p for p in requested if _resolve(p) not in WISHLIST_SYNCABLE_PLATFORMS]
    if unknown:
        valid = sorted(WISHLIST_SYNCABLE_PLATFORMS | set(PLATFORM_ALIASES))
        raise ToolError(
            f"Unknown wishlist platform '{', '.join(unknown)}'. Valid: {valid}. "
            "PSN has no wishlist API — use add_game_to_platform(owned=False)."
        )

    targets = {_resolve(p) for p in requested}
    platform_syncs = resolve_platform_functions("wishlist_sync", namespace=sys.modules[__name__])
    selected = [(name, fn) for name, fn in platform_syncs.items() if name in targets]

    await _info(ctx, f"Syncing wishlist for {len(selected)} platform(s)")
    await report_progress(ctx, 0, len(selected))
    outcomes = await asyncio.gather(
        *(fn() for _, fn in selected),
        return_exceptions=True,
    )

    results: dict = {}
    for index, ((name, _), outcome) in enumerate(zip(selected, outcomes, strict=True), start=1):
        if isinstance(outcome, BaseException):
            results[name] = {"error": str(outcome)}
            await _info(ctx, f"Failed {name} wishlist sync: {outcome}")
        else:
            results[name] = outcome
            await _info(ctx, f"Finished {name} wishlist sync")
        await report_progress(ctx, index, len(selected))

    # A stale external wishlist can list a game already owned locally (bought
    # elsewhere, or ownership synced since the last wishlist check) — reconcile
    # immediately rather than waiting for the next library refresh.
    try:
        await clear_fulfilled_wishlist_entries()
    except Exception:
        logger.exception("Wishlist fulfillment cleanup failed after wishlist sync")

    return results


def _save_session_cookies(cookies: str, env_var: str, default_filename: str, label: str) -> dict:
    """Normalize a pasted cookie-export JSON and save it as {name: value}.

    Shared by every cookie-based session setter. Accepts either a JSON object
    ({"cookie_name": "value", ...}) or a Cookie Editor / EditThisCookie array
    ([{"name": ..., "value": ...}, ...]); saves to the path in ``env_var``,
    falling back to ``default_filename`` inside ``default_data_dir()`` (the
    DB's writable directory — a mounted ``/data`` volume in production) so a
    relative ``data/`` that the non-root container process can't create never
    triggers ``PermissionError: [Errno 13] Permission denied: 'data'``.
    """
    try:
        raw = json.loads(cookies)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Invalid JSON: {exc}") from exc

    if isinstance(raw, list):
        normalized = {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}
    elif isinstance(raw, dict):
        normalized = raw
    else:
        raise ToolError("Expected a JSON object or array")

    if not normalized:
        raise ToolError("No valid cookies found in input")

    path = os.getenv(env_var) or str(default_data_dir() / default_filename)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2)

    logger.info("%s session cookies saved to %s (%d cookies)", label, path, len(normalized))
    return {"cookie_count": len(normalized), "path": path}


async def set_nintendo_session(cookies: str) -> dict:
    """
    Store Nintendo Account session cookies (accounts.nintendo.com).

    This one login session drives BOTH Switch ownership sync (VGCS) AND eShop
    purchase-history import (import_purchases sources=["eshop"], via a silent
    OAuth handshake) — no separate cookie export for purchases.

    Accepts either:
    - A JSON object: {"cookie_name": "value", ...}
    - A JSON array (Cookie Editor / EditThisCookie format):
      [{"name": "...", "value": "..."}, ...]

    How to get your cookies:
    1. Open https://accounts.nintendo.com/portal/vgcs/ in your browser
    2. Install the "Cookie Editor" browser extension
    3. Click the extension icon → Export → copy the JSON
    4. Pass that JSON string to this tool

    Cookies are saved to the path in NINTENDO_COOKIES_FILE
    (defaults to nintendo_cookies.json beside the database).
    """
    return _save_session_cookies(
        cookies, "NINTENDO_COOKIES_FILE", "nintendo_cookies.json", "Nintendo"
    )


async def set_nintendo_ec_session(cookies: str) -> dict:
    """
    Store ec.nintendo.com session cookies for eShop purchase-history import.

    Accepts the same JSON shapes as set_nintendo_session (object or Cookie
    Editor array). These cookies are separate from the VGCS ones — they must
    come from the ec.nintendo.com domain, and the export MUST include the
    ``__Secure-next-auth.session-token`` cookie (the importer exchanges it for a
    short-lived account token; without it the import fails immediately).

    How to get your cookies:
    1. Open https://ec.nintendo.com/my/transactions/ in your browser (logged in)
    2. Install the "Cookie Editor" browser extension
    3. Click the extension icon → Export → copy the JSON (export everything on
       the page; it will include __Secure-next-auth.session-token)
    4. Pass that JSON string to this tool

    Cookies are saved to the path in NINTENDO_EC_COOKIES_FILE
    (defaults to nintendo_ec_cookies.json beside the database).
    """
    return _save_session_cookies(
        cookies, "NINTENDO_EC_COOKIES_FILE", "nintendo_ec_cookies.json", "Nintendo eShop"
    )


async def set_humble_session(cookies: str) -> dict:
    """
    Store Humble Bundle session cookies for purchase-history import.

    Accepts the same JSON shapes as set_nintendo_session (object or Cookie
    Editor array). Only the ``_simpleauth_sess`` cookie is strictly needed,
    but exporting/storing all humblebundle.com cookies is fine.

    How to get your cookies:
    1. Open https://www.humblebundle.com/ in your browser (logged in)
    2. Install the "Cookie Editor" browser extension
    3. Click the extension icon → Export → copy the JSON
    4. Pass that JSON string to this tool

    Cookies are saved to the path in HUMBLE_COOKIES_FILE
    (defaults to humble_cookies.json beside the database).
    """
    return _save_session_cookies(
        cookies, "HUMBLE_COOKIES_FILE", "humble_cookies.json", "Humble Bundle"
    )


async def set_steam_store_session(cookies: str) -> dict:
    """
    Store Steam store session cookies for purchase-history import.

    Accepts the same JSON shapes as set_nintendo_session (object or Cookie
    Editor array). Only the ``steamLoginSecure`` cookie is strictly required;
    ``sessionid`` is recommended too (the history load-more endpoint wants
    it). These store.steampowered.com cookies are unrelated to STEAM_API_KEY.

    How to get your cookies:
    1. Open https://store.steampowered.com/account/ in your browser (logged in)
    2. Install the "Cookie Editor" browser extension
    3. Click the extension icon → Export → copy the JSON
    4. Pass that JSON string to this tool

    Cookies are saved to the path in STEAM_STORE_COOKIES_FILE
    (defaults to steam_store_cookies.json beside the database).
    """
    return _save_session_cookies(
        cookies, "STEAM_STORE_COOKIES_FILE", "steam_store_cookies.json", "Steam store"
    )


# Holds the PKCE code_verifier between the two set_nintendo_pctl_session calls.
# The verifier that generated the login URL must be the one used to exchange the
# pasted code, so it has to survive across the (interactive) gap.
_PENDING_PCTL_LOGIN: dict[str, str] = {}


async def set_nintendo_pctl_session(response: str = "") -> dict:
    """
    Set up Nintendo Switch Parental Controls playtime sync (no `f` token needed).

    The Parental Controls API reports per-game playtime for any console registered
    to Parental Controls, regardless of which account owns the game — so games
    played on your console under another account show up too. This is the playtime
    source for switch2 (VGCS provides ownership).

    Two-step flow (the server can't open a browser):
    1. Call with no argument → returns a `login_url`. Open it, sign in to your
       Nintendo account, right-click "Select this person" and copy the link.
    2. Call again with that `npf…://auth` link (or a bare session token) → the
       session token is stored for playtime sync.

    Saved to NINTENDO_PCTL_SESSION_FILE (defaults to nintendo_pctl_session.json
    beside the database).
    """
    import aiohttp
    from pynintendoparental.authenticator import Authenticator

    from ..data.nintendo_pctl import _token_file_path

    text = (response or "").strip()
    async with aiohttp.ClientSession() as session:
        if not text:
            auth = Authenticator(client_session=session)
            _PENDING_PCTL_LOGIN["verifier"] = auth._auth_code_verifier
            return {
                "status": "awaiting_login",
                "login_url": auth.login_url,
                "instructions": (
                    "Open login_url, sign in, right-click 'Select this person' and copy "
                    "the link, then call set_nintendo_pctl_session again with that "
                    "npf://auth link."
                ),
            }

        if "session_token_code" in text or text.startswith("npf"):
            auth = Authenticator(client_session=session)
            verifier = _PENDING_PCTL_LOGIN.get("verifier")
            if verifier:
                auth._auth_code_verifier = verifier
            try:
                await auth.async_complete_login(response_token=text)
            except Exception as exc:
                raise ToolError(
                    f"Parental Controls login failed: {exc}. Re-run with no argument "
                    "to get a fresh login URL, then paste the link promptly."
                ) from exc
            token = auth._session_token
            _PENDING_PCTL_LOGIN.pop("verifier", None)
        else:
            token = text  # treat as a bare session token

    if not token:
        raise ToolError("No session token obtained")

    path = _token_file_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"session_token": token}, f, indent=2)

    logger.info("Nintendo Parental Controls session token saved to %s", path)
    return {"status": "stored", "path": path}


async def merge_games(
    source_game_id: int,
    target_game_id: int,
    dry_run: bool = False,
) -> dict:
    """
    Merge one game row into another and delete the source.

    Transfers all platform ownership rows (re-pointing or merging into an
    existing target platform), platform identifiers, enrichment, ratings, series
    memberships, and game aliases from source to target in a single atomic
    transaction. When both games own the same platform, identifiers are
    re-pointed to the target row, playtime is set to the higher of the two
    values, and the source platform row is deleted. Ratings for the same source
    are kept on the target if already present; otherwise they are moved.

    Use this to consolidate PSN/localized duplicate rows that were ingested
    before the English title resolver existed. After merging, the source
    game_id is deleted.

    dry_run=True previews what would change without committing anything.
    Returns a summary dict with moved/merged counts for each data type.
    """
    if source_game_id == target_game_id:
        raise ToolError("source_game_id and target_game_id must differ")

    async with get_db() as db:
        source_row = await db.execute_fetchone(
            "SELECT id, name FROM games WHERE id = ?", (source_game_id,)
        )
        target_row = await db.execute_fetchone(
            "SELECT id, name FROM games WHERE id = ?", (target_game_id,)
        )
        if source_row is None:
            raise ToolError(f"Source game {source_game_id} not found")
        if target_row is None:
            raise ToolError(f"Target game {target_game_id} not found")

        acquisition_cols = ", ".join(ACQUISITION_FIELDS)
        source_platforms = await db.execute_fetchall(
            f"""SELECT id, platform, playtime_minutes, owned, last_played, {acquisition_cols}
                FROM game_platforms WHERE game_id = ?""",
            (source_game_id,),
        )

        platforms_moved: list[str] = []
        platforms_merged: list[str] = []

        for sp in source_platforms:
            sp_id: int = sp["id"]
            platform: str = sp["platform"]
            target_platform = await db.execute_fetchone(
                f"""SELECT id, playtime_minutes, last_played, owned, {acquisition_cols}
                    FROM game_platforms WHERE game_id = ? AND platform = ?""",
                (target_game_id, platform),
            )

            if not dry_run:
                if target_platform is None:
                    # Re-pointing the whole row carries its acquisition
                    # columns (and everything else) along untouched.
                    await db.execute(
                        "UPDATE game_platforms SET game_id = ? WHERE id = ?",
                        (target_game_id, sp_id),
                    )
                    platforms_moved.append(platform)
                else:
                    tp_id: int = target_platform["id"]
                    # Keep better playtime on target
                    src_mins = sp["playtime_minutes"] or 0
                    tgt_mins = target_platform["playtime_minutes"] or 0
                    if src_mins > tgt_mins:
                        await db.execute(
                            "UPDATE game_platforms SET playtime_minutes = ? WHERE id = ?",
                            (src_mins, tp_id),
                        )
                    # Keep most-recent last_played
                    src_lp = sp["last_played"]
                    tgt_lp = target_platform["last_played"]
                    if src_lp and (not tgt_lp or src_lp > tgt_lp):
                        await db.execute(
                            "UPDATE game_platforms SET last_played = ? WHERE id = ?",
                            (src_lp, tp_id),
                        )
                    # Don't silently drop ownership the source had (e.g. target was
                    # a manual add_game_to_platform stub with owned=0).
                    src_owned = sp["owned"]
                    tgt_owned = target_platform["owned"] or 0
                    if src_owned and not tgt_owned:
                        await db.execute(
                            "UPDATE game_platforms SET owned = 1 WHERE id = ?",
                            (tp_id,),
                        )
                    # Acquisition data would be silently dropped with the source
                    # row's DELETE below: fill each target column that is NULL
                    # from the source (target wins on conflict — matches the
                    # merge's keep-target philosophy).
                    acq_updates = {
                        col: sp[col]
                        for col in ACQUISITION_FIELDS
                        if target_platform[col] is None and sp[col] is not None
                    }
                    if acq_updates:
                        acq_sql = ", ".join(f"{col} = ?" for col in acq_updates)
                        await db.execute(
                            f"UPDATE game_platforms SET {acq_sql} WHERE id = ?",
                            (*acq_updates.values(), tp_id),
                        )
                    # Move identifiers: UPDATE OR IGNORE keeps target row on unique conflict
                    await db.execute(
                        """UPDATE OR IGNORE game_platform_identifiers
                              SET game_platform_id = ?
                            WHERE game_platform_id = ?""",
                        (tp_id, sp_id),
                    )
                    # Move enrichment only if target has none
                    has_target_enrichment = await db.execute_fetchone(
                        "SELECT 1 FROM game_platform_enrichment WHERE game_platform_id = ?",
                        (tp_id,),
                    )
                    has_source_enrichment = await db.execute_fetchone(
                        "SELECT 1 FROM game_platform_enrichment WHERE game_platform_id = ?",
                        (sp_id,),
                    )
                    if has_source_enrichment and not has_target_enrichment:
                        await db.execute(
                            "UPDATE game_platform_enrichment SET game_platform_id = ? WHERE game_platform_id = ?",
                            (tp_id, sp_id),
                        )
                    # Delete source platform row (cascade cleans remaining identifiers/enrichment/steam_platform_data)
                    await db.execute("DELETE FROM game_platforms WHERE id = ?", (sp_id,))
                    platforms_merged.append(platform)
            else:
                if target_platform is None:
                    platforms_moved.append(platform)
                else:
                    platforms_merged.append(platform)

        # Ratings — UNIQUE(game_id, source); keep target's if conflict
        source_ratings = await db.execute_fetchall(
            "SELECT source FROM ratings WHERE game_id = ?", (source_game_id,)
        )
        ratings_moved: list[str] = []
        ratings_kept_target: list[str] = []

        for r in source_ratings:
            src = r["source"]
            target_has = await db.execute_fetchone(
                "SELECT id FROM ratings WHERE game_id = ? AND source = ?",
                (target_game_id, src),
            )
            if not dry_run:
                if target_has is None:
                    await db.execute(
                        "UPDATE ratings SET game_id = ? WHERE game_id = ? AND source = ?",
                        (target_game_id, source_game_id, src),
                    )
                    ratings_moved.append(src)
                else:
                    await db.execute(
                        "DELETE FROM ratings WHERE game_id = ? AND source = ?",
                        (source_game_id, src),
                    )
                    ratings_kept_target.append(src)
            else:
                if target_has is None:
                    ratings_moved.append(src)
                else:
                    ratings_kept_target.append(src)

        # Series memberships — count only rows actually transferred (the target
        # may already share some), so both the live result and the dry-run
        # preview reflect what INSERT OR IGNORE would really add.
        source_series = await db.execute_fetchall(
            "SELECT series_id FROM game_series_membership WHERE game_id = ?",
            (source_game_id,),
        )
        series_transferred = 0
        for s in source_series:
            if not dry_run:
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO game_series_membership (game_id, series_id) VALUES (?, ?)",
                    (target_game_id, s["series_id"]),
                )
                series_transferred += cursor.rowcount
            else:
                existing = await db.execute_fetchone(
                    "SELECT 1 FROM game_series_membership WHERE game_id = ? AND series_id = ?",
                    (target_game_id, s["series_id"]),
                )
                if existing is None:
                    series_transferred += 1
        if not dry_run:
            await db.execute(
                "DELETE FROM game_series_membership WHERE game_id = ?", (source_game_id,)
            )

        # Game aliases — same accurate-count treatment. The dry-run check mirrors
        # the idx_game_aliases_unique columns so a preview never over-reports.
        source_aliases = await db.execute_fetchall(
            "SELECT alias, alias_normalized, alias_type, source, source_key FROM game_aliases WHERE game_id = ?",
            (source_game_id,),
        )
        aliases_transferred = 0
        for a in source_aliases:
            if not dry_run:
                cursor = await db.execute(
                    """INSERT OR IGNORE INTO game_aliases
                           (game_id, alias, alias_normalized, alias_type, source, source_key)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        target_game_id,
                        a["alias"],
                        a["alias_normalized"],
                        a["alias_type"],
                        a["source"],
                        a["source_key"],
                    ),
                )
                aliases_transferred += cursor.rowcount
            else:
                existing = await db.execute_fetchone(
                    """SELECT 1 FROM game_aliases
                        WHERE game_id = ? AND alias_normalized = ? AND alias_type = ?
                          AND COALESCE(source, '') = COALESCE(?, '')
                          AND COALESCE(source_key, '') = COALESCE(?, '')""",
                    (
                        target_game_id,
                        a["alias_normalized"],
                        a["alias_type"],
                        a["source"],
                        a["source_key"],
                    ),
                )
                if existing is None:
                    aliases_transferred += 1
        # play_history — keyed (game_id, platform, snapshot_date) with ON DELETE
        # CASCADE, so deleting the source game would silently drop its snapshot
        # history and get_play_history would underreport. Transfer rows to the
        # target first; on a same-day collision keep MAX(playtime_minutes) —
        # snapshots are cumulative totals of the same underlying game, so the
        # higher value is the more complete total (mirroring how the platform
        # merge above keeps the higher playtime_minutes).
        history_row = await db.execute_fetchone(
            "SELECT COUNT(*) AS c FROM play_history WHERE game_id = ?",
            (source_game_id,),
        )
        play_history_rows_transferred = history_row["c"] if history_row else 0
        if not dry_run and play_history_rows_transferred:
            await db.execute(
                """INSERT INTO play_history (game_id, platform, snapshot_date, playtime_minutes)
                   SELECT ?, platform, snapshot_date, playtime_minutes
                   FROM play_history WHERE game_id = ?
                   ON CONFLICT(game_id, platform, snapshot_date)
                       DO UPDATE SET playtime_minutes =
                           MAX(playtime_minutes, excluded.playtime_minutes)""",
                (target_game_id, source_game_id),
            )
            await db.execute(
                "DELETE FROM play_history WHERE game_id = ?", (source_game_id,)
            )

        if not dry_run:
            await db.execute("DELETE FROM game_aliases WHERE game_id = ?", (source_game_id,))
            await db.execute("DELETE FROM games WHERE id = ?", (source_game_id,))
            await db.commit()

    # Moving ratings shifts which games feed the taste profile, so recompute tag
    # affinity the same way rate_game/sync_ratings do — otherwise discover_games
    # ranks on stale scores until the next background pass. Outside the db
    # context manager since recompute opens its own connection.
    if not dry_run and (ratings_moved or ratings_kept_target):
        from ..data.db import recompute_tag_affinity
        await recompute_tag_affinity()

    return {
        "dry_run": dry_run,
        "source": {"game_id": source_game_id, "name": source_row["name"]},
        "target": {"game_id": target_game_id, "name": target_row["name"]},
        "platforms_moved": platforms_moved,
        "platforms_merged": platforms_merged,
        "ratings_moved": ratings_moved,
        "ratings_kept_target": ratings_kept_target,
        "series_memberships_transferred": series_transferred,
        "aliases_transferred": aliases_transferred,
        "play_history_rows_transferred": play_history_rows_transferred,
        "source_deleted": not dry_run,
    }


async def delete_game(
    name: str | None = None,
    game_id: int | None = None,
    confirm: bool = False,
) -> dict:
    """
    Permanently delete one game and all of its data. IRREVERSIBLE.

    Resolve the game with game_id or name (partial/fuzzy match — the resolved
    name is echoed back so you can confirm the right row), then remove it and
    every dependent record: platform ownership rows, store identifiers,
    provider enrichment, ratings, wishlist entries, price cache, play-history
    snapshots, series memberships, and aliases.

    Two-step by design: with confirm=False (the default) nothing is deleted —
    the call returns deleted=false plus a would_delete breakdown of the row
    counts that WOULD be removed, so you can verify before committing. Call
    again with confirm=True to actually delete.

    A game that is the parent of nested content (DLC/expansions) is refused
    (children are listed in the error): reparent or delete those children first
    with update_game/delete_game, so nothing is silently orphaned. To remove a
    duplicate that should be consolidated rather than erased, use merge_games
    instead — it preserves playtime and history on the surviving row.

    Returns the resolved game, whether it was deleted, and the per-table counts.
    """
    # Lazy import: platforms.py imports admin lazily elsewhere; keep this local
    # to avoid a top-level cycle, mirroring acquisition.py's usage.
    from .platforms import _resolve_game_row

    row = await _resolve_game_row(name, game_id)
    resolved_id = row["id"]
    resolved_name = row["name"]

    async with get_db() as db:
        children = await db.execute_fetchall(
            "SELECT id, name FROM games WHERE parent_game_id = ?", (resolved_id,)
        )
        if children:
            listed = ", ".join(f"{c['name']} (id {c['id']})" for c in children)
            raise ToolError(
                f"'{resolved_name}' (id {resolved_id}) is the parent of "
                f"{len(children)} nested item(s): {listed}. Reparent or delete "
                "them first (update_game/delete_game) so they are not orphaned."
            )

        # Count dependents for the preview / summary. game_platform_identifiers,
        # steam_platform_data, and game_platform_enrichment cascade from
        # game_platforms; game_wishlist/game_prices/play_history/
        # game_series_membership/game_aliases cascade from games.
        async def _count(sql: str) -> int:
            r = await db.execute_fetchone(sql, (resolved_id,))
            return r["c"] if r else 0

        would_delete = {
            "platforms": await _count(
                "SELECT COUNT(*) AS c FROM game_platforms WHERE game_id = ?"
            ),
            "ratings": await _count(
                "SELECT COUNT(*) AS c FROM ratings WHERE game_id = ?"
            ),
            "wishlist_entries": await _count(
                "SELECT COUNT(*) AS c FROM game_wishlist WHERE game_id = ?"
            ),
            "price_rows": await _count(
                "SELECT COUNT(*) AS c FROM game_prices WHERE game_id = ?"
            ),
            "play_history_rows": await _count(
                "SELECT COUNT(*) AS c FROM play_history WHERE game_id = ?"
            ),
            "series_memberships": await _count(
                "SELECT COUNT(*) AS c FROM game_series_membership WHERE game_id = ?"
            ),
            "aliases": await _count(
                "SELECT COUNT(*) AS c FROM game_aliases WHERE game_id = ?"
            ),
        }

        if not confirm:
            return {
                "deleted": False,
                "game_id": resolved_id,
                "name": resolved_name,
                "would_delete": would_delete,
                "hint": "Re-run with confirm=True to permanently delete.",
            }

        # ratings and game_platforms do NOT cascade from games (no ON DELETE
        # action on their FKs), so delete them explicitly before the games row —
        # deleting game_platforms first cascades its identifier/enrichment/
        # steam_platform_data children. The remaining child tables cascade on
        # the final games delete, and the games_fts_ad trigger cleans the index.
        await db.execute("DELETE FROM ratings WHERE game_id = ?", (resolved_id,))
        await db.execute("DELETE FROM game_platforms WHERE game_id = ?", (resolved_id,))
        await db.execute("DELETE FROM games WHERE id = ?", (resolved_id,))
        await db.commit()

    # A deleted game changes which games feed the taste profile — not only via
    # its explicit ratings, but also the low-weight playtime pseudo-rating
    # recompute_tag_affinity folds in for owned/non-farmed/unrated/>=2h games.
    # So recompute unconditionally after a confirmed delete (deletes are rare
    # admin ops) rather than gating on ratings, which would leave an unrated but
    # played game's taste signal skewing discover_games until an unrelated pass.
    from ..data.db import recompute_tag_affinity
    await recompute_tag_affinity()

    return {
        "deleted": True,
        "game_id": resolved_id,
        "name": resolved_name,
        "deleted_counts": would_delete,
    }


async def detect_farmed_games(
    dry_run: bool = True,
    threshold_hours: float = 8.0,
    min_games_per_day: int = 8,
) -> dict:
    """
    Auto-detect ArchiSteamFarm card-farming sessions and mark games as is_farmed.

    Algorithm:
    1. Find Steam games with rtime_last_played set and low playtime.
    2. Group by date; days with >= min_games_per_day games are "farming days".
    3. All Steam games last played on those days are candidates.
    4. If dry_run=False, marks their canonical game rows is_farmed=1.
    """
    threshold_minutes = int(threshold_hours * 60)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT g.id AS game_id,
                      g.name,
                      CAST(gpi.identifier_value AS INTEGER) AS appid,
                      COALESCE(gp.playtime_minutes, 0) AS playtime_forever,
                      spd.rtime_last_played,
                      date(spd.rtime_last_played, 'unixepoch') AS last_played_date
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               JOIN game_platform_identifiers gpi
                 ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
               LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
               WHERE spd.rtime_last_played IS NOT NULL
                 AND COALESCE(gp.playtime_minutes, 0) > 0
                 AND COALESCE(gp.playtime_minutes, 0) <= ?""",
            (STEAM_APP_ID, threshold_minutes),
        )

    by_date: dict[str, list] = defaultdict(list)
    for row in rows:
        by_date[row["last_played_date"]].append(row)

    farming_days = []
    candidate_game_ids: set[int] = set()
    candidate_appids: set[int] = set()
    for date, games in sorted(by_date.items()):
        if len(games) >= min_games_per_day:
            playtimes = [game["playtime_forever"] / 60 for game in games]
            farming_days.append(
                {
                    "date": date,
                    "game_count": len(games),
                    "median_playtime_hours": round(statistics.median(playtimes), 2),
                }
            )
            for game in games:
                candidate_game_ids.add(game["game_id"])
                candidate_appids.add(game["appid"])

    sample: list[dict] = []
    for row in rows:
        if row["game_id"] in candidate_game_ids and len(sample) < 10:
            sample.append(
                {
                    "game_id": row["game_id"],
                    "appid": row["appid"],
                    "name": row["name"],
                    "playtime_hours": round(row["playtime_forever"] / 60, 2),
                    "last_played": row["last_played_date"],
                }
            )

    if not dry_run and candidate_game_ids:
        placeholders = ",".join("?" * len(candidate_game_ids))
        async with get_db() as db:
            # Respect a manual is_farmed value set via update_game (e.g. a user
            # un-farming a false positive). json_each decouples the guard from
            # manual_overrides' JSON serialization format (json_each(NULL) yields
            # no rows, so the IS NULL clause is belt-and-suspenders).
            await db.execute(
                f"""UPDATE games SET is_farmed = 1
                    WHERE id IN ({placeholders})
                      AND (manual_overrides IS NULL
                           OR 'is_farmed' NOT IN (SELECT value FROM json_each(manual_overrides)))""",
                list(candidate_game_ids),
            )
            await db.commit()

    return {
        "farming_days": farming_days,
        "candidates": len(candidate_game_ids),
        "steam_appids": sorted(candidate_appids),
        "threshold_hours": threshold_hours,
        "dry_run": dry_run,
        "sample_games": sample,
    }


async def detect_collapsed_games() -> dict:
    """Surface games that were over-merged by name into a single row.

    The fingerprint of an over-merge is one platform row carrying more than one
    distinct store identifier of the same type — e.g. a single "Dead Space" game
    holding two ``steam_appid`` values (the 2008 original and the 2023 remake).
    Read-only: it lists candidates for manual review; cleanup is left to the user
    (re-sync after the resolution fix, or a hand edit). No automatic split is
    attempted because commingled playtime cannot be reliably re-attributed.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT g.id AS game_id,
                      g.name,
                      gp.platform,
                      gpi.identifier_type,
                      COUNT(DISTINCT gpi.identifier_value) AS identifier_count,
                      GROUP_CONCAT(DISTINCT gpi.identifier_value) AS identifier_values
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id
               JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
               WHERE gpi.identifier_type IN
                     ('steam_appid', 'epic_artifact_id', 'psn_title_id', 'nintendo_title_id')
               GROUP BY gp.id, gpi.identifier_type
               HAVING COUNT(DISTINCT gpi.identifier_value) > 1
               ORDER BY identifier_count DESC, g.name""",
        )

    candidates = [
        {
            "game_id": row["game_id"],
            "name": row["name"],
            "platform": row["platform"],
            "identifier_type": row["identifier_type"],
            "identifier_count": row["identifier_count"],
            "identifier_values": (row["identifier_values"] or "").split(","),
        }
        for row in rows
    ]
    return {"collapsed_count": len(candidates), "candidates": candidates}


async def split_game(
    source_game_id: int,
    platform: str,
    identifier_values: list[str],
    new_name: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Split store identifiers off an over-merged game into a new game row.

    The inverse of ``merge_games``: it peels the given ``identifier_values`` (on
    ``platform``) out of ``source_game_id`` and attaches them to a freshly created
    game. Two shapes are handled in one operation:

    * Whole-platform split (cross-platform collapse, e.g. Dead Space = Steam 2008 +
      PS5 2023): when the peeled values are *all* the identifiers on the source's
      platform row, that ``game_platforms`` row is simply re-pointed to the new
      game, carrying its identifiers, enrichment, steam_platform_data and playtime.
    * Subset split (within-platform collapse, e.g. one Steam row holding two
      appids): a new ``game_platforms`` row is created under the new game and only
      the named identifiers move to it; per-platform enrichment stays on the source
      row and playtime re-populates per identifier on the next sync (Steam reports
      per-appid playtime, so the split is lossless after a re-sync).

    Game-level rows (ratings, name, igdb_id, tags) stay on the source. The new game
    starts unenriched so background IGDB backfill re-resolves it; set a distinct
    ``new_name`` (e.g. "Dead Space (2023)") so it does not re-resolve onto the
    source's contaminated identity. ``dry_run=True`` previews without writing.
    """
    if not identifier_values:
        raise ToolError("identifier_values must be non-empty")

    async with get_db() as db:
        source_row = await db.execute_fetchone(
            "SELECT id, name FROM games WHERE id = ?", (source_game_id,)
        )
        if source_row is None:
            raise ToolError(f"Source game {source_game_id} not found")

        platform_row = await db.execute_fetchone(
            "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
            (source_game_id, platform),
        )
        if platform_row is None:
            raise ToolError(f"Game {source_game_id} has no {platform!r} platform row")
        source_platform_id = platform_row["id"]

        all_identifiers = await db.execute_fetchall(
            "SELECT id, identifier_type, identifier_value FROM game_platform_identifiers "
            "WHERE game_platform_id = ?",
            (source_platform_id,),
        )
        owned_values = {row["identifier_value"] for row in all_identifiers}
        requested = set(map(str, identifier_values))
        missing = requested - owned_values
        if missing:
            raise ToolError(
                f"{platform!r} row of game {source_game_id} does not own identifier(s): "
                f"{sorted(missing)}"
            )
        if requested == owned_values and len(owned_values) == 1:
            # The platform row exists only for these identifiers and would be left
            # empty — moving the whole row is the clean, lossless path.
            move_whole_platform = True
        else:
            move_whole_platform = requested == owned_values
        remaining = sorted(owned_values - requested)
        target_name = new_name or source_row["name"]

        if dry_run:
            return {
                "source_game_id": source_game_id,
                "source_name": source_row["name"],
                "new_game_id": None,
                "new_name": target_name,
                "platform": platform,
                "identifiers_moved": sorted(requested),
                "moved_whole_platform": move_whole_platform,
                "identifiers_remaining_on_source": remaining,
                "dry_run": True,
            }

        cursor = await db.execute(
            "INSERT INTO games (name, name_normalized) VALUES (?, ?)",
            (target_name, normalize_search_text(target_name)),
        )
        new_game_id = cursor.lastrowid

        play_history_rows_moved = 0
        if move_whole_platform:
            await db.execute(
                "UPDATE game_platforms SET game_id = ? WHERE id = ?",
                (new_game_id, source_platform_id),
            )
            # The platform relationship now belongs to the new game, so its
            # snapshot history follows — otherwise get_play_history would keep
            # attributing this platform's playtime to the source game. No
            # collision possible: the new game was just created. In the subset
            # split below, history deliberately stays on the source: snapshots
            # are per-(game, platform), not per-identifier, so past totals
            # can't be attributed to the peeled identifier (the same reason
            # the platform row's playtime stays put and re-syncs).
            cursor = await db.execute(
                "UPDATE play_history SET game_id = ? WHERE game_id = ? AND platform = ?",
                (new_game_id, source_game_id, platform),
            )
            play_history_rows_moved = cursor.rowcount
        else:
            now = datetime.now(timezone.utc).isoformat()
            cursor = await db.execute(
                """INSERT INTO game_platforms (game_id, platform, owned, last_synced)
                   VALUES (?, ?, 1, ?)""",
                (new_game_id, platform, now),
            )
            new_platform_id = cursor.lastrowid
            await db.executemany(
                "UPDATE game_platform_identifiers SET game_platform_id = ? WHERE id = ?",
                [
                    (new_platform_id, row["id"])
                    for row in all_identifiers
                    if row["identifier_value"] in requested
                ],
            )
        await db.commit()

    return {
        "source_game_id": source_game_id,
        "source_name": source_row["name"],
        "new_game_id": new_game_id,
        "new_name": target_name,
        "platform": platform,
        "identifiers_moved": sorted(requested),
        "moved_whole_platform": move_whole_platform,
        "identifiers_remaining_on_source": remaining,
        "play_history_rows_moved": play_history_rows_moved,
        "dry_run": False,
    }


async def detect_orphan_games() -> dict:
    """Find primary-library games rows with no ownership and no wishlist entry.

    ``is_primary_library_item`` is a content-type flag (real game vs
    DLC/soundtrack/edition) — it says nothing about ownership. A games row can
    legitimately exist with zero ``game_platforms`` rows in two shapes:

    * wishlist-only (a ``game_wishlist`` row exists) — a normal, intentional
      shape produced by ``sync_wishlist``/``add_game_to_platform(owned=False)``.
      Counted in ``wishlist_only_count`` but not returned as a candidate.
    * a true orphan (no ``game_platforms`` row AND no ``game_wishlist`` row) —
      e.g. a wishlist entry that was later removed upstream
      (``delete_stale_wishlist_entries``) without ever being owned, leaving the
      ``games`` row dangling with nothing pointing at it. These are returned in
      ``orphans`` for review; no write happens (use ``delete_game``'s two-step
      confirm for genuine phantoms, or ``merge_games`` to consolidate — a
      false positive would silently destroy a game row and its ratings/series
      links).

    CAUTION — an "orphan" can be a RETIRED STEAM APP THE ACCOUNT STILL OWNS:
    GetOwnedGames omits some delisted apps, so the game never got a platform
    row while its games row survived (observed in prod: Burnout Paradise,
    75 rows). Run ``audit_steam_licenses`` (or a refresh with a Steam store
    session stored) BEFORE deleting anything here: the audit mints owned rows
    for retired licenses, and any orphan that is really owned drops out of
    this list on its own. ``license_audit`` reports whether a store session is
    stored and, from the last audit run, how many owned licenses were still
    unclassified — non-zero means this orphan list is not yet trustworthy.
    """
    from ..data.db import get_meta
    from ..data.steam_licenses import (
        AUDIT_REMAINING_META_KEY,
        is_license_audit_configured,
    )

    async with get_db() as db:
        orphan_rows = await db.execute_fetchall(
            """SELECT g.id AS game_id, g.name, g.igdb_id
               FROM games g
               WHERE g.is_primary_library_item = 1
                 AND NOT EXISTS (SELECT 1 FROM game_platforms gp WHERE gp.game_id = g.id)
                 AND NOT EXISTS (SELECT 1 FROM game_wishlist w WHERE w.game_id = g.id)
               ORDER BY g.id"""
        )
        wishlist_only_row = await db.execute_fetchone(
            """SELECT COUNT(*) AS c
               FROM games g
               WHERE g.is_primary_library_item = 1
                 AND NOT EXISTS (SELECT 1 FROM game_platforms gp WHERE gp.game_id = g.id)
                 AND EXISTS (SELECT 1 FROM game_wishlist w WHERE w.game_id = g.id)"""
        )

    orphans = [
        {
            "game_id": row["game_id"],
            "name": row["name"],
            "igdb_id": row["igdb_id"],
        }
        for row in orphan_rows
    ]
    remaining_raw = await get_meta(AUDIT_REMAINING_META_KEY)
    return {
        "orphans": orphans,
        "orphan_count": len(orphans),
        "wishlist_only_count": wishlist_only_row["c"] if wishlist_only_row else 0,
        "license_audit": {
            "configured": is_license_audit_configured(),
            # None = the audit has never run; run audit_steam_licenses first.
            "unclassified_at_last_run": (
                int(remaining_raw) if remaining_raw is not None else None
            ),
        },
    }


async def detect_stranded_duplicates() -> dict:
    """List same-name game pairs where a sync forked a stranded duplicate row.

    The fingerprint: two games rows share a normalized name and an owned
    platform, and exactly one side's platform row carries store identifiers —
    the identifier-less twin was ingested before that identifier type was
    recorded, and a later sync (whose identifier lookup missed) refused to
    attach onto it (anti-collapse guard) and created a fresh row instead.
    The sync paths now adopt the identifier onto such rows, so new pairs
    should not appear; existing ones are merge_games candidates. Read-only.
    Pairs where BOTH sides carry identifiers are deliberately excluded — those
    are distinct store entries (see detect_collapsed_games for the inverse
    over-merge shape).
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT ga.id   AS game_id,
                      gb.id   AS duplicate_game_id,
                      ga.name AS name,
                      gb.name AS duplicate_name,
                      gpa.platform,
                      gpa.playtime_minutes AS playtime_minutes,
                      gpb.playtime_minutes AS duplicate_playtime_minutes,
                      (SELECT GROUP_CONCAT(gpi.identifier_type || '=' || gpi.identifier_value)
                       FROM game_platform_identifiers gpi
                       WHERE gpi.game_platform_id = gpa.id) AS identifiers
               FROM games ga
               JOIN games gb
                 ON gb.id != ga.id
                AND COALESCE(gb.name_normalized, '') = COALESCE(ga.name_normalized, '')
                AND ga.name_normalized IS NOT NULL
               JOIN game_platforms gpa ON gpa.game_id = ga.id AND gpa.owned = 1
               JOIN game_platforms gpb
                 ON gpb.game_id = gb.id AND gpb.platform = gpa.platform AND gpb.owned = 1
               WHERE EXISTS (SELECT 1 FROM game_platform_identifiers gpi
                             WHERE gpi.game_platform_id = gpa.id)
                 AND NOT EXISTS (SELECT 1 FROM game_platform_identifiers gpi
                                 WHERE gpi.game_platform_id = gpb.id)
               ORDER BY ga.name, gpa.platform""",
        )

    candidates = [
        {
            "game_id": row["game_id"],
            "name": row["name"],
            "duplicate_game_id": row["duplicate_game_id"],
            "duplicate_name": row["duplicate_name"],
            "platform": row["platform"],
            "playtime_minutes": row["playtime_minutes"],
            "duplicate_playtime_minutes": row["duplicate_playtime_minutes"],
            "identifiers": (row["identifiers"] or "").split(",") if row["identifiers"] else [],
        }
        for row in rows
    ]
    return {"stranded_count": len(candidates), "candidates": candidates}


async def detect_cross_platform_collapses(limit: int = 0) -> dict:
    """Flag multi-platform games whose Steam appid is a *different* IGDB game.

    detect_collapsed_games finds one platform row holding several store IDs; this
    finds the cross-platform case where a single row merged two editions across
    stores (e.g. Steam appid 17470 = Dead Space 2008 sitting on the same row as the
    PS5 2023 remake). For each multi-platform game that has a Steam appid and a
    stored ``igdb_id``, it asks IGDB which game that appid actually is; a mismatch
    against the row's ``igdb_id`` means the Steam side does not belong here. Pure
    read (queries IGDB, no writes); resolve a hit with ``split_game``.
    """
    from ..data.igdb import (
        fetch_igdb_game_names,
        igdb_credentials_configured,
        resolve_steam_appids_to_igdb,
    )

    igdb_configured = igdb_credentials_configured()

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT g.id AS game_id,
                      g.name,
                      g.igdb_id AS row_igdb_id,
                      gpi.identifier_value AS steam_appid
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               JOIN game_platform_identifiers gpi
                 ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
               WHERE g.igdb_id IS NOT NULL
                 AND (SELECT COUNT(*) FROM game_platforms gp2 WHERE gp2.game_id = g.id) > 1
               ORDER BY g.id""",
            (STEAM_APP_ID,),
        )

    if limit and limit > 0:
        rows = rows[:limit]

    if not igdb_configured or not rows:
        return {
            "checked": 0,
            "collapsed_count": 0,
            "candidates": [],
            "igdb_configured": igdb_configured,
        }

    appid_to_igdb = await resolve_steam_appids_to_igdb([r["steam_appid"] for r in rows])

    flagged = []
    for row in rows:
        true_igdb = appid_to_igdb.get(str(row["steam_appid"]))
        if true_igdb is not None and true_igdb != row["row_igdb_id"]:
            flagged.append(row)

    # Resolve names for the (small) flagged set so the report is human-readable.
    names = await fetch_igdb_game_names(
        [r["row_igdb_id"] for r in flagged]
        + [appid_to_igdb[str(r["steam_appid"])] for r in flagged]
    )

    candidates = []
    for row in flagged:
        steam_true_igdb = appid_to_igdb[str(row["steam_appid"])]
        candidates.append(
            {
                "game_id": row["game_id"],
                "name": row["name"],
                "steam_appid": row["steam_appid"],
                "row_igdb_id": row["row_igdb_id"],
                "row_igdb_name": names.get(row["row_igdb_id"]),
                "steam_true_igdb_id": steam_true_igdb,
                "steam_true_igdb_name": names.get(steam_true_igdb),
            }
        )

    return {
        "checked": len(rows),
        "collapsed_count": len(candidates),
        "candidates": candidates,
        "igdb_configured": igdb_configured,
    }


# Name patterns that betray addon content misfiled as a primary base_game.
# Case-insensitive; "dlc" matches only as a whole word so "Half-Life" and
# friends never trip it. soundtrack/artbook are noise in game counts with no
# real parent gameplay, so they map to unknown_addon; the rest map to dlc.
_ADDON_NAME_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"season pass", re.IGNORECASE), CONTENT_DLC, "season pass"),
    (re.compile(r"expansion pass", re.IGNORECASE), CONTENT_DLC, "expansion pass"),
    (re.compile(r"soundtrack", re.IGNORECASE), CONTENT_UNKNOWN_ADDON, "soundtrack"),
    (re.compile(r"upgrade pack", re.IGNORECASE), CONTENT_DLC, "upgrade pack"),
    (re.compile(r"\bdlc\b", re.IGNORECASE), CONTENT_DLC, "dlc"),
    (re.compile(r"bonus content", re.IGNORECASE), CONTENT_DLC, "bonus content"),
    (re.compile(r"character pass", re.IGNORECASE), CONTENT_DLC, "character pass"),
    (re.compile(r"cosmetic", re.IGNORECASE), CONTENT_DLC, "cosmetic"),
    (re.compile(r"costume pack", re.IGNORECASE), CONTENT_DLC, "costume pack"),
    (re.compile(r"art\s?book", re.IGNORECASE), CONTENT_UNKNOWN_ADDON, "artbook"),
)

_MISCLASSIFIED_BUCKET_CAP = 200


def _match_addon_name(name: str | None) -> tuple[str, str] | None:
    """First addon-ish name pattern hit → (suggested content_type, label)."""
    for pattern, content_type, label in _ADDON_NAME_PATTERNS:
        if pattern.search(name or ""):
            return content_type, label
    return None


def _pinned_columns(raw) -> set[str]:
    """Parse a games.manual_overrides JSON blob into a set of column names.

    Duplicates data/db/upserts.py::_decode_overrides (private, per-connection
    API) for rows already loaded in bulk — keep the two in sync if the
    manual_overrides encoding ever changes.
    """
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    return set(data) if isinstance(data, list) else set()


async def _resolve_primary_parent(
    candidate_names, exclude_game_id: int, *, steam_appid: int | None = None
) -> tuple[int, str] | None:
    """First candidate that resolves to an existing PRIMARY library game.

    Tries the Steam ``steam_appid`` identifier first (when given), then each name
    in ``candidate_names`` in order, via resolve_parent_game(create=False) — so a
    parent is never minted. A resolved row is returned only when it is a primary
    library item and is not the child itself. Returns (parent_game_id,
    parent_name) or None.
    """
    from ..data.db import resolve_parent_game

    async def _primary(parent_id: int | None) -> tuple[int, str] | None:
        if parent_id is None or parent_id == exclude_game_id:
            return None
        async with get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id, name, is_primary_library_item FROM games WHERE id = ?",
                (parent_id,),
            )
        if row is not None and row["is_primary_library_item"]:
            return row["id"], row["name"]
        return None

    if steam_appid is not None:
        found = await _primary(
            await resolve_parent_game(
                None, steam_appid=steam_appid, exclude_game_id=exclude_game_id
            )
        )
        if found is not None:
            return found

    for candidate in candidate_names:
        if not candidate:
            continue
        found = await _primary(
            await resolve_parent_game(
                candidate, exclude_game_id=exclude_game_id, create=False
            )
        )
        if found is not None:
            return found
    return None


async def _fetch_steam_appdetails(appid: int) -> dict | None:
    """Fetch one Steam app's appdetails ``data`` payload (type/fullgame).

    Store-only (no review half) through steam_store's rate-gated fetch path —
    the probe has no use for reviews and every request costs a slot on the
    shared quota-budgeted gate. Module-level so the DLC probe's only network call
    can be patched in tests (gamelib_mcp.tools.admin._fetch_steam_appdetails).
    """
    from ..data.steam_store import fetch_store_appdetails

    return await fetch_store_appdetails(appid)


async def detect_misclassified_dlc(
    limit: int = 25, probe_steam: bool = True, probe_offset: int = 0
) -> dict:
    """Surface primary rows that are really nested content (DLC/soundtrack/etc).

    Read-only detector powering the human-confirmed repair loop: each candidate
    carries a ``suggested_update`` that is a ready-to-apply set of update_game
    kwargs. It NEVER writes and never mints parent rows. Buckets (a row lands in
    its first matching bucket only — order: inconsistent_primary_nested,
    nested_parent, needs_parent, purchase_minted_suspect, addon_name_pattern):

    * inconsistent_primary_nested — a row whose content_type is a NESTED value
      (dlc/expansion/edition/…) yet is_primary_library_item is still 1: an
      internally contradictory shape no current writer produces (is_primary is
      always derived from content_type), left behind by older writers. A row
      with real substance (store identifier or playtime) suggests promotion to
      base_game; an insubstantial row suggests re-applying its nested
      content_type (update_game re-derives is_primary), plus a parent when one
      resolves. Rows whose content_type is a manual override are skipped.
    * nested_parent — a nested row (is_primary_library_item=0) that other rows
      nest under: the parent is hidden by the is_primary filter and its children
      are reachable only through it, so both fall out of the library. Suggests
      content_type base_game (update_game promotes the row and clears its own
      parent). Checked ahead of needs_parent — that suggestion would deepen the
      chain rather than repair it.
    * needs_parent — a nested row (is_primary_library_item=0) with no
      parent_game_id. When a split-title candidate resolves to an existing
      primary game, the suggestion sets parent_game_id; otherwise it is null.
    * purchase_minted_suspect — a primary base_game with no store identifiers, a
      purchase_source on an owned platform row, no igdb_id, and either an
      addon-ish name or a resolvable parent — the phantom shape a purchase import
      mints. Suggests a nested content_type (+ parent when resolved).
    * addon_name_pattern — a primary base_game whose NAME reads like addon
      content (season pass, soundtrack, "DLC", upgrade/costume pack, artbook, …).
      Rows whose content_type is a manual override are skipped (already decided).
      Suggests content_type dlc (or unknown_addon for soundtrack/artbook), plus a
      parent_name when one resolves.

    Live probe (probe_steam=True, the default): walks owned-Steam base_game rows
    oldest-cached first, capped at ``limit`` appdetails fetches (``limit=0`` =
    no cap, probe everything — paced under Steam's request quota, so a large
    library takes minutes), and flags rows Steam itself reports as
    dlc/music/demo (steam_type_mismatch). The tool is read-only, so the
    ordering never changes between calls — to walk the whole library, pass the
    returned ``next_probe_offset`` back as ``probe_offset`` on the next call
    (``next_probe_offset`` is null once the walk is complete). ``probed`` is
    how many rows were fetched this call and ``probe_remaining`` how many
    remain beyond this call's window; per-appid fetch errors are collected in
    ``skipped``. Pass probe_steam=False to skip the network entirely
    (probed=0). ``limit``/``probe_offset`` bound only the probe; the offline
    buckets are capped at 200 candidates each.
    """
    from ..data.content import classify_steam_app_type, parent_name_candidates
    from ..data.db import get_game_substance

    candidates: list[dict] = []
    counts = {
        "inconsistent_primary_nested": 0,
        "nested_parent": 0,
        "needs_parent": 0,
        "purchase_minted_suspect": 0,
        "addon_name_pattern": 0,
        "steam_type_mismatch": 0,
    }

    # --- offline bucket: inconsistent_primary_nested (nested type, primary flag)
    # No current writer can produce this shape (is_primary is always derived
    # from content_type), so every hit is legacy damage — and an invisible one:
    # the row passes the is_primary filter while claiming to be nested, so the
    # nested-content views skip it too. Ordered first: it is definite (a plain
    # column contradiction), unlike the heuristic buckets below.
    async with get_db() as db:
        nested_placeholders = ", ".join("?" for _ in NESTED_CONTENT_TYPES)
        inconsistent_rows = await db.execute_fetchall(
            f"""SELECT id AS game_id, name, content_type, parent_game_id,
                       manual_overrides
               FROM games
               WHERE is_primary_library_item = 1
                 AND content_type IN ({nested_placeholders})
               ORDER BY id
               LIMIT ?""",
            (*sorted(NESTED_CONTENT_TYPES), _MISCLASSIFIED_BUCKET_CAP),
        )
    for row in inconsistent_rows:
        if "content_type" in _pinned_columns(row["manual_overrides"]):
            continue
        async with get_db() as db:
            substance = await get_game_substance(db, row["game_id"])
        inc_evidence: dict = {
            "content_type": row["content_type"],
            "is_primary_library_item": True,
            "has_identifier": substance["has_identifier"],
            "playtime_minutes": substance["playtime_minutes"],
        }
        if substance["has_identifier"] or substance["playtime_minutes"] > 0:
            # A real, played/store-backed game mislabeled nested (the Forza
            # Horizon 4 shape) — promote it back to a primary base game.
            inc_suggested: dict = {
                "game_id": row["game_id"],
                "content_type": "base_game",
            }
        else:
            # Insubstantial: likely genuinely nested content whose is_primary
            # flag desynced. Re-applying the stored content_type through
            # update_game re-derives is_primary=0; link a parent when one
            # resolves so it doesn't just move to the needs_parent bucket.
            inc_suggested = {
                "game_id": row["game_id"],
                "content_type": row["content_type"],
            }
            if row["parent_game_id"] is None:
                parent = await _resolve_primary_parent(
                    parent_name_candidates(row["name"] or ""), row["game_id"]
                )
                if parent is not None:
                    inc_evidence["parent_game_id"] = parent[0]
                    inc_evidence["parent_name"] = parent[1]
                    inc_suggested["parent_game_id"] = parent[0]
        candidates.append(
            {
                "game_id": row["game_id"],
                "name": row["name"],
                "reason": "inconsistent_primary_nested",
                "evidence": inc_evidence,
                "suggested_update": inc_suggested,
            }
        )
    counts["inconsistent_primary_nested"] = len(candidates)

    # --- offline bucket: nested_parent (a nested row other rows hang off) ---
    # Both rows are invisible in this shape: the parent fails the is_primary
    # filter, and its children are only reachable through it. Promoting the
    # parent back to base_game (which also clears its own parent) is the fix, so
    # this bucket is checked ahead of needs_parent — giving such a row a parent
    # (needs_parent's suggestion) would deepen the chain instead of repairing it.
    async with get_db() as db:
        stranded_rows = await db.execute_fetchall(
            """SELECT g.id AS game_id, g.name, g.content_type,
                      (SELECT COUNT(*) FROM games c WHERE c.parent_game_id = g.id)
                          AS child_count
               FROM games g
               WHERE g.is_primary_library_item = 0
                 AND EXISTS (SELECT 1 FROM games c WHERE c.parent_game_id = g.id)
               ORDER BY g.id
               LIMIT ?""",
            (_MISCLASSIFIED_BUCKET_CAP,),
        )
    stranded_ids = {row["game_id"] for row in stranded_rows}
    for row in stranded_rows:
        candidates.append(
            {
                "game_id": row["game_id"],
                "name": row["name"],
                "reason": "nested_parent",
                "evidence": {
                    "content_type": row["content_type"],
                    "child_count": row["child_count"],
                    "note": "nested row that other rows nest under — both are "
                    "hidden from the library until it is promoted",
                },
                "suggested_update": {
                    "game_id": row["game_id"],
                    "content_type": CONTENT_BASE_GAME,
                },
            }
        )
    counts["nested_parent"] = len(stranded_rows)

    # --- offline bucket: needs_parent (nested rows lacking a parent link) ---
    async with get_db() as db:
        # Restricted to rows whose stored content_type is genuinely nested: an
        # is_primary=0 row with a PRIMARY content_type is a desync artifact,
        # and the parent-only suggested_update emitted here would be rejected
        # by update_game ("row must end up nested") — breaking the
        # ready-to-apply contract.
        nested_placeholders = ", ".join("?" for _ in NESTED_CONTENT_TYPES)
        nested_rows = await db.execute_fetchall(
            f"""SELECT id AS game_id, name, content_type
               FROM games
               WHERE is_primary_library_item = 0 AND parent_game_id IS NULL
                 AND content_type IN ({nested_placeholders})
               ORDER BY id
               LIMIT ?""",
            (*sorted(NESTED_CONTENT_TYPES), _MISCLASSIFIED_BUCKET_CAP),
        )
    needs_parent_count = 0
    for row in nested_rows:
        # A row is reported in its first matching bucket only, and a parent that
        # is itself nested already landed in nested_parent above.
        if row["game_id"] in stranded_ids:
            continue
        needs_parent_count += 1
        parent = await _resolve_primary_parent(
            parent_name_candidates(row["name"] or ""), row["game_id"]
        )
        evidence: dict = {"content_type": row["content_type"]}
        if parent is not None:
            evidence["parent_game_id"] = parent[0]
            evidence["parent_name"] = parent[1]
            suggested: dict | None = {
                "game_id": row["game_id"],
                "parent_game_id": parent[0],
            }
        else:
            evidence["note"] = "no parent candidate resolved"
            suggested = None
        candidates.append(
            {
                "game_id": row["game_id"],
                "name": row["name"],
                "reason": "needs_parent",
                "evidence": evidence,
                "suggested_update": suggested,
            }
        )
    counts["needs_parent"] = needs_parent_count

    # --- offline buckets over PRIMARY base_game rows ---
    async with get_db() as db:
        base_rows = await db.execute_fetchall(
            """SELECT g.id AS game_id, g.name, g.igdb_id, g.manual_overrides,
                      EXISTS(SELECT 1 FROM game_platforms gp
                             JOIN game_platform_identifiers gpi
                               ON gpi.game_platform_id = gp.id
                             WHERE gp.game_id = g.id) AS has_identifier,
                      (SELECT gp.purchase_source FROM game_platforms gp
                        WHERE gp.game_id = g.id AND gp.owned = 1
                          AND gp.purchase_source IS NOT NULL
                        LIMIT 1) AS purchase_source
               FROM games g
               WHERE g.content_type = 'base_game'
                 AND g.is_primary_library_item = 1
               ORDER BY g.id"""
        )

    purchase_count = 0
    addon_count = 0
    for row in base_rows:
        gid = row["game_id"]
        name = row["name"]
        addon = _match_addon_name(name)
        # Parent resolution runs unindexed name lookups per split candidate —
        # only pay for it on rows that can actually still become a candidate.
        may_be_purchase_suspect = (
            not row["has_identifier"]
            and row["purchase_source"] is not None
            and row["igdb_id"] is None
            and purchase_count < _MISCLASSIFIED_BUCKET_CAP
        )
        addon_pinned = addon is not None and "content_type" in _pinned_columns(
            row["manual_overrides"]
        )
        may_be_addon_candidate = (
            addon is not None
            and not addon_pinned
            and addon_count < _MISCLASSIFIED_BUCKET_CAP
        )
        if not (may_be_purchase_suspect or may_be_addon_candidate):
            continue
        parent = await _resolve_primary_parent(parent_name_candidates(name or ""), gid)

        # purchase_minted_suspect takes precedence over addon_name_pattern.
        is_purchase_suspect = may_be_purchase_suspect and (
            addon is not None or parent is not None
        )
        if is_purchase_suspect:
            content_type = addon[0] if addon is not None else CONTENT_DLC
            evidence = {
                "purchase_source": row["purchase_source"],
                "igdb_id": None,
                "has_identifier": False,
            }
            if addon is not None:
                evidence["matched_pattern"] = addon[1]
            suggested = {"game_id": gid, "content_type": content_type}
            if parent is not None:
                evidence["parent_game_id"] = parent[0]
                evidence["parent_name"] = parent[1]
                suggested["parent_game_id"] = parent[0]
            candidates.append(
                {
                    "game_id": gid,
                    "name": name,
                    "reason": "purchase_minted_suspect",
                    "evidence": evidence,
                    "suggested_update": suggested,
                }
            )
            purchase_count += 1
            continue

        # Pinned rows (user already decided the type) and full buckets were
        # excluded above, before the parent resolution was paid for.
        if may_be_addon_candidate:
            content_type, label = addon  # type: ignore[misc]
            evidence = {"matched_pattern": label}
            suggested = {"game_id": gid, "content_type": content_type}
            if parent is not None:
                evidence["parent_game_id"] = parent[0]
                evidence["parent_name"] = parent[1]
                # By id, like every other bucket: the exact row this detector
                # validated as primary, with no name re-resolution at apply time.
                suggested["parent_game_id"] = parent[0]
            candidates.append(
                {
                    "game_id": gid,
                    "name": name,
                    "reason": "addon_name_pattern",
                    "evidence": evidence,
                    "suggested_update": suggested,
                }
            )
            addon_count += 1

    counts["purchase_minted_suspect"] = purchase_count
    counts["addon_name_pattern"] = addon_count

    # --- live probe: steam_type_mismatch ---
    probed = 0
    probe_remaining = 0
    next_probe_offset: int | None = None
    skipped: list[dict] = []
    if probe_steam:
        async with get_db() as db:
            steam_rows = await db.execute_fetchall(
                """SELECT g.id AS game_id, g.name,
                          gpi.identifier_value AS steam_appid
                   FROM games g
                   JOIN game_platforms gp
                     ON gp.game_id = g.id AND gp.platform = 'steam' AND gp.owned = 1
                   JOIN game_platform_identifiers gpi
                     ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
                   LEFT JOIN steam_platform_data spd
                     ON spd.game_platform_id = gp.id
                   WHERE g.content_type = 'base_game'
                     AND g.is_primary_library_item = 1
                   ORDER BY spd.store_cached_at IS NOT NULL, spd.store_cached_at, g.id""",
                (STEAM_APP_ID,),
            )
        # The tool is read-only, so the store_cached_at ordering never changes
        # between calls — the caller advances the walk explicitly by passing
        # back next_probe_offset. limit=0 means "no cap" (sibling detector
        # convention), i.e. probe everything from probe_offset on.
        start = max(0, probe_offset)
        end = len(steam_rows) if limit <= 0 else start + limit
        to_probe = list(steam_rows[start:end])
        probe_remaining = max(0, len(steam_rows) - min(end, len(steam_rows)))
        next_probe_offset = end if end < len(steam_rows) else None

        from ..data.steam_store import _parse_content_fields

        for row in to_probe:
            probed += 1
            try:
                appid = int(str(row["steam_appid"]).strip())
            except (TypeError, ValueError):
                appid = None
            if appid is None:
                continue
            try:
                store_data = await _fetch_steam_appdetails(appid)
            except Exception as exc:
                skipped.append(
                    {
                        "game_id": row["game_id"],
                        "steam_appid": row["steam_appid"],
                        "error": str(exc),
                    }
                )
                continue
            if not store_data:
                continue
            store_type, fullgame_name, fullgame_appid, _dlc = _parse_content_fields(
                store_data
            )
            classification = classify_steam_app_type(
                store_type,
                title=row["name"],
                fullgame_name=fullgame_name,
                fullgame_appid=fullgame_appid,
            )
            # Only a nested Steam verdict on a primary row is a mismatch.
            if classification is None or classification.is_primary_library_item:
                continue
            parent = await _resolve_primary_parent(
                [classification.parent_name] if classification.parent_name else [],
                row["game_id"],
                steam_appid=classification.parent_steam_appid,
            )
            evidence = {
                "steam_appid": row["steam_appid"],
                "steam_type": store_type,
                "content_type": classification.content_type,
            }
            suggested = {"game_id": row["game_id"], "content_type": classification.content_type}
            if parent is not None:
                evidence["parent_game_id"] = parent[0]
                evidence["parent_name"] = parent[1]
                suggested["parent_game_id"] = parent[0]
            elif classification.parent_name:
                evidence["parent_name"] = classification.parent_name
            candidates.append(
                {
                    "game_id": row["game_id"],
                    "name": row["name"],
                    "reason": "steam_type_mismatch",
                    "evidence": evidence,
                    "suggested_update": suggested,
                }
            )
        counts["steam_type_mismatch"] = sum(
            1 for c in candidates if c["reason"] == "steam_type_mismatch"
        )

    return {
        "candidates": candidates,
        "counts": counts,
        "probed": probed,
        "probe_remaining": probe_remaining,
        "next_probe_offset": next_probe_offset,
        "skipped": skipped,
    }


async def revalidate_igdb_matches(dry_run: bool = True, limit: int | None = None) -> dict:
    """Audit every stored igdb_id against IGDB's actual name for that id.

    Wrong name-based enrichment is worse than none: prod carried rows like
    "Tales from the Borderlands" enriched as "New Tales from the Borderlands"
    (214139), "PAYDAY 2" as "Payday 2 VR" (150511), and "Borderlands GOTY" as
    the unrelated "The Tower on the Borderland" (258897) — poisoning series
    gaps, deals availability, and series memberships. This tool batch-fetches
    the IGDB name for every games row with an igdb_id (chunked, rate-gated via
    fetch_igdb_game_names) and applies the same strict gate new enrichment
    uses (edition-stripped normalized titles must be equal,
    normalize_series_gap_title).

    dry_run=True (default) only reports mismatches. dry_run=False resets the
    IGDB enrichment on mismatched rows — igdb_id/igdb_platforms/
    igdb_cached_at/igdb_claimed_at to NULL and that game's
    game_series_membership rows deleted (they came from the bad match) — so
    background enrichment re-resolves them under the strict gate. Rows whose
    igdb_id is listed in games.manual_overrides are reported separately and
    never reset. limit caps how many rows are checked (None/0 = all).
    """
    from ..data.db import get_manual_overrides
    from ..data.igdb import fetch_igdb_game_names, igdb_credentials_configured
    from ..data.title_normalization import normalize_series_gap_title

    igdb_configured = igdb_credentials_configured()

    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT id, name, igdb_id FROM games WHERE igdb_id IS NOT NULL ORDER BY id"
        )
    if limit is not None and limit > 0:
        rows = rows[:limit]

    result = {
        "dry_run": dry_run,
        "igdb_configured": igdb_configured,
        "checked": 0,
        "mismatch_count": 0,
        "mismatches": [],
        "reset_count": 0,
        "skipped_overridden": 0,
        "unresolved_igdb_ids": 0,
    }
    if not igdb_configured or not rows:
        return result

    igdb_names = await fetch_igdb_game_names([row["igdb_id"] for row in rows])

    mismatches: list[dict] = []
    skipped_overridden = 0
    unresolved = 0
    async with get_db() as db:
        for row in rows:
            igdb_name = igdb_names.get(row["igdb_id"])
            if igdb_name is None:
                # IGDB no longer returns this id (deleted/merged upstream) —
                # can't validate the name, so don't touch the row.
                unresolved += 1
                continue
            if normalize_series_gap_title(row["name"]) == normalize_series_gap_title(
                igdb_name
            ):
                continue
            if "igdb_id" in await get_manual_overrides(db, row["id"]):
                skipped_overridden += 1
                continue
            mismatches.append(
                {
                    "game_id": row["id"],
                    "name": row["name"],
                    "igdb_id": row["igdb_id"],
                    "igdb_name": igdb_name,
                }
            )

        reset_count = 0
        if not dry_run and mismatches:
            for mismatch in mismatches:
                await db.execute(
                    """UPDATE games
                       SET igdb_id = NULL,
                           igdb_platforms = NULL,
                           igdb_cached_at = NULL,
                           igdb_claimed_at = NULL
                       WHERE id = ?""",
                    (mismatch["game_id"],),
                )
                await db.execute(
                    "DELETE FROM game_series_membership WHERE game_id = ?",
                    (mismatch["game_id"],),
                )
            await db.commit()
            reset_count = len(mismatches)

    result.update(
        {
            "checked": len(rows),
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
            "reset_count": reset_count,
            "skipped_overridden": skipped_overridden,
            "unresolved_igdb_ids": unresolved,
        }
    )
    return result
