import asyncio
import contextlib
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from conftest import DEADLOCK_TIMEOUT, ToolDBTestCase

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import enrich_bg, provider_health


class EnrichmentClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "enrich.sqlite"
        db_module._DB_READY_PATH = None
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            await db_module.init_db()

    async def asyncTearDown(self) -> None:
        db_module._DB_READY_PATH = None
        self.tmpdir.cleanup()

    async def test_claim_helper_prevents_double_claim(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            game_id = await db_module.upsert_game(appid=None, name="Portal")
            first = await db_module.claim_game_ids_for_igdb(limit=1, stale_before="1970-01-01T00:00:00+00:00")
            second = await db_module.claim_game_ids_for_igdb(limit=1, stale_before="1970-01-01T00:00:00+00:00")

        self.assertEqual(first, [game_id])
        self.assertEqual(second, [])

    async def test_clear_claim_waits_for_transient_sqlite_lock(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO games (id, name, is_farmed, hltb_claimed_at) VALUES (?, ?, 0, ?)",
            (1, "Portal", "2026-04-07T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

        lock_conn = sqlite3.connect(self.db_path, timeout=5)
        lock_conn.execute("BEGIN IMMEDIATE")
        lock_conn.execute("UPDATE games SET name = ? WHERE id = ?", ("Portal Locked", 1))

        # Fires the instant clear_claim's UPDATE is handed to sqlite, which is
        # the instant it starts contending for the held write lock. Waiting on
        # this instead of a fixed sleep is what makes the test mean something:
        # release the lock too early and clear_claim never meets it, so a
        # regressed clear_claim that no longer waits would pass unnoticed.
        write_attempted = asyncio.Event()
        original_execute = db_module.aiosqlite.Connection.execute

        # Not `async def`: aiosqlite.execute returns a Result, which callers use
        # as an async context manager as well as awaiting it. Wrapping it in a
        # coroutine would quietly break `async with db.execute(...)`.
        def signal_then_execute(self, sql, *args, **kwargs):
            if sql.lstrip().upper().startswith("UPDATE GAMES SET HLTB_CLAIMED_AT"):
                write_attempted.set()
            return original_execute(self, sql, *args, **kwargs)

        # Budgets are sized generously on purpose. A clear_claim that does NOT
        # wait raises "database is locked" immediately whatever they are set to,
        # so a tight budget bought nothing and cost correctness: at 0.3s the
        # lock had to be released within 300ms of the attempt, and a loaded
        # machine overshooting failed a perfectly good clear_claim.
        with (
            patch.dict(
                "os.environ",
                {"DATABASE_URL": f"file:{self.db_path}"},
                clear=False,
            ),
            patch.object(db_module.aiosqlite.Connection, "execute", signal_then_execute),
            patch.object(db_module, "_SQLITE_CONNECT_TIMEOUT_SECONDS", 5.0, create=True),
            patch.object(db_module, "_SQLITE_BUSY_TIMEOUT_MS", 5000, create=True),
        ):
            clear_task = asyncio.create_task(db_module.clear_claim("games", "hltb_claimed_at", 1))
            await asyncio.wait_for(write_attempted.wait(), timeout=DEADLOCK_TIMEOUT)

            # Hold the lock a while longer and require the task to stay parked.
            # This is the assertion the test exists for, and the direction of
            # the timing matters: a clear_claim that does NOT wait raises
            # "database is locked" as soon as sqlite reaches the held lock, so
            # anything still pending at the end of this window is genuinely
            # blocked on it. A slow machine only makes that conclusion safer —
            # unlike releasing the lock after a fixed sleep, which on a slow
            # machine can let the write through before it ever meets the lock.
            finished, _ = await asyncio.wait({clear_task}, timeout=0.2)
            self.assertFalse(
                finished, "clear_claim gave up on the locked database instead of waiting"
            )

            lock_conn.rollback()
            await asyncio.wait_for(clear_task, timeout=DEADLOCK_TIMEOUT)

            async with db_module.get_db() as db:
                row = await db.execute_fetchone("SELECT hltb_claimed_at FROM games WHERE id = ?", (1,))

        lock_conn.close()
        self.assertIsNone(row["hltb_claimed_at"])

    async def test_hltb_claim_helper_reclaims_legacy_failed_rows(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            game_id = await db_module.upsert_game(appid=None, name="Portal")
            platform_id = await db_module.upsert_game_platform(
                game_id=game_id,
                platform="steam",
                playtime_minutes=120,
                owned=1,
            )
            await db_module.upsert_steam_platform_data(
                platform_id,
                store_cached_at="2026-04-07T12:00:00+00:00",
            )
            async with db_module.get_db() as db:
                await db.execute(
                    "UPDATE games SET hltb_cached_at = 'FAILED' WHERE id = ?",
                    (game_id,),
                )
                await db.commit()

            claimed = await db_module.claim_game_ids_for_hltb(
                limit=1,
                stale_before="1970-01-01T00:00:00+00:00",
            )

        self.assertEqual(claimed, [game_id])

    async def test_hltb_claims_farmed_games_after_regular_games(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            farmed_id = await db_module.upsert_game(appid=None, name="Farmed", is_farmed=1)
            regular_id = await db_module.upsert_game(appid=None, name="Regular")

            claimed = await db_module.claim_game_ids_for_hltb(
                limit=2,
                stale_before="1970-01-01T00:00:00+00:00",
            )

        self.assertEqual(claimed, [regular_id, farmed_id])

    async def test_igdb_claims_farmed_games_after_regular_games(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            farmed_id = await db_module.upsert_game(appid=None, name="Farmed", is_farmed=1)
            regular_id = await db_module.upsert_game(appid=None, name="Regular")

            claimed = await db_module.claim_game_ids_for_igdb(
                limit=2,
                stale_before="1970-01-01T00:00:00+00:00",
            )

        self.assertEqual(claimed, [regular_id, farmed_id])

    async def test_steam_claims_include_farmed_games_after_regular_playtime(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            farmed_game_id = await db_module.upsert_game(appid=None, name="Farmed", is_farmed=1)
            farmed_platform_id = await db_module.upsert_game_platform(
                game_id=farmed_game_id,
                platform="steam",
                playtime_minutes=9999,
                owned=1,
            )
            await db_module.upsert_game_platform_identifier(
                farmed_platform_id,
                db_module.STEAM_APP_ID,
                "1",
            )
            await db_module.upsert_steam_platform_data(farmed_platform_id, rtime_last_played=0)

            regular_game_id = await db_module.upsert_game(appid=None, name="Regular")
            regular_platform_id = await db_module.upsert_game_platform(
                game_id=regular_game_id,
                platform="steam",
                playtime_minutes=10,
                owned=1,
            )
            await db_module.upsert_game_platform_identifier(
                regular_platform_id,
                db_module.STEAM_APP_ID,
                "2",
            )
            await db_module.upsert_steam_platform_data(regular_platform_id, rtime_last_played=0)

            stale_before = "1970-01-01T00:00:00+00:00"
            store_claimed = await db_module.claim_steam_platform_ids_for_store(
                limit=2,
                stale_before=stale_before,
            )
            proton_claimed = await db_module.claim_steam_platform_ids_for_protondb(
                limit=2,
                stale_before=stale_before,
            )
            steamspy_claimed = await db_module.claim_steam_platform_ids_for_steamspy(
                limit=2,
                stale_before=stale_before,
            )

        self.assertEqual(store_claimed, [regular_platform_id, farmed_platform_id])
        self.assertEqual(proton_claimed, [regular_platform_id, farmed_platform_id])
        self.assertEqual(steamspy_claimed, [regular_platform_id, farmed_platform_id])

    async def test_review_claims_include_farmed_games_after_regular_playtime(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            farmed_game_id = await db_module.upsert_game(appid=None, name="Farmed", is_farmed=1)
            farmed_platform_id = await db_module.upsert_game_platform(
                game_id=farmed_game_id,
                platform="steam",
                playtime_minutes=9999,
                owned=1,
            )
            regular_game_id = await db_module.upsert_game(appid=None, name="Regular")
            regular_platform_id = await db_module.upsert_game_platform(
                game_id=regular_game_id,
                platform="steam",
                playtime_minutes=10,
                owned=1,
            )

            stale_before = "1970-01-01T00:00:00+00:00"
            opencritic_claimed = await db_module.claim_game_platform_ids_for_opencritic(
                limit=2,
                stale_before=stale_before,
            )
            metacritic_claimed = await db_module.claim_game_platform_ids_for_metacritic(
                limit=2,
                stale_before=stale_before,
            )

        self.assertEqual(opencritic_claimed, [regular_platform_id, farmed_platform_id])
        self.assertEqual(metacritic_claimed, [regular_platform_id, farmed_platform_id])


class BackgroundEnrichmentSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_until_quiescent_does_not_claim_new_work_while_paused(self) -> None:
        run_batch = AsyncMock(return_value=1)

        enrich_bg.pause_background_enrichment()
        try:
            processed = await enrich_bg._run_until_quiescent(run_batch)
        finally:
            enrich_bg.resume_background_enrichment()

        self.assertEqual(processed, 0)
        run_batch.assert_not_awaited()

    async def test_background_enrich_runs_opencritic_workers_without_api_key(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("gamelib_mcp.data.enrich_bg._run_store_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_igdb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_hltb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_protondb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_steamspy_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_opencritic_workers", AsyncMock(return_value=0)) as opencritic_workers,
            patch("gamelib_mcp.data.enrich_bg._run_metacritic_workers", AsyncMock(return_value=0)),
        ):
            await enrich_bg.background_enrich()

        opencritic_workers.assert_awaited_once()

    async def test_background_enrich_runs_worker_families_concurrently(self) -> None:
        started = {"store": asyncio.Event(), "igdb": asyncio.Event()}
        release = asyncio.Event()

        async def fake_store_worker() -> int:
            started["store"].set()
            await release.wait()
            return 1

        async def fake_igdb_worker() -> int:
            started["igdb"].set()
            await release.wait()
            return 1

        with (
            patch("gamelib_mcp.data.enrich_bg._run_store_workers", AsyncMock(side_effect=fake_store_worker)),
            patch("gamelib_mcp.data.enrich_bg._run_igdb_workers", AsyncMock(side_effect=fake_igdb_worker)),
            patch("gamelib_mcp.data.enrich_bg._run_hltb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_protondb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_steamspy_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_opencritic_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_metacritic_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg.recompute_tag_affinity", AsyncMock()),
        ):
            task = asyncio.create_task(enrich_bg.background_enrich())
            await asyncio.wait_for(started["store"].wait(), timeout=DEADLOCK_TIMEOUT)
            await asyncio.wait_for(started["igdb"].wait(), timeout=DEADLOCK_TIMEOUT)
            release.set()
            await asyncio.wait_for(task, timeout=DEADLOCK_TIMEOUT)

    async def test_background_enrich_logs_family_exceptions(self) -> None:
        with (
            patch("gamelib_mcp.data.enrich_bg._run_store_workers", AsyncMock(side_effect=RuntimeError("store boom"))),
            patch("gamelib_mcp.data.enrich_bg._run_igdb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_hltb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_protondb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_steamspy_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_opencritic_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_metacritic_workers", AsyncMock(return_value=0)),
            self.assertLogs("gamelib_mcp.data.enrich_bg", level="ERROR") as logs,
        ):
            await enrich_bg.background_enrich()

        self.assertTrue(any("Background enrichment family failed: store" in line for line in logs.output))

    async def test_recomputes_affinity_after_non_empty_pass(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("gamelib_mcp.data.enrich_bg._run_store_workers", AsyncMock(return_value=3)),
            patch("gamelib_mcp.data.enrich_bg._run_igdb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_hltb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_protondb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_steamspy_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_opencritic_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_metacritic_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg.recompute_tag_affinity", AsyncMock()) as recompute,
        ):
            await enrich_bg.background_enrich()

        recompute.assert_awaited_once()

    async def test_skips_affinity_recompute_when_nothing_processed(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("gamelib_mcp.data.enrich_bg._run_store_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_igdb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_hltb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_protondb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_steamspy_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_opencritic_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_metacritic_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg.recompute_tag_affinity", AsyncMock()) as recompute,
        ):
            await enrich_bg.background_enrich()

        recompute.assert_not_awaited()

    async def test_background_enrich_keeps_igdb_polling_while_other_families_progress(self) -> None:
        real_sleep = asyncio.sleep
        store_results = iter([1, 1, 1, 1, 0, 0, 0])
        igdb_results = iter([0, 0, 0, 1, 0, 0, 0])

        async def fake_sleep(_seconds: float) -> None:
            await real_sleep(0)

        async def fake_store_batch() -> int:
            await real_sleep(0)
            return next(store_results, 0)

        async def fake_igdb_batch() -> int:
            await real_sleep(0)
            return next(igdb_results, 0)

        with (
            patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", new=fake_sleep),
            patch("gamelib_mcp.data.enrich_bg._run_store_batch", AsyncMock(side_effect=fake_store_batch)),
            patch("gamelib_mcp.data.enrich_bg._run_igdb_batch", AsyncMock(side_effect=fake_igdb_batch)) as igdb_batch,
            patch("gamelib_mcp.data.enrich_bg._run_hltb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_protondb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_steamspy_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_opencritic_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_metacritic_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg.recompute_tag_affinity", AsyncMock()),
        ):
            await enrich_bg.background_enrich()

        self.assertGreaterEqual(igdb_batch.await_count, 4)

    async def test_hltb_batch_logs_claimed_row_count(self) -> None:
        with (
            patch("gamelib_mcp.data.enrich_bg.claim_game_ids_for_hltb", AsyncMock(return_value=[1, 2])),
            patch(
                "gamelib_mcp.data.enrich_bg.load_hltb_batch_rows",
                AsyncMock(
                    return_value=[
                        {"game_id": 1, "name": "Portal"},
                        {"game_id": 2, "name": "Half-Life 2"},
                    ]
                ),
            ),
            patch("gamelib_mcp.data.enrich_bg.get_hltb", AsyncMock(return_value=None)),
            patch("gamelib_mcp.data.enrich_bg.clear_claim", AsyncMock()),
            patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
            self.assertLogs("gamelib_mcp.data.enrich_bg", level="INFO") as logs,
        ):
            processed = await enrich_bg._run_hltb_batch()

        self.assertEqual(processed, 2)
        self.assertTrue(any("HLTB worker claimed 2 rows" in line for line in logs.output))

    async def test_hltb_workers_log_total_processed(self) -> None:
        with (
            patch("gamelib_mcp.data.enrich_bg._run_until_quiescent", AsyncMock(return_value=7)),
            self.assertLogs("gamelib_mcp.data.enrich_bg", level="INFO") as logs,
        ):
            processed = await enrich_bg._run_hltb_workers()

        self.assertEqual(processed, 7)
        self.assertTrue(any("HLTB worker complete: processed 7 rows" in line for line in logs.output))

    async def test_store_batch_skips_rows_already_claimed(self) -> None:
        with (
            patch("gamelib_mcp.data.enrich_bg.claim_steam_platform_ids_for_store", AsyncMock(return_value=[11])),
            patch(
                "gamelib_mcp.data.enrich_bg.load_store_batch_rows",
                AsyncMock(return_value=[{"game_platform_id": 11, "appid": 10, "name": "Portal 2"}]),
            ),
            patch("gamelib_mcp.data.enrich_bg.enrich_game", AsyncMock()) as enrich_game,
            patch("gamelib_mcp.data.enrich_bg._finalize_store_claim", AsyncMock()),
        ):
            processed = await enrich_bg._run_store_batch()

        self.assertEqual(processed, 1)
        enrich_game.assert_awaited_once()

    async def test_store_batch_releases_claim_without_marking_failed_on_exception(self) -> None:
        db_mock = AsyncMock()
        db_cm = AsyncMock()
        db_cm.__aenter__.return_value = db_mock
        db_cm.__aexit__.return_value = False

        with (
            patch("gamelib_mcp.data.enrich_bg.claim_steam_platform_ids_for_store", AsyncMock(return_value=[11])),
            patch(
                "gamelib_mcp.data.enrich_bg.load_store_batch_rows",
                AsyncMock(return_value=[{"game_platform_id": 11, "appid": 10, "name": "Portal 2"}]),
            ),
            patch("gamelib_mcp.data.enrich_bg.enrich_game", AsyncMock(side_effect=RuntimeError("timeout"))),
            patch("gamelib_mcp.data.enrich_bg.get_db", return_value=db_cm),
        ):
            await enrich_bg._run_store_batch()

        sql = db_mock.execute.await_args.args[0]
        self.assertIn("SET store_claimed_at = NULL", sql)
        self.assertNotIn("store_cached_at = 'FAILED'", sql)

    async def test_opencritic_batch_releases_claim_without_forcing_failed_cache_marker(self) -> None:
        with (
            patch("gamelib_mcp.data.enrich_bg.claim_game_platform_ids_for_opencritic", AsyncMock(return_value=[11])),
            patch(
                "gamelib_mcp.data.enrich_bg.load_opencritic_batch_rows",
                AsyncMock(return_value=[{"game_platform_id": 11, "name": "Portal 2"}]),
            ),
            patch("gamelib_mcp.data.enrich_bg.enrich_opencritic", AsyncMock(return_value={"status": "ambiguous"})),
            patch("gamelib_mcp.data.enrich_bg._finalize_platform_enrichment_claim", AsyncMock()) as finalize,
            patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
        ):
            await enrich_bg._run_opencritic_batch()

        finalize.assert_awaited_once_with(11, "opencritic_claimed_at", "opencritic_cached_at", True)

    async def test_opencritic_batch_treats_http_error_as_successful_finalization(self) -> None:
        with (
            patch("gamelib_mcp.data.enrich_bg.claim_game_platform_ids_for_opencritic", AsyncMock(return_value=[11])),
            patch(
                "gamelib_mcp.data.enrich_bg.load_opencritic_batch_rows",
                AsyncMock(return_value=[{"game_platform_id": 11, "name": "Portal 2"}]),
            ),
            patch("gamelib_mcp.data.enrich_bg.enrich_opencritic", AsyncMock(return_value={"status": "http_error"})),
            patch("gamelib_mcp.data.enrich_bg._finalize_platform_enrichment_claim", AsyncMock()) as finalize,
            patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
        ):
            await enrich_bg._run_opencritic_batch()

        finalize.assert_awaited_once_with(11, "opencritic_claimed_at", "opencritic_cached_at", True)

    async def test_protondb_batch_releases_claim_without_marking_failed_on_exception(self) -> None:
        db_mock = AsyncMock()
        db_cm = AsyncMock()
        db_cm.__aenter__.return_value = db_mock
        db_cm.__aexit__.return_value = False

        with (
            patch("gamelib_mcp.data.enrich_bg.claim_steam_platform_ids_for_protondb", AsyncMock(return_value=[11])),
            patch(
                "gamelib_mcp.data.enrich_bg.load_steam_platform_batch_rows",
                AsyncMock(return_value=[{"game_platform_id": 11, "appid": 10, "name": "Portal 2"}]),
            ),
            patch("gamelib_mcp.data.enrich_bg.get_protondb", AsyncMock(side_effect=RuntimeError("timeout"))),
            patch("gamelib_mcp.data.enrich_bg.get_db", return_value=db_cm),
            patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
        ):
            await enrich_bg._run_protondb_batch()

        sql = db_mock.execute.await_args.args[0]
        self.assertIn("SET protondb_claimed_at = NULL", sql)
        self.assertNotIn("protondb_cached_at = 'FAILED'", sql)

    async def test_finalize_steam_claim_defers_transient_sqlite_lock(self) -> None:
        db_mock = AsyncMock()
        db_mock.execute.side_effect = sqlite3.OperationalError("database is locked")
        db_cm = AsyncMock()
        db_cm.__aenter__.return_value = db_mock
        db_cm.__aexit__.return_value = False

        with (
            patch("gamelib_mcp.data.enrich_bg.get_db", return_value=db_cm),
            self.assertLogs("gamelib_mcp.data.enrich_bg", level="INFO") as logs,
        ):
            await enrich_bg._finalize_steam_claim(11, "protondb_claimed_at")

        self.assertTrue(any("Deferring enrichment claim release" in line for line in logs.output))

    async def test_finalize_steam_claim_defers_without_db_write_while_paused(self) -> None:
        db_cm = AsyncMock()

        enrich_bg.pause_background_enrichment()
        try:
            with (
                patch("gamelib_mcp.data.enrich_bg.get_db", return_value=db_cm) as get_db,
                self.assertLogs("gamelib_mcp.data.enrich_bg", level="INFO") as logs,
            ):
                await enrich_bg._finalize_steam_claim(11, "protondb_claimed_at")
        finally:
            enrich_bg.resume_background_enrichment()

        get_db.assert_not_called()
        self.assertTrue(any("while enrichment is paused" in line for line in logs.output))

    async def test_hltb_batch_defers_claim_release_when_pause_starts_after_claim(self) -> None:
        async def pause_during_hltb(_game_id: int, _name: str) -> None:
            enrich_bg.pause_background_enrichment()

        with (
            patch("gamelib_mcp.data.enrich_bg.claim_game_ids_for_hltb", AsyncMock(return_value=[1])),
            patch(
                "gamelib_mcp.data.enrich_bg.load_hltb_batch_rows",
                AsyncMock(return_value=[{"game_id": 1, "name": "Portal"}]),
            ),
            patch("gamelib_mcp.data.enrich_bg.get_hltb", AsyncMock(side_effect=pause_during_hltb)),
            patch("gamelib_mcp.data.enrich_bg.clear_claim", AsyncMock()) as clear_claim,
            patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
            self.assertLogs("gamelib_mcp.data.enrich_bg", level="INFO") as logs,
        ):
            try:
                processed = await enrich_bg._run_hltb_batch()
            finally:
                enrich_bg.resume_background_enrichment()

        self.assertEqual(processed, 1)
        clear_claim.assert_not_awaited()
        self.assertTrue(any("while enrichment is paused" in line for line in logs.output))

    async def test_steamspy_batch_releases_claim_without_marking_failed_on_exception(self) -> None:
        db_mock = AsyncMock()
        db_cm = AsyncMock()
        db_cm.__aenter__.return_value = db_mock
        db_cm.__aexit__.return_value = False

        with (
            patch("gamelib_mcp.data.enrich_bg.claim_steam_platform_ids_for_steamspy", AsyncMock(return_value=[11])),
            patch(
                "gamelib_mcp.data.enrich_bg.load_steam_platform_batch_rows",
                AsyncMock(return_value=[{"game_platform_id": 11, "appid": 10, "name": "Portal 2"}]),
            ),
            patch("gamelib_mcp.data.enrich_bg.enrich_steamspy", AsyncMock(side_effect=RuntimeError("timeout"))),
            patch("gamelib_mcp.data.enrich_bg.get_db", return_value=db_cm),
            patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
        ):
            await enrich_bg._run_steamspy_batch()

        sql = db_mock.execute.await_args.args[0]
        self.assertIn("SET steamspy_claimed_at = NULL", sql)
        self.assertNotIn("steamspy_cached_at = 'FAILED'", sql)


class EnrichmentRunStatsTests(unittest.IsolatedAsyncioTestCase):
    """Per-provider processed/failed accounting.

    Every batch function returns "rows handled" whether the fetch succeeded or
    threw, so the worker summary alone cannot tell a healthy provider from one
    that broke outright. These tests pin the counters and the WARNING that makes
    a dead provider visible at production's INFO level.
    """

    def setUp(self) -> None:
        enrich_bg._reset_run_stats()

    def tearDown(self) -> None:
        enrich_bg._reset_run_stats()

    def _enter_hltb_patches(self, stack: ExitStack, rows, fetch) -> None:
        # _run_until_quiescent polls until three consecutive empty batches, so
        # the loaders keep answering after the one real batch.
        empties = [[] for _ in range(6)]
        for patcher in (
            patch(
                "gamelib_mcp.data.enrich_bg.claim_game_ids_for_hltb",
                AsyncMock(side_effect=[[row["game_id"] for row in rows], *empties]),
            ),
            patch(
                "gamelib_mcp.data.enrich_bg.load_hltb_batch_rows",
                AsyncMock(side_effect=[rows, *empties]),
            ),
            patch("gamelib_mcp.data.enrich_bg.get_hltb", fetch),
            patch("gamelib_mcp.data.enrich_bg.clear_claim", AsyncMock()),
            patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
        ):
            stack.enter_context(patcher)

    def _enter_protondb_patches(self, stack: ExitStack, rows, fetch) -> None:
        empties = [[] for _ in range(6)]
        for patcher in (
            patch(
                "gamelib_mcp.data.enrich_bg.claim_steam_platform_ids_for_protondb",
                AsyncMock(side_effect=[[row["game_platform_id"] for row in rows], *empties]),
            ),
            patch(
                "gamelib_mcp.data.enrich_bg.load_steam_platform_batch_rows",
                AsyncMock(side_effect=[rows, *empties]),
            ),
            patch("gamelib_mcp.data.enrich_bg.get_protondb", fetch),
            patch("gamelib_mcp.data.enrich_bg._finalize_steam_claim", AsyncMock()),
            patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
        ):
            stack.enter_context(patcher)

    async def test_hltb_worker_counts_failures_and_warns(self) -> None:
        rows = [{"game_id": n, "name": f"Game {n}"} for n in (1, 2, 3)]
        fetch = AsyncMock(side_effect=RuntimeError("hltb markup changed"))

        with ExitStack() as stack:
            self._enter_hltb_patches(stack, rows, fetch)
            with self.assertLogs("gamelib_mcp.data.enrich_bg", level="WARNING") as logs:
                total = await enrich_bg._run_hltb_workers()

        stats = enrich_bg.last_run_stats()["hltb"]
        self.assertEqual(total, 3)
        self.assertEqual(stats["failed"], 3)
        self.assertEqual(stats["processed"], 0)
        self.assertIn("hltb markup changed", stats["last_error"])
        self.assertTrue(
            any(
                "HLTB enrichment: 3 of 3 items failed this run" in line
                and "hltb markup changed" in line
                for line in logs.output
            ),
            logs.output,
        )

    async def test_protondb_worker_counts_failures_and_warns(self) -> None:
        rows = [
            {"game_platform_id": 10 + n, "appid": 100 + n, "name": f"Game {n}"} for n in (1, 2, 3)
        ]
        fetch = AsyncMock(side_effect=RuntimeError("protondb 502"))

        with ExitStack() as stack:
            self._enter_protondb_patches(stack, rows, fetch)
            with self.assertLogs("gamelib_mcp.data.enrich_bg", level="WARNING") as logs:
                total = await enrich_bg._run_protondb_workers()

        stats = enrich_bg.last_run_stats()["protondb"]
        self.assertEqual(total, 3)
        self.assertEqual(stats["failed"], 3)
        self.assertEqual(stats["processed"], 0)
        self.assertIn("protondb 502", stats["last_error"])
        self.assertTrue(
            any(
                "ProtonDB enrichment: 3 of 3 items failed this run" in line
                and "protondb 502" in line
                for line in logs.output
            ),
            logs.output,
        )

    async def test_hltb_worker_stays_quiet_when_every_fetch_succeeds(self) -> None:
        rows = [{"game_id": n, "name": f"Game {n}"} for n in (1, 2, 3)]
        # A COMPLETE result, not None: get_hltb answers None for a failed fetch
        # too, so pinning success on None pinned the bug this class exists for.
        hit = {"hltb_main": 8.5, "hltb_extra": 12.0, "hltb_complete": 30.0}

        with ExitStack() as stack:
            self._enter_hltb_patches(stack, rows, AsyncMock(return_value=hit))
            with self.assertNoLogs("gamelib_mcp.data.enrich_bg", level="WARNING"):
                total = await enrich_bg._run_hltb_workers()

        stats = enrich_bg.last_run_stats()["hltb"]
        self.assertEqual(total, 3)
        self.assertEqual(stats["processed"], 3)
        self.assertEqual(stats["failed"], 0)
        self.assertIsNone(stats["last_error"])

    async def test_protondb_worker_stays_quiet_when_every_fetch_succeeds(self) -> None:
        rows = [
            {"game_platform_id": 10 + n, "appid": 100 + n, "name": f"Game {n}"} for n in (1, 2, 3)
        ]

        with ExitStack() as stack:
            # A real tier, not None — None is also what a 502 returns.
            self._enter_protondb_patches(stack, rows, AsyncMock(return_value="platinum"))
            with self.assertNoLogs("gamelib_mcp.data.enrich_bg", level="WARNING"):
                total = await enrich_bg._run_protondb_workers()

        stats = enrich_bg.last_run_stats()["protondb"]
        self.assertEqual(total, 3)
        self.assertEqual(stats["processed"], 3)
        self.assertEqual(stats["failed"], 0)

    async def test_single_failure_in_a_healthy_batch_does_not_warn(self) -> None:
        # One flaky fetch out of four is neither 3 failures nor half the batch,
        # which is the point of the threshold: the WARNING has to mean "this
        # provider is broken", not "one request timed out".
        rows = [{"game_id": n, "name": f"Game {n}"} for n in (1, 2, 3, 4)]

        async def fetch(game_id: int, _name: str) -> None:
            if game_id == 2:
                raise RuntimeError("one flaky fetch")

        with ExitStack() as stack:
            self._enter_hltb_patches(stack, rows, AsyncMock(side_effect=fetch))
            with self.assertNoLogs("gamelib_mcp.data.enrich_bg", level="WARNING"):
                total = await enrich_bg._run_hltb_workers()

        stats = enrich_bg.last_run_stats()["hltb"]
        self.assertEqual(total, 4)
        self.assertEqual(stats["processed"], 3)
        self.assertEqual(stats["failed"], 1)

    async def test_last_run_stats_returns_a_copy(self) -> None:
        enrich_bg._record_failure("steamspy", RuntimeError("boom"))

        snapshot = enrich_bg.last_run_stats()
        snapshot["steamspy"]["failed"] = 999
        snapshot["steamspy"]["last_error"] = "mutated"
        snapshot.pop("hltb")

        fresh = enrich_bg.last_run_stats()
        self.assertEqual(fresh["steamspy"]["failed"], 1)
        self.assertIn("boom", fresh["steamspy"]["last_error"])
        self.assertIn("hltb", fresh)

    async def test_background_enrich_resets_stats_and_logs_summary(self) -> None:
        enrich_bg._record_failure("hltb", RuntimeError("stale failure from a previous run"))

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("gamelib_mcp.data.enrich_bg._run_store_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_igdb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_hltb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_protondb_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_steamspy_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_opencritic_workers", AsyncMock(return_value=0)),
            patch("gamelib_mcp.data.enrich_bg._run_metacritic_workers", AsyncMock(return_value=0)),
            self.assertLogs("gamelib_mcp.data.enrich_bg", level="INFO") as logs,
        ):
            await enrich_bg.background_enrich()

        self.assertEqual(enrich_bg.last_run_stats()["hltb"]["failed"], 0)
        self.assertIsNone(enrich_bg.last_run_stats()["hltb"]["last_error"])
        self.assertTrue(
            any(
                "Background enrichment complete" in line and "hltb processed=0 failed=0" in line
                for line in logs.output
            ),
            logs.output,
        )


class ProviderSwallowedFailureTests(ToolDBTestCase):
    """The counters have to see the failures the providers SWALLOW.

    Every provider here keeps data/CLAUDE.md's best-effort contract: a dead API
    becomes a logged ``None`` (or a status dict), never an exception, so that
    ``get_game_detail`` can never fail on enrichment. Counting exceptions in
    enrich_bg therefore counted nothing: an outage logged "processed 25 rows, 0
    failed" and the WARNING that exists to surface it could not fire. These
    tests break the provider's own HTTP layer so the real swallow runs, and
    assert what the run reported afterwards.
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        enrich_bg._reset_run_stats()
        provider_health.reset()

    async def asyncTearDown(self) -> None:
        enrich_bg._reset_run_stats()
        provider_health.reset()
        await super().asyncTearDown()

    @contextlib.contextmanager
    def _real_hltb_batch(self, rows, search, *, empties: int = 0):
        """Claim `rows` and run the REAL get_hltb over a patched HLTB client."""
        client = MagicMock()
        client.async_search = search
        empty = [[] for _ in range(empties)]
        with ExitStack() as stack:
            for patcher in (
                patch(
                    "gamelib_mcp.data.enrich_bg.claim_game_ids_for_hltb",
                    AsyncMock(side_effect=[[row["game_id"] for row in rows], *empty]),
                ),
                patch(
                    "gamelib_mcp.data.enrich_bg.load_hltb_batch_rows",
                    AsyncMock(side_effect=[rows, *empty]),
                ),
                patch("gamelib_mcp.data.enrich_bg._clear_claim_or_defer", AsyncMock()),
                patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
                patch("gamelib_mcp.data.hltb.HowLongToBeat", MagicMock(return_value=client)),
            ):
                stack.enter_context(patcher)
            yield

    @contextlib.contextmanager
    def _opencritic_batch(self, rows, enrich, *, empties: int = 0):
        empty = [[] for _ in range(empties)]
        with ExitStack() as stack:
            for patcher in (
                patch(
                    "gamelib_mcp.data.enrich_bg.claim_game_platform_ids_for_opencritic",
                    AsyncMock(
                        side_effect=[[row["game_platform_id"] for row in rows], *empty]
                    ),
                ),
                patch(
                    "gamelib_mcp.data.enrich_bg.load_opencritic_batch_rows",
                    AsyncMock(side_effect=[rows, *empty]),
                ),
                patch("gamelib_mcp.data.enrich_bg.enrich_opencritic", enrich),
                patch(
                    "gamelib_mcp.data.enrich_bg._finalize_platform_enrichment_claim",
                    AsyncMock(),
                ),
                patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
            ):
                stack.enter_context(patcher)
            yield

    async def test_hltb_transport_failure_the_provider_swallows_is_counted(self) -> None:
        rows = [{"game_id": n, "name": f"Game {n}"} for n in (1, 2, 3)]
        search = AsyncMock(side_effect=httpx.ConnectError("hltb is unreachable"))

        with self._real_hltb_batch(rows, search):
            handled = await enrich_bg._run_hltb_batch()

        stats = enrich_bg.last_run_stats()["hltb"]
        self.assertEqual(handled, 3)
        self.assertEqual(stats["failed"], 3)
        self.assertEqual(stats["processed"], 0)
        self.assertIn("hltb is unreachable", stats["last_error"])

    async def test_hltb_api_answering_nothing_is_a_failure_not_a_not_found(self) -> None:
        # howlongtobeatpy answers None (not []) when the request itself failed.
        # The provider deliberately does NOT write a NOT_FOUND marker for it,
        # and it must not read as a processed row either.
        rows = [{"game_id": n, "name": f"Game {n}"} for n in (1, 2)]

        with self._real_hltb_batch(rows, AsyncMock(return_value=None)):
            await enrich_bg._run_hltb_batch()

        stats = enrich_bg.last_run_stats()["hltb"]
        self.assertEqual(stats["failed"], 2)
        self.assertEqual(stats["processed"], 0)

    async def test_hltb_not_found_counts_as_processed(self) -> None:
        # HLTB answered and has no entry for the title. That is the provider
        # working, and it must never look like an outage.
        rows = [{"game_id": n, "name": f"Game {n}"} for n in (1, 2, 3)]

        with self._real_hltb_batch(rows, AsyncMock(return_value=[])):
            await enrich_bg._run_hltb_batch()

        stats = enrich_bg.last_run_stats()["hltb"]
        self.assertEqual(stats["processed"], 3)
        self.assertEqual(stats["failed"], 0)
        self.assertIsNone(stats["last_error"])

    async def test_hltb_match_counts_as_processed(self) -> None:
        rows = [{"game_id": n, "name": f"Game {n}"} for n in (1, 2)]
        entry = SimpleNamespace(
            similarity=0.98, main_story=8.5, main_extra=12.0, completionist=30.0
        )

        with self._real_hltb_batch(rows, AsyncMock(return_value=[entry])):
            await enrich_bg._run_hltb_batch()

        stats = enrich_bg.last_run_stats()["hltb"]
        self.assertEqual(stats["processed"], 2)
        self.assertEqual(stats["failed"], 0)

    async def test_dead_hltb_still_warns_through_the_swallow(self) -> None:
        # The whole point: the WARNING has to fire for the outage shape that
        # never raises, which is the only shape HLTB actually produces.
        rows = [{"game_id": n, "name": f"Game {n}"} for n in (1, 2, 3)]
        search = AsyncMock(side_effect=httpx.ReadTimeout("hltb timed out"))

        with (
            self._real_hltb_batch(rows, search, empties=6),
            self.assertLogs("gamelib_mcp.data.enrich_bg", level="WARNING") as logs,
        ):
            await enrich_bg._run_hltb_workers()

        self.assertTrue(
            any("HLTB enrichment: 3 of 3 items failed this run" in line for line in logs.output),
            logs.output,
        )

    async def test_opencritic_failure_status_is_counted_as_a_failure(self) -> None:
        # enrich_opencritic reports failure in its RETURN VALUE; http_error
        # stays in _OPENCRITIC_SUCCESS_STATUSES because that set answers a
        # different question (was the claim resolved), so nothing before this
        # counted it as anything but a processed row.
        rows = [{"game_platform_id": 10 + n, "name": f"Game {n}"} for n in (1, 2, 3)]
        enrich = AsyncMock(return_value={"status": "http_error"})

        with self._opencritic_batch(rows, enrich):
            handled = await enrich_bg._run_opencritic_batch()

        stats = enrich_bg.last_run_stats()["opencritic"]
        self.assertEqual(handled, 3)
        self.assertEqual(stats["failed"], 3)
        self.assertEqual(stats["processed"], 0)
        self.assertIn("http_error", stats["last_error"])

    async def test_opencritic_no_match_counts_as_processed(self) -> None:
        rows = [{"game_platform_id": 10 + n, "name": f"Game {n}"} for n in (1, 2, 3)]
        enrich = AsyncMock(return_value={"status": "no_match", "cached_at": "NO_MATCH:x"})

        with self._opencritic_batch(rows, enrich):
            await enrich_bg._run_opencritic_batch()

        stats = enrich_bg.last_run_stats()["opencritic"]
        self.assertEqual(stats["processed"], 3)
        self.assertEqual(stats["failed"], 0)

    async def test_opencritic_row_is_not_counted_twice(self) -> None:
        # The provider records the http_error on its way out AND reports it in
        # the status dict. One bad row is one failed row.
        rows = [{"game_platform_id": 10 + n, "name": f"Game {n}"} for n in (1, 2, 3)]

        async def enrich(game_platform_id: int, name: str) -> dict:
            if game_platform_id == 11:
                provider_health.record_failure("opencritic", "export http error")
                return {"status": "http_error"}
            return {"status": "matched", "fields": {}}

        with self._opencritic_batch(rows, AsyncMock(side_effect=enrich)):
            await enrich_bg._run_opencritic_batch()

        stats = enrich_bg.last_run_stats()["opencritic"]
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["processed"], 2)

    async def test_igdb_swallowed_request_failure_is_counted(self) -> None:
        # backfill_missing_games returns rows RESOLVED and swallows
        # IGDBRequestFailure per game, so a dead IGDB used to be
        # indistinguishable from a pass with no work left.
        async def backfill(limit: int = 10) -> int:
            provider_health.record_failure(
                "igdb", RuntimeError("IGDB search failed for 'Portal 2'")
            )
            return 0

        with (
            patch("gamelib_mcp.data.enrich_bg.igdb.backfill_missing_games", backfill),
            patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
        ):
            total = await enrich_bg._run_igdb_batch()

        stats = enrich_bg.last_run_stats()["igdb"]
        self.assertEqual(total, 0)
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(stats["failed"], enrich_bg._IGDB_WORKER_CONCURRENCY)
        self.assertIn("IGDB search failed", stats["last_error"])

    async def test_igdb_resolved_rows_count_as_processed(self) -> None:
        with (
            patch(
                "gamelib_mcp.data.enrich_bg.igdb.backfill_missing_games",
                AsyncMock(return_value=4),
            ),
            patch("gamelib_mcp.data.enrich_bg.asyncio.sleep", AsyncMock()),
        ):
            total = await enrich_bg._run_igdb_batch()

        stats = enrich_bg.last_run_stats()["igdb"]
        self.assertEqual(total, 4 * enrich_bg._IGDB_WORKER_CONCURRENCY)
        self.assertEqual(stats["processed"], total)
        self.assertEqual(stats["failed"], 0)

    async def test_lazy_path_failures_cannot_inflate_a_batch_past_its_rows(self) -> None:
        # provider_health is process-wide, so a get_game_detail enrichment
        # failing mid-pass lands in the same counter. It may not report more
        # failed rows than the batch attempted — the WARNING's ratio depends on
        # that.
        rows = [{"game_id": 1, "name": "Game 1"}]

        async def search(query: str):
            provider_health.record_failure("hltb", "a concurrent lazy fetch failed")
            raise httpx.ConnectError("hltb is unreachable")

        with self._real_hltb_batch(rows, AsyncMock(side_effect=search)):
            await enrich_bg._run_hltb_batch()

        stats = enrich_bg.last_run_stats()["hltb"]
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["processed"], 0)
