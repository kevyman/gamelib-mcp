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
                appid INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                playtime_forever INTEGER DEFAULT 0,
                playtime_2weeks INTEGER DEFAULT 0,
                -- Steam Store cache
                genres TEXT,
                tags TEXT,
                short_description TEXT,
                steam_review_score INTEGER,
                steam_review_desc TEXT,
                store_cached_at TEXT,
                -- HLTB cache
                hltb_main REAL,
                hltb_extra REAL,
                hltb_completionist REAL,
                hltb_cached_at TEXT,
                -- Metacritic cache (sourced from Steam Store appdetails)
                metacritic_score INTEGER,
                metacritic_cached_at TEXT,
                -- ProtonDB cache
                protondb_tier TEXT,
                protondb_cached_at TEXT,
                -- SteamSpy user-curated tags cache
                steamspy_cached_at TEXT,
                -- Card-farm detection
                rtime_last_played INTEGER,
                is_farmed INTEGER DEFAULT 0,
                -- Meta
                library_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appid INTEGER,
                source TEXT NOT NULL,
                raw_score REAL,
                normalized_score REAL,
                review_text TEXT,
                synced_at TEXT NOT NULL,
                UNIQUE(appid, source)
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

        # Migration: add rtime_last_played and is_farmed columns
        cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(games)")}
        if "rtime_last_played" not in cols:
            await db.execute("ALTER TABLE games ADD COLUMN rtime_last_played INTEGER")
            await db.execute("ALTER TABLE games ADD COLUMN is_farmed INTEGER DEFAULT 0")
            await db.commit()

        # Migration: add steamspy_cached_at column
        cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(games)")}
        if "steamspy_cached_at" not in cols:
            await db.execute("ALTER TABLE games ADD COLUMN steamspy_cached_at TEXT")
            await db.commit()

        # Migration: rename opencritic_* → metacritic_* if old columns exist
        cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(games)")}
        if "opencritic_score" in cols and "metacritic_score" not in cols:
            await db.execute("ALTER TABLE games ADD COLUMN metacritic_score INTEGER")
            await db.execute("ALTER TABLE games ADD COLUMN metacritic_cached_at TEXT")
            await db.execute("UPDATE games SET metacritic_score = opencritic_score, metacritic_cached_at = opencritic_cached_at")
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
            SELECT r.appid, r.source, r.normalized_score, g.tags
            FROM ratings r
            JOIN games g ON g.appid = r.appid
            WHERE g.tags IS NOT NULL AND r.normalized_score IS NOT NULL
        """)

    # Accumulate per-tag weighted scores
    tag_data: dict[str, dict] = {}  # tag -> {weighted_sum, weight_sum, appids}

    for row in rows:
        try:
            tags = json.loads(row["tags"])
        except (ValueError, TypeError):
            continue
        weight = source_weights.get(row["source"], 0.5)
        score = row["normalized_score"]
        appid = row["appid"]

        for tag in tags:
            tag_lower = tag.lower()
            if tag_lower not in tag_data:
                tag_data[tag_lower] = {"weighted_sum": 0.0, "weight_sum": 0.0, "appids": set()}
            tag_data[tag_lower]["weighted_sum"] += score * weight
            tag_data[tag_lower]["weight_sum"] += weight
            tag_data[tag_lower]["appids"].add(appid)

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
