"""Characterization tests for add_game_to_platform(push_to_store=True).

Issue #110 phase 2: an opt-in, wishlist-only push of a manual wishlist add to
the real store wishlist. gamelib_mcp.data.steam_wishlist.push_to_steam_wishlist
is a fixed contract owned by a concurrent change (do not import its real
implementation here) — it's always patched with an AsyncMock so these tests
exercise only the tool-surface plumbing: validation, local-write durability,
appid resolution, and per-platform response shape.
"""

from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, add_identifier, add_platform, seed_game
from fastmcp.exceptions import ToolError

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.steam_wishlist import SteamWishlistPushError
from gamelib_mcp.tools import platforms


async def _wishlist_row(game_id: int, platform: str):
    async with db_module.get_db() as db:
        return await db.execute_fetchone(
            "SELECT * FROM game_wishlist WHERE game_id = ? AND platform = ?",
            (game_id, platform),
        )


class PushToStoreValidationTests(ToolDBTestCase):
    async def test_push_with_owned_true_raises(self):
        with self.assertRaisesRegex(ToolError, "requires owned=False"):
            await platforms.add_game_to_platform(
                "Some Game", "steam", owned=True, push_to_store=True
            )

    async def test_default_does_not_push(self):
        gid = await seed_game("Untouched Wishlist Add")
        with patch.object(
            platforms, "push_to_steam_wishlist", new=AsyncMock()
        ) as mock_push:
            result = await platforms.add_game_to_platform(
                game_id=gid, platform="steam", owned=False
            )
        mock_push.assert_not_awaited()
        self.assertIsNone(result["store_push"])
        row = await _wishlist_row(gid, "steam")
        self.assertIsNotNone(row)


class PushToStoreDryRunTests(ToolDBTestCase):
    async def test_dry_run_never_pushes_and_writes_nothing(self):
        with patch.object(
            platforms, "push_to_steam_wishlist", new=AsyncMock()
        ) as mock_push:
            result = await platforms.add_game_to_platform(
                name="__PUSH_DRY_RUN__",
                platform="steam",
                owned=False,
                push_to_store=True,
                identifier_type="steam_appid",
                identifier_value="42",
                dry_run=True,
            )
        mock_push.assert_not_awaited()
        self.assertIsNone(result["store_push"])
        async with db_module.get_db() as db:
            game = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ?", ("__PUSH_DRY_RUN__",)
            )
            wishlist_count = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM game_wishlist"
            )
        self.assertIsNone(game)
        self.assertEqual(wishlist_count["c"], 0)


class PushToStoreSteamTests(ToolDBTestCase):
    async def test_success_records_row_and_fills_store_identifier(self):
        gid = await seed_game("Push Success Game")
        with patch.object(
            platforms,
            "push_to_steam_wishlist",
            new=AsyncMock(return_value={"appid": 440, "via": "webapi", "wishlist_count": 3}),
        ) as mock_push:
            result = await platforms.add_game_to_platform(
                game_id=gid,
                platform="steam",
                owned=False,
                push_to_store=True,
                identifier_type="steam_appid",
                identifier_value="440",
            )
        mock_push.assert_awaited_once_with(440)
        self.assertEqual(
            result["store_push"],
            {
                "attempted": True,
                "pushed": True,
                "via": "webapi",
                "appid": "440",
                "wishlist_count": 3,
            },
        )
        row = await _wishlist_row(gid, "steam")
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["store_identifier"], "440")

    async def test_failure_keeps_local_row_and_reports_error(self):
        gid = await seed_game("Push Failure Game")
        with patch.object(
            platforms,
            "push_to_steam_wishlist",
            new=AsyncMock(side_effect=SteamWishlistPushError("boom")),
        ):
            result = await platforms.add_game_to_platform(
                game_id=gid,
                platform="steam",
                owned=False,
                push_to_store=True,
                identifier_type="steam_appid",
                identifier_value="123",
            )
        self.assertEqual(
            result["store_push"],
            {"attempted": True, "pushed": False, "appid": "123", "error": "boom"},
        )
        row = await _wishlist_row(gid, "steam")
        self.assertIsNotNone(row)

    async def test_unexpected_exception_still_returns_recorded_row(self):
        gid = await seed_game("Push Blows Up")
        with patch.object(
            platforms,
            "push_to_steam_wishlist",
            new=AsyncMock(side_effect=RuntimeError("kaboom")),
        ):
            result = await platforms.add_game_to_platform(
                game_id=gid,
                platform="steam",
                owned=False,
                push_to_store=True,
                identifier_type="steam_appid",
                identifier_value="7",
            )
        self.assertTrue(result["store_push"]["attempted"])
        self.assertFalse(result["store_push"]["pushed"])
        self.assertIn("error", result["store_push"])
        row = await _wishlist_row(gid, "steam")
        self.assertIsNotNone(row)

    async def test_no_appid_anywhere_does_not_call_push(self):
        gid = await seed_game("No Appid Known Game")
        with patch.object(
            platforms, "push_to_steam_wishlist", new=AsyncMock()
        ) as mock_push:
            result = await platforms.add_game_to_platform(
                game_id=gid, platform="steam", owned=False, push_to_store=True
            )
        mock_push.assert_not_awaited()
        self.assertFalse(result["store_push"]["attempted"])
        self.assertFalse(result["store_push"]["pushed"])
        self.assertIn("steam_appid", result["store_push"]["error"])
        row = await _wishlist_row(gid, "steam")
        self.assertIsNotNone(row)
        self.assertIsNone(row["store_identifier"])

    async def test_appid_resolved_from_existing_identifier_row(self):
        gid = await seed_game("Refunded Steam Copy")
        gpid = await add_platform(gid, "steam", owned=0)
        await add_identifier(gpid, "steam_appid", 999)
        with patch.object(
            platforms,
            "push_to_steam_wishlist",
            new=AsyncMock(return_value={"appid": 999, "via": "storefront", "wishlist_count": None}),
        ) as mock_push:
            result = await platforms.add_game_to_platform(
                game_id=gid, platform="steam", owned=False, push_to_store=True
            )
        mock_push.assert_awaited_once_with(999)
        self.assertTrue(result["store_push"]["pushed"])
        self.assertEqual(result["store_push"]["via"], "storefront")
        row = await _wishlist_row(gid, "steam")
        self.assertEqual(row["store_identifier"], "999")


