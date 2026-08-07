"""Tests for wishlist tracking: the dedicated game_wishlist table, its DB
helpers (upsert + fulfillment cleanup), the manual add_game_to_platform
(owned=False) path, get_wishlist, and the Steam / DekuDeals wishlist syncs.
"""

import asyncio
import json
import unittest
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import httpx
from conftest import ToolDBTestCase, add_identifier, add_platform, seed_game
from fastmcp.exceptions import ToolError

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import dekudeals, steam_wishlist
from gamelib_mcp.data.scrape_validate import FIXTURES_DIR
from gamelib_mcp.tools import platforms


class UpsertWishlistEntryTests(ToolDBTestCase):
    async def test_lives_in_its_own_table_not_game_platforms(self):
        game_id = await seed_game("Wanted Game")

        upserted = await db_module.upsert_wishlist_entry(game_id, "switch2", source="manual")
        self.assertTrue(upserted["created"])
        wishlist_id = upserted["id"]
        # A second upsert updates in place and says so — the sync counters
        # read this flag, so it must be reported from the same transaction
        # as the write (not a separate exists-check racing other writers).
        self.assertFalse(
            (await db_module.upsert_wishlist_entry(game_id, "switch2", source="manual"))["created"]
        )

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

    async def test_upsert_wishlist_entry_stores_store_identifier(self):
        game_id = await seed_game("Hollow Knight: Silksong")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam", store_identifier="1030300")
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT store_identifier FROM game_wishlist WHERE game_id = ?", (game_id,)
            )
        self.assertEqual(row["store_identifier"], "1030300")

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


class DeleteStaleWishlistEntriesTests(ToolDBTestCase):
    async def test_removes_entries_not_in_keep_set(self):
        removed_game = await seed_game("Removed From Wishlist")
        await db_module.upsert_wishlist_entry(removed_game, "steam", source="steam")
        kept_game = await seed_game("Still On Wishlist")
        await db_module.upsert_wishlist_entry(kept_game, "steam", source="steam")

        deleted = await db_module.delete_stale_wishlist_entries("steam", "steam", {kept_game})

        self.assertEqual(deleted, 1)
        async with db_module.get_db() as db:
            gone = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (removed_game,)
            )
            still_there = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (kept_game,)
            )
        self.assertIsNone(gone)
        self.assertIsNotNone(still_there)

    async def test_empty_keep_set_deletes_all_for_that_platform_and_source(self):
        game_id = await seed_game("Wishlist Now Empty")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam")

        deleted = await db_module.delete_stale_wishlist_entries("steam", "steam", set())

        self.assertEqual(deleted, 1)

    async def test_scoped_to_source_never_touches_manual_entries(self):
        manual_game = await seed_game("Manually Tracked")
        await db_module.upsert_wishlist_entry(manual_game, "steam", source="manual")
        synced_game = await seed_game("Sync Tracked")
        await db_module.upsert_wishlist_entry(synced_game, "steam", source="steam")

        # Simulate a full steam-source reconciliation that found neither game
        # in the current fetch — only the "steam"-sourced row should go.
        deleted = await db_module.delete_stale_wishlist_entries("steam", "steam", set())

        self.assertEqual(deleted, 1)
        async with db_module.get_db() as db:
            manual_row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (manual_game,)
            )
        self.assertIsNotNone(manual_row)

    async def test_scoped_to_platform_never_touches_other_platforms(self):
        switch_game = await seed_game("Switch Game")
        await db_module.upsert_wishlist_entry(switch_game, "switch2", source="dekudeals")
        steam_game = await seed_game("Steam Game")
        await db_module.upsert_wishlist_entry(steam_game, "steam", source="steam")

        deleted = await db_module.delete_stale_wishlist_entries("steam", "steam", set())

        self.assertEqual(deleted, 1)
        async with db_module.get_db() as db:
            switch_row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (switch_game,)
            )
        self.assertIsNotNone(switch_row)


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

    async def test_non_steam_appid_identifier_rejected_when_unowned(self):
        with self.assertRaisesRegex(
            ToolError, "only supports 'steam_appid'"
        ):
            await platforms.add_game_to_platform(
                "Some Game", "steam", identifier_type="gog_product_id", identifier_value="1", owned=False
            )

    async def test_steam_appid_requires_steam_platform_when_unowned(self):
        with self.assertRaisesRegex(
            ToolError, "requires platform='steam'"
        ):
            await platforms.add_game_to_platform(
                "Some Game", "ps5", identifier_type="steam_appid", identifier_value="1", owned=False
            )

    async def test_steam_appid_stored_as_wishlist_store_identifier_when_unowned(self):
        result = await platforms.add_game_to_platform(
            "Perfect Tides", "steam", identifier_type="steam_appid", identifier_value="2088810", owned=False
        )

        self.assertIsNone(result["game_platform_id"])
        self.assertEqual(result["identifier"], {"type": "steam_appid", "value": "2088810"})

        async with db_module.get_db() as db:
            wishlist_row = await db.execute_fetchone(
                "SELECT store_identifier FROM game_wishlist WHERE id = ?", (result["wishlist_id"],)
            )
        self.assertEqual(wishlist_row["store_identifier"], "2088810")


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


