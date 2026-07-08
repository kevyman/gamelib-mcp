"""Characterization tests for gamelib_mcp.tools.stats."""

from conftest import ToolDBTestCase, make_steam_game, seed_game, add_platform, add_rating
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import stats
from gamelib_mcp.tools.platforms import update_game


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
                "unknown_playtime",
                "unknown_pct",
                "farmed_games",
                "playing",
                "completed",
                "abandoned",
                "evergreen",
                "unplayed_with_hltb",
                "backlog_hours_hltb",
                "weekly_pace_hours",
                "years_to_clear_backlog",
                "most_played_genre_in_backlog",
                "highest_rated_unplayed_metacritic",
                "highest_rated_unplayed_opencritic",
                "highest_rated_unplayed_personal",
                "unplayed_spend",
            },
        )
        self.assertEqual(result["total_library"], 0)
        self.assertEqual(result["played"], 0)
        self.assertEqual(result["played_pct"], 0)
        self.assertEqual(result["unknown_playtime"], 0)
        self.assertEqual(result["unknown_pct"], 0)
        self.assertIsNone(result["years_to_clear_backlog"])
        self.assertIsNone(result["most_played_genre_in_backlog"])
        self.assertEqual(result["unplayed_spend"], {"totals": [], "top": []})

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
        self.assertEqual(result["unknown_playtime"], 0)
        self.assertEqual(result["unknown_pct"], 0)
        self.assertEqual(
            result["played_pct"] + result["unplayed_pct"] + result["unknown_pct"], 100
        )

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

    async def test_unknown_playtime_excluded_from_backlog(self):
        # Manually-added / GOG-style game: NULL playtime is "unknown", not
        # confirmed backlog. It must not pollute unplayed counts or metrics.
        gid = await seed_game("Manual", hltb_main=10)
        await add_platform(gid, "gog")  # no playtime -> NULL
        await make_steam_game("RealBacklog", 1, playtime_minutes=0, hltb_main=20)
        result = await stats.get_backlog_stats()
        self.assertEqual(result["total_library"], 2)
        self.assertEqual(result["unplayed"], 1)            # RealBacklog only
        self.assertEqual(result["unknown_playtime"], 1)    # Manual
        self.assertEqual(result["played"], 0)
        self.assertEqual(result["unplayed_with_hltb"], 1)  # excludes unknown
        self.assertEqual(result["backlog_hours_hltb"], 20) # excludes Manual's 10h

    async def test_completed_game_with_unknown_playtime_counts_as_played(self):
        # e.g. a GOG game with no playtime tracking, marked completed by hand.
        gid = await seed_game("Chrono Trigger")
        await add_platform(gid, "gog")  # no playtime -> NULL
        await update_game(game_id=gid, completion_status="completed")
        result = await stats.get_backlog_stats()
        self.assertEqual(result["played"], 1)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["unknown_playtime"], 0)

    async def test_abandoned_game_excluded_from_backlog_hours(self):
        gid = await make_steam_game("Starfield", 1, playtime_minutes=0, hltb_main=40.0)
        await update_game(game_id=gid, completion_status="abandoned")
        result = await stats.get_backlog_stats()
        self.assertEqual(result["backlog_hours_hltb"], 0)
        self.assertEqual(result["unplayed_with_hltb"], 0)
        self.assertEqual(result["abandoned"], 1)
        # Still counted as unplayed by the pure playtime signal (0 minutes) —
        # only the backlog-hours/hltb aggregates exclude abandoned games.
        self.assertEqual(result["unplayed"], 1)

    async def test_playing_count(self):
        gid = await make_steam_game("In Progress", 1, playtime_minutes=120)
        await update_game(game_id=gid, completion_status="playing")
        result = await stats.get_backlog_stats()
        self.assertEqual(result["playing"], 1)

    async def test_evergreen_game_excluded_from_backlog_hours_and_counted(self):
        gid = await make_steam_game(
            "Rocket League", 1, playtime_minutes=0, hltb_main=40.0
        )
        await update_game(game_id=gid, completion_status="evergreen")
        result = await stats.get_backlog_stats()
        self.assertEqual(result["backlog_hours_hltb"], 0)
        self.assertEqual(result["unplayed_with_hltb"], 0)
        self.assertEqual(result["evergreen"], 1)
        # Still counted as unplayed by the pure playtime signal (0 minutes) —
        # only the backlog-hours/hltb aggregates exclude evergreen games.
        self.assertEqual(result["unplayed"], 1)

    async def test_unowned_stub_playtime_does_not_leak_into_aggregates(self):
        # A stale/manual owned=0 stub's playtime must not feed play_state or
        # backlog aggregates: with owned steam at 0 minutes and a 600-minute
        # owned=0 stub, the game is still UNPLAYED backlog (before the join
        # guard, the stub's minutes marked it 'played' and dropped its HLTB
        # hours from the backlog).
        gid = await make_steam_game("Doom", 1, playtime_minutes=0, hltb_main=10)
        await add_platform(gid, "epic", playtime_minutes=600, owned=0)

        result = await stats.get_backlog_stats()

        self.assertEqual(result["total_library"], 1)
        self.assertEqual(result["played"], 0)
        self.assertEqual(result["unplayed"], 1)
        self.assertEqual(result["unplayed_with_hltb"], 1)
        self.assertEqual(result["backlog_hours_hltb"], 10)

    async def _price_platform(self, game_id: int, platform: str, price: float,
                              currency: str = "USD") -> None:
        async with db_module.get_db() as db:
            gpid = await db.execute_fetchone(
                "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
                (game_id, platform),
            )
        await db_module.set_platform_acquisition(
            gpid["id"], {"price_paid": price, "price_currency": currency}
        )

    async def test_unplayed_spend_counts_priced_unplayed_only(self):
        played = await make_steam_game("Played Purchase", 1, playtime_minutes=600)
        await self._price_platform(played, "steam", 59.99)
        unplayed = await make_steam_game("Shelfware", 2, playtime_minutes=0)
        await self._price_platform(unplayed, "steam", 39.99)
        also_unplayed = await make_steam_game("More Shelfware", 3, playtime_minutes=0)
        await self._price_platform(also_unplayed, "steam", 10.00)
        # Priced but free (0) never counts; unpriced-unplayed never counts.
        free = await make_steam_game("Epic Freebie", 4, playtime_minutes=0)
        await self._price_platform(free, "steam", 0.0)
        await make_steam_game("Unpriced Backlog", 5, playtime_minutes=0)

        result = await stats.get_backlog_stats()

        spend = result["unplayed_spend"]
        self.assertEqual(
            spend["totals"], [{"currency": "USD", "spent": 49.99, "count": 2}]
        )
        self.assertEqual(
            [(t["name"], t["price_paid"], t["currency"]) for t in spend["top"]],
            [("Shelfware", 39.99, "USD"), ("More Shelfware", 10.0, "USD")],
        )
        top = spend["top"][0]
        self.assertEqual(set(top), {"game_id", "name", "platform", "price_paid", "currency"})
        self.assertEqual(top["platform"], "steam")

    async def test_unplayed_spend_groups_by_currency(self):
        usd = await make_steam_game("USD Buy", 1, playtime_minutes=0)
        await self._price_platform(usd, "steam", 20.0, "USD")
        eur = await make_steam_game("EUR Buy", 2, playtime_minutes=0)
        await self._price_platform(eur, "steam", 30.0, "EUR")

        result = await stats.get_backlog_stats()

        self.assertEqual(
            result["unplayed_spend"]["totals"],
            [
                {"currency": "EUR", "spent": 30.0, "count": 1},
                {"currency": "USD", "spent": 20.0, "count": 1},
            ],
        )

    async def test_unplayed_spend_includes_unknown_playtime_and_excludes_written_off(self):
        # NULL playtime (GOG-style) with a price: still effectively unplayed.
        unknown = await seed_game("Priced GOG Mystery")
        await add_platform(unknown, "gog")  # no playtime -> NULL
        await self._price_platform(unknown, "gog", 15.0)
        # Written-off statuses leave the backlog, mirroring backlog-hours.
        abandoned = await make_steam_game("Abandoned Buy", 1, playtime_minutes=0)
        await self._price_platform(abandoned, "steam", 25.0)
        await update_game(game_id=abandoned, completion_status="abandoned")
        completed = await seed_game("Completed On GOG")
        await add_platform(completed, "gog")
        await self._price_platform(completed, "gog", 35.0)
        await update_game(game_id=completed, completion_status="completed")

        result = await stats.get_backlog_stats()

        self.assertEqual(
            result["unplayed_spend"]["totals"],
            [{"currency": "USD", "spent": 15.0, "count": 1}],
        )
        self.assertEqual(
            [t["name"] for t in result["unplayed_spend"]["top"]],
            ["Priced GOG Mystery"],
        )

    async def test_wishlist_only_game_excluded_from_totals(self):
        # A wishlist sync creates a games row + a game_wishlist row with zero
        # game_platforms rows. is_primary_library_item is a content-type flag
        # (game vs DLC), not ownership — this row must not inflate backlog
        # totals, unplayed counts, or backlog hours (its 100h hltb_main would
        # leak into backlog_hours_hltb if the ownership guard were missing).
        await make_steam_game("Owned Backlog", 1, playtime_minutes=0, hltb_main=10)
        wishlist_only = await seed_game("Persona 3 Reload", hltb_main=100)
        await db_module.upsert_wishlist_entry(wishlist_only, "switch2", source="dekudeals")

        result = await stats.get_backlog_stats()

        self.assertEqual(result["total_library"], 1)
        self.assertEqual(result["unplayed"], 1)
        self.assertEqual(result["unplayed_with_hltb"], 1)
        self.assertEqual(result["backlog_hours_hltb"], 10)
