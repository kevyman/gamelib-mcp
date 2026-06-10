"""Characterization tests for gamelib_mcp.tools.platforms."""

import json

from conftest import ToolDBTestCase, seed_game, add_platform
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import platforms


class PlatformBreakdownTests(ToolDBTestCase):
    async def test_counts_overlap_and_shape(self):
        multi = await seed_game("Multiplat")
        await add_platform(multi, "steam")
        await add_platform(multi, "switch2")
        solo = await seed_game("SteamOnly")
        await add_platform(solo, "steam")
        result = await platforms.get_platform_breakdown()
        self.assertEqual(
            set(result),
            {"by_platform", "total_unique_games", "overlap_count", "overlap_games"},
        )
        self.assertEqual(result["total_unique_games"], 2)
        counts = {r["platform"]: r["owned_games"] for r in result["by_platform"]}
        self.assertEqual(counts, {"steam": 2, "switch2": 1})
        self.assertEqual(result["overlap_count"], 1)
        overlap = result["overlap_games"][0]
        self.assertEqual(overlap["name"], "Multiplat")
        self.assertEqual(set(overlap["owned_on"]), {"steam", "switch2"})


class SyncPlatformTests(ToolDBTestCase):
    async def test_unknown_platform_returns_error(self):
        result = await platforms.sync_platform("playstation")
        self.assertIn("error", result)
        self.assertIn("Unknown platform", result["error"])


class SetHardwarePreferenceTests(ToolDBTestCase):
    async def test_normalizes_aliases_and_persists(self):
        result = await platforms.set_hardware_preference(["nintendo", "steam"])
        self.assertEqual(
            result, {"success": True, "hardware_preference": ["switch2", "steam"]}
        )
        stored = await db_module.get_meta("hardware_preference")
        self.assertEqual(json.loads(stored), ["switch2", "steam"])


class AddGameToPlatformTests(ToolDBTestCase):
    async def test_unknown_platform_error(self):
        result = await platforms.add_game_to_platform("Foo", "playstation")
        self.assertIn("error", result)

    async def test_creates_new_game(self):
        result = await platforms.add_game_to_platform(
            "Physical Cart",
            "switch2",
            identifier_type="gog_product_id",
            identifier_value="123",
            playtime_minutes=45,
        )
        self.assertEqual(
            set(result),
            {
                "success",
                "created",
                "game_id",
                "game_platform_id",
                "name",
                "platform",
                "playtime_minutes",
                "identifier",
            },
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["created"])
        self.assertEqual(result["platform"], "switch2")
        self.assertEqual(result["playtime_minutes"], 45)
        self.assertEqual(result["identifier"], {"type": "gog_product_id", "value": "123"})

    async def test_existing_game_not_created_and_alias_resolved(self):
        await seed_game("Existing Game")
        result = await platforms.add_game_to_platform("Existing Game", "nintendo")
        self.assertFalse(result["created"])
        self.assertEqual(result["platform"], "switch2")  # alias resolved
        self.assertIsNone(result["identifier"])
