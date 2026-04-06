"""Fetch Steam library via IPlayerService/GetOwnedGames API."""

import os
from datetime import datetime, timezone

import httpx

from .db import (
    bulk_upsert_steam_library,
    set_meta,
)

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
STEAM_ID = os.getenv("STEAM_ID", "")
OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
STALE_HOURS = 6


async def fetch_library() -> dict:
    """Fetch owned games from Steam Web API and upsert into games table."""
    if not STEAM_API_KEY or not STEAM_ID:
        raise ValueError("STEAM_API_KEY and STEAM_ID environment variables must be set")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            OWNED_GAMES_URL,
            params={
                "key": STEAM_API_KEY,
                "steamid": STEAM_ID,
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
    normalized_rows = [
        {
            "appid": game["appid"],
            "name": game.get("name", f"App {game['appid']}"),
            "playtime_minutes": game.get("playtime_forever", 0),
            "playtime_2weeks_minutes": game.get("playtime_2weeks", 0),
            "rtime_last_played": game.get("rtime_last_played") or None,
        }
        for game in games
    ]
    upserted = await bulk_upsert_steam_library(normalized_rows, synced_at=now)

    await set_meta("library_synced_at", now)
    return {"games_upserted": upserted, "synced_at": now}
