import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import admin as admin_tools
from gamelib_mcp.main import (
    _drain_background_enrich_reruns,
    _ensure_periodic_refresh_loop,
    _schedule_background_enrich,
    _ensure_startup_refresh,
    _run_periodic_refresh_loop,
    _run_startup_refresh,
    lifespan,
)


class StartupSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        import gamelib_mcp.main as main_module

        task = main_module._LIBRARY_REFRESH_TASK
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        main_module._LIBRARY_REFRESH_TASK = None

        periodic_task = getattr(main_module, "_PERIODIC_REFRESH_TASK", None)
        if periodic_task is not None and not periodic_task.done():
            periodic_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await periodic_task
        main_module._PERIODIC_REFRESH_TASK = None
        enrich_task = getattr(main_module, "_ENRICHMENT_TASK", None)
        if enrich_task is not None and not enrich_task.done():
            enrich_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await enrich_task
        main_module._ENRICHMENT_TASK = None
        main_module._ENRICHMENT_RERUN_REQUESTED = False

    async def test_stale_startup_schedules_background_refresh(self) -> None:
        stale_at = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()

        with (
            patch("gamelib_mcp.data.db.init_db", AsyncMock()),
            patch("gamelib_mcp.data.db.clear_all_enrichment_claims", AsyncMock()),
            patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=stale_at)),
            patch("gamelib_mcp.main._ensure_startup_refresh", AsyncMock()) as mock_ensure_refresh,
            patch("gamelib_mcp.main._run_background_enrich", AsyncMock()),
        ):
            async with lifespan(object()):
                pass

        mock_ensure_refresh.assert_awaited_once()

    def test_main_defers_lock_creation_until_runtime(self) -> None:
        import gamelib_mcp.main as main_module

        self.assertIsNone(main_module._LIBRARY_REFRESH_LOCK)
        self.assertIsNone(main_module._PERIODIC_REFRESH_LOCK)
        self.assertIsNone(main_module._ENRICHMENT_LOCK)

    async def test_run_startup_refresh_records_success_state(self) -> None:
        refresh_result = {"steam": {"games_upserted": 3, "synced_at": "2026-04-07T00:00:00+00:00"}}

        with (
            patch("gamelib_mcp.data.db.set_meta_many", AsyncMock()) as mock_set_meta_many,
            patch("gamelib_mcp.main._admin_refresh_library", AsyncMock(return_value=refresh_result)),
            patch("gamelib_mcp.main._drain_background_enrich_reruns", AsyncMock()) as mock_drain,
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
        mock_drain.assert_awaited_once()

    async def test_run_startup_refresh_records_partial_failure_summary(self) -> None:
        refresh_result = {
            "steam": {"games_upserted": 3, "synced_at": "2026-04-07T00:00:00+00:00"},
            "epic": {"error": "legendary unavailable"},
            "ps5": {"error": "network timeout"},
        }

        with (
            patch("gamelib_mcp.data.db.set_meta_many", AsyncMock()) as mock_set_meta_many,
            patch("gamelib_mcp.main._admin_refresh_library", AsyncMock(return_value=refresh_result)),
            patch("gamelib_mcp.main._drain_background_enrich_reruns", AsyncMock()),
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
            patch("gamelib_mcp.main._drain_background_enrich_reruns", AsyncMock()) as mock_drain,
        ):
            await _run_startup_refresh()

        finished = mock_set_meta_many.await_args_list[1].args[0]
        self.assertEqual(finished["library_sync_status"], "idle")
        self.assertEqual(finished["library_sync_error"], "boom")
        mock_drain.assert_not_awaited()

    async def test_run_startup_refresh_records_cancellation_cleanup(self) -> None:
        started = asyncio.Event()

        async def blocked_refresh(*_args, **_kwargs) -> dict:
            started.set()
            await asyncio.Future()
            return {"steam": {"games_upserted": 0}}

        with (
            patch("gamelib_mcp.data.db.set_meta_many", AsyncMock()) as mock_set_meta_many,
            patch("gamelib_mcp.main._admin_refresh_library", AsyncMock(side_effect=blocked_refresh)),
            patch("gamelib_mcp.main._drain_background_enrich_reruns", AsyncMock()) as mock_drain,
        ):
            task = asyncio.create_task(_run_startup_refresh())
            await asyncio.wait_for(started.wait(), timeout=0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        finished = mock_set_meta_many.await_args_list[1].args[0]
        self.assertEqual(finished["library_sync_status"], "idle")
        self.assertEqual(finished["library_sync_error"], "cancelled")
        mock_drain.assert_not_awaited()

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

    async def test_run_periodic_refresh_loop_waits_then_requests_refresh(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_refresh() -> asyncio.Task:
            started.set()
            await release.wait()
            return asyncio.current_task()

        with patch("gamelib_mcp.main._ensure_startup_refresh", AsyncMock(side_effect=fake_refresh)) as mock_refresh:
            task = asyncio.create_task(_run_periodic_refresh_loop(0.01))
            await asyncio.wait_for(started.wait(), timeout=0.1)
            self.assertEqual(mock_refresh.await_count, 1)

            release.set()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_ensure_periodic_refresh_loop_skips_duplicate_running_task(self) -> None:
        import gamelib_mcp.main as main_module

        running_task = asyncio.create_task(asyncio.sleep(0.2))
        main_module._PERIODIC_REFRESH_TASK = running_task

        with patch("gamelib_mcp.main.asyncio.create_task", AsyncMock()) as mock_create_task:
            task = await _ensure_periodic_refresh_loop(3600)

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
            patch("gamelib_mcp.data.db.clear_all_enrichment_claims", AsyncMock()),
            patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=stale_at)),
            patch("gamelib_mcp.main._run_startup_refresh", side_effect=slow_refresh),
            patch("gamelib_mcp.main._run_background_enrich", AsyncMock()),
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
            patch("gamelib_mcp.data.db.clear_all_enrichment_claims", AsyncMock()),
            patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=stale_at)),
            patch("gamelib_mcp.data.db.set_meta_many", AsyncMock()),
            patch("gamelib_mcp.main._admin_refresh_library", AsyncMock(side_effect=slow_refresh)),
            patch("gamelib_mcp.main._run_background_enrich", AsyncMock(side_effect=fake_enrich)),
        ):
            cm = lifespan(object())
            await asyncio.wait_for(cm.__aenter__(), timeout=0.1)
            await asyncio.wait_for(refresh_started.wait(), timeout=0.1)
            await asyncio.wait_for(enrich_started.wait(), timeout=0.1)
            self.assertFalse(refresh_release.is_set())
            refresh_release.set()
            await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=0.1)

    async def test_stale_startup_requeues_enrichment_after_refresh_finishes(self) -> None:
        stale_at = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        refresh_started = asyncio.Event()
        refresh_release = asyncio.Event()
        first_enrich_started = asyncio.Event()
        second_enrich_started = asyncio.Event()
        enrich_calls = 0

        async def slow_refresh(*_args, **_kwargs) -> dict:
            refresh_started.set()
            await refresh_release.wait()
            return {"steam": {"games_upserted": 1}}

        async def fake_enrich() -> None:
            nonlocal enrich_calls
            enrich_calls += 1
            if enrich_calls == 1:
                first_enrich_started.set()
                return
            second_enrich_started.set()

        with (
            patch("gamelib_mcp.data.db.init_db", AsyncMock()),
            patch("gamelib_mcp.data.db.clear_all_enrichment_claims", AsyncMock()),
            patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=stale_at)),
            patch("gamelib_mcp.data.db.set_meta_many", AsyncMock()),
            patch("gamelib_mcp.main._admin_refresh_library", AsyncMock(side_effect=slow_refresh)),
            patch("gamelib_mcp.main._run_background_enrich", AsyncMock(side_effect=fake_enrich)),
        ):
            cm = lifespan(object())
            await asyncio.wait_for(cm.__aenter__(), timeout=0.1)
            await asyncio.wait_for(refresh_started.wait(), timeout=0.1)
            await asyncio.wait_for(first_enrich_started.wait(), timeout=0.1)

            refresh_release.set()
            await asyncio.wait_for(second_enrich_started.wait(), timeout=0.1)
            await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=0.1)

        self.assertEqual(enrich_calls, 2)

    async def test_drain_background_enrich_reruns_requeues_after_active_run(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        enrich_calls = 0

        async def fake_enrich() -> None:
            nonlocal enrich_calls
            enrich_calls += 1
            if enrich_calls == 1:
                started.set()
                await release.wait()

        with patch("gamelib_mcp.main._run_background_enrich", AsyncMock(side_effect=fake_enrich)):
            first_task = await _schedule_background_enrich()
            await asyncio.wait_for(started.wait(), timeout=0.1)
            second_task = await _schedule_background_enrich()

            self.assertIs(first_task, second_task)

            drain_task = asyncio.create_task(_drain_background_enrich_reruns())
            release.set()
            await asyncio.wait_for(drain_task, timeout=0.1)

        self.assertEqual(enrich_calls, 2)

    async def test_lifespan_starts_periodic_refresh_loop(self) -> None:
        fresh_at = datetime.now(timezone.utc).isoformat()

        with (
            patch("gamelib_mcp.data.db.init_db", AsyncMock()),
            patch("gamelib_mcp.data.db.clear_all_enrichment_claims", AsyncMock()),
            patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=fresh_at)),
            patch("gamelib_mcp.main._ensure_periodic_refresh_loop", AsyncMock()) as mock_periodic,
            patch("gamelib_mcp.main._run_background_enrich", AsyncMock()),
        ):
            async with lifespan(object()):
                pass

        mock_periodic.assert_awaited_once()

    async def test_lifespan_cancels_periodic_refresh_loop_on_shutdown(self) -> None:
        fresh_at = datetime.now(timezone.utc).isoformat()
        started = asyncio.Event()

        async def idle_forever() -> None:
            started.set()
            await asyncio.Future()

        with (
            patch("gamelib_mcp.data.db.init_db", AsyncMock()),
            patch("gamelib_mcp.data.db.clear_all_enrichment_claims", AsyncMock()),
            patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=fresh_at)),
            patch("gamelib_mcp.main._run_background_enrich", AsyncMock()),
            patch.dict(os.environ, {"LIBRARY_REFRESH_INTERVAL_HOURS": "24"}, clear=False),
        ):
            cm = lifespan(object())
            await asyncio.wait_for(cm.__aenter__(), timeout=0.1)

            import gamelib_mcp.main as main_module

            if main_module._PERIODIC_REFRESH_TASK is not None:
                main_module._PERIODIC_REFRESH_TASK.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await main_module._PERIODIC_REFRESH_TASK
            main_module._PERIODIC_REFRESH_TASK = asyncio.create_task(idle_forever())
            await asyncio.wait_for(started.wait(), timeout=0.1)

            await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=0.1)
            self.assertTrue(main_module._PERIODIC_REFRESH_TASK.done())

    async def test_startup_clears_abandoned_hltb_claims_before_background_enrich(self) -> None:
        fresh_at = datetime.now(timezone.utc).isoformat()
        enrich_started = asyncio.Event()
        call_order: list[str] = []

        async def fake_clear_claims() -> None:
            call_order.append("clear_claims")

        async def fake_enrich() -> None:
            call_order.append("background_enrich")
            enrich_started.set()

        with (
            patch("gamelib_mcp.data.db.init_db", AsyncMock()),
            patch("gamelib_mcp.data.db.clear_all_enrichment_claims", AsyncMock(side_effect=fake_clear_claims)),
            patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=fresh_at)),
            patch("gamelib_mcp.main._ensure_periodic_refresh_loop", AsyncMock(return_value=None)),
            patch("gamelib_mcp.main._run_background_enrich", AsyncMock(side_effect=fake_enrich)),
        ):
            async with lifespan(object()):
                await asyncio.wait_for(enrich_started.wait(), timeout=0.1)

        self.assertEqual(call_order[:2], ["clear_claims", "background_enrich"])

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
            patch("gamelib_mcp.tools.admin.detect_farmed_games", AsyncMock(return_value={"candidates": 0})) as mock_detect,
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
        mock_detect.assert_awaited_once_with(dry_run=False)

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
            patch("gamelib_mcp.tools.admin.detect_farmed_games", AsyncMock(return_value={"candidates": 0})) as mock_detect,
        ):
            result = await admin_tools.refresh_library(["steam", "epic"])

        self.assertEqual(
            result,
            {
                "steam": {"platform": "steam", "synced": True},
                "epic": {"error": "epic cancelled"},
            },
        )
        mock_detect.assert_awaited_once_with(dry_run=False)

    async def test_refresh_library_runs_farm_detection_after_successful_steam_sync(self) -> None:
        refresh_result = {"platform": "steam", "synced": True}

        with (
            patch("gamelib_mcp.tools.admin.fetch_library", AsyncMock(return_value=refresh_result)),
            patch("gamelib_mcp.tools.admin.detect_farmed_games", AsyncMock(return_value={"candidates": 3})) as mock_detect,
        ):
            result = await admin_tools.refresh_library(["steam"])

        self.assertEqual(result, {"steam": refresh_result})
        mock_detect.assert_awaited_once_with(dry_run=False)

    async def test_refresh_library_skips_farm_detection_without_steam(self) -> None:
        with (
            patch("gamelib_mcp.tools.admin.sync_epic", AsyncMock(return_value={"platform": "epic", "synced": True})),
            patch("gamelib_mcp.tools.admin.detect_farmed_games", AsyncMock()) as mock_detect,
        ):
            result = await admin_tools.refresh_library(["epic"])

        self.assertEqual(result, {"epic": {"platform": "epic", "synced": True}})
        mock_detect.assert_not_awaited()

    async def test_refresh_library_supports_switch2_alias(self) -> None:
        switch_result = {"platform": "switch2", "synced": True}

        with (
            patch("gamelib_mcp.tools.admin.sync_nintendo", AsyncMock(return_value=switch_result)) as mock_sync,
            patch("gamelib_mcp.tools.admin.detect_farmed_games", AsyncMock()) as mock_detect,
        ):
            result = await admin_tools.refresh_library(["switch2"])

        self.assertEqual(result, {"switch2": switch_result})
        mock_sync.assert_awaited_once()
        mock_detect.assert_not_awaited()

    async def test_refresh_library_ignores_farm_detection_failures(self) -> None:
        refresh_result = {"platform": "steam", "synced": True}

        with (
            patch("gamelib_mcp.tools.admin.fetch_library", AsyncMock(return_value=refresh_result)),
            patch("gamelib_mcp.tools.admin.detect_farmed_games", AsyncMock(side_effect=RuntimeError("detector boom"))) as mock_detect,
        ):
            result = await admin_tools.refresh_library(["steam"])

        self.assertEqual(result, {"steam": refresh_result})
        mock_detect.assert_awaited_once_with(dry_run=False)

    async def test_db_path_reads_database_url_at_call_time(self) -> None:
        with patch.dict(os.environ, {"DATABASE_URL": "file:./first.db"}, clear=False):
            self.assertEqual(db_module._db_path(), "./first.db")

        with patch.dict(os.environ, {"DATABASE_URL": "file:./second.db"}, clear=False):
            self.assertEqual(db_module._db_path(), "./second.db")

    def test_db_path_prefers_database_url(self):
        with patch.dict(os.environ, {"DATABASE_URL": "file:./data/gamelib.db"}, clear=False):
            self.assertEqual(db_module._db_path(), "./data/gamelib.db")

    def test_db_path_defaults_to_project_data_db(self):
        db_module._ENV_LOADED = False
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("gamelib_mcp.data.db.load_dotenv", return_value=False),
        ):
            self.assertEqual(db_module._db_path(), "data/gamelib.db")

    def test_db_path_loads_database_url_from_dotenv(self) -> None:
        db_module._ENV_LOADED = False
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("gamelib_mcp.data.db.load_dotenv") as load_dotenv,
        ):
            def fake_load_dotenv(path: str | None = None, *args, **kwargs) -> bool:
                os.environ["DATABASE_URL"] = "file:./from-dotenv.db"
                return True

            load_dotenv.side_effect = fake_load_dotenv
            self.assertEqual(db_module._db_path(), "./from-dotenv.db")

    def test_db_path_ignores_legacy_root_db_files(self) -> None:
        db_module._ENV_LOADED = False
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("gamelib_mcp.data.db.load_dotenv", return_value=False),
            # os.path.exists is NOT called — the function ignores legacy root-level files unconditionally
        ):
            self.assertEqual(db_module._db_path(), "data/gamelib.db")

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
