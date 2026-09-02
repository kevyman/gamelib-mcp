"""Data-integrity detectors backing ``tools/checks.py``.

These functions were their own MCP tools until ADR 0003 consolidated them into
``check_library``; ``tools/checks.py`` now adapts each one's output into a
finding. They are kept UNCHANGED here (same logic, same unit tests) and split
out of ``tools/admin.py`` purely so that module stays about identity repair and
sync orchestration. Read-mostly: only ``detect_farmed_games(dry_run=False)``
writes, and ``run_library_sync`` calls it post-Steam-sync (imported into
``tools/admin.py`` so ``patch("gamelib_mcp.tools.admin.detect_farmed_games")``
keeps working).
"""

import json
import logging
import statistics
from collections import defaultdict

from ..data.content import (
    CONTENT_BASE_GAME,
    CONTENT_DLC,
    NESTED_CONTENT_TYPES,
    match_addon_name,
)
from ..data.db import STEAM_APP_ID, get_db
from ..data.title_normalization import normalize_search_text

logger = logging.getLogger(__name__)


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


async def detect_orphan_games() -> dict:
    """Find primary-library games rows with no ownership and no wishlist entry.

    ``is_primary_library_item`` is a content-type flag (real game vs
    DLC/soundtrack/edition) — it says nothing about ownership. A games row can
    legitimately exist with zero ``game_platforms`` rows in two shapes:

    * wishlist-only (a ``game_wishlist`` row exists) — a normal, intentional
      shape produced by ``sync_wishlist``/``add_game_to_platform(owned=False)``.
      Counted in ``wishlist_only_count`` but not returned as a candidate.
    * assessment-only (a ``game_assessments`` row exists) — the row
      ``record_assessment`` mints for a candidate that was evaluated but
      neither bought nor wishlisted (a "skip" verdict is exactly this shape).
      Counted in ``assessment_only_count``, never an orphan: deleting it would
      erase the recorded verdict the calibration report reads.
    * a true orphan (no ``game_platforms`` row AND no ``game_wishlist`` row) —
      e.g. a wishlist entry that was later removed upstream
      (``delete_stale_wishlist_entries``) without ever being owned, leaving the
      ``games`` row dangling with nothing pointing at it. These are returned in
      ``orphans`` for review; no write happens (use ``delete_game``'s two-step
      confirm for genuine phantoms, or ``merge_games`` to consolidate — a
      false positive would silently destroy a game row and its ratings/series
      links).

    A third shape is reported separately, NOT as an orphan: a ``phantom_parent``
    — zero ownership and zero wishlist, but other rows nest under it (typically
    the empty base-game shell a wrong edition classification minted while the
    OWNED edition row sat nested beneath it). These are not deletable
    (``delete_game`` refuses parents by design) and deleting one would discard
    a row that represents an owned game. Remediate by merging
    (``merge_games(source_game_id=<phantom>, target_game_id=<owned child>)``,
    which re-points siblings and promotes the child) or by reclassifying the
    child via ``update_game``; ``detect_misclassified_dlc`` surfaces the same
    pairs with suggested updates.

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
            """SELECT g.id AS game_id, g.name, g.igdb_id,
                      EXISTS (SELECT 1 FROM game_assessments a
                              WHERE a.game_id = g.id) AS has_assessment,
                      (SELECT COUNT(*) FROM games c WHERE c.parent_game_id = g.id)
                          AS child_count,
                      (SELECT COUNT(*) FROM games c
                        WHERE c.parent_game_id = g.id
                          AND EXISTS (SELECT 1 FROM game_platforms gp
                                      WHERE gp.game_id = c.id AND gp.owned = 1))
                          AS owned_child_count
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

    orphans = []
    phantom_parents = []
    assessment_only_count = 0
    for row in orphan_rows:
        if row["child_count"]:
            phantom_parents.append(
                {
                    "game_id": row["game_id"],
                    "name": row["name"],
                    "igdb_id": row["igdb_id"],
                    "child_count": row["child_count"],
                    "owned_child_count": row["owned_child_count"],
                    "remediation": (
                        "not deletable (parent of nested content) — merge into "
                        "the owned child (merge_games) or reclassify the child "
                        "(update_game); see detect_misclassified_dlc"
                    ),
                }
            )
        elif row["has_assessment"]:
            # A recorded verdict is what points at this row; it is no more an
            # orphan than a wishlist entry is. Counted, never listed.
            assessment_only_count += 1
        else:
            orphans.append(
                {
                    "game_id": row["game_id"],
                    "name": row["name"],
                    "igdb_id": row["igdb_id"],
                }
            )
    remaining_raw = await get_meta(AUDIT_REMAINING_META_KEY)
    return {
        "orphans": orphans,
        "orphan_count": len(orphans),
        "phantom_parents": phantom_parents,
        "phantom_parent_count": len(phantom_parents),
        "wishlist_only_count": wishlist_only_row["c"] if wishlist_only_row else 0,
        "assessment_only_count": assessment_only_count,
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


# The addon-name pattern table lives in data/content.py (match_addon_name) —
# shared with the Humble purchase importer's content_type hint.
_MISCLASSIFIED_BUCKET_CAP = 200


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
    can be patched in tests (gamelib_mcp.tools.detectors._fetch_steam_appdetails).
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
    nested_parent, needs_parent, wrong_parent_suspect, purchase_minted_suspect,
    addon_name_pattern):

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
    * wrong_parent_suspect — a nested row whose parent link looks wrong: the
      child holds a store identifier + real playtime while the parent holds
      neither (the shape today's substance guard refuses), the child's name
      is a proper prefix of the parent's and the two conflict on sequel
      identity ("Mass Effect" nested under "Mass Effect 3"), or the child is
      an OWNED edition row while nothing owns the parent (the shell shape
      edition_hides_owned_game now refuses — an owned edition is the game's
      ownership record). The residue of pre-gate IGDB fuzzy matching.
      Suggests content_type base_game (update_game promotes the child and
      detaches the parent); for the owned-edition shape merge_games
      (source=parent, target=child) folds the shell in instead.
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
    from ..data.db import (
        edition_hides_owned_game,
        get_game_substance,
        nesting_substance_conflict,
        titles_conflict_on_identity,
    )

    candidates: list[dict] = []
    counts = {
        "inconsistent_primary_nested": 0,
        "nested_parent": 0,
        "needs_parent": 0,
        "wrong_parent_suspect": 0,
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

    # --- offline bucket: wrong_parent_suspect (nested under the wrong game) ---
    # The residue of pre-gate IGDB fuzzy matching: a real library title matched
    # onto some OTHER game's DLC/edition record and got nested under that
    # game's (often freshly minted, ownerless) row — "A Hat in Time" as DLC of
    # "Among Us 3D: VR", "DiRT Rally" as DLC of "DiRT Rally 2.0". Two
    # fingerprints, either suffices:
    #   * retro substance conflict — the child carries a store identifier AND
    #     real playtime while the parent carries neither (today's
    #     nesting_substance_conflict guard would refuse this write; stored
    #     rows predate it);
    #   * base-under-sibling shape — the child's normalized name is a proper
    #     PREFIX of the parent's and the two conflict on sequel identity
    #     ("Mass Effect" under "Mass Effect 3 (2012)"). Restricted to the
    #     child-is-prefix direction (legit DLC is the parent's name PLUS a
    #     suffix, never a prefix of it) and to children without an addon-ish
    #     name, so "Borderlands 3: Season Pass 2" stays unflagged.
    #   * owned edition under an unowned parent — the child is an 'edition'
    #     row with real ownership while nothing owns the parent (the shell
    #     shape edition_hides_owned_game now refuses to write; stored rows
    #     predate the guard). The owned edition IS the game — if the parent
    #     is the same game, merge_games(source=parent, target=child) folds
    #     the shell in; the suggested promotion works too, leaving the shell
    #     for detect_orphan_games.
    # Suggests content_type=base_game, which promotes the child and detaches
    # the wrong parent in one update_game call.
    async with get_db() as db:
        parented_rows = await db.execute_fetchall(
            """SELECT g.id AS game_id, g.name, g.content_type, g.manual_overrides,
                      g.parent_game_id, p.name AS parent_name
               FROM games g
               JOIN games p ON p.id = g.parent_game_id
               WHERE g.is_primary_library_item = 0
               ORDER BY g.id
               LIMIT ?""",
            (_MISCLASSIFIED_BUCKET_CAP,),
        )
    wrong_parent_count = 0
    for row in parented_rows:
        if row["game_id"] in stranded_ids:
            continue
        if {"content_type", "parent_game_id"} & _pinned_columns(row["manual_overrides"]):
            continue
        async with get_db() as db:
            substance_conflict = await nesting_substance_conflict(
                db, row["game_id"], row["parent_game_id"]
            )
            edition_ownership_conflict = row["content_type"] == "edition" and (
                await edition_hides_owned_game(
                    db, row["game_id"], row["parent_game_id"]
                )
            )
        child_norm = normalize_search_text(row["name"] or "")
        parent_norm = normalize_search_text(row["parent_name"] or "")
        sibling_shape = bool(
            child_norm
            and child_norm != parent_norm
            and parent_norm.startswith(child_norm)
            and titles_conflict_on_identity(row["name"] or "", row["parent_name"] or "")
            and match_addon_name(row["name"]) is None
        )
        if not substance_conflict and not sibling_shape and not edition_ownership_conflict:
            continue
        wrong_parent_count += 1
        candidates.append(
            {
                "game_id": row["game_id"],
                "name": row["name"],
                "reason": "wrong_parent_suspect",
                "evidence": {
                    "content_type": row["content_type"],
                    "parent_game_id": row["parent_game_id"],
                    "parent_name": row["parent_name"],
                    "substance_conflict": substance_conflict,
                    "sibling_identity_conflict": sibling_shape,
                    "edition_ownership_conflict": edition_ownership_conflict,
                },
                "suggested_update": {
                    "game_id": row["game_id"],
                    "content_type": CONTENT_BASE_GAME,
                },
            }
        )
    counts["wrong_parent_suspect"] = wrong_parent_count

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
        addon = match_addon_name(name)
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
            except Exception as exc:  # noqa: BLE001 - isolation boundary: any failure becomes an error record
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


async def _steam_appids_for_games(game_ids: list[int]) -> dict[int, str]:
    """{game_id: steam_appid} for the given games (one appid per game)."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" * len(game_ids))
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT gp.game_id AS game_id, MIN(gpi.identifier_value) AS appid
                FROM game_platform_identifiers gpi
                JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                WHERE gpi.identifier_type = ?
                  AND gp.game_id IN ({placeholders})
                GROUP BY gp.game_id""",
            (STEAM_APP_ID, *game_ids),
        )
    return {row["game_id"]: str(row["appid"]) for row in rows if row["appid"] is not None}


async def revalidate_igdb_matches(
    dry_run: bool = True,
    limit: int | None = None,
    include_edition_suffix: bool = False,
) -> dict:
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

    A name difference is not automatically a WRONG match: a library row named
    for an edition SKU ("Nioh 2 - The Complete Edition", "Cities XL Platinum",
    "Mass Effect (2007)") is correctly linked to the base game's IGDB record,
    and resetting it would throw away good enrichment for nothing. Both names
    therefore also go through normalize_edition_comparison_title; when they
    agree there, the row is classified ``drift_kind="edition_suffix"`` and
    reported separately in ``edition_suffix_matches`` — never reset. Only
    ``drift_kind="wrong_entity"`` rows land in ``mismatches``.
    ``include_edition_suffix=True`` folds the edition rows back into
    ``mismatches`` (carrying their drift_kind) for a caller that really does
    want them repinned.

    Neither is a link IGDB's own ``external_games`` maps the row's Steam appid
    to. That mapping is authoritative and ``backfill_missing_games`` applies it
    ahead of any name check, so resetting such a row only makes the next
    backfill re-pin the identical id — a permanent loop (prod: "FTL: Faster
    Than Light" ↔ 178437, whose IGDB record is named "Faster than light?").
    Those land in ``store_authoritative_matches`` with
    ``drift_kind="store_authoritative"`` and are never reset; the batched
    external_games lookup covers only the already-mismatched rows.

    dry_run=True (default) only reports mismatches. dry_run=False resets the
    IGDB enrichment on mismatched rows — igdb_id/igdb_platforms/
    igdb_cached_at/igdb_claimed_at and the (unpinned) cover_image_id to NULL,
    and that game's game_series_membership rows deleted (all of it came from
    the bad match; the cover is literally the wrong game's art) — so
    background enrichment re-resolves them under the strict gate. Rows whose
    igdb_id is listed in games.manual_overrides are reported separately and
    never reset. limit caps how many rows are checked (None/0 = all).

    A bad match can also have written a content classification: a library
    title fuzzy-matched onto some other game's DLC/edition record got
    content_type/parent_game_id/is_primary_library_item set from that record
    (prod: "A Hat in Time" nested as DLC under a minted "Among Us 3D: VR" row
    because the match landed on one of that game's cosmetic packs). Resetting
    only the link would leave the row demoted and invisibly parented under
    the wrong game. So each mismatch is checked for classification damage
    ATTRIBUTABLE to the bad record — the stored parent row matches the bad
    record's parent/version_parent (by igdb id or by the exact name a parent
    mint would have used), or the stored content_type equals what the bad
    record's category/version_parent implies (when that isn't plain
    base_game) — and attributable rows are reset to base_game / primary / no
    parent so re-enrichment can re-derive the truth. Rows with any of the
    three classification columns pinned in manual_overrides keep their
    classification. Each mismatch entry carries ``classification_reset``
    (would-be in dry_run) and the result ``classification_reset_count``.
    """
    from ..data.content import content_type_from_igdb_category
    from ..data.db import get_manual_overrides
    from ..data.igdb import (
        fetch_igdb_game_records,
        igdb_credentials_configured,
        resolve_steam_appids_to_igdb,
    )
    from ..data.title_normalization import (
        normalize_edition_comparison_title,
        normalize_series_gap_title,
    )

    igdb_configured = igdb_credentials_configured()

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT id, name, igdb_id, content_type, parent_game_id,
                      is_primary_library_item
               FROM games WHERE igdb_id IS NOT NULL ORDER BY id"""
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
        "classification_reset_count": 0,
        "skipped_overridden": 0,
        "unresolved_igdb_ids": 0,
        "edition_suffix_count": 0,
        "edition_suffix_matches": [],
        "store_authoritative_count": 0,
        "store_authoritative_matches": [],
    }
    if not igdb_configured or not rows:
        return result

    igdb_records = await fetch_igdb_game_records([row["igdb_id"] for row in rows])

    def _expected_content_type(record: dict) -> str:
        """content_type the bad record's classification path would have written."""
        if record.get("version_parent_igdb_id") or record.get("version_parent_name"):
            return "edition"
        category = record.get("category")
        if category is None:
            category = record.get("game_type")
        return content_type_from_igdb_category(category)

    async def _classification_attributable(db, row, record: dict) -> bool:
        """Whether the stored classification plausibly came from the bad match."""
        stored_default = (
            (row["content_type"] or "base_game") == "base_game"
            and row["parent_game_id"] is None
            and bool(row["is_primary_library_item"])
        )
        if stored_default:
            return False
        if row["parent_game_id"] is not None:
            parent = await db.execute_fetchone(
                "SELECT igdb_id, name FROM games WHERE id = ?",
                (row["parent_game_id"],),
            )
            if parent is not None:
                record_parent_ids = {
                    record.get("parent_igdb_id"),
                    record.get("version_parent_igdb_id"),
                } - {None}
                if parent["igdb_id"] in record_parent_ids:
                    return True
                record_parent_names = {
                    name.casefold()
                    for name in (
                        record.get("parent_name"),
                        record.get("version_parent_name"),
                    )
                    if name
                }
                if (parent["name"] or "").casefold() in record_parent_names:
                    return True
        expected = _expected_content_type(record)
        return expected != "base_game" and row["content_type"] == expected

    mismatches: list[dict] = []
    edition_suffix_matches: list[dict] = []
    skipped_overridden = 0
    unresolved = 0
    classification_resets: list[int] = []
    async with get_db() as db:
        for row in rows:
            record = igdb_records.get(row["igdb_id"])
            if record is None:
                # IGDB no longer returns this id (deleted/merged upstream) —
                # can't validate the name, so don't touch the row.
                unresolved += 1
                continue
            igdb_name = record["name"]
            if normalize_series_gap_title(row["name"]) == normalize_series_gap_title(
                igdb_name
            ):
                continue
            # The library name is the IGDB name wearing an edition/SKU suffix —
            # a correct link, not drift. Reported, never reset (unless the
            # caller explicitly asks for those too).
            drift_kind = (
                "edition_suffix"
                if normalize_edition_comparison_title(row["name"])
                == normalize_edition_comparison_title(igdb_name)
                else "wrong_entity"
            )
            if drift_kind == "edition_suffix" and not include_edition_suffix:
                edition_suffix_matches.append(
                    {
                        "game_id": row["id"],
                        "name": row["name"],
                        "igdb_id": row["igdb_id"],
                        "igdb_name": igdb_name,
                        "drift_kind": drift_kind,
                    }
                )
                continue
            overrides = await get_manual_overrides(db, row["id"])
            if "igdb_id" in overrides:
                skipped_overridden += 1
                continue
            classification_pinned = bool(
                {"content_type", "parent_game_id", "is_primary_library_item"}
                & set(overrides)
            )
            reset_classification = (
                not classification_pinned
                and await _classification_attributable(db, row, record)
            )
            if reset_classification:
                classification_resets.append(row["id"])
            mismatches.append(
                {
                    "game_id": row["id"],
                    "name": row["name"],
                    "igdb_id": row["igdb_id"],
                    "igdb_name": igdb_name,
                    "classification_reset": reset_classification,
                    "drift_kind": drift_kind,
                }
            )

    # A link IGDB's own external_games maps this Steam appid to is not drift,
    # whatever the names look like: it is the authoritative store→game mapping,
    # and backfill_missing_games consults it BEFORE any name check. Resetting
    # such a row just makes the next backfill re-apply the identical link —
    # observed in prod as "FTL: Faster Than Light" ↔ IGDB 178437 ("Faster than
    # light?"), a permanent report/reset/re-pin loop. One batched lookup, over
    # the mismatched rows only.
    store_authoritative: list[dict] = []
    if mismatches:
        appid_by_game = await _steam_appids_for_games(
            [mismatch["game_id"] for mismatch in mismatches]
        )
        if appid_by_game:
            try:
                external = await resolve_steam_appids_to_igdb(
                    sorted(set(appid_by_game.values()))
                )
            except Exception as exc:  # noqa: BLE001 - isolation boundary: any failure becomes an error record
                # Report-only degradation: without the mapping we cannot prove a
                # link is store-authoritative, so keep every mismatch (a reset
                # is still recoverable; a silent skip would hide real drift).
                logger.warning("IGDB external_games check failed during drift audit: %s", exc)
                external = {}
            kept: list[dict] = []
            for mismatch in mismatches:
                appid = appid_by_game.get(mismatch["game_id"])
                if appid is not None and external.get(appid) == mismatch["igdb_id"]:
                    store_authoritative.append({**mismatch, "drift_kind": "store_authoritative"})
                    if mismatch["classification_reset"]:
                        classification_resets.remove(mismatch["game_id"])
                    continue
                kept.append(mismatch)
            mismatches = kept

    async with get_db() as db:
        reset_count = 0
        if not dry_run and mismatches:
            for mismatch in mismatches:
                # cover_image_id goes too (unless hand-pinned): it is the WRONG
                # game's art. Re-enrichment overwrites it when the row
                # re-resolves, but a row that never finds a match would
                # otherwise keep showing the wrong cover forever.
                await db.execute(
                    """UPDATE games
                       SET igdb_id = NULL,
                           igdb_platforms = NULL,
                           igdb_cached_at = NULL,
                           igdb_claimed_at = NULL,
                           cover_image_id = CASE
                               WHEN manual_overrides IS NOT NULL
                                    AND 'cover_image_id' IN (
                                        SELECT value FROM json_each(manual_overrides))
                               THEN cover_image_id
                               ELSE NULL
                           END
                       WHERE id = ?""",
                    (mismatch["game_id"],),
                )
                await db.execute(
                    "DELETE FROM game_series_membership WHERE game_id = ?",
                    (mismatch["game_id"],),
                )
                if mismatch["classification_reset"]:
                    await db.execute(
                        """UPDATE games
                           SET content_type = 'base_game',
                               parent_game_id = NULL,
                               is_primary_library_item = 1
                           WHERE id = ?""",
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
            "classification_reset_count": (
                len(classification_resets) if not dry_run else 0
            ),
            "skipped_overridden": skipped_overridden,
            "unresolved_igdb_ids": unresolved,
            "edition_suffix_count": len(edition_suffix_matches),
            "edition_suffix_matches": edition_suffix_matches,
            "store_authoritative_count": len(store_authoritative),
            "store_authoritative_matches": store_authoritative,
        }
    )
    return result
