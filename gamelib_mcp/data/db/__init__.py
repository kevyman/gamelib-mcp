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
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast
from weakref import WeakKeyDictionary

import aiosqlite

from gamelib_mcp.env import load_project_dotenv


# Polyfill: aiosqlite <0.20 doesn't have execute_fetchone as a Connection method
async def _execute_fetchone(
    self: aiosqlite.Connection, sql: str, parameters: Iterable[Any] = ()
) -> Any:
    async with self.execute(sql, parameters) as cursor:
        return await cursor.fetchone()


if not hasattr(aiosqlite.Connection, "execute_fetchone"):
    aiosqlite.Connection.execute_fetchone = _execute_fetchone  # type: ignore[attr-defined]


if TYPE_CHECKING:
    class DBConnection(aiosqlite.Connection):
        """An aiosqlite connection as this package hands it out.

        Identical to aiosqlite.Connection at runtime — the only difference is
        the execute_fetchone polyfill installed above, which aiosqlite's own
        class does not declare. Typed as a plain callable returning Any because
        a row's columns are whatever the caller's SELECT asked for.
        """

        execute_fetchone: Callable[..., Coroutine[Any, Any, Any]]
        # aiosqlite declares execute_fetchall as returning Iterable[Row],
        # but sqlite3's fetchall really returns a list and callers here len()
        # and index the result. Left loose rather than narrowed to list[Any],
        # which mypy rejects as an incompatible override of the base class.
        execute_fetchall: Callable[..., Any]
else:
    DBConnection = aiosqlite.Connection


logger = logging.getLogger(__name__)

_DB_READY_PATH: str | None = None
_FTS_READY_PATH: str | None = None
_DB_INIT_LOCK: asyncio.Lock | None = None
_ENV_LOADED = False
_FuzzyKey = TypeVar("_FuzzyKey")
_RetryParams = ParamSpec("_RetryParams")
_RetryResult = TypeVar("_RetryResult")
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
    asyncio.AbstractEventLoop, dict[str, list[DBConnection]]
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


def _pool_checkout(db_path: str) -> "DBConnection | None":
    loop = asyncio.get_running_loop()
    conns = _POOL_IDLE.get(loop, {}).get(db_path)
    if conns:
        return conns.pop()
    return None


async def _pool_checkin(db_path: str, conn: DBConnection) -> None:
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


# DDL re-export. The migration chain moved to .migrations and imports these
# from .schema itself; the names stay bound here because migration tests and
# scripts/seed_v1_sample_db.py reach them as gamelib_mcp.data.db._V{N}_SCHEMA_DDL.
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


def retry_on_write_contention(
    func: Callable[_RetryParams, Awaitable[_RetryResult]],
) -> Callable[_RetryParams, Awaitable[_RetryResult]]:
    """Retry an idempotent DB write on SQLITE_BUSY/BUSY_SNAPSHOT, backing off.

    Delays are 0.1s, 0.2s, 0.4s, 0.8s — under a second in total, since the
    contending writer is another coroutine on this same process's loop and the
    lock it holds is measured in milliseconds. The final attempt re-raises, so
    a genuinely stuck database still surfaces as an error rather than a hang.
    """
    @functools.wraps(func)
    async def wrapper(
        *args: _RetryParams.args, **kwargs: _RetryParams.kwargs
    ) -> _RetryResult:
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
async def get_db() -> AsyncIterator[DBConnection]:
    """Async context manager for a WAL-enabled, Row-factory SQLite connection.

    When pooling is enabled (server lifespan), connections are checked out
    exclusively and reused across calls on the same event loop.
    """
    db_path = _db_path()
    _ensure_db_parent_dir(db_path)

    if not _POOL_ENABLED:
        async with aiosqlite.connect(db_path, timeout=_SQLITE_CONNECT_TIMEOUT_SECONDS) as direct:
            await _configure_connection(direct, enable_wal=_DB_READY_PATH != db_path)
            await _ensure_db_initialized(direct)
            yield cast("DBConnection", direct)
        return

    conn: DBConnection | None = _pool_checkout(db_path)
    if conn is None:
        conn = cast(
            "DBConnection",
            await aiosqlite.connect(db_path, timeout=_SQLITE_CONNECT_TIMEOUT_SECONDS),
        )
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

# _ensure_db_initialized and migrate_db above call _run_migrations by that
# name; the chain itself needs only the bottom layer, so it loads like any
# other leaf.
from .migrations import _run_migrations
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
