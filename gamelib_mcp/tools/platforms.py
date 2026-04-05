"""get_platform_breakdown, sync_platform, add_game_to_platform, and set_hardware_preference tools."""

import json
import os
from ..data.db import get_db, set_meta, upsert_game, upsert_game_platform, upsert_game_platform_identifier


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

        total = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")

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


async def sync_platform(platform: str) -> dict:
    """
    Sync a single platform on demand.
    platform: steam | epic | gog | nintendo | ps5
    """
    import importlib

    _PLATFORM_MAP = {
        "steam":    ("gamelib_mcp.data.steam_xml", "fetch_library",  None),
        "epic":     ("gamelib_mcp.data.epic",       "sync_epic",      "EPIC_LEGENDARY_PATH"),
        "gog":      ("gamelib_mcp.data.gog",        "sync_gog",       "GOG_REFRESH_TOKEN"),
        "nintendo": ("gamelib_mcp.data.nintendo",   "sync_nintendo",  "NINTENDO_SESSION_TOKEN"),
        "ps5":      ("gamelib_mcp.data.psn",        "sync_psn",       "PSN_NPSSO"),
    }

    if platform not in _PLATFORM_MAP:
        return {"error": f"Unknown platform '{platform}'. Valid: {list(_PLATFORM_MAP)}"}

    module_path, fn_name, env_key = _PLATFORM_MAP[platform]

    if env_key and not os.getenv(env_key):
        return {"error": f"{env_key} not set — cannot sync {platform}"}

    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, fn_name)
        return await fn()
    except Exception as exc:
        return {"error": str(exc)}


async def set_hardware_preference(platforms: list[str]) -> dict:
    """
    Set your hardware preference order for get_recommendations suggested_platform.

    platforms: ordered list, highest priority first.
    e.g. ["switch2", "steam_deck", "ps5"]

    Valid values: any platform name used in your library (steam, epic, gog, nintendo, ps5, etc.)
    """
    await set_meta("hardware_preference", json.dumps(platforms))
    return {"success": True, "hardware_preference": platforms}


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
    platform: steam | epic | gog | nintendo | ps5 | itchio | xbox | other
    identifier_type: Optional store identifier type (e.g. 'steam_appid', 'gog_product_id')
    identifier_value: Optional store identifier value
    playtime_minutes: Optional known playtime in minutes
    """
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
        "success": True,
        "game_id": game_id,
        "game_platform_id": game_platform_id,
        "name": name,
        "platform": platform,
        "playtime_minutes": playtime_minutes,
        "identifier": added_identifier,
    }
