"""get_platform_breakdown, sync_platform, add_game_to_platform, and set_hardware_preference tools."""

import importlib
import json
from ..data.db import get_db, set_meta, upsert_game, upsert_game_platform, upsert_game_platform_identifier
from .library import _resolve_platform

_PLATFORM_MAP = {
    "steam":    ("gamelib_mcp.data.steam_xml", "fetch_library"),
    "epic":     ("gamelib_mcp.data.epic",       "sync_epic"),
    "gog":      ("gamelib_mcp.data.gog",        "sync_gog"),
    "nintendo": ("gamelib_mcp.data.nintendo",   "sync_nintendo"),
    "switch":   ("gamelib_mcp.data.nintendo",   "sync_nintendo"),
    "switch2":  ("gamelib_mcp.data.nintendo",   "sync_nintendo"),
    "ps5":      ("gamelib_mcp.data.psn",        "sync_psn"),
}

_VALID_PLATFORMS = {"steam", "epic", "gog", "nintendo", "switch", "switch2", "ps5", "itchio", "xbox", "other"}


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


async def sync_platform(platform: str) -> dict:
    """
    Sync a single platform on demand.
    platform: steam | epic | gog | nintendo | switch | switch2 | ps5

    Credential handling is left to each sync module (default config paths,
    cookie fallback, etc.) — this tool does not gate on env vars.
    """
    if platform not in _PLATFORM_MAP:
        return {"error": f"Unknown platform '{platform}'. Valid: {sorted(set(_PLATFORM_MAP))}"}

    module_path, fn_name = _PLATFORM_MAP[platform]
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

    Valid values: any platform name used in your library (steam, epic, gog, switch2, ps5, etc.)
    """
    normalized = [_resolve_platform(p) or p for p in platforms]
    await set_meta("hardware_preference", json.dumps(normalized))
    return {"success": True, "hardware_preference": normalized}


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
    platform: steam | epic | gog | nintendo | switch2 | ps5 | itchio | xbox | other
    identifier_type: Optional store identifier type (e.g. 'steam_appid', 'gog_product_id')
    identifier_value: Optional store identifier value
    playtime_minutes: Optional known playtime in minutes
    """
    if platform not in _VALID_PLATFORMS:
        return {"error": f"Unknown platform '{platform}'. Valid: {sorted(_VALID_PLATFORMS)}"}

    # Normalize aliases (e.g. "nintendo" → "switch2") to match auto-synced data
    platform = _resolve_platform(platform) or platform

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
        "success": True,
        "created": created,
        "game_id": game_id,
        "game_platform_id": game_platform_id,
        "name": name,
        "platform": platform,
        "playtime_minutes": playtime_minutes,
        "identifier": added_identifier,
    }
