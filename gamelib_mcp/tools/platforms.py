"""get_platform_breakdown, add_game_to_platform, update_game, and set_hardware_preference tools."""

import json
from datetime import date

from fastmcp.exceptions import ToolError

from ..data.db import (
    GAME_EDITABLE_FIELDS,
    PLATFORM_EDITABLE_FIELDS,
    apply_manual_game_fields,
    apply_manual_platform_fields,
    clear_fulfilled_wishlist_entries,
    fts_ready,
    get_db,
    invalidate_igdb_match_enrichment,
    invalidate_name_derived_enrichment,
    nesting_substance_conflict,
    recompute_tag_affinity,
    remove_manual_overrides,
    remove_platform_manual_overrides,
    resolve_parent_game,
    set_meta,
    set_platform_acquisition,
    upsert_game,
    upsert_game_platform,
    upsert_game_platform_identifier,
    upsert_wishlist_entry,
)
from ..data.content import NESTED_CONTENT_TYPES, PRIMARY_CONTENT_TYPES, derive_is_primary
from ..data.tag_synonyms import canonical_tag
# Safe direction: acquisition.py never imports this module at top level (it
# lazy-imports _resolve_game_row inside functions), so importing its validator
# helpers here cannot form a cycle.
from .acquisition import _validated_fields as _validated_acquisition_fields
from .common import (
    LIBRARY_PLATFORMS,
    validate_platform as _validate_platform,
)
from .search import (
    NORMALIZED_NAME_SQL,
    build_name_match,
    fuzzy_fallback_game_ids,
)

COMPLETION_STATUSES = {"playing", "completed", "abandoned", "evergreen"}
CONTENT_TYPES = PRIMARY_CONTENT_TYPES | NESTED_CONTENT_TYPES


async def get_platform_breakdown() -> dict:
    """
    Return per-platform game counts, total unique games, and overlap list
    (games owned on 2+ platforms).

    Counts split games (primary library items) from addons (owned DLC/
    expansions/editions/bundles etc.) — an owned addon no longer inflates
    owned_games/total_unique_games/overlap_games; it's reported separately
    via owned_addons/total_unique_addons so DLC ownership stays visible
    without corrupting the "how many games do I own" numbers.
    """
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

        overlap_rows = await db.execute_fetchall(
            """SELECT g.name, g.id AS game_id,
                      COUNT(gp.platform) AS platform_count,
                      GROUP_CONCAT(gp.platform) AS platforms
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
               WHERE g.is_primary_library_item = 1
               GROUP BY g.id
               HAVING platform_count >= 2
               ORDER BY platform_count DESC"""
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
        "overlap_count": len(overlap_rows),
        "overlap_games": [
            {
                "game_id": r["game_id"],
                "name": r["name"],
                "owned_on": r["platforms"].split(","),
            }
            for r in overlap_rows
        ],
    }


