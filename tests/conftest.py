"""Shared harness + seed helpers for tool characterization tests.

No pytest-asyncio is installed, so these tests follow the repo's existing
pattern (``unittest.IsolatedAsyncioTestCase`` over a real temp SQLite DB with
HTTP mocked). ``ToolDBTestCase`` points ``DATABASE_URL`` at a throwaway file and
runs the real migrations via ``init_db``; the ``seed_*`` helpers below write rows
through the production upsert/SQL paths so tests exercise real schema behavior.

pytest's default ``prepend`` import mode puts ``tests/`` on ``sys.path`` (no
``__init__.py`` here), so other test modules can ``from conftest import ...``.
"""

import asyncio
import json
import os
import shutil
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

# Importing gamelib_mcp.main builds the process-lifetime security configuration.
# Tests deliberately run without interactive OAuth, but must still opt out
# explicitly so production cannot become unauthenticated through omission.
os.environ.setdefault("MCP_AUTH_MODE", "disabled")
os.environ.setdefault("MCP_ADMIN_AUTH_TOKEN", "test-admin-token-at-least-32-characters")
os.environ.setdefault("FASTMCP_HOME", "/tmp/gamelib-mcp-fastmcp-tests")

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.db import readonly
from gamelib_mcp.data.title_normalization import normalize_search_text


@pytest.fixture(scope="session", autouse=True)
def _throwaway_default_database():
    """Point the session's default DATABASE_URL at a temp file.

    A few sync tests exercise production code paths whose unmocked calls reach
    get_db(); without this guard they materialize the real default DB
    (data/gamelib.db) in the working tree — or worse, write into a developer's
    dev database if .env points DATABASE_URL at one. Tests that manage their
    own DATABASE_URL (e.g. ToolDBTestCase) simply override this value.
    """
    prev = os.environ.get("DATABASE_URL")
    with TemporaryDirectory() as tmpdir:
        os.environ["DATABASE_URL"] = f"file:{Path(tmpdir) / 'session-default.sqlite'}"
        db_module._DB_READY_PATH = None
        try:
            yield
        finally:
            db_module._DB_READY_PATH = None
            if prev is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = prev


@pytest.fixture(scope="session", autouse=True)
def _no_leaked_sqlite_worker_threads():
    """Fail the run if an aiosqlite worker thread outlives the test session.

    aiosqlite runs each connection on a non-daemon thread, so one unclosed
    connection means threading._shutdown blocks forever and pytest never exits
    — after reporting every test as passed. CI has no way to tell that apart
    from a slow job, and it burned a full six-hour job timeout before anyone
    looked. Checking here turns a silent hang into a named failure.
    """
    yield
    # A connection closed at the very end of the last test can still be winding
    # its thread down; only a thread that survives the grace period is a leak.
    deadline = time.monotonic() + 5.0
    while True:
        leaked = [
            t.name for t in threading.enumerate()
            if "_connection_worker_thread" in t.name and not t.daemon
        ]
        if not leaked or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert not leaked, (
        f"aiosqlite worker thread(s) still alive after the session: {leaked}. "
        "Some connection was never closed; the interpreter will hang at exit. "
        "Close it in the owning test's teardown (see ToolDBTestCase)."
    )


class ToolDBTestCase(unittest.IsolatedAsyncioTestCase):
    """Base case giving each test an isolated, migrated SQLite database."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "tools.sqlite"
        self._prev_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"file:{self._db_path}"
        db_module._DB_READY_PATH = None
        await db_module.init_db()

    async def asyncTearDown(self) -> None:
        # The read-only connection is a per-event-loop singleton, and
        # IsolatedAsyncioTestCase gives every test its own loop. Left open, the
        # aiosqlite worker thread behind it is NOT a daemon thread: it outlives
        # the loop, and threading._shutdown joins it forever at interpreter
        # exit. That is what wedged CI for six hours after a green run — the
        # tests all passed, then pytest never exited. Closing it here rather
        # than in the one test module that first hit it keeps the next test
        # that touches query_library from re-introducing the hang.
        await readonly.close_readonly_connection()
        db_module._DB_READY_PATH = None
        if self._prev_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prev_database_url
        # A test that cancelled an in-flight background task (e.g. the refresh
        # ack tests) can race cleanup: the task's aiosqlite worker thread may
        # recreate WAL/SHM files between rmtree's listing and rmdir, failing the
        # whole test on "directory not empty". Give stragglers a beat, then stop
        # letting temp-dir residue fail an otherwise-passing test.
        for _ in range(3):
            try:
                self._tmpdir.cleanup()
                return
            except OSError:
                await asyncio.sleep(0.05)
        shutil.rmtree(self._tmpdir.name, ignore_errors=True)



# --- seed helpers (write through production paths) ---------------------------

async def seed_game(
    name: str,
    *,
    tags: list[str] | None = None,
    genres: list[str] | None = None,
    hltb_main: float | None = None,
    hltb_extra: float | None = None,
    hltb_complete: float | None = None,
    is_farmed: int = 0,
    release_date: str | None = None,
    short_description: str | None = None,
    content_type: str | None = None,
    parent_game_id: int | None = None,
    is_primary_library_item: int | None = None,
) -> int:
    """Create a canonical games row and return its id."""
    fields: dict = {"is_farmed": is_farmed}
    if tags is not None:
        fields["tags"] = json.dumps(tags)
    if genres is not None:
        fields["genres"] = json.dumps(genres)
    if hltb_main is not None:
        fields["hltb_main"] = hltb_main
    if hltb_extra is not None:
        fields["hltb_extra"] = hltb_extra
    if hltb_complete is not None:
        fields["hltb_complete"] = hltb_complete
    if release_date is not None:
        fields["release_date"] = release_date
    if short_description is not None:
        fields["short_description"] = short_description
    game_id = await db_module.upsert_game(None, name, **fields)
    related_updates = {
        "content_type": content_type,
        "parent_game_id": parent_game_id,
        "is_primary_library_item": is_primary_library_item,
    }
    related_updates = {key: value for key, value in related_updates.items() if value is not None}
    if related_updates:
        cols_sql = ", ".join(f"{column} = ?" for column in related_updates)
        async with db_module.get_db() as db:
            await db.execute(
                f"UPDATE games SET {cols_sql} WHERE id = ?",
                (*related_updates.values(), game_id),
            )
            await db.commit()
    return game_id


async def add_game_alias(
    game_id: int,
    alias: str,
    *,
    alias_type: str = "edition",
    source: str | None = None,
    source_key: str | None = None,
) -> None:
    async with db_module.get_db() as db:
        await db.execute(
            """INSERT INTO game_aliases
               (game_id, alias, alias_normalized, alias_type, source, source_key)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (game_id, alias, normalize_search_text(alias), alias_type, source, source_key),
        )
        await db.commit()


