"""SQLite connection, schema migrations, and shared game/platform helpers."""

import asyncio
import json
import math
import os
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Callable, Iterable, TypeVar

import aiosqlite


# Polyfill: aiosqlite <0.20 doesn't have execute_fetchone as a Connection method
async def _execute_fetchone(self, sql, parameters=()):
    async with self.execute(sql, parameters) as cursor:
        return await cursor.fetchone()


if not hasattr(aiosqlite.Connection, "execute_fetchone"):
    aiosqlite.Connection.execute_fetchone = _execute_fetchone  # type: ignore[method-assign]


_DB_READY_PATH: str | None = None
_DB_INIT_LOCK: asyncio.Lock | None = None
_FuzzyKey = TypeVar("_FuzzyKey")
_Progress = Callable[[str], None]
_SQLITE_CONNECT_TIMEOUT_SECONDS = 30.0
_SQLITE_BUSY_TIMEOUT_MS = 30_000

STEAM_PLATFORM = "steam"
STEAM_APP_ID = "steam_appid"
EPIC_ARTIFACT_ID = "epic_artifact_id"
GOG_PRODUCT_ID = "gog_product_id"
SCHEMA_VERSION = 5


@dataclass
class MigrationResult:
    initial_version: int
    final_version: int
    detected_state: str
    applied_steps: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.applied_steps)


def _db_path() -> str:
    configured = os.getenv("DATABASE_URL")
    if configured:
        return configured.removeprefix("file:")

    if os.path.exists("gamelib.db"):
        return "gamelib.db"
    if os.path.exists("steam.db"):
        return "steam.db"
    return "gamelib.db"


def _default_process(value: str) -> str:
    return " ".join(sorted(re.findall(r"[a-z0-9]+", value.casefold())))