class ParseSteamAddedAtTests(unittest.TestCase):
    def test_parses_epoch_int(self):
        self.assertEqual(
            steam_wishlist._parse_steam_added_at(1735689600),
            "2025-01-01T00:00:00+00:00",
        )

    def test_parses_numeric_string_as_epoch(self):
        self.assertEqual(
            steam_wishlist._parse_steam_added_at("1735689600"),
            "2025-01-01T00:00:00+00:00",
        )

    def test_parses_iso_string_passthrough(self):
        self.assertEqual(
            steam_wishlist._parse_steam_added_at("2025-01-01T00:00:00+00:00"),
            "2025-01-01T00:00:00+00:00",
        )

    def test_none_and_garbage_return_none(self):
        self.assertIsNone(steam_wishlist._parse_steam_added_at(None))
        self.assertIsNone(steam_wishlist._parse_steam_added_at("not a date"))
        self.assertIsNone(steam_wishlist._parse_steam_added_at(True))


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
            # added/matched count the WISHLIST row, not game resolution: the
            # first sync mints the row (added), a re-sync updates it in place
            # (matched). Before this split, a routine re-sync of a whole
            # wishlist reported every unchanged row as "added".
            resync = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0, "removed": 0})
        self.assertEqual(resync, {"added": 0, "matched": 1, "skipped": 0, "removed": 0})
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

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0, "removed": 0})
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

    async def test_resync_of_wishlist_only_game_counts_matched_not_added(self):
        # The 2026-08-06 prod finding: a wishlist-only game (no game_platforms
        # row, so no identifier row) resolves by NAME on every sync, and the
        # old counters reported that as "added" forever — a full re-sync of a
        # 171-row wishlist read as 171 additions / 0 matches.
        game_id = await seed_game("Wishlist Only Resync")
        await db_module.upsert_wishlist_entry(
            game_id, "steam", source="steam", store_identifier="444"
        )

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": [{"appid": 444}]}}

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
            patch.object(
                steam_wishlist, "fetch_app_name", AsyncMock(return_value="Wishlist Only Resync")
            ),
        ):
            result = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result, {"added": 0, "matched": 1, "skipped": 0, "removed": 0})
        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT id FROM game_wishlist WHERE game_id = ?", (game_id,)
            )
        self.assertEqual(len(rows), 1)

    async def test_resolves_via_stored_wishlist_identifier_without_network_lookup(self):
        # Issue #139 fix 2: a re-synced wishlist-only item resolves off its
        # OWN game_wishlist.store_identifier from a prior sync — no name
        # lookup, no network call at all.
        game_id = await seed_game("Wishlist Only Via Identifier")
        await db_module.upsert_wishlist_entry(
            game_id, "steam", source="steam", store_identifier="777"
        )

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": [{"appid": 777}]}}

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
            patch.object(steam_wishlist, "fetch_app_name", AsyncMock()) as mock_fetch_name,
        ):
            result = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["added"], 0)
        mock_fetch_name.assert_not_awaited()
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT game_id FROM game_wishlist WHERE store_identifier = ?", ("777",)
            )
        self.assertEqual(row["game_id"], game_id)

    async def test_collision_guard_mints_separate_row_for_same_raw_name(self):
        # Issue #139 fix 1, Dead Space case: the wishlisted 2023 remake
        # (appid 1693980) must never attach onto the owned 2008 original
        # (appid 17470) just because the store name resolves to the same
        # "Dead Space" — Steam never lists an owned game on your wishlist.
        owned_id = await seed_game("Dead Space")
        platform_id = await add_platform(owned_id, "steam", owned=1)
        await add_identifier(platform_id, db_module.STEAM_APP_ID, "17470", is_primary=True)

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": [{"appid": 1693980}]}}

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
            patch.object(steam_wishlist, "fetch_app_name", AsyncMock(return_value="Dead Space")),
        ):
            result = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result["added"], 1)
        async with db_module.get_db() as db:
            rows = await db.execute_fetchall("SELECT id FROM games WHERE name = 'Dead Space'")
            wishlist_row = await db.execute_fetchone(
                "SELECT game_id, store_identifier FROM game_wishlist WHERE store_identifier = ?",
                ("1693980",),
            )
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(wishlist_row["game_id"], owned_id)
        self.assertEqual(wishlist_row["store_identifier"], "1693980")

        # The minted row has no owned steam platform row of its own, so it
        # must survive clear_fulfilled_wishlist_entries (unlike before this
        # fix, where the row landed ON the owned game and was deleted here).
        deleted = await db_module.clear_fulfilled_wishlist_entries()
        self.assertEqual(deleted, 0)
        async with db_module.get_db() as db:
            still_there = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE store_identifier = ?", ("1693980",)
            )
        self.assertIsNotNone(still_there)

    async def test_collision_guard_fires_on_refunded_row_with_different_appid(self):
        # Identity doesn't lapse with ownership (ADR 0007): a REFUNDED copy
        # (owned=0) keeps its steam appid identifier, and a wishlist item with
        # a different appid attaching onto it by name is the same collapse as
        # the owned case. The owned=1-only guard missed exactly this.
        refunded_id = await seed_game("Dead Space")
        platform_id = await add_platform(refunded_id, "steam", owned=0)
        await add_identifier(platform_id, db_module.STEAM_APP_ID, "17470", is_primary=True)

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": [{"appid": 1693980}]}}

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
            patch.object(steam_wishlist, "fetch_app_name", AsyncMock(return_value="Dead Space")),
        ):
            result = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result["added"], 1)
        async with db_module.get_db() as db:
            rows = await db.execute_fetchall("SELECT id FROM games WHERE name = 'Dead Space'")
            wishlist_row = await db.execute_fetchone(
                "SELECT game_id FROM game_wishlist WHERE store_identifier = ?", ("1693980",)
            )
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(wishlist_row["game_id"], refunded_id)

    async def test_name_fallback_attaches_to_refunded_row_without_identifier(self):
        # The allow case the guard must NOT block: a refunded steam row with
        # NO stored appid is plausibly the same game (identifier never
        # captured), and re-wishlisting it should keep history on one row.
        refunded_id = await seed_game("Forgotten Refund")
        await add_platform(refunded_id, "steam", owned=0)

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": [{"appid": 777001}]}}

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
            patch.object(
                steam_wishlist, "fetch_app_name", AsyncMock(return_value="Forgotten Refund")
            ),
        ):
            result = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result["added"], 1)
        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT id FROM games WHERE name = 'Forgotten Refund'"
            )
            wishlist_row = await db.execute_fetchone(
                "SELECT game_id FROM game_wishlist WHERE store_identifier = ?", ("777001",)
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(wishlist_row["game_id"], refunded_id)

    async def test_duplicate_store_identifier_resolves_to_oldest_row(self):
        # Prod holds duplicate (platform, store_identifier) pairs (the DL2 /
        # D2R edition splits). The tie-break is the OLDEST wishlist row — the
        # original entry, not a later minted artifact — pinned here so a
        # future change is a decision, not an accident.
        first = await seed_game("Edition Split A")
        second = await seed_game("Edition Split B")
        await db_module.upsert_wishlist_entry(
            first, "steam", source="steam", store_identifier="888001"
        )
        await db_module.upsert_wishlist_entry(
            second, "steam", source="steam", store_identifier="888001"
        )
        resolved = await db_module.get_wishlist_game_id_by_store_identifier("steam", "888001")
        self.assertEqual(resolved, first)

    async def test_collision_guard_uses_raw_name_when_suffix_stripping_caused_it(self):
        # Issue #139 fix 1, Oblivion Remastered case: prepare_catalog_title
        # strips "Remastered", which would otherwise collide the 2026 remaster
        # (appid 2623190) onto the owned 2006 original. The raw store name
        # doesn't collide, so it's used verbatim instead of the stripped one.
        owned_id = await seed_game("The Elder Scrolls IV: Oblivion")
        platform_id = await add_platform(owned_id, "steam", owned=1)
        await add_identifier(platform_id, db_module.STEAM_APP_ID, "22330", is_primary=True)

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": [{"appid": 2623190}]}}

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
            patch.object(
                steam_wishlist,
                "fetch_app_name",
                AsyncMock(return_value="The Elder Scrolls IV: Oblivion Remastered"),
            ),
        ):
            result = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result["added"], 1)
        async with db_module.get_db() as db:
            minted = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = 'The Elder Scrolls IV: Oblivion Remastered'"
            )
            stripped_dupe = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = 'The Elder Scrolls IV: Oblivion' "
                "AND id != ?",
                (owned_id,),
            )
        self.assertIsNotNone(minted)
        self.assertIsNone(stripped_dupe)

    async def test_steamspy_fallback_names_a_fully_delisted_app(self):
        # Issue #139 fix 3: appdetails returns success:false for a fully
        # delisted app (appid 654050 "JYDGE"), so the store lookup alone can
        # never resolve it. SteamSpy still remembers real games it tracked
        # while they were live.
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": [{"appid": 654050}]}}

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
            patch.object(steam_wishlist, "fetch_app_name", AsyncMock(return_value=None)),
            patch.object(steam_wishlist, "fetch_steamspy_name", AsyncMock(return_value="JYDGE")),
        ):
            result = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped"], 0)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT id FROM games WHERE name = 'JYDGE'")
        self.assertIsNotNone(row)

    async def test_both_name_lookups_failing_still_skips(self):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": [{"appid": 999999}]}}

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
            patch.object(steam_wishlist, "fetch_app_name", AsyncMock(return_value=None)),
            patch.object(steam_wishlist, "fetch_steamspy_name", AsyncMock(return_value=None)),
        ):
            result = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["removed"], 0)

    async def test_reconciliation_resumes_when_all_items_resolve_including_via_identifier(self):
        # Once every item resolves — including one via the new
        # store_identifier path, which needs no name lookup at all —
        # removal reconciliation runs and can delete a genuinely stale row.
        stale_game = await seed_game("Stale Wishlist Entry")
        await db_module.upsert_wishlist_entry(
            stale_game, "steam", source="steam", store_identifier="1"
        )
        resolvable_via_identifier = await seed_game("Resolved Via Identifier")
        await db_module.upsert_wishlist_entry(
            resolvable_via_identifier, "steam", source="steam", store_identifier="888"
        )

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                # appid 1 ("Stale Wishlist Entry") is no longer on the
                # upstream wishlist this round.
                return {"response": {"items": [{"appid": 888}]}}

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
            patch.object(steam_wishlist, "fetch_app_name", AsyncMock()) as mock_fetch_name,
        ):
            result = await steam_wishlist.fetch_wishlist()

        mock_fetch_name.assert_not_awaited()
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["removed"], 1)
        async with db_module.get_db() as db:
            stale_row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (stale_game,)
            )
        self.assertIsNone(stale_row)

    async def test_manual_wishlisted_at_default_is_second_precision(self):
        # The Steam sync writes second-precision timestamps (epoch date_added);
        # the manual-add default should be indistinguishable in format.
        game_id = await seed_game("Precision Check")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="manual")
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT wishlisted_at FROM game_wishlist WHERE game_id = ?", (game_id,)
            )
        self.assertNotIn(".", row["wishlisted_at"])

    async def test_missing_credentials_reports_unconfigured(self):
        with (
            patch.object(steam_wishlist, "STEAM_API_KEY", ""),
            patch.object(steam_wishlist, "STEAM_ID", ""),
        ):
            result = await steam_wishlist.fetch_wishlist()
        self.assertEqual(result["sync_status"], "unconfigured")
        self.assertEqual(result["added"], 0)

    async def test_uses_per_item_date_added_when_present(self):
        game_id = await seed_game("Hollow Knight: Silksong")
        platform_id = await add_platform(game_id, "steam", owned=1)
        await db_module.upsert_game_platform_identifier(
            platform_id, db_module.STEAM_APP_ID, "333", is_primary=True
        )

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                # Steam Web API timestamps are conventionally Unix epoch seconds.
                return {"response": {"items": [{"appid": 333, "date_added": 1735689600}]}}

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
            await steam_wishlist.fetch_wishlist()

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT wishlisted_at FROM game_wishlist WHERE game_id = ?", (game_id,)
            )
        self.assertEqual(row["wishlisted_at"], "2025-01-01T00:00:00+00:00")

    async def test_falls_back_to_sync_time_when_date_added_missing(self):
        game_id = await seed_game("No Timestamp Game")
        platform_id = await add_platform(game_id, "steam", owned=1)
        await db_module.upsert_game_platform_identifier(
            platform_id, db_module.STEAM_APP_ID, "444", is_primary=True
        )

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": [{"appid": 444}]}}

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
            await steam_wishlist.fetch_wishlist()

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT wishlisted_at FROM game_wishlist WHERE game_id = ?", (game_id,)
            )
        self.assertIsNotNone(row["wishlisted_at"])

    async def test_removes_entry_no_longer_on_wishlist_when_fully_resolved(self):
        removed_game = await seed_game("Removed From Steam Wishlist")
        await db_module.upsert_wishlist_entry(removed_game, "steam", source="steam")

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": {"items": []}}

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

        self.assertEqual(result["removed"], 1)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (removed_game,)
            )
        self.assertIsNone(row)

    async def test_skips_removal_reconciliation_when_an_item_is_unresolved(self):
        # A pre-existing wishlist-only game (no stored identifier, per design)
        # that fails to resolve this round (e.g. a Steam Store hiccup) must not
        # be treated as "removed from your wishlist".
        survivor = await seed_game("Survivor Game")
        await db_module.upsert_wishlist_entry(survivor, "steam", source="steam")

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                # One resolvable item, one whose name lookup will fail below.
                return {"response": {"items": [{"appid": 555}]}}

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
            patch.object(steam_wishlist, "fetch_app_name", AsyncMock(return_value=None)),
        ):
            result = await steam_wishlist.fetch_wishlist()

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["removed"], 0)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (survivor,)
            )
        # Not confirmed present, but also not wrongly deleted.
        self.assertIsNotNone(row)


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
                    ]
                ),
            ),
        ):
            result = await dekudeals.sync_dekudeals_wishlist()

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped"], 0)
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

    async def test_creates_new_unowned_game_for_a_title_not_yet_in_the_library(self):
        # Most switch2 wishlist items are Nintendo exclusives never synced from
        # any other platform, so they have no games row yet at all — unlike
        # steam_wishlist.fetch_wishlist, this used to just skip them forever.
        with (
            patch.object(dekudeals, "DEKUDEALS_WISHLIST_URL", "https://www.dekudeals.com/wishlist/abc"),
            patch.object(
                dekudeals,
                "_fetch_wishlist_items",
                AsyncMock(
                    return_value=[
                        {"title": "Pikmin 4", "added_at": "2025-06-29T07:44:40+00:00"},
                    ]
                ),
            ),
        ):
            result = await dekudeals.sync_dekudeals_wishlist()

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0, "removed": 0, "total_scraped": 1})
        async with db_module.get_db() as db:
            game = await db.execute_fetchone("SELECT id FROM games WHERE name = 'Pikmin 4'")
            self.assertIsNotNone(game)
            wishlist_row = await db.execute_fetchone(
                "SELECT source, wishlisted_at FROM game_wishlist WHERE game_id = ? AND platform = ?",
                (game["id"], "switch2"),
            )
            gp_row = await db.execute_fetchone(
                "SELECT 1 FROM game_platforms WHERE game_id = ?", (game["id"],)
            )
        self.assertEqual(wishlist_row["source"], "dekudeals")
        self.assertEqual(wishlist_row["wishlisted_at"], "2025-06-29T07:44:40+00:00")
        # A wishlist-only game has no game_platforms row at all.
        self.assertIsNone(gp_row)

    async def test_re_sync_matches_previously_created_wishlist_only_game(self):
        # A second sync must not create a duplicate row for a title it already
        # created on a prior pass — mirrors upsert_game's exact-name fallback.
        with (
            patch.object(dekudeals, "DEKUDEALS_WISHLIST_URL", "https://www.dekudeals.com/wishlist/abc"),
            patch.object(
                dekudeals,
                "_fetch_wishlist_items",
                AsyncMock(return_value=[{"title": "Pikmin 4", "added_at": None}]),
            ),
        ):
            await dekudeals.sync_dekudeals_wishlist()
            result = await dekudeals.sync_dekudeals_wishlist()

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["matched"], 1)
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS n FROM games WHERE name = 'Pikmin 4'")
        self.assertEqual(count["n"], 1)

    async def test_removes_entry_no_longer_in_the_fetched_wishlist(self):
        removed_game = await seed_game("Taken Off Wishlist")
        await db_module.upsert_wishlist_entry(removed_game, "switch2", source="dekudeals")
        manual_game = await seed_game("Manually Added Switch Game")
        await db_module.upsert_wishlist_entry(manual_game, "switch2", source="manual")

        with (
            patch.object(dekudeals, "DEKUDEALS_WISHLIST_URL", "https://www.dekudeals.com/wishlist/abc"),
            patch.object(dekudeals, "_fetch_wishlist_items", AsyncMock(return_value=[])),
        ):
            result = await dekudeals.sync_dekudeals_wishlist()

        self.assertEqual(result["removed"], 1)
        async with db_module.get_db() as db:
            removed_row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (removed_game,)
            )
            manual_row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (manual_game,)
            )
        self.assertIsNone(removed_row)
        # Manual entries are never touched by the sync-source reconciliation.
        self.assertIsNotNone(manual_row)

    async def test_fetch_failure_propagates_instead_of_wiping_wishlist(self):
        game_id = await seed_game("Should Survive A Failed Fetch")
        await db_module.upsert_wishlist_entry(game_id, "switch2", source="dekudeals")

        class _Client:
            def __init__(self):
                self.get = AsyncMock(side_effect=httpx.ConnectError("boom"))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with (
            patch.object(dekudeals, "DEKUDEALS_WISHLIST_URL", "https://www.dekudeals.com/wishlist/abc"),
            patch.object(dekudeals.httpx, "AsyncClient", return_value=_Client()),
            self.assertRaises(httpx.ConnectError),
        ):
            await dekudeals.sync_dekudeals_wishlist()

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (game_id,)
            )
        self.assertIsNotNone(row)

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
            headers: ClassVar[dict[str, str]] = {}
            is_redirect = False

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


