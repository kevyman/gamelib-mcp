"""Characterization tests for gamelib_mcp.tools.series.get_series_breakdown."""

from conftest import ToolDBTestCase, seed_game, add_platform
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import series


async def link_series(game_id: int, kind: str, igdb_id: int, name: str) -> None:
    await db_module.upsert_game_series_links(game_id, [(kind, igdb_id, name)])


class SeriesBreakdownTests(ToolDBTestCase):
    async def test_empty_library(self):
        result = await series.get_series_breakdown()
        self.assertEqual(result["results"], [])
        self.assertEqual(result["total_matches"], 0)
        self.assertFalse(result["has_more"])
        self.assertEqual(result["counting_mode"], "distinct_games")

    async def _seed_fallout(self) -> None:
        base = await seed_game("Fallout: New Vegas", content_type="base_game")
        dlc = await seed_game(
            "Fallout New Vegas: Dead Money", content_type="dlc", is_primary_library_item=0
        )
        edition = await seed_game(
            "Fallout New Vegas Ultimate Edition",
            content_type="edition",
            is_primary_library_item=0,
        )
        remaster = await seed_game(
            "Fallout 4 Remastered", content_type="remaster", is_primary_library_item=1
        )
        for gid in (base, dlc, edition, remaster):
            await link_series(gid, "franchise", 100, "Fallout")

    async def test_counts_narrow_by_mode(self):
        await self._seed_fallout()

        default = await series.get_series_breakdown()
        self.assertEqual(
            set(default),
            {"results", "counting_mode", "total_matches", "has_more"},
        )
        self.assertEqual(default["total_matches"], 1)
        row = default["results"][0]
        self.assertEqual(row["series_name"], "Fallout")
        self.assertEqual(row["kind"], "franchise")
        # entries: all 4; distinct (primary): base + remaster; base only: base.
        self.assertEqual(row["count_entries"], 4)
        self.assertEqual(row["count_distinct_games"], 2)
        self.assertEqual(row["count_base_games_only"], 1)
        # default counting_mode == distinct_games drives `count`.
        self.assertEqual(row["count"], 2)

        entries_mode = await series.get_series_breakdown(counting_mode="entries")
        self.assertEqual(entries_mode["results"][0]["count"], 4)

        base_mode = await series.get_series_breakdown(counting_mode="base_games_only")
        self.assertEqual(base_mode["results"][0]["count"], 1)

    async def test_kind_filter(self):
        col = await seed_game("Assassin's Creed II")
        await link_series(col, "collection", 1, "Assassin's Creed")
        fr = await seed_game("Star Wars: KOTOR")
        await link_series(fr, "franchise", 2, "Star Wars")

        collections = await series.get_series_breakdown(kind="collection")
        self.assertEqual([r["series_name"] for r in collections["results"]], ["Assassin's Creed"])

        franchises = await series.get_series_breakdown(kind="franchise")
        self.assertEqual([r["series_name"] for r in franchises["results"]], ["Star Wars"])

        both = await series.get_series_breakdown()
        self.assertEqual(both["total_matches"], 2)

    async def test_min_games_filter(self):
        a1 = await seed_game("Mass Effect")
        a2 = await seed_game("Mass Effect 2")
        for gid in (a1, a2):
            await link_series(gid, "collection", 10, "Mass Effect")
        solo = await seed_game("Solo Game")
        await link_series(solo, "collection", 11, "Solo Series")

        result = await series.get_series_breakdown(min_games=2)
        self.assertEqual([r["series_name"] for r in result["results"]], ["Mass Effect"])
        self.assertEqual(result["total_matches"], 1)

    async def test_ranking_order(self):
        for i in range(3):
            gid = await seed_game(f"Borderlands {i}")
            await link_series(gid, "collection", 20, "Borderlands")
        small = await seed_game("Bastion")
        await link_series(small, "collection", 21, "Bastion Series")

        result = await series.get_series_breakdown()
        names = [r["series_name"] for r in result["results"]]
        self.assertEqual(names[0], "Borderlands")
        self.assertEqual(result["results"][0]["count"], 3)

    async def test_platform_filter(self):
        steam_only = await seed_game("Halo: CE")
        await add_platform(steam_only, "steam", playtime_minutes=120)
        epic_game = await seed_game("Halo Infinite")
        await add_platform(epic_game, "epic", playtime_minutes=60)
        for gid in (steam_only, epic_game):
            await link_series(gid, "franchise", 30, "Halo")

        steam = await series.get_series_breakdown(platform="steam")
        self.assertEqual(steam["results"][0]["count"], 1)
        self.assertEqual(steam["results"][0]["total_playtime_hours"], 2.0)

        # alias resolution: "nintendo" -> switch2 (no owners here)
        nintendo = await series.get_series_breakdown(platform="nintendo")
        self.assertEqual(nintendo["results"], [])

    async def test_platform_filter_excludes_non_owned(self):
        # A stale/manual game_platforms row with owned=0 must not contribute to
        # platform-scoped counts, playtime, or include_games results.
        owned = await seed_game("Forza Horizon 5")
        await add_platform(owned, "steam", playtime_minutes=120, owned=1)
        not_owned = await seed_game("Forza Motorsport")
        await add_platform(not_owned, "steam", playtime_minutes=999, owned=0)
        for gid in (owned, not_owned):
            await link_series(gid, "franchise", 50, "Forza")

        result = await series.get_series_breakdown(platform="steam", include_games=True)
        row = result["results"][0]
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["total_playtime_hours"], 2.0)
        self.assertEqual(row["included_games"], ["Forza Horizon 5"])

    async def test_include_games(self):
        await self._seed_fallout()
        result = await series.get_series_breakdown(include_games=True)
        row = result["results"][0]
        self.assertIn("Fallout: New Vegas", row["included_games"])
        self.assertIn("Fallout 4 Remastered", row["included_games"])
        reasons = {e["name"]: e["reason"] for e in row["collapsed_entries"]}
        self.assertEqual(reasons["Fallout New Vegas: Dead Money"], "dlc")
        self.assertEqual(reasons["Fallout New Vegas Ultimate Edition"], "edition")

    async def test_pagination(self):
        for i in range(3):
            gid = await seed_game(f"Series {i} Game")
            await link_series(gid, "collection", 40 + i, f"Series {i}")

        page1 = await series.get_series_breakdown(limit=2, offset=0)
        self.assertEqual(len(page1["results"]), 2)
        self.assertEqual(page1["total_matches"], 3)
        self.assertTrue(page1["has_more"])

        page2 = await series.get_series_breakdown(limit=2, offset=2)
        self.assertEqual(len(page2["results"]), 1)
        self.assertFalse(page2["has_more"])

    async def test_invalid_counting_mode_raises(self):
        with self.assertRaises(Exception):
            await series.get_series_breakdown(counting_mode="bogus")
