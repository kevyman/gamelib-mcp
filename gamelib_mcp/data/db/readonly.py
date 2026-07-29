"""Dedicated read-only SQLite connection for query_library()/get_db_schema().

A single dedicated aiosqlite connection, opened against ``file:{path}?mode=ro``
and locked down with ``PRAGMA query_only`` plus an authorizer allowlist, so an
arbitrary AI-generated SELECT/WITH/EXPLAIN can never mutate data even if every
other guard in ``tools/query.py`` somehow failed. Kept entirely separate from
``get_db()``'s RW connection (which stays WAL) — a ro reader coexists fine
alongside a WAL writer on the same file.

aiosqlite 0.22.1 (the version this project pins) exposes ``set_authorizer``
and ``set_progress_handler`` as public ``Connection`` methods that internally
dispatch to the underlying ``sqlite3.Connection`` on its worker thread (see
``_install_authorizer``/``_install_progress_handler`` below). It has no such
wrapper for ``setlimit``, so ``_install_limits`` is the one helper that
reaches through the private ``_conn``/``_execute`` API — still routed onto
the connection's worker thread, which is the hard constraint all three
share. Each call is kept in its own small helper so an aiosqlite upgrade
that changes the API surface has exactly one place per call to patch.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from weakref import WeakKeyDictionary

import aiosqlite

from . import (
    _SQLITE_BUSY_TIMEOUT_MS,
    _SQLITE_CONNECT_TIMEOUT_SECONDS,
    _db_path,
    _register_gl_ln,
)

# Runaway-query protection: a query is aborted once it's been running longer
# than this many seconds (see _install_progress_handler). query_library()
# accepts no caller override — 5s is generous for anything a schema-aware
# SELECT should need, and a longer-running query is a sign to add WHERE/LIMIT
# or an aggregate rather than let an AI-generated scan run unbounded.
DEFAULT_QUERY_TIMEOUT_SECONDS = 5.0

# How many SQLite VM instructions elapse between progress-handler checks. Small
# enough that a runaway query is caught promptly; large enough not to add
# meaningful overhead to a normal query.
_PROGRESS_HANDLER_OPCODE_INTERVAL = 1000

# The progress handler can't interrupt work done inside a single VM opcode, so
# a memory bomb like length(randomblob(2000000000)) would allocate gigabytes
# before the handler ever fires. SQLITE_LIMIT_LENGTH caps every string/blob the
# engine will construct — the oversized call fails immediately with "string or
# blob too big" instead of allocating. 1 MiB is generous headroom for real
# intermediate values (query_library truncates cells to 300 chars anyway); the
# SQL-text cap is a companion belt against absurd statement sizes.
_MAX_LENGTH_BYTES = 1_048_576
_MAX_SQL_LENGTH_BYTES = 100_000

# Authorizer allowlist — SQLITE_SELECT/READ/FUNCTION/RECURSIVE are everything a
# read-only SELECT/WITH RECURSIVE/EXPLAIN needs; everything else (PRAGMA,
# ATTACH, INSERT/UPDATE/DELETE, DDL, transaction control) is denied. This is
# the real enforcement layer — deliberately not a SQL keyword blocklist (see
# _first_keyword below, which is only a cheap belt).
_ALLOWED_AUTHORIZER_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)

# First-keyword belt: Python's sqlite3.execute() already refuses to run more
# than one statement per call, so this is not the anti-injection boundary —
# it's a cheap, obvious rejection for the common "not even trying to be a
# SELECT" case before we bother opening a cursor.
ALLOWED_FIRST_KEYWORDS = frozenset({"SELECT", "WITH", "EXPLAIN", "VALUES"})

_LEADING_NOISE_RE = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)+", re.DOTALL)
_FIRST_WORD_RE = re.compile(r"[A-Za-z]+")


def _first_keyword(sql: str) -> str:
    """The statement's first SQL keyword, ignoring leading whitespace/comments."""
    stripped = _LEADING_NOISE_RE.sub("", sql)
    match = _FIRST_WORD_RE.match(stripped)
    return match.group(0).upper() if match else ""


def _authorizer(
    action_code: int,
    _arg1: str | None,
    _arg2: str | None,
    _db_name: str | None,
    _trigger_name: str | None,
) -> int:
    if action_code in _ALLOWED_AUTHORIZER_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


async def _install_authorizer(conn: aiosqlite.Connection) -> None:
    """Allowlist-only authorizer: only SELECT/READ/FUNCTION/RECURSIVE pass.

    PUBLIC-API CAVEAT: aiosqlite.Connection.set_authorizer is a public method
    in the pinned 0.22.1 release; it marshals the callback onto the
    connection's own worker thread internally, which is the hard constraint
    (sqlite3's set_authorizer must be called on the thread that owns the
    connection object — aiosqlite's Connection always satisfies this because
    every operation, including this one, runs through its single worker
    thread). If a future aiosqlite drops the public wrapper, replace this
    call with ``await conn._execute(conn._conn.set_authorizer, _authorizer)``
    (still routed through the same worker thread via ``_execute``).
    """
    await conn.set_authorizer(_authorizer)


def _make_progress_handler(deadline: float):
    def _handler() -> int:
        # A non-zero return aborts the running statement (raises
        # sqlite3.OperationalError: interrupted).
        return 1 if time.monotonic() > deadline else 0

    return _handler


