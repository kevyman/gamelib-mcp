"""Characterization tests for gamelib_mcp.tools.library."""

from fastmcp.exceptions import ToolError

from conftest import ToolDBTestCase, make_steam_game, seed_game, add_platform, add_game_alias
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import library
from gamelib_mcp.tools.common import MAX_RESULT_LIMIT
from gamelib_mcp.tools.platforms import update_game


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
                "series",
                "platforms",
                "playtime_hours",
                "playtime_2weeks_hours",
                "hltb_main",
                "metacritic_score",
                "opencritic_score",
                "protondb_tier",
                "steam_review_desc",
                "is_farmed",
                "completion_status",
                "content_type",
                "parent_game_id",
                "is_primary_library_item",
                "play_state",
                "owned",
                "wishlisted",
            },
        )
        self.assertEqual(game["name"], "Portal 2")
        self.assertEqual(game["play_state"], "played")
        self.assertEqual(game["appid"], 620)
        self.assertEqual(game["steam_appid"], 620)
        self.assertEqual(game["playtime_hours"], 10.0)
        self.assertEqual(game["playtime_2weeks_hours"], 0.0)
        self.assertEqual(game["metacritic_score"], 95)
        self.assertEqual(game["protondb_tier"], "platinum")
        self.assertEqual(game["steam_review_desc"], "Overwhelmingly Positive")
        self.assertIs(game["is_farmed"], False)
        self.assertIs(game["owned"], True)
        self.assertIs(game["wishlisted"], False)
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

    async def test_search_matches_edition_alias_and_returns_parent_game(self):
        gid = await seed_game("Fallout: New Vegas")
        await add_platform(gid, "gog")
        await add_game_alias(gid, "Fallout New Vegas Ultimate Edition", alias_type="edition")

        results = await library.search_games("fallout new vegas ultimate", response_format="detailed")

        self.assertEqual(results["total_matches"], 1)
        game = results["results"][0]
        self.assertEqual(game["game_id"], gid)
        self.assertEqual(game["name"], "Fallout: New Vegas")
        self.assertEqual(game["match_type"], "alias")
        self.assertEqual(game["matched_alias"], "Fallout New Vegas Ultimate Edition")

    async def test_search_excludes_nested_related_content_by_default(self):
        parent_id = await seed_game("Fallout: New Vegas")
        await add_platform(parent_id, "steam")
        dlc_id = await seed_game(
            "Fallout New Vegas: Dead Money",
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        await add_platform(dlc_id, "epic")

        results = await library.search_games("fallout")

        self.assertEqual([game["name"] for game in results["results"]], ["Fallout: New Vegas"])

    async def test_search_finds_nested_content_via_fallback(self):
        parent_id = await seed_game("Fallout: New Vegas")
        await add_platform(parent_id, "steam")
        dlc_id = await seed_game(
            "Fallout New Vegas: Dead Money",
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        await add_platform(dlc_id, "epic")

        results = await library.search_games("dead money")

        self.assertEqual(results["total_matches"], 1)
        game = results["results"][0]
        self.assertEqual(game["name"], "Fallout New Vegas: Dead Money")
        self.assertEqual(game["match_type"], "nested_content")
        self.assertEqual(game["parent_name"], "Fallout: New Vegas")

    async def test_search_prefers_primary_match_over_nested_when_both_match(self):
        parent_id = await seed_game("Civilization VI")
        await add_platform(parent_id, "steam")
        dlc_id = await seed_game(
            "Civilization VI: Gathering Storm",
            content_type="expansion",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        await add_platform(dlc_id, "steam")

        results = await library.search_games("civilization vi")

        self.assertEqual(results["total_matches"], 1)
        self.assertEqual(results["results"][0]["name"], "Civilization VI")
        self.assertNotIn("match_type", results["results"][0])

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

    async def test_fuzzy_fallback_respects_offset(self):
        await make_steam_game("Sekiro: Shadows Die Twice", 814380, playtime_minutes=344)
        results = await library.search_games("sekrio shadows die twice", limit=1, offset=1)
        self.assertEqual(results["results"], [])
        self.assertEqual(results["total_matches"], 1)
        self.assertFalse(results["has_more"])

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


class LibraryStatsTagGenreFilterTests(ToolDBTestCase):
    async def test_genre_filter_is_case_insensitive(self):
        await make_steam_game("Witcher 3", 1, genres=["RPG"], hltb_main=50)
        await make_steam_game("Doom", 2, genres=["Shooter"], hltb_main=12)
        results = await library.get_library_stats(genres=["rpg"])
        self.assertEqual([g["name"] for g in results["results"]], ["Witcher 3"])

    async def test_tag_filter_requires_every_entry(self):
        await make_steam_game("Hades", 1, tags=["Roguelike", "Action"])
        await make_steam_game("Dead Cells", 2, tags=["Roguelike"])
        results = await library.get_library_stats(tags=["roguelike", "action"])
        self.assertEqual([g["name"] for g in results["results"]], ["Hades"])

    async def test_combines_with_hltb_filter(self):
        await make_steam_game("Short RPG", 1, genres=["RPG"], hltb_main=8)
        await make_steam_game("Long RPG", 2, genres=["RPG"], hltb_main=80)
        results = await library.get_library_stats(genres=["RPG"], max_hltb_hours=10)
        self.assertEqual([g["name"] for g in results["results"]], ["Short RPG"])


class SearchGamesBatchTests(ToolDBTestCase):
    async def test_keyed_by_query(self):
        await make_steam_game("Portal", 400, playtime_minutes=120)
        await make_steam_game("Hades", 1145360, playtime_minutes=240)
        results = await library.search_games_batch(["portal", "hades", "missing"])
        self.assertEqual(set(results), {"portal", "hades", "missing"})
        self.assertEqual(len(results["portal"]), 1)
        self.assertEqual(results["portal"][0]["name"], "Portal")
        self.assertEqual(results["missing"], [])

    async def test_batch_resolves_nested_content_via_fallback(self):
        parent_id = await seed_game("Fallout: New Vegas")
        await add_platform(parent_id, "steam")
        dlc_id = await seed_game(
            "Fallout New Vegas: Dead Money",
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        await add_platform(dlc_id, "epic")

        results = await library.search_games_batch(["dead money"])

        self.assertEqual(len(results["dead money"]), 1)
        game = results["dead money"][0]
        self.assertEqual(game["name"], "Fallout New Vegas: Dead Money")
        self.assertEqual(game["match_type"], "nested_content")
        self.assertEqual(game["parent_name"], "Fallout: New Vegas")


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
                "unknown",
                "farmed_games",
                "total_playtime_hours",
                "filter",
                "sort_by",
                "spending",
                "addons",
                "results",
                "total_matches",
                "has_more",
            },
        )
        self.assertEqual(stats["total_games"], 3)
        self.assertEqual(stats["played"], 1)
        self.assertEqual(stats["unplayed"], 2)  # real-unplayed + farmed (play_state='unplayed')
        self.assertEqual(stats["unknown"], 0)
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

    async def test_library_stats_excludes_nested_related_content(self):
        parent_id = await seed_game("Sid Meier's Civilization IV")
        await add_platform(parent_id, "gog")
        expansion_id = await seed_game(
            "Sid Meier's Civilization IV: Warlords",
            content_type="expansion",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        await add_platform(expansion_id, "gog")

        stats = await library.get_library_stats()

        self.assertEqual(stats["total_games"], 1)
        self.assertEqual([game["name"] for game in stats["results"]], ["Sid Meier's Civilization IV"])

    async def test_filter_unknown_returns_null_playtime_games(self):
        await make_steam_game("RealUnplayed", 1, playtime_minutes=0)
        gid = await seed_game("Manual")
        await add_platform(gid, "gog")  # no playtime -> NULL
        unplayed = await library.get_library_stats(filter="unplayed")
        self.assertEqual([g["name"] for g in unplayed["results"]], ["RealUnplayed"])
        unknown = await library.get_library_stats(filter="unknown")
        self.assertEqual([g["name"] for g in unknown["results"]], ["Manual"])
        self.assertEqual(unknown["unknown"], 1)

    async def test_filter_playing_completed_abandoned(self):
        playing = await make_steam_game("Playing Now", 1, playtime_minutes=100)
        await update_game(game_id=playing, completion_status="playing")
        completed = await make_steam_game("Chrono Trigger", 2, playtime_minutes=0)
        await update_game(game_id=completed, completion_status="completed")
        abandoned = await make_steam_game("Starfield", 3, playtime_minutes=0)
        await update_game(game_id=abandoned, completion_status="abandoned")
        await make_steam_game("Untouched", 4, playtime_minutes=0)

        result = await library.get_library_stats(filter="playing")
        self.assertEqual([g["name"] for g in result["results"]], ["Playing Now"])

        result = await library.get_library_stats(filter="completed")
        self.assertEqual([g["name"] for g in result["results"]], ["Chrono Trigger"])
        self.assertEqual(result["results"][0]["completion_status"], "completed")

        result = await library.get_library_stats(filter="abandoned")
        self.assertEqual([g["name"] for g in result["results"]], ["Starfield"])

    async def test_filter_evergreen(self):
        evergreen = await make_steam_game("Rocket League", 1, playtime_minutes=100)
        await update_game(game_id=evergreen, completion_status="evergreen")
        await make_steam_game("Untouched", 2, playtime_minutes=0)

        result = await library.get_library_stats(filter="evergreen")
        self.assertEqual([g["name"] for g in result["results"]], ["Rocket League"])
        self.assertEqual(result["results"][0]["completion_status"], "evergreen")

    async def test_search_marks_unknown_playtime_with_null_hours(self):
        gid = await seed_game("Manual")
        await add_platform(gid, "gog")  # no playtime -> NULL
        results = await library.search_games("manual", response_format="detailed")
        game = results["results"][0]
        self.assertEqual(game["play_state"], "unknown")
        self.assertIsNone(game["playtime_hours"])

    async def test_spending_block_empty_library(self):
        stats = await library.get_library_stats()
        self.assertEqual(
            stats["spending"],
            {"totals": [], "owned_rows": 0, "priced_rows": 0, "coverage_pct": 0.0},
        )

    async def test_addons_block_empty_library(self):
        stats = await library.get_library_stats()
        self.assertEqual(
            stats["addons"],
            {"count": 0, "spend": {}, "top_parents": []},
        )

    async def test_spending_block_totals_and_coverage(self):
        priced_usd = await make_steam_game("Priced USD", 1, playtime_minutes=0)
        another_usd = await make_steam_game("Another USD", 2, playtime_minutes=100)
        priced_eur = await make_steam_game("Priced EUR", 3, playtime_minutes=0)
        await make_steam_game("Unpriced", 4, playtime_minutes=0)
        for gid, price, currency in (
            (priced_usd, 10.0, "USD"),
            (another_usd, 30.0, "USD"),
            (priced_eur, 20.0, "EUR"),
        ):
            async with db_module.get_db() as db:
                gp = await db.execute_fetchone(
                    "SELECT id FROM game_platforms WHERE game_id = ?", (gid,)
                )
            await db_module.set_platform_acquisition(
                gp["id"], {"price_paid": price, "price_currency": currency}
            )

        stats = await library.get_library_stats()

        spending = stats["spending"]
        self.assertEqual(spending["owned_rows"], 4)
        self.assertEqual(spending["priced_rows"], 3)
        self.assertEqual(spending["coverage_pct"], 75.0)
        # Per-currency, never summed across; ordered by total_spent DESC.
        self.assertEqual(
            spending["totals"],
            [
                {"currency": "USD", "total_spent": 40.0, "priced_rows": 2},
                {"currency": "EUR", "total_spent": 20.0, "priced_rows": 1},
            ],
        )

    async def test_spending_block_ignores_unowned_rows_and_filter_params(self):
        owned = await make_steam_game("Owned Priced", 1, playtime_minutes=600)
        async with db_module.get_db() as db:
            gp = await db.execute_fetchone(
                "SELECT id FROM game_platforms WHERE game_id = ?", (owned,)
            )
        await db_module.set_platform_acquisition(
            gp["id"], {"price_paid": 12.5, "price_currency": "USD"}
        )
        # owned=0 stub with a price must not count.
        stub = await seed_game("Unowned Stub")
        stub_gpid = await add_platform(stub, "epic", owned=0)
        await db_module.set_platform_acquisition(stub_gpid, {"price_paid": 99.0})

        # filter=unplayed excludes the played game from results, but the
        # spending summary is library-wide and unaffected.
        stats = await library.get_library_stats(filter="unplayed")

        self.assertEqual(stats["results"], [])
        self.assertEqual(stats["spending"]["owned_rows"], 1)
        self.assertEqual(stats["spending"]["priced_rows"], 1)
        self.assertEqual(
            stats["spending"]["totals"],
            [{"currency": "USD", "total_spent": 12.5, "priced_rows": 1}],
        )


class LibraryStatsContentTests(ToolDBTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.parent_id = await seed_game("Sid Meier's Civilization VI")
        await add_platform(self.parent_id, "steam")
        self.dlc_id = await seed_game(
            "Civilization VI: Gathering Storm",
            content_type="expansion",
            parent_game_id=self.parent_id,
            is_primary_library_item=0,
        )
        dlc_gpid = await add_platform(self.dlc_id, "steam")
        await db_module.set_platform_acquisition(
            dlc_gpid, {"price_paid": 25.0, "price_currency": "USD"}
        )
        # Unowned nested row: no game_platforms row at all, so it must not
        # count toward the addons block or content="addons"/"all" listings.
        await seed_game(
            "Civilization VI: Rise and Fall",
            content_type="expansion",
            parent_game_id=self.parent_id,
            is_primary_library_item=0,
        )

    async def test_default_content_excludes_addons_but_reports_addons_block(self):
        stats = await library.get_library_stats()

        self.assertEqual(stats["total_games"], 1)
        self.assertEqual(
            [g["name"] for g in stats["results"]], ["Sid Meier's Civilization VI"]
        )
        self.assertEqual(stats["addons"]["count"], 1)
        self.assertEqual(stats["addons"]["spend"], {"USD": 25.0})
        self.assertEqual(
            stats["addons"]["top_parents"],
            [
                {
                    "game_id": self.parent_id,
                    "name": "Sid Meier's Civilization VI",
                    "addon_count": 1,
                }
            ],
        )

    async def test_content_addons_lists_only_owned_nested_rows(self):
        stats = await library.get_library_stats(content="addons")

        self.assertEqual(stats["total_games"], 1)
        self.assertEqual(
            [g["name"] for g in stats["results"]], ["Civilization VI: Gathering Storm"]
        )

    async def test_content_all_lists_both_primary_and_owned_nested(self):
        stats = await library.get_library_stats(content="all")

        self.assertEqual(stats["total_games"], 2)
        self.assertEqual(
            {g["name"] for g in stats["results"]},
            {"Sid Meier's Civilization VI", "Civilization VI: Gathering Storm"},
        )

    async def test_invalid_content_raises(self):
        with self.assertRaisesRegex(ToolError, "Unknown content 'bogus'"):
            await library.get_library_stats(content="bogus")

    async def test_addons_block_is_library_wide_and_ignores_filter_params(self):
        # The addons block is computed independently of filter/tags/genres
        # (same pattern as `spending`) — an unrelated genre filter that
        # zeroes out `results` must not affect it.
        stats = await library.get_library_stats(genres=["nonexistent-genre"])
        self.assertEqual(stats["results"], [])
        self.assertEqual(stats["addons"]["count"], 1)
        self.assertEqual(stats["addons"]["spend"], {"USD": 25.0})


class WishlistOnlyOwnershipFlagTests(ToolDBTestCase):
    """A wishlist sync creates a games row + game_wishlist row with zero
    game_platforms rows (e.g. prod: Persona 3 Reload, wishlist-only). Such a
    row is still is_primary_library_item=1 (content-type, not ownership) and
    must be presented as owned:false/wishlisted:true rather than looking like
    an owned game with no platforms.
    """

    async def test_search_games_flags_wishlist_only_game(self):
        gid = await seed_game("Persona 3 Reload")
        await db_module.upsert_wishlist_entry(gid, "switch2", source="dekudeals")

        results = await library.search_games("persona 3 reload", response_format="detailed")

        self.assertEqual(results["total_matches"], 1)
        game = results["results"][0]
        self.assertIs(game["owned"], False)
        self.assertIs(game["wishlisted"], True)
        self.assertEqual(game["platforms"], [])

    async def test_get_library_stats_excludes_wishlist_only_game(self):
        await make_steam_game("Owned Game", 1, playtime_minutes=60)
        wishlist_only = await seed_game("Persona 3 Reload")
        await db_module.upsert_wishlist_entry(wishlist_only, "switch2", source="dekudeals")

        results = await library.get_library_stats()

        self.assertEqual(results["total_games"], 1)
        self.assertEqual([g["name"] for g in results["results"]], ["Owned Game"])

    async def test_unowned_stub_playtime_excluded_from_aggregates(self):
        # An owned=0 stub's 600 minutes aren't real playtime anywhere — search
        # and library-stats playtime/play_state must derive from the owned
        # steam row's 60 minutes only.
        gid = await make_steam_game("Doom", 1, playtime_minutes=60)
        await add_platform(gid, "epic", playtime_minutes=600, owned=0)

        results = await library.search_games("doom")
        game = results["results"][0]
        self.assertEqual(game["playtime_hours"], 1.0)
        self.assertEqual(game["play_state"], "played")

        stats = await library.get_library_stats()
        self.assertEqual(stats["total_playtime_hours"], 1.0)
