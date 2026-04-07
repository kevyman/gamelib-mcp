import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class BackgroundEnrichmentSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_enrich_skips_opencritic_workers_when_api_key_is_missing(self) -> None:
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

        opencritic_workers.assert_not_awaited()

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
        ):
            task = asyncio.create_task(enrich_bg.background_enrich())
            await asyncio.wait_for(started["store"].wait(), timeout=0.1)
            await asyncio.wait_for(started["igdb"].wait(), timeout=0.1)
            release.set()
            await asyncio.wait_for(task, timeout=0.1)

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
