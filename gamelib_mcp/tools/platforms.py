"""get_platform_breakdown, add_game_to_platform, and set_hardware_preference tools."""

import json
from fastmcp.exceptions import ToolError

from ..data.db import get_db, set_meta, upsert_game, upsert_game_platform, upsert_game_platform_identifier
from .common import (
    LIBRARY_PLATFORMS,
    validate_platform as _validate_platform,
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

    Valid values: steam, epic, gog, switch2 (aka nintendo/switch), ps5, itchio, xbox, ea, other.
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
    platform: steam | epic | gog | nintendo | switch2 | ps5 | itchio | xbox | ea | other
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
