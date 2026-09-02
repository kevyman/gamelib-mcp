"""Library sync orchestration and identity repair (merge/split/delete).

Two neighbours were split out of this module unchanged: ``tools/detectors.py``
(the ADR-0003-obsoleted data-integrity detectors ``tools/checks.py`` adapts) and
``tools/session_admin.py`` (the cookie/token save paths ``session_ingest.py``
dispatches to). ``create_session_ingest_link``, the MCP tool that mints the
paste link, stays here.
"""

import asyncio
import logging
import sys
from datetime import UTC, datetime

from fastmcp.exceptions import ToolError

from ..data.db import (
    ACQUISITION_FIELDS,
    NINTENDO_BASELINE_DEVICE_ID,
    clear_fulfilled_wishlist_entries,
    get_db,
    record_play_history_snapshots,
)
from ..data.enrich_bg import pause_background_enrichment, resume_background_enrichment

# The platform sync dicts are built from platforms_registry at call time; the
# imports below keep the functions bound on this module so existing tests can
# patch gamelib_mcp.tools.admin.<sync_fn> (resolve_platform_functions checks
# this namespace first). F401: referenced via getattr, not by name.
from ..data.epic import sync_epic  # noqa: F401
from ..data.gog import sync_gog  # noqa: F401
from ..data.nintendo import NINTENDO_TITLE_ID, sync_nintendo  # noqa: F401
from ..data.psn import sync_psn  # noqa: F401
from ..data.steam_xml import fetch_library  # noqa: F401
from ..data.title_normalization import normalize_search_text
from ..data.xbox import sync_xbox  # noqa: F401
from ..lifecycle import (
    _schedule_background_enrich,
    get_startup_refresh_task,
    record_platform_sync_outcome,
)
from ..platforms_registry import WISHLIST_SYNCABLE_PLATFORMS
from .batch import apply_batch_item, check_batch_items, count_status
from .common import (
    PLATFORM_ALIASES,
    SYNCABLE_PLATFORMS,
    PlatformSyncFanout,
    report_progress,
)
from .common import info as _info

# Bound on this module on purpose: run_library_sync calls it after a Steam sync,
# and the established test seam is patch("gamelib_mcp.tools.admin.detect_farmed_games").
from .detectors import detect_farmed_games

logger = logging.getLogger(__name__)


async def _mark_sync_started(targets: set[str]) -> None:
    """Mark the overall sync in-progress and each selected platform running.

    Clearing each target's recorded error is part of entering "running": the
    error field describes the run the state names, and a platform that is
    running has no outcome yet. Leaving the last run's message in place is how
    a successful retry could still be read as a failure mid-run.
    """
    from ..data.db import set_meta_many

    started_at = datetime.now(UTC).isoformat()
    updates: dict[str, str | None] = {
        "library_sync_status": "in_progress",
        "library_sync_started_at": started_at,
        "library_sync_finished_at": None,
    }
    for name in targets:
        updates[f"sync_platform_state_{name}"] = "running"
        updates[f"integration_sync_{name}_last_attempt_at"] = started_at
        updates[f"integration_sync_{name}_last_error_summary"] = None
        updates[f"integration_sync_{name}_last_error_classification"] = None
    await set_meta_many(updates)


async def run_library_sync(
    platforms: list[str] | None = None,
    ctx=None,
) -> dict:
    """
    Re-sync game library. Defaults to all configured platforms.
    platforms: optional subset, e.g. ["steam", "epic"]. If omitted, syncs all.
    """
    fanout = PlatformSyncFanout(
        platforms,
        SYNCABLE_PLATFORMS,
        unknown_message=lambda unknown, valid: (
            "Unknown platform '{}'. Valid: {}".format("', '".join(unknown), valid)
        ),
    )
    targets = fanout.targets

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
    selected = fanout.dispatch("sync", namespace=sys.modules[__name__])
    result_names = fanout.display_names

    pause_background_enrichment()
    try:
        # Mark started here too (not only in the refresh_library tool): the startup and
        # periodic paths reach this worker via _run_startup_refresh without going through
        # the tool, so this is what records per-platform "running" state on those paths.
        # On the tool path it's an idempotent re-write of state the tool already set.
        await _mark_sync_started(targets)
        await report_progress(ctx, 0, len(selected))
        await _info(ctx, f"Refreshing {len(selected)} platform(s)")

        results: dict = {}
        async for name, outcome in fanout.gather(ctx):
            result_name = result_names.get(name, name)
            finished_at = datetime.now(UTC).isoformat()
            if isinstance(outcome, BaseException):
                payload = {"error": str(outcome)}
                results[result_name] = payload
                # Record this platform's own outcome (state + error + success
                # time) now rather than after the whole run, so a poll between
                # platforms never pairs a fresh state with a stale error.
                await record_platform_sync_outcome(name, payload, finished_at)
                await _info(ctx, f"Failed {result_name} refresh: {outcome}")
            else:
                results[result_name] = outcome
                await record_platform_sync_outcome(
                    name, outcome if isinstance(outcome, dict) else {}, finished_at
                )
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
                )
                from ..data.steam_licenses import (
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
        "library_sync_finished_at": datetime.now(UTC).isoformat(),
    })
    return results


