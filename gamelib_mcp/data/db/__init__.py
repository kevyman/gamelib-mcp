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
import functools
import json
import logging
import math
import os
import re
import sqlite3
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import TypeVar
from weakref import WeakKeyDictionary

import aiosqlite

from gamelib_mcp.env import load_project_dotenv


# Polyfill: aiosqlite <0.20 doesn't have execute_fetchone as a Connection method
async def _execute_fetchone(self, sql, parameters=()):
    async with self.execute(sql, parameters) as cursor:
        return await cursor.fetchone()


if not hasattr(aiosqlite.Connection, "execute_fetchone"):
    aiosqlite.Connection.execute_fetchone = _execute_fetchone  # type: ignore[attr-defined]


logger = logging.getLogger(__name__)

_DB_READY_PATH: str | None = None
_FTS_READY_PATH: str | None = None
_DB_INIT_LOCK: asyncio.Lock | None = None
_ENV_LOADED = False
_FuzzyKey = TypeVar("_FuzzyKey")
_Progress = Callable[[str], None]
_SQLITE_CONNECT_TIMEOUT_SECONDS = 30.0
_SQLITE_BUSY_TIMEOUT_MS = 30_000
_REQUIRE_ABSOLUTE_DB_PATH_ENV = "GAMELIB_REQUIRE_ABSOLUTE_DB_PATH"

# ── Opt-in connection pool ────────────────────────────────────────────────────
# get_db() defaults to connection-per-call (each aiosqlite connection is a
# worker thread). The server lifespan enables pooling for its process; tests
# stay per-call unless they opt in, because pooled threads have no loop-close
# hook to die on. Checkout is exclusive: a pooled connection is never shared
# between concurrent coroutines, so per-call transaction semantics are
# unchanged.
_POOL_ENABLED = False
_POOL_MAX_IDLE = 4
_POOL_IDLE: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, list[aiosqlite.Connection]]
] = WeakKeyDictionary()


def enable_db_pooling() -> None:
    """Reuse SQLite connections across get_db() calls on the current process."""
    global _POOL_ENABLED
    _POOL_ENABLED = True


async def close_db_pool() -> None:
    """Disable pooling and close idle connections owned by the current loop."""
    global _POOL_ENABLED
    _POOL_ENABLED = False
    loop = asyncio.get_running_loop()
    by_path = _POOL_IDLE.pop(loop, None) or {}
    for conns in by_path.values():
        for conn in conns:
            await conn.close()


def _pool_checkout(db_path: str) -> "aiosqlite.Connection | None":
    loop = asyncio.get_running_loop()
    conns = _POOL_IDLE.get(loop, {}).get(db_path)
    if conns:
        return conns.pop()
    return None


async def _pool_checkin(db_path: str, conn: aiosqlite.Connection) -> None:
    loop = asyncio.get_running_loop()
    conns = _POOL_IDLE.setdefault(loop, {}).setdefault(db_path, [])
    if _POOL_ENABLED and len(conns) < _POOL_MAX_IDLE:
        conns.append(conn)
    else:
        await conn.close()


STEAM_PLATFORM = "steam"
STEAM_APP_ID = "steam_appid"
EPIC_ARTIFACT_ID = "epic_artifact_id"
GOG_PRODUCT_ID = "gog_product_id"
XBOX_TITLE_ID = "xbox_title_id"
# Kept as a literal (not imported from data/nintendo.py::NINTENDO_TITLE_ID) to
# avoid a db -> nintendo import cycle: nintendo.py imports this package at
# module load time, so this package must never import back from nintendo.py.
# Must stay in sync with that constant's value.
NINTENDO_TITLE_ID_TYPE = "nintendo_title_id"
SCHEMA_VERSION = 39


def normalize_identifier_value(identifier_type: str, value: str) -> str:
    """Canonicalize an identifier value the same way at every write and lookup.

    Nintendo title ids are the one identifier type with a known case mismatch
    across sources: VGCS (ownership) stores them verbatim from the console's
    catalog while the Parental Controls API (playtime) reports uppercase hex
    for the same title — so the same game could accumulate a lowercase
    game_platform_identifiers row and an uppercase nintendo_play_summary row.
    Normalizing both to uppercase here, called from every write chokepoint
    (upsert_game_platform_identifier, upsert_nintendo_play_summary) and lookup
    chokepoint (get_game_by_identifier, get_nintendo_synced_minutes, ...),
    means every join/comparison between them can be plain equality — no
    UPPER(x) = UPPER(y) duct tape at read time. Every other identifier_type
    (steam_appid, gog_product_id, xbox_title_id, epic_artifact_id, ...) passes
    through unchanged. Also safe to call directly with NINTENDO_TITLE_ID_TYPE
    to normalize a nintendo_play_summary.application_id value — it's the same
    value space as a nintendo_title_id identifier, just a sibling table.
    """
    if identifier_type == NINTENDO_TITLE_ID_TYPE and value is not None:
        return value.strip().upper()
    return value


@dataclass
class MigrationResult:
    initial_version: int
    final_version: int
    detected_state: str
    applied_steps: list[str]
    fts_enabled: bool = False

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
        db_path = configured.removeprefix("file:")
    elif os.getenv(_REQUIRE_ABSOLUTE_DB_PATH_ENV):
        db_path = "/data/gamelib.db"
    else:
        db_path = "data/gamelib.db"

    if (
        os.getenv(_REQUIRE_ABSOLUTE_DB_PATH_ENV)
        and db_path != ":memory:"
        and not Path(db_path).expanduser().is_absolute()
    ):
        raise RuntimeError(
            f"DATABASE_URL must resolve to an absolute SQLite path when "
            f"{_REQUIRE_ABSOLUTE_DB_PATH_ENV} is set; got {db_path!r}"
        )

    return db_path


def default_data_dir() -> Path:
    """Writable directory for app-managed state files (session cookies, tokens).

    Derives from the configured database location so these files land in the
    same writable place as the DB — the mounted ``/data`` volume in production,
    ``data/`` in local dev — rather than a hardcoded relative ``data/`` that,
    under the container's root-owned ``/app`` cwd, the non-root process cannot
    create (``PermissionError: [Errno 13] Permission denied: 'data'``).
    """
    db_path = _db_path()
    if db_path != ":memory:":
        parent = Path(db_path).expanduser().parent
        if str(parent) not in ("", "."):
            return parent
    return Path("/data") if os.getenv(_REQUIRE_ABSOLUTE_DB_PATH_ENV) else Path("data")


def fts_ready() -> bool:
    """True when the configured database has a live games_fts index."""
    return _FTS_READY_PATH is not None and _FTS_READY_PATH == _db_path()


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
    _FTS_DDL,
    _QUERY_VIEWS_DDL,
    _V1_SCHEMA_DDL,
    _V2_SCHEMA_DDL,
    _V3_SCHEMA_DDL,
    _V4_SCHEMA_DDL,
    _V5_SCHEMA_DDL,
    _V6_SCHEMA_DDL,
    _V7_SCHEMA_DDL,
    _V8_SCHEMA_DDL,
    _V9_SCHEMA_DDL,
    _V10_SCHEMA_DDL,
    _V11_SCHEMA_DDL,
    _V12_SCHEMA_DDL,
    _V16_SCHEMA_DDL,
    _V17_SCHEMA_DDL,
    _V18_SCHEMA_DDL,
    _V19_SCHEMA_DDL,
    _V20_SCHEMA_DDL,
    _V21_SCHEMA_DDL,
    _V22_SCHEMA_DDL,
    _V25_SCHEMA_DDL,
    _V29_SCHEMA_DDL,
    _V31_SCHEMA_DDL,
    _V32_SCHEMA_DDL,
    _V34_SCHEMA_DDL,
    _V36_SCHEMA_DDL,
    _V37_SCHEMA_DDL,
    _V38_SCHEMA_DDL,
    _V39_SCHEMA_DDL,
)


async def _table_names(db: aiosqlite.Connection) -> set[str]:
    rows = await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in rows}


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
    return {row[1] for row in rows}


