import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from conftest import DEADLOCK_TIMEOUT
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import enrich_bg


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
