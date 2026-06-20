"""get_platform_breakdown, add_game_to_platform, update_game, and set_hardware_preference tools."""

import json
from fastmcp.exceptions import ToolError

from ..data.db import (
    apply_manual_game_fields,
    get_db,
    recompute_tag_affinity,
    set_meta,
    upsert_game,
    upsert_game_platform,
    upsert_game_platform_identifier,
)
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
) -> dict:
    """
    Manually add a game to a platform — useful for games that aren't fetched
    automatically (e.g. physical copies, unreported digital titles).

    name: Game name (will match an existing game by exact name or create a new one)
    platform: steam | epic | gog | nintendo | switch2 | ps5 | itchio | xbox | ea | ubisoft | other (aliases: origin→ea, uplay→ubisoft)
    identifier_type: Optional store identifier type (e.g. 'steam_appid', 'gog_product_id')
    identifier_value: Optional store identifier value
    playtime_minutes: Optional known playtime in minutes
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
    game_platform_id = await upsert_game_platform(
        game_id,
        platform,
        playtime_minutes=playtime_minutes,
        owned=1,
    )

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
        "playtime_minutes": playtime_minutes,
        "identifier": added_identifier,
    }


async def _resolve_game_row(name: str | None, game_id: int | None) -> dict:
    """Resolve a single game by id or name (tiered match + fuzzy fallback)."""
    async with get_db() as db:
        if game_id is not None:
            row = await db.execute_fetchone(
                "SELECT id, name, tags FROM games WHERE id = ?", (game_id,)
            )
        elif name is not None:
            match = build_name_match(name, column=NORMALIZED_NAME_SQL)
            row = await db.execute_fetchone(
                f"""SELECT g.id, g.name, g.tags, {match.rank_sql} AS match_rank
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
                    "SELECT id, name, tags FROM games WHERE id = ?", (fuzzy_ids[0],)
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
) -> dict:
    """
    Manually edit one game's properties and protect them from being overwritten.

    Resolve the game with game_id or name, then set any subset of fields. Each
    edited field is recorded as a manual override so later library syncs and
    background enrichment will not clobber it. Editing tags recomputes the taste
    profile. Returns the updated fields and the full manual-override list.
    """
    row = await _resolve_game_row(name, game_id)
    resolved_id = row["id"]

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
        fields["tags"] = json.dumps(tags)
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

    if not fields:
        raise ToolError("Provide at least one field to update")

    overrides = await apply_manual_game_fields(resolved_id, fields)

    # Tags feed the taste profile; recompute so recommendations reflect the edit.
    if "tags" in fields:
        await recompute_tag_affinity()

    updated = {key: json.loads(value) if key in {"genres", "tags", "features"} else value
               for key, value in fields.items()}
    updated_name = fields.get("name", row["name"])

    return {
        "game_id": resolved_id,
        "name": updated_name,
        "updated": updated,
        "manual_overrides": sorted(overrides),
    }
