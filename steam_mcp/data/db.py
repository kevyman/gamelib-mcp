"""SQLite connection, schema migrations, and tag_affinity recompute."""

import json
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiosqlite


# Polyfill: aiosqlite <0.20 doesn't have execute_fetchone as a Connection method
async def _execute_fetchone(self, sql, parameters=()):
    async with self.execute(sql, parameters) as cursor:
        return await cursor.fetchone()

if not hasattr(aiosqlite.Connection, "execute_fetchone"):
    aiosqlite.Connection.execute_fetchone = _execute_fetchone  # type: ignore[method-assign]


_DB_PATH = os.getenv("DATABASE_URL", "file:steam.db").removeprefix("file:")


def _db_path() -> str:
    return _DB_PATH


@asynccontextmanager
async def get_db():
    """Async context manager for a WAL-enabled, Row-factory SQLite connection."""
    async with aiosqlite.connect(_db_path()) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn


async def init_db() -> None:
    """Create tables if they don't exist."""
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS games (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                appid            INTEGER UNIQUE,
                igdb_id          INTEGER UNIQUE,
                name             TEXT NOT NULL,
                sort_name        TEXT,
                release_date     TEXT,
                genres           TEXT,
                tags             TEXT,
                short_description TEXT,
                metacritic_score INTEGER,
                hltb_main        REAL,
                hltb_extra       REAL,
                hltb_complete    REAL,
                protondb_tier    TEXT,
                opencritic_score INTEGER,
                steam_review_score INTEGER,
                steam_review_desc  TEXT,
                store_enriched   INTEGER DEFAULT 0,
                store_enriched_at TEXT,
                store_cached_at  TEXT,
                hltb_cached_at   TEXT,
                metacritic_cached_at TEXT,
                protondb_cached_at TEXT,
                steamspy_cached_at TEXT,
                rtime_last_played INTEGER,
                is_farmed        INTEGER DEFAULT 0,
                library_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS game_platforms (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id          INTEGER NOT NULL REFERENCES games(id),
                platform         TEXT NOT NULL,
                owned            INTEGER NOT NULL DEFAULT 1,
                playtime_minutes INTEGER,
                last_synced      TEXT,
                UNIQUE(game_id, platform)
            );

            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER REFERENCES games(id),
                source TEXT NOT NULL,
                raw_score REAL,
                normalized_score REAL,
                review_text TEXT,
                synced_at TEXT NOT NULL,
                UNIQUE(game_id, source)
            );

            CREATE TABLE IF NOT EXISTS tag_affinity (
                tag TEXT PRIMARY KEY,
                affinity_score REAL,
                avg_score REAL,
                game_count INTEGER,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        await db.commit()


async def recompute_tag_affinity() -> int:
    """
    Recompute tag_affinity from all rated games.

    affinity_score = weighted_avg_score × log(game_count + 1)

    Backloggd weight = 1.0, Steam review weight = 0.5.
    Returns number of tags updated.
    """
    source_weights = {"backloggd": 1.0, "steam_review": 0.5}

    async with get_db() as db:
        # Fetch all ratings joined with game tags
        rows = await db.execute_fetchall("""
            SELECT r.game_id, r.source, r.normalized_score, g.tags
            FROM ratings r
            JOIN games g ON g.id = r.game_id
            WHERE g.tags IS NOT NULL AND r.normalized_score IS NOT NULL
        """)

    # Accumulate per-tag weighted scores
    tag_data: dict[str, dict] = {}  # tag -> {weighted_sum, weight_sum, game_ids}

    for row in rows:
        try:
            tags = json.loads(row["tags"])
        except (ValueError, TypeError):
            continue
        weight = source_weights.get(row["source"], 0.5)
        score = row["normalized_score"]
        game_id = row["game_id"]

        for tag in tags:
            tag_lower = tag.lower()
            if tag_lower not in tag_data:
                tag_data[tag_lower] = {"weighted_sum": 0.0, "weight_sum": 0.0, "appids": set()}
            tag_data[tag_lower]["weighted_sum"] += score * weight
            tag_data[tag_lower]["weight_sum"] += weight
            tag_data[tag_lower]["appids"].add(game_id)

    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        await db.execute("DELETE FROM tag_affinity")
        for tag, data in tag_data.items():
            if data["weight_sum"] == 0:
                continue
            avg_score = data["weighted_sum"] / data["weight_sum"]
            game_count = len(data["appids"])
            affinity_score = avg_score * math.log(game_count + 1)
            await db.execute(
                """INSERT INTO tag_affinity (tag, affinity_score, avg_score, game_count, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (tag, affinity_score, avg_score, game_count, now),
            )
        await db.commit()

    return len(tag_data)


async def get_meta(key: str) -> str | None:
    async with get_db() as db:
        row = await db.execute_fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None


async def set_meta(key: str, value: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
        await db.commit()


async def get_game_by_appid(appid: int) -> aiosqlite.Row | None:
    async with get_db() as db:
        return await db.execute_fetchone(
            "SELECT * FROM games WHERE appid = ?", (appid,)
        )


async def upsert_game(
    appid: int | None,
    name: str,
    **fields,
) -> int:
    """Insert or update a game row. Returns games.id."""
    async with get_db() as db:
        if appid is not None:
            await db.execute(
                """INSERT INTO games (appid, name) VALUES (?, ?)
                   ON CONFLICT(appid) DO UPDATE SET name=excluded.name""",
                (appid, name),
            )
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE appid = ?", (appid,)
            )
        else:
            await db.execute(
                "INSERT OR IGNORE INTO games (name) VALUES (?)", (name,)
            )
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ? AND appid IS NULL", (name,)
            )
        game_id = row["id"]
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            await db.execute(
                f"UPDATE games SET {cols} WHERE id = ?",
                (*fields.values(), game_id),
            )
        await db.commit()
        return game_id


async def upsert_game_platform(
    game_id: int,
    platform: str,
    playtime_minutes: int | None = None,
    owned: int = 1,
) -> None:
    """Insert or update a game_platforms row."""
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO game_platforms (game_id, platform, owned, playtime_minutes, last_synced)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(game_id, platform) DO UPDATE SET
                   owned=excluded.owned,
                   playtime_minutes=excluded.playtime_minutes,
                   last_synced=excluded.last_synced""",
            (game_id, platform, owned, playtime_minutes, now),
        )
        await db.commit()
