import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import AsyncMock, patch

from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import admin as admin_tools
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

        async def blocked_refresh(*_args, **_kwargs) -> dict:
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

    async def test_stale_startup_starts_enrichment_without_waiting_for_refresh(self) -> None:
        stale_at = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        refresh_started = asyncio.Event()
        refresh_release = asyncio.Event()
        enrich_started = asyncio.Event()

        async def slow_refresh(*_args, **_kwargs) -> dict:
            refresh_started.set()
            await refresh_release.wait()
            return {"steam": {"games_upserted": 1}}

        async def fake_enrich() -> None:
            enrich_started.set()

        with (
            patch("gamelib_mcp.data.db.init_db", AsyncMock()),
            patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=stale_at)),
            patch("gamelib_mcp.data.db.set_meta_many", AsyncMock()),
            patch("gamelib_mcp.main._admin_refresh_library", AsyncMock(side_effect=slow_refresh)),
            patch("gamelib_mcp.data.enrich_bg.background_enrich", AsyncMock(side_effect=fake_enrich)),
        ):
            cm = lifespan(object())
            await asyncio.wait_for(cm.__aenter__(), timeout=0.1)
            await asyncio.wait_for(refresh_started.wait(), timeout=0.1)
            await asyncio.wait_for(enrich_started.wait(), timeout=0.1)
            self.assertFalse(refresh_release.is_set())
            refresh_release.set()
            await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=0.1)

    async def test_refresh_library_parallelizes_platform_syncs_and_isolates_failures(self) -> None:
        started = {
            "steam": asyncio.Event(),
            "epic": asyncio.Event(),
            "gog": asyncio.Event(),
            "nintendo": asyncio.Event(),
            "ps5": asyncio.Event(),
        }
        release = asyncio.Event()

        async def make_sync(name: str, result: dict | None = None, error: Exception | None = None) -> dict:
            started[name].set()
            await release.wait()
            if error is not None:
                raise error
            return result or {"platform": name, "synced": True}

        async def steam_sync() -> dict:
            return await make_sync("steam", {"platform": "steam", "synced": True})

        async def epic_sync() -> dict:
            return await make_sync("epic", error=RuntimeError("epic boom"))

        async def gog_sync() -> dict:
            return await make_sync("gog", {"platform": "gog", "synced": True})

        async def nintendo_sync() -> dict:
            return await make_sync("nintendo", {"platform": "nintendo", "synced": True})

        async def psn_sync() -> dict:
            return await make_sync("ps5", {"platform": "ps5", "synced": True})

        with (
            patch("gamelib_mcp.tools.admin.fetch_library", AsyncMock(side_effect=steam_sync)),
            patch("gamelib_mcp.tools.admin.sync_epic", AsyncMock(side_effect=epic_sync)),
            patch("gamelib_mcp.tools.admin.sync_gog", AsyncMock(side_effect=gog_sync)),
            patch("gamelib_mcp.tools.admin.sync_nintendo", AsyncMock(side_effect=nintendo_sync)),
            patch("gamelib_mcp.tools.admin.sync_psn", AsyncMock(side_effect=psn_sync)),
        ):
            refresh_task = asyncio.create_task(admin_tools.refresh_library())
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in started.values())),
                timeout=0.1,
            )

            release.set()
            result = await asyncio.wait_for(refresh_task, timeout=0.1)

        self.assertEqual(
            result,
            {
                "steam": {"platform": "steam", "synced": True},
                "epic": {"error": "epic boom"},
                "gog": {"platform": "gog", "synced": True},
                "nintendo": {"platform": "nintendo", "synced": True},
                "ps5": {"platform": "ps5", "synced": True},
            },
        )

    async def test_refresh_library_normalizes_baseexception_results(self) -> None:
        class PlatformAborted(BaseException):
            pass

        async def steam_sync() -> dict:
            return {"platform": "steam", "synced": True}

        async def epic_sync() -> dict:
            raise PlatformAborted("epic cancelled")

        with (
            patch("gamelib_mcp.tools.admin.fetch_library", AsyncMock(side_effect=steam_sync)),
            patch("gamelib_mcp.tools.admin.sync_epic", AsyncMock(side_effect=epic_sync)),
        ):
            result = await admin_tools.refresh_library(["steam", "epic"])

        self.assertEqual(
            result,
            {
                "steam": {"platform": "steam", "synced": True},
                "epic": {"error": "epic cancelled"},
            },
        )

    async def test_db_path_reads_database_url_at_call_time(self) -> None:
        with patch.dict(os.environ, {"DATABASE_URL": "file:./first.db"}, clear=False):
            self.assertEqual(db_module._db_path(), "./first.db")

        with patch.dict(os.environ, {"DATABASE_URL": "file:./second.db"}, clear=False):
            self.assertEqual(db_module._db_path(), "./second.db")

    async def test_db_path_defaults_to_gamelib_name_with_legacy_fallback(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("gamelib_mcp.data.db.os.path.exists", side_effect=lambda path: path == "steam.db"),
        ):
            self.assertEqual(db_module._db_path(), "steam.db")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("gamelib_mcp.data.db.os.path.exists", return_value=False),
        ):
            self.assertEqual(db_module._db_path(), "gamelib.db")

    async def test_refresh_library_reuses_running_startup_refresh_task(self) -> None:
        import gamelib_mcp.main as main_module

        release = asyncio.Event()
        refresh_result = {"steam": {"games_upserted": 3}}

        async def running_refresh() -> dict:
            await release.wait()
            return refresh_result

        startup_task = asyncio.create_task(running_refresh())
        main_module._LIBRARY_REFRESH_TASK = startup_task

        with patch("gamelib_mcp.tools.admin.fetch_library", AsyncMock()) as mock_fetch_library:
            refresh_call = asyncio.create_task(admin_tools.refresh_library())
            await asyncio.sleep(0)
            self.assertFalse(refresh_call.done())

            release.set()
            result = await asyncio.wait_for(refresh_call, timeout=0.1)

        self.assertEqual(result, refresh_result)
        mock_fetch_library.assert_not_called()


if __name__ == "__main__":
    unittest.main()
