"""SQLite data layer — package facade.

This module holds the bottom layer (connection management, schema detection,
the migration chain, init, and the mutable readiness globals) and re-exports the
domain submodules so ``gamelib_mcp.data.db.<name>`` remains the single stable
import surface for all consumers. Submodules: schema (DDL), claims (row-claiming
+ batch loaders), queries (meta KV, lookups, platform assembly), upserts,
affinity (tag-affinity recompute), fuzzy (name matching). The submodule
re-exports sit at the end of this file so the bottom layer is fully defined
before each leaf does ``from . import get_db, ...``.
"""

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
from pathlib import Path
from typing import Callable, Iterable, TypeVar

import aiosqlite

from gamelib_mcp.env import load_project_dotenv


# Polyfill: aiosqlite <0.20 doesn't have execute_fetchone as a Connection method
async def _execute_fetchone(self, sql, parameters=()):
    async with self.execute(sql, parameters) as cursor:
        return await cursor.fetchone()


if not hasattr(aiosqlite.Connection, "execute_fetchone"):
    aiosqlite.Connection.execute_fetchone = _execute_fetchone  # type: ignore[method-assign]


_DB_READY_PATH: str | None = None
_DB_INIT_LOCK: asyncio.Lock | None = None
_ENV_LOADED = False
_FuzzyKey = TypeVar("_FuzzyKey")
_Progress = Callable[[str], None]
_SQLITE_CONNECT_TIMEOUT_SECONDS = 30.0
_SQLITE_BUSY_TIMEOUT_MS = 30_000

STEAM_PLATFORM = "steam"
STEAM_APP_ID = "steam_appid"
EPIC_ARTIFACT_ID = "epic_artifact_id"
GOG_PRODUCT_ID = "gog_product_id"
SCHEMA_VERSION = 8


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
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_project_dotenv(Path(__file__).resolve().parents[2] / ".env")
        _ENV_LOADED = True

    configured = os.getenv("DATABASE_URL")
    if configured:
        return configured.removeprefix("file:")

    return "data/gamelib.db"


def _ensure_db_parent_dir(db_path: str) -> None:
    if not db_path or db_path == ":memory:":
        return

    parent = Path(db_path).expanduser().parent
    if str(parent) in ("", "."):
        return

    parent.mkdir(parents=True, exist_ok=True)


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


from .schema import (
    _V1_SCHEMA_DDL,
    _V2_SCHEMA_DDL,
    _V3_SCHEMA_DDL,
    _V4_SCHEMA_DDL,
    _V5_SCHEMA_DDL,
    _V6_SCHEMA_DDL,
    _V7_SCHEMA_DDL,
    _V8_SCHEMA_DDL,
)


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
    if {"name_normalized", "features"}.issubset(game_cols) and {
        "opencritic_url",
        "opencritic_num_reviews",
    }.issubset(gpe_cols):
        return "v8"

    if "name_normalized" in game_cols and {
        "opencritic_url",
        "opencritic_num_reviews",
    }.issubset(gpe_cols):
        return "v7"

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
    await db.execute("PRAGMA legacy_alter_table=ON")
    await db.execute("ALTER TABLE games RENAME TO games_v2_old")
    await db.execute("PRAGMA legacy_alter_table=OFF")
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


