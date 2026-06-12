"""Characterization tests for gamelib_mcp.tools.library."""

from fastmcp.exceptions import ToolError

from conftest import ToolDBTestCase, make_steam_game, seed_game, add_platform
from gamelib_mcp.tools import library
from gamelib_mcp.tools.common import MAX_RESULT_LIMIT


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
        results = await library.search_games("portal", response_format="detailed")
        self.assertEqual(set(results), {"results", "total_matches", "has_more"})
        self.assertEqual(results["total_matches"], 1)
        self.assertFalse(results["has_more"])
        game = results["results"][0]
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
                "opencritic_score",
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

    async def test_orders_by_playtime_desc_within_same_match_rank(self):
        await make_steam_game("Game Alpha", 1, playtime_minutes=60)
        await make_steam_game("Game Beta", 2, playtime_minutes=600)
        results = await library.search_games("game")
        names = [g["name"] for g in results["results"]]
        self.assertEqual(names[0], "Game Beta")  # higher playtime first

    async def test_orders_by_match_relevance_before_playtime(self):
        await make_steam_game("Hades II", 2, playtime_minutes=6000)
        await make_steam_game("Hades", 1, playtime_minutes=10)
        results = await library.search_games("hades")
        names = [g["name"] for g in results["results"]]
        # Exact normalized match outranks the higher-playtime prefix match.
        self.assertEqual(names, ["Hades", "Hades II"])

    async def test_limit_applies_and_reports_more(self):
        for i in range(5):
            await make_steam_game(f"Game {i}", 100 + i, playtime_minutes=i)
        results = await library.search_games("game", limit=2)
        self.assertEqual(len(results["results"]), 2)
        self.assertEqual(results["total_matches"], 5)
        self.assertTrue(results["has_more"])

    async def test_offset_pages_results(self):
        for i in range(3):
            await make_steam_game(f"Game {i}", 100 + i, playtime_minutes=300 - i)
        first = await library.search_games("game", limit=1)
        second = await library.search_games("game", limit=1, offset=1)
        self.assertNotEqual(first["results"][0]["name"], second["results"][0]["name"])

    async def test_platform_filter_and_alias(self):
        gid = await seed_game("Zelda")
        await add_platform(gid, "switch2", playtime_minutes=120)
        await make_steam_game("Half-Life", 70, playtime_minutes=300)
        # alias: "nintendo" resolves to "switch2"
        results = await library.search_games("", platform="nintendo")
        names = [g["name"] for g in results["results"]]
        self.assertEqual(names, ["Zelda"])

    async def test_concise_drops_platform_arrays(self):
        await make_steam_game("Portal 2", 620, playtime_minutes=600, tags=["puzzle"])
        results = await library.search_games("portal")
        self.assertNotIn("platforms", results["results"][0])
        self.assertNotIn("tags", results["results"][0])

    async def test_like_wildcards_are_treated_literally(self):
        await make_steam_game("Portal 2", 620, playtime_minutes=600)
        # "%" must not match every game; only literal "%" would.
        results = await library.search_games("%")
        self.assertEqual(results["total_matches"], 0)

    async def test_underscore_normalizes_to_token_boundary(self):
        await make_steam_game("Hades", 1, playtime_minutes=10)
        await make_steam_game("Bastion", 2, playtime_minutes=10)
        # "_" is never a SQL wildcard: it becomes a token boundary, so "h_des"
        # token-matches "Hades" (h + des) but cannot match unrelated titles.
        results = await library.search_games("h_des")
        names = {g["name"] for g in results["results"]}
        self.assertEqual(names, {"Hades"})

    async def test_punctuation_in_title_does_not_block_match(self):
        await make_steam_game("Sekiro: Shadows Die Twice", 814380, playtime_minutes=344)
        results = await library.search_games("sekiro shadow")
        self.assertEqual(results["total_matches"], 1)
        self.assertEqual(results["results"][0]["name"], "Sekiro: Shadows Die Twice")
        self.assertNotIn("match_type", results["results"][0])

    async def test_punctuation_in_query_does_not_block_match(self):
        await make_steam_game("Don't Starve", 219740, playtime_minutes=100)
        results = await library.search_games("dont starve")
        self.assertEqual(results["total_matches"], 1)
        self.assertEqual(results["results"][0]["name"], "Don't Starve")

    async def test_misspelling_falls_back_to_fuzzy(self):
        await make_steam_game("Sekiro: Shadows Die Twice", 814380, playtime_minutes=344)
        results = await library.search_games("sekrio shadows die twice")
        self.assertEqual(results["total_matches"], 1)
        game = results["results"][0]
        self.assertEqual(game["name"], "Sekiro: Shadows Die Twice")
        self.assertEqual(game["match_type"], "fuzzy")

    async def test_fuzzy_fallback_respects_platform_filter(self):
        await make_steam_game("Sekiro: Shadows Die Twice", 814380, playtime_minutes=344)
        results = await library.search_games("sekrio shadows die twice", platform="gog")
        self.assertEqual(results["total_matches"], 0)

    async def test_negative_limit_is_clamped_not_unbounded(self):
        for i in range(3):
            await make_steam_game(f"Game {i}", 100 + i, playtime_minutes=i)
        results = await library.search_games("game", limit=-1)
        # SQLite treats LIMIT -1 as unbounded; clamp must cap the row count.
        self.assertLessEqual(len(results["results"]), MAX_RESULT_LIMIT)
        self.assertEqual(results["total_matches"], 3)

    async def test_huge_limit_is_clamped(self):
        await make_steam_game("Solo", 1, playtime_minutes=10)
        results = await library.search_games("solo", limit=10**9)
        self.assertEqual(len(results["results"]), 1)

    async def test_exposes_opencritic_score(self):
        await make_steam_game("Sekiro: Shadows Die Twice", 814380, opencritic_score=90)
        results = await library.search_games("sekiro")
        self.assertEqual(results["results"][0]["opencritic_score"], 90)


