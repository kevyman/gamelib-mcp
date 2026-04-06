import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

from gamelib_mcp.main import _ensure_startup_refresh, _run_startup_refresh, lifespan


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

    async def test_run_startup_refresh_records_success_state(self) -> None:
        refresh_result = {"steam": {"games_upserted": 3, "synced_at": "2026-04-07T00:00:00+00:00"}}

        with (
            patch("gamelib_mcp.data.db.set_meta_many", AsyncMock()) as mock_set_meta_many,
            patch("gamelib_mcp.main._admin_refresh_library", AsyncMock(return_value=refresh_result)),
        ):
            await _run_startup_refresh()

        self.assertEqual(len(mock_set_meta_many.await_args_list), 2)
        started = mock_set_meta_many.await_args_list[0].args[0]
        finished = mock_set_meta_many.await_args_list[1].args[0]
        self.assertEqual(started["library_sync_status"], "in_progress")
        self.assertIsNone(started["library_sync_error"])
        self.assertEqual(finished["library_sync_status"], "idle")
        self.assertIsNone(finished["library_sync_error"])
        self.assertIn("library_sync_finished_at", finished)

    async def test_run_startup_refresh_records_partial_failure_summary(self) -> None:
        refresh_result = {
            "steam": {"games_upserted": 3, "synced_at": "2026-04-07T00:00:00+00:00"},
            "epic": {"error": "legendary unavailable"},
            "ps5": {"error": "network timeout"},
        }

        with (
            patch("gamelib_mcp.data.db.set_meta_many", AsyncMock()) as mock_set_meta_many,
            patch("gamelib_mcp.main._admin_refresh_library", AsyncMock(return_value=refresh_result)),
        ):
            await _run_startup_refresh()

        finished = mock_set_meta_many.await_args_list[1].args[0]
        self.assertEqual(finished["library_sync_status"], "idle")
        self.assertEqual(
            finished["library_sync_error"],
            "epic: legendary unavailable; ps5: network timeout",
        )

    async def test_run_startup_refresh_records_exception_failure(self) -> None:
        with (
            patch("gamelib_mcp.data.db.set_meta_many", AsyncMock()) as mock_set_meta_many,
            patch("gamelib_mcp.main._admin_refresh_library", AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            await _run_startup_refresh()

        finished = mock_set_meta_many.await_args_list[1].args[0]
        self.assertEqual(finished["library_sync_status"], "idle")
        self.assertEqual(finished["library_sync_error"], "boom")

    async def test_run_startup_refresh_records_cancellation_cleanup(self) -> None:
        started = asyncio.Event()

        async def blocked_refresh() -> dict:
            started.set()
            await asyncio.Future()
            return {"steam": {"games_upserted": 0}}

        with (
            patch("gamelib_mcp.data.db.set_meta_many", AsyncMock()) as mock_set_meta_many,
            patch("gamelib_mcp.main._admin_refresh_library", AsyncMock(side_effect=blocked_refresh)),
        ):
            task = asyncio.create_task(_run_startup_refresh())
            await asyncio.wait_for(started.wait(), timeout=0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        finished = mock_set_meta_many.await_args_list[1].args[0]
        self.assertEqual(finished["library_sync_status"], "idle")
        self.assertEqual(finished["library_sync_error"], "cancelled")

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