async def get_wishlist(platform: str | None = None) -> dict:
    """
    List wishlist items — games marked wanted but not necessarily owned.

    platform: optional filter (e.g. "steam", "switch2"); omit for all platforms.
    Populated by sync_wishlist (Steam, DekuDeals→switch2) or by
    add_game_to_platform(owned=False) for manual entries (e.g. PSN). owned
    reflects live game_platforms state — normally False, since sync_wishlist
    and add_game_to_platform both clear an entry once it's actually owned;
    True here is a transient diagnostic (ownership was just established and
    the next cleanup pass hasn't run yet), not a common case.
    """
    resolved_platform = _validate_platform(platform, LIBRARY_PLATFORMS) if platform else None

    where = "WHERE 1=1"
    params: list = []
    if resolved_platform:
        where += " AND w.platform = ?"
        params.append(resolved_platform)

    async with get_db() as db:
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
                ORDER BY w.wishlisted_at DESC""",
            params,
        )

    return {
        "count": len(rows),
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
    name: str,
    platform: str,
    identifier_type: str | None = None,
    identifier_value: str | None = None,
    playtime_minutes: int | None = None,
    owned: bool = True,
    acquired_at: str | None = None,
    price_paid: float | None = None,
    price_currency: str | None = None,
    purchase_source: str | None = None,
    bundle_name: str | None = None,
) -> dict:
    """
    Manually add a game to a platform — useful for games that aren't fetched
    automatically (e.g. physical copies, unreported digital titles), or to
    record a wishlist item on a platform with no wishlist sync (e.g. PSN, which
    has no public wishlist API — pass owned=False there).

    name: Game name (will match an existing game by exact name or create a new one)
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
    acquired_at / price_paid / price_currency / purchase_source / bundle_name:
        optional acquisition details recorded on the new ownership row, with
        the same validation and vocabulary as set_acquisition. They require
        owned=True — a wishlist entry has no platform-ownership row to record
        them on.
    """
    # Resolve aliases (e.g. "nintendo" → "switch2") and validate in one step.
    platform = _validate_platform(platform, LIBRARY_PLATFORMS)

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

    # Check whether the game already exists before upserting
    async with get_db() as db:
        existing = await db.execute_fetchone(
            "SELECT id FROM games WHERE lower(name) = lower(?) ORDER BY id LIMIT 1",
            (name,),
        )
    created = existing is None

    game_id = await upsert_game(None, name)
    added_identifier = None
    acquisition = None
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
        wishlist_id = await upsert_wishlist_entry(
            game_id, platform, source="manual", store_identifier=store_identifier
        )
        if store_identifier:
            added_identifier = {"type": "steam_appid", "value": store_identifier}

    # Either branch may have just made a prior wishlist entry moot (owned=True
    # fulfills it directly; owned=False on an already-owned game reconciles it
    # right away instead of leaving a stale row for the next sync to notice).
    await clear_fulfilled_wishlist_entries(game_id=game_id, platform=platform)

    return {
        "created": created,
        "game_id": game_id,
        "game_platform_id": game_platform_id,
        "wishlist_id": wishlist_id,
        "name": name,
        "platform": platform,
        "owned": owned,
        "playtime_minutes": playtime_minutes if owned else None,
        "identifier": added_identifier,
        "acquisition": acquisition,
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
) -> dict:
    """
    Manually edit one game's properties, with revocable sync protection.

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
    if fields:
        overrides = await apply_manual_game_fields(resolved_id, fields)
    if clear:
        overrides = await remove_manual_overrides(resolved_id, clear)

    # A rename invalidates name-matched enrichment (IGDB series/metadata, HLTB,
    # OpenCritic/Metacritic): the cached values describe the old title. Clear those
    # caches so background workers re-fetch under the new name. Field-level
    # manual_overrides still protect any user-pinned columns at write time.
    enrichment_invalidated: list[str] = []
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

    # Tags feed the taste profile; recompute so recommendations reflect the edit.
    if "tags" in fields:
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
) -> dict:
    """
    Manually set playtime for one game on one platform, protected from sync.

    Resolve the game with game_id or name, then pin playtime_minutes (total
    minutes played, not a delta) and/or last_played (YYYY-MM-DD) on that
    platform's ownership row. Each pinned column is recorded as a manual override
    on the game_platforms row, so later platform syncs (Steam, PSN, Xbox, Epic,
    Nintendo) will NOT overwrite it — unlike add_game_to_platform, whose value
    the next sync clobbers. clear lists column name(s) (playtime_minutes,
    last_played) to hand back to automatic sync: it removes the override so the
    next sync repopulates the column, and does not change the stored value (the
    same semantics as update_game's clear_overrides). A missing game_platforms
    row is created (owned=1) unless create_platform_row=False.

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
            "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
            (resolved_id, platform),
        )

    platform_row_created = False
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
        gpid = await upsert_game_platform(resolved_id, platform, owned=1)
        platform_row_created = True
    else:
        gpid = gp["id"]

    # Apply pins first (records their protection), then revoke any requested
    # protections. fields and clear_list are disjoint, so order only affects the
    # returned override set, which the clear finalizes.
    overrides: set[str] = set()
    if fields:
        overrides = await apply_manual_platform_fields(gpid, fields)
    if clear_list:
        overrides = await remove_platform_manual_overrides(gpid, clear_list)

    async with get_db() as db:
        final = await db.execute_fetchone(
            "SELECT playtime_minutes, last_played FROM game_platforms WHERE id = ?",
            (gpid,),
        )

    return {
        "game_id": resolved_id,
        "name": row["name"],
        "platform": platform,
        "game_platform_id": gpid,
        "platform_row_created": platform_row_created,
        "updated": dict(fields),
        "cleared": clear_list,
        "playtime_minutes": final["playtime_minutes"],
        "last_played": final["last_played"],
        "manual_overrides": sorted(overrides),
    }