async def _install_progress_handler(conn: aiosqlite.Connection, deadline: float) -> None:
    """Abort the in-flight statement once ``time.monotonic()`` passes ``deadline``.

    Same public-API note as _install_authorizer: aiosqlite.Connection.
    set_progress_handler is public in 0.22.1 and internally dispatches to the
    worker thread; no private-API reach-through was needed.
    """
    await conn.set_progress_handler(_make_progress_handler(deadline), _PROGRESS_HANDLER_OPCODE_INTERVAL)


async def _clear_progress_handler(conn: aiosqlite.Connection) -> None:
    await conn.set_progress_handler(None, 0)  # type: ignore[arg-type]


async def _install_limits(conn: aiosqlite.Connection) -> None:
    """Cap string/blob and SQL-text sizes on the ro connection.

    PRIVATE-API NOTE: unlike set_authorizer/set_progress_handler, aiosqlite
    0.22.1 has no public wrapper for sqlite3.Connection.setlimit, so this is
    the one place that reaches through to the underlying connection —
    ``_execute`` routes the call onto the connection's own worker thread,
    which is the hard constraint setlimit shares with the other two.
    """
    await conn._execute(conn._conn.setlimit, sqlite3.SQLITE_LIMIT_LENGTH, _MAX_LENGTH_BYTES)
    await conn._execute(conn._conn.setlimit, sqlite3.SQLITE_LIMIT_SQL_LENGTH, _MAX_SQL_LENGTH_BYTES)


# ── Lazy per-event-loop singleton connection ─────────────────────────────────
# Mirrors the per-event-loop WeakKeyDictionary lock pattern in lifecycle.py:
# there's no lifespan hook for this module, so the connection is opened lazily
# on first use per running loop (each test's IsolatedAsyncioTestCase gets its
# own loop, so this also keeps tests isolated from each other for free).
_RO_CONNECTIONS: WeakKeyDictionary[asyncio.AbstractEventLoop, aiosqlite.Connection] = WeakKeyDictionary()
_RO_CONNECTION_PATHS: WeakKeyDictionary[asyncio.AbstractEventLoop, str] = WeakKeyDictionary()
_RO_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()


def _get_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _RO_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _RO_LOCKS[loop] = lock
    return lock


async def _open_readonly_connection(db_path: str) -> aiosqlite.Connection:
    uri = f"file:{db_path}?mode=ro"
    conn = await aiosqlite.connect(uri, uri=True, timeout=_SQLITE_CONNECT_TIMEOUT_SECONDS)
    conn.row_factory = aiosqlite.Row
    await _register_gl_ln(conn)
    await conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    # Belt worn under the authorizer's suspenders: even if the authorizer were
    # ever misconfigured, the connection itself refuses to write.
    await conn.execute("PRAGMA query_only=ON")
    await _install_limits(conn)
    # Installed last: PRAGMA above must run before the authorizer exists, since
    # PRAGMA is not in the allowlist and would otherwise deny itself.
    await _install_authorizer(conn)
    return conn


async def _get_readonly_connection() -> aiosqlite.Connection:
    loop = asyncio.get_running_loop()
    db_path = _db_path()
    conn = _RO_CONNECTIONS.get(loop)
    if conn is not None:
        if _RO_CONNECTION_PATHS.get(loop) == db_path:
            return conn
        # DATABASE_URL changed under this loop (only ever happens in tests
        # that repoint the DB mid-run) — the cached connection points at the
        # wrong file; drop it and open a fresh one against the new path.
        await conn.close()
    conn = await _open_readonly_connection(db_path)
    _RO_CONNECTIONS[loop] = conn
    _RO_CONNECTION_PATHS[loop] = db_path
    return conn


async def close_readonly_connection() -> None:
    """Close and forget this loop's read-only connection, if one is open.

    Not needed in production (the connection lives for the process/loop's
    lifetime), but tests that swap DATABASE_URL between cases call this to
    avoid leaking a file handle onto a temp DB that's about to be deleted.
    """
    loop = asyncio.get_running_loop()
    conn = _RO_CONNECTIONS.pop(loop, None)
    _RO_CONNECTION_PATHS.pop(loop, None)
    if conn is not None:
        await conn.close()


async def execute_readonly_query(
    sql: str,
    *,
    row_limit: int,
    timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
) -> tuple[list[str], list[tuple], bool]:
    """Run one read-only statement on the dedicated ro connection.

    Fetches ``row_limit + 1`` rows so the caller can detect truncation without
    rewriting the SQL to inject a LIMIT clause. Returns
    ``(columns, rows, truncated)``. Raises ``sqlite3.Error`` subclasses on
    failure (denied statement, syntax error, timeout) — callers (tools/query.py)
    catch these and turn them into the tool's error response shape; nothing
    here raises ToolError or any MCP-layer exception.

    Execution is serialized per event loop via a lock: the connection's
    query_only/progress-handler state is shared, so two overlapping calls
    stomping each other's deadline would be a real (if narrow) bug.
    """
    keyword = _first_keyword(sql)
    if keyword not in ALLOWED_FIRST_KEYWORDS:
        raise sqlite3.OperationalError(
            f"only SELECT/WITH/EXPLAIN/VALUES statements are allowed "
            f"(first keyword was {keyword or '<empty>'!r})"
        )

    async with _get_lock():
        conn = await _get_readonly_connection()
        deadline = time.monotonic() + timeout_seconds
        await _install_progress_handler(conn, deadline)
        try:
            async with conn.execute(sql) as cursor:
                columns = [d[0] for d in cursor.description] if cursor.description else []
                fetched = list(await cursor.fetchmany(row_limit + 1))
        finally:
            await _clear_progress_handler(conn)

    truncated = len(fetched) > row_limit
    rows = [tuple(row) for row in fetched[:row_limit]]
    return columns, rows, truncated