class PushToStoreOtherPlatformsTests(ToolDBTestCase):
    async def test_switch2_returns_manual_dekudeals_link_without_calling_push(self):
        with patch.object(
            platforms, "push_to_steam_wishlist", new=AsyncMock()
        ) as mock_push:
            result = await platforms.add_game_to_platform(
                name="Manual Link Game", platform="switch2", owned=False, push_to_store=True
            )
        mock_push.assert_not_awaited()
        store_push = result["store_push"]
        self.assertFalse(store_push["attempted"])
        self.assertFalse(store_push["pushed"])
        self.assertTrue(
            store_push["manual_url"].startswith("https://www.dekudeals.com/search?q=")
        )
        self.assertIn("Manual%20Link%20Game", store_push["manual_url"])
        self.assertIn("no wishlist write API", store_push["note"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT g.id FROM games g JOIN game_wishlist w ON w.game_id = g.id "
                "WHERE g.name = ?",
                ("Manual Link Game",),
            )
        self.assertIsNotNone(row)

    async def test_unsupported_platform_reports_no_push_available(self):
        with patch.object(
            platforms, "push_to_steam_wishlist", new=AsyncMock()
        ) as mock_push:
            result = await platforms.add_game_to_platform(
                name="PS5 Wishlist Game", platform="ps5", owned=False, push_to_store=True
            )
        mock_push.assert_not_awaited()
        store_push = result["store_push"]
        self.assertFalse(store_push["attempted"])
        self.assertFalse(store_push["pushed"])
        self.assertIn("No store wishlist push available", store_push["error"])
        self.assertIn("ps5", store_push["error"])


class PushToStoreBatchTests(ToolDBTestCase):
    async def test_batch_reports_per_item_store_push(self):
        with patch.object(
            platforms,
            "push_to_steam_wishlist",
            new=AsyncMock(return_value={"appid": 555, "via": "webapi", "wishlist_count": 2}),
        ) as mock_push:
            result = await platforms.add_games_to_platform_batch(
                [
                    {
                        "name": "Batch Steam Push",
                        "platform": "steam",
                        "owned": False,
                        "identifier_type": "steam_appid",
                        "identifier_value": "555",
                        "push_to_store": True,
                    },
                    {
                        "name": "Batch Switch2 Push",
                        "platform": "switch2",
                        "owned": False,
                        "push_to_store": True,
                    },
                ]
            )
        mock_push.assert_awaited_once_with(555)
        self.assertEqual(result["ok"], 2)
        steam_item, switch2_item = result["results"]
        self.assertEqual(steam_item["store_push"]["pushed"], True)
        self.assertEqual(steam_item["store_push"]["appid"], "555")
        self.assertFalse(switch2_item["store_push"]["attempted"])
        self.assertTrue(
            switch2_item["store_push"]["manual_url"].startswith(
                "https://www.dekudeals.com/search?q="
            )
        )
