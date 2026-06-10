"""Characterization tests for gamelib_mcp.tools.library."""

from conftest import ToolDBTestCase, make_steam_game, seed_game, add_platform
from gamelib_mcp.tools import library


def _platform_names(game: dict) -> list[str]:
    return [p["platform"] for p in game["platforms"]]


class SearchGamesTests(ToolDBTestCase):
    async def test_substring_match_and_formatted_shape(self):
        await make_steam_game(
            "Portal 2",
            620,
            playtime_minutes=600,
            tags=["puzzle"],
            metacritic_score=95,
            protondb_tier="platinum",
            steam_review_desc="Overwhelmingly Positive",
        )
        results = await library.search_games("portal")
        self.assertEqual(len(results), 1)
        game = results[0]
        self.assertEqual(
            set(game),
            {
                "game_id",
                "appid",
                "steam_appid",
                "name",
                "platforms",
                "playtime_hours",
                "playtime_2weeks_hours",
                "hltb_main",
                "metacritic_score",
                "protondb_tier",
                "steam_review_desc",
                "is_farmed",
            },
        )
        self.assertEqual(game["name"], "Portal 2")
        self.assertEqual(game["appid"], 620)
        self.assertEqual(game["steam_appid"], 620)
        self.assertEqual(game["playtime_hours"], 10.0)
        self.assertEqual(game["playtime_2weeks_hours"], 0.0)
        self.assertEqual(game["metacritic_score"], 95)
        self.assertEqual(game["protondb_tier"], "platinum")
        self.assertEqual(game["steam_review_desc"], "Overwhelmingly Positive")
        self.assertIs(game["is_farmed"], False)
        self.assertEqual(_platform_names(game), ["steam"])

    async def test_orders_by_playtime_desc(self):
        await make_steam_game("Alpha", 1, playtime_minutes=60)
        await make_steam_game("Beta", 2, playtime_minutes=600)
        results = await library.search_games("a")
        names = [g["name"] for g in results]
        self.assertEqual(names[0], "Beta")  # higher playtime first

    async def test_limit_applies(self):
        for i in range(5):
            await make_steam_game(f"Game {i}", 100 + i, playtime_minutes=i)
        results = await library.search_games("game", limit=2)
        self.assertEqual(len(results), 2)

    async def test_platform_filter_and_alias(self):
        gid = await seed_game("Zelda")
        await add_platform(gid, "switch2", playtime_minutes=120)
        await make_steam_game("Half-Life", 70, playtime_minutes=300)
        # alias: "nintendo" resolves to "switch2"
        results = await library.search_games("", platform="nintendo")
        names = [g["name"] for g in results]
        self.assertEqual(names, ["Zelda"])


class SearchGamesBatchTests(ToolDBTestCase):
    async def test_keyed_by_query(self):
        await make_steam_game("Portal", 400, playtime_minutes=120)
        await make_steam_game("Hades", 1145360, playtime_minutes=240)
        results = await library.search_games_batch(["portal", "hades", "missing"])
        self.assertEqual(set(results), {"portal", "hades", "missing"})
        self.assertEqual(len(results["portal"]), 1)
        self.assertEqual(results["portal"][0]["name"], "Portal")
        self.assertEqual(results["missing"], [])


class LibraryStatsTests(ToolDBTestCase):
    async def test_summary_counts_and_echoes(self):
        await make_steam_game("Played", 1, playtime_minutes=600)
        await make_steam_game("Unplayed", 2, playtime_minutes=0)
        farmed = await make_steam_game("Farmed", 3, playtime_minutes=30, is_farmed=1)
        self.assertTrue(farmed)
        stats = await library.get_library_stats()
        self.assertEqual(
            set(stats),
            {
                "total_games",
                "played",
                "unplayed",
                "farmed_games",
                "total_playtime_hours",
                "filter",
                "sort_by",
                "results",
            },
        )
        self.assertEqual(stats["total_games"], 3)
        self.assertEqual(stats["played"], 1)
        self.assertEqual(stats["unplayed"], 2)  # unplayed OR farmed
        self.assertEqual(stats["farmed_games"], 1)
        self.assertEqual(stats["total_playtime_hours"], round(630 / 60, 1))
        self.assertEqual(stats["filter"], "all")
        self.assertEqual(stats["sort_by"], "playtime")

    async def test_filter_unplayed(self):
        await make_steam_game("Played", 1, playtime_minutes=600)
        await make_steam_game("Unplayed", 2, playtime_minutes=0)
        stats = await library.get_library_stats(filter="unplayed")
        names = [g["name"] for g in stats["results"]]
        self.assertEqual(names, ["Unplayed"])

    async def test_sort_by_name_is_ascending(self):
        await make_steam_game("Beta", 1, playtime_minutes=10)
        await make_steam_game("Alpha", 2, playtime_minutes=999)
        stats = await library.get_library_stats(sort_by="name")
        names = [g["name"] for g in stats["results"]]
        self.assertEqual(names, ["Alpha", "Beta"])

    async def test_min_metacritic_filter(self):
        await make_steam_game("Great", 1, playtime_minutes=0, metacritic_score=95)
        await make_steam_game("Meh", 2, playtime_minutes=0, metacritic_score=60)
        stats = await library.get_library_stats(min_metacritic=90)
        names = [g["name"] for g in stats["results"]]
        self.assertEqual(names, ["Great"])
