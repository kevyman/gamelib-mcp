"""Characterization tests for gamelib_mcp.tools.series: get_series_breakdown
and discover_series_gaps.
"""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, add_platform, add_rating, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.igdb import IGDBRequestFailure, SeriesMember
from gamelib_mcp.tools import series

_IGDB_ENV = {"TWITCH_CLIENT_ID": "test-client", "TWITCH_CLIENT_SECRET": "test-secret"}


async def link_series(game_id: int, kind: str, igdb_id: int, name: str) -> None:
    await db_module.upsert_game_series_links(game_id, [(kind, igdb_id, name)])


async def set_igdb_id(game_id: int, igdb_id: int) -> None:
    async with db_module.get_db() as db:
        await db.execute("UPDATE games SET igdb_id = ? WHERE id = ?", (igdb_id, game_id))
        await db.commit()


async def add_wishlist(game_id: int, platform: str, *, source: str = "manual") -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with db_module.get_db() as db:
        await db.execute(
            """INSERT INTO game_wishlist (game_id, platform, wishlisted_at, source)
               VALUES (?, ?, ?, ?)""",
            (game_id, platform, now, source),
        )
        await db.commit()


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


class DiscoverSeriesGapsTests(ToolDBTestCase):
    async def test_unconfigured_without_igdb_credentials(self):
        backup = {
            key: os.environ.pop(key, None) for key in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")
        }
        try:
            result = await series.discover_series_gaps()
        finally:
            for key, value in backup.items():
                if value is not None:
                    os.environ[key] = value

        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["series_checked"], 0)
        self.assertIn("TWITCH_CLIENT_ID", result["error_summary"])

    async def test_min_owned_filters_series(self):
        solo = await seed_game("Mass Effect")
        await link_series(solo, "collection", 10, "Mass Effect")
        b1 = await seed_game("Dragon Age")
        b2 = await seed_game("Dragon Age 2")
        for gid in (b1, b2):
            await link_series(gid, "collection", 20, "Dragon Age")

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=[]),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        self.assertEqual([r["series_name"] for r in result["results"]], ["Dragon Age"])
        self.assertEqual(result["series_checked"], 1)

    async def test_excludes_owned_and_wishlisted_igdb_ids(self):
        base = await seed_game("Kirby's Dream Land")
        await link_series(base, "franchise", 30, "Kirby")
        second = await seed_game("Kirby's Dream Land 2")
        await link_series(second, "franchise", 30, "Kirby")
        await set_igdb_id(second, 200)

        # A wishlist-only row whose game already carries an igdb_id: still
        # counts as "have", even though it isn't a series member itself.
        wishlisted = await seed_game("Kirby's Adventure")
        await set_igdb_id(wishlisted, 300)
        await add_wishlist(wishlisted, "switch2")

        members = [
            SeriesMember(200, "Kirby's Dream Land 2", "1995-03-21", 0, []),
            SeriesMember(300, "Kirby's Adventure", "1993-03-23", 0, []),
            SeriesMember(400, "Kirby 64: The Crystal Shards", "2000-03-24", 0, []),
        ]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=members),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=1)

        entry = result["results"][0]
        gap_ids = {g["igdb_id"] for g in entry["gaps"]}
        self.assertEqual(gap_ids, {400})

    async def test_unreleased_filtered_unless_requested(self):
        a = await seed_game("Metroid Prime")
        b = await seed_game("Metroid Prime 2")
        for gid in (a, b):
            await link_series(gid, "franchise", 60, "Metroid")

        members = [
            SeriesMember(500, "Metroid Prime 4", "2099-01-01", 0, []),
            SeriesMember(501, "Metroid Prime Undated", None, 0, []),
            SeriesMember(502, "Metroid Prime 3", "2007-08-28", 0, []),
        ]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=members),
            ),
        ):
            default_result = await series.discover_series_gaps(min_owned=1)
            unreleased_result = await series.discover_series_gaps(
                min_owned=1, include_unreleased=True
            )

        default_gap_ids = {g["igdb_id"] for g in default_result["results"][0]["gaps"]}
        self.assertEqual(default_gap_ids, {502})

        unreleased_gap_ids = {g["igdb_id"] for g in unreleased_result["results"][0]["gaps"]}
        self.assertEqual(unreleased_gap_ids, {500, 501, 502})

    async def test_per_series_fetch_error_recorded_without_failing(self):
        a1 = await seed_game("Halo")
        a2 = await seed_game("Halo 2")
        for gid in (a1, a2):
            await link_series(gid, "franchise", 70, "Halo")
        b1 = await seed_game("Gears of War")
        b2 = await seed_game("Gears of War 2")
        for gid in (b1, b2):
            await link_series(gid, "franchise", 71, "Gears of War")
        # Give "Halo" the higher rating so it's ranked (and attempted) first.
        await add_rating(a1, "manual", 9.0, 9.0)
        await add_rating(b1, "manual", 5.0, 5.0)

        async def fake_get_members(kind, igdb_id, refresh=False):
            if igdb_id == 70:
                raise IGDBRequestFailure("IGDB is down")
            return []

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(side_effect=fake_get_members),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        self.assertEqual(result["series_checked"], 2)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["series"], "Halo")
        self.assertIn("IGDB is down", result["errors"][0]["error"])
        self.assertEqual([r["series_name"] for r in result["results"]], ["Gears of War"])

    async def test_available_on_maps_igdb_platforms(self):
        a = await seed_game("Hollow Knight")
        b = await seed_game("Hollow Knight: Voidheart")
        for gid in (a, b):
            await link_series(gid, "collection", 80, "Hollow Knight")

        members = [SeriesMember(600, "Hollow Knight: Silksong", "2025-09-04", 0, [130, 6])]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=members),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=1)

        gap = result["results"][0]["gaps"][0]
        self.assertEqual(gap["available_on"], ["steam", "switch2"])

    async def test_invalid_kind_raises(self):
        with self.assertRaises(Exception):
            await series.discover_series_gaps(kind="saga")