async def add_platform(
    game_id: int,
    platform: str,
    *,
    playtime_minutes: int | None = None,
    playtime_2weeks_minutes: int | None = None,
    owned: int = 1,
) -> int:
    """Attach a platform to a game and return the game_platform id."""
    return await db_module.upsert_game_platform(
        game_id,
        platform,
        playtime_minutes=playtime_minutes,
        playtime_2weeks_minutes=playtime_2weeks_minutes,
        owned=owned,
    )


async def add_identifier(
    game_platform_id: int,
    identifier_type: str,
    identifier_value: str | int,
    *,
    is_primary: bool = True,
) -> None:
    await db_module.upsert_game_platform_identifier(
        game_platform_id, identifier_type, identifier_value, is_primary=is_primary
    )


async def add_steam_appid(game_platform_id: int, appid: int) -> None:
    await add_identifier(game_platform_id, db_module.STEAM_APP_ID, appid)


async def add_steam_data(game_platform_id: int, **fields) -> None:
    await db_module.upsert_steam_platform_data(game_platform_id, **fields)


async def add_enrichment(game_platform_id: int, **fields) -> None:
    await db_module.upsert_game_platform_enrichment(game_platform_id, **fields)


async def add_rating(
    game_id: int,
    source: str,
    raw_score: float,
    normalized_score: float,
    review_text: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with db_module.get_db() as db:
        await db.execute(
            """INSERT INTO ratings
               (game_id, source, raw_score, normalized_score, review_text, synced_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (game_id, source, raw_score, normalized_score, review_text, now),
        )
        await db.commit()


async def set_tag_affinity(
    tag: str,
    affinity_score: float,
    avg_score: float,
    game_count: int,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with db_module.get_db() as db:
        await db.execute(
            """INSERT INTO tag_affinity (tag, affinity_score, avg_score, game_count, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (tag, affinity_score, avg_score, game_count, now),
        )
        await db.commit()


async def make_steam_game(
    name: str,
    appid: int,
    *,
    playtime_minutes: int | None = None,
    playtime_2weeks_minutes: int | None = None,
    tags: list[str] | None = None,
    genres: list[str] | None = None,
    hltb_main: float | None = None,
    is_farmed: int = 0,
    metacritic_score: int | None = None,
    opencritic_score: int | None = None,
    protondb_tier: str | None = None,
    steam_review_desc: str | None = None,
    steam_review_score: int | None = None,
    rtime_last_played: int | None = None,
) -> int:
    """Convenience: a Steam-owned game with optional enrichment, returns game_id."""
    game_id = await seed_game(
        name,
        tags=tags,
        genres=genres,
        hltb_main=hltb_main,
        is_farmed=is_farmed,
    )
    gpid = await add_platform(
        game_id,
        "steam",
        playtime_minutes=playtime_minutes,
        playtime_2weeks_minutes=playtime_2weeks_minutes,
    )
    await add_steam_appid(gpid, appid)
    steam_fields: dict = {}
    if protondb_tier is not None:
        steam_fields["protondb_tier"] = protondb_tier
    if steam_review_desc is not None:
        steam_fields["steam_review_desc"] = steam_review_desc
    if steam_review_score is not None:
        steam_fields["steam_review_score"] = steam_review_score
    if rtime_last_played is not None:
        steam_fields["rtime_last_played"] = rtime_last_played
    if steam_fields:
        await add_steam_data(gpid, **steam_fields)
    enrichment_fields: dict = {}
    if metacritic_score is not None:
        enrichment_fields["metacritic_score"] = metacritic_score
    if opencritic_score is not None:
        enrichment_fields["opencritic_score"] = opencritic_score
    if enrichment_fields:
        await add_enrichment(gpid, **enrichment_fields)
    return game_id
