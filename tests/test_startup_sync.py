import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

from gamelib_mcp.main import _ensure_startup_refresh, lifespan


class StartupSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        import gamelib_mcp.main as main_module

        task = main_module._LIBRARY_REFRESH_TASK
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        main_module._LIBRARY_REFRESH_TASK = None

    async def test_stale_startup_schedules_background_refresh(self) -> None:
        stale_at = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()

        with (
            patch("gamelib_mcp.data.db.init_db", AsyncMock()),
            patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=stale_at)),
            patch("gamelib_mcp.main._ensure_startup_refresh", AsyncMock()) as mock_ensure_refresh,
            patch("gamelib_mcp.data.enrich_bg.background_enrich", AsyncMock()),
        ):
            async with lifespan(object()):
                pass

        mock_ensure_refresh.assert_awaited_once()

    async def test_ensure_startup_refresh_skips_duplicate_running_task(self) -> None:
        import gamelib_mcp.main as main_module

        running_task = asyncio.create_task(asyncio.sleep(0.2))
        main_module._LIBRARY_REFRESH_TASK = running_task

        with patch("gamelib_mcp.main.asyncio.create_task", AsyncMock()) as mock_create_task:
            task = await _ensure_startup_refresh()

        self.assertIs(task, running_task)
        mock_create_task.assert_not_called()
        running_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running_task

    async def test_startup_is_non_blocking_while_refresh_runs(self) -> None:
        stale_at = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_refresh() -> None:
            started.set()
            await release.wait()

        with (
            patch("gamelib_mcp.data.db.init_db", AsyncMock()),
            patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=stale_at)),
            patch("gamelib_mcp.main._run_startup_refresh", side_effect=slow_refresh),
            patch("gamelib_mcp.data.enrich_bg.background_enrich", AsyncMock()),
        ):
            cm = lifespan(object())
            await asyncio.wait_for(cm.__aenter__(), timeout=0.1)

            await asyncio.wait_for(started.wait(), timeout=0.1)
            self.assertFalse(release.is_set())

            release.set()
            await cm.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
