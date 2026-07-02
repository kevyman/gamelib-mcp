"""Connection pooling for get_db(): opt-in, checkout-exclusive, loop-scoped."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

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
        ):
            with self.assertRaises(RuntimeError):
                async with lifespan(object()):
                    pass  # pragma: no cover - should never reach the yield

        # Pooling must be disabled: two subsequent get_db() calls each get a
        # fresh connection instead of reusing one from a leaked pool.
        async with db_module.get_db() as first:
            pass
        async with db_module.get_db() as second:
            pass
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