def _response(html: str):
    class _Resp:
        headers: ClassVar[dict[str, str]] = {}
        is_redirect = False

        def raise_for_status(self):
            return None

    resp = _Resp()
    resp.text = html
    return resp


class FetchSearchPricesTests(unittest.IsolatedAsyncioTestCase):
    async def test_maps_requested_title_to_matched_card(self):
        html = (FIXTURES_DIR / "dekudeals_search_page.html").read_text(encoding="utf-8")
        with patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=_response(html))
            results = await dekudeals.fetch_search_prices(["Hades"])
        self.assertIn("Hades", results)
        self.assertEqual(results["Hades"]["currency"], "EUR")

    async def test_switch2_filter_match_makes_single_request(self):
        # Real capture: https://www.dekudeals.com/search?q=mario+kart+world&filter[platform]=switch_2
        # "Mario Kart World" is Switch-2-exclusive and matches on the very first
        # (switch_2-filtered) request, so no fallback request should be made.
        html = (FIXTURES_DIR / "dekudeals_search_page_switch2_filter_match.html").read_text(
            encoding="utf-8"
        )
        with patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=_response(html))
            results = await dekudeals.fetch_search_prices(["Mario Kart World"])
        self.assertIn("Mario Kart World", results)
        self.assertEqual(client.get.call_count, 1)
        called_url = client.get.call_args.args[0]
        self.assertEqual(
            called_url,
            "https://www.dekudeals.com/search?q=Mario+Kart+World&filter%5Bplatform%5D=switch_2",
        )

    async def test_falls_back_to_switch_filter_when_switch2_has_no_match(self):
        # Real captures: switch_2-filtered "hades" search has no "Hades" card
        # (only "Hades II" and its upgrade pack — the two facets are disjoint),
        # but the switch-filtered search does have one, at EUR 24.99.
        no_match_html = (
            FIXTURES_DIR / "dekudeals_search_page_switch2_filter_no_match.html"
        ).read_text(encoding="utf-8")
        match_html = (FIXTURES_DIR / "dekudeals_search_page_switch_filter_fallback.html").read_text(
            encoding="utf-8"
        )
        with patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(side_effect=[_response(no_match_html), _response(match_html)])
            with patch("gamelib_mcp.data.dekudeals.asyncio.sleep", new=AsyncMock()) as sleep_mock:
                results = await dekudeals.fetch_search_prices(["Hades"])

        self.assertIn("Hades", results)
        self.assertEqual(results["Hades"]["currency"], "EUR")
        self.assertEqual(client.get.call_count, 2)
        first_url = client.get.call_args_list[0].args[0]
        second_url = client.get.call_args_list[1].args[0]
        self.assertEqual(
            first_url, "https://www.dekudeals.com/search?q=Hades&filter%5Bplatform%5D=switch_2"
        )
        self.assertEqual(
            second_url, "https://www.dekudeals.com/search?q=Hades&filter%5Bplatform%5D=switch"
        )
        # Politeness pacing applies before the fallback request too.
        sleep_mock.assert_awaited_once()

    async def test_neither_filter_matches_reports_confirmed_miss(self):
        # Both filtered searches loaded and neither held a card: an answer, not
        # an absence of one. Reported as an explicit None so the caller can
        # negatively cache it.
        html = (FIXTURES_DIR / "dekudeals_search_page_switch2_filter_no_match.html").read_text(
            encoding="utf-8"
        )
        with patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=_response(html))
            with patch("gamelib_mcp.data.dekudeals.asyncio.sleep", new=AsyncMock()):
                results = await dekudeals.fetch_search_prices(["Completely Unrelated Title XYZ"])
        self.assertEqual(results, {"Completely Unrelated Title XYZ": None})
        self.assertEqual(client.get.call_count, 2)

    async def test_fetch_error_skips_title_without_raising(self):
        # The counterpart to the test above: a title whose searches never loaded
        # must stay ABSENT, never a None. A None would be negatively cached and
        # suppress retries for a game DekuDeals may well sell.
        with patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
            with patch("gamelib_mcp.data.dekudeals.asyncio.sleep", new=AsyncMock()):
                results = await dekudeals.fetch_search_prices(["Hades"])
        self.assertEqual(results, {})

    async def test_partial_fetch_failure_is_not_a_confirmed_miss(self):
        # switch_2 loaded with no match, the switch fallback errored — the
        # fallback might have matched, so the outcome is unknown, not a miss.
        html = (FIXTURES_DIR / "dekudeals_search_page_switch2_filter_no_match.html").read_text(
            encoding="utf-8"
        )
        with patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(
                side_effect=[_response(html), httpx.ConnectError("boom")]
            )
            with patch("gamelib_mcp.data.dekudeals.asyncio.sleep", new=AsyncMock()):
                results = await dekudeals.fetch_search_prices(["Hades"])
        self.assertEqual(results, {})

    async def test_no_platform_filters_reports_nothing_rather_than_a_miss(self):
        # A confirmed miss must rest on a page that actually loaded. With no
        # filters to try, no request is made — reporting the title as absent
        # from DekuDeals would negatively cache an answer nothing looked for.
        with patch("gamelib_mcp.data.dekudeals._SEARCH_PLATFORM_FILTERS", ()), \
             patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock()
            results = await dekudeals.fetch_search_prices(["Hades"])
        self.assertEqual(results, {})
        client.get.assert_not_awaited()

    async def test_no_fuzzy_match_yields_confirmed_miss(self):
        html = (FIXTURES_DIR / "dekudeals_search_page.html").read_text(encoding="utf-8")
        with patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=_response(html))
            with patch("gamelib_mcp.data.dekudeals.asyncio.sleep", new=AsyncMock()):
                results = await dekudeals.fetch_search_prices(["Completely Unrelated Title XYZ"])
        self.assertEqual(results, {"Completely Unrelated Title XYZ": None})

    async def test_empty_titles_returns_empty_without_fetching(self):
        with patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            results = await dekudeals.fetch_search_prices([])
        self.assertEqual(results, {})
        client_cls.assert_not_called()


