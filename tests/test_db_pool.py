"""Connection pooling for get_db(): opt-in, checkout-exclusive, loop-scoped."""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

from gamelib_mcp.data import db as db_module


class PoolTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._tmp.name, "pool_test.db")
        self._env = patch.dict(os.environ, {"DATABASE_URL": db_path})
        self._env.start()
        db_module._DB_READY_PATH = None

    def tearDown(self):
        self._env.stop()
        db_module._DB_READY_PATH = None
        self._tmp.cleanup()

    async def asyncTearDown(self):
        await db_module.close_db_pool()


class TestPoolDisabledByDefault(PoolTestBase):
    async def test_each_call_gets_a_fresh_connection(self):
        async with db_module.get_db() as first:
            pass
        async with db_module.get_db() as second:
            pass
        self.assertIsNot(first, second)


class TestPoolEnabled(PoolTestBase):
    async def test_sequential_calls_reuse_the_connection(self):
        db_module.enable_db_pooling()
        async with db_module.get_db() as first:
            pass
        async with db_module.get_db() as second:
            pass
        self.assertIs(first, second)

    async def test_concurrent_checkouts_get_distinct_connections(self):
        db_module.enable_db_pooling()
        seen = []
        release = asyncio.Event()

        async def hold():
            async with db_module.get_db() as conn:
                seen.append(conn)
                await release.wait()

        tasks = [asyncio.create_task(hold()) for _ in range(3)]
        while len(seen) < 3:
            await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(len({id(c) for c in seen}), 3)

    async def test_exception_in_block_discards_the_connection(self):
        db_module.enable_db_pooling()
        with self.assertRaises(RuntimeError):
            async with db_module.get_db() as broken:
                raise RuntimeError("boom")
        async with db_module.get_db() as fresh:
            # discarded connection must not be reused, and must be closed
            self.assertIsNot(broken, fresh)
        with self.assertRaises(ValueError):
            await broken.execute("SELECT 1")  # aiosqlite: "no active connection"

    async def test_uncommitted_write_is_rolled_back_on_checkin(self):
        db_module.enable_db_pooling()
        async with db_module.get_db() as conn:
            await conn.execute(
                "INSERT INTO meta (key, value) VALUES ('pool_probe', 'x')"
            )
            # no commit
        async with db_module.get_db() as conn:
            cur = await conn.execute(
                "SELECT value FROM meta WHERE key = 'pool_probe'"
            )
            self.assertIsNone(await cur.fetchone())

    async def test_close_db_pool_disables_and_closes_idle(self):
        db_module.enable_db_pooling()
        async with db_module.get_db() as pooled:
            pass
        await db_module.close_db_pool()
        with self.assertRaises(ValueError):
            await pooled.execute("SELECT 1")
        async with db_module.get_db() as first:
            pass
        async with db_module.get_db() as second:
            pass
        self.assertIsNot(first, second)  # pooling is off again


class TestLifespanStartupFailureDisablesPooling(PoolTestBase):
    async def test_pooling_disabled_if_startup_raises_before_yield(self):
        # enable_db_pooling() runs early in lifespan(), before init_db()/etc.
        # If a later pre-yield startup step raises, teardown (including
        # close_db_pool()) must still run via the finally block, or pooling
        # is left stuck on and pooled connections leak.
        from gamelib_mcp.lifecycle import lifespan

        with patch(
            "gamelib_mcp.data.db.init_db",
            AsyncMock(side_effect=RuntimeError("boom")),
        ), self.assertRaises(RuntimeError):
            async with lifespan(object()):
                pass  # pragma: no cover - should never reach the yield

        # Pooling must be disabled: two subsequent get_db() calls each get a
        # fresh connection instead of reusing one from a leaked pool.
        async with db_module.get_db() as first:
            pass
        async with db_module.get_db() as second:
            pass
        self.assertIsNot(first, second)


class TestWriteContentionRetry(PoolTestBase):
    """SQLITE_BUSY_SNAPSHOT ignores busy_timeout, so the only fix is a retry.

    A transaction that read the main database and then tries to write it after
    another connection committed fails IMMEDIATELY with "database is locked" —
    no waiting is attempted, because a stale read snapshot cannot be extended.
    That is the shape that failed a production Steam sync silently for 3 days.
    """

    async def test_retries_a_locked_write_until_it_succeeds(self):
        attempts = []
        sleeps = []

        @db_module.retry_on_write_contention
        async def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "written"

        with patch("gamelib_mcp.data.db.asyncio.sleep", AsyncMock(side_effect=lambda d: sleeps.append(d))):
            self.assertEqual(await flaky(), "written")

        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [0.1, 0.2])

    async def test_gives_up_and_re_raises_rather_than_hanging(self):
        @db_module.retry_on_write_contention
        async def always_locked():
            raise sqlite3.OperationalError("database is locked")

        with (
            patch("gamelib_mcp.data.db.asyncio.sleep", AsyncMock()),
            self.assertRaises(sqlite3.OperationalError),
        ):
            await always_locked()

    async def test_an_unrelated_sqlite_error_is_not_retried(self):
        attempts = []

        @db_module.retry_on_write_contention
        async def broken_sql():
            attempts.append(1)
            raise sqlite3.OperationalError("no such column: nope")

        with self.assertRaises(sqlite3.OperationalError):
            await broken_sql()
        self.assertEqual(len(attempts), 1)

    async def test_a_real_contended_write_survives(self):
        # Not a simulation: a second connection holds an exclusive write
        # transaction while an upsert runs, which is what makes the first
        # attempt fail. WAL + busy_timeout alone do not cover every such case.
        game_id = await db_module.upsert_game(None, "Contended")

        blocker = await aiosqlite.connect(db_module._db_path())
        try:
            await blocker.execute("BEGIN IMMEDIATE")
            await blocker.execute(
                "INSERT INTO games (name) VALUES ('holds the write lock')"
            )

            async def release_soon():
                await asyncio.sleep(0.05)
                await blocker.commit()

            releaser = asyncio.create_task(release_soon())
            gpid = await db_module.upsert_game_platform(game_id, "steam", owned=1)
            await releaser
        finally:
            await blocker.close()

        self.assertIsNotNone(gpid)


if __name__ == "__main__":
    unittest.main()