# The platform filter a sync target honors, by target name. "ratings" is absent
# because it ignores the filter entirely.
_PLATFORM_SCOPED_SYNC_TARGETS: dict[str, frozenset[str]] = {
    "library": SYNCABLE_PLATFORMS,
    "wishlist": WISHLIST_SYNCABLE_PLATFORMS,
}


def validate_sync_platforms(targets: list[str], platforms: list[str] | None) -> None:
    """
    Reject a platform filter a selected target cannot honor, before ANY of them
    starts work.

    refresh_library and sync_wishlist each validate their own filter, but a
    combined sync() runs them in sequence and the library one is
    fire-and-forget: sync(targets=["library", "wishlist"], platforms=["gog"])
    would launch the background library sync and only then hit sync_wishlist's
    rejection, so the caller sees an error for a sync that is in fact running —
    and the retry reports already_running while still failing. Validating every
    selected target up front keeps a rejected call a no-op.
    """
    if not platforms:
        return

    problems: list[str] = []
    for target in targets:
        supported = _PLATFORM_SCOPED_SYNC_TARGETS.get(target)
        if supported is None:
            continue
        unsupported = sorted(
            {p for p in platforms if PLATFORM_ALIASES.get(p.lower(), p.lower()) not in supported}
        )
        if unsupported:
            problems.append(
                f"target '{target}' does not sync {unsupported} "
                f"(valid: {sorted(supported | {a for a, n in PLATFORM_ALIASES.items() if n in supported})})"
            )

    if problems:
        hint = (
            " PSN has no wishlist API — use add_game_to_platform(owned=False)."
            if any(p.startswith("target 'wishlist'") for p in problems)
            else ""
        )
        raise ToolError(f"Nothing was synced: {'; '.join(problems)}.{hint}")


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
    platform reports state (pending/running/done/error/unconfigured), its last
    success time, and the last error summary if any. Poll this after calling
    refresh_library.

    Each platform's state and error always describe the SAME run: entering
    "running" clears the previous error, and the outcome is recorded when that
    platform finishes rather than when the whole run does. "done" therefore
    means this platform is finished even while the overall status is still
    in_progress (other platforms, the license audit, and background enrichment
    can outlast it). "unconfigured" means the integration has never been set up
    — the error names what is missing, and last_success_at is null.
    """
    from ..data.db import get_meta, get_meta_prefix

    overall = await get_meta("library_sync_status") or "idle"
    started_at = await get_meta("library_sync_started_at")
    finished_at = await get_meta("library_sync_finished_at")

    state_keys = await get_meta_prefix("sync_platform_state_")
    integ = await get_meta_prefix("integration_sync_")

    platforms: dict[str, dict] = {}
    for name in sorted(SYNCABLE_PLATFORMS):
        state = state_keys.get(f"sync_platform_state_{name}", "pending")
        error = integ.get(f"integration_sync_{name}_last_error_summary")
        # Heals rows recorded before "unconfigured" existed: a platform whose
        # last failure was a missing-credential one is not "done".
        if (
            state == "done"
            and error
            and integ.get(f"integration_sync_{name}_last_error_classification")
            == "missing_configuration"
        ):
            state = "unconfigured"
        platforms[name] = {
            "state": state,
            "last_success_at": integ.get(f"integration_sync_{name}_last_success_at"),
            "error": error,
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
    fanout = PlatformSyncFanout(
        platforms,
        WISHLIST_SYNCABLE_PLATFORMS,
        unknown_message=lambda unknown, valid: (
            f"Unknown wishlist platform '{', '.join(unknown)}'. Valid: {valid}. "
            "PSN has no wishlist API — use add_game_to_platform(owned=False)."
        ),
    )
    selected = fanout.dispatch("wishlist_sync", namespace=sys.modules[__name__])

    await _info(ctx, f"Syncing wishlist for {len(selected)} platform(s)")
    await report_progress(ctx, 0, len(selected))

    results: dict = {}
    async for name, outcome in fanout.gather(ctx):
        if isinstance(outcome, BaseException):
            results[name] = {"error": str(outcome)}
            await _info(ctx, f"Failed {name} wishlist sync: {outcome}")
        else:
            results[name] = outcome
            await _info(ctx, f"Finished {name} wishlist sync")

    # A stale external wishlist can list a game already owned locally (bought
    # elsewhere, or ownership synced since the last wishlist check) — reconcile
    # immediately rather than waiting for the next library refresh.
    try:
        await clear_fulfilled_wishlist_entries()
    except Exception:
        logger.exception("Wishlist fulfillment cleanup failed after wishlist sync")

    return results


async def create_session_ingest_link(provider: str) -> dict:
    """Mint a single-use browser URL for pasting session cookies outside chat.

    The returned URL serves a paste form that saves through the matching
    set_*_session tool; see gamelib_mcp/session_ingest.py for the flow.

    For Steam, prefer provider="steam_refresh" (long-lived token, no re-pasting);
    "steam_store" is a short-lived legacy fallback.
    """
    # Lazy import keeps session_ingest a leaf module (it imports this module
    # lazily in turn for setter dispatch).
    from ..session_ingest import mint_ingest_link

    return mint_ingest_link(provider)


async def merge_games(
    source_game_id: int,
    target_game_id: int,
    dry_run: bool = False,
    *,
    recompute_affinity: bool = True,
) -> dict:
    """
    Merge one game row into another and delete the source.

    Transfers all platform ownership rows (re-pointing or merging into an
    existing target platform), platform identifiers, enrichment, ratings, series
    memberships, game aliases, play history, wishlist entries, cached price
    rows, and recorded assessments from source to target in a single atomic
    transaction. When both games
    own the same platform, identifiers are re-pointed to the target row,
    playtime is set to the higher of the two values, and the source platform
    row is deleted. Ratings for the same source are kept on the target if
    already present; otherwise they are moved. A source wishlist entry whose
    platform the merged target owns is dropped as fulfilled; a price row the
    target already caches for the same platform+shop is dropped (target wins);
    a source assessment colliding on the target's (game, UTC day) is dropped
    the same way, and "play what you own instead" links pointing at the source
    are re-pointed at the target.
    Children nested under the source are re-pointed at the target, and a nested
    target that absorbs its own parent (or inherits children) is promoted to a
    primary base game — the remediation path for phantom edition parents.

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

        # game_wishlist / game_prices — both FK games(id) ON DELETE CASCADE, so
        # the source-row DELETE below would silently destroy them (observed in
        # prod: a merge preview reported every field empty while delete_game's
        # preview counted 1 wishlist entry + 1 price row on the same id).
        # Transfer to the target, target's row winning a unique-key collision;
        # a source wishlist entry whose platform the merged target OWNS is
        # fulfilled (what clear_fulfilled_wishlist_entries would do after the
        # next sync) and is dropped rather than transferred.
        source_wishlist = await db.execute_fetchall(
            "SELECT id, platform FROM game_wishlist WHERE game_id = ?",
            (source_game_id,),
        )
        wishlist_entries_transferred = 0
        wishlist_entries_dropped = 0
        for w in source_wishlist:
            platform = w["platform"]
            # In the wet run source platforms were already re-pointed above, so
            # the SQL check sees them; the source_platforms fallback keeps the
            # dry-run preview faithful to that outcome.
            fulfilled = await db.execute_fetchone(
                """SELECT 1 FROM game_platforms
                    WHERE game_id = ? AND platform = ? AND owned = 1""",
                (target_game_id, platform),
            ) is not None or any(
                sp["platform"] == platform and sp["owned"] for sp in source_platforms
            )
            target_has = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ? AND platform = ?",
                (target_game_id, platform),
            )
            if fulfilled or target_has is not None:
                wishlist_entries_dropped += 1
                # No explicit DELETE needed: the source-row cascade removes it.
            else:
                if not dry_run:
                    await db.execute(
                        "UPDATE game_wishlist SET game_id = ? WHERE id = ?",
                        (target_game_id, w["id"]),
                    )
                wishlist_entries_transferred += 1

        source_prices = await db.execute_fetchall(
            "SELECT id, platform, shop FROM game_prices WHERE game_id = ?",
            (source_game_id,),
        )
        price_rows_transferred = 0
        price_rows_dropped = 0
        for p in source_prices:
            target_has = await db.execute_fetchone(
                "SELECT 1 FROM game_prices WHERE game_id = ? AND platform = ? AND shop = ?",
                (target_game_id, p["platform"], p["shop"]),
            )
            if target_has is None:
                if not dry_run:
                    await db.execute(
                        "UPDATE game_prices SET game_id = ? WHERE id = ?",
                        (target_game_id, p["id"]),
                    )
                price_rows_transferred += 1
            else:
                # Target already caches this platform+shop price; keep it and
                # let the source's row cascade away (it's a cache, not history).
                price_rows_dropped += 1

        # game_assessments — FK games(id) ON DELETE CASCADE like the two above,
        # so the source-row DELETE would erase the recorded verdicts the
        # calibration report reads. Transfer them; on the (game_id, UTC day)
        # unique index the TARGET's row wins (the merge's keep-target rule)
        # and the source's cascades away. Verdicts that named the source as
        # "play what you own instead: X" are re-pointed too — the game they
        # meant is the surviving row.
        source_assessments = await db.execute_fetchall(
            "SELECT id, date(assessed_at) AS day FROM game_assessments WHERE game_id = ?",
            (source_game_id,),
        )
        assessments_transferred = 0
        assessments_dropped = 0
        for assessment in source_assessments:
            collision = await db.execute_fetchone(
                """SELECT 1 FROM game_assessments
                    WHERE game_id = ? AND date(assessed_at) = ?""",
                (target_game_id, assessment["day"]),
            )
            if collision is not None:
                assessments_dropped += 1
                continue
            if not dry_run:
                await db.execute(
                    "UPDATE game_assessments SET game_id = ? WHERE id = ?",
                    (target_game_id, assessment["id"]),
                )
            assessments_transferred += 1

        instead_row = await db.execute_fetchone(
            "SELECT COUNT(*) AS c FROM game_assessments WHERE instead_game_id = ?",
            (source_game_id,),
        )
        assessment_instead_links_repointed = instead_row["c"] if instead_row else 0
        if not dry_run and assessment_instead_links_repointed:
            await db.execute(
                "UPDATE game_assessments SET instead_game_id = ? WHERE instead_game_id = ?",
                (target_game_id, source_game_id),
            )

        # Nested children — games.parent_game_id is ON DELETE SET NULL, so the
        # source DELETE would strand its children parentless (dropping them into
        # detect_misclassified_dlc's needs_parent bucket). Re-point them at the
        # target. A target that was itself the source's child (merging a phantom
        # parent into its owned edition row) gets its parent cleared, and a
        # nested target that absorbs its parent or inherits children is promoted
        # to a primary base game — a parent must stay primary (ADR 0002), and a
        # nested row left with no parent would be invisible to every rollup.
        child_rows = await db.execute_fetchall(
            "SELECT id FROM games WHERE parent_game_id = ?", (source_game_id,)
        )
        children_reparented = sum(
            1 for c in child_rows if c["id"] != target_game_id
        )
        target_was_child = any(c["id"] == target_game_id for c in child_rows)
        target_promoted_to_primary = False
        if children_reparented or target_was_child:
            target_state = await db.execute_fetchone(
                "SELECT is_primary_library_item FROM games WHERE id = ?",
                (target_game_id,),
            )
            target_promoted_to_primary = bool(
                target_state is not None
                and not target_state["is_primary_library_item"]
            )
        if not dry_run:
            if target_was_child:
                await db.execute(
                    "UPDATE games SET parent_game_id = NULL WHERE id = ?",
                    (target_game_id,),
                )
            if children_reparented:
                await db.execute(
                    "UPDATE games SET parent_game_id = ? "
                    "WHERE parent_game_id = ? AND id != ?",
                    (target_game_id, source_game_id, target_game_id),
                )
            if target_promoted_to_primary:
                await db.execute(
                    """UPDATE games
                          SET content_type = 'base_game',
                              is_primary_library_item = 1,
                              parent_game_id = NULL
                        WHERE id = ?""",
                    (target_game_id,),
                )

        if not dry_run:
            await db.execute("DELETE FROM game_aliases WHERE game_id = ?", (source_game_id,))
            await db.execute("DELETE FROM games WHERE id = ?", (source_game_id,))
            await db.commit()

    # Moving ratings shifts which games feed the taste profile, so recompute tag
    # affinity the same way rate_game/sync_ratings do — otherwise discover_games
    # ranks on stale scores until the next background pass. Outside the db
    # context manager since recompute opens its own connection.
    if not dry_run and recompute_affinity and (ratings_moved or ratings_kept_target):
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
        "wishlist_entries_transferred": wishlist_entries_transferred,
        "wishlist_entries_dropped": wishlist_entries_dropped,
        "price_rows_transferred": price_rows_transferred,
        "price_rows_dropped": price_rows_dropped,
        "assessments_transferred": assessments_transferred,
        "assessments_dropped": assessments_dropped,
        "assessment_instead_links_repointed": assessment_instead_links_repointed,
        "children_reparented": children_reparented,
        "target_promoted_to_primary": target_promoted_to_primary,
        "source_deleted": not dry_run,
    }