def _iter_chunks(rows: list[dict], chunk_size: int) -> Iterable[list[dict]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")

    for start in range(0, len(rows), chunk_size):
        yield rows[start : start + chunk_size]


def extract_best_fuzzy_key(
    query: str,
    choices: dict[_FuzzyKey, str],
    cutoff: int = 85,
) -> _FuzzyKey | None:
    """Return the best fuzzy-match key, with a stdlib fallback if rapidfuzz is absent."""
    if not choices:
        return None

    try:
        from rapidfuzz import fuzz, process, utils

        result = process.extractOne(
            query,
            choices,
            scorer=fuzz.token_sort_ratio,
            processor=utils.default_process,
            score_cutoff=cutoff,
        )
        if result is None:
            return None
        return result[2]
    except ModuleNotFoundError:
        processed_query = _default_process(query)
        if not processed_query:
            return None

        best_key = None
        best_score = float("-inf")
        for key, value in choices.items():
            processed_value = _default_process(value)
            if not processed_value:
                continue
            score = SequenceMatcher(None, processed_query, processed_value).ratio() * 100
            if score > best_score:
                best_key = key
                best_score = score

        if best_key is None or best_score < cutoff:
            return None
        return best_key


_V1_SCHEMA_DDL = """
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


_V2_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS games (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
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
        opencritic_score INTEGER,
        hltb_cached_at   TEXT,
        is_farmed        INTEGER NOT NULL DEFAULT 0
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

    CREATE TABLE IF NOT EXISTS game_platform_identifiers (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        game_platform_id INTEGER NOT NULL REFERENCES game_platforms(id) ON DELETE CASCADE,
        identifier_type  TEXT NOT NULL,
        identifier_value TEXT NOT NULL,
        is_primary       INTEGER NOT NULL DEFAULT 1,
        last_seen_at     TEXT,
        UNIQUE(identifier_type, identifier_value)
    );

    CREATE TABLE IF NOT EXISTS steam_platform_data (
        game_platform_id    INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
        steam_review_score  INTEGER,
        steam_review_desc   TEXT,
        protondb_tier       TEXT,
        store_cached_at     TEXT,
        protondb_cached_at  TEXT,
        steamspy_cached_at  TEXT,
        rtime_last_played   INTEGER,
        library_updated_at  TEXT
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

    CREATE INDEX IF NOT EXISTS idx_game_platforms_game_id ON game_platforms(game_id);
    CREATE INDEX IF NOT EXISTS idx_game_platforms_platform ON game_platforms(platform);
    CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_platform_id
        ON game_platform_identifiers(game_platform_id);
    CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_lookup
        ON game_platform_identifiers(identifier_type, identifier_value);
"""


_V3_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS games (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        igdb_id          INTEGER UNIQUE,
        name             TEXT NOT NULL,
        sort_name        TEXT,
        release_date     TEXT,
        genres           TEXT,
        tags             TEXT,
        short_description TEXT,
        hltb_main        REAL,
        hltb_extra       REAL,
        hltb_complete    REAL,
        hltb_cached_at   TEXT,
        igdb_cached_at   TEXT,
        is_farmed        INTEGER NOT NULL DEFAULT 0
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

    CREATE TABLE IF NOT EXISTS game_platform_identifiers (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        game_platform_id INTEGER NOT NULL REFERENCES game_platforms(id) ON DELETE CASCADE,
        identifier_type  TEXT NOT NULL,
        identifier_value TEXT NOT NULL,
        is_primary       INTEGER NOT NULL DEFAULT 1,
        last_seen_at     TEXT,
        UNIQUE(identifier_type, identifier_value)
    );

    CREATE TABLE IF NOT EXISTS steam_platform_data (
        game_platform_id    INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
        steam_review_score  INTEGER,
        steam_review_desc   TEXT,
        protondb_tier       TEXT,
        store_cached_at     TEXT,
        protondb_cached_at  TEXT,
        steamspy_cached_at  TEXT,
        rtime_last_played   INTEGER,
        library_updated_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS game_platform_enrichment (
        game_platform_id      INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
        platform_release_date TEXT,
        metacritic_score      INTEGER,
        metacritic_url        TEXT,
        opencritic_id         INTEGER,
        opencritic_score      INTEGER,
        opencritic_tier       TEXT,
        opencritic_percent_rec REAL,
        metacritic_cached_at  TEXT,
        opencritic_cached_at  TEXT
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

    CREATE INDEX IF NOT EXISTS idx_game_platforms_game_id ON game_platforms(game_id);
    CREATE INDEX IF NOT EXISTS idx_game_platforms_platform ON game_platforms(platform);
    CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_platform_id
        ON game_platform_identifiers(game_platform_id);
    CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_lookup
        ON game_platform_identifiers(identifier_type, identifier_value);
"""


_V4_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS games (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        igdb_id          INTEGER UNIQUE,
        name             TEXT NOT NULL,
        sort_name        TEXT,
        release_date     TEXT,
        genres           TEXT,
        tags             TEXT,
        short_description TEXT,
        hltb_main        REAL,
        hltb_extra       REAL,
        hltb_complete    REAL,
        hltb_cached_at   TEXT,
        hltb_claimed_at  TEXT,
        igdb_cached_at   TEXT,
        igdb_claimed_at  TEXT,
        is_farmed        INTEGER NOT NULL DEFAULT 0
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

    CREATE TABLE IF NOT EXISTS game_platform_identifiers (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        game_platform_id INTEGER NOT NULL REFERENCES game_platforms(id) ON DELETE CASCADE,
        identifier_type  TEXT NOT NULL,
        identifier_value TEXT NOT NULL,
        is_primary       INTEGER NOT NULL DEFAULT 1,
        last_seen_at     TEXT,
        UNIQUE(identifier_type, identifier_value)
    );

    CREATE TABLE IF NOT EXISTS steam_platform_data (
        game_platform_id    INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
        steam_review_score  INTEGER,
        steam_review_desc   TEXT,
        protondb_tier       TEXT,
        store_cached_at     TEXT,
        store_claimed_at    TEXT,
        protondb_cached_at  TEXT,
        protondb_claimed_at TEXT,
        steamspy_cached_at  TEXT,
        steamspy_claimed_at TEXT,
        rtime_last_played   INTEGER,
        library_updated_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS game_platform_enrichment (
        game_platform_id       INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
        platform_release_date  TEXT,
        metacritic_score       INTEGER,
        metacritic_url         TEXT,
        metacritic_claimed_at  TEXT,
        opencritic_id          INTEGER,
        opencritic_url         TEXT,
        opencritic_score       INTEGER,
        opencritic_tier        TEXT,
        opencritic_percent_rec REAL,
        opencritic_num_reviews INTEGER,
        opencritic_cached_at   TEXT,
        opencritic_claimed_at  TEXT,
        metacritic_cached_at   TEXT
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

    CREATE INDEX IF NOT EXISTS idx_game_platforms_game_id ON game_platforms(game_id);
    CREATE INDEX IF NOT EXISTS idx_game_platforms_platform ON game_platforms(platform);
    CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_platform_id
        ON game_platform_identifiers(game_platform_id);
    CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_lookup
        ON game_platform_identifiers(identifier_type, identifier_value);
"""


async def _table_names(db: aiosqlite.Connection) -> set[str]:
    rows = await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in rows}


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
    return {row[1] for row in rows}


async def _get_user_version(db: aiosqlite.Connection) -> int:
    row = await db.execute_fetchone("PRAGMA user_version")
    return int(row[0]) if row else 0


async def _set_user_version(db: aiosqlite.Connection, version: int) -> None:
    await db.execute(f"PRAGMA user_version = {version}")


async def _detect_schema_state(db: aiosqlite.Connection) -> str:
    tables = await _table_names(db)
    if "games" not in tables:
        return "fresh"

    game_cols = await _table_columns(db, "games")
    if "id" not in game_cols:
        return "legacy"

    spd_cols = await _table_columns(db, "steam_platform_data") if "steam_platform_data" in tables else set()
    gpe_cols = await _table_columns(db, "game_platform_enrichment") if "game_platform_enrichment" in tables else set()
    if {
        "igdb_claimed_at",
        "hltb_claimed_at",
    }.issubset(game_cols) and {
        "store_claimed_at",
        "protondb_claimed_at",
        "steamspy_claimed_at",
    }.issubset(spd_cols) and {
        "opencritic_claimed_at",
        "metacritic_claimed_at",
        "opencritic_url",
        "opencritic_num_reviews",
    }.issubset(gpe_cols):
        return "v5"

    if {
        "igdb_claimed_at",
        "hltb_claimed_at",
    }.issubset(game_cols) and {
        "store_claimed_at",
        "protondb_claimed_at",
        "steamspy_claimed_at",
    }.issubset(spd_cols) and {
        "opencritic_claimed_at",
        "metacritic_claimed_at",
    }.issubset(gpe_cols):
        return "v4"

    if "game_platform_enrichment" in tables and "metacritic_score" not in game_cols:
        return "v3"

    if {
        "game_platform_identifiers",
        "steam_platform_data",
    }.issubset(tables) and "appid" not in game_cols:
        return "v2"

    return "v1"


def _emit(progress: _Progress | None, message: str, applied_steps: list[str], changed: bool = True) -> None:
    if changed:
        applied_steps.append(message)
    if progress is not None:
        progress(message)


async def _migrate_legacy_to_v1(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    await db.execute("PRAGMA foreign_keys=OFF")
    db.row_factory = aiosqlite.Row

    tables = await _table_names(db)
    if "games" not in tables:
        await db.executescript(_V1_SCHEMA_DDL)
        await _set_user_version(db, 1)
        await db.commit()
        await db.execute("PRAGMA foreign_keys=ON")
        return

    game_cols = await _table_columns(db, "games")
    if "id" in game_cols:
        await _set_user_version(db, 1)
        await db.commit()
        await db.execute("PRAGMA foreign_keys=ON")
        return

    if progress is not None:
        progress("Migrating legacy Steam schema to v1.")

    await db.execute("ALTER TABLE games RENAME TO games_old")
    if "ratings" in tables:
        await db.execute("ALTER TABLE ratings RENAME TO ratings_old")
    if "tag_affinity" in tables:
        await db.execute("ALTER TABLE tag_affinity RENAME TO tag_affinity_old")
    await db.commit()

    await db.executescript(_V1_SCHEMA_DDL)

    old_cols = await _table_columns(db, "games_old")
    old_games = await db.execute_fetchall("SELECT * FROM games_old")

    keep_cols = [
        "appid",
        "name",
        "genres",
        "tags",
        "short_description",
        "metacritic_score",
        "hltb_main",
        "hltb_extra",
        "protondb_tier",
        "steam_review_score",
        "steam_review_desc",
        "store_cached_at",
        "hltb_cached_at",
        "metacritic_cached_at",
        "protondb_cached_at",
        "steamspy_cached_at",
        "rtime_last_played",
        "is_farmed",
        "library_updated_at",
    ]

    for row in old_games:
        present = [col for col in keep_cols if col in old_cols and row[col] is not None]
        if "hltb_completionist" in old_cols and row["hltb_completionist"] is not None:
            present = [*present, "hltb_complete"]

        if not present:
            continue

        values = []
        for col in present:
            if col == "hltb_complete":
                values.append(row["hltb_completionist"])
            else:
                values.append(row[col])

        cols_sql = ", ".join(present)
        placeholders = ", ".join("?" for _ in present)
        await db.execute(
            f"INSERT OR IGNORE INTO games ({cols_sql}) VALUES ({placeholders})",
            values,
        )

    await db.commit()

    for row in old_games:
        game = await db.execute_fetchone(
            "SELECT id FROM games WHERE appid = ?",
            (row["appid"],),
        )
        if game is None:
            continue
        playtime = row["playtime_forever"] if "playtime_forever" in old_cols else None
        playtime_2weeks = row["playtime_2weeks"] if "playtime_2weeks" in old_cols else None
        await db.execute(
            """INSERT OR IGNORE INTO game_platforms
               (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
               VALUES (?, ?, 1, ?, ?, datetime('now'))""",
            (game["id"], STEAM_PLATFORM, playtime, playtime_2weeks),
        )

    await db.commit()

    if "ratings_old" in await _table_names(db):
        old_ratings = await db.execute_fetchall("SELECT * FROM ratings_old")
        for row in old_ratings:
            game = await db.execute_fetchone(
                "SELECT id FROM games WHERE appid = ?",
                (row["appid"],),
            )
            if game is None:
                continue
            await db.execute(
                """INSERT OR IGNORE INTO ratings
                   (game_id, source, raw_score, normalized_score, review_text, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    game["id"],
                    row["source"],
                    row["raw_score"],
                    row["normalized_score"],
                    row["review_text"],
                    row["synced_at"],
                ),
            )
        await db.commit()

    if "tag_affinity_old" in await _table_names(db):
        await db.execute("DELETE FROM tag_affinity")
        await db.execute("INSERT INTO tag_affinity SELECT * FROM tag_affinity_old")
        await db.commit()

    for table in ("games_old", "ratings_old", "tag_affinity_old"):
        await db.execute(f"DROP TABLE IF EXISTS {table}")

    await _set_user_version(db, 1)
    await db.commit()
    await db.execute("PRAGMA foreign_keys=ON")


async def _migrate_v1_to_v2(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    if progress is not None:
        progress("Migrating cross-platform schema to v2 normalization.")

    await db.execute("PRAGMA foreign_keys=OFF")
    db.row_factory = aiosqlite.Row
    now = datetime.now(timezone.utc).isoformat()
    game_platform_rows = await db.execute_fetchall(
        """SELECT id, game_id, platform, owned, playtime_minutes,
                  playtime_2weeks_minutes, last_synced
           FROM game_platforms"""
    )
    ratings_rows = await db.execute_fetchall(
        """SELECT id, game_id, source, raw_score, normalized_score,
                  review_text, synced_at
           FROM ratings"""
    )

    await db.execute("ALTER TABLE games RENAME TO games_v1_old")
    await db.execute("ALTER TABLE game_platforms RENAME TO game_platforms_v1_old")
    await db.execute("ALTER TABLE ratings RENAME TO ratings_v1_old")
    await db.commit()

    await db.executescript(_V2_SCHEMA_DDL)

    old_cols = await _table_columns(db, "games_v1_old")
    keep_cols = [
        "id",
        "igdb_id",
        "name",
        "sort_name",
        "release_date",
        "genres",
        "tags",
        "short_description",
        "metacritic_score",
        "hltb_main",
        "hltb_extra",
        "hltb_complete",
        "opencritic_score",
        "hltb_cached_at",
        "is_farmed",
    ]
    present = [col for col in keep_cols if col in old_cols]
    cols_sql = ", ".join(present)
    await db.execute(
        f"INSERT INTO games ({cols_sql}) SELECT {cols_sql} FROM games_v1_old"
    )

    for row in game_platform_rows:
        await db.execute(
            """INSERT INTO game_platforms
               (id, game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                row["game_id"],
                row["platform"],
                row["owned"],
                row["playtime_minutes"],
                row["playtime_2weeks_minutes"],
                row["last_synced"],
            ),
        )

    missing_steam_rows = await db.execute_fetchall(
        """SELECT g.id AS game_id
           FROM games_v1_old g
           LEFT JOIN game_platforms gp
             ON gp.game_id = g.id AND gp.platform = ?
           WHERE g.appid IS NOT NULL AND gp.id IS NULL""",
        (STEAM_PLATFORM,),
    )
    for row in missing_steam_rows:
        await db.execute(
            """INSERT INTO game_platforms
               (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
               VALUES (?, ?, 1, NULL, NULL, ?)""",
            (row["game_id"], STEAM_PLATFORM, now),
        )

    rows = await db.execute_fetchall(
        """SELECT gp.id AS game_platform_id, g.appid
           FROM games_v1_old g
           JOIN game_platforms gp
             ON gp.game_id = g.id AND gp.platform = ?
           WHERE g.appid IS NOT NULL""",
        (STEAM_PLATFORM,),
    )
    for row in rows:
        await db.execute(
            """INSERT INTO game_platform_identifiers
               (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at)
               VALUES (?, ?, ?, 1, ?)
               ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                   game_platform_id = excluded.game_platform_id,
                   is_primary = excluded.is_primary,
                   last_seen_at = excluded.last_seen_at""",
            (row["game_platform_id"], STEAM_APP_ID, str(row["appid"]), now),
        )

    steam_rows = await db.execute_fetchall(
        """SELECT gp.id AS game_platform_id,
                  g.steam_review_score,
                  g.steam_review_desc,
                  g.protondb_tier,
                  g.store_cached_at,
                  g.protondb_cached_at,
                  g.steamspy_cached_at,
                  g.rtime_last_played,
                  g.library_updated_at
           FROM games_v1_old g
           JOIN game_platforms gp
             ON gp.game_id = g.id AND gp.platform = ?""",
        (STEAM_PLATFORM,),
    )
    for row in steam_rows:
        await db.execute(
            """INSERT INTO steam_platform_data
               (game_platform_id, steam_review_score, steam_review_desc, protondb_tier,
                store_cached_at, protondb_cached_at, steamspy_cached_at,
                rtime_last_played, library_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_platform_id) DO UPDATE SET
                   steam_review_score = excluded.steam_review_score,
                   steam_review_desc = excluded.steam_review_desc,
                   protondb_tier = excluded.protondb_tier,
                   store_cached_at = excluded.store_cached_at,
                   protondb_cached_at = excluded.protondb_cached_at,
                   steamspy_cached_at = excluded.steamspy_cached_at,
                   rtime_last_played = excluded.rtime_last_played,
                   library_updated_at = excluded.library_updated_at""",
            (
                row["game_platform_id"],
                row["steam_review_score"],
                row["steam_review_desc"],
                row["protondb_tier"],
                row["store_cached_at"],
                row["protondb_cached_at"],
                row["steamspy_cached_at"],
                row["rtime_last_played"],
                row["library_updated_at"],
            ),
        )

    for row in ratings_rows:
        await db.execute(
            """INSERT INTO ratings
               (id, game_id, source, raw_score, normalized_score, review_text, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                row["game_id"],
                row["source"],
                row["raw_score"],
                row["normalized_score"],
                row["review_text"],
                row["synced_at"],
            ),
        )

    await db.execute("DROP TABLE IF EXISTS games_v1_old")
    await db.execute("DROP TABLE IF EXISTS game_platforms_v1_old")
    await db.execute("DROP TABLE IF EXISTS ratings_v1_old")
    await _set_user_version(db, 2)
    await db.commit()
    await db.execute("PRAGMA foreign_keys=ON")


async def _migrate_v2_to_v3(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    if progress is not None:
        progress("Migrating to v3: platform-specific enrichment schema.")

    await db.execute("PRAGMA foreign_keys=OFF")
    db.row_factory = aiosqlite.Row

    # 1. Create game_platform_enrichment table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS game_platform_enrichment (
            game_platform_id      INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
            platform_release_date TEXT,
            metacritic_score      INTEGER,
            metacritic_url        TEXT,
            opencritic_id         INTEGER,
            opencritic_score      INTEGER,
            opencritic_tier       TEXT,
            opencritic_percent_rec REAL,
            metacritic_cached_at  TEXT,
            opencritic_cached_at  TEXT
        )
    """)

    # 2. Migrate metacritic_score from games → game_platform_enrichment (Steam rows only)
    game_cols = await _table_columns(db, "games")
    if "metacritic_score" in game_cols:
        await db.execute(
            """INSERT OR IGNORE INTO game_platform_enrichment (game_platform_id, metacritic_score)
               SELECT gp.id, g.metacritic_score
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = ?
               WHERE g.metacritic_score IS NOT NULL""",
            (STEAM_PLATFORM,),
        )

    # 3. Rebuild games table: drop metacritic_score, opencritic_score; add igdb_cached_at
    await db.execute("ALTER TABLE games RENAME TO games_v2_old")
    await db.execute("""
        CREATE TABLE games (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            igdb_id          INTEGER UNIQUE,
            name             TEXT NOT NULL,
            sort_name        TEXT,
            release_date     TEXT,
            genres           TEXT,
            tags             TEXT,
            short_description TEXT,
            hltb_main        REAL,
            hltb_extra       REAL,
            hltb_complete    REAL,
            hltb_cached_at   TEXT,
            igdb_cached_at   TEXT,
            is_farmed        INTEGER NOT NULL DEFAULT 0
        )
    """)

    old_cols = await _table_columns(db, "games_v2_old")
    keep = [c for c in [
        "id", "igdb_id", "name", "sort_name", "release_date",
        "genres", "tags", "short_description",
        "hltb_main", "hltb_extra", "hltb_complete", "hltb_cached_at", "is_farmed",
    ] if c in old_cols]
    cols_sql = ", ".join(keep)
    await db.execute(f"INSERT INTO games ({cols_sql}) SELECT {cols_sql} FROM games_v2_old")
    await db.execute("DROP TABLE IF EXISTS games_v2_old")

    await _set_user_version(db, 3)
    await db.commit()
    await db.execute("PRAGMA foreign_keys=ON")


async def _migrate_v3_to_v4(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    if progress is not None:
        progress("Migrating to v4: claim-aware enrichment schema.")

    async def add_column_if_missing(table: str, column_name: str, ddl: str) -> None:
        columns = await _table_columns(db, table)
        if column_name not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    await add_column_if_missing("games", "igdb_claimed_at", "igdb_claimed_at TEXT")
    await add_column_if_missing("games", "hltb_claimed_at", "hltb_claimed_at TEXT")
    await add_column_if_missing("steam_platform_data", "store_claimed_at", "store_claimed_at TEXT")
    await add_column_if_missing("steam_platform_data", "protondb_claimed_at", "protondb_claimed_at TEXT")
    await add_column_if_missing("steam_platform_data", "steamspy_claimed_at", "steamspy_claimed_at TEXT")
    await add_column_if_missing("game_platform_enrichment", "opencritic_claimed_at", "opencritic_claimed_at TEXT")
    await add_column_if_missing("game_platform_enrichment", "metacritic_claimed_at", "metacritic_claimed_at TEXT")

    await _set_user_version(db, 4)
    await db.commit()


async def _migrate_v4_to_v5(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    if progress is not None:
        progress("Migrating to v5: add OpenCritic scrape result columns.")

    async def add_column_if_missing(table: str, column_name: str, ddl: str) -> None:
        columns = await _table_columns(db, table)
        if column_name not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    await add_column_if_missing("game_platform_enrichment", "opencritic_url", "opencritic_url TEXT")
    await add_column_if_missing(
        "game_platform_enrichment",
        "opencritic_num_reviews",
        "opencritic_num_reviews INTEGER",
    )

    await _set_user_version(db, 5)
    await db.commit()


async def _run_migrations(
    db: aiosqlite.Connection,
    progress: _Progress | None = None,
) -> MigrationResult:
    detected_state = await _detect_schema_state(db)
    initial_version = await _get_user_version(db)
    version = initial_version
    applied_steps: list[str] = []

    if detected_state == "fresh":
        await db.executescript(_V4_SCHEMA_DDL)
        await _set_user_version(db, SCHEMA_VERSION)
        await db.commit()
        _emit(progress, "Initialized fresh database at schema v5.", applied_steps)
        return MigrationResult(
            initial_version=initial_version,
            final_version=SCHEMA_VERSION,
            detected_state=detected_state,
            applied_steps=applied_steps,
        )

    if version == 0:
        if detected_state == "legacy":
            _emit(progress, "Applying migration step v0 -> v1.", applied_steps)
            await _migrate_legacy_to_v1(db, progress=None)
            version = 1
        elif detected_state == "v1":
            await _set_user_version(db, 1)
            await db.commit()
            version = 1
            _emit(progress, "Recorded existing schema as v1.", applied_steps)
        elif detected_state == "v2":
            await _set_user_version(db, 2)
            await db.commit()
            version = 2
            _emit(progress, "Recorded existing schema as v2.", applied_steps)
        elif detected_state == "v3":
            await _set_user_version(db, 3)
            await db.commit()
            version = 3
            _emit(progress, "Recorded existing schema as v3.", applied_steps)
        elif detected_state == "v4":
            await _set_user_version(db, 4)
            await db.commit()
            version = 4
            _emit(progress, "Recorded existing schema as v4.", applied_steps)
        elif detected_state == "v5":
            await _set_user_version(db, 5)
            await db.commit()
            version = 5
            _emit(progress, "Recorded existing schema as v5.", applied_steps)

    if version == 1:
        _emit(progress, "Applying migration step v1 -> v2.", applied_steps)
        await _migrate_v1_to_v2(db, progress=None)
        version = 2

    if version == 2:
        _emit(progress, "Applying migration step v2 -> v3.", applied_steps)
        await _migrate_v2_to_v3(db, progress=None)
        version = 3

    if version == 3:
        _emit(progress, "Applying migration step v3 -> v4.", applied_steps)
        await _migrate_v3_to_v4(db, progress=None)
        version = 4

    if version == 4:
        _emit(progress, "Applying migration step v4 -> v5.", applied_steps)
        await _migrate_v4_to_v5(db, progress=None)
        version = 5

    await db.executescript(_V4_SCHEMA_DDL)
    if version != SCHEMA_VERSION:
        await _set_user_version(db, SCHEMA_VERSION)
        version = SCHEMA_VERSION
    await db.commit()

    return MigrationResult(
        initial_version=initial_version,
        final_version=version,
        detected_state=detected_state,
        applied_steps=applied_steps,
    )


async def _ensure_db_initialized(db: aiosqlite.Connection) -> None:
    global _DB_READY_PATH, _DB_INIT_LOCK

    db_path = _db_path()
    if _DB_READY_PATH == db_path:
        return

    if _DB_INIT_LOCK is None:
        _DB_INIT_LOCK = asyncio.Lock()

    async with _DB_INIT_LOCK:
        if _DB_READY_PATH == db_path:
            return
        await _run_migrations(db)
        _DB_READY_PATH = db_path


async def _configure_connection(conn: aiosqlite.Connection, *, enable_wal: bool) -> None:
    conn.row_factory = aiosqlite.Row
    await conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    await conn.execute("PRAGMA foreign_keys=ON")
    if enable_wal:
        await conn.execute("PRAGMA journal_mode=WAL")


@asynccontextmanager
async def get_db():
    """Async context manager for a WAL-enabled, Row-factory SQLite connection."""
    db_path = _db_path()
    async with aiosqlite.connect(db_path, timeout=_SQLITE_CONNECT_TIMEOUT_SECONDS) as conn:
        await _configure_connection(conn, enable_wal=_DB_READY_PATH != db_path)
        await _ensure_db_initialized(conn)
        yield conn


async def migrate_db(progress: _Progress | None = None) -> MigrationResult:
    """Run all schema migrations against the configured DB path."""
    global _DB_READY_PATH

    db_path = _db_path()
    async with aiosqlite.connect(db_path, timeout=_SQLITE_CONNECT_TIMEOUT_SECONDS) as db:
        await _configure_connection(db, enable_wal=True)
        result = await _run_migrations(db, progress=progress)
        _DB_READY_PATH = db_path
        return result


async def init_db() -> None:
    """Create tables if they don't exist and migrate to the latest schema."""
    await migrate_db()


def _claim_cutoff_iso(minutes: int = 15) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


async def clear_claim(table: str, claim_column: str, row_id: int, id_column: str = "id") -> None:
    async with get_db() as db:
        await db.execute(
            f"UPDATE {table} SET {claim_column} = NULL WHERE {id_column} = ?",
            (row_id,),
        )
        await db.commit()


async def clear_all_enrichment_claims() -> None:
    async with get_db() as db:
        await db.execute("UPDATE games SET igdb_claimed_at = NULL, hltb_claimed_at = NULL")
        await db.execute(
            "UPDATE steam_platform_data SET store_claimed_at = NULL, protondb_claimed_at = NULL, steamspy_claimed_at = NULL"
        )
        await db.execute(
            "UPDATE game_platform_enrichment SET opencritic_claimed_at = NULL, metacritic_claimed_at = NULL"
        )
        await db.commit()


async def release_game_claim(game_id: int, column: str) -> None:
    await clear_claim("games", column, game_id)


async def _claim_ids(
    select_sql: str,
    select_params: tuple,
    update_sql: str,
    update_params_for_id: Callable[[str, int], tuple],
) -> list[int]:
    async with get_db() as db:
        rows = await db.execute_fetchall(select_sql, select_params)
        ids = [row["id"] for row in rows]
        if not ids:
            return []

        now = datetime.now(timezone.utc).isoformat()
        claimed: list[int] = []
        for row_id in ids:
            cursor = await db.execute(update_sql, update_params_for_id(now, row_id))
            if cursor.rowcount:
                claimed.append(row_id)
        await db.commit()
        return claimed


async def claim_game_ids_for_igdb(limit: int, stale_before: str) -> list[int]:
    return await _claim_ids(
        """SELECT id
           FROM games
           WHERE igdb_cached_at IS NULL
             AND (igdb_claimed_at IS NULL OR igdb_claimed_at < ?)
           ORDER BY id
           LIMIT ?""",
        (stale_before, limit),
        """UPDATE games
           SET igdb_claimed_at = ?
           WHERE id = ?
             AND igdb_cached_at IS NULL
             AND (igdb_claimed_at IS NULL OR igdb_claimed_at < ?)""",
        lambda now, game_id: (now, game_id, stale_before),
    )


async def claim_game_ids_for_hltb(limit: int, stale_before: str) -> list[int]:
    return await _claim_ids(
        """SELECT DISTINCT g.id
           FROM games g
           JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = ?
           LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
           WHERE spd.store_cached_at IS NOT NULL
             AND spd.store_cached_at != 'FAILED'
             AND (g.hltb_cached_at IS NULL OR g.hltb_cached_at = 'FAILED')
             AND (g.hltb_claimed_at IS NULL OR g.hltb_claimed_at < ?)
             AND g.is_farmed = 0
           ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, g.id
           LIMIT ?""",
        (STEAM_PLATFORM, stale_before, limit),
        """UPDATE games
           SET hltb_claimed_at = ?
           WHERE id = ?
             AND (hltb_cached_at IS NULL OR hltb_cached_at = 'FAILED')
             AND (hltb_claimed_at IS NULL OR hltb_claimed_at < ?)""",
        lambda now, game_id: (now, game_id, stale_before),
    )


async def claim_steam_platform_ids_for_store(limit: int, stale_before: str) -> list[int]:
    return await _claim_ids(
        """SELECT spd.game_platform_id AS id
           FROM steam_platform_data spd
           JOIN game_platforms gp ON gp.id = spd.game_platform_id
           JOIN games g ON g.id = gp.game_id
           JOIN game_platform_identifiers gpi
             ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
           WHERE spd.store_cached_at IS NULL
             AND (spd.store_claimed_at IS NULL OR spd.store_claimed_at < ?)
             AND g.is_farmed = 0
           ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, spd.game_platform_id
           LIMIT ?""",
        (STEAM_APP_ID, stale_before, limit),
        """UPDATE steam_platform_data
           SET store_claimed_at = ?
           WHERE game_platform_id = ?
             AND store_cached_at IS NULL
             AND (store_claimed_at IS NULL OR store_claimed_at < ?)""",
        lambda now, platform_id: (now, platform_id, stale_before),
    )


async def claim_steam_platform_ids_for_protondb(limit: int, stale_before: str) -> list[int]:
    return await _claim_ids(
        """SELECT spd.game_platform_id AS id
           FROM steam_platform_data spd
           JOIN game_platforms gp ON gp.id = spd.game_platform_id
           JOIN games g ON g.id = gp.game_id
           JOIN game_platform_identifiers gpi
             ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
           WHERE spd.store_cached_at IS NOT NULL
             AND spd.store_cached_at != 'FAILED'
             AND spd.protondb_cached_at IS NULL
             AND (spd.protondb_claimed_at IS NULL OR spd.protondb_claimed_at < ?)
             AND g.is_farmed = 0
           ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, spd.game_platform_id
           LIMIT ?""",
        (STEAM_APP_ID, stale_before, limit),
        """UPDATE steam_platform_data
           SET protondb_claimed_at = ?
           WHERE game_platform_id = ?
             AND protondb_cached_at IS NULL
             AND (protondb_claimed_at IS NULL OR protondb_claimed_at < ?)""",
        lambda now, platform_id: (now, platform_id, stale_before),
    )


async def claim_steam_platform_ids_for_steamspy(limit: int, stale_before: str) -> list[int]:
    return await _claim_ids(
        """SELECT spd.game_platform_id AS id
           FROM steam_platform_data spd
           JOIN game_platforms gp ON gp.id = spd.game_platform_id
           JOIN games g ON g.id = gp.game_id
           JOIN game_platform_identifiers gpi
             ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
           WHERE spd.store_cached_at IS NOT NULL
             AND spd.store_cached_at != 'FAILED'
             AND spd.steamspy_cached_at IS NULL
             AND (spd.steamspy_claimed_at IS NULL OR spd.steamspy_claimed_at < ?)
             AND g.is_farmed = 0
           ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, spd.game_platform_id
           LIMIT ?""",
        (STEAM_APP_ID, stale_before, limit),
        """UPDATE steam_platform_data
           SET steamspy_claimed_at = ?
           WHERE game_platform_id = ?
             AND steamspy_cached_at IS NULL
             AND (steamspy_claimed_at IS NULL OR steamspy_claimed_at < ?)""",
        lambda now, platform_id: (now, platform_id, stale_before),
    )


async def claim_game_platform_ids_for_opencritic(limit: int, stale_before: str) -> list[int]:
    async with get_db() as db:
        await db.execute(
            """INSERT OR IGNORE INTO game_platform_enrichment (game_platform_id)
               SELECT gp.id
               FROM game_platforms gp
               JOIN games g ON g.id = gp.game_id
               WHERE g.is_farmed = 0"""
        )
        await db.commit()

    return await _claim_ids(
        """SELECT gp.id AS id
           FROM game_platforms gp
           JOIN games g ON g.id = gp.game_id
           JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
           WHERE gpe.opencritic_cached_at IS NULL
             AND (gpe.opencritic_claimed_at IS NULL OR gpe.opencritic_claimed_at < ?)
             AND g.is_farmed = 0
           ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, gp.id
           LIMIT ?""",
        (stale_before, limit),
        """UPDATE game_platform_enrichment
           SET opencritic_claimed_at = ?
           WHERE game_platform_id = ?
             AND opencritic_cached_at IS NULL
             AND (opencritic_claimed_at IS NULL OR opencritic_claimed_at < ?)""",
        lambda now, platform_id: (now, platform_id, stale_before),
    )


async def claim_game_platform_ids_for_metacritic(limit: int, stale_before: str) -> list[int]:
    async with get_db() as db:
        await db.execute(
            """INSERT OR IGNORE INTO game_platform_enrichment (game_platform_id)
               SELECT gp.id
               FROM game_platforms gp
               JOIN games g ON g.id = gp.game_id
               WHERE g.is_farmed = 0"""
        )
        await db.commit()

    return await _claim_ids(
        """SELECT gp.id AS id
           FROM game_platforms gp
           JOIN games g ON g.id = gp.game_id
           JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
           WHERE gpe.metacritic_cached_at IS NULL
             AND (gpe.metacritic_claimed_at IS NULL OR gpe.metacritic_claimed_at < ?)
             AND g.is_farmed = 0
           ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, gp.id
           LIMIT ?""",
        (stale_before, limit),
        """UPDATE game_platform_enrichment
           SET metacritic_claimed_at = ?
           WHERE game_platform_id = ?
             AND metacritic_cached_at IS NULL
             AND (metacritic_claimed_at IS NULL OR metacritic_claimed_at < ?)""",
        lambda now, platform_id: (now, platform_id, stale_before),
    )


async def load_games_for_igdb_backfill(game_ids: Iterable[int]) -> list[aiosqlite.Row]:
    ids = list(dict.fromkeys(game_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT id, name, igdb_id
                FROM games
                WHERE id IN ({placeholders})
                ORDER BY id""",
            ids,
        )


async def load_store_batch_rows(platform_ids: Iterable[int]) -> list[aiosqlite.Row]:
    ids = list(dict.fromkeys(platform_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT gp.id AS game_platform_id,
                       CAST(gpi.identifier_value AS INTEGER) AS appid,
                       g.name
                FROM game_platforms gp
                JOIN games g ON g.id = gp.game_id
                JOIN game_platform_identifiers gpi
                  ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
                WHERE gp.id IN ({placeholders})
                ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, gp.id""",
            [STEAM_APP_ID, *ids],
        )


async def load_hltb_batch_rows(game_ids: Iterable[int]) -> list[aiosqlite.Row]:
    ids = list(dict.fromkeys(game_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT id AS game_id, name
                FROM games
                WHERE id IN ({placeholders})
                ORDER BY id""",
            ids,
        )


async def load_steam_platform_batch_rows(platform_ids: Iterable[int]) -> list[aiosqlite.Row]:
    ids = list(dict.fromkeys(platform_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT gp.id AS game_platform_id,
                       CAST(gpi.identifier_value AS INTEGER) AS appid,
                       g.name,
                       gp.platform
                FROM game_platforms gp
                JOIN games g ON g.id = gp.game_id
                JOIN game_platform_identifiers gpi
                  ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
                WHERE gp.id IN ({placeholders})
                ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, gp.id""",
            [STEAM_APP_ID, *ids],
        )


async def load_opencritic_batch_rows(platform_ids: Iterable[int]) -> list[aiosqlite.Row]:
    ids = list(dict.fromkeys(platform_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT gp.id AS game_platform_id, g.name
                FROM game_platforms gp
                JOIN games g ON g.id = gp.game_id
                WHERE gp.id IN ({placeholders})
                ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, gp.id""",
            ids,
        )


async def load_metacritic_batch_rows(platform_ids: Iterable[int]) -> list[aiosqlite.Row]:
    ids = list(dict.fromkeys(platform_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT gp.id AS game_platform_id, gp.platform, g.name
                FROM game_platforms gp
                JOIN games g ON g.id = gp.game_id
                WHERE gp.id IN ({placeholders})
                ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, gp.id""",
            ids,
        )


async def recompute_tag_affinity() -> int:
    """
    Recompute tag_affinity from all rated games.

    affinity_score = weighted_avg_score x log(game_count + 1)

    Backloggd weight = 1.0, Steam review weight = 0.5.
    Returns number of tags updated.
    """
    source_weights = {"backloggd": 1.0, "steam_review": 0.5}

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT r.game_id, r.source, r.normalized_score, g.tags
            FROM ratings r
            JOIN games g ON g.id = r.game_id
            WHERE g.tags IS NOT NULL AND r.normalized_score IS NOT NULL
            """
        )

    tag_data: dict[str, dict] = {}

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
                tag_data[tag_lower] = {
                    "weighted_sum": 0.0,
                    "weight_sum": 0.0,
                    "game_ids": set(),
                }
            tag_data[tag_lower]["weighted_sum"] += score * weight
            tag_data[tag_lower]["weight_sum"] += weight
            tag_data[tag_lower]["game_ids"].add(game_id)

    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        await db.execute("DELETE FROM tag_affinity")
        for tag, data in tag_data.items():
            if data["weight_sum"] == 0:
                continue
            avg_score = data["weighted_sum"] / data["weight_sum"]
            game_count = len(data["game_ids"])
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
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def set_meta_many(values: dict[str, str | None]) -> None:
    if not values:
        return

    async with get_db() as db:
        for key, value in values.items():
            await db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()


async def get_game_by_identifier(identifier_type: str, identifier_value: str) -> aiosqlite.Row | None:
    async with get_db() as db:
        return await db.execute_fetchone(
            """SELECT g.*
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id
               JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
               WHERE gpi.identifier_type = ? AND gpi.identifier_value = ?
               LIMIT 1""",
            (identifier_type, identifier_value),
        )


async def get_game_by_appid(appid: int) -> aiosqlite.Row | None:
    return await get_game_by_identifier(STEAM_APP_ID, str(appid))


async def get_game_by_igdb_id(igdb_id: int) -> aiosqlite.Row | None:
    async with get_db() as db:
        return await db.execute_fetchone(
            "SELECT * FROM games WHERE igdb_id = ?", (igdb_id,)
        )


async def get_game_by_name_exact(name: str) -> aiosqlite.Row | None:
    async with get_db() as db:
        return await db.execute_fetchone(
            "SELECT * FROM games WHERE lower(name) = lower(?) ORDER BY id LIMIT 1",
            (name,),
        )


async def get_steam_appid_for_game(game_id: int) -> int | None:
    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT gpi.identifier_value
               FROM game_platform_identifiers gpi
               JOIN game_platforms gp ON gp.id = gpi.game_platform_id
               WHERE gp.game_id = ? AND gpi.identifier_type = ?
               ORDER BY gpi.is_primary DESC, gpi.id ASC
               LIMIT 1""",
            (game_id, STEAM_APP_ID),
        )
    if row is None:
        return None
    try:
        return int(row["identifier_value"])
    except (TypeError, ValueError):
        return None


async def get_steam_platform_row_by_appid(appid: int) -> aiosqlite.Row | None:
    async with get_db() as db:
        return await db.execute_fetchone(
            """SELECT gp.id AS game_platform_id,
                      gp.game_id,
                      gp.platform,
                      gp.owned,
                      gp.playtime_minutes,
                      gp.playtime_2weeks_minutes,
                      gp.last_synced,
                      g.name,
                      g.genres,
                      g.tags,
                      g.short_description,
                      g.release_date,
                      g.hltb_main,
                      g.hltb_extra,
                      g.hltb_complete,
                      g.hltb_cached_at,
                      g.is_farmed,
                      spd.steam_review_score,
                      spd.steam_review_desc,
                      spd.protondb_tier,
                      spd.store_cached_at,
                      spd.protondb_cached_at,
                      spd.steamspy_cached_at,
                      spd.rtime_last_played,
                      spd.library_updated_at,
                      gpe.metacritic_score,
                      gpe.metacritic_url,
                      gpe.opencritic_score,
                      gpe.opencritic_tier,
                      gpe.opencritic_percent_rec,
                      gpe.platform_release_date
               FROM game_platform_identifiers gpi
               JOIN game_platforms gp ON gp.id = gpi.game_platform_id
               JOIN games g ON g.id = gp.game_id
               LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
               LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
               WHERE gpi.identifier_type = ? AND gpi.identifier_value = ?
               LIMIT 1""",
            (STEAM_APP_ID, str(appid)),
        )


async def upsert_game(
    appid: int | None,
    name: str,
    **fields,
) -> int:
    """Insert or update a canonical game row. Returns games.id."""
    async with get_db() as db:
        row = None
        if appid is not None:
            row = await db.execute_fetchone(
                """SELECT g.id
                   FROM games g
                   JOIN game_platforms gp ON gp.game_id = g.id
                   JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
                   WHERE gpi.identifier_type = ? AND gpi.identifier_value = ?
                   LIMIT 1""",
                (STEAM_APP_ID, str(appid)),
            )

        if row is None:
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE lower(name) = lower(?) ORDER BY id LIMIT 1",
                (name,),
            )

        if row is None:
            cursor = await db.execute("INSERT INTO games (name) VALUES (?)", (name,))
            game_id = cursor.lastrowid
        else:
            game_id = row["id"]

        updates = {"name": name, **fields}
        cols_sql = ", ".join(f"{column} = ?" for column in updates)
        await db.execute(
            f"UPDATE games SET {cols_sql} WHERE id = ?",
            (*updates.values(), game_id),
        )
        await db.commit()
        return game_id


async def upsert_game_platform(
    game_id: int,
    platform: str,
    playtime_minutes: int | None = None,
    playtime_2weeks_minutes: int | None = None,
    owned: int = 1,
) -> int:
    """Insert or update a game_platforms row and return its id."""
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO game_platforms
               (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id, platform) DO UPDATE SET
                   owned = excluded.owned,
                   playtime_minutes = COALESCE(excluded.playtime_minutes, game_platforms.playtime_minutes),
                   playtime_2weeks_minutes = COALESCE(
                       excluded.playtime_2weeks_minutes,
                       game_platforms.playtime_2weeks_minutes
                   ),
                   last_synced = excluded.last_synced""",
            (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, now),
        )
        row = await db.execute_fetchone(
            "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
            (game_id, platform),
        )
        await db.commit()
        return row["id"]


async def upsert_game_platform_identifier(
    game_platform_id: int,
    identifier_type: str,
    identifier_value: str | int,
    *,
    is_primary: bool = True,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO game_platform_identifiers
               (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                   game_platform_id = excluded.game_platform_id,
                   is_primary = excluded.is_primary,
                   last_seen_at = excluded.last_seen_at""",
            (game_platform_id, identifier_type, str(identifier_value), int(is_primary), now),
        )
        await db.commit()


async def upsert_steam_platform_data(game_platform_id: int, **fields) -> None:
    if not fields:
        return

    columns = ", ".join(["game_platform_id", *fields.keys()])
    placeholders = ", ".join("?" for _ in range(len(fields) + 1))
    updates = ", ".join(f"{column} = excluded.{column}" for column in fields)
    async with get_db() as db:
        await db.execute(
            f"""INSERT INTO steam_platform_data ({columns})
                VALUES ({placeholders})
                ON CONFLICT(game_platform_id) DO UPDATE SET {updates}""",
            (game_platform_id, *fields.values()),
        )
        await db.commit()


async def bulk_upsert_steam_library(
    rows: list[dict],
    synced_at: str,
    chunk_size: int = 250,
) -> int:
    if not rows:
        return 0

    async with get_db() as db:
        await db.execute(
            """CREATE TEMP TABLE IF NOT EXISTS temp_steam_library_sync (
                   appid INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   playtime_minutes INTEGER,
                   playtime_2weeks_minutes INTEGER,
                   rtime_last_played INTEGER,
                   row_order INTEGER NOT NULL
               )"""
        )

        row_offset = 0
        for chunk in _iter_chunks(rows, chunk_size):
            await db.execute("DELETE FROM temp_steam_library_sync")
            await db.executemany(
                """INSERT INTO temp_steam_library_sync
                   (appid, name, playtime_minutes, playtime_2weeks_minutes, rtime_last_played, row_order)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(appid) DO UPDATE SET
                       name = excluded.name,
                       playtime_minutes = excluded.playtime_minutes,
                       playtime_2weeks_minutes = excluded.playtime_2weeks_minutes,
                       rtime_last_played = excluded.rtime_last_played,
                       row_order = excluded.row_order""",
                [
                    (
                        row["appid"],
                        row["name"],
                        row.get("playtime_minutes"),
                        row.get("playtime_2weeks_minutes"),
                        row.get("rtime_last_played"),
                        row_offset + index,
                    )
                    for index, row in enumerate(chunk)
                ],
            )
            row_offset += len(chunk)

            await db.execute(
                """INSERT INTO games (name)
                   SELECT MIN(t.name)
                   FROM temp_steam_library_sync t
                   WHERE NOT EXISTS (
                       SELECT 1
                       FROM game_platform_identifiers gpi
                       JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                       WHERE gpi.identifier_type = ?
                         AND gpi.identifier_value = CAST(t.appid AS TEXT)
                   )
                     AND NOT EXISTS (
                       SELECT 1
                       FROM games g
                       WHERE lower(g.name) = lower(t.name)
                   )
                   GROUP BY lower(t.name)""",
                (STEAM_APP_ID,),
            )

            await db.execute(
                """WITH resolved AS (
                       SELECT t.appid,
                              t.name,
                              t.row_order,
                              COALESCE(
                                  (
                                      SELECT gp.game_id
                                      FROM game_platform_identifiers gpi
                                      JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                                      WHERE gpi.identifier_type = ?
                                        AND gpi.identifier_value = CAST(t.appid AS TEXT)
                                      LIMIT 1
                                  ),
                                  (
                                      SELECT g.id
                                      FROM games g
                                      WHERE lower(g.name) = lower(t.name)
                                      ORDER BY g.id
                                      LIMIT 1
                                  )
                              ) AS game_id
                       FROM temp_steam_library_sync t
                   )
                   UPDATE games
                   SET name = (
                       SELECT resolved.name
                       FROM resolved
                       WHERE resolved.game_id = games.id
                       ORDER BY resolved.row_order DESC
                       LIMIT 1
                   )
                   WHERE id IN (
                       SELECT game_id
                       FROM resolved
                       WHERE game_id IS NOT NULL
                   )""",
                (STEAM_APP_ID,),
            )

            await db.execute(
                """WITH resolved AS (
                       SELECT t.appid,
                              t.name,
                              t.playtime_minutes,
                              t.playtime_2weeks_minutes,
                              COALESCE(
                                  (
                                      SELECT gp.game_id
                                      FROM game_platform_identifiers gpi
                                      JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                                      WHERE gpi.identifier_type = ?
                                        AND gpi.identifier_value = CAST(t.appid AS TEXT)
                                      LIMIT 1
                                  ),
                                  (
                                      SELECT g.id
                                      FROM games g
                                      WHERE lower(g.name) = lower(t.name)
                                      ORDER BY g.id
                                      LIMIT 1
                                  )
                              ) AS game_id
                       FROM temp_steam_library_sync t
                   )
                   INSERT INTO game_platforms
                   (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
                   SELECT resolved.game_id, ?, 1,
                          resolved.playtime_minutes,
                          resolved.playtime_2weeks_minutes,
                          ?
                   FROM resolved
                   WHERE resolved.game_id IS NOT NULL
                   ON CONFLICT(game_id, platform) DO UPDATE SET
                       owned = excluded.owned,
                       playtime_minutes = COALESCE(
                           excluded.playtime_minutes,
                           game_platforms.playtime_minutes
                       ),
                       playtime_2weeks_minutes = COALESCE(
                           excluded.playtime_2weeks_minutes,
                           game_platforms.playtime_2weeks_minutes
                       ),
                       last_synced = excluded.last_synced""",
                (STEAM_APP_ID, STEAM_PLATFORM, synced_at),
            )

            await db.execute(
                """WITH resolved AS (
                       SELECT t.appid,
                              t.name,
                              COALESCE(
                                  (
                                      SELECT gp.game_id
                                      FROM game_platform_identifiers gpi
                                      JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                                      WHERE gpi.identifier_type = ?
                                        AND gpi.identifier_value = CAST(t.appid AS TEXT)
                                      LIMIT 1
                                  ),
                                  (
                                      SELECT g.id
                                      FROM games g
                                      WHERE lower(g.name) = lower(t.name)
                                      ORDER BY g.id
                                      LIMIT 1
                                  )
                              ) AS game_id
                       FROM temp_steam_library_sync t
                   )
                   INSERT INTO game_platform_identifiers
                   (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at)
                   SELECT gp.id, ?, CAST(resolved.appid AS TEXT), 1, ?
                   FROM resolved
                   JOIN game_platforms gp
                     ON gp.game_id = resolved.game_id AND gp.platform = ?
                   WHERE resolved.game_id IS NOT NULL
                   ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                       game_platform_id = excluded.game_platform_id,
                       is_primary = excluded.is_primary,
                       last_seen_at = excluded.last_seen_at""",
                (STEAM_APP_ID, STEAM_APP_ID, synced_at, STEAM_PLATFORM),
            )

            await db.execute(
                """WITH resolved AS (
                       SELECT t.appid,
                              t.name,
                              t.rtime_last_played,
                              COALESCE(
                                  (
                                      SELECT gp.game_id
                                      FROM game_platform_identifiers gpi
                                      JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                                      WHERE gpi.identifier_type = ?
                                        AND gpi.identifier_value = CAST(t.appid AS TEXT)
                                      LIMIT 1
                                  ),
                                  (
                                      SELECT g.id
                                      FROM games g
                                      WHERE lower(g.name) = lower(t.name)
                                      ORDER BY g.id
                                      LIMIT 1
                                  )
                              ) AS game_id
                       FROM temp_steam_library_sync t
                   )
                   INSERT INTO steam_platform_data
                   (game_platform_id, rtime_last_played, library_updated_at)
                   SELECT gp.id, resolved.rtime_last_played, ?
                   FROM resolved
                   JOIN game_platforms gp
                     ON gp.game_id = resolved.game_id AND gp.platform = ?
                   WHERE resolved.game_id IS NOT NULL
                   ON CONFLICT(game_platform_id) DO UPDATE SET
                       rtime_last_played = excluded.rtime_last_played,
                       library_updated_at = excluded.library_updated_at""",
                (STEAM_APP_ID, synced_at, STEAM_PLATFORM),
            )

            await db.commit()

        await db.execute("DROP TABLE IF EXISTS temp_steam_library_sync")
        await db.commit()

    return len(rows)


async def upsert_game_platform_enrichment(game_platform_id: int, **fields) -> None:
    if not fields:
        return
    columns = ", ".join(["game_platform_id", *fields.keys()])
    placeholders = ", ".join("?" for _ in range(len(fields) + 1))
    updates = ", ".join(f"{column} = excluded.{column}" for column in fields)
    async with get_db() as db:
        await db.execute(
            f"""INSERT INTO game_platform_enrichment ({columns})
                VALUES ({placeholders})
                ON CONFLICT(game_platform_id) DO UPDATE SET {updates}""",
            (game_platform_id, *fields.values()),
        )
        await db.commit()


async def load_fuzzy_candidates() -> dict[int, str]:
    """Load all game id->name pairs for use with find_game_by_name_fuzzy."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id, name FROM games")
    return {row["id"]: row["name"] for row in rows}


async def find_game_by_name_fuzzy(
    name: str,
    cutoff: int = 85,
    candidates: dict[int, str] | None = None,
) -> aiosqlite.Row | None:
    """Return the best-matching games row for a given title, or None if below cutoff."""
    if candidates is None:
        candidates = await load_fuzzy_candidates()

    best_id = extract_best_fuzzy_key(name, candidates, cutoff=cutoff)
    if best_id is None:
        return None

    async with get_db() as db:
        return await db.execute_fetchone("SELECT * FROM games WHERE id = ?", (best_id,))


def _coerce_identifier_value(identifier_type: str, identifier_value: str) -> str | int:
    if identifier_type in {STEAM_APP_ID, GOG_PRODUCT_ID}:
        try:
            return int(identifier_value)
        except ValueError:
            return identifier_value
    return identifier_value


def _platform_dict(row: aiosqlite.Row) -> dict:
    playtime_minutes = row["playtime_minutes"]
    playtime_2weeks_minutes = row["playtime_2weeks_minutes"]
    platform = {
        "game_platform_id": row["game_platform_id"],
        "platform": row["platform"],
        "owned": bool(row["owned"]),
        "playtime_minutes": playtime_minutes,
        "playtime_hours": round((playtime_minutes or 0) / 60, 1),
        "playtime_2weeks_minutes": playtime_2weeks_minutes,
        "playtime_2weeks_hours": round((playtime_2weeks_minutes or 0) / 60, 1),
        "last_synced": row["last_synced"],
        "identifiers": {},
        "provider_data": {},
        "platform_release_date": row["platform_release_date"],
        "metacritic_score": row["metacritic_score"],
        "metacritic_url": row["metacritic_url"],
        "opencritic_score": row["opencritic_score"],
        "opencritic_tier": row["opencritic_tier"],
        "opencritic_percent_rec": row["opencritic_percent_rec"],
    }

    if row["platform"] == STEAM_PLATFORM:
        last_played = row["rtime_last_played"]
        platform["provider_data"] = {
            "steam_review_score": row["steam_review_score"],
            "steam_review_desc": row["steam_review_desc"],
            "protondb_tier": row["protondb_tier"],
            "last_played_date": (
                datetime.fromtimestamp(last_played, tz=timezone.utc).date().isoformat()
                if last_played
                else None
            ),
            "library_updated_at": row["library_updated_at"],
        }

    return platform


async def load_platforms_for_games(game_ids: Iterable[int]) -> dict[int, list[dict]]:
    """Load platform rows, identifiers, and provider-specific data for many games."""
    ids = list(dict.fromkeys(game_ids))
    if not ids:
        return {}

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT gp.id AS game_platform_id,
                       gp.game_id,
                       gp.platform,
                       gp.owned,
                       gp.playtime_minutes,
                       gp.playtime_2weeks_minutes,
                       gp.last_synced,
                       gpi.identifier_type,
                       gpi.identifier_value,
                       gpi.is_primary,
                       spd.steam_review_score,
                       spd.steam_review_desc,
                       spd.protondb_tier,
                       spd.rtime_last_played,
                       spd.library_updated_at,
                       gpe.platform_release_date,
                       gpe.metacritic_score,
                       gpe.metacritic_url,
                       gpe.opencritic_score,
                       gpe.opencritic_tier,
                       gpe.opencritic_percent_rec
                FROM game_platforms gp
                LEFT JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
                LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
                LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
                WHERE gp.game_id IN ({placeholders})
                ORDER BY gp.game_id, gp.platform, gp.id, gpi.is_primary DESC, gpi.identifier_type""",
            ids,
        )

    by_game: dict[int, list[dict]] = defaultdict(list)
    by_platform_id: dict[int, dict] = {}
    for row in rows:
        game_id = row["game_id"]
        platform_id = row["game_platform_id"]
        platform = by_platform_id.get(platform_id)
        if platform is None:
            platform = _platform_dict(row)
            by_platform_id[platform_id] = platform
            by_game[game_id].append(platform)

        identifier_type = row["identifier_type"]
        identifier_value = row["identifier_value"]
        if identifier_type and identifier_value:
            platform["identifiers"][identifier_type] = _coerce_identifier_value(
                identifier_type,
                identifier_value,
            )

    for platforms in by_game.values():
        platforms.sort(key=lambda item: item["platform"])

    return dict(by_game)
