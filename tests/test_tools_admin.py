"""Characterization tests for gamelib_mcp.tools.admin.

Covers detect_farmed_games (pure DB) and set_nintendo_session, including its
successful file-write path (writes to a temp NINTENDO_COOKIES_FILE).
"""

import json

from conftest import ToolDBTestCase, make_steam_game
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import admin


class DetectFarmedGamesTests(ToolDBTestCase):
    async def _seed_farming_day(self):
        # Two low-playtime Steam games last played on the same day (2023-11-14).
        epoch = 1700000000
        await make_steam_game(
            "Card Farm A", 1, playtime_minutes=30, rtime_last_played=epoch
        )
        await make_steam_game(
            "Card Farm B", 2, playtime_minutes=60, rtime_last_played=epoch
        )

    async def test_dry_run_reports_candidates_without_marking(self):
        await self._seed_farming_day()
        result = await admin.detect_farmed_games(dry_run=True, min_games_per_day=2)
        self.assertEqual(
            set(result),
            {
                "farming_days",
                "candidates",
                "steam_appids",
                "threshold_hours",
                "dry_run",
                "sample_games",
            },
        )
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["steam_appids"], [1, 2])
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(result["farming_days"]), 1)
        day = result["farming_days"][0]
        self.assertEqual(day["date"], "2023-11-14")
        self.assertEqual(day["game_count"], 2)
        self.assertEqual(day["median_playtime_hours"], round((0.5 + 1.0) / 2, 2))
        # dry run leaves is_farmed untouched
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM games WHERE is_farmed = 1"
            )
        self.assertEqual(row["c"], 0)

    async def test_non_dry_run_marks_games(self):
        await self._seed_farming_day()
        result = await admin.detect_farmed_games(dry_run=False, min_games_per_day=2)
        self.assertFalse(result["dry_run"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM games WHERE is_farmed = 1"
            )
        self.assertEqual(row["c"], 2)

    async def test_below_threshold_no_farming_day(self):
        await make_steam_game("Lonely", 1, playtime_minutes=30, rtime_last_played=1700000000)
        result = await admin.detect_farmed_games(dry_run=True, min_games_per_day=8)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["farming_days"], [])


class SetNintendoSessionValidationTests(ToolDBTestCase):
    async def test_invalid_json_returns_error(self):
        result = await admin.set_nintendo_session("not json{")
        self.assertFalse(result["success"])
        self.assertIn("Invalid JSON", result["error"])

    async def test_non_object_or_array_rejected(self):
        result = await admin.set_nintendo_session(json.dumps(42))
        self.assertEqual(
            result, {"success": False, "error": "Expected a JSON object or array"}
        )

    async def test_empty_cookies_rejected(self):
        result = await admin.set_nintendo_session(json.dumps({}))
        self.assertEqual(
            result, {"success": False, "error": "No valid cookies found in input"}
        )

    async def test_valid_cookies_write_succeeds(self):
        import os
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = os.path.join(tmp, "nested", "cookies.json")
            with patch.dict(os.environ, {"NINTENDO_COOKIES_FILE": cookie_path}):
                result = await admin.set_nintendo_session(
                    json.dumps([{"name": "id_token", "value": "abc"}])
                )
            self.assertTrue(result["success"])
            self.assertEqual(result["cookie_count"], 1)
            self.assertEqual(result["path"], cookie_path)
            with open(cookie_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"id_token": "abc"})
