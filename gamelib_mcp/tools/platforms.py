"""Platform-row implementations: breakdown, ownership, game edits, playtime pins."""

import json
import logging
import math
import re
import urllib.parse
from datetime import date

from fastmcp.exceptions import ToolError

from ..data.content import (
    NESTED_CONTENT_TYPES,
    PRIMARY_CONTENT_TYPES,
    derive_is_primary,
)
from ..data.db import (
    GAME_EDITABLE_FIELDS,
    NINTENDO_BASELINE_DEVICE_ID,
    NINTENDO_BASELINE_PERIOD_KEY,
    PLATFORM_EDITABLE_FIELDS,
    apply_manual_game_fields,
    apply_manual_platform_fields,
    clear_fulfilled_wishlist_entries,
    delete_nintendo_playtime_baseline,
    edition_hides_owned_game,
    fts_ready,
    get_db,
    get_game_by_identifier,
    get_manual_overrides,
    get_nintendo_baseline_minutes,
    get_nintendo_synced_minutes,
    get_platform_manual_overrides,
    get_steam_appid_for_game,
    has_nested_children,
    invalidate_igdb_match_enrichment,
    invalidate_name_derived_enrichment,
    nesting_substance_conflict,
    normalize_identifier_value,
    recompute_tag_affinity,
    remove_manual_overrides,
    remove_platform_manual_overrides,
    resolve_parent_game,
    set_meta,
    set_platform_acquisition,
    set_platform_ownership,
    upsert_game,
    upsert_game_platform,
    upsert_game_platform_identifier,
    upsert_nintendo_play_summary,
    upsert_wishlist_entry,
)
from ..data.nintendo import NINTENDO_TITLE_ID
from ..data.steam_wishlist import SteamWishlistPushError, push_to_steam_wishlist
from ..data.tag_synonyms import canonical_tag

# Safe direction: acquisition.py never imports this module at top level (it
# lazy-imports _resolve_game_row inside functions), so importing its validator
# helpers here cannot form a cycle.
from .acquisition import _validate_acquired_at
from .acquisition import _validated_fields as _validated_acquisition_fields
from .batch import apply_batch_item, check_batch_items, count_status
from .common import (
    LIBRARY_PLATFORMS,
)
from .common import (
    validate_platform as _validate_platform,
)
from .search import (
    NORMALIZED_NAME_SQL,
    build_name_match,
    fuzzy_fallback_game_ids,
)

logger = logging.getLogger(__name__)

COMPLETION_STATUSES = {"playing", "completed", "abandoned", "evergreen"}
CONTENT_TYPES = PRIMARY_CONTENT_TYPES | NESTED_CONTENT_TYPES

# ADR 0006 / issue #110 phase 1: hand-writable wishlist sources for
# add_game_to_platform(owned=False). Deliberately excludes the sync-reserved
# sources ("steam", "dekudeals") — a hand-written row claiming one of those
# would become deletable by that sync's source-scoped removal reconciliation
# (delete_stale_wishlist_entries), which assumes every row it didn't just see
# is genuinely gone from the upstream list.
WISHLIST_SOURCES = {"manual", "assessment"}


async def get_platform_breakdown(overlap_limit: int = 25) -> dict:
    """
    Return per-platform game counts, total unique games, and overlap list
    (games owned on 2+ platforms).

    Counts split games (primary library items) from addons (owned DLC/
    expansions/editions/bundles etc.) — an owned addon no longer inflates
    owned_games/total_unique_games/overlap_games; it's reported separately
    via owned_addons/total_unique_addons so DLC ownership stays visible
    without corrupting the "how many games do I own" numbers.

    overlap_games is CAPPED at overlap_limit (most-platforms first, then most
    playtime). It is the only field here that grows with library size — at
    ~3k owned rows it was 428 entries and 98% of this response — so the full
    list is deliberately not returned. overlap_count always reports the true
    total and overlap_truncated says whether the list was cut; page through
    the rest with get_library_stats or query_library.
    """
    overlap_limit = max(0, min(int(overlap_limit), 200))
    async with get_db() as db:
        platform_rows = await db.execute_fetchall(
            """SELECT gp.platform AS platform,
                      COUNT(DISTINCT CASE WHEN g.is_primary_library_item = 1
                                           THEN gp.game_id END) AS owned_games,
                      COUNT(DISTINCT CASE WHEN g.is_primary_library_item = 0
                                           THEN gp.game_id END) AS owned_addons
               FROM game_platforms gp
               JOIN games g ON g.id = gp.game_id
               WHERE gp.owned = 1
               GROUP BY gp.platform
               ORDER BY owned_games DESC"""
        )

        total = await db.execute_fetchone(
            """SELECT COUNT(DISTINCT CASE WHEN g.is_primary_library_item = 1
                                           THEN gp.game_id END) AS games,
                      COUNT(DISTINCT CASE WHEN g.is_primary_library_item = 0
                                           THEN gp.game_id END) AS addons
               FROM game_platforms gp
               JOIN games g ON g.id = gp.game_id
               WHERE gp.owned = 1"""
        )

        overlap_total = await db.execute_fetchone(
            """SELECT COUNT(*) AS c FROM (
                   SELECT g.id
                   FROM games g
                   JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
                   WHERE g.is_primary_library_item = 1
                   GROUP BY g.id
                   HAVING COUNT(gp.platform) >= 2
               )"""
        )
        # Ordered so a truncated page is the most interesting slice, not an
        # arbitrary one: widest ownership first, then most-played.
        overlap_rows = await db.execute_fetchall(
            """SELECT g.name, g.id AS game_id,
                      COUNT(gp.platform) AS platform_count,
                      GROUP_CONCAT(gp.platform) AS platforms
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
               WHERE g.is_primary_library_item = 1
               GROUP BY g.id
               HAVING platform_count >= 2
               ORDER BY platform_count DESC,
                        COALESCE(SUM(gp.playtime_minutes), 0) DESC,
                        g.name ASC
               LIMIT ?""",
            (overlap_limit,),
        )

    return {
        "by_platform": [
            {
                "platform": r["platform"],
                "owned_games": r["owned_games"],
                "owned_addons": r["owned_addons"],
            }
            for r in platform_rows
        ],
        "total_unique_games": total["games"],
        "total_unique_addons": total["addons"],
        # The true total, independent of the cap below.
        "overlap_count": overlap_total["c"],
        "overlap_truncated": overlap_total["c"] > len(overlap_rows),
        "overlap_limit": overlap_limit,
        "overlap_games": [
            {
                "game_id": r["game_id"],
                "name": r["name"],
                "owned_on": r["platforms"].split(","),
            }
            for r in overlap_rows
        ],
    }


