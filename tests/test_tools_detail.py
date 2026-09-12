"""Characterization tests for gamelib_mcp.tools.detail.

Enrichment calls (Steam Store / ProtonDB / HLTB) are patched to no-ops so the
test characterizes lookup + formatting only, without network.
"""

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from conftest import (
    DEADLOCK_TIMEOUT,
    ToolDBTestCase,
    add_platform,
    add_rating,
    make_steam_game,
    seed_game,
)
from fastmcp.exceptions import ToolError

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.title_normalization import normalize_search_text
from gamelib_mcp.tools import detail, game_media
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
                # Present because this row has no igdb_id and IGDB is
                # unconfigured in the test environment (see
                # GetGameDetailEnrichmentReportTests below).
                "enrichment",
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


class GetGameDetailEnrichmentReportTests(ToolDBTestCase):
    """`enrichment`: why a provider produced nothing, structurally.

    Only present when something WAS skipped, and only in single mode (the bulk
    path reports its own enrichment="skipped"). Every provider is patched here
    — the point is which ones get CALLED, not what they return.
    """

    def setUp(self):
        super().setUp()
        self.enrich_game = AsyncMock(return_value=None)
        self.get_protondb = AsyncMock(return_value=None)
        self.backfill = AsyncMock(return_value=1)
        self._patchers = [
            patch.object(detail, "enrich_game", self.enrich_game),
            patch.object(detail, "get_protondb", self.get_protondb),
            patch.object(detail, "get_hltb", AsyncMock(return_value=None)),
            patch.object(detail, "backfill_missing_games", self.backfill),
            patch.object(
                detail, "igdb_credentials_configured", lambda: False
            ),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        super().tearDown()

    async def _link(self, game_id: int, igdb_id: int) -> None:
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET igdb_id = ?, igdb_cached_at = ? WHERE id = ?",
                (igdb_id, datetime.now(UTC).isoformat(), game_id),
            )
            await db.commit()

    async def test_absent_for_an_owned_linked_steam_game(self):
        gid = await make_steam_game("Hollow Knight", 367520)
        await self._link(gid, 3227)

        result = await detail.get_game_detail(game_id=gid)

        self.assertNotIn("enrichment", result)
        self.enrich_game.assert_awaited_once_with(367520)
        self.get_protondb.assert_awaited_once_with(367520)

    async def test_a_name_only_row_has_no_appid_for_either_steam_provider(self):
        gid = await seed_game("Nothing But A Name")
        await self._link(gid, 999)

        result = await detail.get_game_detail(game_id=gid)

        self.assertEqual(
            result["enrichment"],
            {"steam_store": "no_steam_appid", "protondb": "no_steam_appid"},
        )
        self.enrich_game.assert_not_awaited()
        self.get_protondb.assert_not_awaited()

    async def test_a_checked_but_unmatched_row_reports_no_match_without_fetching(self):
        gid = await make_steam_game("Obscure Thing", 424242)
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET igdb_cached_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), gid),
            )
            await db.commit()

        result = await detail.get_game_detail(game_id=gid)

        self.assertEqual(result["enrichment"], {"igdb": "no_match"})
        self.backfill.assert_not_awaited()

    async def test_an_unlinked_row_runs_the_backfill_scoped_to_itself(self):
        gid = await make_steam_game("Never Linked", 515151)

        async def _link_it(limit, *, game_ids):
            await self._link(game_ids[0], 4242)
            return 1

        self.backfill.side_effect = _link_it
        with patch.object(detail, "igdb_credentials_configured", lambda: True):
            result = await detail.get_game_detail(game_id=gid)

        self.backfill.assert_awaited_once_with(limit=1, game_ids=[gid])
        self.assertNotIn("enrichment", result)

    async def test_a_link_that_resolves_nothing_reports_unresolved(self):
        gid = await make_steam_game("Not In IGDB", 525252)
        with patch.object(detail, "igdb_credentials_configured", lambda: True):
            result = await detail.get_game_detail(game_id=gid)

        self.assertEqual(result["enrichment"], {"igdb": "unresolved"})

    async def test_a_slow_link_reports_link_pending_and_keeps_running(self):
        gid = await make_steam_game("Slow Link", 535353)
        started = asyncio.Event()
        never = asyncio.Event()

        async def _hang(limit, *, game_ids):
            started.set()
            # An event nobody sets — a timed sleep would measure machine load.
            await never.wait()
            return 0

        self.backfill.side_effect = _hang
        with (
            patch.object(detail, "igdb_credentials_configured", lambda: True),
            patch.object(detail, "DETAIL_IGDB_LINK_TIMEOUT_SECONDS", 0.05),
        ):
            result = await detail.get_game_detail(game_id=gid)

        self.assertEqual(result["enrichment"], {"igdb": "link_pending"})
        # Shielded, so the fetch is still in flight rather than cancelled.
        await asyncio.wait_for(started.wait(), timeout=DEADLOCK_TIMEOUT)
        (task,) = [t for t in detail._PENDING_IGDB_LINKS]
        self.assertFalse(task.done())
        never.set()
        await asyncio.wait_for(task, timeout=DEADLOCK_TIMEOUT)

    async def test_a_link_that_fails_after_the_wait_is_logged_not_swallowed(self):
        # Once the caller has moved on with "link_pending", nobody awaits the
        # shielded task; a failure then must still reach the log rather than
        # asyncio's never-retrieved handler (a background task dying silently).
        gid = await make_steam_game("Late Failure", 545454)
        release = asyncio.Event()

        async def _fail_late(limit, *, game_ids):
            await release.wait()
            raise RuntimeError("database is locked")

        self.backfill.side_effect = _fail_late
        with (
            patch.object(detail, "igdb_credentials_configured", lambda: True),
            patch.object(detail, "DETAIL_IGDB_LINK_TIMEOUT_SECONDS", 0.05),
        ):
            result = await detail.get_game_detail(game_id=gid)
        self.assertEqual(result["enrichment"], {"igdb": "link_pending"})

        (task,) = [t for t in detail._PENDING_IGDB_LINKS]
        with self.assertLogs(detail.logger, level="WARNING") as logs:
            release.set()
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(task, timeout=DEADLOCK_TIMEOUT)
            # Done-callbacks run on the loop after the task settles.
            await asyncio.sleep(0)
        self.assertTrue(
            any("Backgrounded IGDB link task failed" in line for line in logs.output)
        )
        self.assertTrue(any("database is locked" in line for line in logs.output))
        self.assertNotIn(task, detail._PENDING_IGDB_LINKS)


