"""Tests for gamelib_mcp.data.steam_reviews.sync_steam_reviews return shape."""

from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, make_steam_game
from gamelib_mcp.data import steam_reviews


class SyncSteamReviewsReturnTests(ToolDBTestCase):
    async def test_reports_volume_and_distinct_games(self):
        # Two scraped review rows for the same appid collapse onto one
        # UNIQUE(game_id, source) rating: volume 2, distinct game 1. The return
        # must expose both so the gap reads as dedup, not a lost write.
        await make_steam_game("Hades", 1, steam_review_score=8)
        scraped = [
            {"appid": 1, "vote": 1, "text": "great"},
            {"appid": 1, "vote": 1, "text": "still great"},
        ]
        with patch.object(
            steam_reviews, "_scrape_all_pages", AsyncMock(return_value=scraped)
        ):
            result = await steam_reviews.sync_steam_reviews()

        self.assertEqual(result["scraped_rows"], 2)
        self.assertEqual(result["rows_upserted"], 2)
        self.assertEqual(result["distinct_games_after"], 1)

    async def test_unmatched_appids_do_not_count(self):
        await make_steam_game("Hades", 1, steam_review_score=8)
        scraped = [
            {"appid": 1, "vote": 1, "text": "great"},
            {"appid": 999, "vote": -1, "text": "unowned, unmatched"},
        ]
        with patch.object(
            steam_reviews, "_scrape_all_pages", AsyncMock(return_value=scraped)
        ):
            result = await steam_reviews.sync_steam_reviews()

        self.assertEqual(result["scraped_rows"], 2)
        self.assertEqual(result["rows_upserted"], 1)
        self.assertEqual(result["distinct_games_after"], 1)