async def get_wishlist(
    platform: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """
    List wishlist items — games marked wanted but not necessarily owned.

    platform: optional filter (e.g. "steam", "switch2"); omit for all platforms.
    Populated by sync_wishlist (Steam, DekuDeals→switch2) or by
    add_game_to_platform(owned=False) for manual entries (e.g. PSN). owned
    reflects live game_platforms state — normally False, since sync_wishlist
    and add_game_to_platform both clear an entry once it's actually owned;
    True here is a transient diagnostic (ownership was just established and
    the next cleanup pass hasn't run yet), not a common case.

    Paginated: the wishlist grows without bound, so results are capped at
    `limit` (newest first). count is the page size, total_matches the true
    total, has_more whether another page exists.
    """
    resolved_platform = _validate_platform(platform, LIBRARY_PLATFORMS) if platform else None
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    where = "WHERE 1=1"
    params: list = []
    if resolved_platform:
        where += " AND w.platform = ?"
        params.append(resolved_platform)

    async with get_db() as db:
        total = await db.execute_fetchone(
            f"SELECT COUNT(*) AS c FROM game_wishlist w JOIN games g ON g.id = w.game_id {where}",
            params,
        )
        rows = await db.execute_fetchall(
            f"""SELECT g.id AS game_id, g.name, g.content_type, w.platform,
                       w.wishlisted_at, w.source,
                       EXISTS (
                           SELECT 1 FROM game_platforms gp
                           WHERE gp.game_id = w.game_id AND gp.platform = w.platform AND gp.owned = 1
                       ) AS owned
                FROM game_wishlist w
                JOIN games g ON g.id = w.game_id
                {where}
                ORDER BY w.wishlisted_at DESC, w.id DESC
                LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        )

    return {
        "count": len(rows),
        "total_matches": total["c"],
        "has_more": offset + len(rows) < total["c"],
        "items": [
            {
                "game_id": r["game_id"],
                "name": r["name"],
                "platform": r["platform"],
                "wishlisted_at": r["wishlisted_at"],
                "source": r["source"],
                "owned": bool(r["owned"]),
                "content_type": r["content_type"],
            }
            for r in rows
        ],
    }


async def set_hardware_preference(platforms: list[str]) -> dict:
    """
    Set your hardware preference order for discover_games suggested_platform.

    platforms: ordered list, highest priority first.
    e.g. ["switch2", "ps5", "steam"]

    Valid values: steam, epic, gog, switch2 (aka nintendo/switch), ps5, itchio, xbox, ea (aka origin), ubisoft (aka uplay), other.
    """
    normalized = [_validate_platform(p, LIBRARY_PLATFORMS) for p in platforms]
    await set_meta("hardware_preference", json.dumps(normalized))
    return {"hardware_preference": normalized}


async def add_game_to_platform(
    name: str | None = None,
    platform: str | None = None,
    game_id: int | None = None,
    identifier_type: str | None = None,
    identifier_value: str | None = None,
    playtime_minutes: int | None = None,
    owned: bool = True,
    acquired_at: str | None = None,
    price_paid: float | None = None,
    price_currency: str | None = None,
    purchase_source: str | None = None,
    bundle_name: str | None = None,
    delisted: bool | None = None,
    unowned_at: str | None = None,
    *,
    dry_run: bool = False,
    push_to_store: bool = False,
    wishlist_source: str | None = None,
) -> dict:
    """
    Manually add a game to a platform — useful for games that aren't fetched
    automatically (e.g. physical copies, unreported digital titles), or to
    record a wishlist item on a platform with no wishlist sync (e.g. PSN, which
    has no public wishlist API — pass owned=False there).

    name: Game name (matches an existing game by EXACT name, or creates a new
        one — a typo therefore mints a phantom row rather than erroring; pass
        game_id instead when correcting an existing row, or dry_run first and
        assert created=false)
    game_id: Target an existing game by id instead of by name. Never creates a
        game (an unknown id is an error), which makes it the safe way to edit a
        row that already exists. Provide name or game_id, not both.
    platform: steam | epic | gog | nintendo | switch2 | ps5 | itchio | xbox | ea | ubisoft | other (aliases: origin→ea, uplay→ubisoft)
    identifier_type: Optional store identifier type (e.g. 'steam_appid', 'gog_product_id').
        With owned=True, attaches to the new platform-ownership row. With
        owned=False, only 'steam_appid' (with platform='steam') is accepted —
        it's stored as the wishlist entry's store_identifier so
        get_wishlist_deals can price it via ITAD immediately, without waiting
        on a sync_wishlist run to discover the same appid.
    identifier_value: Optional store identifier value
    playtime_minutes: Optional known playtime in minutes
    owned: True (default) records an owned copy; False records a wishlist entry
        instead (playtime_minutes is ignored in that case). Either way, any
        existing wishlist entry for this game+platform that's now fulfilled is
        cleared.
    wishlist_source: owned=False only. Labels the wishlist row's origin —
        "manual" (default when omitted) for a hand-curated entry, "assessment"
        for a promotion out of a game-quality verdict ("wishlist for sale" →
        a price-watched row) so it stays distinct from hand-curated entries and
        is bulk-removable later by source. Any other value is rejected,
        including the sync-reserved sources ("steam", "dekudeals") — see
        WISHLIST_SOURCES above for why.
    acquired_at / price_paid / price_currency / purchase_source / bundle_name:
        optional acquisition details recorded on the new ownership row, with
        the same validation and vocabulary as set_acquisition. They require
        owned=True — a wishlist entry has no platform-ownership row to record
        them on.
    delisted: correct the ownership row's delisted flag (True = the store page
        is gone and ownership comes from the account license list; False = the
        game is still listed). Requires owned=True and is the only write path
        for this column, which check_library's ownership.license_gap otherwise
        sets on its own. Setting it records a manual override on the
        game_platforms row, so neither the Steam sync nor the license audit
        flips it back; hand it back to sync with
        set_playtime(clear=["delisted"]).
    unowned_at: record that ownership on this platform ENDED — a refund, a
        revoked key, or a lapsed subscription title. Takes the date it ended
        (YYYY / YYYY-MM / YYYY-MM-DD) and flips the EXISTING ownership row to
        owned=0, keeping its acquisition history, identifiers, and playtime
        (delete_game would cascade every other platform away). Every aggregate
        already filters owned=1, so the row stops counting toward spending,
        duplication, and platform counts the moment it is stamped. Requires
        owned=True and an existing platform row — this never mints one, and it
        is not a wishlist entry (owned=False is). It pins `owned` as a manual
        override so a source that keeps listing the title can't re-own it; pass
        unowned_at="none" to undo (clears the stamp, restores owned=1, releases
        the pin) when you buy it again.
    push_to_store: opt-in only (default False never pushes anything implicitly),
        wishlist-only (requires owned=False — combining it with owned=True
        raises before any write). The local wishlist row is always written
        first and always survives, even if the push fails — a push failure
        lands in the response's store_push field, never raises, never rolls
        back the local write. On steam, resolves a Steam appid (identifier_type
        ='steam_appid'/identifier_value, else an existing steam_appid on file
        for this game, even from an owned=0 row — a refunded copy still knows
        the appid) and pushes it to the REAL Steam wishlist via the stored web
        session (create_session_ingest_link(provider="steam_refresh")); no
        resolvable appid means the push is not attempted at all. On switch2,
        DekuDeals has no wishlist write API, so store_push instead returns a
        DekuDeals search link to add it there by hand. Other platforms report
        no push available. dry_run never pushes — store_push is always None on
        a dry run. Composes orthogonally with wishlist_source — an
        assessment-sourced row may also push — with no special-casing. A
        successful push still leaves the local row's source whatever
        wishlist_source resolved to (default "manual") until the next
        sync(targets=["wishlist"]) re-observes it store-side and converges it
        to source="steam" (game_wishlist is UNIQUE(game_id, platform), so it's
        the same row either way).
    """
    if platform is None:
        raise ToolError("platform is required")
    # Resolve aliases (e.g. "nintendo" → "switch2") and validate in one step.
    platform = _validate_platform(platform, LIBRARY_PLATFORMS)

    if (name is None) == (game_id is None):
        raise ToolError("Provide exactly one of name or game_id")
    if name is not None:
        name = name.strip()
        if not name:
            raise ToolError("name must not be empty")
    if playtime_minutes is not None and playtime_minutes < 0:
        raise ToolError("playtime_minutes must not be negative")
    acquisition_params = (
        acquired_at, price_paid, price_currency, purchase_source, bundle_name
    )
    if not owned and any(value is not None for value in acquisition_params):
        raise ToolError(
            "Acquisition fields require owned=True — a wishlist entry "
            "(owned=False) has no platform-ownership row to record them on"
        )
    if not owned and delisted is not None:
        raise ToolError(
            "delisted requires owned=True — it describes a platform-ownership "
            "row, which a wishlist entry (owned=False) has none of"
        )
    if not owned and unowned_at is not None:
        raise ToolError(
            "unowned_at requires owned=True — it retires an EXISTING ownership "
            "row (refund/revoked key/lapsed subscription). owned=False records "
            "a wishlist entry instead, which is a different thing entirely"
        )
    if push_to_store and owned:
        raise ToolError(
            "push_to_store pushes a wishlist add to the store — it requires "
            "owned=False"
        )
    if wishlist_source is not None and owned:
        raise ToolError(
            "wishlist_source describes a wishlist entry — it requires owned=False"
        )
    resolved_wishlist_source = "manual"
    if wishlist_source is not None:
        normalized_wishlist_source = wishlist_source.strip().lower()
        if normalized_wishlist_source not in WISHLIST_SOURCES:
            raise ToolError(
                f"Unknown wishlist_source '{wishlist_source}'. "
                f"Valid: {sorted(WISHLIST_SOURCES)}"
            )
        resolved_wishlist_source = normalized_wishlist_source
    # "none" is the release sentinel (as on update_game's completion_status),
    # so it has to be recognized before the date validator sees it.
    restore_ownership = isinstance(unowned_at, str) and unowned_at.strip().lower() == "none"
    unowned_stamp: str | None = None
    if unowned_at is not None and not restore_ownership:
        unowned_stamp = _validate_acquired_at(str(unowned_at), "unowned_at")
    # Validate before any write so a bad price/source/date leaves no partial row.
    acquisition_fields = _validated_acquisition_fields(*acquisition_params)
    if not owned:
        if identifier_type not in (None, "steam_appid"):
            raise ToolError(
                "identifier_type on a wishlist entry (owned=False) only supports "
                "'steam_appid'"
            )
        if identifier_type == "steam_appid" and platform != "steam":
            raise ToolError("identifier_type='steam_appid' requires platform='steam'")

    # Check whether the game already exists before upserting. A game_id target
    # must already exist — it can only ever edit, never mint.
    async with get_db() as db:
        if game_id is not None:
            existing = await db.execute_fetchone(
                "SELECT id, name FROM games WHERE id = ?", (game_id,)
            )
            if existing is None:
                raise ToolError(f"No game with id {game_id}")
            name = existing["name"]
        else:
            existing = await db.execute_fetchone(
                "SELECT id FROM games WHERE lower(name) = lower(?) ORDER BY id LIMIT 1",
                (name,),
            )
    created = existing is None

    # unowned_at edits an ownership row that must already exist. Minting a game
    # (or a platform row) only to immediately mark it un-owned would record a
    # purchase that never happened — a typo'd name has to be an error here, not
    # a phantom row, which is the one place this tool's mint-on-miss behavior
    # would actively corrupt the spend/duplication picture it is fixing.
    existing_platform_row = None
    if unowned_at is not None:
        if existing is not None:
            async with get_db() as db:
                existing_platform_row = await db.execute_fetchone(
                    "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
                    (existing["id"], platform),
                )
        if existing_platform_row is None:
            raise ToolError(
                f"unowned_at needs an existing {platform} ownership row to retire"
                + (
                    f" — no game matches '{name}'"
                    if existing is None
                    else f" — '{name}' has no {platform} row"
                )
            )

    if dry_run:
        # Same validation path as the wet run, no writes. A to-be-created game
        # reports game_id null (matching set_acquisitions_batch's convention);
        # acquisition previews the validated fields rather than post-write state.
        if owned:
            identifier = (
                {"type": identifier_type, "value": identifier_value}
                if identifier_type and identifier_value
                else None
            )
        else:
            identifier = (
                {"type": "steam_appid", "value": identifier_value}
                if identifier_type == "steam_appid" and identifier_value
                else None
            )
        return {
            "created": created,
            "game_id": existing["id"] if existing else None,
            "game_platform_id": None,
            "wishlist_id": None,
            "name": name,
            "platform": platform,
            "owned": owned and (unowned_at is None or restore_ownership),
            "playtime_minutes": playtime_minutes if owned else None,
            "identifier": identifier,
            "acquisition": acquisition_fields or None,
            "delisted": delisted,
            # Previewed post-write ownership: a restore ("none") reports null,
            # a retirement reports the stamp the wet run would write.
            "unowned_at": None if restore_ownership else unowned_stamp,
            # dry_run never pushes, regardless of push_to_store.
            "store_push": None,
            # Resolved value (defaults to "manual"); null when owned=True.
            "wishlist_source": resolved_wishlist_source if not owned else None,
        }

    # An id target resolved above; a name target adopts an exact-name row or
    # mints one. name is non-None on that branch (the XOR check guarantees it).
    if game_id is not None:
        game_id = existing["id"]
    else:
        assert name is not None
        game_id = await upsert_game(None, name)
    added_identifier = None
    acquisition = None
    resulting_unowned_at: str | None = None
    if owned:
        game_platform_id = await upsert_game_platform(
            game_id,
            platform,
            playtime_minutes=playtime_minutes,
            owned=1,
        )
        if acquisition_fields:
            acquisition = await set_platform_acquisition(
                game_platform_id, acquisition_fields
            )
        if delisted is not None:
            # Pinned, not just written: the Steam sync clears this flag when an
            # app reappears in GetOwnedGames and the license audit sets it when
            # it doesn't — a hand correction has to outrank both.
            await apply_manual_platform_fields(
                game_platform_id, {"delisted": int(bool(delisted))}
            )
        if unowned_at is not None:
            # Runs AFTER the upsert above (which re-asserts owned=1 on an
            # unpinned row): retire last so the row ends the call unowned.
            ownership = await set_platform_ownership(
                game_platform_id,
                owned=restore_ownership,
                unowned_at=unowned_stamp,
            )
            resulting_unowned_at = ownership["unowned_at"]
        wishlist_id = None
        if identifier_type and identifier_value:
            await upsert_game_platform_identifier(
                game_platform_id,
                identifier_type,
                identifier_value,
                is_primary=True,
            )
            added_identifier = {"type": identifier_type, "value": identifier_value}
    else:
        game_platform_id = None
        store_identifier = identifier_value if identifier_type == "steam_appid" else None
        # Resolved BEFORE the wishlist upsert (push_to_store, steam only) so the
        # appid can be handed to store_identifier below — the upsert only fills
        # that column (COALESCE), so an already-provided identifier_value wins.
        push_appid: int | None = None
        if push_to_store and platform == "steam":
            if store_identifier is not None:
                try:
                    push_appid = int(store_identifier)
                except (TypeError, ValueError):
                    push_appid = None
            else:
                push_appid = await get_steam_appid_for_game(game_id)
                if push_appid is None:
                    # Wishlist-only games deliberately have no game_platforms
                    # row (so no game_platform_identifiers either), but an
                    # earlier local add may have stored the appid on the
                    # existing wishlist entry — reuse it.
                    async with get_db() as db:
                        wl_row = await db.execute_fetchone(
                            """SELECT store_identifier FROM game_wishlist
                               WHERE game_id = ? AND platform = 'steam'""",
                            (game_id,),
                        )
                    if wl_row is not None and wl_row["store_identifier"]:
                        try:
                            push_appid = int(wl_row["store_identifier"])
                        except (TypeError, ValueError):
                            push_appid = None
            if push_appid is not None and store_identifier is None:
                store_identifier = str(push_appid)
        wishlist_id = (
            await upsert_wishlist_entry(
                game_id,
                platform,
                source=resolved_wishlist_source,
                store_identifier=store_identifier,
            )
        )["id"]
        if store_identifier:
            added_identifier = {"type": "steam_appid", "value": store_identifier}

    # Either branch may have just made a prior wishlist entry moot (owned=True
    # fulfills it directly; owned=False on an already-owned game reconciles it
    # right away instead of leaving a stale row for the next sync to notice).
    await clear_fulfilled_wishlist_entries(game_id=game_id, platform=platform)

    # Runs AFTER the local write, which always survives a push failure: a
    # push is entirely best-effort on top of the wishlist row this call
    # already recorded. dry_run never reaches here (it returns above).
    store_push: dict | None = None
    if push_to_store:
        if platform == "steam":
            if push_appid is None:
                store_push = {
                    "attempted": False,
                    "pushed": False,
                    "error": (
                        "No Steam appid known for this game — pass "
                        "identifier_type='steam_appid', identifier_value=<appid>"
                    ),
                }
            else:
                try:
                    push_result = await push_to_steam_wishlist(push_appid)
                except SteamWishlistPushError as exc:
                    store_push = {
                        "attempted": True,
                        "pushed": False,
                        "appid": str(push_appid),
                        "error": str(exc),
                    }
                except Exception:
                    logger.exception(
                        "Unexpected error pushing appid %s to the Steam wishlist",
                        push_appid,
                    )
                    store_push = {
                        "attempted": True,
                        "pushed": False,
                        "appid": str(push_appid),
                        "error": (
                            "Unexpected error pushing to the Steam wishlist — "
                            "see server logs"
                        ),
                    }
                else:
                    store_push = {
                        "attempted": True,
                        "pushed": True,
                        "via": push_result.get("via"),
                        "appid": str(push_appid),
                        "wishlist_count": push_result.get("wishlist_count"),
                    }
        elif platform == "switch2":
            # name is always resolved to a str by this point (from game_id, or
            # the validated/stripped input name) — see the XOR check above.
            assert name is not None
            store_push = {
                "attempted": False,
                "pushed": False,
                "manual_url": (
                    "https://www.dekudeals.com/search?q="
                    + urllib.parse.quote(name)
                ),
                "note": (
                    "DekuDeals has no wishlist write API — add it there "
                    "manually; the next wishlist sync picks it up from the "
                    "shared-wishlist export."
                ),
            }
        else:
            store_push = {
                "attempted": False,
                "pushed": False,
                "error": f"No store wishlist push available for {platform}",
            }

    return {
        "created": created,
        "game_id": game_id,
        "game_platform_id": game_platform_id,
        "wishlist_id": wishlist_id,
        "name": name,
        # The row's resulting ownership, not the parameter: an unowned_at
        # retirement ends the call with owned=0 even though owned=True was
        # required to get here.
        "owned": owned and resulting_unowned_at is None,
        "platform": platform,
        "playtime_minutes": playtime_minutes if owned else None,
        "identifier": added_identifier,
        "acquisition": acquisition,
        "delisted": delisted,
        "unowned_at": resulting_unowned_at,
        "store_push": store_push,
        # Resolved value (defaults to "manual"); null when owned=True.
        "wishlist_source": resolved_wishlist_source if not owned else None,
    }


async def _resolve_game_row(name: str | None, game_id: int | None) -> dict:
    """Resolve a single game by id or name (tiered match + fuzzy fallback).

    Selects content_type/parent_game_id/is_primary_library_item alongside
    id/name so update_game's parent-linking logic can inspect the row's
    current classification without a second round-trip; other callers
    (set_acquisition) simply ignore the extra columns.
    """
    async with get_db() as db:
        if game_id is not None:
            row = await db.execute_fetchone(
                """SELECT id, name, content_type, parent_game_id,
                          is_primary_library_item
                   FROM games WHERE id = ?""",
                (game_id,),
            )
        elif name is not None:
            match = build_name_match(name, column=NORMALIZED_NAME_SQL, use_fts=fts_ready())
            row = await db.execute_fetchone(
                f"""SELECT g.id, g.name, g.content_type, g.parent_game_id,
                           g.is_primary_library_item, {match.rank_sql} AS match_rank
                    FROM games g
                    WHERE {match.where_sql}
                    ORDER BY match_rank ASC, length(g.name) ASC, g.id ASC
                    LIMIT 1""",
                (*match.rank_params, *match.where_params),
            )
        else:
            raise ToolError("Provide game_id or name")

    if row is None and name is not None:
        fuzzy_ids = await fuzzy_fallback_game_ids(name)
        if fuzzy_ids:
            async with get_db() as db:
                row = await db.execute_fetchone(
                    """SELECT id, name, content_type, parent_game_id,
                              is_primary_library_item
                       FROM games WHERE id = ?""",
                    (fuzzy_ids[0],),
                )

    if row is None:
        raise ToolError("Game not found in library")
    return row


async def update_game(
    name: str | None = None,
    game_id: int | None = None,
    new_name: str | None = None,
    sort_name: str | None = None,
    release_date: str | None = None,
    genres: list[str] | None = None,
    tags: list[str] | None = None,
    features: list[str] | None = None,
    short_description: str | None = None,
    hltb_main: float | None = None,
    hltb_extra: float | None = None,
    hltb_complete: float | None = None,
    is_farmed: bool | None = None,
    completion_status: str | None = None,
    content_type: str | None = None,
    parent_game_id: int | None = None,
    parent_name: str | None = None,
    cover_image_id: str | None = None,
    igdb_id: int | None = None,
    igdb_platforms: list[int] | None = None,
    clear_overrides: list[str] | None = None,
    *,
    dry_run: bool = False,
    recompute_affinity: bool = True,
) -> dict:
    """
    Manually edit one game's properties, with revocable sync protection.

    dry_run (internal, batch-only) runs the identical validation/guard path and
    skips every write; the returned manual_overrides simulate the post-write
    set, while enrichment_invalidated is not simulated (always empty).
    recompute_affinity=False (internal, batch-only) defers the tag-affinity
    recompute a tags edit normally triggers, so a batch recomputes once.

    Resolve the game with game_id or name, then set any subset of fields. Each
    edited field is recorded as a manual override so later library syncs and
    background enrichment will not clobber it. To hand a field back to automatic
    sync, pass its column name(s) in clear_overrides (e.g.
    clear_overrides=["is_farmed"] to let auto-detection manage it again); this
    only removes protection and does not change the stored value. Editing tags
    recomputes the taste profile. Renaming a game (new_name) additionally clears
    its name-matched enrichment caches (IGDB series/metadata, HowLongToBeat,
    OpenCritic/Metacritic) so background workers re-fetch under the correct title;
    any field you also pinned in the same edit stays protected. completion_status
    accepts playing, completed, abandoned, or evergreen (endless games with no
    completion concept, e.g. Rocket League, Tabletop Simulator, MMOs, sandboxes),
    or "none" to reset to automatic playtime-based inference. content_type
    corrects a wrong DLC/bundle/edition classification (e.g. a "X + Y"
    compilation misfiled as a bundle); it re-derives is_primary_library_item
    (which controls whether the game appears in stats/series/discover) and, when
    promoting to a primary type, detaches any wrong parent.

    parent_game_id/parent_name link this row under a base game (the repair
    workflow: detect_misclassified_dlc suggests the args, update_game applies
    them) — provide at most one, not both. The target must resolve to an
    existing PRIMARY library item (never another nested row — no parent
    chains) and can't be the game itself. Linking a parent only succeeds when
    the row will END UP nested: either content_type is also set to a nested
    value (dlc/expansion/bundle/edition/unknown_addon) in this same call, or
    the row is already nested; otherwise pass a nested content_type alongside
    it. Pass parent_game_id=0 to detach (null) the parent without touching
    content_type — 0 is never a real game id, so it's used the same way
    completion_status="none" resets that field. Setting a parent while also
    promoting content_type to a primary type in the same call is a
    contradiction and raises an error (a primary item can't have a parent).

    cover_image_id, igdb_id, and igdb_platforms correct a wrong IGDB match or
    cover art. cover_image_id is the IGDB cover slug (e.g. "co1wyy"; images render
    from it, falling back to the Steam capsule for Steam games). igdb_id repins
    the IGDB link (a positive integer, unique across the library — used by
    discover_series_gaps, which matches on igdb_id only, so a wrong id silently
    hides series gaps). igdb_platforms is the list of IGDB platform ids (ints,
    e.g. [6, 130]) feeding cross-platform availability. All three become manual
    overrides, so IGDB enrichment stops overwriting them; clear them via
    clear_overrides to let enrichment manage them again.

    Returns the updated fields, any cleared columns, the full manual-override
    list, and the providers whose enrichment was invalidated.
    """
    row = await _resolve_game_row(name, game_id)
    resolved_id = row["id"]

    clear = list(dict.fromkeys(clear_overrides or []))
    invalid = [c for c in clear if c not in GAME_EDITABLE_FIELDS]
    if invalid:
        raise ToolError(
            f"clear_overrides has unknown column(s): {invalid}. "
            f"Valid: {sorted(GAME_EDITABLE_FIELDS)}"
        )

    # Map the public params to games columns, JSON-encoding list fields and
    # coercing the is_farmed flag. Only explicitly-provided fields are written.
    fields: dict = {}
    if new_name is not None:
        clean = new_name.strip()
        if not clean:
            raise ToolError("new_name must not be empty")
        fields["name"] = clean
    if sort_name is not None:
        fields["sort_name"] = sort_name
    if release_date is not None:
        fields["release_date"] = release_date
    if genres is not None:
        fields["genres"] = json.dumps(genres)
    if tags is not None:
        # Canonicalize manual tags too, so a hand-set synonym variant matches the
        # shared vocabulary used by affinity/discover/library filters.
        fields["tags"] = json.dumps([canonical_tag(t) for t in tags])
    if features is not None:
        fields["features"] = json.dumps(features)
    if short_description is not None:
        fields["short_description"] = short_description
    if cover_image_id is not None:
        clean_cover = cover_image_id.strip()
        if not clean_cover:
            raise ToolError("cover_image_id must not be empty")
        fields["cover_image_id"] = clean_cover
    if igdb_id is not None:
        if igdb_id <= 0:
            raise ToolError("igdb_id must be a positive integer")
        async with get_db() as db:
            clash = await db.execute_fetchone(
                "SELECT id FROM games WHERE igdb_id = ? AND id != ?",
                (igdb_id, resolved_id),
            )
        if clash is not None:
            raise ToolError(
                f"igdb_id {igdb_id} is already used by game id {clash['id']}"
            )
        fields["igdb_id"] = int(igdb_id)
    if igdb_platforms is not None:
        if not all(isinstance(p, int) and not isinstance(p, bool) for p in igdb_platforms):
            raise ToolError("igdb_platforms must be a list of integers")
        # Store as a sorted, de-duplicated int list to match the IGDB writer.
        fields["igdb_platforms"] = json.dumps(sorted(set(igdb_platforms)))
    for label, value in (
        ("hltb_main", hltb_main),
        ("hltb_extra", hltb_extra),
        ("hltb_complete", hltb_complete),
    ):
        if value is not None:
            if value < 0:
                raise ToolError(f"{label} must not be negative")
            fields[label] = float(value)
    if is_farmed is not None:
        fields["is_farmed"] = int(bool(is_farmed))
    if completion_status is not None:
        normalized_status = completion_status.strip().lower()
        if normalized_status == "none":
            fields["completion_status"] = None
        elif normalized_status in COMPLETION_STATUSES:
            fields["completion_status"] = normalized_status
        else:
            raise ToolError(
                f"Unknown completion_status '{completion_status}'. "
                f"Valid: {sorted(COMPLETION_STATUSES)} or 'none' to reset"
            )
    if content_type is not None:
        normalized_ct = content_type.strip().lower()
        if normalized_ct not in CONTENT_TYPES:
            raise ToolError(
                f"Unknown content_type '{content_type}'. Valid: {sorted(CONTENT_TYPES)}"
            )
        fields["content_type"] = normalized_ct
        # is_primary_library_item is derived from the content type, never set
        # by hand — recompute it (and record it as an override) so the row's
        # visibility in rollups matches the corrected classification.
        is_primary = derive_is_primary(normalized_ct)
        fields["is_primary_library_item"] = int(is_primary)
        # A primary library item must not keep a parent: it is excluded from
        # search/rollups by the is_primary filter yet unreachable as any other
        # row's edition, so a leftover parent from a wrong nested classification
        # would orphan it. Clear (and protect) it when promoting to primary.
        if is_primary:
            fields["parent_game_id"] = None
        else:
            # The inverse of the "parent must be primary" rule enforced below:
            # nesting a row that other rows already hang off would hide it from
            # the rollups AND strand its children under an unreachable parent.
            # Manual is the highest-precedence writer, so this is the one place
            # that refuses out loud instead of silently skipping the write.
            async with get_db() as db:
                if await has_nested_children(db, resolved_id):
                    raise ToolError(
                        f"'{row['name']}' is the parent of nested content, so it "
                        f"cannot become '{normalized_ct}' — the children would be "
                        "stranded under a row that is itself hidden from the "
                        "library. Re-parent them (update_game) or fold them in "
                        "(merge_games) first."
                    )

    if parent_game_id is not None and parent_name is not None:
        raise ToolError("Provide parent_game_id or parent_name, not both")

    if parent_game_id == 0:
        # 0 is never a real game id (AUTOINCREMENT starts at 1) — used here as
        # a detach sentinel, mirroring completion_status="none" above: a value
        # that means "clear it" rather than a real id. Works regardless of
        # content_type, and does not conflict with a primary promotion's own
        # parent-clearing above (both just null the same column).
        fields["parent_game_id"] = None
    elif parent_game_id is not None or parent_name is not None:
        if fields.get("is_primary_library_item") == 1:
            raise ToolError(
                "Cannot set a parent while also promoting content_type to a "
                "primary type in the same call — a primary library item "
                "cannot have a parent"
            )
        if parent_game_id is not None:
            async with get_db() as db:
                parent_row = await db.execute_fetchone(
                    "SELECT id, name, is_primary_library_item FROM games WHERE id = ?",
                    (parent_game_id,),
                )
            if parent_row is None:
                raise ToolError(f"No game with id {parent_game_id}")
        else:
            assert parent_name is not None  # guaranteed by the elif condition above
            cleaned_parent_name = parent_name.strip()
            if not cleaned_parent_name:
                raise ToolError("parent_name must not be empty")
            resolved_parent_id = await resolve_parent_game(cleaned_parent_name, create=False)
            if resolved_parent_id is None:
                raise ToolError(f"No game named '{parent_name}' found in library")
            async with get_db() as db:
                parent_row = await db.execute_fetchone(
                    "SELECT id, name, is_primary_library_item FROM games WHERE id = ?",
                    (resolved_parent_id,),
                )

        if parent_row["id"] == resolved_id:
            raise ToolError("A game cannot be its own parent")
        if not parent_row["is_primary_library_item"]:
            raise ToolError(
                f"'{parent_row['name']}' is nested content itself and cannot "
                "be a parent — nesting under nested content is not supported"
            )
        # The row must END UP nested: either content_type is also being set to
        # a nested value in this same call, or it's already nested.
        final_content_type = fields.get("content_type", row["content_type"])
        if final_content_type not in NESTED_CONTENT_TYPES:
            raise ToolError(
                f"'{row['name']}' is not nested content (content_type="
                f"'{final_content_type}'); pass a nested content_type in this "
                f"call ({sorted(NESTED_CONTENT_TYPES)}) to set a parent"
            )
        # Substance guard (same invariant the sync classifiers enforce): a row
        # holding a store identifier and real playtime is a real, played
        # library item — nesting it under a parent with neither hides it
        # behind an empty shell (the Titanfall 2 shape). Raised, not skipped:
        # this is the manual path, so the human should see why and pick the
        # right repair instead.
        async with get_db() as db:
            substance_conflict = await nesting_substance_conflict(
                db, resolved_id, parent_row["id"]
            )
        if substance_conflict:
            raise ToolError(
                f"Refusing to nest '{row['name']}' (store identifier + recorded "
                f"playtime) under '{parent_row['name']}', which has neither — "
                "this would hide the real game behind an empty row. If the "
                "parent is a duplicate of the same game, consolidate with "
                "merge_games instead; if the nesting is genuinely intended, "
                "give the parent an ownership row first (add_game_to_platform)."
            )
        # Edition-ownership guard (same invariant the classifiers enforce,
        # ownership-keyed): an owned edition IS the game's ownership record,
        # so demoting it under an unowned parent hides an owned game. Raised
        # here so the human sees why and picks merge_games instead.
        if final_content_type == "edition":
            async with get_db() as db:
                edition_conflict = await edition_hides_owned_game(
                    db, resolved_id, parent_row["id"]
                )
            if edition_conflict:
                raise ToolError(
                    f"Refusing to nest owned '{row['name']}' as an edition of "
                    f"'{parent_row['name']}', which is owned nowhere — an owned "
                    "edition is the game's ownership record, and nesting it "
                    "would hide the game and leave the parent a false orphan. "
                    "Consolidate with merge_games "
                    f"(source_game_id={parent_row['id']}, "
                    f"target_game_id={resolved_id}) instead."
                )
        fields["parent_game_id"] = parent_row["id"]

    if not fields and not clear:
        raise ToolError("Provide at least one field to update or clear")

    conflict = set(fields) & set(clear)
    if conflict:
        raise ToolError(
            f"Cannot set and clear the same column(s) in one call: {sorted(conflict)}"
        )

    # Apply edits first (records their protection), then revoke any requested
    # protections. fields and clear are disjoint, so order only matters for the
    # returned override set, which clearing finalizes.
    overrides: set[str] = set()
    enrichment_invalidated: list[str] = []
    if dry_run:
        # Simulate the post-write override set the apply/remove pair below
        # would return: current ∪ (fields ∩ editable) − clear.
        async with get_db() as db:
            current = await get_manual_overrides(db, resolved_id)
        overrides = (current | (set(fields) & GAME_EDITABLE_FIELDS)) - set(clear)
    else:
        if fields:
            overrides = await apply_manual_game_fields(resolved_id, fields)
        if clear:
            overrides = await remove_manual_overrides(resolved_id, clear)

        # A rename invalidates name-matched enrichment (IGDB series/metadata, HLTB,
        # OpenCritic/Metacritic): the cached values describe the old title. Clear those
        # caches so background workers re-fetch under the new name. Field-level
        # manual_overrides still protect any user-pinned columns at write time.
        if "name" in fields and fields["name"] != row["name"]:
            enrichment_invalidated = await invalidate_name_derived_enrichment(
                resolved_id, overrides
            )

        # Repinning igdb_id corrects a wrong match: the stored igdb_cached_at (and any
        # series/cover/platform metadata from the old match) still describes the wrong
        # game, and claim_game_ids_for_igdb only revisits rows with igdb_cached_at
        # NULL — so the corrected id would never re-fetch. Invalidate the IGDB cache so
        # the backfill re-fetches under the pinned id. A rename already did this via
        # invalidate_name_derived_enrichment above, so skip the double work.
        if "igdb_id" in fields and "igdb" not in enrichment_invalidated:
            await invalidate_igdb_match_enrichment(resolved_id)
            enrichment_invalidated.append("igdb")

        # Tags feed the taste profile; recompute so recommendations reflect the
        # edit (a batch defers this and recomputes once at the end).
        if "tags" in fields and recompute_affinity:
            await recompute_tag_affinity()

    def _display(key: str, value):
        if key in {"genres", "tags", "features", "igdb_platforms"}:
            return json.loads(value)
        if key in {"is_farmed", "is_primary_library_item"}:
            return bool(value)
        return value

    updated = {key: _display(key, value) for key, value in fields.items()}
    updated_name = fields.get("name", row["name"])

    return {
        "game_id": resolved_id,
        "name": updated_name,
        "updated": updated,
        "cleared": clear,
        "manual_overrides": sorted(overrides),
        "enrichment_invalidated": enrichment_invalidated,
    }


def _validate_last_played(value: str) -> str:
    """Accept a full ISO calendar date YYYY-MM-DD (how game_platforms stores it)."""
    cleaned = value.strip()
    try:
        date.fromisoformat(cleaned)
    except ValueError:
        raise ToolError(f"last_played must be a real YYYY-MM-DD date (got '{value}')")
    return cleaned


async def set_playtime(
    name: str | None = None,
    game_id: int | None = None,
    platform: str | None = None,
    playtime_minutes: int | None = None,
    last_played: str | None = None,
    clear: list[str] | None = None,
    create_platform_row: bool = True,
    *,
    dry_run: bool = False,
) -> dict:
    """
    Manually set playtime for one game on one platform, protected from sync.

    dry_run (internal, batch-only) runs the identical validation/guard path and
    skips every write; a to-be-created platform row reports game_platform_id
    null, and manual_overrides/playtime values simulate the post-write state.

    Resolve the game with game_id or name, then pin playtime_minutes (total
    minutes played, not a delta) and/or last_played (YYYY-MM-DD) on that
    platform's ownership row. Each pinned column is recorded as a manual override
    on the game_platforms row, so later platform syncs (Steam, PSN, Xbox, Epic,
    Nintendo) will NOT overwrite it — unlike add_game_to_platform, whose value
    the next sync clobbers. clear lists column name(s) (playtime_minutes,
    last_played, delisted, owned) to hand back to automatic sync: it removes the
    override so the next sync repopulates the column, and does not change the
    stored value (the same semantics as update_game's clear_overrides).
    "delisted" and "owned" are pinned by add_game_to_platform(delisted=...) and
    add_game_to_platform(unowned_at=...) rather than here, but this is their
    un-pin path — note that clearing "owned" leaves the row unowned until some
    sync re-owns it, whereas add_game_to_platform(unowned_at="none") restores
    ownership immediately. A missing game_platforms row is created
    (owned=1) unless create_platform_row=False.

    Note: a pinned playtime feeds get_play_history like any other — the next
    refresh records a snapshot dated that day, so history windows reflect the
    manual value from then on.

    Returns the resolved game, the pinned/cleared columns, the row's resulting
    playtime_minutes/last_played, and the full manual-override list.
    """
    if platform is None:
        raise ToolError("platform is required")
    platform = _validate_platform(platform, LIBRARY_PLATFORMS)

    clear_list = list(dict.fromkeys(clear or []))
    invalid = [c for c in clear_list if c not in PLATFORM_EDITABLE_FIELDS]
    if invalid:
        raise ToolError(
            f"clear has unknown column(s): {invalid}. "
            f"Valid: {sorted(PLATFORM_EDITABLE_FIELDS)}"
        )

    fields: dict = {}
    if playtime_minutes is not None:
        if playtime_minutes < 0:
            raise ToolError("playtime_minutes must not be negative")
        fields["playtime_minutes"] = int(playtime_minutes)
    if last_played is not None:
        fields["last_played"] = _validate_last_played(last_played)

    if not fields and not clear_list:
        raise ToolError("Provide playtime_minutes/last_played to set, or clear")
    conflict = set(fields) & set(clear_list)
    if conflict:
        raise ToolError(
            f"Cannot set and clear the same column(s) in one call: {sorted(conflict)}"
        )

    row = await _resolve_game_row(name, game_id)
    resolved_id = row["id"]

    async with get_db() as db:
        gp = await db.execute_fetchone(
            """SELECT id, playtime_minutes, last_played
               FROM game_platforms WHERE game_id = ? AND platform = ?""",
            (resolved_id, platform),
        )

    platform_row_created = False
    gpid: int | None
    if gp is None:
        if not fields:
            # Nothing to pin and no row to unprotect — a clear-only call on a
            # platform the game isn't on is a no-op the caller should know about.
            raise ToolError(
                f"'{row['name']}' has no {platform} platform row to clear"
            )
        if not create_platform_row:
            raise ToolError(
                f"'{row['name']}' has no {platform} platform row. Pass "
                "create_platform_row=True or add it first with add_game_to_platform."
            )
        if dry_run:
            # The wet run would create the row; report game_platform_id null
            # (matching set_acquisitions_batch's created-game convention).
            gpid = None
        else:
            gpid = await upsert_game_platform(resolved_id, platform, owned=1)
        platform_row_created = True
    else:
        gpid = gp["id"]

    if dry_run:
        # Simulate the post-write state: current ∪ fields − clear for the
        # override set, pinned values overlaid on the row's current ones.
        current: set[str] = set()
        if gp is not None:
            async with get_db() as db:
                current = await get_platform_manual_overrides(db, gp["id"])
        overrides = (current | set(fields)) - set(clear_list)
        final_playtime = fields.get(
            "playtime_minutes", gp["playtime_minutes"] if gp else None
        )
        final_last_played = fields.get(
            "last_played", gp["last_played"] if gp else None
        )
    else:
        assert gpid is not None
        # Apply pins first (records their protection), then revoke any requested
        # protections. fields and clear_list are disjoint, so order only affects
        # the returned override set, which the clear finalizes.
        overrides = set()
        if fields:
            overrides = await apply_manual_platform_fields(gpid, fields)
        if clear_list:
            overrides = await remove_platform_manual_overrides(gpid, clear_list)

        async with get_db() as db:
            final = await db.execute_fetchone(
                "SELECT playtime_minutes, last_played FROM game_platforms WHERE id = ?",
                (gpid,),
            )
        final_playtime = final["playtime_minutes"]
        final_last_played = final["last_played"]

    return {
        "game_id": resolved_id,
        "name": row["name"],
        "platform": platform,
        "game_platform_id": gpid,
        "platform_row_created": platform_row_created,
        "updated": dict(fields),
        "cleared": clear_list,
        "playtime_minutes": final_playtime,
        "last_played": final_last_played,
        "manual_overrides": sorted(overrides),
    }


_NINTENDO_APPLICATION_ID_RE = re.compile(r"^[0-9A-Fa-f]{16}$")

_SWITCH2 = "switch2"


async def set_switch2_playtime_baseline(
    name: str | None = None,
    game_id: int | None = None,
    total_hours: float | None = None,
    application_id: str | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Record missing pre-tracking switch2 playtime without blocking future sync.

    Parental Controls tracking is forward-only, so play from before it began is
    invisible. Pinning via set_playtime would freeze the total (the sync
    recomputes it as SUM of daily summary rows and a pin blocks that write), so
    this tool instead does the delta math: total_hours is the game's CURRENT
    total playtime as Nintendo shows it; the synced daily minutes are
    subtracted and the remainder is stored as a synthetic baseline day
    (device 'manual-baseline', dated 1970-01-01) that future syncs keep adding
    real days on top of. Re-running with a fresh total replaces the baseline —
    never double-counts. An entered total equal to the synced minutes removes
    any existing baseline (total_hours=0 undoes a mistaken baseline on a
    never-synced game). application_id (16-hex Nintendo title id) is only
    needed when the game has no nintendo_title_id identifier yet, i.e. it was
    never seen by a Parental Controls sync; it is then recorded on the switch2
    platform row so history/sync bridging works.

    dry_run=True runs the identical validation and math without writing
    (identifier_recorded/baseline values simulate the post-write state).
    """
    if total_hours is None:
        raise ToolError("total_hours is required (the game's current total, not a delta)")
    # Zero is allowed: it is the correct current total for a never-synced game
    # whose only playtime is an erroneous baseline, and entering it removes
    # that baseline (total == synced ⇒ nothing missing).
    if not math.isfinite(total_hours) or total_hours < 0:
        raise ToolError("total_hours must not be negative")
    target_minutes = round(total_hours * 60)

    if application_id is not None:
        application_id = normalize_identifier_value(NINTENDO_TITLE_ID, application_id.strip())
        if not _NINTENDO_APPLICATION_ID_RE.match(application_id):
            raise ToolError(
                "application_id must be a 16-character hex Nintendo title id "
                "(e.g. 010067300059A000)"
            )

    row = await _resolve_game_row(name, game_id)
    resolved_id = row["id"]

    async with get_db() as db:
        gp = await db.execute_fetchone(
            """SELECT id, owned FROM game_platforms
               WHERE game_id = ? AND platform = ?""",
            (resolved_id, _SWITCH2),
        )
        if gp is None:
            raise ToolError(
                f"'{row['name']}' has no {_SWITCH2} platform row. Add it first with "
                "add_game_to_platform (or run sync(targets=[\"library\"]) if it should be synced)."
            )
        overrides = await get_platform_manual_overrides(db, gp["id"])
        if "playtime_minutes" in overrides:
            raise ToolError(
                f"'{row['name']}' has playtime_minutes pinned on {_SWITCH2} — the pin "
                "blocks the sync writes this baseline relies on. Clear it first with "
                "set_playtime(clear=['playtime_minutes']), then retry."
            )
        identifier_row = await db.execute_fetchone(
            """SELECT identifier_value FROM game_platform_identifiers
               WHERE game_platform_id = ? AND identifier_type = ?
               ORDER BY is_primary DESC, id ASC LIMIT 1""",
            (gp["id"], NINTENDO_TITLE_ID),
        )

    identifier_recorded = False
    if identifier_row is not None:
        # Already normalized uppercase in storage (write chokepoint +
        # migration — see normalize_identifier_value), so this is a plain
        # equality check, not a case-insensitive one.
        stored = str(identifier_row["identifier_value"])
        if application_id is not None and application_id != stored:
            raise ToolError(
                f"'{row['name']}' already has nintendo_title_id {stored}, which does "
                f"not match the given application_id {application_id}"
            )
        application_id = stored
    else:
        if application_id is None:
            raise ToolError(
                f"'{row['name']}' has no nintendo_title_id identifier — it is recorded "
                "automatically once a Parental Controls sync has seen the game. For a "
                "game never played since tracking began, pass application_id (the "
                "16-character hex title id, visible in the game's eShop page URL)."
            )
        other = await get_game_by_identifier(NINTENDO_TITLE_ID, application_id)
        if other is not None and other["id"] != resolved_id:
            raise ToolError(
                f"application_id {application_id} is already recorded on "
                f"'{other['name']}' (game_id {other['id']})"
            )
        identifier_recorded = True

    # application_id is normalized (uppercase) by this point on every path
    # above, and nintendo_play_summary.application_id is normalized the same
    # way at ingest (upsert_nintendo_play_summary), so it IS the daily-summary
    # key — no separate lookup needed to discover "the casing real rows use"
    # (that used to be get_nintendo_summary_key, now removed).
    summary_key = application_id
    synced_minutes = await get_nintendo_synced_minutes(application_id)
    previous_baseline = await get_nintendo_baseline_minutes(application_id)
    baseline_minutes = target_minutes - synced_minutes
    # All validation happens above this line: a failed call must leave no
    # trace, including the identifier a never-synced game would gain.
    if baseline_minutes < 0:
        raise ToolError(
            f"Synced {_SWITCH2} playtime for '{row['name']}' is already "
            f"{synced_minutes} minutes (~{synced_minutes / 60:.1f}h), more than the "
            f"entered total of {target_minutes} minutes ({total_hours}h). Enter the "
            "game's current total playtime as Nintendo shows it, not the missing part."
        )

    if identifier_recorded and not dry_run:
        await upsert_game_platform_identifier(gp["id"], NINTENDO_TITLE_ID, application_id)

    baseline_removed = False
    if baseline_minutes == 0:
        if previous_baseline is not None:
            if not dry_run:
                await delete_nintendo_playtime_baseline(application_id)
            baseline_removed = True
    elif not dry_run:
        if previous_baseline is not None:
            # Delete before re-inserting the corrected baseline row (belt: the
            # upsert's ON CONFLICT would update it in place regardless, since
            # application_id is normalized identically on both sides now).
            await delete_nintendo_playtime_baseline(application_id)
        await upsert_nintendo_play_summary([
            {
                "device_id": NINTENDO_BASELINE_DEVICE_ID,
                "application_id": summary_key,
                "period_type": "day",
                "period_key": NINTENDO_BASELINE_PERIOD_KEY,
                "playtime_minutes": baseline_minutes,
                "app_name": row["name"],
            }
        ])

    # Reflect the corrected total immediately instead of waiting for the next
    # Parental Controls sync (which recomputes the same SUM and agrees).
    if not dry_run:
        await upsert_game_platform(
            resolved_id,
            _SWITCH2,
            playtime_minutes=target_minutes,
            owned=gp["owned"],
        )

    return {
        "game_id": resolved_id,
        "name": row["name"],
        "platform": _SWITCH2,
        "application_id": application_id,
        "identifier_recorded": identifier_recorded,
        "total_hours": total_hours,
        "total_minutes": target_minutes,
        "synced_minutes": synced_minutes,
        "baseline_minutes": max(baseline_minutes, 0),
        "previous_baseline_minutes": previous_baseline,
        "baseline_removed": baseline_removed,
        "playtime_minutes": target_minutes,
        "dry_run": dry_run,
    }


_UPDATE_BATCH_ITEM_KEYS = frozenset({
    "name", "game_id", "new_name", "sort_name", "release_date", "genres",
    "tags", "features", "short_description", "hltb_main", "hltb_extra",
    "hltb_complete", "is_farmed", "completion_status", "content_type",
    "parent_game_id", "parent_name", "cover_image_id", "igdb_id",
    "igdb_platforms", "clear_overrides",
})


async def update_games_batch(items: list[dict], dry_run: bool = False) -> dict:
    """
    Apply update_game to many games; per-item errors never fail the whole call.

    Each item takes exactly update_game's parameters ({name or game_id} + any
    fields/clear_overrides). Every single-item guard (nesting, substance,
    igdb_id uniqueness, ...) runs identically per item; a guard refusal is that
    item's status="error", never an abort. The tag-affinity recompute a tags
    edit triggers is deferred and run ONCE after the loop (reported in
    tag_affinity_tags_updated; 0 when no tags changed or dry_run).

    dry_run=True runs the identical validation/guard path per item and writes
    nothing, returning the statuses a wet run would. Statuses are computed
    against the current database, so an item depending on an earlier item's
    write in the same batch (igdb_id uniqueness, nesting/parent state) may
    preview ok yet error in the wet run. Also not simulated:
    enrichment_invalidated (always empty) and the affinity recompute.
    """
    check_batch_items(items)

    async def _one(**kwargs):
        return await update_game(**kwargs, dry_run=dry_run, recompute_affinity=False)

    results: list[dict] = []
    tags_touched = False
    tag_count = 0
    try:
        for item in items:
            result = await apply_batch_item(item, _UPDATE_BATCH_ITEM_KEYS, _one)
            results.append(result)
            if result["status"] == "ok" and "tags" in result.get("updated", {}):
                tags_touched = True
    finally:
        # Even an unexpected escape mid-loop must not leave committed tag
        # edits without their deferred recompute.
        if tags_touched and not dry_run:
            tag_count = await recompute_tag_affinity()

    return {
        "results": results,
        "total": len(items),
        "ok": count_status(results, "ok"),
        "errors": count_status(results, "error"),
        "dry_run": dry_run,
        "tag_affinity_tags_updated": tag_count,
    }


_ADD_BATCH_ITEM_KEYS = frozenset({
    "name", "game_id", "platform", "identifier_type", "identifier_value",
    "playtime_minutes", "owned", "acquired_at", "price_paid",
    "price_currency", "purchase_source", "bundle_name", "delisted",
    "unowned_at", "push_to_store", "wishlist_source",
})


async def add_games_to_platform_batch(items: list[dict], dry_run: bool = False) -> dict:
    """
    Apply add_game_to_platform to many games; per-item errors never fail the call.

    Each item takes exactly add_game_to_platform's parameters (platform plus
    exactly one of name/game_id required, per-item owned/identifier/acquisition/
    delisted optional). created counts ok items that minted a brand-new games
    row (vs attaching to an existing one) — always 0 for game_id items, which
    can only edit. dry_run=True runs the identical validation path and writes nothing;
    a to-be-created game reports game_id null, and acquisition previews the
    validated fields rather than post-write row state. A repeated new name
    within one dry-run batch reports created=False (the wet run creates it
    once, then attaches); other statuses are computed against the current
    database, so cross-item interactions beyond that aren't simulated.
    """
    check_batch_items(items)

    # A wet run creates each new name once (later items attach to it by exact
    # name); mirror that in dry_run so the created counter matches.
    seen_new_names: set[str] = set()

    async def _one(**kwargs):
        result = await add_game_to_platform(**kwargs, dry_run=dry_run)
        if dry_run and result["created"]:
            key = result["name"].lower()
            if key in seen_new_names:
                result["created"] = False
            else:
                seen_new_names.add(key)
        return result

    results = [
        await apply_batch_item(item, _ADD_BATCH_ITEM_KEYS, _one) for item in items
    ]
    return {
        "results": results,
        "total": len(items),
        "ok": count_status(results, "ok"),
        "created": sum(1 for r in results if r["status"] == "ok" and r["created"]),
        "errors": count_status(results, "error"),
        "dry_run": dry_run,
    }


_PLAYTIME_BATCH_ITEM_KEYS = frozenset({
    "name", "game_id", "platform", "playtime_minutes", "last_played",
    "clear", "create_platform_row",
})


async def set_playtime_batch(items: list[dict], dry_run: bool = False) -> dict:
    """
    Apply set_playtime to many game+platform rows; per-item errors never fail
    the call.

    Each item takes exactly set_playtime's parameters ({name or game_id} +
    platform required; playtime_minutes/last_played/clear/create_platform_row
    optional, create_platform_row defaulting True like the single tool).
    dry_run=True runs the identical validation path and writes nothing; a
    to-be-created platform row reports game_platform_id null, and
    manual_overrides/playtime values simulate the post-write state. Preview
    statuses are computed against the CURRENT database, so an item depending
    on an earlier item's write (e.g. clearing a column on a platform row an
    earlier item would create) may preview as error where the wet run
    succeeds.
    """
    check_batch_items(items)

    async def _one(**kwargs):
        return await set_playtime(**kwargs, dry_run=dry_run)

    results = [
        await apply_batch_item(item, _PLAYTIME_BATCH_ITEM_KEYS, _one) for item in items
    ]
    return {
        "results": results,
        "total": len(items),
        "ok": count_status(results, "ok"),
        "errors": count_status(results, "error"),
        "dry_run": dry_run,
    }