async def _get_user_version(db: aiosqlite.Connection) -> int:
    row = await db.execute_fetchone("PRAGMA user_version")  # type: ignore[attr-defined]
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
        "content_type",
        "parent_game_id",
        "is_primary_library_item",
    }.issubset(game_cols) and "game_aliases" in tables and "game_series" in tables:
        if "nintendo_play_summary" in tables:
            return "v12"
        return "v11"
    if {"name_normalized", "features", "manual_overrides"}.issubset(game_cols) and {
        "opencritic_url",
        "opencritic_num_reviews",
    }.issubset(gpe_cols):
        if "game_series" in tables:
            return "v10"
        return "v9"

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
        game = await db.execute_fetchone(  # type: ignore[attr-defined]
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
            game = await db.execute_fetchone(  # type: ignore[attr-defined]
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
    now = datetime.now(UTC).isoformat()
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

    rows = list(await db.execute_fetchall(
        "SELECT id, name FROM games WHERE name_normalized IS NULL"
    ))
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


async def _migrate_v8_to_v9(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add games.manual_overrides (JSON array of tool-protected column names)."""
    if progress is not None:
        progress("Migrating to v9: add games.manual_overrides.")

    game_cols = await _table_columns(db, "games")
    if "manual_overrides" not in game_cols:
        await db.execute("ALTER TABLE games ADD COLUMN manual_overrides TEXT")

    await _set_user_version(db, 9)
    await db.commit()


async def _migrate_v9_to_v10(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add normalized series tables (IGDB collections + franchises)."""
    if progress is not None:
        progress("Migrating to v10: add game_series + game_series_membership.")

    await db.executescript(_V10_SCHEMA_DDL)
    # Requeue already-matched games for IGDB so the background worker re-fetches
    # them and backfills series. Without this, games enriched before v10 keep a
    # non-null igdb_cached_at and claim_game_ids_for_igdb never revisits them, so
    # game_series_membership would stay empty for the existing library. Safe and
    # one-time: _apply_igdb_metadata only writes columns that are currently NULL
    # and not manually overridden, so genres/tags/release_date are preserved.
    await db.execute(
        "UPDATE games SET igdb_cached_at = NULL, igdb_claimed_at = NULL "
        "WHERE igdb_id IS NOT NULL"
    )
    await _set_user_version(db, 10)
    await db.commit()


async def _migrate_v10_to_v11(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add DLC/expansion content relationships and alias support."""
    if progress is not None:
        progress("Migrating to v11: add content relationship fields + game_aliases.")

    game_cols = await _table_columns(db, "games")
    if "content_type" not in game_cols:
        await db.execute("ALTER TABLE games ADD COLUMN content_type TEXT NOT NULL DEFAULT 'base_game'")
    if "parent_game_id" not in game_cols:
        await db.execute("ALTER TABLE games ADD COLUMN parent_game_id INTEGER REFERENCES games(id) ON DELETE SET NULL")
    if "is_primary_library_item" not in game_cols:
        await db.execute(
            "ALTER TABLE games ADD COLUMN is_primary_library_item INTEGER NOT NULL DEFAULT 1"
        )

    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS game_aliases (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id          INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            alias            TEXT NOT NULL,
            alias_normalized TEXT NOT NULL,
            alias_type       TEXT NOT NULL,
            source           TEXT,
            source_key       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_games_parent_game_id ON games(parent_game_id);
        CREATE INDEX IF NOT EXISTS idx_games_primary_library_item ON games(is_primary_library_item);
        CREATE INDEX IF NOT EXISTS idx_game_aliases_game_id ON game_aliases(game_id);
        CREATE INDEX IF NOT EXISTS idx_game_aliases_normalized ON game_aliases(alias_normalized);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_game_aliases_unique
            ON game_aliases(game_id, alias_normalized, alias_type, COALESCE(source, ''), COALESCE(source_key, ''));
        """
    )

    await _set_user_version(db, 11)
    await db.commit()


async def _migrate_v11_to_v12(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add nintendo_play_summary (Parental Controls per-app playtime history)."""
    if progress is not None:
        progress("Migrating to v12: add nintendo_play_summary.")

    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS nintendo_play_summary (
            device_id        TEXT NOT NULL,
            application_id   TEXT NOT NULL,
            period_type      TEXT NOT NULL,
            period_key       TEXT NOT NULL,
            playtime_minutes INTEGER NOT NULL,
            app_name         TEXT,
            updated_at       TEXT,
            PRIMARY KEY (device_id, application_id, period_type, period_key)
        );

        CREATE INDEX IF NOT EXISTS idx_nps_app ON nintendo_play_summary(application_id);
        """
    )

    await _set_user_version(db, 12)
    await db.commit()


async def _migrate_v12_to_v13(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Canonicalize existing tags and re-claim Steam tag enrichment.

    Data-only. Rewrites games.tags in place to the shared canonical vocabulary
    (synonym map + feature-flag filter), then nulls the SteamSpy cache for all Steam
    rows and the IGDB cache for Steam-linked games so the background worker refills
    the richer community/keyword tags the old genre-clobbering path suppressed.
    """
    if progress is not None:
        progress("Migrating to v13: canonicalize tags and re-claim Steam enrichment.")

    from ..tag_synonyms import canonical_tag
    from ..tags import is_feature_flag

    # Canonicalize every row's tags, including manually-overridden ones: this is a
    # content-preserving normalization (souls-like == soulslike), not a sync
    # clobber, and update_game also canonicalizes manual tags on write. Keeping
    # manual rows verbatim would leave them unmatchable by canonicalized filters.
    rows = await db.execute_fetchall("SELECT id, tags FROM games WHERE tags IS NOT NULL")
    for row in rows:
        try:
            tags = json.loads(row["tags"])
        except (ValueError, TypeError):
            continue
        if not isinstance(tags, list):
            continue
        seen: set[str] = set()
        canon: list[str] = []
        for t in tags:
            if not isinstance(t, str) or is_feature_flag(t):
                continue
            c = canonical_tag(t)
            if c and c not in seen:
                seen.add(c)
                canon.append(c)
        new_value = json.dumps(canon)
        if new_value != row["tags"]:
            await db.execute("UPDATE games SET tags = ? WHERE id = ?", (new_value, row["id"]))

    # Re-run SteamSpy for all Steam rows (community tags the old store path clobbered).
    await db.execute(
        "UPDATE steam_platform_data SET steamspy_cached_at = NULL, steamspy_claimed_at = NULL"
    )
    # Re-apply IGDB (union) for Steam-linked games only, to bound re-fetch cost.
    await db.execute(
        """
        UPDATE games SET igdb_cached_at = NULL, igdb_claimed_at = NULL
        WHERE id IN (
            SELECT gp.game_id FROM game_platforms gp
            JOIN game_platform_identifiers gpi
              ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
        )
        """,
        (STEAM_APP_ID,),
    )

    await _set_user_version(db, 13)
    await db.commit()


async def _migrate_v13_to_v14(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add a generic game_platforms.last_played column.

    Previously last-played was Steam-only (steam_platform_data.rtime_last_played).
    This adds a cross-platform per-platform last-played date (ISO ``YYYY-MM-DD``)
    so non-Steam syncs (Nintendo Parental Controls, PSN) can record it too.
    Additive column only — backfilled by the next sync of each platform.
    """
    if progress is not None:
        progress("Migrating to v14: add game_platforms.last_played.")

    cols = await _table_columns(db, "game_platforms")
    if "last_played" not in cols:
        await db.execute("ALTER TABLE game_platforms ADD COLUMN last_played TEXT")

    await _set_user_version(db, 14)
    await db.commit()


async def _migrate_v14_to_v15(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Repair self-referencing parent_game_id rows.

    IGDB edition resolution could resolve a row's parent back to the row itself
    (e.g. IGDB listed an edition/version whose parent is the same entry), writing
    ``parent_game_id = id``. Such a row is a non-primary library item whose parent
    points at itself, so it is excluded from search/list (which filter on
    ``is_primary_library_item = 1``) yet is unreachable as any other row's edition
    — an orphan that silently reads as "not owned". Clear the bogus self-parent
    and promote these rows back to primary library items so they surface again.
    Data-only; the ingest path now refuses to write a self-referencing parent.
    """
    if progress is not None:
        progress("Migrating to v15: repair self-referencing parent_game_id rows.")

    # content_type is forced back to base_game alongside is_primary so the pair
    # stays consistent (is_primary is always derived from content_type — a
    # 'dlc' + primary row would be invisible to both games and addons views).
    await db.execute(
        "UPDATE games SET parent_game_id = NULL, is_primary_library_item = 1, "
        "content_type = 'base_game' "
        "WHERE parent_game_id = id"
    )

    await _set_user_version(db, 15)
    await db.commit()


async def _migrate_v15_to_v16(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add game_wishlist: "want to play" tracking, kept out of game_platforms.

    A wishlist item may not be owned anywhere yet, so it gets a games row but no
    game_platforms row until it's actually owned — overloading owned=0 there
    would blur that table's "real platform relationship" invariant and risk a
    sync accidentally un-owning a row. Additive table only; a wishlist row is
    later deleted once ownership sync confirms the game is owned on that
    platform (see clear_fulfilled_wishlist_entries).
    """
    if progress is not None:
        progress("Migrating to v16: add game_wishlist.")

    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS game_wishlist (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            platform      TEXT NOT NULL,
            wishlisted_at TEXT NOT NULL,
            source        TEXT,
            UNIQUE(game_id, platform)
        );

        CREATE INDEX IF NOT EXISTS idx_game_wishlist_game_id ON game_wishlist(game_id);
        """
    )

    await _set_user_version(db, 16)
    await db.commit()


async def _migrate_v16_to_v17(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add scrape_config: versioned DB overrides for scrape descriptors.

    Additive table only (see the DDL comment in schema.py). An empty table
    means every provider runs on its code-level defaults, so existing
    databases need no data migration.
    """
    if progress is not None:
        progress("Migrating to v17: add scrape_config.")

    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS scrape_config (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            provider          TEXT NOT NULL,
            version           INTEGER NOT NULL,
            config_json       TEXT NOT NULL,
            status            TEXT NOT NULL CHECK (status IN ('active', 'pending', 'superseded', 'rolled_back')),
            source            TEXT NOT NULL DEFAULT 'manual',
            note              TEXT,
            validation_report TEXT,
            created_at        TEXT NOT NULL,
            UNIQUE(provider, version)
        );

        CREATE INDEX IF NOT EXISTS idx_scrape_config_provider_status
            ON scrape_config(provider, status);
        """
    )

    await _set_user_version(db, 17)
    await db.commit()


async def _migrate_v17_to_v18(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add game_prices + game_wishlist.store_identifier (see schema.py v18 note).

    Additive only — no data migration. ALTER TABLE ADD COLUMN is guarded so a
    re-run after a partial failure doesn't error.
    """
    if progress is not None:
        progress("Migrating to v18: add game_prices and game_wishlist.store_identifier.")

    wl_cols = await _table_columns(db, "game_wishlist")
    if "store_identifier" not in wl_cols:
        await db.execute("ALTER TABLE game_wishlist ADD COLUMN store_identifier TEXT")

    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS game_prices (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            platform      TEXT NOT NULL,
            shop          TEXT NOT NULL,
            price         REAL,
            regular_price REAL,
            cut_pct       INTEGER,
            currency      TEXT,
            deal_url      TEXT,
            fetched_at    TEXT NOT NULL,
            UNIQUE(game_id, platform, shop)
        );

        CREATE INDEX IF NOT EXISTS idx_game_prices_game_id ON game_prices(game_id);
        """
    )

    await _set_user_version(db, 18)
    await db.commit()


async def _migrate_v18_to_v19(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add games.igdb_platforms (see schema.py v19 note) and re-claim IGDB
    enrichment for wishlisted games so the backfill re-fetches their platform
    availability. Scoped to game_wishlist rows: re-claiming the whole library
    would burn thousands of IGDB calls for data only the deals tool reads,
    and only wishlist items are ever priced."""
    if progress is not None:
        progress("Migrating to v19: add games.igdb_platforms; re-claim IGDB for wishlisted games.")

    cols = await _table_columns(db, "games")
    if "igdb_platforms" not in cols:
        await db.execute("ALTER TABLE games ADD COLUMN igdb_platforms TEXT")

    await db.execute(
        """UPDATE games
           SET igdb_cached_at = NULL, igdb_claimed_at = NULL
           WHERE igdb_platforms IS NULL
             AND id IN (SELECT game_id FROM game_wishlist)"""
    )

    await _set_user_version(db, 19)
    await db.commit()


async def _migrate_v19_to_v20(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Re-run the v19 wishlist IGDB re-claim with the fixed resolution logic.

    Data-only, no DDL change. The v19 re-claim ran into several IGDB
    resolution gaps (category/game_type mismatch, a too-tight search limit
    burying base games behind DLC, zero-result searches for a few titles, and
    igdb_id-linked rows never being re-fetched by id) that left a handful of
    wishlisted games with igdb_platforms still NULL after the v19 pass
    completed. Re-run the identical re-claim UPDATE so production retries
    those stragglers through the fixed igdb.py backfill path post-deploy.
    """
    if progress is not None:
        progress("Migrating to v20: re-claim IGDB for still-unresolved wishlisted games.")

    await db.execute(
        """UPDATE games
           SET igdb_cached_at = NULL, igdb_claimed_at = NULL
           WHERE igdb_platforms IS NULL
             AND id IN (SELECT game_id FROM game_wishlist)"""
    )

    await _set_user_version(db, 20)
    await db.commit()


async def _migrate_v20_to_v21(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add games.completion_status (user-set play status; NULL = infer)."""
    if progress is not None:
        progress("Migrating to v21: add games.completion_status.")
    cols = await _table_columns(db, "games")
    if "completion_status" not in cols:
        # ALTER TABLE cannot add a CHECK'd column with existing rows in old
        # SQLite versions; add plain — the CHECK lives in the canonical DDL
        # applied on rebuilds, and update_game validates the vocabulary anyway.
        await db.execute("ALTER TABLE games ADD COLUMN completion_status TEXT")
    await _set_user_version(db, 21)
    await db.commit()


async def _migrate_v21_to_v22(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add play_history (cumulative playtime snapshots; see schema.py note)."""
    if progress is not None:
        progress("Migrating to v22: add play_history.")
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS play_history (
            game_id          INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            platform         TEXT NOT NULL,
            snapshot_date    TEXT NOT NULL,
            playtime_minutes INTEGER NOT NULL,
            PRIMARY KEY (game_id, platform, snapshot_date)
        );

        CREATE INDEX IF NOT EXISTS idx_play_history_date ON play_history(snapshot_date);
        """
    )
    await _set_user_version(db, 22)
    await db.commit()


# Indexes declared directly on games() in the versioned DDL. A table rebuild
# (see _rebuild_games_table_for_evergreen) renames games out of the way, and
# SQLite carries indexes along with a RENAME (unlike triggers, which stay
# bound to the old name and get dropped for free with the old table) — so
# these names stay claimed by the renamed-away copy and must be freed
# explicitly before the new table's CREATE INDEX IF NOT EXISTS statements can
# (re)create them on the new table.
_GAMES_TABLE_INDEXES = (
    "idx_games_name_normalized",
    "idx_games_parent_game_id",
    "idx_games_primary_library_item",
)


async def _games_check_constraint_missing_evergreen(db: aiosqlite.Connection) -> bool:
    """True if games.completion_status's CHECK predates 'evergreen'.

    Only databases whose games table was created via the fresh-DB DDL while
    SCHEMA_VERSION was 21 or 22 carry this CHECK at all — see the schema.py
    v20/v22 notes. The v20->v21 in-place migration adds the column via plain
    ALTER TABLE with no CHECK, so a DB that took that path has nothing to
    repair here (SQLite has no other way to store an unconstrained value).
    """
    row = await db.execute_fetchone(  # type: ignore[attr-defined]
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'games'"
    )
    sql = row[0] if row and row[0] else ""
    return "completion_status" in sql and "CHECK" in sql and "evergreen" not in sql


async def _rebuild_games_table_for_evergreen(db: aiosqlite.Connection) -> None:
    """Rebuild games with the current (evergreen-including) CHECK constraint.

    SQLite cannot ALTER a CHECK constraint, so this is a rename/recreate/copy
    dance in the same spirit as _rebuild_table_from_current_schema, but
    targeted at games itself (that helper is only ever called for its
    dependents, never for games). foreign_keys is toggled off around the
    whole sequence: with it on, SQLite treats the final DROP TABLE of the
    renamed-away copy as if it were a DELETE, cascading ON DELETE CASCADE
    children (e.g. game_platforms) and silently wiping data that was never
    meant to be deleted.
    """
    await db.execute("PRAGMA foreign_keys=OFF")
    try:
        await db.execute("DROP TABLE IF EXISTS games_evergreen_rebuild_old")
        await db.execute("PRAGMA legacy_alter_table=ON")
        await db.execute("ALTER TABLE games RENAME TO games_evergreen_rebuild_old")
        await db.execute("PRAGMA legacy_alter_table=OFF")
        for index_name in _GAMES_TABLE_INDEXES:
            await db.execute(f"DROP INDEX IF EXISTS {index_name}")
        await db.executescript(_V22_SCHEMA_DDL)

        old_cols = await _table_columns(db, "games_evergreen_rebuild_old")
        new_cols = await _table_columns(db, "games")
        keep = [col for col in new_cols if col in old_cols]
        cols_sql = ", ".join(keep)
        await db.execute(
            f"INSERT INTO games ({cols_sql}) SELECT {cols_sql} "
            "FROM games_evergreen_rebuild_old"
        )
        await db.execute("DROP TABLE IF EXISTS games_evergreen_rebuild_old")
        await db.commit()
    finally:
        await db.execute("PRAGMA legacy_alter_table=OFF")
        await db.execute("PRAGMA foreign_keys=ON")


async def _migrate_v22_to_v23(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Widen games.completion_status's CHECK to accept 'evergreen'.

    Only DBs whose games table still carries the pre-evergreen CHECK (created
    fresh while SCHEMA_VERSION was 21 or 22 — see schema.py) need the rebuild;
    DBs that only ever added the column via the v20->v21 plain ALTER TABLE
    have no CHECK to begin with and already accept 'evergreen'.
    """
    needs_rebuild = await _games_check_constraint_missing_evergreen(db)
    if progress is not None:
        if needs_rebuild:
            progress("Migrating to v23: rebuilding games table for 'evergreen' CHECK.")
        else:
            progress("Migrating to v23: games.completion_status already accepts 'evergreen'.")
    if needs_rebuild:
        await _rebuild_games_table_for_evergreen(db)
    await _set_user_version(db, 23)
    await db.commit()


async def _migrate_v23_to_v24(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Re-claim IGDB enrichment for owned/wishlisted games discover_series_gaps
    was falsely reporting as gaps (data-only, no DDL change).

    Two confirmed cases fall through the id-based have-set diff in
    tools/series.py:

    1. An owned row with igdb_id NULL (e.g. "Borderlands GOTY" ingested before
       IGDB backfill resolved it, or a resolution that never landed) is
       invisible to a diff keyed on igdb_id.
    2. An owned row whose igdb_id points at an edition-specific IGDB entry
       (e.g. "The Witcher: Enhanced Edition" igdb 283715 vs the canonical
       series member id 80) rather than the canonical member IGDB's own
       collections/franchises field lists. These entries typically carry no
       collection/franchise of their own, so series backfill never attaches a
       game_series_membership row for them either.

    Both cases share a signature: an owned/wishlisted game with no igdb_id, or
    with an igdb_id but zero game_series_membership rows. Resetting
    igdb_cached_at/igdb_claimed_at to NULL re-claims these rows for the normal
    background IGDB backfill pass, mirroring the v19/v20 wishlist IGDB
    re-claim. discover_series_gaps' new version-parent alias and
    normalized-name fallbacks close the same gap for future/unresolved rows
    without waiting on this backfill, but existing rows still benefit from a
    correctly linked igdb_id/series membership. Deletes nothing.
    """
    if progress is not None:
        progress(
            "Migrating to v24: re-claim IGDB for owned/wishlisted games with "
            "no igdb_id or no series membership."
        )

    await db.execute(
        """UPDATE games
           SET igdb_cached_at = NULL, igdb_claimed_at = NULL
           WHERE (EXISTS (SELECT 1 FROM game_platforms gp
                          WHERE gp.game_id = games.id AND gp.owned = 1)
                  OR EXISTS (SELECT 1 FROM game_wishlist w
                             WHERE w.game_id = games.id))
             AND (igdb_id IS NULL
                  OR NOT EXISTS (SELECT 1 FROM game_series_membership m
                                 WHERE m.game_id = games.id))"""
    )

    await _set_user_version(db, 24)
    await db.commit()


async def _migrate_v24_to_v25(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add games.cover_image_id (see schema.py v25 note) and re-claim IGDB
    enrichment for igdb_id-linked library/wishlist games so the backfill
    re-fetches their cover slug. Scoped to rows with an igdb_id: those re-fetch
    through the exact fetch-by-id path (no fuzzy-resolution risk), while rows
    without one are already claimed or failed for unrelated reasons and would
    burn search calls for no cover. Steam games render a capsule-art fallback
    by appid regardless, so a slow backfill only delays non-Steam covers."""
    if progress is not None:
        progress("Migrating to v25: add games.cover_image_id; re-claim IGDB for covers.")

    cols = await _table_columns(db, "games")
    if "cover_image_id" not in cols:
        await db.execute("ALTER TABLE games ADD COLUMN cover_image_id TEXT")

    await db.execute(
        """UPDATE games
           SET igdb_cached_at = NULL, igdb_claimed_at = NULL
           WHERE cover_image_id IS NULL
             AND igdb_id IS NOT NULL
             AND (EXISTS (SELECT 1 FROM game_platforms gp
                          WHERE gp.game_id = games.id AND gp.owned = 1)
                  OR EXISTS (SELECT 1 FROM game_wishlist w
                             WHERE w.game_id = games.id))"""
    )

    await _set_user_version(db, 25)
    await db.commit()


async def _migrate_v25_to_v26(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """NULL out OpenCritic's 'no score yet' sentinels (data-only, no DDL).

    OpenCritic's API reports an unscored game as topCriticScore -1 with an
    empty tier (percentRecommended may be -1 too); enrichment stored those
    raw, so -1 surfaced as a real score in tool responses and the game-cards
    widget. The write path now normalizes to NULL; this cleans rows written
    before the fix.
    """
    if progress is not None:
        progress("Migrating to v26: NULL out OpenCritic no-score sentinels.")

    await db.execute(
        "UPDATE game_platform_enrichment SET opencritic_score = NULL WHERE opencritic_score < 0"
    )
    await db.execute(
        "UPDATE game_platform_enrichment SET opencritic_percent_rec = NULL"
        " WHERE opencritic_percent_rec < 0"
    )
    await db.execute(
        "UPDATE game_platform_enrichment SET opencritic_tier = NULL WHERE opencritic_tier = ''"
    )

    await _set_user_version(db, 26)
    await db.commit()


async def _migrate_v26_to_v27(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Re-quarantine feature flags from games.tags with the widened vocabulary.

    STEAM_FEATURE_FLAGS grew Steam accessibility categories ("save anytime",
    "custom volume controls") and IGDB distribution keywords ("digital
    distribution", "achievements") — capability metadata, not taste. Rows
    written before the widening still carry them in tags, polluting
    tag_affinity, match-score denominators, and matched_tags explanations.
    Same shape as the v7->v8 split, but merges into existing games.features
    instead of overwriting.
    """
    from ..tags import STEAM_FEATURE_FLAGS, split_features

    if progress is not None:
        progress("Migrating to v27: quarantine widened feature-flag vocabulary.")

    db.row_factory = aiosqlite.Row
    rows = await db.execute_fetchall(
        "SELECT id, tags, features FROM games WHERE tags IS NOT NULL"
    )
    emptied_game_ids: list[int] = []
    for row in rows:
        try:
            tags = json.loads(row["tags"])
        except (ValueError, TypeError):
            continue
        if not isinstance(tags, list):
            continue
        real_tags, new_features = split_features(tags)
        if not new_features:
            continue
        try:
            features = json.loads(row["features"]) if row["features"] else []
        except (ValueError, TypeError):
            features = []
        if not isinstance(features, list):
            features = []
        merged = features + [f for f in new_features if f not in features]
        await db.execute(
            "UPDATE games SET tags = ?, features = ? WHERE id = ?",
            (json.dumps(real_tags), json.dumps(merged), row["id"]),
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

    await _set_user_version(db, 27)
    await db.commit()


async def _migrate_v27_to_v28(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Re-claim enrichment rows poisoned by operational failures (data-only).

    A 2026-07-05 prod audit found three classes of false-terminal cache state:

    1. IGDB false no-matches: an outage/creds window caused ~944 rows
       (687 owned base games, incl. Rocket League and Baldur's Gate 3) to be
       marked "checked, no match" (igdb_cached_at set, igdb_id NULL). The
       write paths are now failure-hygienic (creds-missing raises, plus a
       consecutive-miss circuit breaker in backfill_missing_games), so a
       one-time re-claim retries them; genuine no-matches just retry once.
    2. Rows enriched before cover fetching existed (igdb_id set,
       cover_image_id NULL, 84 rows) — re-claim so the exact by-id /
       external_games backfill path re-fetches the cover slug. This also
       routes steam-linked rows through the new self-correcting
       external_games-first resolution, repairing wrong-edition links like
       Layers of Fear (2016 appid pointing at the 2023 remake's igdb_id).
    3. HLTB permanent NOT_FOUND sentinels (309 rows, e.g. "HITMAN 2",
       "Grand Theft Auto V Legacy") written by the old matcher. NOT_FOUND is
       now a retryable timestamped marker and the matcher tries normalized /
       title-cased / edition-stripped variants, so reset for one immediate
       retry under the improved matcher.

    Mirrors the v19/v20/v24/v25 re-claim pattern (cached_at + claimed_at to
    NULL). Deletes nothing; existing enrichment values are preserved
    (_apply_igdb_metadata only fills NULL/non-overridden columns, and the
    HLTB writer keeps prior durations on a not-found result).
    """
    if progress is not None:
        progress(
            "Migrating to v28: re-claim IGDB false no-matches, missing covers, "
            "and HLTB NOT_FOUND rows."
        )

    await db.execute(
        "UPDATE games SET igdb_cached_at = NULL, igdb_claimed_at = NULL "
        "WHERE igdb_id IS NULL AND igdb_cached_at IS NOT NULL"
    )
    await db.execute(
        "UPDATE games SET igdb_cached_at = NULL, igdb_claimed_at = NULL "
        "WHERE igdb_id IS NOT NULL AND cover_image_id IS NULL"
    )
    await db.execute(
        "UPDATE games SET hltb_cached_at = NULL, hltb_claimed_at = NULL "
        "WHERE hltb_cached_at LIKE 'NOT_FOUND%'"
    )

    await _set_user_version(db, 28)
    await db.commit()


async def _migrate_v28_to_v29(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add game_platforms acquisition columns (additive, nullable; see schema.py
    v29 note). No backfill exists — no sync source carries purchase data; the
    columns are populated by the acquisition tools and purchase importers."""
    if progress is not None:
        progress("Migrating to v29: add game_platforms acquisition columns.")

    cols = await _table_columns(db, "game_platforms")
    for column, decl in (
        ("acquired_at", "TEXT"),
        ("price_paid", "REAL"),
        ("price_currency", "TEXT"),
        ("purchase_source", "TEXT"),
        ("bundle_name", "TEXT"),
    ):
        if column not in cols:
            await db.execute(f"ALTER TABLE game_platforms ADD COLUMN {column} {decl}")

    await _set_user_version(db, 29)
    await db.commit()


async def _migrate_v29_to_v30(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Re-quarantine feature flags from games.tags with the widened vocabulary.

    STEAM_FEATURE_FLAGS grew IGDB storefront/funding/capability keywords
    ("previously on - prime gaming", "kickstarter", "controller
    recommendation") plus the open-ended FEATURE_FLAG_PREFIXES families
    (subscription catalogs, expo appearances, award nominations). Same shape
    as v26->v27; the tag_affinity purge filters via is_feature_flag in Python
    because prefix families can't be expressed as an IN list.
    """
    from ..tags import is_feature_flag, split_features

    if progress is not None:
        progress("Migrating to v30: quarantine IGDB metadata-keyword families.")

    db.row_factory = aiosqlite.Row
    rows = await db.execute_fetchall(
        "SELECT id, tags, features FROM games WHERE tags IS NOT NULL"
    )
    emptied_game_ids: list[int] = []
    for row in rows:
        try:
            tags = json.loads(row["tags"])
        except (ValueError, TypeError):
            continue
        if not isinstance(tags, list):
            continue
        real_tags, new_features = split_features(tags)
        if not new_features:
            continue
        try:
            features = json.loads(row["features"]) if row["features"] else []
        except (ValueError, TypeError):
            features = []
        if not isinstance(features, list):
            features = []
        merged = features + [f for f in new_features if f not in features]
        await db.execute(
            "UPDATE games SET tags = ?, features = ? WHERE id = ?",
            (json.dumps(real_tags), json.dumps(merged), row["id"]),
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

    affinity_rows = await db.execute_fetchall("SELECT tag FROM tag_affinity")
    junk = [row["tag"] for row in affinity_rows if is_feature_flag(row["tag"])]
    for tag in junk:
        await db.execute("DELETE FROM tag_affinity WHERE tag = ?", (tag,))

    await _set_user_version(db, 30)
    await db.commit()


async def _migrate_v30_to_v31(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add game_platforms.manual_overrides (additive, nullable; see schema.py
    v31 note). No backfill — no existing row is hand-pinned yet; the column is
    populated only by set_playtime and consulted by the sync write paths."""
    if progress is not None:
        progress("Migrating to v31: add game_platforms.manual_overrides column.")

    cols = await _table_columns(db, "game_platforms")
    if "manual_overrides" not in cols:
        await db.execute("ALTER TABLE game_platforms ADD COLUMN manual_overrides TEXT")

    await _set_user_version(db, 31)
    await db.commit()


async def _migrate_v31_to_v32(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add game_platforms.delisted (additive, default 0; see schema.py v32
    note). No backfill — rows gain the flag only via the Steam license audit
    (an owned app the public owned-games API no longer returns)."""
    if progress is not None:
        progress("Migrating to v32: add game_platforms.delisted column.")

    cols = await _table_columns(db, "game_platforms")
    if "delisted" not in cols:
        await db.execute(
            "ALTER TABLE game_platforms ADD COLUMN delisted INTEGER NOT NULL DEFAULT 0"
        )

    await _set_user_version(db, 32)
    await db.commit()


async def _normalize_nintendo_title_ids(db: aiosqlite.Connection) -> None:
    """Uppercase every nintendo_title_id identifier + nintendo_play_summary
    application_id, merging case-only duplicates first.

    Historically VGCS (ownership) stored title ids verbatim while the
    Parental Controls API (playtime) reported uppercase hex for the same
    title, so the two tables could disagree in case only. Every write
    chokepoint now normalizes to uppercase (normalize_identifier_value), so
    every comparison between them can be plain equality instead of
    UPPER(x) = UPPER(y) at read time — this one-time data fix backfills
    existing rows to match. Idempotent: re-running on an already-normalized
    DB is a no-op (every case-insensitive group already has exactly one
    member, already in its own uppercase form).
    """
    # game_platform_identifiers: UNIQUE(identifier_type, identifier_value) is
    # an exact-string constraint, so "0100aaa" and "0100AAA" could both exist
    # as separate rows (typically the same game re-recorded under a different
    # casing over time — e.g. set_switch2_playtime_baseline's manual entry vs
    # a later VGCS sync). Keep the newest survivor (by last_seen_at, ties
    # broken by id) per case-insensitive group and delete the rest BEFORE
    # uppercasing, so the UNIQUE constraint never sees a collision.
    await db.executescript(
        """
        DROP TABLE IF EXISTS temp._gpi_nintendo_winners;
        CREATE TEMP TABLE _gpi_nintendo_winners AS
        SELECT gpi1.id AS id
        FROM game_platform_identifiers gpi1
        WHERE gpi1.identifier_type = 'nintendo_title_id'
          AND gpi1.id = (
              SELECT gpi2.id
              FROM game_platform_identifiers gpi2
              WHERE gpi2.identifier_type = 'nintendo_title_id'
                AND UPPER(gpi2.identifier_value) = UPPER(gpi1.identifier_value)
              ORDER BY gpi2.last_seen_at IS NULL, gpi2.last_seen_at DESC, gpi2.id DESC
              LIMIT 1
          );

        DELETE FROM game_platform_identifiers
        WHERE identifier_type = 'nintendo_title_id'
          AND id NOT IN (SELECT id FROM _gpi_nintendo_winners);

        UPDATE game_platform_identifiers
        SET identifier_value = UPPER(identifier_value)
        WHERE identifier_type = 'nintendo_title_id';

        DROP TABLE temp._gpi_nintendo_winners;
        """
    )

    # nintendo_play_summary: PK is (device_id, application_id, period_type,
    # period_key), so a case-only duplicate is a second full PK conflicting
    # only in application_id's case. MERGE each such group (sum minutes,
    # coalesce app_name, keep the latest updated_at) rather than picking a
    # winner — this reproduces exactly what the UPPER()-SUM readers this PR
    # removes used to report, so totals don't shift under this migration.
    await db.executescript(
        """
        DROP TABLE IF EXISTS temp._nps_nintendo_merged;
        CREATE TEMP TABLE _nps_nintendo_merged AS
        SELECT device_id,
               UPPER(application_id) AS application_id,
               period_type,
               period_key,
               SUM(playtime_minutes) AS playtime_minutes,
               MAX(app_name) AS app_name,
               MAX(updated_at) AS updated_at
        FROM nintendo_play_summary
        GROUP BY device_id, UPPER(application_id), period_type, period_key;

        DELETE FROM nintendo_play_summary;

        INSERT INTO nintendo_play_summary
            (device_id, application_id, period_type, period_key,
             playtime_minutes, app_name, updated_at)
        SELECT device_id, application_id, period_type, period_key,
               playtime_minutes, app_name, updated_at
        FROM _nps_nintendo_merged;

        DROP TABLE temp._nps_nintendo_merged;
        """
    )
    await db.commit()


async def _migrate_v32_to_v33(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add query_log (additive; see schema.py v33 note) and normalize every
    nintendo_title_id/application_id to uppercase (folded into this step
    rather than a separate v34, since v33 has not shipped anywhere yet)."""
    if progress is not None:
        progress("Migrating to v33: add query_log; normalize Nintendo title id casing.")

    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS query_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            sql        TEXT NOT NULL,
            row_count  INTEGER,
            truncated  INTEGER,
            elapsed_ms INTEGER,
            error      TEXT
        );
        """
    )
    await db.commit()

    await _normalize_nintendo_title_ids(db)

    await _set_user_version(db, 33)
    await db.commit()


async def _migrate_v33_to_v34(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add game_platforms.unowned_at + last_seen_in_source (additive, nullable;
    see schema.py's v34 note).

    Deliberately no backfill on either column. No row has been hand-marked
    unowned yet, and stamping every existing row's last_seen_in_source with
    "now" would assert source evidence that no sync ever produced — the whole
    point of the column is telling "the source returned this row" apart from
    "something wrote this row". NULL means unknown, and every reader treats it
    that way.
    """
    if progress is not None:
        progress(
            "Migrating to v34: add game_platforms.unowned_at + last_seen_in_source."
        )

    cols = await _table_columns(db, "game_platforms")
    if "unowned_at" not in cols:
        await db.execute("ALTER TABLE game_platforms ADD COLUMN unowned_at TEXT")
    if "last_seen_in_source" not in cols:
        await db.execute(
            "ALTER TABLE game_platforms ADD COLUMN last_seen_in_source TEXT"
        )

    await _set_user_version(db, 34)
    await db.commit()


async def _migrate_v34_to_v35(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Backfill game_platforms.last_played for Steam from rtime_last_played.

    v14 added the generic last_played column but only the Nintendo and PSN syncs
    ever wrote it — Steam kept its own epoch copy in
    steam_platform_data.rtime_last_played and the generic column stayed NULL for
    every Steam row. Now that last_played gates play-history window deltas (a
    game last played in 2022 cannot contribute to a 2026 window), Steam needs to
    populate it, and the rows already synced need the value they always had.

    Additive: only fills rows that are NULL, so a set_playtime pin (or any value
    written since) is never overwritten. rtime_last_played = 0 means "never
    played" in GetOwnedGames and stays NULL rather than becoming 1970-01-01.
    """
    if progress is not None:
        progress("Migrating to v35: backfill Steam last_played from rtime_last_played.")

    await db.execute(
        """
        UPDATE game_platforms
        SET last_played = (
            SELECT date(spd.rtime_last_played, 'unixepoch')
            FROM steam_platform_data spd
            WHERE spd.game_platform_id = game_platforms.id
              AND COALESCE(spd.rtime_last_played, 0) > 0
        )
        WHERE platform = 'steam'
          AND last_played IS NULL
          AND EXISTS (
              SELECT 1 FROM steam_platform_data spd
              WHERE spd.game_platform_id = game_platforms.id
                AND COALESCE(spd.rtime_last_played, 0) > 0
          )
        """
    )

    await _set_user_version(db, 35)
    await db.commit()


async def _migrate_v35_to_v36(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Freeze the platform's last-played date into each play_history snapshot.

    The history gate asks "was this game played during the window?", and answers
    it from last_played. Reading ``game_platforms.last_played`` — a mutable
    column the next sync advances — made that answer unstable for any window in
    the past: a correction correctly suppressed while the game sat unplayed
    since 2022 would start counting as playtime again the moment the game was
    next launched, because last_played would then be newer than the old
    window's start. A snapshot is an immutable observation, so the date it was
    taken with belongs on the snapshot.

    Existing rows are backfilled from the current game_platforms value. That is
    the best available estimate — it is exactly right today (nothing has moved
    since) and it freezes there, which is the whole point.
    """
    if progress is not None:
        progress("Migrating to v36: record last_played on play_history snapshots.")

    cols = await _table_columns(db, "play_history")
    if not cols:
        # play_history arrived in v21->v22; a database recorded at a later
        # version without it (an upgrade path that never ran that step) gets it
        # from the fresh-schema pass at the end of migrate_db, already carrying
        # last_played. Nothing to alter or backfill here.
        await _set_user_version(db, 36)
        await db.commit()
        return
    if "last_played" not in cols:
        await db.execute("ALTER TABLE play_history ADD COLUMN last_played TEXT")

    await db.execute(
        """
        UPDATE play_history
        SET last_played = (
            SELECT gp.last_played FROM game_platforms gp
            WHERE gp.game_id = play_history.game_id
              AND gp.platform = play_history.platform
        )
        WHERE last_played IS NULL
        """
    )

    await _set_user_version(db, 36)
    await db.commit()


async def _migrate_v36_to_v37(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add game_assessments (additive; see schema.py's v37 note).

    Nothing to backfill: recorded verdicts start the day the recording tool
    ships. The expression unique index (game_id, date(assessed_at)) is created
    here rather than left to the fresh-schema pass at the end of migrate_db,
    because it is the ON CONFLICT target ``record_assessment`` writes against —
    without it a same-day re-record would append a second verdict instead of
    replacing the day's row.
    """
    if progress is not None:
        progress("Migrating to v37: add game_assessments.")

    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS game_assessments (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id                  INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            assessed_at              TEXT NOT NULL,
            verdict                  TEXT NOT NULL CHECK (verdict IN
                                         ('buy_now', 'wishlist_for_sale', 'try_demo',
                                          'skip', 'play_what_you_own')),
            summary                  TEXT,
            craft_adjusted           REAL,
            craft_positive_pct       REAL,
            review_count             INTEGER,
            recent_trajectory        TEXT CHECK (recent_trajectory IN
                                         ('improving', 'stable', 'regressing')),
            opencritic_score         REAL,
            fit_call                 TEXT CHECK (fit_call IN
                                         ('strong fit', 'probable fit', 'coin flip',
                                          'probable miss')),
            anchors_cited            TEXT,
            flags                    TEXT,
            price_seen               REAL,
            price_currency           TEXT,
            price_platform           TEXT,
            target_price             REAL,
            instead_game_id          INTEGER REFERENCES games(id) ON DELETE SET NULL,
            steam_appid              INTEGER,
            context                  TEXT,
            owned_at_assessment      INTEGER NOT NULL DEFAULT 0,
            wishlisted_at_assessment INTEGER NOT NULL DEFAULT 0
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_game_assessments_game_day
            ON game_assessments(game_id, date(assessed_at));
        CREATE INDEX IF NOT EXISTS idx_game_assessments_game_id
            ON game_assessments(game_id);
        """
    )

    await _set_user_version(db, 37)
    await db.commit()


async def _migrate_v37_to_v38(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add the methodology provenance columns to game_assessments (additive).

    skill / skill_version / model are DECLARED-ONLY claims (see schema.py's v38
    note): what the recording client said about itself. Existing rows stay NULL
    and are deliberately NOT backfilled — this repo's canonical skill version is
    not evidence about a verdict recorded before the columns existed, and
    stamping it would invent a methodology history. NULL is the honest value,
    and every reader treats it as "unknown" rather than dropping the row.
    """
    if progress is not None:
        progress("Migrating to v38: add assessment provenance (skill/version/model).")

    cols = await _table_columns(db, "game_assessments")
    if cols:
        # An upgrade path that never ran v37 gets the table (already carrying
        # these columns) from the fresh-schema pass at the end of migrate_db.
        for column in ("skill", "skill_version", "model"):
            if column not in cols:
                await db.execute(
                    f"ALTER TABLE game_assessments ADD COLUMN {column} TEXT"
                )

    await _set_user_version(db, 38)
    await db.commit()


async def _migrate_v38_to_v39(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add game_assessments.presentation (JSON) — additive, no backfill.

    The model-authored presentation of a verdict (see schema.py's v39 note).
    Existing rows stay NULL because they genuinely carry none: the pitch and
    the for-you-if bullets are authored at recording time and cannot be
    reconstructed from the components afterwards.
    """
    if progress is not None:
        progress("Migrating to v39: add assessment presentation (JSON).")

    cols = await _table_columns(db, "game_assessments")
    if cols and "presentation" not in cols:
        await db.execute("ALTER TABLE game_assessments ADD COLUMN presentation TEXT")

    await _set_user_version(db, 39)
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
    await db.executescript(_V39_SCHEMA_DDL)

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


async def _snapshot_before_migration(
    db: aiosqlite.Connection, detected_state: str, current_version: int
) -> str | None:
    """Snapshot the DB file (VACUUM INTO) before a schema-changing migration.

    Migration steps rebuild tables destructively, and some tables hold data
    with no external source to re-sync from (nintendo_play_summary is
    forward-only; manual ratings and overrides exist only here). VACUUM INTO
    is atomic and WAL-safe. One snapshot is kept per source version; retrying
    the same migration overwrites it. A snapshot failure aborts the migration
    — better not to migrate than to migrate without the safety net.
    """
    if detected_state == "fresh" or current_version == SCHEMA_VERSION:
        return None

    # Resolve the file behind THIS connection (not _db_path(): callers such as
    # tests migrate connections that aren't the configured database).
    row = await db.execute_fetchone(  # type: ignore[attr-defined]
        "SELECT file FROM pragma_database_list WHERE name = 'main'"
    )
    db_path = row[0] if row else ""
    if not db_path:  # in-memory / temporary database — nothing on disk to back up
        return None

    backup_path = f"{db_path}.pre-v{current_version}.bak"
    Path(backup_path).unlink(missing_ok=True)
    await db.commit()  # VACUUM cannot run inside a transaction
    await db.execute("VACUUM INTO ?", (backup_path,))
    return backup_path


async def _sync_fts_index(db: aiosqlite.Connection) -> bool:
    """Ensure games_fts + triggers exist and mirror games; False if no FTS5.

    Runs on every migrate_db call. A destructive games-table rebuild drops the
    triggers with the old table; CREATE ... IF NOT EXISTS restores them and the
    full resync repairs any rows changed while triggers were absent.
    """
    try:
        await db.executescript(_FTS_DDL)
    except sqlite3.OperationalError:
        return False  # SQLite build lacks FTS5/trigram — LIKE path still works
    await db.execute("DELETE FROM games_fts")
    await db.execute(
        "INSERT INTO games_fts(rowid, name_normalized)"
        " SELECT id, COALESCE(name_normalized, lower(name)) FROM games"
    )
    await db.commit()
    return True


async def _sync_query_views(db: aiosqlite.Connection) -> None:
    """(Re)create the query-tool semantic views (data/db/readonly.py + tools/query.py).

    Runs on every migrate_db call, like _sync_fts_index — DROP VIEW IF EXISTS +
    CREATE VIEW so a view-definition change (schema.py::_QUERY_VIEWS_DDL) takes
    effect on the next restart without a schema-version bump. Views are cheap;
    no data migration is ever needed for them.
    """
    await db.executescript(_QUERY_VIEWS_DDL)
    await db.commit()


_MigrationStep = Callable[[aiosqlite.Connection, "_Progress | None"], Awaitable[None]]

# Pre-user_version databases are recognized by shape; recording the detected
# version lets the step ladder below take over. Detection has no states for
# v6 or v13-v16 (those versions changed no detectable shape).
_RECORDED_STATE_VERSIONS: dict[str, int] = {
    f"v{n}": n for n in (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12)
}

# Ordered chain of (from_version, step). Each step migrates from_version ->
# from_version + 1. Adding a schema version = one entry here + a new step
# function + bumping SCHEMA_VERSION.
_MIGRATION_STEPS: tuple[tuple[int, _MigrationStep], ...] = (
    (1, _migrate_v1_to_v2),
    (2, _migrate_v2_to_v3),
    (3, _migrate_v3_to_v4),
    (4, _migrate_v4_to_v5),
    (5, _migrate_v5_to_v6),
    (6, _migrate_v6_to_v7),
    (7, _migrate_v7_to_v8),
    (8, _migrate_v8_to_v9),
    (9, _migrate_v9_to_v10),
    (10, _migrate_v10_to_v11),
    (11, _migrate_v11_to_v12),
    (12, _migrate_v12_to_v13),
    (13, _migrate_v13_to_v14),
    (14, _migrate_v14_to_v15),
    (15, _migrate_v15_to_v16),
    (16, _migrate_v16_to_v17),
    (17, _migrate_v17_to_v18),
    (18, _migrate_v18_to_v19),
    (19, _migrate_v19_to_v20),
    (20, _migrate_v20_to_v21),
    (21, _migrate_v21_to_v22),
    (22, _migrate_v22_to_v23),
    (23, _migrate_v23_to_v24),
    (24, _migrate_v24_to_v25),
    (25, _migrate_v25_to_v26),
    (26, _migrate_v26_to_v27),
    (27, _migrate_v27_to_v28),
    (28, _migrate_v28_to_v29),
    (29, _migrate_v29_to_v30),
    (30, _migrate_v30_to_v31),
    (31, _migrate_v31_to_v32),
    (32, _migrate_v32_to_v33),
    (33, _migrate_v33_to_v34),
    (34, _migrate_v34_to_v35),
    (35, _migrate_v35_to_v36),
    (36, _migrate_v36_to_v37),
    (37, _migrate_v37_to_v38),
    (38, _migrate_v38_to_v39),
)


async def _run_migrations(
    db: aiosqlite.Connection,
    progress: _Progress | None = None,
) -> MigrationResult:
    detected_state = await _detect_schema_state(db)
    initial_version = await _get_user_version(db)
    version = initial_version
    if initial_version > SCHEMA_VERSION:
        # A newer build already migrated this file (a rollback across a schema
        # bump). Running on would re-stamp user_version DOWN below the tables
        # that exist, and the next forward deploy would re-apply that step over
        # already-migrated data. Refuse before touching anything, snapshot
        # included; deploy.md "Manual rollback" says what to restore.
        raise RuntimeError(
            f"database schema is v{initial_version} but this build knows v{SCHEMA_VERSION}: "
            "a newer build migrated it. Deploy that build, or restore "
            f"gamelib.db.pre-v{SCHEMA_VERSION}.bak before running this one."
        )
    applied_steps: list[str] = []

    snapshot_path = await _snapshot_before_migration(db, detected_state, initial_version)
    if snapshot_path is not None:
        _emit(progress, f"Backed up database to {snapshot_path} before migrating.", applied_steps)

    if detected_state == "fresh":
        await db.executescript(_V39_SCHEMA_DDL)
        fts_enabled = await _sync_fts_index(db)
        await _sync_query_views(db)
        await _set_user_version(db, SCHEMA_VERSION)
        await db.commit()
        _emit(progress, f"Initialized fresh database at schema v{SCHEMA_VERSION}.", applied_steps)
        return MigrationResult(
            initial_version=initial_version,
            final_version=SCHEMA_VERSION,
            detected_state=detected_state,
            applied_steps=applied_steps,
            fts_enabled=fts_enabled,
        )

    if version == 0:
        if detected_state == "legacy":
            _emit(progress, "Applying migration step v0 -> v1.", applied_steps)
            await _migrate_legacy_to_v1(db, progress=None)
            version = 1
        elif detected_state in _RECORDED_STATE_VERSIONS:
            version = _RECORDED_STATE_VERSIONS[detected_state]
            await _set_user_version(db, version)
            await db.commit()
            _emit(progress, f"Recorded existing schema as v{version}.", applied_steps)

    for from_version, step in _MIGRATION_STEPS:
        if version == from_version:
            _emit(
                progress,
                f"Applying migration step v{from_version} -> v{from_version + 1}.",
                applied_steps,
            )
            await step(db, None)
            version = from_version + 1

    await _repair_game_foreign_keys(db)
    await db.execute("DROP INDEX IF EXISTS idx_game_platform_identifiers_lookup")
    await _repair_identifier_primary_flags(db)
    await db.executescript(_V39_SCHEMA_DDL)
    if version != SCHEMA_VERSION:
        await _set_user_version(db, SCHEMA_VERSION)
        version = SCHEMA_VERSION
    await db.commit()
    fts_enabled = await _sync_fts_index(db)
    await _sync_query_views(db)

    return MigrationResult(
        initial_version=initial_version,
        final_version=version,
        detected_state=detected_state,
        applied_steps=applied_steps,
        fts_enabled=fts_enabled,
    )


async def _ensure_db_initialized(db: aiosqlite.Connection) -> None:
    global _DB_READY_PATH, _FTS_READY_PATH, _DB_INIT_LOCK

    db_path = _db_path()
    if _DB_READY_PATH == db_path:
        return

    if _DB_INIT_LOCK is None:
        _DB_INIT_LOCK = asyncio.Lock()

    async with _DB_INIT_LOCK:
        if _DB_READY_PATH == db_path:
            return
        result = await _run_migrations(db)
        _DB_READY_PATH = db_path
        _FTS_READY_PATH = db_path if result.fts_enabled else None


def _gl_ln(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    return math.log(value)


async def _register_gl_ln(conn: aiosqlite.Connection) -> None:
    """Register the gl_ln custom SQL function (natural log for IDF weights).

    Shared by the RW connection setup below and the read-only query connection
    in data/db/readonly.py, so the two connections never drift on what gl_ln
    means.
    """
    # Natural log for SQL scoring (IDF weights in discover_games). SQLite's
    # builtin ln() only exists when compiled with SQLITE_ENABLE_MATH_FUNCTIONS,
    # so ship our own under a distinct name rather than depend on the build.
    await conn.create_function("gl_ln", 1, _gl_ln, deterministic=True)


async def _configure_connection(conn: aiosqlite.Connection, *, enable_wal: bool) -> None:
    conn.row_factory = aiosqlite.Row
    await _register_gl_ln(conn)
    await conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    await conn.execute("PRAGMA foreign_keys=ON")
    if enable_wal:
        await conn.execute("PRAGMA journal_mode=WAL")


# ── Write-contention retry ───────────────────────────────────────────────────
# WAL + a 30s busy_timeout (above) handle the ordinary case: a writer waiting
# on another writer's lock. They do NOT cover the one this codebase actually
# hits. A transaction that has already READ the main database and then tries to
# WRITE it, after some other connection committed in between, fails with
# SQLITE_BUSY_SNAPSHOT — the read snapshot it holds can no longer be extended
# into a write. SQLite reports that as "database is locked" and returns it
# IMMEDIATELY: the busy handler is deliberately not consulted, because no
# amount of waiting can fix a stale snapshot. Only restarting the transaction
# can, which is what these retries do.
#
# That shape is exactly the platform-sync write path — read-then-write inside
# one transaction (bulk_upsert_steam_library resolves appids against the live
# tables, then writes) while background enrichment commits alongside it. It is
# how a Steam sync failed silently for three days in production while every
# other platform in the same run succeeded.
#
# Only wrap operations that are safe to run twice: an idempotent upsert whose
# failed attempt committed nothing. Never wrap something that mints rows from a
# partially-committed state.
_WRITE_RETRY_ATTEMPTS = 5
_WRITE_RETRY_BASE_DELAY_SECONDS = 0.1


def _is_write_contention_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


def retry_on_write_contention(func):
    """Retry an idempotent DB write on SQLITE_BUSY/BUSY_SNAPSHOT, backing off.

    Delays are 0.1s, 0.2s, 0.4s, 0.8s — under a second in total, since the
    contending writer is another coroutine on this same process's loop and the
    lock it holds is measured in milliseconds. The final attempt re-raises, so
    a genuinely stuck database still surfaces as an error rather than a hang.
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        for attempt in range(_WRITE_RETRY_ATTEMPTS):
            try:
                return await func(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_write_contention_error(exc) or attempt == _WRITE_RETRY_ATTEMPTS - 1:
                    raise
                delay = _WRITE_RETRY_BASE_DELAY_SECONDS * (2**attempt)
                logger.warning(
                    "%s hit SQLite write contention (%s); retrying in %.1fs "
                    "(attempt %d/%d)",
                    func.__name__,
                    exc,
                    delay,
                    attempt + 1,
                    _WRITE_RETRY_ATTEMPTS,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    return wrapper


@asynccontextmanager
async def get_db():
    """Async context manager for a WAL-enabled, Row-factory SQLite connection.

    When pooling is enabled (server lifespan), connections are checked out
    exclusively and reused across calls on the same event loop.
    """
    db_path = _db_path()
    _ensure_db_parent_dir(db_path)

    if not _POOL_ENABLED:
        async with aiosqlite.connect(db_path, timeout=_SQLITE_CONNECT_TIMEOUT_SECONDS) as conn:
            await _configure_connection(conn, enable_wal=_DB_READY_PATH != db_path)
            await _ensure_db_initialized(conn)
            yield conn
        return

    conn = _pool_checkout(db_path)
    if conn is None:
        conn = await aiosqlite.connect(db_path, timeout=_SQLITE_CONNECT_TIMEOUT_SECONDS)
        try:
            await _configure_connection(conn, enable_wal=_DB_READY_PATH != db_path)
            await _ensure_db_initialized(conn)
        except BaseException:
            await conn.close()
            raise
    try:
        yield conn
        # Match per-call semantics: uncommitted work dies with the "connection".
        await conn.rollback()
    except BaseException:
        # Transaction state is unknown after a failure inside the block (or if
        # rollback() itself raised) — never return this connection to the pool.
        await conn.close()
        raise
    await _pool_checkin(db_path, conn)


async def migrate_db(progress: _Progress | None = None) -> MigrationResult:
    """Run all schema migrations against the configured DB path."""
    global _DB_READY_PATH, _FTS_READY_PATH

    db_path = _db_path()
    _ensure_db_parent_dir(db_path)
    async with aiosqlite.connect(db_path, timeout=_SQLITE_CONNECT_TIMEOUT_SECONDS) as db:
        await _configure_connection(db, enable_wal=True)
        result = await _run_migrations(db, progress=progress)
        _DB_READY_PATH = db_path
        _FTS_READY_PATH = db_path if result.fts_enabled else None
        return result


async def init_db() -> None:
    """Create tables if they don't exist and migrate to the latest schema."""
    result = await migrate_db()
    # The v12->v13 migration canonicalizes games.tags in place, which can orphan
    # tag_affinity rows still keyed on the old synonym form; v26->v27 changes
    # the affinity formula itself (mean-centered/shrunk), so rows computed on
    # the old avg*log(count) scale would be misread as signed centered values.
    # Rebuild affinity once so discover/taste scoring is correct immediately,
    # without waiting for the next sync_ratings/rate_game/enrichment pass.
    # Any later change to the affinity formula or its scale bumps
    # AFFINITY_FORMULA_VERSION instead of minting a schema migration — the
    # stored scale record says which formula produced the current rows, so a
    # stale-scale table heals on the next startup by the same reasoning.
    if (
        any("v12 -> v13" in step or "v26 -> v27" in step for step in result.applied_steps)
        or not await affinity_scale_is_current()
    ):
        await recompute_tag_affinity()


# ── Domain submodules (re-exported; imported last so the bottom layer above is
# fully defined before each leaf does `from . import get_db, ...`). ───────────
from .affinity import (
    affinity_scale_is_current,
    estimate_shrinkage_weight,
    get_affinity_scale,
    recompute_tag_affinity,
    strong_affinity_cut,
)
from .claims import (
    HLTB_NOT_FOUND_RETRY_DAYS,
    _claim_cutoff_iso,
    _claim_ids,
    claim_game_ids_for_hltb,
    claim_game_ids_for_igdb,
    claim_game_platform_ids_for_metacritic,
    claim_game_platform_ids_for_opencritic,
    claim_steam_platform_ids_for_protondb,
    claim_steam_platform_ids_for_steamspy,
    claim_steam_platform_ids_for_store,
    clear_all_enrichment_claims,
    clear_claim,
    invalidate_igdb_match_enrichment,
    invalidate_name_derived_enrichment,
    load_games_for_igdb_backfill,
    load_hltb_batch_rows,
    load_metacritic_batch_rows,
    load_opencritic_batch_rows,
    load_steam_platform_batch_rows,
    load_store_batch_rows,
    release_game_claim,
)
from .fuzzy import (
    find_conflicting_fuzzy_key,
    find_game_by_name_fuzzy,
    load_fuzzy_candidates,
    titles_conflict_on_identity,
)
from .history import record_play_history_snapshots
from .queries import (
    ASSESSMENT_SUMMARY_COLUMNS,
    NINTENDO_BASELINE_DEVICE_ID,
    NINTENDO_BASELINE_PERIOD_KEY,
    _coerce_identifier_value,
    _platform_dict,
    edition_hides_owned_game,
    exact_name_steam_conflict,
    get_assessed_game_id_by_appid,
    get_game_by_appid,
    get_game_by_identifier,
    get_game_by_igdb_id,
    get_game_by_name_exact,
    get_game_substance,
    get_meta,
    get_meta_prefix,
    get_nintendo_baseline_minutes,
    get_nintendo_play_totals,
    get_nintendo_synced_minutes,
    get_platform_game_by_normalized_name,
    get_steam_appid_for_game,
    get_steam_platform_row_by_appid,
    get_wishlist_game_id_by_store_identifier,
    has_nested_children,
    load_latest_assessments,
    load_platforms_for_games,
    load_recent_assessments,
    load_related_content_for_games,
    load_series_for_games,
    load_wishlist_with_prices,
    nesting_substance_conflict,
    set_meta,
    set_meta_many,
)
from .upserts import (
    ACQUISITION_FIELDS,
    GAME_EDITABLE_FIELDS,
    PLATFORM_EDITABLE_FIELDS,
    adopt_platform_identifier,
    apply_content_classification,
    apply_manual_game_fields,
    apply_manual_platform_fields,
    bulk_upsert_steam_library,
    clear_fulfilled_wishlist_entries,
    delete_nintendo_playtime_baseline,
    delete_stale_wishlist_entries,
    get_manual_overrides,
    get_platform_manual_overrides,
    remove_manual_overrides,
    remove_platform_manual_overrides,
    repair_misclassified_platform_row,
    resolve_parent_game,
    seed_platform_provider_alias,
    set_platform_acquisition,
    set_platform_ownership,
    set_steam_delisted,
    upsert_game,
    upsert_game_alias,
    upsert_game_platform,
    upsert_game_platform_enrichment,
    upsert_game_platform_identifier,
    upsert_game_prices,
    upsert_game_series_links,
    upsert_nintendo_play_summary,
    upsert_steam_platform_data,
    upsert_wishlist_entry,
)
