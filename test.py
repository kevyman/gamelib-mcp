from datetime import datetime, timezone
import os
import sqlite3
import httpx
from dotenv import load_dotenv
from gamelib_mcp.data.db import _V1_SCHEMA_DDL

load_dotenv()

db_path = "/tmp/steam-migration-sample.db"
steam_api_key = os.environ["STEAM_API_KEY"]
steam_id = os.environ["STEAM_ID"]

resp = httpx.get(
    "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
    params={
        "key": steam_api_key,
        "steamid": steam_id,
        "include_appinfo": 1,
        "include_played_free_games": 1,
        "skip_unvetted_apps": 0,
        "format": "json",
    },
    timeout=30,
)
resp.raise_for_status()
games = resp.json()["response"]["games"][:15]

conn = sqlite3.connect(db_path)
conn.executescript(
    "DROP TABLE IF EXISTS game_platforms; DROP TABLE IF EXISTS ratings; DROP TABLE IF EXISTS tag_affinity; DROP TABLE IF EXISTS meta; DROP TABLE IF EXISTS games;"
)
conn.executescript(_V1_SCHEMA_DDL)
conn.execute("PRAGMA user_version = 1")

now = datetime.now(timezone.utc).isoformat()

for game in games:
    cur = conn.execute(
        """
        INSERT INTO games (appid, name, rtime_last_played, library_updated_at, is_farmed)
        VALUES (?, ?, ?, ?, 0)
        """,
        (
            game["appid"],
            game.get("name", f"App {game['appid']}"),
            game.get("rtime_last_played"),
            now,
        ),
    )
    game_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO game_platforms
        (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
        VALUES (?, 'steam', 1, ?, ?, ?)
        """,
        (
            game_id,
            game.get("playtime_forever", 0),
            game.get("playtime_2weeks", 0),
            now,
        ),
    )

conn.execute(
    "INSERT OR REPLACE INTO meta (key, value) VALUES ('library_synced_at', ?)", (now,)
)
conn.commit()
conn.close()

print(f"Seeded {len(games)} real Steam games into v1 DB at {db_path}")
