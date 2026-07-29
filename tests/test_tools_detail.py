"""Characterization tests for gamelib_mcp.tools.detail.

Enrichment calls (Steam Store / ProtonDB / HLTB) are patched to no-ops so the
test characterizes lookup + formatting only, without network.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from conftest import (
    ToolDBTestCase,
    add_platform,
    add_rating,
    make_steam_game,
    seed_game,
)
from fastmcp.exceptions import ToolError

from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import detail
from gamelib_mcp.tools.platforms import update_game


class GetGameDetailTests(ToolDBTestCase):
    def setUp(self):
        super().setUp()
        self._patchers = [
            patch.object(detail, "enrich_game", AsyncMock(return_value=None)),
            patch.object(detail, "get_protondb", AsyncMock(return_value=None)),
            patch.object(detail, "get_hltb", AsyncMock(return_value=None)),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        super().tearDown()

    async def test_requires_an_identifier(self):
        with self.assertRaisesRegex(ToolError, "Provide game_id, name, or appid"):
            await detail.get_game_detail()

    async def test_not_found_returns_error(self):
        with self.assertRaisesRegex(ToolError, "Game not found in library"):
            await detail.get_game_detail(name="does-not-exist")

    async def test_lookup_by_game_id_shape(self):
        gid = await make_steam_game(
            "Celeste",
            504230,
            playtime_minutes=300,
            tags=["platformer"],
            genres=["Indie"],
            metacritic_score=92,
        )
        result = await detail.get_game_detail(game_id=gid)
        self.assertEqual(
            set(result),
            {
                "game_id",
                "appid",
                "steam_appid",
                "name",
                "cover_url",
                "release_date",
                "series",
                "platforms",
                "playtime_hours",
                "playtime_2weeks_hours",
                "last_played_date",
                "is_farmed",
                "completion_status",
                "content_type",
                "parent_game_id",
                "is_primary_library_item",
                "related_content",
                "genres",
                "tags",
                "features",
                "short_description",
                "steam_review_score",
                "steam_review_desc",
                "metacritic_score",
                "metacritic_url",
                "opencritic_score",
                "opencritic_tier",
                "opencritic_percent_rec",
                "opencritic_url",
                "hltb_main",
                "hltb_extra",
                "hltb_complete",
                "protondb_tier",
                "manual_overrides",
                "play_state",
                "owned",
                "wishlisted",
            },
        )
        self.assertEqual(result["name"], "Celeste")
        self.assertEqual(result["appid"], 504230)
        self.assertEqual(result["playtime_hours"], 5.0)
        self.assertEqual(result["play_state"], "played")
        self.assertEqual(result["tags"], ["platformer"])
        self.assertEqual(result["genres"], ["Indie"])
        self.assertEqual(result["content_type"], "base_game")
        self.assertIsNone(result["parent_game_id"])
        self.assertIs(result["is_primary_library_item"], True)
        self.assertIs(result["owned"], True)
        self.assertIs(result["wishlisted"], False)
        self.assertEqual(
            result["related_content"],
            {"dlc": [], "expansions": [], "editions": [], "bundles": [], "other": []},
        )
        self.assertNotIn("my_rating", result)

    async def test_lookup_by_appid(self):
        await make_steam_game("Hollow Knight", 367520, playtime_minutes=120)
        result = await detail.get_game_detail(appid=367520)
        self.assertEqual(result["name"], "Hollow Knight")

    async def test_lookup_by_name_partial(self):
        await make_steam_game("Hollow Knight", 367520, playtime_minutes=120)
        result = await detail.get_game_detail(name="hollow")
        self.assertEqual(result["name"], "Hollow Knight")

    async def test_lookup_by_name_across_punctuation(self):
        await make_steam_game("Sekiro: Shadows Die Twice", 814380, playtime_minutes=344)
        result = await detail.get_game_detail(name="sekiro shadow")
        self.assertEqual(result["name"], "Sekiro: Shadows Die Twice")

    async def test_lookup_by_name_prefers_exact_match_over_longer_titles(self):
        await make_steam_game("Hades II", 1145350, playtime_minutes=600)
        await make_steam_game("Hades", 1145360, playtime_minutes=10)
        result = await detail.get_game_detail(name="hades")
        self.assertEqual(result["name"], "Hades")

    async def test_lookup_by_name_falls_back_to_fuzzy(self):
        await make_steam_game("Sekiro: Shadows Die Twice", 814380, playtime_minutes=344)
        result = await detail.get_game_detail(name="sekrio shadows die twice")
        self.assertEqual(result["name"], "Sekiro: Shadows Die Twice")

    async def test_hoists_best_opencritic_fields_from_platforms(self):
        from conftest import add_enrichment, add_platform

        gid = await make_steam_game("Sekiro: Shadows Die Twice", 814380)
        # Second platform with a higher OpenCritic score must win the hoist.
        ps5_gpid = await add_platform(gid, "ps5")
        await add_enrichment(
            ps5_gpid,
            opencritic_score=91,
            opencritic_tier="Mighty",
            opencritic_percent_rec=95.4,
            opencritic_url="https://opencritic.com/game/6630/sekiro-shadows-die-twice",
            metacritic_score=88,
        )
        result = await detail.get_game_detail(game_id=gid)
        self.assertEqual(result["opencritic_score"], 91)
        self.assertEqual(result["opencritic_tier"], "Mighty")
        self.assertEqual(result["metacritic_score"], 88)
        self.assertEqual(
            result["opencritic_url"],
            "https://opencritic.com/game/6630/sekiro-shadows-die-twice",
        )

    async def test_includes_rating_when_present(self):
        gid = await seed_game("Disco Elysium")
        await add_rating(gid, "backloggd", raw_score=5.0, normalized_score=10.0, review_text="GOAT")
        result = await detail.get_game_detail(game_id=gid)
        self.assertEqual(
            result["my_rating"],
            {
                "source": "backloggd",
                "raw_score": 5.0,
                "normalized_score": 10.0,
                "review_text": "GOAT",
            },
        )

    async def test_includes_related_content_grouped_by_type(self):
        parent_id = await seed_game("Fallout: New Vegas")
        await add_platform(parent_id, "steam", playtime_minutes=2694)
        dlc_id = await seed_game(
            "Fallout New Vegas: Dead Money",
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        expansion_id = await seed_game(
            "Fallout New Vegas: Old World Blues",
            content_type="expansion",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        await add_platform(dlc_id, "epic")
        await add_platform(expansion_id, "epic")

        result = await detail.get_game_detail(game_id=parent_id)

        self.assertEqual(
            [entry["name"] for entry in result["related_content"]["dlc"]],
            ["Fallout New Vegas: Dead Money"],
        )
        self.assertEqual(
            [entry["name"] for entry in result["related_content"]["expansions"]],
            ["Fallout New Vegas: Old World Blues"],
        )
        self.assertEqual(result["related_content"]["editions"], [])
        self.assertEqual(result["related_content"]["bundles"], [])

    async def test_unknown_playtime_game_reports_null_hours(self):
        gid = await seed_game("Manual")
        await add_platform(gid, "gog")  # no playtime -> NULL
        result = await detail.get_game_detail(game_id=gid)
        self.assertEqual(result["play_state"], "unknown")
        self.assertIsNone(result["playtime_hours"])

    async def test_completed_status_overrides_unknown_play_state(self):
        gid = await seed_game("Manual Completed")
        await add_platform(gid, "gog")  # no playtime -> NULL
        await update_game(game_id=gid, completion_status="completed")
        result = await detail.get_game_detail(game_id=gid)
        self.assertEqual(result["play_state"], "played")
        self.assertEqual(result["completion_status"], "completed")

    async def test_completion_status_defaults_to_none(self):
        gid = await make_steam_game("Untouched", 1, playtime_minutes=0)
        result = await detail.get_game_detail(game_id=gid)
        self.assertIsNone(result["completion_status"])

    async def test_nested_row_with_parent_includes_parent_object(self):
        parent_id = await seed_game("Fallout: New Vegas")
        dlc_id = await seed_game(
            "Fallout New Vegas: Dead Money",
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        await add_platform(dlc_id, "steam")

        result = await detail.get_game_detail(game_id=dlc_id)

        self.assertEqual(
            result["parent"], {"game_id": parent_id, "name": "Fallout: New Vegas"}
        )

    async def test_primary_row_has_no_parent_key(self):
        gid = await make_steam_game("Celeste", 504230)
        result = await detail.get_game_detail(game_id=gid)
        self.assertNotIn("parent", result)

    async def test_nested_row_without_parent_has_no_parent_key(self):
        gid = await seed_game(
            "Orphan DLC", content_type="dlc", is_primary_library_item=0
        )
        await add_platform(gid, "steam")

        result = await detail.get_game_detail(game_id=gid)

        self.assertNotIn("parent", result)

    async def test_dlc_ownership_present_for_base_game_with_catalog(self):
        gid = await make_steam_game("Base Game", 1000, playtime_minutes=60)
        await db_module.set_meta(
            "steam_dlc_catalog:1000",
            json.dumps({"appids": [10, 20, 30], "fetched_at": "2024-01-01T00:00:00+00:00"}),
        )
        owned_steam_child = await seed_game(
            "Base Game: DLC1",
            content_type="dlc",
            parent_game_id=gid,
            is_primary_library_item=0,
        )
        await add_platform(owned_steam_child, "steam", owned=1)
        # An owned child on a different platform than the base game still
        # counts toward `owned` — ownership is ownership regardless of where
        # Steam's own catalog was fetched from.
        owned_switch_child = await seed_game(
            "Base Game: DLC2",
            content_type="dlc",
            parent_game_id=gid,
            is_primary_library_item=0,
        )
        await add_platform(owned_switch_child, "switch2", owned=1)
        unowned_child = await seed_game(
            "Base Game: DLC3",
            content_type="dlc",
            parent_game_id=gid,
            is_primary_library_item=0,
        )
        await add_platform(unowned_child, "steam", owned=0)

        result = await detail.get_game_detail(game_id=gid)

        self.assertEqual(
            result["dlc_ownership"], {"owned": 2, "known": 3, "source": "steam"}
        )

    async def test_dlc_ownership_absent_without_catalog(self):
        gid = await make_steam_game("No Catalog Game", 2000)
        result = await detail.get_game_detail(game_id=gid)
        self.assertNotIn("dlc_ownership", result)

    async def test_dlc_ownership_absent_on_malformed_meta(self):
        gid = await make_steam_game("Malformed Meta Game", 3000)
        await db_module.set_meta("steam_dlc_catalog:3000", "not valid json")

        result = await detail.get_game_detail(game_id=gid)

        self.assertNotIn("dlc_ownership", result)

    async def test_dlc_ownership_falls_back_to_igdb_catalog_without_steam(self):
        # Switch-only base game: no Steam appid, so no steam_dlc_catalog key
        # was ever written. igdb_children:{igdb_id} is seeded directly (as
        # get_igdb_children_cached would leave it after a live fetch) so this
        # exercises the full cache-hit path without any network involved.
        gid = await seed_game("Switch Base Game")
        await add_platform(gid, "switch2", owned=1)
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = ? WHERE id = ?", (777, gid))
            await db.commit()
        now = datetime.now(UTC).isoformat()
        await db_module.set_meta(
            "igdb_children:777",
            json.dumps(
                {
                    "fetched_at": now,
                    "children": [
                        {"igdb_id": 1, "name": "DLC One", "kind": "dlc"},
                        {"igdb_id": 2, "name": "DLC Two", "kind": "dlc"},
                        {"igdb_id": 3, "name": "Expansion One", "kind": "expansion"},
                    ],
                }
            ),
        )
        owned_child = await seed_game(
            "Switch Base Game: DLC One",
            content_type="dlc",
            parent_game_id=gid,
            is_primary_library_item=0,
        )
        await add_platform(owned_child, "switch2", owned=1)
        unowned_child = await seed_game(
            "Switch Base Game: DLC Two",
            content_type="dlc",
            parent_game_id=gid,
            is_primary_library_item=0,
        )
        await add_platform(unowned_child, "switch2", owned=0)

        result = await detail.get_game_detail(game_id=gid)

        self.assertEqual(
            result["dlc_ownership"], {"owned": 1, "known": 3, "source": "igdb"}
        )

    async def test_dlc_ownership_prefers_steam_catalog_over_igdb(self):
        gid = await make_steam_game("Steam Wins Game", 4000, playtime_minutes=10)
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = ? WHERE id = ?", (888, gid))
            await db.commit()
        await db_module.set_meta(
            "steam_dlc_catalog:4000",
            json.dumps({"appids": [10], "fetched_at": "2024-01-01T00:00:00+00:00"}),
        )
        now = datetime.now(UTC).isoformat()
        await db_module.set_meta(
            "igdb_children:888",
            json.dumps(
                {
                    "fetched_at": now,
                    "children": [{"igdb_id": 1, "name": "DLC One", "kind": "dlc"}],
                }
            ),
        )

        result = await detail.get_game_detail(game_id=gid)

        self.assertEqual(
            result["dlc_ownership"], {"owned": 0, "known": 1, "source": "steam"}
        )

    async def test_dlc_ownership_absent_without_igdb_id_or_steam_catalog(self):
        gid = await seed_game("No Igdb Id No Catalog")
        await add_platform(gid, "switch2", owned=1)

        result = await detail.get_game_detail(game_id=gid)

        self.assertNotIn("dlc_ownership", result)

    async def test_wishlist_only_game_reports_owned_false(self):
        # prod: Persona 3 Reload, wishlist-only, no game_platforms row at all.
        # is_primary_library_item is a content-type flag (game vs DLC), not
        # ownership — the tool must expose owned/wishlisted explicitly rather
        # than looking like an owned game with an empty platforms list.
        gid = await seed_game("Persona 3 Reload")
        await db_module.upsert_wishlist_entry(gid, "switch2", source="dekudeals")

        result = await detail.get_game_detail(game_id=gid)

        self.assertIs(result["owned"], False)
        self.assertIs(result["wishlisted"], True)
        self.assertEqual(result["platforms"], [])
        self.assertIs(result["is_primary_library_item"], True)


class GetGameDetailsBatchTests(ToolDBTestCase):
    async def test_batch_never_triggers_lazy_enrichment(self):
        gid = await make_steam_game("Celeste", 504230, playtime_minutes=300)
        enrich = AsyncMock(return_value=None)
        protondb = AsyncMock(return_value=None)
        hltb = AsyncMock(return_value=None)
        with (
            patch.object(detail, "enrich_game", enrich),
            patch.object(detail, "get_protondb", protondb),
            patch.object(detail, "get_hltb", hltb),
        ):
            result = await detail.get_game_details_batch([{"game_id": gid}])
        enrich.assert_not_awaited()
        protondb.assert_not_awaited()
        hltb.assert_not_awaited()
        self.assertEqual(result["enrichment"], "skipped")
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["results"][0]["name"], "Celeste")

    async def test_mixed_resolution_preserves_order_and_isolates_errors(self):
        a = await make_steam_game("Celeste", 504230)
        b = await make_steam_game("Hades", 1145360)
        result = await detail.get_game_details_batch(
            [
                {"name": "celeste"},
                {"name": "does-not-exist-at-all"},
                {"appid": 1145360},
                {"bogus_key": 1},
            ]
        )
        statuses = [r["status"] for r in result["results"]]
        self.assertEqual(statuses, ["ok", "error", "ok", "error"])
        self.assertEqual(result["ok"], 2)
        self.assertEqual(result["errors"], 2)
        self.assertEqual(result["results"][0]["game_id"], a)
        self.assertEqual(result["results"][2]["game_id"], b)
        self.assertIn("item", result["results"][1])
        self.assertIn("bogus_key", result["results"][3]["error"])

    async def test_batch_serves_cached_dlc_ownership_without_fetching(self):
        # Regression: a warm IGDB children cache must still feed dlc_ownership
        # in batch mode — only the cache-miss live fetch is skipped.
        from gamelib_mcp.data import igdb as igdb_module

        warm = await make_steam_game("Warm Cache Game", 111)
        cold = await make_steam_game("Cold Cache Game", 222)
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = 5001 WHERE id = ?", (warm,))
            await db.execute("UPDATE games SET igdb_id = 5002 WHERE id = ?", (cold,))
            await db.commit()
        await db_module.set_meta(
            "igdb_children:5001",
            json.dumps({
                "fetched_at": datetime.now(UTC).isoformat(),
                "children": [{"id": 1}, {"id": 2}, {"id": 3}],
            }),
        )

        network = AsyncMock(side_effect=AssertionError("network call in batch"))
        with (
            patch.object(detail, "enrich_game", network),
            patch.object(detail, "get_protondb", network),
            patch.object(detail, "get_hltb", network),
            patch.object(igdb_module, "fetch_igdb_children", network),
        ):
            result = await detail.get_game_details_batch(
                [{"game_id": warm}, {"game_id": cold}]
            )
        network.assert_not_awaited()
        self.assertEqual([r["status"] for r in result["results"]], ["ok", "ok"])
        self.assertEqual(
            result["results"][0]["dlc_ownership"],
            {"owned": 0, "known": 3, "source": "igdb"},
        )
        self.assertNotIn("dlc_ownership", result["results"][1])

    async def test_empty_and_cap_raise(self):
        with self.assertRaisesRegex(ToolError, "must not be empty"):
            await detail.get_game_details_batch([])
        with self.assertRaisesRegex(ToolError, "capped at 50"):
            await detail.get_game_details_batch([{"game_id": 1}] * 51)
