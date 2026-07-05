"""Characterization tests for gamelib_mcp.tools.series: get_series_breakdown
and discover_series_gaps.
"""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, add_platform, add_rating, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.igdb import IGDBRequestFailure, SeriesMember
from gamelib_mcp.data.series_gaps import SeriesMembersResult
from gamelib_mcp.tools import series

_IGDB_ENV = {"TWITCH_CLIENT_ID": "test-client", "TWITCH_CLIENT_SECRET": "test-secret"}


def _members_result(members: list[SeriesMember], aliases: dict[int, int] | None = None) -> SeriesMembersResult:
    return SeriesMembersResult(members=list(members), aliases=dict(aliases or {}))


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


async def seed_owned_game(name: str, *, playtime_minutes: int | None = None) -> int:
    """Seed a game with an owned platform row — discover_series_gaps only
    counts games with a real owned game_platforms relationship."""
    game_id = await seed_game(name)
    await add_platform(game_id, "steam", playtime_minutes=playtime_minutes, owned=1)
    return game_id


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
        solo = await seed_owned_game("Mass Effect")
        await link_series(solo, "collection", 10, "Mass Effect")
        b1 = await seed_owned_game("Dragon Age")
        b2 = await seed_owned_game("Dragon Age 2")
        for gid in (b1, b2):
            await link_series(gid, "collection", 20, "Dragon Age")

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result([])),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        self.assertEqual([r["series_name"] for r in result["results"]], ["Dragon Age"])
        self.assertEqual(result["series_checked"], 1)

    async def test_owned_excludes_but_wishlisted_member_appears_annotated(self):
        base = await seed_owned_game("Kirby's Dream Land")
        await link_series(base, "franchise", 30, "Kirby")
        second = await seed_owned_game("Kirby's Dream Land 2")
        await link_series(second, "franchise", 30, "Kirby")
        await set_igdb_id(second, 200)

        # A wishlist-only row whose game already carries an igdb_id: does NOT
        # count as "have" anymore — it must surface as a gap, annotated
        # on_wishlist=true, rather than silently vanishing.
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
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=1)

        entry = result["results"][0]
        gaps_by_id = {g["igdb_id"]: g for g in entry["gaps"]}
        # 200 excluded (owned by direct igdb_id match); 300 and 400 are gaps,
        # but only 300 (wishlisted) is annotated.
        self.assertEqual(set(gaps_by_id), {300, 400})
        self.assertTrue(gaps_by_id[300]["on_wishlist"])
        self.assertFalse(gaps_by_id[400]["on_wishlist"])

    async def test_unreleased_filtered_unless_requested(self):
        a = await seed_owned_game("Metroid Prime")
        b = await seed_owned_game("Metroid Prime 2")
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
                AsyncMock(return_value=_members_result(members)),
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
        a1 = await seed_owned_game("Halo")
        a2 = await seed_owned_game("Halo 2")
        for gid in (a1, a2):
            await link_series(gid, "franchise", 70, "Halo")
        b1 = await seed_owned_game("Gears of War")
        b2 = await seed_owned_game("Gears of War 2")
        for gid in (b1, b2):
            await link_series(gid, "franchise", 71, "Gears of War")
        # Give "Halo" the higher rating so it's ranked (and attempted) first.
        await add_rating(a1, "manual", 9.0, 9.0)
        await add_rating(b1, "manual", 5.0, 5.0)

        async def fake_get_members(kind, igdb_id, refresh=False):
            if igdb_id == 70:
                raise IGDBRequestFailure("IGDB is down")
            return _members_result([])

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
        a = await seed_owned_game("Hollow Knight")
        b = await seed_owned_game("Hollow Knight: Voidheart")
        for gid in (a, b):
            await link_series(gid, "collection", 80, "Hollow Knight")

        members = [SeriesMember(600, "Hollow Knight: Silksong", "2025-09-04", 0, [130, 6])]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=1)

        gap = result["results"][0]["gaps"][0]
        self.assertEqual(gap["available_on"], ["steam", "switch2"])

    async def test_wishlist_only_games_do_not_count_toward_min_owned(self):
        # Wishlist sync creates games rows with no owned game_platforms row,
        # and IGDB backfill adds their series memberships — such entries must
        # not make a series rank as "owned".
        owned = await seed_owned_game("Zelda: Breath of the Wild")
        await link_series(owned, "franchise", 90, "Zelda")

        wishlist_only = await seed_game("Zelda: Tears of the Kingdom")
        await add_wishlist(wishlist_only, "switch2")
        await link_series(wishlist_only, "franchise", 90, "Zelda")

        unowned_stub = await seed_game("Zelda: Echoes of Wisdom")
        await add_platform(unowned_stub, "switch2", owned=0)
        await link_series(unowned_stub, "franchise", 90, "Zelda")

        members_mock = AsyncMock(return_value=_members_result([]))
        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch("gamelib_mcp.data.series_gaps.get_series_members_cached", members_mock),
        ):
            two_owned = await series.discover_series_gaps(min_owned=2)
            one_owned = await series.discover_series_gaps(min_owned=1)

        # Only 1 of the 3 memberships is actually owned.
        self.assertEqual(two_owned["series_checked"], 0)
        self.assertEqual(two_owned["results"], [])
        self.assertEqual(one_owned["series_checked"], 1)
        self.assertEqual(one_owned["results"][0]["owned_count"], 1)

    async def test_multiple_rating_sources_do_not_inflate_playtime(self):
        # A raw LEFT JOIN on ratings fans out per rating source; the playtime
        # subquery would then be summed once per rating row.
        rated_twice = await seed_owned_game("Portal", playtime_minutes=120)
        await add_rating(rated_twice, "manual", 9.0, 9.0)
        await add_rating(rated_twice, "backloggd", 8.0, 8.0)
        other = await seed_owned_game("Portal 2", playtime_minutes=60)
        for gid in (rated_twice, other):
            await link_series(gid, "collection", 95, "Portal")

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result([])),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        entry = result["results"][0]
        # 120 + 60 minutes = 3.0h — not doubled by the two rating rows.
        self.assertEqual(entry["total_playtime_hours"], 3.0)
        # avg_rating averages per-game averages: Portal (9+8)/2=8.5, Portal 2 unrated.
        self.assertEqual(entry["avg_rating"], 8.5)

    async def test_unowned_unwishlisted_games_row_still_appears_as_gap(self):
        # A games row can carry an igdb_id while being neither owned nor
        # wishlisted (an owned=0 stub, or an orphaned row left over after a
        # wishlist removal). Such a title must NOT be subtracted from the
        # member list — it's still a gap.
        a = await seed_owned_game("Pikmin")
        b = await seed_owned_game("Pikmin 2")
        for gid in (a, b):
            await link_series(gid, "collection", 96, "Pikmin")

        orphan = await seed_game("Pikmin 3")
        await set_igdb_id(orphan, 700)
        stub = await seed_game("Pikmin 4")
        await add_platform(stub, "switch2", owned=0)
        await set_igdb_id(stub, 701)

        members = [
            SeriesMember(700, "Pikmin 3", "2013-07-13", 0, []),
            SeriesMember(701, "Pikmin 4", "2023-07-21", 0, []),
        ]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        gap_ids = {g["igdb_id"] for g in result["results"][0]["gaps"]}
        self.assertEqual(gap_ids, {700, 701})

    async def test_unowned_stub_playtime_does_not_count(self):
        owned = await seed_owned_game("Doom", playtime_minutes=60)
        await add_platform(owned, "epic", playtime_minutes=600, owned=0)
        other = await seed_owned_game("Doom Eternal")
        for gid in (owned, other):
            await link_series(gid, "collection", 97, "Doom")

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result([])),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        # Only the owned steam row's 60 minutes counts, not the owned=0 stub's 600.
        self.assertEqual(result["results"][0]["total_playtime_hours"], 1.0)

    async def test_invalid_kind_raises(self):
        with self.assertRaises(Exception):
            await series.discover_series_gaps(kind="saga")

    async def test_name_fallback_excludes_owned_game_with_no_igdb_id(self):
        # Root cause 1: an owned row ingested (or never IGDB-resolved) with
        # igdb_id NULL is invisible to the igdb-id have-set diff. The
        # normalized-name fallback (layer B) must still recognize it as
        # "have" once its trailing "GOTY" is stripped.
        owned = await seed_owned_game("Borderlands GOTY")
        other_owned = await seed_owned_game("Borderlands 2")
        for gid in (owned, other_owned):
            await link_series(gid, "collection", 200, "Borderlands")

        members = [SeriesMember(999, "Borderlands", "2009-10-20", 0, [])]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        self.assertEqual(result["results"][0]["gaps"], [])

    async def test_version_parent_alias_excludes_owned_edition_igdb_id(self):
        # Root cause 2: an owned row whose igdb_id points at an
        # edition-specific IGDB entry (e.g. "The Witcher: Enhanced Edition"
        # igdb 283715) rather than the canonical member id (80) IGDB's own
        # collections field lists. The edition carries no series membership
        # row of its own and its (deliberately non-matching) name can't be
        # rescued by the normalized-name fallback either — only the
        # version-parent alias map can recognize it.
        linked_a = await seed_owned_game("The Witcher 2")
        linked_b = await seed_owned_game("The Witcher 3: Wild Hunt")
        for gid in (linked_a, linked_b):
            await link_series(gid, "franchise", 901, "The Witcher")

        edition = await seed_owned_game("Wiedzmin Rozszerzona Edycja")
        await set_igdb_id(edition, 283715)

        members = [
            SeriesMember(80, "The Witcher", "2007-10-26", 0, []),
            SeriesMember(999, "The Witcher 4", "2015-01-01", 0, []),
        ]
        aliases = {283715: 80}

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members, aliases)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        gap_ids = {g["igdb_id"] for g in result["results"][0]["gaps"]}
        self.assertEqual(gap_ids, {999})

    async def test_name_fallback_excludes_owned_game_with_curly_apostrophe(self):
        # An owned row can carry a *different* igdb_id than a series member
        # that is nonetheless the same title, differing only by apostrophe
        # glyph ("Marvel's" vs "Marvel’s"). The normalized-name fallback must
        # treat these as identical and exclude the member as a gap.
        owned = await seed_owned_game("Marvel's Spider-Man 2")
        await set_igdb_id(owned, 5001)
        other_owned = await seed_owned_game("Marvel's Spider-Man")
        for gid in (owned, other_owned):
            await link_series(gid, "franchise", 902, "Spider-Man")

        members = [SeriesMember(1000, "Marvel’s Spider-Man 2", "2023-10-20", 0, [])]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        self.assertEqual(result["results"][0]["gaps"], [])

    async def test_id_matched_row_is_consumed_and_cannot_name_suppress(self):
        # DOOM (2016, igdb 7351) and Doom (1993, igdb 673) both normalize to
        # "doom". Owning the 2016 reboot with its proper igdb_id excludes the
        # 7351 member by id — and CONSUMES the row, so it must not
        # additionally name-suppress the 1993 member, which stays a true gap.
        reboot = await seed_game("DOOM", release_date="2016-05-13")
        await add_platform(reboot, "steam", owned=1)
        await set_igdb_id(reboot, 7351)
        eternal = await seed_owned_game("Doom Eternal")
        for gid in (reboot, eternal):
            await link_series(gid, "franchise", 904, "Doom")

        members = [
            SeriesMember(673, "Doom", "1993-12-10", 0, []),
            SeriesMember(7351, "DOOM", "2016-05-13", 0, []),
        ]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        gap_ids = {g["igdb_id"] for g in result["results"][0]["gaps"]}
        self.assertEqual(gap_ids, {673})

    async def test_igdbless_row_suppresses_only_closest_year_member(self):
        # An igdb-less row whose normalized name matches SEVERAL members
        # suppresses at most one: the member closest by year to the row's
        # release_date. An unresolved DOOM row with a 2016 store date hides
        # only the 2016 member — Doom (1993) is still a gap.
        reboot = await seed_game("DOOM", release_date="2016-05-13")
        await add_platform(reboot, "steam", owned=1)
        eternal = await seed_owned_game("Doom Eternal")
        for gid in (reboot, eternal):
            await link_series(gid, "franchise", 907, "Doom")

        members = [
            SeriesMember(673, "Doom", "1993-12-10", 0, []),
            SeriesMember(7351, "DOOM", "2016-05-13", 0, []),
        ]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        gap_ids = {g["igdb_id"] for g in result["results"][0]["gaps"]}
        self.assertEqual(gap_ids, {673})

    async def test_prod_borderlands_goty_skewed_store_dates_suppress(self):
        # Prod: games.release_date is the STORE LISTING/PORT date, not the
        # original release. "Borderlands GOTY" (igdb NULL, 2023-08-31) and
        # "Borderlands Game of the Year Enhanced" (igdb 258897, 2024-05-20)
        # must both count as owning the 2009 "Borderlands" member: the first
        # via the name fallback DESPITE the 14-year skew (no year veto), the
        # second via its re-release alias.
        goty = await seed_game("Borderlands GOTY", release_date="2023-08-31")
        await add_platform(goty, "steam", owned=1)
        enhanced = await seed_game(
            "Borderlands Game of the Year Enhanced", release_date="2024-05-20"
        )
        await add_platform(enhanced, "steam", owned=1)
        await set_igdb_id(enhanced, 258897)
        other_owned = await seed_owned_game("Borderlands 2")
        for gid in (goty, enhanced, other_owned):
            await link_series(gid, "collection", 908, "Borderlands")

        members = [SeriesMember(1240, "Borderlands", "2009-10-20", 0, [])]
        aliases = {258897: 1240}

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members, aliases)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        self.assertEqual(result["results"][0]["gaps"], [])

    async def test_prod_tales_re_release_alias_suppresses(self):
        # Prod: "Tales from the Borderlands" owned as the 2021 re-release
        # (igdb 214139, release_date 2021-02-16) while the member list has the
        # 2014 original (igdb 6707). The parent_game alias {214139: 6707}
        # excludes the member directly.
        tales = await seed_game("Tales from the Borderlands", release_date="2021-02-16")
        await add_platform(tales, "steam", owned=1)
        await set_igdb_id(tales, 214139)
        other_owned = await seed_owned_game("Borderlands 2")
        for gid in (tales, other_owned):
            await link_series(gid, "franchise", 909, "Borderlands")

        members = [SeriesMember(6707, "Tales from the Borderlands", "2014-11-25", 0, [])]
        aliases = {214139: 6707}

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members, aliases)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        self.assertEqual(result["results"][0]["gaps"], [])

    async def test_prod_payday2_store_date_skew_name_suppresses(self):
        # Prod: "PAYDAY 2" owned as igdb 150511 with release_date 2018-03-15
        # (a store re-listing) vs the member 2058 (2013-08-13). No alias in
        # this fixture: the row is unconsumed, and the name fallback must
        # suppress the member despite the 5-year skew, while a genuinely
        # unowned member (PAYDAY 3) stays a gap.
        payday2 = await seed_game("PAYDAY 2", release_date="2018-03-15")
        await add_platform(payday2, "steam", owned=1)
        await set_igdb_id(payday2, 150511)
        heist = await seed_owned_game("PAYDAY: The Heist")
        for gid in (payday2, heist):
            await link_series(gid, "franchise", 910, "Payday")

        members = [
            SeriesMember(2058, "PAYDAY 2", "2013-08-13", 0, []),
            SeriesMember(4000, "PAYDAY 3", "2023-09-21", 0, []),
        ]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        gap_ids = {g["igdb_id"] for g in result["results"][0]["gaps"]}
        self.assertEqual(gap_ids, {4000})

    async def test_name_match_with_close_year_still_suppresses(self):
        # Both sides have years and they agree — the name match counts as
        # ownership (single same-named candidate).
        owned = await seed_game("Borderlands GOTY", release_date="2010-10-12")
        await add_platform(owned, "steam", owned=1)
        other_owned = await seed_owned_game("Borderlands 2")
        for gid in (owned, other_owned):
            await link_series(gid, "collection", 905, "Borderlands")

        members = [SeriesMember(999, "Borderlands", "2009-10-20", 0, [])]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        self.assertEqual(result["results"][0]["gaps"], [])

    async def test_name_match_with_unknown_member_year_still_suppresses(self):
        # The member side lacking a release year must not defeat the name
        # fallback (mirrors the library-row-without-year case covered by
        # test_name_fallback_excludes_owned_game_with_no_igdb_id).
        owned = await seed_game("Borderlands GOTY", release_date="2010-10-12")
        await add_platform(owned, "steam", owned=1)
        other_owned = await seed_owned_game("Borderlands 2")
        for gid in (owned, other_owned):
            await link_series(gid, "collection", 906, "Borderlands")

        members = [SeriesMember(999, "Borderlands", None, 0, [])]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2, include_unreleased=True)

        self.assertEqual(result["results"][0]["gaps"], [])

    async def test_mismatched_id_consumption_does_not_bar_name_suppression(self):
        # Prod: row 951 "Tales from the Borderlands" was wrongly enriched with
        # igdb 214139 — which is actually "New Tales from the Borderlands"
        # (the 2022 sequel) and is itself a franchise member. The row
        # id-matches that member, but the consumed member's normalized name
        # differs from the row's own — the id contradicts the name, so the
        # enrichment is suspect and the row RETAINS its name-suppression
        # right: the real 2014 "Tales from the Borderlands" member (6707)
        # must be suppressed, not reported as a gap.
        tales = await seed_game("Tales from the Borderlands", release_date="2021-02-16")
        await add_platform(tales, "steam", owned=1)
        await set_igdb_id(tales, 214139)
        other_owned = await seed_owned_game("Borderlands 2")
        for gid in (tales, other_owned):
            await link_series(gid, "franchise", 911, "Borderlands")

        members = [
            SeriesMember(6707, "Tales from the Borderlands", "2014-11-25", 0, []),
            SeriesMember(214139, "New Tales from the Borderlands", "2022-10-21", 0, []),
            SeriesMember(8000, "Borderlands 3", "2019-09-13", 0, []),
        ]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        gap_ids = {g["igdb_id"] for g in result["results"][0]["gaps"]}
        # 6707 suppressed by the retained name right; 214139 excluded by the
        # (wrong, but present) id match; Borderlands 3 stays a true gap.
        self.assertEqual(gap_ids, {8000})

    async def test_payday2_vr_member_id_match_does_not_hide_base_game(self):
        # Prod: row 394 "PAYDAY 2" wrongly enriched as igdb 150511 =
        # "Payday 2 VR", which is itself a franchise member. Consumed member
        # name ("payday 2 vr") differs from the row's ("payday 2") -> the row
        # keeps its name right and suppresses the real PAYDAY 2 member.
        payday2 = await seed_game("PAYDAY 2", release_date="2018-03-15")
        await add_platform(payday2, "steam", owned=1)
        await set_igdb_id(payday2, 150511)
        heist = await seed_owned_game("PAYDAY: The Heist")
        for gid in (payday2, heist):
            await link_series(gid, "franchise", 912, "Payday")

        members = [
            SeriesMember(2058, "PAYDAY 2", "2013-08-13", 0, []),
            SeriesMember(150511, "Payday 2 VR", "2017-11-16", 0, []),
            SeriesMember(4000, "PAYDAY 3", "2023-09-21", 0, []),
        ]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        gap_ids = {g["igdb_id"] for g in result["results"][0]["gaps"]}
        self.assertEqual(gap_ids, {4000})

    async def test_true_positive_gap_preserved_by_all_three_layers(self):
        # A member that isn't owned by id, alias, or normalized name must
        # still be reported — none of the three new exclusion layers should
        # over-match an unrelated (but same-series) title.
        owned_a = await seed_owned_game("Payday 2")
        owned_b = await seed_owned_game("Payday: The Heist")
        for gid in (owned_a, owned_b):
            await link_series(gid, "franchise", 903, "Payday")

        members = [SeriesMember(2000, "Payday 3", "2023-09-21", 0, [])]

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.series_gaps.get_series_members_cached",
                AsyncMock(return_value=_members_result(members)),
            ),
        ):
            result = await series.discover_series_gaps(min_owned=2)

        gap_ids = {g["igdb_id"] for g in result["results"][0]["gaps"]}
        self.assertEqual(gap_ids, {2000})