class UpsertGamePricesTests(ToolDBTestCase):
    async def test_reupsert_overwrites_stale_price_with_one_row(self):
        game_id = await seed_game("Price Tracked Game")

        written_first = await db_module.upsert_game_prices([
            {
                "game_id": game_id,
                "platform": "steam",
                "shop": "steam",
                "price": 19.99,
                "regular_price": 39.99,
                "cut_pct": 50,
                "currency": "USD",
                "deal_url": "https://store.steampowered.com/app/1",
            }
        ])
        async with db_module.get_db() as db:
            first_row = await db.execute_fetchone(
                "SELECT price, fetched_at FROM game_prices "
                "WHERE game_id = ? AND platform = ? AND shop = ?",
                (game_id, "steam", "steam"),
            )

        # Sleep to ensure the second upsert gets a distinct timestamp
        await asyncio.sleep(0.01)

        written_second = await db_module.upsert_game_prices([
            {
                "game_id": game_id,
                "platform": "steam",
                "shop": "steam",
                "price": 9.99,
                "regular_price": 39.99,
                "cut_pct": 75,
                "currency": "USD",
                "deal_url": "https://store.steampowered.com/app/1",
            }
        ])

        self.assertEqual(written_first, 1)
        self.assertEqual(written_second, 1)
        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT price, cut_pct, fetched_at FROM game_prices "
                "WHERE game_id = ? AND platform = ? AND shop = ?",
                (game_id, "steam", "steam"),
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 9.99)
        self.assertEqual(rows[0]["cut_pct"], 75)
        # fetched_at is re-stamped on every upsert, even when reusing the row.
        # Strictly greater (not just >=) to prove it actually advanced.
        self.assertGreater(rows[0]["fetched_at"], first_row["fetched_at"])

    async def test_distinct_shops_for_same_game_platform_get_separate_rows(self):
        game_id = await seed_game("Multi Shop Game")

        await db_module.upsert_game_prices([
            {
                "game_id": game_id,
                "platform": "steam",
                "shop": "steam",
                "price": 19.99,
                "regular_price": 19.99,
                "cut_pct": 0,
                "currency": "USD",
                "deal_url": None,
            },
            {
                "game_id": game_id,
                "platform": "steam",
                "shop": "gog",
                "price": 14.99,
                "regular_price": 19.99,
                "cut_pct": 25,
                "currency": "USD",
                "deal_url": None,
            },
        ])

        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT shop, price FROM game_prices WHERE game_id = ? ORDER BY shop", (game_id,)
            )
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["shop"] for r in rows], ["gog", "steam"])

    async def test_new_winning_shop_prunes_stale_losing_shop_row(self):
        # Regression for: game_prices' UNIQUE(game_id, platform, shop) means
        # each ITAD refresh only ever upserts the row for the CURRENT winning
        # shop. A previous winner's row (e.g. a GOG sale two weeks ago) must
        # not survive forever once a different shop wins, or the stale price
        # looks permanently "cheapest" and refresh=True can never fix it.
        game_id = await seed_game("Stale Shop Game")

        await db_module.upsert_game_prices([
            {
                "game_id": game_id,
                "platform": "steam",
                "shop": "GOG",
                "price": 5.00,
                "regular_price": 5.00,
                "cut_pct": 0,
                "currency": "USD",
                "deal_url": "https://gog.com/deal",
            }
        ])

        # A later refresh's batch reflects only today's winning shop (Steam).
        await db_module.upsert_game_prices([
            {
                "game_id": game_id,
                "platform": "steam",
                "shop": "Steam",
                "price": 20.00,
                "regular_price": 20.00,
                "cut_pct": 0,
                "currency": "USD",
                "deal_url": "https://store.steampowered.com/app/1",
            }
        ])

        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT shop, price FROM game_prices WHERE game_id = ?", (game_id,)
            )
        self.assertEqual([(r["shop"], r["price"]) for r in rows], [("Steam", 20.00)])