_MEDIA_PAYLOAD = {
    "media": {
        "source": "steam",
        "trailer": {
            "kind": "mp4",
            "url": "https://cdn.cloudflare.steamstatic.com/steam/apps/1/movie480.mp4",
            "hq_url": "https://cdn.cloudflare.steamstatic.com/steam/apps/1/movie_max.mp4",
            "poster": "https://shared.akamai.steamstatic.com/poster.jpg",
            "name": "Trailer",
        },
        "screenshots": [{"thumb": "t1", "full": "f1"}],
        "screenshot_count": 1,
        "screenshots_truncated": False,
        "short_description": "A tiny bug with a nail.",
    },
    "igdb_id": None,
}


class GetGameDetailMediaTests(ToolDBTestCase):
    """get_game_detail(media=True): the neutral game representation.

    The one provider call is patched at tools/game_media.py's own binding —
    the seam both this path and record_assessment's package resolve through.
    media=False needs no patch at all, which is the point: the default costs
    nothing and reaches nothing. The `similar` row is NOT behind that seam: it
    is the library's own tag similarity (`similar_in_library`), so the tests
    below seed real tagged games rather than mocking a provider answer.
    """

    def _media(self, payload=_MEDIA_PAYLOAD, **kwargs):
        return patch(
            "gamelib_mcp.tools.game_media.get_game_media",
            AsyncMock(return_value=payload, **kwargs),
        )

    async def _owned(
        self,
        name: str,
        tags: list[str],
        *,
        playtime: int | None = 0,
        platform: str = "steam",
    ) -> int:
        """An owned, primary, tagged library row — one member of the pool."""
        game_id = await seed_game(name, tags=tags)
        await add_platform(game_id, platform, playtime_minutes=playtime)
        return game_id

    async def test_media_off_by_default_adds_no_keys_and_fetches_nothing(self):
        gid = await make_steam_game("Hollow Knight", 367520, playtime_minutes=120)

        result = await detail.get_game_detail(game_id=gid)

        self.assertNotIn("media", result)
        self.assertNotIn("similar", result)

    async def test_media_true_adds_the_media_block(self):
        gid = await make_steam_game("Hollow Knight", 367520, playtime_minutes=120)

        with self._media():
            result = await detail.get_game_detail(game_id=gid, media=True)

        self.assertEqual(result["media"], _MEDIA_PAYLOAD["media"])
        # An untagged game has no neighbours to name, so that key stays absent
        # rather than null.
        self.assertNotIn("similar", result)

    async def test_ranking_prefers_rare_shared_tags_over_ubiquitous_ones(self):
        # IDF is the whole point: agreeing on "metroidvania" says far more than
        # agreeing on "action"/"indie"/"adventure", which half the library
        # carries. The fillers below are what MAKES those three ubiquitous —
        # without them every tag is equally rare and the ranking is arbitrary.
        gid = await self._owned(
            "Similarity Probe",
            ["action", "indie", "adventure",
             "metroidvania", "souls-like", "hand-drawn", "atmospheric"],
        )
        rare = await self._owned(
            "Rare Neighbour",
            ["metroidvania", "souls-like", "hand-drawn", "atmospheric", "action"],
        )
        common = await self._owned("Common Neighbour", ["action", "indie", "adventure"])
        for index in range(6):
            await self._owned(f"Filler {index}", ["action", "indie", "adventure", "casual"])

        block = await game_media.similar_in_library(gid)

        assert block is not None
        names = [item["name"] for item in block["items"]]
        self.assertEqual(names[0], "Rare Neighbour")
        self.assertLess(names.index("Rare Neighbour"), names.index("Common Neighbour"))
        top, = [i for i in block["items"] if i["game_id"] == rare]
        self.assertGreater(top["similarity"], 0)
        self.assertLessEqual(top["similarity"], 1)
        # Four tags shared, three shown: the "why" is a reason, not a dump.
        self.assertEqual(len(top["shared_tags"]), game_media.SIMILAR_WHY_CAP)
        self.assertLessEqual(
            set(top["shared_tags"]),
            {"metroidvania", "souls-like", "hand-drawn", "atmospheric", "action"},
        )
        other, = [i for i in block["items"] if i["game_id"] == common]
        self.assertEqual(
            sorted(other["shared_tags"]), ["action", "adventure", "indie"]
        )
        self.assertGreater(top["similarity"], other["similarity"])

    async def test_three_generic_shared_tags_do_not_clear_the_similarity_floor(self):
        # The shared-tag gate alone qualifies half the real library (1853
        # neighbours for Hollow Knight), which makes the card's own count
        # meaningless. A game whose tag set is mostly about something ELSE is
        # not a neighbour just because both are tagged action/indie/adventure.
        gid = await self._owned(
            "Similarity Probe",
            ["action", "indie", "adventure", "metroidvania", "souls-like"],
        )
        twin = await self._owned(
            "Near Twin", ["action", "indie", "adventure", "metroidvania", "souls-like"]
        )
        diluted = await self._owned(
            "Diluted Neighbour",
            ["shooter", "multiplayer", "fps", "competitive", "military", "racing",
             "action", "indie", "adventure"],
        )
        for index in range(6):
            await self._owned(f"Filler {index}", ["action", "indie", "adventure", "casual"])

        block = await game_media.similar_in_library(gid)

        assert block is not None
        ids = [item["game_id"] for item in block["items"]]
        self.assertIn(twin, ids)
        self.assertNotIn(diluted, ids)
        # The floor lands before the count, so the card never claims a
        # denominator it would not stand behind.
        self.assertEqual(block["count"], len(ids))
        self.assertTrue(
            all(item["similarity"] >= game_media.SIMILAR_MIN_SIMILARITY
                for item in block["items"])
        )

    async def test_two_shared_tags_do_not_qualify(self):
        gid = await self._owned(
            "Similarity Probe", ["metroidvania", "souls-like", "hand-drawn", "action"]
        )
        await self._owned("Two Tags Only", ["metroidvania", "souls-like", "racing"])
        await self._owned("Three Tags", ["metroidvania", "souls-like", "action", "rpg"])

        block = await game_media.similar_in_library(gid)

        assert block is not None
        self.assertEqual([item["name"] for item in block["items"]], ["Three Tags"])
        self.assertEqual(block["count"], 1)
        self.assertFalse(block["truncated"])

    async def test_the_same_game_twice_is_never_a_neighbour(self):
        tags = ["metroidvania", "souls-like", "hand-drawn", "atmospheric"]
        gid = await self._owned("Similarity Probe", tags)
        keeper = await self._owned("Genuine Neighbour", tags)

        # A duplicate row of the SOURCE (a collapse that never happened): the
        # same name, so it scores ~1.0 and would lead the row.
        async with db_module.get_db() as db:
            cursor = await db.execute(
                "INSERT INTO games (name, name_normalized, tags) VALUES (?, ?, ?)",
                ("Similarity Probe", normalize_search_text("Similarity Probe"),
                 json.dumps(tags)),
            )
            duplicate = cursor.lastrowid
            await db.commit()
        await add_platform(duplicate, "gog", playtime_minutes=10)

        parent = await self._owned("Parent Edition", tags)
        child = await self._owned("Source DLC", tags)
        dlc = await self._owned("Someone Else DLC", tags)
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET parent_game_id = ? WHERE id = ?", (parent, gid)
            )
            await db.execute(
                "UPDATE games SET parent_game_id = ? WHERE id = ?", (gid, child)
            )
            await db.execute(
                "UPDATE games SET is_primary_library_item = 0 WHERE id = ?", (dlc,)
            )
            await db.commit()

        # Wishlist-only: a games row with tags and no platform row at all.
        wishlisted = await seed_game("Wishlist Only", tags=tags)
        async with db_module.get_db() as db:
            await db.execute(
                """INSERT INTO game_wishlist (game_id, platform, source, wishlisted_at)
                   VALUES (?, ?, ?, ?)""",
                (wishlisted, "steam", "manual", datetime.now(UTC).isoformat()),
            )
            await db.commit()
        # An owned=0 stub: a platform row that is not ownership.
        stub = await seed_game("Unowned Stub", tags=tags)
        await add_platform(stub, "steam", playtime_minutes=0, owned=0)

        block = await game_media.similar_in_library(gid)

        assert block is not None
        self.assertEqual([item["game_id"] for item in block["items"]], [keeper])
        self.assertEqual(block["count"], 1)

    async def test_the_row_is_capped_at_eight_with_the_true_total(self):
        tags = ["metroidvania", "souls-like", "hand-drawn", "atmospheric"]
        gid = await self._owned("Similarity Probe", tags)
        for index in range(12):
            await self._owned(f"Neighbour {index:02d}", tags)

        block = await game_media.similar_in_library(gid)

        assert block is not None
        self.assertEqual(len(block["items"]), game_media.SIMILAR_ITEM_CAP)
        self.assertEqual(block["count"], 12)
        self.assertTrue(block["truncated"])

    async def test_items_carry_ownership_rating_playtime_and_cover(self):
        tags = ["metroidvania", "souls-like", "hand-drawn"]
        gid = await self._owned("Similarity Probe", tags)
        unplayed = await self._owned("Owned Unplayed", tags, playtime=0)
        unknown = await self._owned("Unknown Playtime", tags, platform="gog",
                                    playtime=None)
        rated = await self._owned("Rated Neighbour", tags, playtime=600)
        await add_rating(rated, "backloggd", 7.0, 7.0)
        await add_rating(rated, "manual", 9.0, 9.0)
        capsule = await make_steam_game("Capsule Cover", 4242, playtime_minutes=30,
                                        tags=tags)
        slug = await make_steam_game("Slug Cover", 4343, playtime_minutes=30, tags=tags)
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET cover_image_id = 'co1' WHERE id = ?", (slug,)
            )
            await db.commit()

        block = await game_media.similar_in_library(gid)

        assert block is not None
        by_id = {item["game_id"]: item for item in block["items"]}
        self.assertTrue(all(item["owned"] for item in block["items"]))
        self.assertTrue(by_id[unplayed]["unplayed"])
        # NULL playtime (GOG, manual adds) is UNKNOWN, not an authoritative
        # zero — the three-state convention: only a known 0 earns "unplayed".
        self.assertFalse(by_id[unknown]["unplayed"])
        self.assertIsNone(by_id[unknown]["playtime_hours"])
        self.assertEqual(by_id[rated]["my_rating"], 9.0)
        self.assertEqual(by_id[rated]["playtime_hours"], 10.0)
        self.assertFalse(by_id[rated]["unplayed"])
        self.assertEqual(
            by_id[slug]["cover_url"],
            "https://images.igdb.com/igdb/image/upload/t_cover_big/co1.jpg",
        )
        self.assertEqual(
            by_id[capsule]["cover_url"],
            "https://cdn.cloudflare.steamstatic.com/steam/apps/4242/library_600x900.jpg",
        )

    async def test_tags_past_the_prominence_window_never_count(self):
        # games.tags is vote-ranked, and past the head of that list the tags
        # describe the store page rather than the game — on BOTH sides.
        window = game_media.SIMILAR_TAG_WINDOW
        source_tags = [f"src{index:02d}" for index in range(window)] + ["outside"]
        gid = await self._owned("Similarity Probe", source_tags)
        await self._owned("Control", ["src00", "src01", "src02"])
        # Shares three tags, but one of them is the source's out-of-window tag.
        await self._owned("Source Side", ["outside", "src00", "src01"])
        # Shares three tags, all of them past ITS OWN window.
        await self._owned(
            "Candidate Side",
            [f"pad{index:02d}" for index in range(window)] + ["src00", "src01", "src02"],
        )

        block = await game_media.similar_in_library(gid)

        assert block is not None
        self.assertEqual([item["name"] for item in block["items"]], ["Control"])

    async def test_too_little_tag_evidence_is_none_not_a_guess(self):
        tags = ["metroidvania", "souls-like"]
        gid = await self._owned("Thin Evidence", tags)
        await self._owned("Would-Be Neighbour", [*tags, "hand-drawn"])

        self.assertIsNone(await game_media.similar_in_library(gid))

        with self._media(None):
            result = await detail.get_game_detail(game_id=gid, media=True)
        self.assertNotIn("similar", result)

    async def test_detail_serves_the_similar_row_when_the_provider_fails(self):
        tags = ["metroidvania", "souls-like", "hand-drawn"]
        gid = await self._owned("Similarity Probe", tags)
        neighbour = await self._owned("Genuine Neighbour", tags)

        with self._media(None, side_effect=RuntimeError("provider down")):
            result = await detail.get_game_detail(game_id=gid, media=True)

        self.assertNotIn("media", result)
        self.assertEqual([i["game_id"] for i in result["similar"]["items"]], [neighbour])

    async def test_detail_serves_the_similar_row_when_the_provider_hangs(self):
        # An event nothing ever sets, so the only thing that ends the fetch is
        # the wait_for budget — no wall-clock sleep, no liveness guess. The
        # similar row is a DB read outside that budget and must survive it.
        tags = ["metroidvania", "souls-like", "hand-drawn"]
        gid = await self._owned("Similarity Probe", tags)
        neighbour = await self._owned("Genuine Neighbour", tags)
        never = asyncio.Event()

        async def hang(**kwargs):
            await never.wait()

        with (
            patch.object(detail, "DETAIL_MEDIA_TIMEOUT_SECONDS", 0.05),
            patch("gamelib_mcp.tools.game_media.get_game_media", hang),
        ):
            result = await asyncio.wait_for(
                detail.get_game_detail(game_id=gid, media=True), DEADLOCK_TIMEOUT
            )

        self.assertNotIn("media", result)
        self.assertEqual([i["game_id"] for i in result["similar"]["items"]], [neighbour])

    async def test_identity_passed_is_the_appid_igdb_id_and_name(self):
        gid = await make_steam_game("Identity Order", 4242)
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = 777 WHERE id = ?", (gid,))
            await db.commit()

        with self._media(None) as fetch:
            await detail.get_game_detail(game_id=gid, media=True)

        self.assertEqual(
            fetch.await_args.kwargs,
            {"steam_appid": 4242, "igdb_id": 777, "name": "Identity Order"},
        )

    async def test_nothing_resolved_leaves_both_keys_absent(self):
        gid = await seed_game("No Media Anywhere")

        with self._media(None):
            result = await detail.get_game_detail(game_id=gid, media=True)

        self.assertNotIn("media", result)
        self.assertNotIn("similar", result)
        self.assertEqual(result["name"], "No Media Anywhere")

    async def test_a_fetch_failure_costs_the_keys_not_the_call(self):
        gid = await seed_game("Media Outage")

        with self._media(None, side_effect=RuntimeError("provider down")):
            result = await detail.get_game_detail(game_id=gid, media=True)

        self.assertNotIn("media", result)
        self.assertNotIn("similar", result)
        self.assertEqual(result["name"], "Media Outage")

    async def test_a_hanging_provider_is_cut_off_by_the_media_budget(self):
        # An event nothing ever sets, so the only thing that ends the fetch is
        # the wait_for budget — no wall-clock sleep, no liveness guess.
        gid = await seed_game("Hanging Provider")
        never = asyncio.Event()

        async def hang(**kwargs):
            await never.wait()

        with (
            patch.object(detail, "DETAIL_MEDIA_TIMEOUT_SECONDS", 0.05),
            patch("gamelib_mcp.tools.game_media.get_game_media", hang),
        ):
            result = await detail.get_game_detail(game_id=gid, media=True)

        self.assertNotIn("media", result)
        self.assertEqual(result["name"], "Hanging Provider")


