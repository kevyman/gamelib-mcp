"""Fetch Steam library via IPlayerService/GetOwnedGames API."""

import logging
import os
from datetime import datetime, timezone

import httpx

from .db import (
    bulk_upsert_steam_library,
    set_meta,
)
from .title_normalization import prepare_catalog_title

logger = logging.getLogger(__name__)

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
STEAM_ID = os.getenv("STEAM_ID", "")
OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
STALE_HOURS = 6


async def fetch_library() -> dict:
    """Fetch owned games from Steam Web API and upsert into games table."""
    steam_api_key = os.getenv("STEAM_API_KEY", STEAM_API_KEY)
    steam_id = os.getenv("STEAM_ID", STEAM_ID)
    if not steam_api_key or not steam_id:
        raise ValueError("STEAM_API_KEY and STEAM_ID environment variables must be set")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            OWNED_GAMES_URL,
            params={
                "key": steam_api_key,
                "steamid": steam_id,
                "include_appinfo": 1,
                "include_played_free_games": 1,
                "skip_unvetted_apps": 0,
                "format": "json",
            },
        )
        resp.raise_for_status()

    data = resp.json().get("response", {})
    games = data.get("games", [])

    if not games and "game_count" not in data:
        raise ValueError(
            "Steam API returned empty response — check STEAM_ID is correct and "
            "game library visibility is set to Public in Steam privacy settings"
        )

    now = datetime.now(timezone.utc).isoformat()
    normalized_rows = []
    skipped_rows = 0
    for game in games:
        prepared_title = prepare_catalog_title(game.get("name", f"App {game['appid']}"))
        if prepared_title is None:
            skipped_rows += 1
            continue

        normalized_rows.append(
            {
                "appid": game["appid"],
                "name": prepared_title,
                "playtime_minutes": game.get("playtime_forever", 0),
                "playtime_2weeks_minutes": game.get("playtime_2weeks", 0),
                "rtime_last_played": game.get("rtime_last_played") or None,
            }
        )

    if skipped_rows:
        logger.info("Steam sync skipped %d non-game rows before DB upsert", skipped_rows)
    upserted = await bulk_upsert_steam_library(normalized_rows, synced_at=now)

    await set_meta("library_synced_at", now)
    return {"games_upserted": upserted, "synced_at": now}