class LibraryStatsOpenCriticTests(ToolDBTestCase):
    async def test_min_opencritic_filters_and_excludes_unscored(self):
        await make_steam_game("Mighty", 1, opencritic_score=90)
        await make_steam_game("Weak", 2, opencritic_score=55)
        await make_steam_game("Unscored", 3)
        results = await library.get_library_stats(min_opencritic=80)
        self.assertEqual([g["name"] for g in results["results"]], ["Mighty"])

    async def test_sort_by_opencritic(self):
        await make_steam_game("Weak", 2, opencritic_score=55)
        await make_steam_game("Mighty", 1, opencritic_score=90)
        results = await library.get_library_stats(sort_by="opencritic")
        names = [g["name"] for g in results["results"]]
        self.assertEqual(names[:2], ["Mighty", "Weak"])


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
                "total_matches",
                "has_more",
            },
        )
        self.assertEqual(stats["total_games"], 3)
        self.assertEqual(stats["played"], 1)
        self.assertEqual(stats["unplayed"], 2)  # unplayed OR farmed
        self.assertEqual(stats["farmed_games"], 1)
        self.assertEqual(stats["total_playtime_hours"], round(630 / 60, 1))
        self.assertEqual(stats["filter"], "all")
        self.assertEqual(stats["sort_by"], "playtime")
        self.assertEqual(stats["total_matches"], 3)
        self.assertFalse(stats["has_more"])

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

    async def test_invalid_filter_raises(self):
        with self.assertRaisesRegex(ToolError, "Unknown filter 'bogus'"):
            await library.get_library_stats(filter="bogus")

    async def test_invalid_sort_by_raises(self):
        with self.assertRaisesRegex(ToolError, "Unknown sort_by 'bogus'"):
            await library.get_library_stats(sort_by="bogus")

    async def test_invalid_protondb_tier_raises(self):
        with self.assertRaisesRegex(ToolError, "Unknown protondb_tier 'diamond'"):
            await library.get_library_stats(protondb_tier="diamond")

    async def test_offset_and_has_more_for_result_list(self):
        for i in range(3):
            await make_steam_game(f"Game {i}", 100 + i, playtime_minutes=300 - i)
        stats = await library.get_library_stats(limit=1, offset=1)
        self.assertEqual(len(stats["results"]), 1)
        self.assertEqual(stats["total_matches"], 3)
        self.assertTrue(stats["has_more"])