class LoadWishlistWithPricesTests(ToolDBTestCase):
    async def test_wishlist_row_with_no_cached_price_has_null_price(self):
        game_id = await seed_game("No Price Yet")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam")

        rows = await db_module.load_wishlist_with_prices(None)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["game_id"], game_id)
        self.assertIsNone(rows[0]["price"])

    async def test_cached_price_surfaces_through_the_join(self):
        game_id = await seed_game("Has A Cached Price")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam")
        await db_module.upsert_game_prices([
            {
                "game_id": game_id,
                "platform": "steam",
                "shop": "steam",
                "price": 4.99,
                "regular_price": 19.99,
                "cut_pct": 75,
                "currency": "USD",
                "deal_url": "https://store.steampowered.com/app/1",
            }
        ])

        rows = await db_module.load_wishlist_with_prices(None)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 4.99)
        self.assertEqual(rows[0]["shop"], "steam")
        self.assertEqual(rows[0]["cut_pct"], 75)
        self.assertEqual(rows[0]["deal_url"], "https://store.steampowered.com/app/1")

    async def test_platform_filter_excludes_other_platform_rows(self):
        game_id = await seed_game("On Two Wishlists")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam")
        await db_module.upsert_wishlist_entry(game_id, "switch2", source="dekudeals")

        steam_only = await db_module.load_wishlist_with_prices("steam")

        self.assertEqual(len(steam_only), 1)
        self.assertEqual(steam_only[0]["platform"], "steam")

    async def test_steam_appid_resolves_from_store_identifier(self):
        game_id = await seed_game("Store Identifier Game")
        await db_module.upsert_wishlist_entry(
            game_id, "steam", source="steam", store_identifier="123456"
        )

        rows = await db_module.load_wishlist_with_prices(None)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["steam_appid"], 123456)

    async def test_steam_appid_falls_back_to_owned_row_identifier(self):
        # Wishlisted on switch2, but owned on steam under the same game_id
        # (e.g. a bundle/gift) — no store_identifier on the wishlist row
        # itself, so it should fall back to the owned-row subquery.
        game_id = await seed_game("Owned Elsewhere Game")
        await db_module.upsert_wishlist_entry(game_id, "switch2", source="dekudeals")
        platform_id = await add_platform(game_id, "steam", owned=1)
        await add_identifier(platform_id, db_module.STEAM_APP_ID, "778899", is_primary=True)

        rows = await db_module.load_wishlist_with_prices(None)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["steam_appid"], 778899)


