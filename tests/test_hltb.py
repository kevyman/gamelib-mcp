import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
            # Seed prior good data so we can verify it is preserved on failure.
            async with db_module.get_db() as db:
                await db.execute(
                    "UPDATE games SET hltb_main = 56.0, hltb_extra = 92.0, hltb_complete = 133.0 WHERE id = ?",
                    (game_id,),
                )
                await db.commit()

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

        # Prior good data must survive the API failure.
        self.assertEqual(row["hltb_main"], 56.0)
        self.assertEqual(row["hltb_extra"], 92.0)
        self.assertEqual(row["hltb_complete"], 133.0)
        # cached_at stays NULL so the row remains eligible for background retry.
        self.assertIsNone(row["hltb_cached_at"])

    async def test_get_hltb_coerces_zero_durations_to_null(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            game_id = await db_module.upsert_game(appid=None, name="ASTRO BOT")
            # HLTB has the title but no main-story duration -> returns 0.0.
            fake_result = SimpleNamespace(main_story=0.0, main_extra=0.0, completionist=12.5, similarity=1.0)
            with patch(
                "gamelib_mcp.data.hltb.HowLongToBeat.async_search",
                return_value=[fake_result],
            ):
                result = await hltb.get_hltb(game_id, "ASTRO BOT")

            self.assertEqual(
                result,
                {"hltb_main": None, "hltb_extra": None, "hltb_complete": 12.5},
            )
            async with db_module.get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT hltb_main, hltb_extra, hltb_complete FROM games WHERE id = ?",
                    (game_id,),
                )

        self.assertIsNone(row["hltb_main"])
        self.assertIsNone(row["hltb_extra"])
        self.assertEqual(row["hltb_complete"], 12.5)

    async def test_get_hltb_preserves_data_on_empty_results(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            game_id = await db_module.upsert_game(appid=None, name="Elden Ring")
            async with db_module.get_db() as db:
                await db.execute(
                    "UPDATE games SET hltb_main = 56.0, hltb_extra = 92.0, hltb_complete = 133.0 WHERE id = ?",
                    (game_id,),
                )
                await db.commit()

            with patch(
                "gamelib_mcp.data.hltb.HowLongToBeat.async_search",
                return_value=[],
            ):
                result = await hltb.get_hltb(game_id, "Elden Ring")

            self.assertIsNone(result)
            async with db_module.get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT hltb_main, hltb_extra, hltb_complete, hltb_cached_at FROM games WHERE id = ?",
                    (game_id,),
                )

        # Data survives a not-found result; only the (timestamped, retryable)
        # marker is written.
        self.assertEqual(row["hltb_main"], 56.0)
        self.assertEqual(row["hltb_extra"], 92.0)
        self.assertEqual(row["hltb_complete"], 133.0)
        self.assertTrue(row["hltb_cached_at"].startswith(f"{hltb.HLTB_NOT_FOUND}:"))

    async def test_get_hltb_preserves_data_on_exception(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            game_id = await db_module.upsert_game(appid=None, name="Elden Ring")
            async with db_module.get_db() as db:
                await db.execute(
                    "UPDATE games SET hltb_main = 56.0, hltb_extra = 92.0, hltb_complete = 133.0 WHERE id = ?",
                    (game_id,),
                )
                await db.commit()

            with patch(
                "gamelib_mcp.data.hltb.HowLongToBeat.async_search",
                side_effect=RuntimeError("network error"),
            ):
                result = await hltb.get_hltb(game_id, "Elden Ring")

            self.assertIsNone(result)
            async with db_module.get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT hltb_main, hltb_extra, hltb_complete, hltb_cached_at FROM games WHERE id = ?",
                    (game_id,),
                )

        # Data survives a fetch exception; row stays retryable.
        self.assertEqual(row["hltb_main"], 56.0)
        self.assertEqual(row["hltb_extra"], 92.0)
        self.assertEqual(row["hltb_complete"], 133.0)
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
            # The first call walks the whole variant ladder ("Unknown Game" is
            # its own normalized form, so a single variant here); the second
            # call is served from the fresh NOT_FOUND marker without searching.
            self.assertEqual(search.await_count, 1)
            async with db_module.get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT hltb_cached_at FROM games WHERE id = ?",
                    (game_id,),
                )

        self.assertTrue(row["hltb_cached_at"].startswith("NOT_FOUND:"))


