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


_NEW_SCHEMA_DDL = """
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
        playtime_2weeks_minutes INTEGER,
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
"""


async def _migrate_legacy_schema(db: aiosqlite.Connection) -> None:
    """Migrate old appid-as-PK schema to cross-platform schema in place.

    Renames existing tables to *_old, re-creates the new schema, migrates
    all data, then drops the *_old tables.  Called from init_db() when the
    games table exists but lacks an 'id' column.
    """
    await db.execute("PRAGMA foreign_keys=OFF")

    old_cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(games)")}
    db.row_factory = aiosqlite.Row
    old_games = await db.execute_fetchall("SELECT * FROM games")

    tables = {
        row[0]
        for row in await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    old_ratings: list = []
    if "ratings" in tables:
        old_ratings = await db.execute_fetchall("SELECT * FROM ratings")
        await db.execute("ALTER TABLE ratings RENAME TO ratings_old")

    await db.execute("ALTER TABLE games RENAME TO games_old")
    await db.commit()

    # Create new schema (executescript issues an implicit COMMIT first)
    await db.executescript(_NEW_SCHEMA_DDL)

    # Migrate games rows
    keep_cols = [
        "appid", "name", "genres", "tags", "short_description",
        "metacritic_score", "hltb_main", "hltb_extra",
        "protondb_tier", "steam_review_score", "steam_review_desc",
        "store_cached_at", "hltb_cached_at", "metacritic_cached_at",
        "protondb_cached_at", "steamspy_cached_at",
        "rtime_last_played", "is_farmed", "library_updated_at",
    ]
    for row in old_games:
        present = [c for c in keep_cols if c in old_cols]
        cols_sql = ", ".join(present)
        placeholders = ", ".join("?" for _ in present)
        values = [row[c] for c in present]
        if "hltb_completionist" in old_cols and row["hltb_completionist"] is not None:
            cols_sql += ", hltb_complete"
            placeholders += ", ?"
            values.append(row["hltb_completionist"])
        if not values:
            continue
        await db.execute(
            f"INSERT OR IGNORE INTO games ({cols_sql}) VALUES ({placeholders})",
            values,
        )
    await db.commit()

    # Create game_platforms rows from old playtime columns
    for row in old_games:
        game = await db.execute_fetchone(
            "SELECT id FROM games WHERE appid = ?", (row["appid"],)
        )
        if game is None:
            continue
        playtime = row["playtime_forever"] if "playtime_forever" in old_cols else None
        playtime_2weeks = row["playtime_2weeks"] if "playtime_2weeks" in old_cols else None
        await db.execute(
            """INSERT OR IGNORE INTO game_platforms
               (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
               VALUES (?, 'steam', 1, ?, ?, datetime('now'))""",
            (game["id"], playtime, playtime_2weeks),
        )
    await db.commit()

    # Migrate ratings rows
    for row in old_ratings:
        game = await db.execute_fetchone(
            "SELECT id FROM games WHERE appid = ?", (row["appid"],)
        )
        if game is None:
            continue
        await db.execute(
            """INSERT OR IGNORE INTO ratings
               (game_id, source, raw_score, normalized_score, review_text, synced_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (game["id"], row["source"], row["raw_score"],
             row["normalized_score"], row["review_text"], row["synced_at"]),
        )
    await db.commit()

    # Drop legacy tables
    await db.execute("DROP TABLE IF EXISTS games_old")
    await db.execute("DROP TABLE IF EXISTS ratings_old")
    await db.commit()

    await db.execute("PRAGMA foreign_keys=ON")


async def init_db() -> None:
    """Create tables if they don't exist, auto-migrating legacy schemas."""
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("PRAGMA journal_mode=WAL")

        # Detect legacy schema (games table exists but has no 'id' column)
        tables = {
            row[0]
            for row in await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "games" in tables:
            game_cols = {
                row[1] for row in await db.execute_fetchall("PRAGMA table_info(games)")
            }
            if "id" not in game_cols:
                await _migrate_legacy_schema(db)

        await db.executescript(_NEW_SCHEMA_DDL)
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
    playtime_2weeks_minutes: int | None = None,
    owned: int = 1,
) -> None:
    """Insert or update a game_platforms row."""
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO game_platforms (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id, platform) DO UPDATE SET
                   owned=excluded.owned,
                   playtime_minutes=excluded.playtime_minutes,
                   playtime_2weeks_minutes=excluded.playtime_2weeks_minutes,
                   last_synced=excluded.last_synced""",
            (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, now),
        )
        await db.commit()


async def find_game_by_name_fuzzy(name: str, cutoff: int = 85) -> aiosqlite.Row | None:
    """Return the best-matching games row for *name* using rapidfuzz, or None."""
    from rapidfuzz import process, fuzz

    async with get_db() as db:
        rows = await (await db.execute("SELECT id, name FROM games")).fetchall()

    if not rows:
        return None

    names = [r["name"] for r in rows]
    match = process.extractOne(name, names, scorer=fuzz.token_sort_ratio, score_cutoff=cutoff)
    if match is None:
        return None

    matched_name = match[0]
    for row in rows:
        if row["name"] == matched_name:
            return row
    return None
