"""refresh_library, detect_farmed_games, and set_nintendo_session admin tools."""

import asyncio
import json
import logging
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone

from fastmcp.exceptions import ToolError

from ..data.db import STEAM_APP_ID, get_db
from ..data.enrich_bg import pause_background_enrichment, resume_background_enrichment
from ..data.epic import sync_epic
from ..data.gog import sync_gog
from ..data.nintendo import sync_nintendo
from ..data.psn import sync_psn
from ..data.steam_xml import fetch_library
from ..lifecycle import _schedule_background_enrich, get_startup_refresh_task
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

    platform_syncs = {
        "steam":   fetch_library,
        "epic":    sync_epic,
        "gog":     sync_gog,
        "switch2": sync_nintendo,
        "ps5":     sync_psn,
    }

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


async def set_nintendo_session(cookies: str) -> dict:
    """
    Store Nintendo Account session cookies for VGCS fallback sync.

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
    (default: data/nintendo_cookies.json).
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

    path = os.getenv("NINTENDO_COOKIES_FILE", "data/nintendo_cookies.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2)

    logger.info("Nintendo session cookies saved to %s (%d cookies)", path, len(normalized))
    return {"cookie_count": len(normalized), "path": path}


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

    Saved to NINTENDO_PCTL_SESSION_FILE (default: data/nintendo_pctl_session.json).
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

        source_platforms = await db.execute_fetchall(
            "SELECT id, platform, playtime_minutes, owned, last_played FROM game_platforms WHERE game_id = ?",
            (source_game_id,),
        )

        platforms_moved: list[str] = []
        platforms_merged: list[str] = []

        for sp in source_platforms:
            sp_id: int = sp["id"]
            platform: str = sp["platform"]
            target_platform = await db.execute_fetchone(
                "SELECT id, playtime_minutes, last_played, owned FROM game_platforms WHERE game_id = ? AND platform = ?",
                (target_game_id, platform),
            )

            if not dry_run:
                if target_platform is None:
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
        "source_deleted": not dry_run,
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

    sample = []
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
