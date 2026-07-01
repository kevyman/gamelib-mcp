"""Tests for wishlist tracking: the dedicated game_wishlist table, its DB
helpers (upsert + fulfillment cleanup), the manual add_game_to_platform
(owned=False) path, get_wishlist, and the Steam / DekuDeals wishlist syncs.
"""

import unittest
from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, add_platform, seed_game
from fastmcp.exceptions import ToolError
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import dekudeals, steam_wishlist
from gamelib_mcp.tools import platforms


class UpsertWishlistEntryTests(ToolDBTestCase):
    async def test_lives_in_its_own_table_not_game_platforms(self):
        game_id = await seed_game("Wanted Game")

        wishlist_id = await db_module.upsert_wishlist_entry(game_id, "switch2", source="manual")

        async with db_module.get_db() as db:
            wishlist_row = await db.execute_fetchone(
                "SELECT wishlisted_at, source FROM game_wishlist WHERE id = ?", (wishlist_id,)
            )
            gp_row = await db.execute_fetchone(
                "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
                (game_id, "switch2"),
            )
        self.assertIsNotNone(wishlist_row["wishlisted_at"])
        self.assertEqual(wishlist_row["source"], "manual")
        # No game_platforms row is created for a pure wishlist entry.
        self.assertIsNone(gp_row)

    async def test_does_not_touch_existing_ownership(self):
        game_id = await seed_game("Owned Game")
        await add_platform(game_id, "steam", owned=1, playtime_minutes=120)

        await db_module.upsert_wishlist_entry(game_id, "steam", wishlisted_at="2026-01-01T00:00:00+00:00")

        async with db_module.get_db() as db:
            gp_row = await db.execute_fetchone(
                "SELECT owned, playtime_minutes FROM game_platforms WHERE game_id = ? AND platform = ?",
                (game_id, "steam"),
            )
        self.assertEqual(gp_row["owned"], 1)
        self.assertEqual(gp_row["playtime_minutes"], 120)


class ClearFulfilledWishlistEntriesTests(ToolDBTestCase):
    async def test_deletes_entry_once_platform_is_owned(self):
        game_id = await seed_game("Bought It")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam")
        await add_platform(game_id, "steam", owned=1)

        deleted = await db_module.clear_fulfilled_wishlist_entries()

        self.assertEqual(deleted, 1)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ? AND platform = ?",
                (game_id, "steam"),
            )
        self.assertIsNone(row)

    async def test_leaves_unowned_entries_alone(self):
        game_id = await seed_game("Still Wanted")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam")

        deleted = await db_module.clear_fulfilled_wishlist_entries()

        self.assertEqual(deleted, 0)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ? AND platform = ?",
                (game_id, "steam"),
            )
        self.assertIsNotNone(row)

    async def test_scoped_to_game_id_and_platform(self):
        fulfilled = await seed_game("Fulfilled Elsewhere Too")
        await db_module.upsert_wishlist_entry(fulfilled, "steam", source="steam")
        await add_platform(fulfilled, "steam", owned=1)
        other_fulfilled = await seed_game("Also Fulfilled")
        await db_module.upsert_wishlist_entry(other_fulfilled, "steam", source="steam")
        await add_platform(other_fulfilled, "steam", owned=1)

        deleted = await db_module.clear_fulfilled_wishlist_entries(game_id=fulfilled, platform="steam")

        self.assertEqual(deleted, 1)
        async with db_module.get_db() as db:
            still_there = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (other_fulfilled,)
            )
        self.assertIsNotNone(still_there)


class AddGameToPlatformWishlistTests(ToolDBTestCase):
    async def test_owned_false_creates_wishlist_entry_with_no_platform_row(self):
        result = await platforms.add_game_to_platform("Elden Ring", "ps5", owned=False)

        self.assertFalse(result["owned"])
        self.assertIsNone(result["game_platform_id"])
        self.assertIsNotNone(result["wishlist_id"])
        self.assertIsNone(result["playtime_minutes"])

        async with db_module.get_db() as db:
            gp_row = await db.execute_fetchone(
                "SELECT 1 FROM game_platforms WHERE game_id = ?", (result["game_id"],)
            )
            wishlist_row = await db.execute_fetchone(
                "SELECT source FROM game_wishlist WHERE id = ?", (result["wishlist_id"],)
            )
        self.assertIsNone(gp_row)
        self.assertEqual(wishlist_row["source"], "manual")

        breakdown = await platforms.get_platform_breakdown()
        self.assertEqual(breakdown["total_unique_games"], 0)

    async def test_owned_false_on_already_owned_game_clears_immediately(self):
        game_id = await seed_game("Already Owned")
        await add_platform(game_id, "ps5", owned=1, playtime_minutes=600)

        result = await platforms.add_game_to_platform("Already Owned", "ps5", owned=False)

        self.assertEqual(result["game_id"], game_id)
        async with db_module.get_db() as db:
            gp_row = await db.execute_fetchone(
                "SELECT owned, playtime_minutes FROM game_platforms WHERE game_id = ? AND platform = ?",
                (game_id, "ps5"),
            )
            wishlist_row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ? AND platform = ?", (game_id, "ps5")
            )
        # Ownership untouched...
        self.assertEqual(gp_row["owned"], 1)
        self.assertEqual(gp_row["playtime_minutes"], 600)
        # ...and the just-created wishlist entry was immediately reconciled away.
        self.assertIsNone(wishlist_row)

    async def test_owned_true_clears_matching_wishlist_entry(self):
        game_id = await seed_game("Was Wishlisted")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam")

        await platforms.add_game_to_platform("Was Wishlisted", "steam", owned=True)

        async with db_module.get_db() as db:
            wishlist_row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ? AND platform = ?", (game_id, "steam")
            )
        self.assertIsNone(wishlist_row)

    async def test_identifier_requires_owned_true(self):
        with self.assertRaisesRegex(ToolError, "identifier_type/identifier_value require owned=True"):
            await platforms.add_game_to_platform(
                "Some Game", "steam", identifier_type="steam_appid", identifier_value="1", owned=False
            )