_MERGE_BATCH_ITEM_KEYS = frozenset({"source_game_id", "target_game_id"})


async def merge_games_batch(items: list[dict], dry_run: bool = False) -> dict:
    """
    Apply merge_games to many source→target pairs; per-item errors never fail
    the whole call.

    Each item is {source_game_id, target_game_id}. Because a merge deletes its
    source row, ids consumed by an earlier item in the same batch are tracked:
    a later item referencing one gets status="stale_id" instead of a confusing
    not-found error — in dry_run too, so the preview predicts the wet outcome.
    The tag-affinity recompute a ratings transfer normally triggers is deferred
    and run ONCE after the loop (tag_affinity_tags_updated; 0 when no ratings
    moved or dry_run). dry_run forwards to merge_games' own faithful preview,
    but its counts are computed against the CURRENT database: a chained item
    whose source or target was an earlier item's target (A→B then B→C) can't
    see what that earlier merge would have moved into the row, so its counts
    may understate the wet run — such items carry chained_preview=true.
    """
    check_batch_items(items)

    consumed: set[int] = set()
    targets_seen: set[int] = set()
    ratings_touched = False

    async def _one(source_game_id=None, target_game_id=None):
        nonlocal ratings_touched
        if source_game_id is None or target_game_id is None:
            raise ToolError("each item requires source_game_id and target_game_id")
        stale = sorted(
            {gid for gid in (source_game_id, target_game_id) if gid in consumed}
        )
        if stale:
            return {
                "status": "stale_id",
                "source_game_id": source_game_id,
                "target_game_id": target_game_id,
                "error": (
                    f"game id(s) {stale} were merged away by an earlier item "
                    "in this batch"
                ),
            }
        result = await merge_games(
            source_game_id, target_game_id, dry_run, recompute_affinity=False
        )
        # A dry-run item touching an earlier item's target reads the pre-batch
        # DB, so its counts miss whatever that merge would have moved in.
        if dry_run and (source_game_id in targets_seen or target_game_id in targets_seen):
            result["chained_preview"] = True
        targets_seen.add(target_game_id)
        # Track in dry_run too: the wet run deletes the source, so a later
        # item reusing it must preview as stale.
        consumed.add(source_game_id)
        if result["ratings_moved"] or result["ratings_kept_target"]:
            ratings_touched = True
        return result

    results: list[dict] = []
    tag_count = 0
    try:
        for item in items:
            results.append(await apply_batch_item(item, _MERGE_BATCH_ITEM_KEYS, _one))
    finally:
        # Committed ratings moves must never be left without their deferred
        # recompute.
        if ratings_touched and not dry_run:
            from ..data.db import recompute_tag_affinity
            tag_count = await recompute_tag_affinity()

    return {
        "results": results,
        "total": len(items),
        "ok": count_status(results, "ok"),
        "stale_id": count_status(results, "stale_id"),
        "errors": count_status(results, "error"),
        "dry_run": dry_run,
        "tag_affinity_tags_updated": tag_count,
    }