class HLTBVariantLadderTests(unittest.IsolatedAsyncioTestCase):
    """Matcher fallbacks for the observed prod NOT_FOUND shapes."""

    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "hltb-variants.sqlite"
        db_module._DB_READY_PATH = None
        self._env = patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        )
        self._env.start()
        await db_module.init_db()

    async def asyncTearDown(self) -> None:
        self._env.stop()
        db_module._DB_READY_PATH = None
        self.tmpdir.cleanup()

    def _result(self, main: float = 10.0) -> SimpleNamespace:
        return SimpleNamespace(
            main_story=main, main_extra=20.0, completionist=30.0, similarity=1.0
        )

    def test_variants_cover_prod_failure_shapes(self) -> None:
        self.assertEqual(
            hltb._search_name_variants("HITMAN 2")[:2], ["HITMAN 2", "Hitman 2"]
        )
        self.assertIn(
            "Grand Theft Auto V",
            hltb._search_name_variants("Grand Theft Auto V Legacy"),
        )
        variants = hltb._search_name_variants("Borderlands: Game of the Year (Classic)")
        # normalize_catalog_title peels "(Classic)"; the fallback suffix pass
        # then peels the bare "Game of the Year".
        self.assertIn("Borderlands: Game of the Year", variants)
        self.assertIn("Borderlands", variants)
        self.assertIn(
            "Sekiro: Shadows Die Twice",
            hltb._search_name_variants("Sekiro™: Shadows Die Twice"),
        )

    async def test_ladder_rescues_all_caps_name(self) -> None:
        game_id = await db_module.upsert_game(appid=None, name="HITMAN 2")
        seen: list[str] = []

        async def fake_search(query):
            seen.append(query)
            return [self._result()] if query == "Hitman 2" else []

        with patch(
            "gamelib_mcp.data.hltb.HowLongToBeat.async_search", side_effect=fake_search
        ):
            result = await hltb.get_hltb(game_id, "HITMAN 2")

        self.assertEqual(seen, ["HITMAN 2", "Hitman 2"])
        self.assertEqual(result["hltb_main"], 10.0)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT hltb_main, hltb_cached_at FROM games WHERE id = ?", (game_id,)
            )
        self.assertEqual(row["hltb_main"], 10.0)
        self.assertFalse(row["hltb_cached_at"].startswith(hltb.HLTB_NOT_FOUND))

    async def test_ladder_strips_edition_suffix(self) -> None:
        game_id = await db_module.upsert_game(appid=None, name="Grand Theft Auto V Legacy")
        seen: list[str] = []

        async def fake_search(query):
            seen.append(query)
            return [self._result()] if query == "Grand Theft Auto V" else []

        with patch(
            "gamelib_mcp.data.hltb.HowLongToBeat.async_search", side_effect=fake_search
        ):
            result = await hltb.get_hltb(game_id, "Grand Theft Auto V Legacy")

        self.assertIsNotNone(result)
        self.assertIn("Grand Theft Auto V", seen)

    async def test_ladder_operational_failure_writes_no_marker(self) -> None:
        game_id = await db_module.upsert_game(appid=None, name="HITMAN 2")

        async def fake_search(query):
            # Literal name misses; the API dies on the fallback variant. The
            # miss must NOT be recorded as NOT_FOUND — the ladder never ran to
            # completion.
            return [] if query == "HITMAN 2" else None

        with patch(
            "gamelib_mcp.data.hltb.HowLongToBeat.async_search", side_effect=fake_search
        ):
            result = await hltb.get_hltb(game_id, "HITMAN 2")

        self.assertIsNone(result)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT hltb_cached_at FROM games WHERE id = ?", (game_id,)
            )
        self.assertIsNone(row["hltb_cached_at"])

    async def test_expired_not_found_marker_is_retried(self) -> None:
        game_id = await db_module.upsert_game(appid=None, name="HELLDIVERS 2")
        stale = (
            datetime.now(timezone.utc)
            - timedelta(days=db_module.HLTB_NOT_FOUND_RETRY_DAYS + 10)
        ).isoformat()
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET hltb_cached_at = ? WHERE id = ?",
                (f"NOT_FOUND:{stale}", game_id),
            )
            await db.commit()

        with patch(
            "gamelib_mcp.data.hltb.HowLongToBeat.async_search",
            return_value=[self._result(42.0)],
        ) as search:
            result = await hltb.get_hltb(game_id, "HELLDIVERS 2")

        search.assert_awaited()
        self.assertEqual(result["hltb_main"], 42.0)

    async def test_fresh_not_found_marker_suppresses_search(self) -> None:
        game_id = await db_module.upsert_game(appid=None, name="HELLDIVERS 2")
        now = datetime.now(timezone.utc).isoformat()
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET hltb_cached_at = ? WHERE id = ?",
                (f"NOT_FOUND:{now}", game_id),
            )
            await db.commit()

        with patch(
            "gamelib_mcp.data.hltb.HowLongToBeat.async_search",
            return_value=[self._result()],
        ) as search:
            result = await hltb.get_hltb(game_id, "HELLDIVERS 2")

        self.assertIsNone(result)
        search.assert_not_awaited()

    async def test_claim_reclaims_expired_and_legacy_not_found_rows(self) -> None:
        fresh = await db_module.upsert_game(appid=None, name="Fresh Not Found")
        expired = await db_module.upsert_game(appid=None, name="Expired Not Found")
        legacy = await db_module.upsert_game(appid=None, name="Legacy Not Found")
        cached = await db_module.upsert_game(appid=None, name="Recently Cached")

        now = datetime.now(timezone.utc)
        stale = (now - timedelta(days=db_module.HLTB_NOT_FOUND_RETRY_DAYS + 10)).isoformat()
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET hltb_cached_at = ? WHERE id = ?",
                (f"NOT_FOUND:{now.isoformat()}", fresh),
            )
            await db.execute(
                "UPDATE games SET hltb_cached_at = ? WHERE id = ?",
                (f"NOT_FOUND:{stale}", expired),
            )
            await db.execute(
                "UPDATE games SET hltb_cached_at = 'NOT_FOUND' WHERE id = ?",
                (legacy,),
            )
            await db.execute(
                "UPDATE games SET hltb_cached_at = ? WHERE id = ?",
                (now.isoformat(), cached),
            )
            await db.commit()

        claimed = await db_module.claim_game_ids_for_hltb(
            limit=10, stale_before=db_module._claim_cutoff_iso()
        )

        self.assertEqual(sorted(claimed), sorted([expired, legacy]))


if __name__ == "__main__":
    asyncio.run(unittest.main())
