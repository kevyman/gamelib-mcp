"""Characterization tests for gamelib_mcp.tools.stats."""

from conftest import ToolDBTestCase, make_steam_game, seed_game, add_platform, add_rating
from gamelib_mcp.tools import stats


class BacklogStatsTests(ToolDBTestCase):
    async def test_empty_library(self):
        result = await stats.get_backlog_stats()
        self.assertEqual(
            set(result),
            {
                "total_library",
                "played",
                "played_pct",
                "unplayed",
                "unplayed_pct",
                "farmed_games",
                "unplayed_with_hltb",
                "backlog_hours_hltb",
                "weekly_pace_hours",
                "years_to_clear_backlog",
                "most_played_genre_in_backlog",
                "highest_rated_unplayed_metacritic",
                "highest_rated_unplayed_personal",
            },
        )
        self.assertEqual(result["total_library"], 0)
        self.assertEqual(result["played"], 0)
        self.assertEqual(result["played_pct"], 0)
        self.assertIsNone(result["years_to_clear_backlog"])
        self.assertIsNone(result["most_played_genre_in_backlog"])

    async def test_counts_and_percentages(self):
        await make_steam_game("Played", 1, playtime_minutes=600)
        await make_steam_game("Unplayed", 2, playtime_minutes=0, hltb_main=10)
        await make_steam_game("AlsoUnplayed", 3, playtime_minutes=0, hltb_main=20)
        result = await stats.get_backlog_stats()
        self.assertEqual(result["total_library"], 3)
        self.assertEqual(result["played"], 1)
        self.assertEqual(result["unplayed"], 2)
        self.assertEqual(result["played_pct"], round(1 / 3 * 100))
        self.assertEqual(result["unplayed_pct"], 100 - round(1 / 3 * 100))
        self.assertEqual(result["unplayed_with_hltb"], 2)
        self.assertEqual(result["backlog_hours_hltb"], 30)

    async def test_weekly_pace_and_years_to_clear(self):
        # 2 weeks of recent playtime: 1200 minutes -> weekly = 1200/2/60 = 10.0h
        await make_steam_game(
            "Active", 1, playtime_minutes=2000, playtime_2weeks_minutes=1200
        )
        await make_steam_game("Backlog", 2, playtime_minutes=0, hltb_main=520)
        result = await stats.get_backlog_stats()
        self.assertEqual(result["weekly_pace_hours"], 10.0)
        # 520 backlog hours / 10 per week / 52 weeks = 1.0 years
        self.assertEqual(result["years_to_clear_backlog"], round((520 / 10) / 52, 1))

    async def test_top_genre_and_best_unplayed(self):
        await make_steam_game(
            "RPG One", 1, playtime_minutes=0, genres=["RPG"], metacritic_score=88
        )
        await make_steam_game(
            "RPG Two", 2, playtime_minutes=0, genres=["RPG"], metacritic_score=95
        )
        gid = await make_steam_game("Rated", 3, playtime_minutes=0, genres=["Indie"])
        await add_rating(gid, "backloggd", 5.0, 9.5)
        result = await stats.get_backlog_stats()
        self.assertEqual(
            result["most_played_genre_in_backlog"], {"genre": "RPG", "count": 2}
        )
        self.assertEqual(
            result["highest_rated_unplayed_metacritic"], {"name": "RPG Two", "score": 95}
        )
        self.assertEqual(
            result["highest_rated_unplayed_personal"], {"name": "Rated", "score": 9.5}
        )
