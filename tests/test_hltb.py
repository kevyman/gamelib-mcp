import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import hltb


class HLTBRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "hltb.sqlite"
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

    async def test_get_hltb_retries_legacy_failed_rows(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            game_id = await db_module.upsert_game(appid=None, name="Elden Ring")
            async with db_module.get_db() as db:
                await db.execute(
                    "UPDATE games SET hltb_cached_at = 'FAILED' WHERE id = ?",
                    (game_id,),
                )
                await db.commit()

            fake_result = SimpleNamespace(main_story=56.0, main_extra=92.0, completionist=133.0, similarity=1.0)
            with patch(
                "gamelib_mcp.data.hltb.HowLongToBeat.async_search",
                return_value=[fake_result],
            ):
                result = await hltb.get_hltb(game_id, "Elden Ring")

            self.assertEqual(
                result,
                {"hltb_main": 56.0, "hltb_extra": 92.0, "hltb_complete": 133.0},
            )
            async with db_module.get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT hltb_main, hltb_extra, hltb_complete, hltb_cached_at FROM games WHERE id = ?",
                    (game_id,),
                )

        self.assertEqual(row["hltb_main"], 56.0)
        self.assertEqual(row["hltb_extra"], 92.0)
        self.assertEqual(row["hltb_complete"], 133.0)
        self.assertNotEqual(row["hltb_cached_at"], "FAILED")

    async def test_get_hltb_leaves_request_failures_retryable(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            game_id = await db_module.upsert_game(appid=None, name="Elden Ring")
            with patch(
                "gamelib_mcp.data.hltb.HowLongToBeat.async_search",
                return_value=None,
            ):
                result = await hltb.get_hltb(game_id, "Elden Ring")

            self.assertIsNone(result)
            async with db_module.get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT hltb_main, hltb_extra, hltb_complete, hltb_cached_at FROM games WHERE id = ?",
                    (game_id,),
                )

        self.assertIsNone(row["hltb_main"])
        self.assertIsNone(row["hltb_extra"])
        self.assertIsNone(row["hltb_complete"])
        self.assertIsNone(row["hltb_cached_at"])

    async def test_get_hltb_marks_true_no_match_as_not_found(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            game_id = await db_module.upsert_game(appid=None, name="Unknown Game")
            with patch(
                "gamelib_mcp.data.hltb.HowLongToBeat.async_search",
                return_value=[],
            ) as search:
                first = await hltb.get_hltb(game_id, "Unknown Game")
                second = await hltb.get_hltb(game_id, "Unknown Game")

            self.assertIsNone(first)
            self.assertIsNone(second)
            self.assertEqual(search.await_count, 1)
            async with db_module.get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT hltb_cached_at FROM games WHERE id = ?",
                    (game_id,),
                )

        self.assertEqual(row["hltb_cached_at"], "NOT_FOUND")


if __name__ == "__main__":
    asyncio.run(unittest.main())