async def _migrate_v5_to_v6(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Data-only migration: clean enrichment values written by older buggy scrapers.

    1. Metacritic: a prior scraper stored the 0-10 user score as if it were the
       0-100 Metascore. The contaminated rows are exactly the int-truncated user
       scores (<= 10); legitimate critic Metascores effectively never fall that
       low. Null the score *and* its cache/claim timestamps so the fixed scraper
       re-fetches the real Metascore.
    2. HLTB: a prior fetch stored 0 for durations HowLongToBeat has no data for.
       0 means "unknown", so normalize to NULL in place (no re-scrape needed —
       re-fetching would just return 0 again).
    """
    if progress is not None:
        progress("Migrating to v6: clean Metacritic user-score and HLTB zero contamination.")

    await db.execute(
        """UPDATE game_platform_enrichment
              SET metacritic_score = NULL,
                  metacritic_url = NULL,
                  metacritic_cached_at = NULL,
                  metacritic_claimed_at = NULL
            WHERE metacritic_score IS NOT NULL
              AND metacritic_score <= 10"""
    )
    await db.execute("UPDATE games SET hltb_main = NULL WHERE hltb_main = 0")
    await db.execute("UPDATE games SET hltb_extra = NULL WHERE hltb_extra = 0")
    await db.execute("UPDATE games SET hltb_complete = NULL WHERE hltb_complete = 0")

    await _set_user_version(db, 6)
    await db.commit()


async def _backfill_name_normalized(db: aiosqlite.Connection) -> int:
    """Populate games.name_normalized wherever it is NULL. Returns rows updated."""
    from ..title_normalization import normalize_search_text

    rows = await db.execute_fetchall(
        "SELECT id, name FROM games WHERE name_normalized IS NULL"
    )
    for row in rows:
        await db.execute(
            "UPDATE games SET name_normalized = ? WHERE id = ?",
            (normalize_search_text(row["name"]), row["id"]),
        )
    return len(rows)


async def _migrate_v6_to_v7(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add games.name_normalized (search matching column) and backfill it."""
    if progress is not None:
        progress("Migrating to v7: add and backfill games.name_normalized.")

    db.row_factory = aiosqlite.Row
    game_cols = await _table_columns(db, "games")
    if "name_normalized" not in game_cols:
        await db.execute("ALTER TABLE games ADD COLUMN name_normalized TEXT")
    await _backfill_name_normalized(db)

    await _set_user_version(db, 7)
    await db.commit()


async def _migrate_v7_to_v8(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Split storefront feature flags out of games.tags into games.features.

    Pre-split rows mixed Steam categories like "Steam Trading Cards" into tags,
    polluting tag_affinity and vibe matching. For games left with NO real tags
    (only feature flags), clear the steam store/steamspy cache stamps so
    background enrichment re-fetches genuine tags.
    """
    from ..tags import STEAM_FEATURE_FLAGS, split_features

    if progress is not None:
        progress("Migrating to v8: quarantine feature flags from games.tags.")

    db.row_factory = aiosqlite.Row
    game_cols = await _table_columns(db, "games")
    if "features" not in game_cols:
        await db.execute("ALTER TABLE games ADD COLUMN features TEXT")

    rows = await db.execute_fetchall(
        "SELECT id, tags FROM games WHERE tags IS NOT NULL"
    )
    emptied_game_ids: list[int] = []
    for row in rows:
        try:
            tags = json.loads(row["tags"])
        except (ValueError, TypeError):
            continue
        if not isinstance(tags, list):
            continue
        real_tags, features = split_features(tags)
        if not features:
            continue
        await db.execute(
            "UPDATE games SET tags = ?, features = ? WHERE id = ?",
            (json.dumps(real_tags), json.dumps(features), row["id"]),
        )
        if not real_tags:
            emptied_game_ids.append(row["id"])

    for game_id in emptied_game_ids:
        await db.execute(
            """UPDATE steam_platform_data
                  SET store_cached_at = NULL, steamspy_cached_at = NULL
                WHERE game_platform_id IN (
                    SELECT id FROM game_platforms
                    WHERE game_id = ? AND platform = ?
                )""",
            (game_id, STEAM_PLATFORM),
        )

    flag_placeholders = ",".join("?" * len(STEAM_FEATURE_FLAGS))
    await db.execute(
        f"DELETE FROM tag_affinity WHERE lower(tag) IN ({flag_placeholders})",
        tuple(STEAM_FEATURE_FLAGS),
    )

    await _set_user_version(db, 8)
    await db.commit()


async def _repair_identifier_primary_flags(db: aiosqlite.Connection) -> None:
    # Only fix groups that have MORE THAN ONE primary row; leave zero-primary and
    # single-primary groups untouched.
    await db.execute(
        """
        UPDATE game_platform_identifiers
        SET is_primary = CASE
            WHEN id = (
                SELECT MIN(id)
                FROM game_platform_identifiers g2
                WHERE g2.game_platform_id = game_platform_identifiers.game_platform_id
                  AND g2.identifier_type = game_platform_identifiers.identifier_type
            ) THEN 1
            ELSE 0
        END
        WHERE game_platform_id IN (
            SELECT game_platform_id
            FROM game_platform_identifiers
            GROUP BY game_platform_id, identifier_type
            HAVING SUM(is_primary) > 1
        )
        """
    )


async def _foreign_key_targets(db: aiosqlite.Connection, table: str) -> set[str]:
    rows = await db.execute_fetchall(f"PRAGMA foreign_key_list({table})")
    return {row[2] for row in rows}


async def _rebuild_table_from_current_schema(db: aiosqlite.Connection, table: str) -> None:
    old_table = f"{table}_fk_repair_old"
    await db.execute(f"DROP TABLE IF EXISTS {old_table}")
    await db.execute("PRAGMA legacy_alter_table=ON")
    await db.execute(f"ALTER TABLE {table} RENAME TO {old_table}")
    await db.execute("PRAGMA legacy_alter_table=OFF")
    await db.executescript(_V8_SCHEMA_DDL)

    old_cols = await _table_columns(db, old_table)
    new_cols = await _table_columns(db, table)
    keep = [col for col in new_cols if col in old_cols]
    cols_sql = ", ".join(keep)
    await db.execute(f"INSERT INTO {table} ({cols_sql}) SELECT {cols_sql} FROM {old_table}")
    await db.execute(f"DROP TABLE IF EXISTS {old_table}")


async def _repair_game_foreign_keys(db: aiosqlite.Connection) -> None:
    tables = await _table_names(db)
    stale_tables = [
        table
        for table in ("game_platforms", "ratings")
        if table in tables and "games_v2_old" in await _foreign_key_targets(db, table)
    ]
    if not stale_tables:
        return

    await db.commit()
    await db.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in stale_tables:
            await _rebuild_table_from_current_schema(db, table)
        await db.commit()
    finally:
        await db.execute("PRAGMA legacy_alter_table=OFF")
        await db.execute("PRAGMA foreign_keys=ON")


async def _run_migrations(
    db: aiosqlite.Connection,
    progress: _Progress | None = None,
) -> MigrationResult:
    detected_state = await _detect_schema_state(db)
    initial_version = await _get_user_version(db)
    version = initial_version
    applied_steps: list[str] = []

    if detected_state == "fresh":
        await db.executescript(_V8_SCHEMA_DDL)
        await _set_user_version(db, SCHEMA_VERSION)
        await db.commit()
        _emit(progress, "Initialized fresh database at schema v8.", applied_steps)
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
        elif detected_state == "v7":
            await _set_user_version(db, 7)
            await db.commit()
            version = 7
            _emit(progress, "Recorded existing schema as v7.", applied_steps)
        elif detected_state == "v8":
            await _set_user_version(db, 8)
            await db.commit()
            version = 8
            _emit(progress, "Recorded existing schema as v8.", applied_steps)

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

    if version == 5:
        _emit(progress, "Applying migration step v5 -> v6.", applied_steps)
        await _migrate_v5_to_v6(db, progress=None)
        version = 6

    if version == 6:
        _emit(progress, "Applying migration step v6 -> v7.", applied_steps)
        await _migrate_v6_to_v7(db, progress=None)
        version = 7

    if version == 7:
        _emit(progress, "Applying migration step v7 -> v8.", applied_steps)
        await _migrate_v7_to_v8(db, progress=None)
        version = 8

    await _repair_game_foreign_keys(db)
    await db.execute("DROP INDEX IF EXISTS idx_game_platform_identifiers_lookup")
    await _repair_identifier_primary_flags(db)
    await db.executescript(_V8_SCHEMA_DDL)
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
    _ensure_db_parent_dir(db_path)
    async with aiosqlite.connect(db_path, timeout=_SQLITE_CONNECT_TIMEOUT_SECONDS) as conn:
        await _configure_connection(conn, enable_wal=_DB_READY_PATH != db_path)
        await _ensure_db_initialized(conn)
        yield conn


async def migrate_db(progress: _Progress | None = None) -> MigrationResult:
    """Run all schema migrations against the configured DB path."""
    global _DB_READY_PATH

    db_path = _db_path()
    _ensure_db_parent_dir(db_path)
    async with aiosqlite.connect(db_path, timeout=_SQLITE_CONNECT_TIMEOUT_SECONDS) as db:
        await _configure_connection(db, enable_wal=True)
        result = await _run_migrations(db, progress=progress)
        _DB_READY_PATH = db_path
        return result


async def init_db() -> None:
    """Create tables if they don't exist and migrate to the latest schema."""
    await migrate_db()


# ── Domain submodules (re-exported; imported last so the bottom layer above is
# fully defined before each leaf does `from . import get_db, ...`). ───────────
from .affinity import recompute_tag_affinity  # noqa: E402
from .claims import (  # noqa: E402
    _claim_cutoff_iso,
    _claim_ids,
    clear_claim,
    clear_all_enrichment_claims,
    release_game_claim,
    claim_game_ids_for_igdb,
    claim_game_ids_for_hltb,
    claim_steam_platform_ids_for_store,
    claim_steam_platform_ids_for_protondb,
    claim_steam_platform_ids_for_steamspy,
    claim_game_platform_ids_for_opencritic,
    claim_game_platform_ids_for_metacritic,
    load_games_for_igdb_backfill,
    load_store_batch_rows,
    load_hltb_batch_rows,
    load_steam_platform_batch_rows,
    load_opencritic_batch_rows,
    load_metacritic_batch_rows,
)
from .queries import (  # noqa: E402
    get_meta,
    get_meta_prefix,
    set_meta,
    set_meta_many,
    get_game_by_identifier,
    get_game_by_appid,
    get_game_by_igdb_id,
    get_game_by_name_exact,
    get_steam_appid_for_game,
    get_steam_platform_row_by_appid,
    _coerce_identifier_value,
    _platform_dict,
    load_platforms_for_games,
)
from .upserts import (  # noqa: E402
    upsert_game,
    upsert_game_platform,
    repair_misclassified_platform_row,
    upsert_game_platform_identifier,
    upsert_steam_platform_data,
    bulk_upsert_steam_library,
    upsert_game_platform_enrichment,
)
from .fuzzy import (  # noqa: E402
    load_fuzzy_candidates,
    find_game_by_name_fuzzy,
    find_conflicting_fuzzy_key,
)
