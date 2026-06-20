"""Characterization tests for gamelib_mcp.tools.platforms."""

import json
from unittest.mock import AsyncMock, patch

from fastmcp.exceptions import ToolError

from conftest import ToolDBTestCase, make_steam_game, seed_game, add_platform
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import steamspy
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


class SetHardwarePreferenceTests(ToolDBTestCase):
    async def test_normalizes_aliases_and_persists(self):
        result = await platforms.set_hardware_preference(["nintendo", "steam"])
        self.assertEqual(
            result, {"hardware_preference": ["switch2", "steam"]}
        )
        stored = await db_module.get_meta("hardware_preference")
        self.assertEqual(json.loads(stored), ["switch2", "steam"])

    async def test_rejects_unknown_platform(self):
        with self.assertRaisesRegex(ToolError, "Unknown platform 'dreamcast'"):
            await platforms.set_hardware_preference(["steam", "dreamcast"])


class AddGameToPlatformTests(ToolDBTestCase):
    async def test_unknown_platform_error(self):
        with self.assertRaisesRegex(ToolError, "Unknown platform 'playstation'"):
            await platforms.add_game_to_platform("Foo", "playstation")

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
                "created",
                "game_id",
                "game_platform_id",
                "name",
                "platform",
                "playtime_minutes",
                "identifier",
            },
        )
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

    async def test_accepts_ea_manual_platform(self):
        result = await platforms.add_game_to_platform("Dragon Age", "ea")
        self.assertTrue(result["created"])
        self.assertEqual(result["platform"], "ea")

    async def test_accepts_ubisoft_manual_platform(self):
        result = await platforms.add_game_to_platform("Assassin's Creed", "ubisoft")
        self.assertTrue(result["created"])
        self.assertEqual(result["platform"], "ubisoft")

    async def test_uplay_alias_resolves_to_ubisoft(self):
        result = await platforms.add_game_to_platform("Far Cry", "uplay")
        self.assertEqual(result["platform"], "ubisoft")  # alias resolved

    async def test_origin_alias_resolves_to_ea(self):
        result = await platforms.add_game_to_platform("Burnout Paradise", "origin")
        self.assertEqual(result["platform"], "ea")  # alias resolved

    async def test_rejects_empty_name(self):
        with self.assertRaisesRegex(ToolError, "name must not be empty"):
            await platforms.add_game_to_platform("   ", "steam")

    async def test_rejects_negative_playtime(self):
        with self.assertRaisesRegex(ToolError, "playtime_minutes must not be negative"):
            await platforms.add_game_to_platform("Some Game", "gog", playtime_minutes=-5)


class UpdateGameTests(ToolDBTestCase):
    async def _overrides(self, game_id: int) -> set[str]:
        async with db_module.get_db() as db:
            return await db_module.get_manual_overrides(db, game_id)

    async def test_updates_fields_and_records_overrides(self):
        gid = await seed_game("Editable", tags=["old"], genres=["RPG"])
        result = await platforms.update_game(
            name="Editable",
            tags=["roguelike", "indie"],
            release_date="2021-01-01",
            short_description="hand fixed",
        )
        self.assertEqual(result["game_id"], gid)
        self.assertEqual(result["updated"]["tags"], ["roguelike", "indie"])
        self.assertEqual(
            set(result["manual_overrides"]),
            {"tags", "release_date", "short_description"},
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT tags, release_date, short_description FROM games WHERE id = ?", (gid,)
            )
        self.assertEqual(json.loads(row["tags"]), ["roguelike", "indie"])
        self.assertEqual(row["release_date"], "2021-01-01")
        self.assertEqual(row["short_description"], "hand fixed")

    async def test_rename_updates_name_and_search(self):
        gid = await seed_game("Wrong Title")
        result = await platforms.update_game(game_id=gid, new_name="Correct Title")
        self.assertEqual(result["name"], "Correct Title")
        self.assertIn("name", result["manual_overrides"])
        # new name is searchable via the normalized column
        found = await platforms.update_game(name="correct title", sort_name="Correct")
        self.assertEqual(found["game_id"], gid)

    async def test_mark_and_unmark_farmed(self):
        gid = await seed_game("Maybe Farmed", is_farmed=0)
        await platforms.update_game(game_id=gid, is_farmed=True)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT is_farmed FROM games WHERE id = ?", (gid,))
        self.assertEqual(row["is_farmed"], 1)
        self.assertIn("is_farmed", await self._overrides(gid))

    async def test_requires_a_field(self):
        gid = await seed_game("Bare")
        with self.assertRaisesRegex(ToolError, "at least one field"):
            await platforms.update_game(game_id=gid)

    async def test_rejects_negative_hltb(self):
        gid = await seed_game("Timed")
        with self.assertRaisesRegex(ToolError, "hltb_main must not be negative"):
            await platforms.update_game(game_id=gid, hltb_main=-1)

    async def test_missing_game_raises(self):
        with self.assertRaisesRegex(ToolError, "Game not found"):
            await platforms.update_game(name="Nope Nope Nope", tags=["x"])

    async def test_requires_lookup_key(self):
        with self.assertRaisesRegex(ToolError, "Provide game_id or name"):
            await platforms.update_game(tags=["x"])

    async def test_rejects_empty_new_name(self):
        gid = await seed_game("Has Name")
        with self.assertRaisesRegex(ToolError, "new_name must not be empty"):
            await platforms.update_game(game_id=gid, new_name="   ")


class UpdateGameProtectionTests(ToolDBTestCase):
    async def test_steamspy_does_not_clobber_manual_tags(self):
        gid = await make_steam_game("Spy Game", 555, tags=["original"])
        await platforms.update_game(game_id=gid, tags=["my", "tags"])

        with patch.object(
            steamspy, "_fetch_steamspy", AsyncMock(return_value={"Action": 99, "Co-op": 50})
        ):
            await steamspy.enrich_steamspy(555)

        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT tags FROM games WHERE id = ?", (gid,))
        self.assertEqual(json.loads(row["tags"]), ["my", "tags"])

    async def test_steamspy_still_updates_unprotected_tags(self):
        gid = await make_steam_game("Open Game", 556, tags=["original"])

        with patch.object(
            steamspy, "_fetch_steamspy", AsyncMock(return_value={"Action": 99})
        ):
            await steamspy.enrich_steamspy(556)

        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT tags FROM games WHERE id = ?", (gid,))
        self.assertIn("Action", json.loads(row["tags"]))