def _pedigree_raw(previous: list[dict], **overrides) -> dict:
    """A raw pedigree block as data/media.py hands it over (un-annotated)."""
    block = {
        "developer": {
            "name": "Team Cherry",
            "igdb_company_id": 6455,
            "founded_year": 2012,
            "country": 36,
        },
        "developer_names": ["Team Cherry"],
        "publisher_name": "Team Cherry",
        "previous_games": previous,
        "previous_count": len(previous),
        "previous_truncated": False,
        "catalog_size": len(previous) + 1,
        "catalog_truncated": False,
        "big_catalog": False,
        "hypes": 12,
    }
    block.update(overrides)
    return block


class GetGameDetailPedigreeTests(ToolDBTestCase):
    """`pedigree`: the developer's previous games, read against the library.

    Same seam and same absence discipline as the media/similar blocks above —
    what is new here is the annotation and the track record computed from it.
    """

    def _media(self, payload, **kwargs):
        return patch(
            "gamelib_mcp.tools.game_media.get_game_media",
            AsyncMock(return_value=payload, **kwargs),
        )

    async def _library_neighbours(self) -> None:
        """One rated+played, one owned+unplayed, one rated but not owned."""
        played = await seed_game("Rated And Played")
        await add_platform(played, "steam", playtime_minutes=600)
        await add_rating(played, "manual", 8.0, 8.0)
        untouched = await seed_game("Owned Unplayed")
        await add_platform(untouched, "steam", playtime_minutes=0)
        let_go = await seed_game("Rated Not Owned")
        await add_rating(let_go, "manual", 9.0, 9.0)
        async with db_module.get_db() as db:
            for game_id, igdb_id in ((played, 501), (untouched, 502), (let_go, 503)):
                await db.execute(
                    "UPDATE games SET igdb_id = ? WHERE id = ?", (igdb_id, game_id)
                )
            await db.commit()

    def _payload(self, pedigree: dict | None) -> dict:
        return {**_MEDIA_PAYLOAD, "pedigree_raw": pedigree}

    async def test_previous_games_are_annotated_and_scored_against_the_library(self):
        gid = await seed_game("Pedigree Probe")
        await self._library_neighbours()
        pedigree = _pedigree_raw(
            [
                {
                    "igdb_id": 501,
                    "name": "Rated And Played",
                    "release_year": 2014,
                    "cover_image_id": "abc",
                    "critic_score": 86,
                },
                {
                    "igdb_id": 502,
                    "name": "Owned Unplayed",
                    "release_year": 2013,
                    "cover_image_id": None,
                    "critic_score": None,
                },
                {
                    "igdb_id": 503,
                    "name": "Rated Not Owned",
                    "release_year": 2012,
                    "cover_image_id": None,
                    "critic_score": 74,
                },
                {
                    "igdb_id": 599,
                    "name": "Never Heard Of It",
                    "release_year": 2011,
                    "cover_image_id": None,
                    "critic_score": None,
                },
            ]
        )
        with self._media(self._payload(pedigree)):
            result = await detail.get_game_detail(game_id=gid, media=True)

        block = result["pedigree"]
        # Passed through untouched: this layer only knows about ownership.
        self.assertEqual(block["developer"]["name"], "Team Cherry")
        self.assertEqual(block["publisher_name"], "Team Cherry")
        self.assertEqual(block["hypes"], 12)
        played, unplayed, let_go, unknown = block["previous_games"]
        self.assertEqual(
            played,
            {
                "igdb_id": 501,
                "name": "Rated And Played",
                "release_year": 2014,
                "critic_score": 86,
                "cover_url": (
                    "https://images.igdb.com/igdb/image/upload/t_cover_big/abc.jpg"
                ),
                "owned": True,
                "my_rating": 8.0,
                "playtime_hours": 10.0,
            },
        )
        self.assertTrue(unplayed["owned"])
        self.assertEqual(unplayed["playtime_hours"], 0.0)
        self.assertIsNone(unplayed["my_rating"])
        self.assertFalse(let_go["owned"])
        self.assertEqual(let_go["my_rating"], 9.0)
        self.assertFalse(unknown["owned"])
        self.assertIsNone(unknown["cover_url"])
        # The raw slug is replaced by the URL, never carried alongside it.
        self.assertNotIn("cover_image_id", played)

        self.assertEqual(
            block["library_track_record"],
            # Owned twice, but only one of them was ever launched; both ratings
            # count, including the one he no longer owns.
            {"owned_count": 2, "played_count": 1, "avg_my_rating": 8.5},
        )

    async def test_no_ratings_leaves_the_average_null(self):
        gid = await seed_game("Unrated Studio")
        owned = await seed_game("Owned Unrated")
        await add_platform(owned, "steam", playtime_minutes=0)
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = 601 WHERE id = ?", (owned,))
            await db.commit()
        pedigree = _pedigree_raw(
            [
                {
                    "igdb_id": 601,
                    "name": "Owned Unrated",
                    "release_year": 2015,
                    "cover_image_id": None,
                    "critic_score": 70,
                }
            ]
        )
        with self._media(self._payload(pedigree)):
            result = await detail.get_game_detail(game_id=gid, media=True)

        self.assertEqual(
            result["pedigree"]["library_track_record"],
            {"owned_count": 1, "played_count": 0, "avg_my_rating": None},
        )

    async def test_the_damper_leaves_the_studio_facts_without_a_track_record(self):
        gid = await seed_game("Big Studio Probe")
        pedigree = _pedigree_raw(
            [], big_catalog=True, catalog_size=30, catalog_truncated=True
        )
        with self._media(self._payload(pedigree)):
            result = await detail.get_game_detail(game_id=gid, media=True)

        block = result["pedigree"]
        self.assertEqual(block["previous_games"], [])
        self.assertIsNone(block["library_track_record"])
        self.assertTrue(block["big_catalog"])
        self.assertEqual(block["catalog_size"], 30)

    async def test_no_pedigree_leaves_the_key_absent(self):
        gid = await seed_game("Studioless")
        with self._media(self._payload(None)):
            result = await detail.get_game_detail(game_id=gid, media=True)

        self.assertNotIn("pedigree", result)
        self.assertIn("media", result)

    async def test_media_off_by_default_never_carries_pedigree(self):
        gid = await seed_game("Quiet Detail")
        result = await detail.get_game_detail(game_id=gid)
        self.assertNotIn("pedigree", result)


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
