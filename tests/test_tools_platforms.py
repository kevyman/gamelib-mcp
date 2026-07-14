"""Characterization tests for gamelib_mcp.tools.platforms."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastmcp.exceptions import ToolError

from conftest import (
    ToolDBTestCase,
    add_enrichment,
    add_platform,
    make_steam_game,
    seed_game,
)
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import hltb, igdb, steamspy
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
            {
                "by_platform",
                "total_unique_games",
                "total_unique_addons",
                "overlap_count",
                "overlap_games",
            },
        )
        self.assertEqual(result["total_unique_games"], 2)
        self.assertEqual(result["total_unique_addons"], 0)
        counts = {r["platform"]: r["owned_games"] for r in result["by_platform"]}
        self.assertEqual(counts, {"steam": 2, "switch2": 1})
        # No addons anywhere in this seed — every platform still reports the key.
        addon_counts = {r["platform"]: r["owned_addons"] for r in result["by_platform"]}
        self.assertEqual(addon_counts, {"steam": 0, "switch2": 0})
        self.assertEqual(result["overlap_count"], 1)
        overlap = result["overlap_games"][0]
        self.assertEqual(overlap["name"], "Multiplat")
        self.assertEqual(set(overlap["owned_on"]), {"steam", "switch2"})

    async def test_owned_dlc_excluded_from_games_counted_as_addons(self):
        base = await seed_game("Base Game")
        await add_platform(base, "steam")
        dlc = await seed_game(
            "Base Game DLC", content_type="dlc", is_primary_library_item=0,
            parent_game_id=base,
        )
        await add_platform(dlc, "steam")
        result = await platforms.get_platform_breakdown()
        self.assertEqual(result["total_unique_games"], 1)
        self.assertEqual(result["total_unique_addons"], 1)
        steam_entry = next(r for r in result["by_platform"] if r["platform"] == "steam")
        self.assertEqual(steam_entry["owned_games"], 1)
        self.assertEqual(steam_entry["owned_addons"], 1)
        # The overlap list stays primary-only even when a DLC row also
        # happens to be owned on multiple platforms.
        await add_platform(dlc, "switch2")
        result2 = await platforms.get_platform_breakdown()
        self.assertEqual(result2["overlap_count"], 0)


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
                "wishlist_id",
                "name",
                "platform",
                "owned",
                "playtime_minutes",
                "identifier",
                "acquisition",
            },
        )
        self.assertTrue(result["created"])
        self.assertEqual(result["platform"], "switch2")
        self.assertTrue(result["owned"])
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

    async def test_acquisition_params_persisted_on_owned_add(self):
        result = await platforms.add_game_to_platform(
            "Boxed Copy",
            "switch2",
            acquired_at="2024-11-29",
            price_paid=39.99,
            price_currency="eur",
            purchase_source="retail",  # alias -> physical
            bundle_name="Black Friday Haul",
        )
        self.assertEqual(
            result["acquisition"],
            {
                "acquired_at": "2024-11-29",
                "price_paid": 39.99,
                "price_currency": "EUR",
                "purchase_source": "physical",
                "bundle_name": "Black Friday Haul",
            },
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                """SELECT acquired_at, price_paid, price_currency,
                          purchase_source, bundle_name
                   FROM game_platforms WHERE id = ?""",
                (result["game_platform_id"],),
            )
        self.assertEqual(row["acquired_at"], "2024-11-29")
        self.assertEqual(row["price_paid"], 39.99)
        self.assertEqual(row["price_currency"], "EUR")
        self.assertEqual(row["purchase_source"], "physical")
        self.assertEqual(row["bundle_name"], "Black Friday Haul")

    async def test_acquisition_omitted_leaves_acquisition_null(self):
        result = await platforms.add_game_to_platform("Plain Add", "gog")
        self.assertIsNone(result["acquisition"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT price_paid, purchase_source FROM game_platforms WHERE id = ?",
                (result["game_platform_id"],),
            )
        self.assertIsNone(row["price_paid"])
        self.assertIsNone(row["purchase_source"])

    async def test_acquisition_param_with_owned_false_raises(self):
        with self.assertRaisesRegex(ToolError, "require owned=True"):
            await platforms.add_game_to_platform(
                "Wishlist Wanted", "ps5", owned=False, price_paid=19.99
            )
        # Validation happens before any write — no games row is left behind.
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = 'Wishlist Wanted'"
            )
        self.assertIsNone(row)

    async def test_invalid_acquisition_value_raises_before_write(self):
        with self.assertRaisesRegex(ToolError, "Unknown purchase_source"):
            await platforms.add_game_to_platform(
                "Bad Source", "steam", purchase_source="carrier pigeon"
            )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = 'Bad Source'"
            )
        self.assertIsNone(row)


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

    async def test_manual_tags_are_canonicalized(self):
        gid = await seed_game("Manual Tags")
        result = await platforms.update_game(
            name="Manual Tags", tags=["Soulslike", "Co-op", "Atmospheric"]
        )
        # Manual tags share the canonical vocabulary so tag filters still match.
        self.assertEqual(result["updated"]["tags"], ["souls-like", "co-op", "atmospheric"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT tags FROM games WHERE id = ?", (gid,))
        self.assertEqual(json.loads(row["tags"]), ["souls-like", "co-op", "atmospheric"])

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

    async def test_sets_completion_status(self):
        gid = await seed_game("Hades")
        result = await platforms.update_game(game_id=gid, completion_status="completed")
        self.assertEqual(result["updated"]["completion_status"], "completed")
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT completion_status FROM games WHERE id = ?", (gid,)
            )
        self.assertEqual(row["completion_status"], "completed")
        self.assertIn("completion_status", await self._overrides(gid))

    async def test_sets_completion_status_evergreen(self):
        gid = await seed_game("Rocket League")
        result = await platforms.update_game(game_id=gid, completion_status="evergreen")
        self.assertEqual(result["updated"]["completion_status"], "evergreen")
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT completion_status FROM games WHERE id = ?", (gid,)
            )
        self.assertEqual(row["completion_status"], "evergreen")
        self.assertIn("completion_status", await self._overrides(gid))

    async def test_completion_status_none_resets(self):
        gid = await seed_game("Hades 2")
        await platforms.update_game(game_id=gid, completion_status="completed")
        result = await platforms.update_game(game_id=gid, completion_status="none")
        self.assertIsNone(result["updated"]["completion_status"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT completion_status FROM games WHERE id = ?", (gid,)
            )
        self.assertIsNone(row["completion_status"])

    async def test_rejects_bad_completion_status(self):
        gid = await seed_game("Hades 3")
        with self.assertRaisesRegex(ToolError, "Unknown completion_status 'finished'"):
            await platforms.update_game(game_id=gid, completion_status="finished")

    async def test_content_type_promotes_bundle_back_to_primary(self):
        # A "+" compilation IGDB mis-filed as a bundle: is_primary=0 hides it
        # from stats/series/discover, and a stray parent orphans it.
        parent = await seed_game("Super Mario 3D World")
        gid = await seed_game(
            "Super Mario 3D World + Bowser's Fury",
            content_type="bundle",
            is_primary_library_item=0,
            parent_game_id=parent,
        )
        result = await platforms.update_game(game_id=gid, content_type="base_game")
        self.assertEqual(result["updated"]["content_type"], "base_game")
        self.assertIs(result["updated"]["is_primary_library_item"], True)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT content_type, is_primary_library_item, parent_game_id "
                "FROM games WHERE id = ?",
                (gid,),
            )
        self.assertEqual(row["content_type"], "base_game")
        self.assertEqual(row["is_primary_library_item"], 1)
        self.assertIsNone(row["parent_game_id"])
        # All three columns are protected so the next IGDB pass can't re-demote it.
        self.assertEqual(
            {"content_type", "is_primary_library_item", "parent_game_id"},
            await self._overrides(gid),
        )

    async def test_content_type_nested_demotes_and_keeps_parent(self):
        gid = await seed_game("Some DLC", is_primary_library_item=1)
        result = await platforms.update_game(game_id=gid, content_type="dlc")
        self.assertIs(result["updated"]["is_primary_library_item"], False)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT is_primary_library_item FROM games WHERE id = ?", (gid,)
            )
        self.assertEqual(row["is_primary_library_item"], 0)
        # Demotion does not touch parent_game_id, so it isn't recorded.
        self.assertEqual(
            {"content_type", "is_primary_library_item"}, await self._overrides(gid)
        )

    async def test_rejects_bad_content_type(self):
        gid = await seed_game("Whatever")
        with self.assertRaisesRegex(ToolError, "Unknown content_type 'game'"):
            await platforms.update_game(game_id=gid, content_type="game")

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

    async def test_clear_only_removes_protection(self):
        gid = await seed_game("Clearable", tags=["x"])
        await platforms.update_game(game_id=gid, tags=["a"], is_farmed=True)
        result = await platforms.update_game(game_id=gid, clear_overrides=["is_farmed"])
        self.assertEqual(result["updated"], {})
        self.assertEqual(result["cleared"], ["is_farmed"])
        self.assertEqual(set(result["manual_overrides"]), {"tags"})
        self.assertEqual(await self._overrides(gid), {"tags"})

    async def test_clear_requires_known_column(self):
        gid = await seed_game("Bad Clear")
        with self.assertRaisesRegex(ToolError, "unknown column"):
            await platforms.update_game(game_id=gid, clear_overrides=["bogus"])

    async def test_cannot_set_and_clear_same_column(self):
        gid = await seed_game("Conflict")
        with self.assertRaisesRegex(ToolError, "set and clear the same"):
            await platforms.update_game(game_id=gid, tags=["a"], clear_overrides=["tags"])


class UpdateGameParentTests(ToolDBTestCase):
    async def _overrides(self, game_id: int) -> set[str]:
        async with db_module.get_db() as db:
            return await db_module.get_manual_overrides(db, game_id)

    async def test_parent_by_name_with_nested_content_type(self):
        parent = await seed_game("Base Game")
        gid = await seed_game("Base Game DLC")
        result = await platforms.update_game(
            game_id=gid, content_type="dlc", parent_name="Base Game"
        )
        self.assertEqual(result["updated"]["parent_game_id"], parent)
        self.assertEqual(result["updated"]["content_type"], "dlc")
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT content_type, parent_game_id, is_primary_library_item "
                "FROM games WHERE id = ?",
                (gid,),
            )
        self.assertEqual(row["content_type"], "dlc")
        self.assertEqual(row["parent_game_id"], parent)
        self.assertEqual(row["is_primary_library_item"], 0)
        self.assertEqual(
            {"content_type", "is_primary_library_item", "parent_game_id"},
            await self._overrides(gid),
        )

    async def test_parent_by_id(self):
        parent = await seed_game("Parent By Id")
        gid = await seed_game(
            "Nested Already", content_type="dlc", is_primary_library_item=0
        )
        result = await platforms.update_game(game_id=gid, parent_game_id=parent)
        self.assertEqual(result["updated"]["parent_game_id"], parent)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT parent_game_id FROM games WHERE id = ?", (gid,)
            )
        self.assertEqual(row["parent_game_id"], parent)

    async def test_parent_name_unresolved_raises(self):
        gid = await seed_game(
            "Orphan DLC", content_type="dlc", is_primary_library_item=0
        )
        with self.assertRaisesRegex(ToolError, "No game named"):
            await platforms.update_game(game_id=gid, parent_name="Nonexistent Game XYZ")

    async def test_parent_cannot_be_self(self):
        gid = await seed_game(
            "Self Referential", content_type="dlc", is_primary_library_item=0
        )
        with self.assertRaisesRegex(ToolError, "cannot be its own parent"):
            await platforms.update_game(game_id=gid, parent_game_id=gid)

    async def test_parent_cannot_be_nested_content(self):
        nested_parent = await seed_game(
            "Nested Parent Candidate", content_type="dlc", is_primary_library_item=0
        )
        gid = await seed_game(
            "Needs A Parent", content_type="dlc", is_primary_library_item=0
        )
        with self.assertRaisesRegex(ToolError, "nested content itself"):
            await platforms.update_game(game_id=gid, parent_game_id=nested_parent)

    async def test_both_parent_params_raises(self):
        parent = await seed_game("Either Parent")
        gid = await seed_game(
            "Ambiguous Parent Call", content_type="dlc", is_primary_library_item=0
        )
        with self.assertRaisesRegex(ToolError, "not both"):
            await platforms.update_game(
                game_id=gid, parent_game_id=parent, parent_name="Either Parent"
            )

    async def test_parent_without_nested_content_type_on_base_game_raises(self):
        parent = await seed_game("Waiting Parent")
        gid = await seed_game("Still A Base Game")  # defaults to base_game
        with self.assertRaisesRegex(ToolError, "not nested content"):
            await platforms.update_game(game_id=gid, parent_game_id=parent)

    async def test_parent_on_already_nested_row_without_content_type_in_call(self):
        parent = await seed_game("Existing Parent")
        gid = await seed_game(
            "Already Nested Row", content_type="dlc", is_primary_library_item=0
        )
        result = await platforms.update_game(game_id=gid, parent_game_id=parent)
        self.assertEqual(result["updated"], {"parent_game_id": parent})
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT content_type, parent_game_id FROM games WHERE id = ?", (gid,)
            )
        self.assertEqual(row["content_type"], "dlc")  # untouched
        self.assertEqual(row["parent_game_id"], parent)

    async def test_primary_content_type_and_parent_in_same_call_raises(self):
        parent = await seed_game("Would Be Parent")
        gid = await seed_game(
            "Bundle Mistake", content_type="bundle", is_primary_library_item=0,
            parent_game_id=parent,
        )
        with self.assertRaisesRegex(ToolError, "Cannot set a parent"):
            await platforms.update_game(
                game_id=gid, content_type="base_game", parent_game_id=parent
            )

    async def test_detach_idiom_nulls_parent(self):
        parent = await seed_game("Detach Parent")
        gid = await seed_game(
            "Detach Child", content_type="dlc", is_primary_library_item=0,
            parent_game_id=parent,
        )
        result = await platforms.update_game(game_id=gid, parent_game_id=0)
        self.assertIsNone(result["updated"]["parent_game_id"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT parent_game_id, content_type FROM games WHERE id = ?", (gid,)
            )
        self.assertIsNone(row["parent_game_id"])
        self.assertEqual(row["content_type"], "dlc")  # untouched by detach
        self.assertIn("parent_game_id", await self._overrides(gid))

    async def test_promotion_to_primary_still_clears_parent_regression(self):
        parent = await seed_game("Regression Parent")
        gid = await seed_game(
            "Regression Compilation",
            content_type="bundle",
            is_primary_library_item=0,
            parent_game_id=parent,
        )
        result = await platforms.update_game(game_id=gid, content_type="base_game")
        self.assertIsNone(result["updated"]["parent_game_id"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT parent_game_id, is_primary_library_item FROM games WHERE id = ?",
                (gid,),
            )
        self.assertIsNone(row["parent_game_id"])
        self.assertEqual(row["is_primary_library_item"], 1)


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
        # SteamSpy tags are canonicalized (lowercased) on write.
        self.assertIn("action", json.loads(row["tags"]))

    async def test_bulk_steam_name_sync_respects_manual_name(self):
        gid = await make_steam_game("Original", 700)
        await platforms.update_game(game_id=gid, new_name="My Title")
        await db_module.bulk_upsert_steam_library(
            [{"appid": 700, "name": "Steam Renamed", "playtime_minutes": 5}],
            synced_at=datetime.now(timezone.utc).isoformat(),
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT name FROM games WHERE id = ?", (gid,))
        self.assertEqual(row["name"], "My Title")

    async def test_bulk_steam_name_sync_renames_unprotected(self):
        gid = await make_steam_game("Original", 701)
        await db_module.bulk_upsert_steam_library(
            [{"appid": 701, "name": "Steam Renamed", "playtime_minutes": 5}],
            synced_at=datetime.now(timezone.utc).isoformat(),
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT name FROM games WHERE id = ?", (gid,))
        self.assertEqual(row["name"], "Steam Renamed")

    async def test_hltb_cache_result_respects_manual_durations(self):
        gid = await seed_game("Timed Game", hltb_main=10.0)
        await platforms.update_game(game_id=gid, hltb_main=42.0)
        # Background HLTB refresh tries to overwrite with fresh durations.
        await hltb._cache_result(gid, 5.0, 6.0, 7.0, "2026-01-01")
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT hltb_main, hltb_extra, hltb_cached_at FROM games WHERE id = ?", (gid,)
            )
        self.assertEqual(row["hltb_main"], 42.0)  # manual value preserved
        self.assertIsNone(row["hltb_extra"])  # whole HLTB group skipped
        self.assertEqual(row["hltb_cached_at"], "2026-01-01")  # cache still stamped

    async def test_clear_override_lets_sync_update_again(self):
        gid = await make_steam_game("Reclaimable", 800, tags=["orig"])
        await platforms.update_game(game_id=gid, tags=["manual"])
        # Revoke protection; value stays until the next sync.
        result = await platforms.update_game(game_id=gid, clear_overrides=["tags"])
        self.assertEqual(result["cleared"], ["tags"])
        self.assertNotIn("tags", result["manual_overrides"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT tags FROM games WHERE id = ?", (gid,))
        self.assertEqual(json.loads(row["tags"]), ["manual"])  # unchanged by clear

        with patch.object(
            steamspy, "_fetch_steamspy", AsyncMock(return_value={"Action": 99})
        ):
            await steamspy.enrich_steamspy(800)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT tags FROM games WHERE id = ?", (gid,))
        self.assertIn("action", json.loads(row["tags"]))  # sync took over again


class UpdateGameRenameReenrichTests(ToolDBTestCase):
    """A rename invalidates name-matched enrichment so it re-fetches the new title."""

    async def _seed_enriched(self, name: str) -> int:
        gid = await seed_game(name)
        gpid = await add_platform(gid, "switch2")
        await add_enrichment(gpid, metacritic_score=83, opencritic_score=83)
        # Stamp every name-matched cache/claim as already done.
        async with db_module.get_db() as db:
            await db.execute(
                """UPDATE games
                      SET igdb_cached_at = '2026-01-01', igdb_claimed_at = '2026-01-01',
                          hltb_cached_at = '2026-01-01', hltb_claimed_at = '2026-01-01'
                    WHERE id = ?""",
                (gid,),
            )
            await db.execute(
                """UPDATE game_platform_enrichment
                      SET metacritic_cached_at = '2026-01-01', metacritic_claimed_at = '2026-01-01',
                          opencritic_cached_at = '2026-01-01', opencritic_claimed_at = '2026-01-01'
                    WHERE game_platform_id = ?""",
                (gpid,),
            )
            await db.commit()
        return gid

    async def _caches(self, gid: int) -> dict:
        async with db_module.get_db() as db:
            game = await db.execute_fetchone(
                "SELECT igdb_cached_at, hltb_cached_at FROM games WHERE id = ?", (gid,)
            )
            enr = await db.execute_fetchone(
                """SELECT metacritic_cached_at, opencritic_cached_at
                     FROM game_platform_enrichment e
                     JOIN game_platforms p ON p.id = e.game_platform_id
                    WHERE p.game_id = ?""",
                (gid,),
            )
        return {**dict(game), **dict(enr)}

    async def test_rename_clears_name_matched_caches(self):
        gid = await self._seed_enriched("Xenoblade Chronicles 2")
        result = await platforms.update_game(
            game_id=gid, new_name="Xenoblade Chronicles: Definitive Edition"
        )
        self.assertEqual(
            set(result["enrichment_invalidated"]),
            {"igdb", "hltb", "opencritic", "metacritic"},
        )
        caches = await self._caches(gid)
        self.assertTrue(all(v is None for v in caches.values()), caches)

    async def test_rename_clears_stale_series_memberships(self):
        gid = await self._seed_enriched("Xenoblade Chronicles 2")
        await db_module.upsert_game_series_links(
            gid, [("collection", 5, "Old Series"), ("franchise", 6, "Old Franchise")]
        )
        await platforms.update_game(
            game_id=gid, new_name="Xenoblade Chronicles: Definitive Edition"
        )
        # Memberships are dropped so re-enrichment can repopulate cleanly; the
        # shared game_series rows themselves remain.
        async with db_module.get_db() as db:
            members = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM game_series_membership WHERE game_id = ?", (gid,)
            )
            series = await db.execute_fetchone("SELECT COUNT(*) AS c FROM game_series")
        self.assertEqual(members["c"], 0)
        self.assertEqual(series["c"], 2)

    async def test_rename_skips_hltb_when_all_durations_pinned(self):
        gid = await self._seed_enriched("Old Title")
        # Pin every HLTB duration in the same edit as the rename.
        result = await platforms.update_game(
            game_id=gid,
            new_name="New Title",
            hltb_main=10.0,
            hltb_extra=20.0,
            hltb_complete=30.0,
        )
        self.assertNotIn("hltb", result["enrichment_invalidated"])
        self.assertIn("igdb", result["enrichment_invalidated"])
        caches = await self._caches(gid)
        self.assertEqual(caches["hltb_cached_at"], "2026-01-01")  # preserved
        self.assertIsNone(caches["igdb_cached_at"])  # still re-claimed

    async def test_noop_rename_and_non_rename_edits_do_not_invalidate(self):
        gid = await self._seed_enriched("Same Title")
        # Renaming to the identical name is a no-op.
        same = await platforms.update_game(game_id=gid, new_name="Same Title")
        self.assertEqual(same["enrichment_invalidated"], [])
        # Editing other fields without renaming leaves enrichment alone.
        edited = await platforms.update_game(game_id=gid, release_date="2020-05-29")
        self.assertEqual(edited["enrichment_invalidated"], [])
        caches = await self._caches(gid)
        self.assertTrue(all(v == "2026-01-01" for v in caches.values()), caches)


class GetWishlistContentTypeTests(ToolDBTestCase):
    async def test_labels_content_type_per_item(self):
        base = await seed_game("Wishlisted Base Game")
        await db_module.upsert_wishlist_entry(base, "steam", source="steam")
        dlc = await seed_game(
            "Wishlisted DLC", content_type="dlc", is_primary_library_item=0
        )
        await db_module.upsert_wishlist_entry(dlc, "steam", source="steam")

        result = await platforms.get_wishlist("steam")

        by_name = {i["name"]: i["content_type"] for i in result["items"]}
        self.assertEqual(by_name["Wishlisted Base Game"], "base_game")
        self.assertEqual(by_name["Wishlisted DLC"], "dlc")


class SetPlaytimeTests(ToolDBTestCase):
    async def test_requires_platform(self):
        gid = await seed_game("No Platform Given")
        with self.assertRaisesRegex(ToolError, "platform is required"):
            await platforms.set_playtime(game_id=gid, playtime_minutes=10)

    async def test_requires_a_field(self):
        gid = await seed_game("Nothing To Do")
        await add_platform(gid, "steam")
        with self.assertRaisesRegex(ToolError, "Provide playtime"):
            await platforms.set_playtime(game_id=gid, platform="steam")

    async def test_rejects_negative_playtime(self):
        gid = await seed_game("Neg")
        await add_platform(gid, "steam")
        with self.assertRaisesRegex(ToolError, "must not be negative"):
            await platforms.set_playtime(game_id=gid, platform="steam", playtime_minutes=-1)

    async def test_rejects_bad_last_played(self):
        gid = await seed_game("Bad Date")
        await add_platform(gid, "steam")
        with self.assertRaisesRegex(ToolError, "last_played"):
            await platforms.set_playtime(
                game_id=gid, platform="steam", last_played="2026-13-40"
            )

    async def test_sets_and_records_override(self):
        gid = await seed_game("GOG Only")
        await add_platform(gid, "gog")
        result = await platforms.set_playtime(
            game_id=gid, platform="gog", playtime_minutes=6000, last_played="2026-07-01"
        )
        self.assertEqual(result["playtime_minutes"], 6000)
        self.assertEqual(result["last_played"], "2026-07-01")
        self.assertEqual(
            set(result["manual_overrides"]), {"playtime_minutes", "last_played"}
        )
        self.assertFalse(result["platform_row_created"])

    async def test_creates_platform_row_when_missing(self):
        gid = await seed_game("New Platform")
        result = await platforms.set_playtime(
            game_id=gid, platform="gog", playtime_minutes=120
        )
        self.assertTrue(result["platform_row_created"])
        self.assertEqual(result["playtime_minutes"], 120)

    async def test_create_platform_row_false_errors(self):
        gid = await seed_game("Strict")
        with self.assertRaisesRegex(ToolError, "no gog platform row"):
            await platforms.set_playtime(
                game_id=gid, platform="gog", playtime_minutes=1, create_platform_row=False
            )

    async def test_manual_playtime_survives_steam_sync(self):
        gid = await make_steam_game("Pinned", 900, tags=["x"])
        await platforms.set_playtime(game_id=gid, platform="steam", playtime_minutes=9999)
        # A later Steam sync reports a different playtime — the pin must hold.
        await db_module.bulk_upsert_steam_library(
            [{"appid": 900, "name": "Pinned", "playtime_minutes": 5}],
            synced_at=datetime.now(timezone.utc).isoformat(),
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM game_platforms "
                "WHERE game_id = ? AND platform = 'steam'",
                (gid,),
            )
        self.assertEqual(row["playtime_minutes"], 9999)

    async def test_manual_playtime_survives_generic_sync(self):
        gid = await seed_game("Ps Game")
        await add_platform(gid, "ps5", playtime_minutes=100)
        await platforms.set_playtime(game_id=gid, platform="ps5", playtime_minutes=7000)
        # upsert_game_platform is the shared non-Steam sync path.
        await db_module.upsert_game_platform(gid, "ps5", playtime_minutes=200, owned=1)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM game_platforms "
                "WHERE game_id = ? AND platform = 'ps5'",
                (gid,),
            )
        self.assertEqual(row["playtime_minutes"], 7000)

    async def test_clear_lets_sync_take_over(self):
        gid = await seed_game("Reclaim Playtime")
        await add_platform(gid, "ps5", playtime_minutes=100)
        await platforms.set_playtime(game_id=gid, platform="ps5", playtime_minutes=7000)
        cleared = await platforms.set_playtime(
            game_id=gid, platform="ps5", clear=["playtime_minutes"]
        )
        self.assertEqual(cleared["cleared"], ["playtime_minutes"])
        self.assertNotIn("playtime_minutes", cleared["manual_overrides"])
        # Value unchanged by the clear itself...
        self.assertEqual(cleared["playtime_minutes"], 7000)
        # ...but the next sync now overwrites it.
        await db_module.upsert_game_platform(gid, "ps5", playtime_minutes=250, owned=1)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM game_platforms "
                "WHERE game_id = ? AND platform = 'ps5'",
                (gid,),
            )
        self.assertEqual(row["playtime_minutes"], 250)

    async def test_clear_only_on_missing_row_errors(self):
        gid = await seed_game("No Row Clear")
        with self.assertRaisesRegex(ToolError, "no gog platform row to clear"):
            await platforms.set_playtime(
                game_id=gid, platform="gog", clear=["playtime_minutes"]
            )


class UpdateGameIgdbOverrideTests(ToolDBTestCase):
    async def test_sets_cover_igdb_id_and_platforms(self):
        gid = await seed_game("Match Me")
        result = await platforms.update_game(
            game_id=gid,
            cover_image_id="co1abc",
            igdb_id=4242,
            igdb_platforms=[130, 6, 6],
        )
        self.assertEqual(result["updated"]["cover_image_id"], "co1abc")
        self.assertEqual(result["updated"]["igdb_id"], 4242)
        # Deduped + sorted, decoded back to a list for display.
        self.assertEqual(result["updated"]["igdb_platforms"], [6, 130])
        self.assertEqual(
            set(result["manual_overrides"]),
            {"cover_image_id", "igdb_id", "igdb_platforms"},
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_platforms FROM games WHERE id = ?", (gid,)
            )
        self.assertEqual(json.loads(row["igdb_platforms"]), [6, 130])

    async def test_rejects_duplicate_igdb_id(self):
        first = await seed_game("First")
        await platforms.update_game(game_id=first, igdb_id=555)
        second = await seed_game("Second")
        with self.assertRaisesRegex(ToolError, "already used by game id"):
            await platforms.update_game(game_id=second, igdb_id=555)

    async def test_rejects_non_positive_igdb_id(self):
        gid = await seed_game("Zero")
        with self.assertRaisesRegex(ToolError, "positive integer"):
            await platforms.update_game(game_id=gid, igdb_id=0)

    async def test_rejects_non_int_platforms(self):
        gid = await seed_game("Bad Platforms")
        with self.assertRaisesRegex(ToolError, "list of integers"):
            await platforms.update_game(game_id=gid, igdb_platforms=[6, "x"])

    async def test_empty_cover_rejected(self):
        gid = await seed_game("Blank Cover")
        with self.assertRaisesRegex(ToolError, "cover_image_id must not be empty"):
            await platforms.update_game(game_id=gid, cover_image_id="   ")

    async def test_pinned_igdb_fields_survive_enrichment(self):
        gid = await seed_game("Wrong Match")
        await platforms.update_game(game_id=gid, igdb_id=1000, igdb_platforms=[6])
        # A later IGDB enrichment pass reports a DIFFERENT id/platforms and an
        # (unpinned) cover — the pins must hold, the cover must apply.
        fetched = igdb.IGDBGame(
            igdb_id=2000,
            name="Wrong Match",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2010-01-01",
            platforms=[48, 130],
            cover_image_id="co9zzz",
        )
        await igdb._apply_igdb_metadata(gid, fetched)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_id, igdb_platforms, cover_image_id FROM games WHERE id = ?",
                (gid,),
            )
        self.assertEqual(row["igdb_id"], 1000)  # pin held
        self.assertEqual(json.loads(row["igdb_platforms"]), [6])  # pin held
        self.assertEqual(row["cover_image_id"], "co9zzz")  # unpinned, applied
