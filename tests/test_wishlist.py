"""Tests for wishlist tracking: the wishlisted_at column, its DB helper, the
manual add_game_to_platform(owned=False) path, get_wishlist, and the Steam /
DekuDeals wishlist syncs.
"""

import unittest
from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, add_platform, seed_game
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import dekudeals, steam_wishlist
from gamelib_mcp.tools import platforms


class UpsertWishlistEntryTests(ToolDBTestCase):
    async def test_does_not_clobber_existing_ownership(self):
        game_id = await seed_game("Owned Game")
        await add_platform(game_id, "steam", owned=1)

        await db_module.upsert_wishlist_entry(game_id, "steam", wishlisted_at="2026-01-01T00:00:00+00:00")

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT owned, wishlisted_at FROM game_platforms WHERE game_id = ? AND platform = ?",
                (game_id, "steam"),
            )
        self.assertEqual(row["owned"], 1)
        self.assertEqual(row["wishlisted_at"], "2026-01-01T00:00:00+00:00")

    async def test_new_row_defaults_to_unowned(self):
        game_id = await seed_game("Wanted Game")

        platform_id = await db_module.upsert_wishlist_entry(game_id, "switch2")

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT owned, wishlisted_at FROM game_platforms WHERE id = ?", (platform_id,)
            )
        self.assertEqual(row["owned"], 0)
        self.assertIsNotNone(row["wishlisted_at"])


class AddGameToPlatformWishlistTests(ToolDBTestCase):
    async def test_owned_false_creates_wishlist_entry_not_ownership(self):
        result = await platforms.add_game_to_platform("Elden Ring", "ps5", owned=False)

        self.assertFalse(result["owned"])
        self.assertIsNone(result["playtime_minutes"])

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT owned, wishlisted_at FROM game_platforms WHERE id = ?",
                (result["game_platform_id"],),
            )
        self.assertEqual(row["owned"], 0)
        self.assertIsNotNone(row["wishlisted_at"])

        breakdown = await platforms.get_platform_breakdown()
        self.assertEqual(breakdown["total_unique_games"], 0)

    async def test_owned_false_on_already_owned_game_does_not_unown_it(self):
        game_id = await seed_game("Already Owned")
        await add_platform(game_id, "ps5", owned=1, playtime_minutes=600)

        result = await platforms.add_game_to_platform("Already Owned", "ps5", owned=False)

        self.assertEqual(result["game_id"], game_id)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT owned, playtime_minutes FROM game_platforms WHERE game_id = ? AND platform = ?",
                (game_id, "ps5"),
            )
        self.assertEqual(row["owned"], 1)
        self.assertEqual(row["playtime_minutes"], 600)


class GetWishlistTests(ToolDBTestCase):
    async def test_lists_only_wishlisted_rows_with_platform_filter(self):
        owned = await seed_game("Owned Only")
        await add_platform(owned, "steam", owned=1)

        wished = await seed_game("Wished Game")
        await db_module.upsert_wishlist_entry(wished, "steam")
        await db_module.upsert_wishlist_entry(wished, "switch2")

        all_items = await platforms.get_wishlist()
        self.assertEqual(all_items["count"], 2)
        self.assertEqual({i["platform"] for i in all_items["items"]}, {"steam", "switch2"})

        steam_only = await platforms.get_wishlist("steam")
        self.assertEqual(steam_only["count"], 1)
        self.assertEqual(steam_only["items"][0]["name"], "Wished Game")


class FetchSteamWishlistTests(ToolDBTestCase):
    async def test_matches_existing_game_by_appid_without_unowning_it(self):
        game_id = await seed_game("Hades II")
        platform_id = await add_platform(game_id, "steam", owned=1, playtime_minutes=300)
        await db_module.upsert_game_platform_identifier(
            platform_id, db_module.STEAM_APP_ID, "111", is_primary=True
        )

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": [{"appid": 111, "priority": 1}]}}

        class _Client:
            def __init__(self):
                self.get = AsyncMock(return_value=_Resp())

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with (
            patch.object(steam_wishlist, "STEAM_API_KEY", "key"),
            patch.object(steam_wishlist, "STEAM_ID", "id"),
            patch.object(steam_wishlist.httpx, "AsyncClient", return_value=_Client()),
        ):
            result = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result, {"added": 0, "matched": 1, "skipped": 0})
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT owned, wishlisted_at, playtime_minutes FROM game_platforms WHERE game_id = ?",
                (game_id,),
            )
        self.assertEqual(row["owned"], 1)
        self.assertEqual(row["playtime_minutes"], 300)
        self.assertIsNotNone(row["wishlisted_at"])

    async def test_creates_new_unowned_game_for_unmatched_appid(self):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": [{"appid": 222}]}}

        class _Client:
            def __init__(self):
                self.get = AsyncMock(return_value=_Resp())

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with (
            patch.object(steam_wishlist, "STEAM_API_KEY", "key"),
            patch.object(steam_wishlist, "STEAM_ID", "id"),
            patch.object(steam_wishlist.httpx, "AsyncClient", return_value=_Client()),
            patch.object(steam_wishlist, "fetch_app_name", AsyncMock(return_value="New Game")),
        ):
            result = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0})
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT g.name, gp.owned, gp.wishlisted_at FROM games g "
                "JOIN game_platforms gp ON gp.game_id = g.id WHERE g.name = 'New Game'"
            )
            identifier = await db.execute_fetchone(
                "SELECT identifier_value FROM game_platform_identifiers WHERE identifier_type = ?",
                (db_module.STEAM_APP_ID,),
            )
        self.assertEqual(row["owned"], 0)
        self.assertIsNotNone(row["wishlisted_at"])
        self.assertEqual(identifier["identifier_value"], "222")

    async def test_missing_credentials_raises(self):
        with (
            patch.object(steam_wishlist, "STEAM_API_KEY", ""),
            patch.object(steam_wishlist, "STEAM_ID", ""),
        ):
            with self.assertRaises(ValueError):
                await steam_wishlist.fetch_wishlist()


class DekuDealsWishlistTests(ToolDBTestCase):
    async def test_unconfigured_reports_status(self):
        with patch.object(dekudeals, "DEKUDEALS_WISHLIST_URL", ""):
            result = await dekudeals.sync_dekudeals_wishlist()
        self.assertEqual(result["sync_status"], "unconfigured")

    async def test_fuzzy_matches_titles_onto_switch2_wishlist(self):
        game_id = await seed_game("Hollow Knight: Silksong")

        with (
            patch.object(dekudeals, "DEKUDEALS_WISHLIST_URL", "https://www.dekudeals.com/wishlist/abc"),
            patch.object(
                dekudeals,
                "_fetch_wishlist_titles",
                AsyncMock(return_value=["Hollow Knight Silksong", "Some Untracked Game"]),
            ),
        ):
            result = await dekudeals.sync_dekudeals_wishlist()

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["skipped"], 1)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT owned, wishlisted_at FROM game_platforms WHERE game_id = ? AND platform = ?",
                (game_id, "switch2"),
            )
        self.assertEqual(row["owned"], 0)
        self.assertIsNotNone(row["wishlisted_at"])


if __name__ == "__main__":
    unittest.main()