class LoadWishlistWithPricesCrossPlatformTests(ToolDBTestCase):
    async def test_returns_prices_from_other_platforms_with_metadata(self):
        game_id = await seed_game("Crossplay Deal")
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET igdb_platforms = '[6, 508]', igdb_cached_at = 'x' WHERE id = ?",
                (game_id,),
            )
            await db.commit()
        await add_platform(game_id, "epic", owned=1)  # owned elsewhere; not on candidates
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam", store_identifier="42")
        await db_module.upsert_game_prices([
            {"game_id": game_id, "platform": "steam", "shop": "Steam", "price": 10.0,
             "regular_price": 10.0, "cut_pct": 0, "currency": "EUR", "deal_url": "u1"},
            {"game_id": game_id, "platform": "switch2", "shop": "dekudeals", "price": 12.0,
             "regular_price": 12.0, "cut_pct": 0, "currency": "EUR", "deal_url": "u2"},
        ])

        rows = await db_module.load_wishlist_with_prices(None)
        mine = [r for r in rows if r["game_id"] == game_id]
        self.assertEqual({r["price_platform"] for r in mine}, {"steam", "switch2"})
        self.assertEqual(json.loads(mine[0]["igdb_platforms"]), [6, 508])
        self.assertEqual(json.loads(mine[0]["owned_platforms"]), ["epic"])
        self.assertEqual(mine[0]["steam_appid"], 42)

    async def test_platform_filter_still_filters_wishlist_rows_not_price_rows(self):
        game_id = await seed_game("Filtered")
        await db_module.upsert_wishlist_entry(game_id, "switch2", source="dekudeals")
        rows = await db_module.load_wishlist_with_prices("steam")
        self.assertEqual([r for r in rows if r["game_id"] == game_id], [])


if __name__ == "__main__":
    unittest.main()
