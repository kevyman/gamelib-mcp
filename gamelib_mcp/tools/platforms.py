"""get_platform_breakdown, add_game_to_platform, update_game, and set_hardware_preference tools."""

import json
from fastmcp.exceptions import ToolError

from ..data.db import (
    GAME_EDITABLE_FIELDS,
    apply_manual_game_fields,
    get_db,
    invalidate_name_derived_enrichment,
    recompute_tag_affinity,
    remove_manual_overrides,
    set_meta,
    upsert_game,
    upsert_game_platform,
    upsert_game_platform_identifier,
    upsert_wishlist_entry,
)
from ..data.tag_synonyms import canonical_tag
from .common import (
    LIBRARY_PLATFORMS,
    validate_platform as _validate_platform,
)
from .search import (
    NORMALIZED_NAME_SQL,
    build_name_match,
    fuzzy_fallback_game_ids,
)


async def get_platform_breakdown() -> dict:
    """
    Return per-platform game counts, total unique games, and overlap list
    (games owned on 2+ platforms).
    """
    async with get_db() as db:
        platform_rows = await db.execute_fetchall(
            """SELECT platform, COUNT(DISTINCT game_id) AS count
               FROM game_platforms
               WHERE owned = 1
               GROUP BY platform
               ORDER BY count DESC"""
        )

        total = await db.execute_fetchone(
            "SELECT COUNT(DISTINCT game_id) AS c FROM game_platforms WHERE owned = 1"
        )

        overlap_rows = await db.execute_fetchall(
            """SELECT g.name, g.id AS game_id,
                      COUNT(gp.platform) AS platform_count,
                      GROUP_CONCAT(gp.platform) AS platforms
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
               GROUP BY g.id
               HAVING platform_count >= 2
               ORDER BY platform_count DESC"""
        )

    return {
        "by_platform": [
            {"platform": r["platform"], "owned_games": r["count"]}
            for r in platform_rows
        ],
        "total_unique_games": total["c"],
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
    add_game_to_platform(owned=False) for manual entries (e.g. PSN).
    """
    resolved_platform = _validate_platform(platform, LIBRARY_PLATFORMS) if platform else None

    where = "WHERE gp.wishlisted_at IS NOT NULL"
    params: list = []
    if resolved_platform:
        where += " AND gp.platform = ?"
        params.append(resolved_platform)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT g.id AS game_id, g.name, gp.platform, gp.wishlisted_at, gp.owned
                FROM game_platforms gp
                JOIN games g ON g.id = gp.game_id
                {where}
                ORDER BY gp.wishlisted_at DESC""",
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
                "owned": bool(r["owned"]),
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
) -> dict:
    """
    Manually add a game to a platform — useful for games that aren't fetched
    automatically (e.g. physical copies, unreported digital titles), or to
    record a wishlist item on a platform with no wishlist sync (e.g. PSN, which
    has no public wishlist API — pass owned=False there).

    name: Game name (will match an existing game by exact name or create a new one)
    platform: steam | epic | gog | nintendo | switch2 | ps5 | itchio | xbox | ea | ubisoft | other (aliases: origin→ea, uplay→ubisoft)
    identifier_type: Optional store identifier type (e.g. 'steam_appid', 'gog_product_id')
    identifier_value: Optional store identifier value
    playtime_minutes: Optional known playtime in minutes
    owned: True (default) records an owned copy; False records a wishlist entry
        instead (playtime_minutes is ignored in that case)
    """
    # Resolve aliases (e.g. "nintendo" → "switch2") and validate in one step.
    platform = _validate_platform(platform, LIBRARY_PLATFORMS)

    name = name.strip()
    if not name:
        raise ToolError("name must not be empty")
    if playtime_minutes is not None and playtime_minutes < 0:
        raise ToolError("playtime_minutes must not be negative")

    # Check whether the game already exists before upserting
    async with get_db() as db:
        existing = await db.execute_fetchone(
            "SELECT id FROM games WHERE lower(name) = lower(?) ORDER BY id LIMIT 1",
            (name,),
        )
    created = existing is None

    game_id = await upsert_game(None, name)
    if owned:
        game_platform_id = await upsert_game_platform(
            game_id,
            platform,
            playtime_minutes=playtime_minutes,
            owned=1,
        )
    else:
        game_platform_id = await upsert_wishlist_entry(game_id, platform)

    added_identifier = None
    if identifier_type and identifier_value:
        await upsert_game_platform_identifier(
            game_platform_id,
            identifier_type,
            identifier_value,
            is_primary=True,
        )
        added_identifier = {"type": identifier_type, "value": identifier_value}

    return {
        "created": created,
        "game_id": game_id,
        "game_platform_id": game_platform_id,
        "name": name,
        "platform": platform,
        "owned": owned,
        "playtime_minutes": playtime_minutes if owned else None,
        "identifier": added_identifier,
    }


async def _resolve_game_row(name: str | None, game_id: int | None) -> dict:
    """Resolve a single game by id or name (tiered match + fuzzy fallback)."""
    async with get_db() as db:
        if game_id is not None:
            row = await db.execute_fetchone(
                "SELECT id, name FROM games WHERE id = ?", (game_id,)
            )
        elif name is not None:
            match = build_name_match(name, column=NORMALIZED_NAME_SQL)
            row = await db.execute_fetchone(
                f"""SELECT g.id, g.name, {match.rank_sql} AS match_rank
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
                    "SELECT id, name FROM games WHERE id = ?", (fuzzy_ids[0],)
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
    any field you also pinned in the same edit stays protected. Returns the
    updated fields, any cleared columns, the full manual-override list, and the
    providers whose enrichment was invalidated.
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

    # Tags feed the taste profile; recompute so recommendations reflect the edit.
    if "tags" in fields:
        await recompute_tag_affinity()

    def _display(key: str, value):
        if key in {"genres", "tags", "features"}:
            return json.loads(value)
        if key == "is_farmed":
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