async def delete_game(
    name: str | None = None,
    game_id: int | None = None,
    confirm: bool = False,
    *,
    recompute_affinity: bool = True,
    ignore_child_ids: frozenset[int] = frozenset(),
) -> dict:
    """
    Permanently delete one game and all of its data. IRREVERSIBLE.

    Resolve the game with game_id or name (partial/fuzzy match — the resolved
    name is echoed back so you can confirm the right row), then remove it and
    every dependent record: platform ownership rows, store identifiers,
    provider enrichment, ratings, wishlist entries, price cache, play-history
    snapshots, series memberships, aliases, and recorded assessments.

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
        # ignore_child_ids (internal, batch-only): children already deleted —
        # or slated for deletion — by earlier items of the same batch don't
        # block the parent, so a [child, parent] batch previews exactly what
        # its confirm run does.
        surviving = [c for c in children if c["id"] not in ignore_child_ids]
        if surviving:
            listed = ", ".join(f"{c['name']} (id {c['id']})" for c in surviving)
            raise ToolError(
                f"'{resolved_name}' (id {resolved_id}) is the parent of "
                f"{len(surviving)} nested item(s): {listed}. Reparent or delete "
                "them first (update_game/delete_game) so they are not orphaned."
            )

        # Count dependents for the preview / summary. game_platform_identifiers,
        # steam_platform_data, and game_platform_enrichment cascade from
        # game_platforms; game_wishlist/game_prices/play_history/
        # game_series_membership/game_aliases/game_assessments cascade from
        # games.
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
            "assessments": await _count(
                "SELECT COUNT(*) AS c FROM game_assessments WHERE game_id = ?"
            ),
        }
        # Synthetic manual-baseline playtime rows (set_switch2_playtime_baseline)
        # have no FK to the game — they bridge via the nintendo_title_id
        # identifier. Left behind, the next Parental Controls sync would find
        # an identifier-less summary total and resurrect the deleted game, so
        # they die with it. Real device-reported daily summaries are kept:
        # actual play history is ownership-agnostic by design. Plain equality:
        # both sides are normalized to uppercase at ingest (see
        # data/db/__init__.py::normalize_identifier_value).
        _baseline_match_sql = """
            FROM nintendo_play_summary AS nps
            WHERE nps.device_id = ?
              AND EXISTS (
                  SELECT 1 FROM game_platform_identifiers gpi
                  JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                  WHERE gp.game_id = ? AND gpi.identifier_type = ?
                    AND gpi.identifier_value = nps.application_id)
        """
        _baseline_params = (NINTENDO_BASELINE_DEVICE_ID, resolved_id, NINTENDO_TITLE_ID)
        baseline_count_row = await db.execute_fetchone(
            f"SELECT COUNT(*) AS c {_baseline_match_sql}", _baseline_params
        )
        would_delete["nintendo_baseline_rows"] = (
            baseline_count_row["c"] if baseline_count_row else 0
        )

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
        # Baseline rows first: the match needs the identifier rows, which
        # cascade away with game_platforms below.
        await db.execute(f"DELETE {_baseline_match_sql}", _baseline_params)
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
    # (A batch defers this and recomputes once at the end.)
    if recompute_affinity:
        from ..data.db import recompute_tag_affinity
        await recompute_tag_affinity()

    return {
        "deleted": True,
        "game_id": resolved_id,
        "name": resolved_name,
        "deleted_counts": would_delete,
    }


_DELETE_BATCH_ITEM_KEYS = frozenset({"name", "game_id"})


async def delete_games_batch(items: list[dict], confirm: bool = False) -> dict:
    """
    Apply delete_game to many games, preserving the two-step confirm.

    Each item is {name or game_id}. All items are pre-resolved to ids BEFORE
    anything is deleted, so preview and confirm resolve names against the same
    library state (a mid-batch delete can't re-route a later name to a
    different row); two items resolving to the same game make the second an
    error in both modes. confirm=False previews every item
    (status="previewed" with its would_delete counts, summed top-level in
    would_delete_total); confirm=True deletes (status="deleted", summed in
    deleted_counts_total) — matching totals. A parent of nested content is
    status="refused" (with its children listed) and never aborts the rest;
    the guard runs net of ids earlier in the batch in both modes, so a
    [child, parent] batch deletes (and previews) both. The per-delete
    tag-affinity recompute is deferred and run once after the loop.
    """
    check_batch_items(items)
    # Lazy import as in delete_game: avoids a top-level cycle with platforms.py.
    from .platforms import _resolve_game_row

    # Phase 1: pre-resolve EVERY item before anything is deleted. Names must
    # resolve against the same library state in preview and confirm — if item
    # N's delete ran first, item N+1's name could re-route to a different row
    # (e.g. two "Dark Souls" items: the second must error, not prefix-match
    # "Dark Souls II" once the exact match is gone). Duplicate resolutions are
    # caught here for the same reason.
    resolved: list[dict] = []
    seen_ids: set[int] = set()
    for item in items:
        try:
            if not isinstance(item, dict):
                raise ToolError("each item must be an object")
            unknown = set(item) - _DELETE_BATCH_ITEM_KEYS
            if unknown:
                raise ToolError(
                    f"unknown key(s): {sorted(unknown)}. "
                    f"Valid: {sorted(_DELETE_BATCH_ITEM_KEYS)}"
                )
            row = await _resolve_game_row(item.get("name"), item.get("game_id"))
            if row["id"] in seen_ids:
                raise ToolError(
                    f"'{row['name']}' (id {row['id']}) is already slated for "
                    "deletion by an earlier item in this batch"
                )
            seen_ids.add(row["id"])
            resolved.append({"row": row})
        except Exception as exc:  # noqa: BLE001 - same per-item isolation as apply_batch_item
            message = (
                str(exc) if isinstance(exc, ToolError)
                else f"{type(exc).__name__}: {exc}"
            )
            payload = item if isinstance(item, dict) else {"item": item}
            resolved.append({"error": message, "item": payload})

    # Phase 2: guard + execute in input order, against pre-resolved ids only.
    # `consumed` holds ids this batch has deleted (confirm) or successfully
    # previewed for deletion — the children guard runs net of it in BOTH
    # modes, so a [child, parent] batch previews exactly what confirm does.
    consumed: set[int] = set()
    results: list[dict] = []
    any_deleted = False
    try:
        for entry in resolved:
            if "error" in entry:
                results.append(
                    {"status": "error", "error": entry["error"], "item": entry["item"]}
                )
                continue
            row = entry["row"]
            resolved_id = row["id"]
            try:
                async with get_db() as db:
                    children = await db.execute_fetchall(
                        "SELECT id, name FROM games WHERE parent_game_id = ?",
                        (resolved_id,),
                    )
                surviving = [c for c in children if c["id"] not in consumed]
                if surviving:
                    # Same guard delete_game enforces by raising; surfaced as
                    # its own status so a repair loop can triage refusals
                    # apart from errors.
                    results.append({
                        "status": "refused",
                        "game_id": resolved_id,
                        "name": row["name"],
                        "error": (
                            f"parent of {len(surviving)} nested item(s) — "
                            "reparent or delete them first "
                            "(update_game/delete_game)"
                        ),
                        "children": [
                            {"game_id": c["id"], "name": c["name"]}
                            for c in surviving
                        ],
                    })
                    continue
                result = await delete_game(
                    game_id=resolved_id,
                    confirm=confirm,
                    recompute_affinity=False,
                    ignore_child_ids=frozenset(consumed),
                )
            except Exception as exc:  # noqa: BLE001 - isolation boundary: any failure becomes an error record
                message = (
                    str(exc) if isinstance(exc, ToolError)
                    else f"{type(exc).__name__}: {exc}"
                )
                results.append({
                    "status": "error",
                    "error": message,
                    "item": {"game_id": resolved_id, "name": row["name"]},
                })
                continue
            consumed.add(resolved_id)
            if result["deleted"]:
                any_deleted = True
            result.pop("hint", None)  # one top-level hint, not one per item
            results.append(
                {"status": "deleted" if result["deleted"] else "previewed", **result}
            )
    finally:
        # Committed deletes must never be left without their deferred recompute.
        if any_deleted:
            from ..data.db import recompute_tag_affinity
            await recompute_tag_affinity()

    def _sum_counts(key: str) -> dict[str, int]:
        totals: dict[str, int] = {}
        for r in results:
            for table, count in (r.get(key) or {}).items():
                totals[table] = totals.get(table, 0) + count
        return totals

    envelope: dict = {
        "results": results,
        "total": len(items),
        "previewed": count_status(results, "previewed"),
        "deleted": count_status(results, "deleted"),
        "refused": count_status(results, "refused"),
        "errors": count_status(results, "error"),
        "confirm": confirm,
    }
    if confirm:
        envelope["deleted_counts_total"] = _sum_counts("deleted_counts")
    else:
        envelope["would_delete_total"] = _sum_counts("would_delete")
        envelope["hint"] = "Re-run with confirm=True to permanently delete."
    return envelope


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
            now = datetime.now(UTC).isoformat()
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