class GetWishlistTests(ToolDBTestCase):
    async def test_lists_only_wishlisted_rows_with_platform_filter(self):
        owned = await seed_game("Owned Only")
        await add_platform(owned, "steam", owned=1)

        wished = await seed_game("Wished Game")
        await db_module.upsert_wishlist_entry(wished, "steam", source="steam")
        await db_module.upsert_wishlist_entry(wished, "switch2", source="dekudeals")

        all_items = await platforms.get_wishlist()
        self.assertEqual(all_items["count"], 2)
        self.assertEqual({i["platform"] for i in all_items["items"]}, {"steam", "switch2"})
        self.assertTrue(all(i["owned"] is False for i in all_items["items"]))

        steam_only = await platforms.get_wishlist("steam")
        self.assertEqual(steam_only["count"], 1)
        self.assertEqual(steam_only["items"][0]["name"], "Wished Game")
        self.assertEqual(steam_only["items"][0]["source"], "steam")

    async def test_surfaces_stale_owned_entry_as_diagnostic(self):
        # A wishlist row that hasn't been cleaned up yet (cleanup didn't run)
        # should still show up, with owned=True surfacing the stale state
        # rather than hiding it.
        game_id = await seed_game("Stale Entry")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam")
        await add_platform(game_id, "steam", owned=1)

        result = await platforms.get_wishlist()

        self.assertEqual(result["count"], 1)
        self.assertTrue(result["items"][0]["owned"])


class FetchSteamWishlistTests(ToolDBTestCase):
    async def test_matches_existing_game_by_appid_without_creating_ownership(self):
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
            gp_row = await db.execute_fetchone(
                "SELECT owned, playtime_minutes FROM game_platforms WHERE game_id = ?", (game_id,)
            )
            wishlist_row = await db.execute_fetchone(
                "SELECT source FROM game_wishlist WHERE game_id = ? AND platform = ?", (game_id, "steam")
            )
        self.assertEqual(gp_row["owned"], 1)
        self.assertEqual(gp_row["playtime_minutes"], 300)
        self.assertEqual(wishlist_row["source"], "steam")

    async def test_creates_new_unowned_game_with_no_platform_row(self):
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
            game = await db.execute_fetchone("SELECT id FROM games WHERE name = 'New Game'")
            self.assertIsNotNone(game)
            gp_row = await db.execute_fetchone(
                "SELECT 1 FROM game_platforms WHERE game_id = ?", (game["id"],)
            )
            wishlist_row = await db.execute_fetchone(
                "SELECT source FROM game_wishlist WHERE game_id = ?", (game["id"],)
            )
        # A wishlist-only game has no game_platforms row at all.
        self.assertIsNone(gp_row)
        self.assertEqual(wishlist_row["source"], "steam")

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
                "_fetch_wishlist_items",
                AsyncMock(
                    return_value=[
                        {"title": "Hollow Knight Silksong", "added_at": "2025-06-29T07:43:28+00:00"},
                        {"title": "Some Untracked Game", "added_at": "2025-06-29T07:44:40+00:00"},
                    ]
                ),
            ),
        ):
            result = await dekudeals.sync_dekudeals_wishlist()

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["skipped"], 1)
        async with db_module.get_db() as db:
            wishlist_row = await db.execute_fetchone(
                "SELECT source, wishlisted_at FROM game_wishlist WHERE game_id = ? AND platform = ?",
                (game_id, "switch2"),
            )
            gp_row = await db.execute_fetchone(
                "SELECT 1 FROM game_platforms WHERE game_id = ?", (game_id,)
            )
        self.assertEqual(wishlist_row["source"], "dekudeals")
        # Uses the item's own wishlist-add time from the export, not sync time.
        self.assertEqual(wishlist_row["wishlisted_at"], "2025-06-29T07:43:28+00:00")
        self.assertIsNone(gp_row)

    async def test_parses_real_wishlist_export_shape(self):
        # Exact shape confirmed from a live DekuDeals wishlist ".json" export
        # (2026-07-01): {"items": [{"name", "link", "added_at"}, ...],
        # "default_desired_price": ...} — no id/nsuid field anywhere.
        payload = {
            "items": [
                {
                    "name": "007 First Light",
                    "link": "https://www.dekudeals.com/items/007-first-light",
                    "added_at": "2025-06-29T07:43:28+00:00",
                },
                {
                    "name": "The Legend of Zelda: Link’s Awakening",
                    "link": "https://www.dekudeals.com/items/the-legend-of-zelda-links-awakening",
                    "added_at": "2025-06-29T07:49:26+00:00",
                },
            ],
            "default_desired_price": "drop",
        }

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        class _Client:
            def __init__(self):
                self.get = AsyncMock(return_value=_Resp())

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with patch.object(dekudeals.httpx, "AsyncClient", return_value=_Client()):
            items = await dekudeals._fetch_wishlist_items("https://www.dekudeals.com/wishlist/abc")

        self.assertEqual(
            items,
            [
                {"title": "007 First Light", "added_at": "2025-06-29T07:43:28+00:00"},
                {
                    "title": "The Legend of Zelda: Link’s Awakening",
                    "added_at": "2025-06-29T07:49:26+00:00",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
