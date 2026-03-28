"""Fetch Steam library via IPlayerService/GetOwnedGames API, upsert into games table."""

import os
from datetime import datetime, timezone

import httpx

from .db import get_db, set_meta, upsert_game, upsert_game_platform

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
    upserted = 0

    async with get_db() as db:
        for game in games:
            appid = game["appid"]
            name = game.get("name", f"App {appid}")
            playtime_forever = game.get("playtime_forever", 0)   # already in minutes
            playtime_2weeks = game.get("playtime_2weeks", 0)      # already in minutes
            rtime = game.get("rtime_last_played") or None         # 0 means never → store as NULL

            await db.execute(
                """INSERT INTO games (appid, name, rtime_last_played, library_updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(appid) DO UPDATE SET
                       name = excluded.name,
                       rtime_last_played = excluded.rtime_last_played,
                       library_updated_at = excluded.library_updated_at""",
                (appid, name, rtime, now),
            )
            row = await db.execute_fetchone("SELECT id FROM games WHERE appid = ?", (appid,))
            game_id = row["id"]

            await db.execute(
                """INSERT INTO game_platforms (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
                   VALUES (?, 'steam', 1, ?, ?, ?)
                   ON CONFLICT(game_id, platform) DO UPDATE SET
                       owned = excluded.owned,
                       playtime_minutes = excluded.playtime_minutes,
                       playtime_2weeks_minutes = excluded.playtime_2weeks_minutes,
                       last_synced = excluded.last_synced""",
                (game_id, playtime_forever, playtime_2weeks, now),
            )
            upserted += 1

        await db.commit()

    await set_meta("library_synced_at", now)
    return {"games_upserted": upserted, "synced_at": now}
