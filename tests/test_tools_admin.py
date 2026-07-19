"""Characterization tests for gamelib_mcp.tools.admin.

Covers detect_farmed_games (pure DB) and set_nintendo_session, including its
successful file-write path (writes to a temp NINTENDO_COOKIES_FILE).
"""

import asyncio
import json
import os

from fastmcp.exceptions import ToolError
from unittest.mock import AsyncMock, patch

from conftest import (
    ToolDBTestCase,
    add_game_alias,
    add_identifier,
    add_platform,
    add_rating,
    make_steam_game,
    seed_game,
)
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.db import get_meta, set_meta_many
from gamelib_mcp.tools import admin
from gamelib_mcp import lifecycle


async def _insert_play_history(game_id: int, platform: str, day: str, minutes: int) -> None:
    async with db_module.get_db() as db:
        await db.execute(
            """INSERT INTO play_history (game_id, platform, snapshot_date, playtime_minutes)
               VALUES (?, ?, ?, ?)""",
            (game_id, platform, day, minutes),
        )
        await db.commit()


class DetectFarmedGamesTests(ToolDBTestCase):
    async def _seed_farming_day(self):
        # Two low-playtime Steam games last played on the same day (2023-11-14).
        epoch = 1700000000
        await make_steam_game(
            "Card Farm A", 1, playtime_minutes=30, rtime_last_played=epoch
        )
        await make_steam_game(
            "Card Farm B", 2, playtime_minutes=60, rtime_last_played=epoch
        )

    async def test_dry_run_reports_candidates_without_marking(self):
        await self._seed_farming_day()
        result = await admin.detect_farmed_games(dry_run=True, min_games_per_day=2)
        self.assertEqual(
            set(result),
            {
                "farming_days",
                "candidates",
                "steam_appids",
                "threshold_hours",
                "dry_run",
                "sample_games",
            },
        )
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["steam_appids"], [1, 2])
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(result["farming_days"]), 1)
        day = result["farming_days"][0]
        self.assertEqual(day["date"], "2023-11-14")
        self.assertEqual(day["game_count"], 2)
        self.assertEqual(day["median_playtime_hours"], round((0.5 + 1.0) / 2, 2))
        # dry run leaves is_farmed untouched
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM games WHERE is_farmed = 1"
            )
        self.assertEqual(row["c"], 0)

    async def test_non_dry_run_marks_games(self):
        await self._seed_farming_day()
        result = await admin.detect_farmed_games(dry_run=False, min_games_per_day=2)
        self.assertFalse(result["dry_run"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM games WHERE is_farmed = 1"
            )
        self.assertEqual(row["c"], 2)

    async def test_manual_is_farmed_override_is_respected(self):
        await self._seed_farming_day()
        # User manually un-farms one of the candidates; detection must not re-flag it.
        async with db_module.get_db() as db:
            target = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ?", ("Card Farm A",)
            )
        from gamelib_mcp.tools import platforms
        await platforms.update_game(game_id=target["id"], is_farmed=False)

        await admin.detect_farmed_games(dry_run=False, min_games_per_day=2)

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT is_farmed FROM games WHERE id = ?", (target["id"],)
            )
        self.assertEqual(row["is_farmed"], 0)

    async def test_below_threshold_no_farming_day(self):
        await make_steam_game("Lonely", 1, playtime_minutes=30, rtime_last_played=1700000000)
        result = await admin.detect_farmed_games(dry_run=True, min_games_per_day=8)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["farming_days"], [])

    async def test_null_playtime_game_is_not_a_farming_candidate(self):
        # A game with NULL playtime has no rtime_last_played and no positive
        # playtime; it must never be flagged as a farmed/card-farming candidate.
        await self._seed_farming_day()
        gid = await seed_game("Manual")
        await add_platform(gid, "gog")  # NULL playtime, no steam data
        result = await admin.detect_farmed_games(dry_run=True, min_games_per_day=2)
        self.assertNotIn("Manual", [g["name"] for g in result["sample_games"]])


class DetectOrphanGamesTests(ToolDBTestCase):
    async def test_clean_library_reports_nothing(self):
        await make_steam_game("Dead Space", 17470)
        result = await admin.detect_orphan_games()
        self.assertEqual(result["orphans"], [])
        self.assertEqual(result["orphan_count"], 0)
        self.assertEqual(result["wishlist_only_count"], 0)

    async def test_wishlist_only_game_counted_but_not_listed(self):
        # Legit shape: games row + game_wishlist row, no game_platforms rows.
        # Must be counted in wishlist_only_count, NOT reported as an orphan.
        wishlist_only = await seed_game("Persona 3 Reload")
        await db_module.upsert_wishlist_entry(wishlist_only, "switch2", source="dekudeals")

        result = await admin.detect_orphan_games()

        self.assertEqual(result["orphans"], [])
        self.assertEqual(result["orphan_count"], 0)
        self.assertEqual(result["wishlist_only_count"], 1)

    async def test_true_orphan_reported_as_candidate(self):
        # No game_platforms row and no game_wishlist row at all — e.g. a
        # wishlist entry that was later removed upstream without ever being
        # owned, leaving the games row dangling.
        orphan = await seed_game("Dangling Game")
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET igdb_id = ? WHERE id = ?", (12345, orphan)
            )
            await db.commit()

        result = await admin.detect_orphan_games()

        self.assertEqual(result["orphan_count"], 1)
        self.assertEqual(result["wishlist_only_count"], 0)
        candidate = result["orphans"][0]
        self.assertEqual(candidate["game_id"], orphan)
        self.assertEqual(candidate["name"], "Dangling Game")
        self.assertEqual(candidate["igdb_id"], 12345)

    async def test_owned_and_manual_stub_games_are_not_orphans(self):
        await make_steam_game("Owned", 1)
        stub = await seed_game("Manual Stub")
        await add_platform(stub, "switch2", owned=0)

        result = await admin.detect_orphan_games()

        self.assertEqual(result["orphans"], [])
        self.assertEqual(result["orphan_count"], 0)

    async def test_non_primary_library_item_is_not_flagged(self):
        # DLC/expansion rows are never real orphans in this sense — they are
        # deliberately excluded by is_primary_library_item, which is a
        # content-type flag, not an ownership one.
        await seed_game(
            "Some DLC", content_type="dlc", is_primary_library_item=0
        )
        result = await admin.detect_orphan_games()
        self.assertEqual(result["orphans"], [])
        self.assertEqual(result["orphan_count"], 0)


class SetNintendoSessionValidationTests(ToolDBTestCase):
    async def test_invalid_json_returns_error(self):
        with self.assertRaisesRegex(ToolError, "Invalid JSON"):
            await admin.set_nintendo_session("not json{")

    async def test_non_object_or_array_rejected(self):
        with self.assertRaisesRegex(ToolError, "Expected a JSON object or array"):
            await admin.set_nintendo_session(json.dumps(42))

    async def test_empty_cookies_rejected(self):
        with self.assertRaisesRegex(ToolError, "No valid cookies found in input"):
            await admin.set_nintendo_session(json.dumps({}))

    async def test_valid_cookies_write_succeeds(self):
        import os
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = os.path.join(tmp, "nested", "cookies.json")
            with patch.dict(os.environ, {"NINTENDO_COOKIES_FILE": cookie_path}):
                result = await admin.set_nintendo_session(
                    json.dumps([{"name": "id_token", "value": "abc"}])
                )
            self.assertEqual(result["cookie_count"], 1)
            self.assertEqual(result["path"], cookie_path)
            with open(cookie_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"id_token": "abc"})


class RefreshLibraryValidationTests(ToolDBTestCase):
    class FakeContext:
        def __init__(self):
            self.progress = []
            self.infos = []

        async def report_progress(self, progress, total):
            self.progress.append((progress, total))

        async def info(self, message):
            self.infos.append(message)

    async def test_unknown_platform_raises_tool_error(self):
        with self.assertRaisesRegex(ToolError, "Unknown platform 'playstation'"):
            await admin.refresh_library(["playstation"])

    async def test_reports_progress(self):
        ctx = self.FakeContext()
        with (
            patch.object(admin, "fetch_library", AsyncMock(return_value={"platform": "steam"})),
            patch.object(admin, "sync_epic", AsyncMock(return_value={"platform": "epic"})),
            patch.object(admin, "detect_farmed_games", AsyncMock(return_value={"candidates": 0})),
        ):
            result = await admin.run_library_sync(["steam", "epic"], ctx=ctx)

        self.assertEqual(result["steam"], {"platform": "steam", "play_history_rows": 0})
        self.assertEqual(result["epic"], {"platform": "epic", "play_history_rows": 0})
        self.assertEqual(ctx.progress, [(0, 2), (1, 2), (2, 2)])
        self.assertIn("Refreshing 2 platform(s)", ctx.infos)
        self.assertIn("Finished steam refresh", ctx.infos)
        self.assertIn("Finished epic refresh", ctx.infos)

    async def test_refresh_library_xbox_uses_patched_sync(self):
        with (
            patch.object(
                admin, "sync_xbox", AsyncMock(return_value={"added": 0, "matched": 0, "skipped": 0})
            ) as mock_sync,
            patch.object(admin, "detect_farmed_games", AsyncMock(return_value={"candidates": 0})),
        ):
            result = await admin.run_library_sync(["xbox"])

        mock_sync.assert_awaited()
        self.assertEqual(
            result["xbox"], {"added": 0, "matched": 0, "skipped": 0, "play_history_rows": 0}
        )


class RunLibrarySyncStateTests(ToolDBTestCase):
    async def test_writes_done_state_for_successful_platform(self):
        async def fake_steam():
            # while running, state must read "running"
            assert await get_meta("sync_platform_state_steam") == "running"
            return {"games_upserted": 3}

        with patch("gamelib_mcp.tools.admin.fetch_library", side_effect=fake_steam), \
             patch("gamelib_mcp.tools.admin.detect_farmed_games", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.admin._schedule_background_enrich", AsyncMock()):
            result = await admin.run_library_sync(["steam"])

        self.assertEqual(
            result["steam"], {"games_upserted": 3, "play_history_rows": 0}
        )
        self.assertEqual(await get_meta("sync_platform_state_steam"), "done")
        self.assertEqual(await get_meta("library_sync_status"), "idle")

    async def test_marks_platform_error_on_failure(self):
        with patch("gamelib_mcp.tools.admin.fetch_library", AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("gamelib_mcp.tools.admin._schedule_background_enrich", AsyncMock()):
            result = await admin.run_library_sync(["steam"])

        self.assertIn("error", result["steam"])
        self.assertEqual(await get_meta("sync_platform_state_steam"), "error")
        self.assertEqual(await get_meta("library_sync_status"), "idle")

    async def test_clears_fulfilled_wishlist_entries_after_sync(self):
        game_id = await seed_game("Was Wishlisted")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam")

        async def fake_steam():
            # Ownership established mid-sync, same as a real Steam refresh would.
            await add_platform(game_id, "steam", owned=1)
            return {"games_upserted": 1}

        with patch("gamelib_mcp.tools.admin.fetch_library", side_effect=fake_steam), \
             patch("gamelib_mcp.tools.admin.detect_farmed_games", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.admin._schedule_background_enrich", AsyncMock()):
            await admin.run_library_sync(["steam"])

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ? AND platform = ?", (game_id, "steam")
            )
        self.assertIsNone(row)

    async def test_snapshots_play_history_after_successful_sync(self):
        game_id = await seed_game("Hades")

        async def fake_steam():
            await add_platform(game_id, "steam", playtime_minutes=42, owned=1)
            return {"games_upserted": 1}

        with patch("gamelib_mcp.tools.admin.fetch_library", side_effect=fake_steam), \
             patch("gamelib_mcp.tools.admin.detect_farmed_games", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.admin._schedule_background_enrich", AsyncMock()):
            result = await admin.run_library_sync(["steam"])

        self.assertEqual(result["steam"]["play_history_rows"], 1)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM play_history WHERE game_id = ? AND platform = 'steam'",
                (game_id,),
            )
        self.assertIsNotNone(row)
        self.assertEqual(row["playtime_minutes"], 42)

    async def test_snapshot_failure_does_not_fail_sync(self):
        with patch("gamelib_mcp.tools.admin.fetch_library", AsyncMock(return_value={"ok": True})), \
             patch("gamelib_mcp.tools.admin.detect_farmed_games", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.admin._schedule_background_enrich", AsyncMock()), \
             patch(
                 "gamelib_mcp.tools.admin.record_play_history_snapshots",
                 AsyncMock(side_effect=RuntimeError("boom")),
             ):
            result = await admin.run_library_sync(["steam"])

        self.assertEqual(result["steam"], {"ok": True})
        self.assertEqual(await get_meta("sync_platform_state_steam"), "done")


class RefreshLibraryAckTests(ToolDBTestCase):
    async def asyncTearDown(self) -> None:
        task = lifecycle._LIBRARY_REFRESH_TASK
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        lifecycle._LIBRARY_REFRESH_TASK = None
        await super().asyncTearDown()

    async def test_returns_started_without_blocking(self):
        release = asyncio.Event()

        async def slow_worker(platforms=None):
            await release.wait()
            return {}

        with patch("gamelib_mcp.lifecycle._admin_refresh_library", AsyncMock(side_effect=slow_worker)):
            ack = await admin.refresh_library(["steam"])
            self.assertEqual(ack["status"], "started")
            self.assertFalse(ack["already_running"])
            self.assertEqual(ack["platforms"], ["steam"])
            self.assertEqual(await get_meta("library_sync_status"), "in_progress")
            release.set()

    async def test_returns_already_running_when_in_flight(self):
        release = asyncio.Event()

        async def slow_worker(platforms=None):
            await release.wait()
            return {}

        with patch("gamelib_mcp.lifecycle._admin_refresh_library", AsyncMock(side_effect=slow_worker)):
            first = await admin.refresh_library(["steam"])
            second = await admin.refresh_library(["gog"])
            self.assertEqual(first["status"], "started")
            self.assertEqual(second["status"], "already_running")
            self.assertTrue(second["already_running"])
            release.set()

    async def test_rejects_unknown_platform(self):
        with self.assertRaises(ToolError):
            await admin.refresh_library(["nope"])


class SyncWishlistTests(ToolDBTestCase):
    async def test_rejects_unknown_platform(self):
        with self.assertRaisesRegex(ToolError, "Unknown wishlist platform 'ps5'"):
            await admin.sync_wishlist(["ps5"])

    async def test_defaults_to_steam_and_switch2(self):
        with (
            patch(
                "gamelib_mcp.data.steam_wishlist.fetch_wishlist",
                AsyncMock(return_value={"added": 1}),
            ) as steam_fn,
            patch(
                "gamelib_mcp.data.dekudeals.sync_dekudeals_wishlist",
                AsyncMock(return_value={"matched": 2}),
            ) as deku_fn,
        ):
            result = await admin.sync_wishlist()

        steam_fn.assert_awaited_once()
        deku_fn.assert_awaited_once()
        self.assertEqual(result, {"steam": {"added": 1}, "switch2": {"matched": 2}})

    async def test_clears_fulfilled_wishlist_entries_after_sync(self):
        game_id = await seed_game("Already Owned Elsewhere")
        await add_platform(game_id, "steam", owned=1)
        # Simulate the sync re-adding a wishlist row for an already-owned game
        # (e.g. a stale external wishlist) by writing it directly, then check
        # that sync_wishlist's post-sync cleanup reconciles it away.
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam")

        with patch(
            "gamelib_mcp.data.steam_wishlist.fetch_wishlist",
            AsyncMock(return_value={"added": 0, "matched": 1, "skipped": 0}),
        ):
            await admin.sync_wishlist(["steam"])

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ? AND platform = ?", (game_id, "steam")
            )
        self.assertIsNone(row)


class GetSyncStatusTests(ToolDBTestCase):
    async def test_reports_idle_with_pending_platforms_when_never_synced(self):
        status = await admin.get_sync_status()
        self.assertEqual(status["status"], "idle")
        self.assertEqual(
            set(status["platforms"]), {"steam", "epic", "gog", "switch2", "ps5", "xbox"}
        )
        self.assertEqual(status["platforms"]["steam"]["state"], "pending")

    async def test_reflects_in_progress_and_per_platform_state(self):
        await set_meta_many(
            {
                "library_sync_status": "in_progress",
                "library_sync_started_at": "2026-06-14T12:00:00+00:00",
                "library_sync_finished_at": None,
                "sync_platform_state_steam": "done",
                "sync_platform_state_gog": "running",
                "sync_platform_state_ps5": "error",
                "integration_sync_ps5_last_error_summary": "refresh token rejected",
            }
        )
        status = await admin.get_sync_status()
        self.assertEqual(status["status"], "in_progress")
        self.assertEqual(status["started_at"], "2026-06-14T12:00:00+00:00")
        self.assertIsNone(status["finished_at"])
        self.assertEqual(status["platforms"]["steam"]["state"], "done")
        self.assertEqual(status["platforms"]["gog"]["state"], "running")
        self.assertEqual(status["platforms"]["ps5"]["state"], "error")
        self.assertEqual(status["platforms"]["ps5"]["error"], "refresh token rejected")


class MergeGamesTests(ToolDBTestCase):
    async def test_same_id_raises_tool_error(self):
        gid = await seed_game("Solo")
        with self.assertRaisesRegex(ToolError, "must differ"):
            await admin.merge_games(gid, gid)

    async def test_missing_source_raises_tool_error(self):
        gid = await seed_game("Target")
        with self.assertRaisesRegex(ToolError, "not found"):
            await admin.merge_games(99999, gid)

    async def test_missing_target_raises_tool_error(self):
        gid = await seed_game("Source")
        with self.assertRaisesRegex(ToolError, "not found"):
            await admin.merge_games(gid, 99999)

    async def test_platform_moved_when_target_lacks_it(self):
        src = await seed_game("PSN Localized")
        tgt = await seed_game("Real English")
        sp_id = await add_platform(src, "ps5", playtime_minutes=120)
        await add_identifier(sp_id, "psn_title_id", "PPSA12345_00")

        result = await admin.merge_games(src, tgt)

        self.assertEqual(result["platforms_moved"], ["ps5"])
        self.assertEqual(result["platforms_merged"], [])
        self.assertTrue(result["source_deleted"])

        # source game gone; target now has ps5
        async with db_module.get_db() as db:
            src_row = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (src,))
            self.assertIsNone(src_row)
            tgt_plat = await db.execute_fetchone(
                "SELECT playtime_minutes FROM game_platforms WHERE game_id = ? AND platform = ?",
                (tgt, "ps5"),
            )
        self.assertIsNotNone(tgt_plat)
        self.assertEqual(tgt_plat["playtime_minutes"], 120)

        # identifier re-pointed to target
        async with db_module.get_db() as db:
            ident = await db.execute_fetchone(
                """SELECT gp.game_id
                     FROM game_platform_identifiers gpi
                     JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                    WHERE gpi.identifier_value = 'PPSA12345_00'""",
            )
        self.assertEqual(ident["game_id"], tgt)

    async def test_platform_merged_keeps_higher_playtime(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        await add_platform(src, "ps5", playtime_minutes=200)
        await add_platform(tgt, "ps5", playtime_minutes=50)

        result = await admin.merge_games(src, tgt)

        self.assertEqual(result["platforms_merged"], ["ps5"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM game_platforms WHERE game_id = ? AND platform = ?",
                (tgt, "ps5"),
            )
        self.assertEqual(row["playtime_minutes"], 200)

    async def test_platform_merged_keeps_target_playtime_when_higher(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        await add_platform(src, "ps5", playtime_minutes=30)
        await add_platform(tgt, "ps5", playtime_minutes=150)

        await admin.merge_games(src, tgt)

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM game_platforms WHERE game_id = ? AND platform = ?",
                (tgt, "ps5"),
            )
        self.assertEqual(row["playtime_minutes"], 150)

    async def _target_acquisition(self, tgt: int, platform: str = "ps5") -> dict:
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                """SELECT acquired_at, price_paid, price_currency,
                          purchase_source, bundle_name
                   FROM game_platforms WHERE game_id = ? AND platform = ?""",
                (tgt, platform),
            )
        return dict(row)

    async def test_platform_merged_fills_target_acquisition_from_source(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        src_gpid = await add_platform(src, "ps5", playtime_minutes=10)
        await add_platform(tgt, "ps5", playtime_minutes=20)
        await db_module.set_platform_acquisition(
            src_gpid,
            {
                "acquired_at": "2023-06-01",
                "price_paid": 24.99,
                "price_currency": "USD",
                "purchase_source": "psn",
                "bundle_name": "Summer Sale",
            },
        )

        result = await admin.merge_games(src, tgt)

        self.assertEqual(result["platforms_merged"], ["ps5"])
        self.assertEqual(
            await self._target_acquisition(tgt),
            {
                "acquired_at": "2023-06-01",
                "price_paid": 24.99,
                "price_currency": "USD",
                "purchase_source": "psn",
                "bundle_name": "Summer Sale",
            },
        )

    async def test_platform_merged_target_acquisition_wins_on_conflict(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        src_gpid = await add_platform(src, "ps5")
        tgt_gpid = await add_platform(tgt, "ps5")
        await db_module.set_platform_acquisition(
            src_gpid,
            {
                "acquired_at": "2021-01-01",
                "price_paid": 59.99,
                "price_currency": "USD",
                "purchase_source": "physical",
            },
        )
        # Target already knows its price but not the date or source: the merge
        # must keep the target's values and only fill its NULL columns.
        await db_module.set_platform_acquisition(
            tgt_gpid, {"price_paid": 9.99, "price_currency": "EUR"}
        )

        await admin.merge_games(src, tgt)

        self.assertEqual(
            await self._target_acquisition(tgt),
            {
                "acquired_at": "2021-01-01",   # filled from source
                "price_paid": 9.99,            # target wins
                "price_currency": "EUR",       # target wins
                "purchase_source": "physical", # filled from source
                "bundle_name": None,
            },
        )

    async def test_platform_moved_carries_acquisition_data(self):
        src = await seed_game("Source Only PSN")
        tgt = await seed_game("Target Without PSN")
        src_gpid = await add_platform(src, "ps5")
        await db_module.set_platform_acquisition(
            src_gpid, {"price_paid": 14.99, "price_currency": "USD"}
        )

        result = await admin.merge_games(src, tgt)

        self.assertEqual(result["platforms_moved"], ["ps5"])
        row = await self._target_acquisition(tgt)
        self.assertEqual(row["price_paid"], 14.99)
        self.assertEqual(row["price_currency"], "USD")

    async def test_ratings_moved_when_target_lacks_source(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        await add_rating(src, "backloggd", 8.0, 8.0)

        result = await admin.merge_games(src, tgt)

        self.assertEqual(result["ratings_moved"], ["backloggd"])
        self.assertEqual(result["ratings_kept_target"], [])

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT game_id FROM ratings WHERE source = 'backloggd'",
            )
        self.assertEqual(row["game_id"], tgt)

    async def test_ratings_kept_target_on_conflict(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        await add_rating(src, "backloggd", 6.0, 6.0)
        await add_rating(tgt, "backloggd", 9.0, 9.0)

        result = await admin.merge_games(src, tgt)

        self.assertEqual(result["ratings_kept_target"], ["backloggd"])
        # target's rating survives
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT raw_score FROM ratings WHERE game_id = ? AND source = 'backloggd'",
                (tgt,),
            )
        self.assertEqual(row["raw_score"], 9.0)

    async def test_series_memberships_transferred(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        async with db_module.get_db() as db:
            await db.execute(
                "INSERT INTO game_series (kind, igdb_id, name) VALUES ('collection', 42, 'Test Series')"
            )
            series_row = await db.execute_fetchone(
                "SELECT id FROM game_series WHERE igdb_id = 42"
            )
            await db.execute(
                "INSERT INTO game_series_membership (game_id, series_id) VALUES (?, ?)",
                (src, series_row["id"]),
            )
            await db.commit()

        result = await admin.merge_games(src, tgt)

        self.assertEqual(result["series_memberships_transferred"], 1)
        async with db_module.get_db() as db:
            membership = await db.execute_fetchone(
                "SELECT game_id FROM game_series_membership WHERE series_id = ?",
                (series_row["id"],),
            )
        self.assertEqual(membership["game_id"], tgt)

    async def test_aliases_transferred(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        await add_game_alias(src, "Source Alt", alias_type="edition")

        result = await admin.merge_games(src, tgt)

        self.assertEqual(result["aliases_transferred"], 1)
        async with db_module.get_db() as db:
            alias = await db.execute_fetchone(
                "SELECT game_id FROM game_aliases WHERE alias = 'Source Alt'",
            )
        self.assertEqual(alias["game_id"], tgt)

    async def test_source_deleted_after_merge(self):
        src = await seed_game("Duplicate PSN Row")
        tgt = await seed_game("English Title")
        await add_platform(src, "ps5", playtime_minutes=60)

        await admin.merge_games(src, tgt)

        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (src,))
        self.assertIsNone(row)

    async def test_dry_run_makes_no_changes(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        await add_platform(src, "ps5", playtime_minutes=100)
        await add_rating(src, "backloggd", 7.0, 7.0)

        result = await admin.merge_games(src, tgt, dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertFalse(result["source_deleted"])
        self.assertEqual(result["platforms_moved"], ["ps5"])
        self.assertEqual(result["ratings_moved"], ["backloggd"])

        # nothing actually changed
        async with db_module.get_db() as db:
            src_row = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (src,))
            src_plat = await db.execute_fetchone(
                "SELECT id FROM game_platforms WHERE game_id = ?", (src,)
            )
        self.assertIsNotNone(src_row)
        self.assertIsNotNone(src_plat)

    async def test_response_shape(self):
        src = await seed_game("PSN Dup")
        tgt = await seed_game("English")
        result = await admin.merge_games(src, tgt)
        expected_keys = {
            "dry_run", "source", "target",
            "platforms_moved", "platforms_merged",
            "ratings_moved", "ratings_kept_target",
            "series_memberships_transferred", "aliases_transferred",
            "play_history_rows_transferred",
            "source_deleted",
        }
        self.assertEqual(set(result.keys()), expected_keys)
        self.assertEqual(result["source"]["game_id"], src)
        self.assertEqual(result["target"]["game_id"], tgt)

    async def test_owned_propagated_when_merging_into_unowned_target(self):
        src = await seed_game("PSN English Synced")
        tgt = await seed_game("Manual Stub")
        await add_platform(src, "ps5", playtime_minutes=90, owned=1)
        await add_platform(tgt, "ps5", playtime_minutes=0, owned=0)

        await admin.merge_games(src, tgt)

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT owned FROM game_platforms WHERE game_id = ? AND platform = ?",
                (tgt, "ps5"),
            )
        self.assertEqual(row["owned"], 1)

    async def test_series_count_excludes_entries_target_already_has(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        async with db_module.get_db() as db:
            await db.execute(
                "INSERT INTO game_series (kind, igdb_id, name) VALUES ('collection', 7, 'Shared')"
            )
            shared = await db.execute_fetchone("SELECT id FROM game_series WHERE igdb_id = 7")
            # both source and target already belong to the shared series
            await db.execute(
                "INSERT INTO game_series_membership (game_id, series_id) VALUES (?, ?)",
                (src, shared["id"]),
            )
            await db.execute(
                "INSERT INTO game_series_membership (game_id, series_id) VALUES (?, ?)",
                (tgt, shared["id"]),
            )
            await db.commit()

        result = await admin.merge_games(src, tgt)
        # target already had the only series — nothing new transferred
        self.assertEqual(result["series_memberships_transferred"], 0)

    async def test_dry_run_series_count_excludes_shared(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        async with db_module.get_db() as db:
            await db.execute(
                "INSERT INTO game_series (kind, igdb_id, name) VALUES ('collection', 8, 'A')"
            )
            await db.execute(
                "INSERT INTO game_series (kind, igdb_id, name) VALUES ('collection', 9, 'B')"
            )
            a = await db.execute_fetchone("SELECT id FROM game_series WHERE igdb_id = 8")
            b = await db.execute_fetchone("SELECT id FROM game_series WHERE igdb_id = 9")
            # source in both A and B; target already in A
            await db.execute(
                "INSERT INTO game_series_membership (game_id, series_id) VALUES (?, ?)",
                (src, a["id"]),
            )
            await db.execute(
                "INSERT INTO game_series_membership (game_id, series_id) VALUES (?, ?)",
                (src, b["id"]),
            )
            await db.execute(
                "INSERT INTO game_series_membership (game_id, series_id) VALUES (?, ?)",
                (tgt, a["id"]),
            )
            await db.commit()

        result = await admin.merge_games(src, tgt, dry_run=True)
        # only B would actually be inserted
        self.assertEqual(result["series_memberships_transferred"], 1)

    async def test_aliases_count_excludes_entries_target_already_has(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        await add_game_alias(src, "Shared Alias", alias_type="edition")
        await add_game_alias(tgt, "Shared Alias", alias_type="edition")

        result = await admin.merge_games(src, tgt)
        self.assertEqual(result["aliases_transferred"], 0)

    async def test_tag_affinity_recomputed_after_rating_move(self):
        src = await seed_game("Source", tags=["roguelike"])
        tgt = await seed_game("Target", tags=["roguelike"])
        await add_rating(src, "backloggd", 9.0, 9.0)

        with patch(
            "gamelib_mcp.data.db.recompute_tag_affinity", AsyncMock()
        ) as mock_recompute:
            await admin.merge_games(src, tgt)
        mock_recompute.assert_awaited_once()

    async def test_tag_affinity_not_recomputed_without_ratings(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        await add_platform(src, "ps5", playtime_minutes=10)

        with patch(
            "gamelib_mcp.data.db.recompute_tag_affinity", AsyncMock()
        ) as mock_recompute:
            await admin.merge_games(src, tgt)
        mock_recompute.assert_not_awaited()

    async def test_dry_run_does_not_recompute_affinity(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        await add_rating(src, "backloggd", 7.0, 7.0)

        with patch(
            "gamelib_mcp.data.db.recompute_tag_affinity", AsyncMock()
        ) as mock_recompute:
            await admin.merge_games(src, tgt, dry_run=True)
        mock_recompute.assert_not_awaited()

    async def test_play_history_transferred_with_collision_resolution(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        await add_platform(src, "steam", playtime_minutes=200)
        await add_platform(tgt, "steam", playtime_minutes=100)
        # Disjoint days on each side + one same-day collision.
        await _insert_play_history(src, "steam", "2026-06-01", 50)
        await _insert_play_history(src, "steam", "2026-06-10", 200)  # collision, src higher
        await _insert_play_history(tgt, "steam", "2026-06-05", 80)
        await _insert_play_history(tgt, "steam", "2026-06-10", 120)  # collision, tgt lower

        result = await admin.merge_games(src, tgt)

        self.assertEqual(result["play_history_rows_transferred"], 2)
        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                """SELECT snapshot_date, playtime_minutes FROM play_history
                   WHERE game_id = ? AND platform = 'steam' ORDER BY snapshot_date""",
                (tgt,),
            )
            orphans = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM play_history WHERE game_id = ?", (src,)
            )
        history = {r["snapshot_date"]: r["playtime_minutes"] for r in rows}
        # Union of both sides survives; the collision kept MAX(200, 120).
        self.assertEqual(
            history, {"2026-06-01": 50, "2026-06-05": 80, "2026-06-10": 200}
        )
        self.assertEqual(orphans["c"], 0)

    async def test_play_history_dry_run_counts_without_moving(self):
        src = await seed_game("Source")
        tgt = await seed_game("Target")
        await add_platform(src, "steam", playtime_minutes=50)
        await _insert_play_history(src, "steam", "2026-06-01", 50)

        result = await admin.merge_games(src, tgt, dry_run=True)

        self.assertEqual(result["play_history_rows_transferred"], 1)
        async with db_module.get_db() as db:
            still_on_src = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM play_history WHERE game_id = ?", (src,)
            )
            on_tgt = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM play_history WHERE game_id = ?", (tgt,)
            )
        self.assertEqual(still_on_src["c"], 1)
        self.assertEqual(on_tgt["c"], 0)


class RevalidateIgdbMatchesTests(ToolDBTestCase):
    """revalidate_igdb_matches: audit stored igdb_ids against IGDB's names."""

    _ENV = {"TWITCH_CLIENT_ID": "test-client", "TWITCH_CLIENT_SECRET": "test-secret"}

    async def _seed(self) -> dict[str, int]:
        good = await seed_game("The Witcher: Enhanced Edition")
        bad_tales = await seed_game("Tales from the Borderlands")
        bad_payday = await seed_game("PAYDAY 2")
        pinned = await seed_game("Pinned Game")
        unenriched = await seed_game("No IGDB Row")
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = 283715 WHERE id = ?", (good,))
            await db.execute("UPDATE games SET igdb_id = 214139 WHERE id = ?", (bad_tales,))
            await db.execute(
                "UPDATE games SET igdb_id = 150511, igdb_platforms = '[6]', "
                "igdb_cached_at = '2026-01-01', igdb_claimed_at = '2026-01-01' "
                "WHERE id = ?",
                (bad_payday,),
            )
            await db.execute(
                "UPDATE games SET igdb_id = 999, manual_overrides = ? WHERE id = ?",
                (json.dumps(["igdb_id"]), pinned),
            )
            await db.commit()
        await db_module.upsert_game_series_links(
            bad_payday, [("franchise", 912, "Payday")]
        )
        return {
            "good": good,
            "bad_tales": bad_tales,
            "bad_payday": bad_payday,
            "pinned": pinned,
            "unenriched": unenriched,
        }

    _IGDB_NAMES = {
        283715: "The Witcher: Enhanced Edition",  # matches (edition-strip equal anyway)
        214139: "New Tales from the Borderlands",  # prod mismatch
        150511: "Payday 2 VR",  # prod mismatch
        999: "Something Else Entirely",  # mismatch but manual override
    }

    async def test_dry_run_reports_mismatches_without_changing_rows(self) -> None:
        ids = await self._seed()
        with (
            patch.dict("os.environ", self._ENV),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_names",
                AsyncMock(return_value=self._IGDB_NAMES),
            ),
        ):
            result = await admin.revalidate_igdb_matches(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["checked"], 4)  # unenriched row not checked
        self.assertEqual(result["mismatch_count"], 2)
        self.assertEqual(
            {m["game_id"] for m in result["mismatches"]},
            {ids["bad_tales"], ids["bad_payday"]},
        )
        by_id = {m["game_id"]: m for m in result["mismatches"]}
        self.assertEqual(
            by_id[ids["bad_payday"]]["igdb_name"], "Payday 2 VR"
        )
        self.assertEqual(result["reset_count"], 0)
        self.assertEqual(result["skipped_overridden"], 1)

        # Nothing was modified.
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_id, igdb_cached_at FROM games WHERE id = ?",
                (ids["bad_payday"],),
            )
            memberships = await db.execute_fetchall(
                "SELECT 1 FROM game_series_membership WHERE game_id = ?",
                (ids["bad_payday"],),
            )
        self.assertEqual(row["igdb_id"], 150511)
        self.assertIsNotNone(row["igdb_cached_at"])
        self.assertEqual(len(memberships), 1)

    async def test_wet_run_resets_only_mismatched_unpinned_rows(self) -> None:
        ids = await self._seed()
        with (
            patch.dict("os.environ", self._ENV),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_names",
                AsyncMock(return_value=self._IGDB_NAMES),
            ),
        ):
            result = await admin.revalidate_igdb_matches(dry_run=False)

        self.assertFalse(result["dry_run"])
        self.assertEqual(result["reset_count"], 2)
        self.assertEqual(result["skipped_overridden"], 1)

        async with db_module.get_db() as db:
            rows = {
                r["id"]: r
                for r in await db.execute_fetchall(
                    "SELECT id, igdb_id, igdb_platforms, igdb_cached_at, igdb_claimed_at "
                    "FROM games"
                )
            }
            memberships = await db.execute_fetchall(
                "SELECT 1 FROM game_series_membership WHERE game_id = ?",
                (ids["bad_payday"],),
            )

        # Mismatched rows fully reset for re-enrichment.
        for key in ("bad_tales", "bad_payday"):
            row = rows[ids[key]]
            self.assertIsNone(row["igdb_id"])
            self.assertIsNone(row["igdb_platforms"])
            self.assertIsNone(row["igdb_cached_at"])
            self.assertIsNone(row["igdb_claimed_at"])
        self.assertEqual(memberships, [])
        # Matching row and manually pinned row untouched.
        self.assertEqual(rows[ids["good"]]["igdb_id"], 283715)
        self.assertEqual(rows[ids["pinned"]]["igdb_id"], 999)

    async def test_unresolved_igdb_ids_are_counted_and_left_alone(self) -> None:
        ids = await self._seed()
        # IGDB returns nothing for any id (all deleted/merged upstream).
        with (
            patch.dict("os.environ", self._ENV),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_names",
                AsyncMock(return_value={}),
            ),
        ):
            result = await admin.revalidate_igdb_matches(dry_run=False)

        self.assertEqual(result["mismatch_count"], 0)
        self.assertEqual(result["reset_count"], 0)
        self.assertEqual(result["unresolved_igdb_ids"], 4)

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_id FROM games WHERE id = ?", (ids["bad_payday"],)
            )
        self.assertEqual(row["igdb_id"], 150511)

    async def test_limit_caps_checked_rows(self) -> None:
        await self._seed()
        with (
            patch.dict("os.environ", self._ENV),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_names",
                AsyncMock(return_value=self._IGDB_NAMES),
            ) as fetch_mock,
        ):
            result = await admin.revalidate_igdb_matches(dry_run=True, limit=2)

        self.assertEqual(result["checked"], 2)
        # Only the first two rows' ids were sent to IGDB.
        self.assertEqual(len(fetch_mock.await_args.args[0]), 2)

    async def test_unconfigured_returns_empty_report(self) -> None:
        await self._seed()
        env_backup = {
            key: os.environ.pop(key, None)
            for key in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")
        }
        try:
            result = await admin.revalidate_igdb_matches()
        finally:
            for key, value in env_backup.items():
                if value is not None:
                    os.environ[key] = value

        self.assertFalse(result["igdb_configured"])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["mismatches"], [])


class DetectStrandedDuplicatesTests(ToolDBTestCase):
    async def _insert_duplicate_game(self, name: str) -> int:
        """Raw insert (seed_game would name-match onto the existing row)."""
        from gamelib_mcp.data.title_normalization import normalize_search_text

        async with db_module.get_db() as db:
            cursor = await db.execute(
                "INSERT INTO games (name, name_normalized) VALUES (?, ?)",
                (name, normalize_search_text(name)),
            )
            await db.commit()
            return cursor.lastrowid

    async def test_reports_only_identifierless_twin_pairs(self):
        # Stranded pair: same name, same owned platform, exactly one side
        # carries a store identifier (the prod Tiny Tina's Wonderlands shape).
        keeper = await seed_game("Tiny Tina's Wonderlands")
        keeper_gpid = await add_platform(keeper, "ps5", playtime_minutes=671)
        await add_identifier(keeper_gpid, "psn_title_id", "PPSA01492_00")
        twin = await self._insert_duplicate_game("Tiny Tina's Wonderlands")
        await add_platform(twin, "ps5", playtime_minutes=671)

        # Both-identifiers pair: two distinct store entries (anti-collapse) —
        # must NOT be flagged.
        original = await seed_game("Dead Space")
        original_gpid = await add_platform(original, "steam")
        await add_identifier(original_gpid, "steam_appid", "17470")
        remake = await self._insert_duplicate_game("Dead Space")
        remake_gpid = await add_platform(remake, "steam")
        await add_identifier(remake_gpid, "steam_appid", "1693980")

        # Same name on different platforms: not a duplicate — must NOT be
        # flagged.
        steam_side = await seed_game("Hades")
        steam_gpid = await add_platform(steam_side, "steam")
        await add_identifier(steam_gpid, "steam_appid", "1145360")
        switch_side = await self._insert_duplicate_game("Hades")
        await add_platform(switch_side, "switch2")

        result = await admin.detect_stranded_duplicates()

        self.assertEqual(result["stranded_count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["game_id"], keeper)
        self.assertEqual(candidate["duplicate_game_id"], twin)
        self.assertEqual(candidate["platform"], "ps5")
        self.assertEqual(candidate["identifiers"], ["psn_title_id=PPSA01492_00"])

    async def test_empty_library_reports_nothing(self):
        result = await admin.detect_stranded_duplicates()
        self.assertEqual(result, {"stranded_count": 0, "candidates": []})


class DetectMisclassifiedDlcTests(ToolDBTestCase):
    """detect_misclassified_dlc: read-only detector + repair-loop suggestions."""

    def _by_reason(self, result: dict, reason: str) -> list[dict]:
        return [c for c in result["candidates"] if c["reason"] == reason]

    async def test_nested_parent_bucket_and_repair(self):
        # The Fallout: New Vegas tangle: the base row was demoted to 'edition',
        # so it fails the is_primary filter while its DLC hangs off it — both
        # disappear from the library. The bucket surfaces the parent, and its
        # suggestion promotes it back through the real update_game.
        from gamelib_mcp.tools import platforms

        base = await seed_game(
            "Fallout: New Vegas", content_type="edition", is_primary_library_item=0
        )
        dlc = await seed_game(
            "Fallout New Vegas: Dead Money",
            content_type="dlc",
            parent_game_id=base,
            is_primary_library_item=0,
        )

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        stranded = self._by_reason(result, "nested_parent")
        self.assertEqual([c["game_id"] for c in stranded], [base])
        self.assertEqual(stranded[0]["evidence"]["child_count"], 1)
        self.assertEqual(stranded[0]["evidence"]["content_type"], "edition")
        self.assertEqual(result["counts"]["nested_parent"], 1)
        # First matching bucket only: the parent is parentless and nested, so it
        # would otherwise also qualify for needs_parent.
        self.assertNotIn(base, [c["game_id"] for c in self._by_reason(result, "needs_parent")])
        self.assertEqual(result["counts"]["needs_parent"], 0)

        await platforms.update_game(**stranded[0]["suggested_update"])

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT content_type, is_primary_library_item, parent_game_id "
                "FROM games WHERE id = ?",
                (base,),
            )
            child = await db.execute_fetchone(
                "SELECT parent_game_id FROM games WHERE id = ?", (dlc,)
            )
        self.assertEqual(row["content_type"], "base_game")
        self.assertEqual(row["is_primary_library_item"], 1)
        self.assertIsNone(row["parent_game_id"])
        # The children keep hanging off it — now off a visible row.
        self.assertEqual(child["parent_game_id"], base)

    async def test_needs_parent_with_resolvable_parent(self):
        base = await seed_game("Base Thing")
        child = await seed_game(
            "Base Thing: The Extra", content_type="dlc", is_primary_library_item=0
        )

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        needs = self._by_reason(result, "needs_parent")
        self.assertEqual([c["game_id"] for c in needs], [child])
        cand = needs[0]
        self.assertEqual(cand["suggested_update"], {"game_id": child, "parent_game_id": base})
        self.assertEqual(cand["evidence"]["parent_game_id"], base)
        self.assertEqual(result["counts"]["needs_parent"], 1)
        self.assertEqual(result["probed"], 0)

    async def test_needs_parent_without_resolvable_parent(self):
        child = await seed_game(
            "Standalone Mystery Widget", content_type="dlc", is_primary_library_item=0
        )

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        needs = self._by_reason(result, "needs_parent")
        self.assertEqual([c["game_id"] for c in needs], [child])
        self.assertIsNone(needs[0]["suggested_update"])
        self.assertEqual(needs[0]["evidence"]["note"], "no parent candidate resolved")

    async def test_addon_name_pattern_dlc_and_unknown_addon(self):
        season = await seed_game("Elden Ring Season Pass")
        soundtrack = await seed_game("Celeste Soundtrack")

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        by_id = {c["game_id"]: c for c in self._by_reason(result, "addon_name_pattern")}
        self.assertEqual(by_id[season]["suggested_update"], {"game_id": season, "content_type": "dlc"})
        self.assertEqual(by_id[season]["evidence"]["matched_pattern"], "season pass")
        self.assertEqual(
            by_id[soundtrack]["suggested_update"],
            {"game_id": soundtrack, "content_type": "unknown_addon"},
        )
        self.assertEqual(by_id[soundtrack]["evidence"]["matched_pattern"], "soundtrack")

    async def test_addon_name_pattern_with_resolvable_parent_suggests_parent_id(self):
        # By id, like every other bucket: the exact row the detector validated
        # as primary, with no name re-resolution at apply time.
        parent = await seed_game("Elden Ring")
        pass_id = await seed_game("Elden Ring: Season Pass")

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        by_id = {c["game_id"]: c for c in self._by_reason(result, "addon_name_pattern")}
        self.assertEqual(
            by_id[pass_id]["suggested_update"],
            {"game_id": pass_id, "content_type": "dlc", "parent_game_id": parent},
        )

    async def test_inconsistent_primary_nested_substantial_row_promotes(self):
        # The Forza Horizon 4 shape: content_type 'dlc' with is_primary=1 —
        # internally contradictory. A row with real substance (identifier /
        # playtime) suggests promotion back to base_game.
        forza = await seed_game(
            "Forza Horizon 4", content_type="dlc", is_primary_library_item=1
        )
        gpid = await add_platform(forza, "steam", playtime_minutes=600, owned=1)
        await add_identifier(gpid, "steam_appid", "1293830")

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        bucket = {
            c["game_id"]: c
            for c in self._by_reason(result, "inconsistent_primary_nested")
        }
        self.assertIn(forza, bucket)
        self.assertEqual(
            bucket[forza]["suggested_update"],
            {"game_id": forza, "content_type": "base_game"},
        )
        self.assertEqual(result["counts"]["inconsistent_primary_nested"], 1)
        # It does NOT also land in a later bucket.
        matching = [c for c in result["candidates"] if c["game_id"] == forza]
        self.assertEqual(len(matching), 1)

    async def test_inconsistent_primary_nested_insubstantial_row_renests(self):
        base = await seed_game("Cool Game")
        desync = await seed_game(
            "Cool Game: Season Pass", content_type="dlc", is_primary_library_item=1
        )

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        bucket = {
            c["game_id"]: c
            for c in self._by_reason(result, "inconsistent_primary_nested")
        }
        self.assertIn(desync, bucket)
        # Re-applying the nested content_type re-derives is_primary=0; the
        # resolvable parent rides along so it doesn't just become needs_parent.
        self.assertEqual(
            bucket[desync]["suggested_update"],
            {"game_id": desync, "content_type": "dlc", "parent_game_id": base},
        )

    async def test_inconsistent_primary_nested_skips_pinned_content_type(self):
        pinned = await seed_game(
            "Pinned Contradiction", content_type="edition", is_primary_library_item=1
        )
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET manual_overrides = ? WHERE id = ?",
                ('["content_type"]', pinned),
            )
            await db.commit()

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        self.assertNotIn(
            pinned,
            [
                c["game_id"]
                for c in self._by_reason(result, "inconsistent_primary_nested")
            ],
        )

    async def test_needs_parent_skips_desync_rows(self):
        # An is_primary=0 row with a PRIMARY content_type is a desync artifact;
        # its parent-only suggested_update would be rejected by update_game, so
        # the bucket is restricted to genuinely nested content_types.
        await seed_game("Desync Base")
        desync = await seed_game(
            "Desync Base: Weird Row",
            content_type="base_game",
            is_primary_library_item=0,
        )

        result = await admin.detect_misclassified_dlc(probe_steam=False)
        needs_parent_ids = {c["game_id"] for c in self._by_reason(result, "needs_parent")}
        self.assertNotIn(desync, needs_parent_ids)

    async def test_addon_name_pattern_excludes_content_type_override(self):
        from gamelib_mcp.tools import platforms

        pinned = await seed_game("Halo Season Pass")
        # User pins content_type via update_game — the detector must not nag.
        await platforms.update_game(game_id=pinned, content_type="base_game")

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        self.assertNotIn(
            pinned, [c["game_id"] for c in self._by_reason(result, "addon_name_pattern")]
        )

    async def test_purchase_minted_suspect_flagged(self):
        await seed_game("Hollow Knight")
        phantom = await seed_game("Hollow Knight: Voidheart Pack")
        gpid = await add_platform(phantom, "steam", owned=1)
        await db_module.set_platform_acquisition(gpid, {"purchase_source": "humble"})
        # No identifier, no igdb_id — the phantom shape.

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        suspects = {c["game_id"]: c for c in self._by_reason(result, "purchase_minted_suspect")}
        self.assertIn(phantom, suspects)
        cand = suspects[phantom]
        self.assertEqual(cand["evidence"]["purchase_source"], "humble")
        self.assertEqual(cand["suggested_update"]["content_type"], "dlc")
        self.assertEqual(cand["suggested_update"]["game_id"], phantom)
        # parent resolved via the "Hollow Knight" prefix.
        self.assertEqual(cand["evidence"]["parent_name"], "Hollow Knight")

    async def test_purchase_minted_not_flagged_when_identifier_present(self):
        await seed_game("Hollow Knight")
        owned = await seed_game("Hollow Knight: Voidheart Pack")
        gpid = await add_platform(owned, "steam", owned=1)
        await db_module.set_platform_acquisition(gpid, {"purchase_source": "humble"})
        await add_identifier(gpid, "steam_appid", "424481")

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        self.assertNotIn(
            owned, [c["game_id"] for c in self._by_reason(result, "purchase_minted_suspect")]
        )

    async def test_bucket_dedup_nested_addon_name_lands_in_needs_parent(self):
        await seed_game("Cool Game")
        child = await seed_game(
            "Cool Game: Season Pass", content_type="dlc", is_primary_library_item=0
        )

        result = await admin.detect_misclassified_dlc(probe_steam=False)

        matching = [c for c in result["candidates"] if c["game_id"] == child]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["reason"], "needs_parent")

    async def test_probe_flags_steam_type_mismatch(self):
        base = await seed_game("Base Game")
        game = await make_steam_game("Mysterious Content", 555)

        payload = {"type": "dlc", "fullgame": {"appid": 999, "name": "Base Game"}}
        with patch.object(admin, "_fetch_steam_appdetails", AsyncMock(return_value=payload)):
            result = await admin.detect_misclassified_dlc(limit=5, probe_steam=True)

        mismatches = {c["game_id"]: c for c in self._by_reason(result, "steam_type_mismatch")}
        self.assertIn(game, mismatches)
        cand = mismatches[game]
        self.assertEqual(cand["evidence"]["steam_type"], "dlc")
        self.assertEqual(
            cand["suggested_update"],
            {"game_id": game, "content_type": "dlc", "parent_game_id": base},
        )
        self.assertGreaterEqual(result["probed"], 1)

    async def test_probe_respects_cap(self):
        await make_steam_game("Alpha Thing", 111)
        await make_steam_game("Beta Thing", 222)

        payload = {"type": "dlc", "fullgame": {"appid": 999, "name": "Unknown"}}
        with patch.object(
            admin, "_fetch_steam_appdetails", AsyncMock(return_value=payload)
        ) as fetch_mock:
            result = await admin.detect_misclassified_dlc(limit=1, probe_steam=True)

        self.assertEqual(result["probed"], 1)
        self.assertEqual(result["probe_remaining"], 1)
        self.assertEqual(result["next_probe_offset"], 1)
        self.assertEqual(len(self._by_reason(result, "steam_type_mismatch")), 1)
        self.assertEqual(fetch_mock.await_count, 1)

    async def test_probe_offset_walks_distinct_rows(self):
        # The tool is read-only so the ordering never changes between calls —
        # the walk advances by passing next_probe_offset back as probe_offset.
        await make_steam_game("Walk One", 111)
        await make_steam_game("Walk Two", 222)
        await make_steam_game("Walk Three", 333)

        seen: list[int] = []

        async def fake_fetch(appid):
            seen.append(appid)
            return {"type": "game"}

        offset = 0
        with patch.object(
            admin, "_fetch_steam_appdetails", AsyncMock(side_effect=fake_fetch)
        ):
            for _ in range(3):
                result = await admin.detect_misclassified_dlc(
                    limit=1, probe_steam=True, probe_offset=offset
                )
                self.assertEqual(result["probed"], 1)
                if result["next_probe_offset"] is None:
                    break
                offset = result["next_probe_offset"]

        self.assertEqual(sorted(seen), [111, 222, 333])
        self.assertEqual(len(set(seen)), 3)
        self.assertIsNone(result["next_probe_offset"])
        self.assertEqual(result["probe_remaining"], 0)

    async def test_probe_limit_zero_probes_all(self):
        # limit=0 = no cap (sibling detector convention) — probe everything.
        await make_steam_game("All One", 111)
        await make_steam_game("All Two", 222)

        with patch.object(
            admin, "_fetch_steam_appdetails", AsyncMock(return_value={"type": "game"})
        ) as fetch_mock:
            result = await admin.detect_misclassified_dlc(limit=0, probe_steam=True)

        self.assertEqual(result["probed"], 2)
        self.assertEqual(fetch_mock.await_count, 2)
        self.assertEqual(result["probe_remaining"], 0)
        self.assertIsNone(result["next_probe_offset"])

    async def test_probe_fetch_error_is_skipped(self):
        err_game = await make_steam_game("Err Game", 111)
        ok_game = await make_steam_game("Ok Game", 222)

        async def fake_fetch(appid):
            if appid == 111:
                raise RuntimeError("boom")
            return {"type": "dlc", "fullgame": {"appid": 999, "name": "Unknown"}}

        with patch.object(admin, "_fetch_steam_appdetails", AsyncMock(side_effect=fake_fetch)):
            result = await admin.detect_misclassified_dlc(limit=5, probe_steam=True)

        self.assertEqual(result["probed"], 2)
        self.assertEqual([s["steam_appid"] for s in result["skipped"]], ["111"])
        self.assertEqual(
            [c["game_id"] for c in self._by_reason(result, "steam_type_mismatch")], [ok_game]
        )
        self.assertNotEqual(err_game, ok_game)

    async def test_probe_steam_false_does_no_fetch(self):
        await make_steam_game("Some Game", 555)

        with patch.object(admin, "_fetch_steam_appdetails", AsyncMock()) as fetch_mock:
            result = await admin.detect_misclassified_dlc(probe_steam=False)

        fetch_mock.assert_not_awaited()
        self.assertEqual(result["probed"], 0)
        self.assertEqual(result["probe_remaining"], 0)

    async def test_repair_loop_applies_suggested_update(self):
        from gamelib_mcp.data.db.queries import load_related_content_for_games
        from gamelib_mcp.tools import platforms

        base = await seed_game("Repairable Base")
        child = await seed_game(
            "Repairable Base: Story DLC", content_type="dlc", is_primary_library_item=0
        )

        result = await admin.detect_misclassified_dlc(probe_steam=False)
        cand = self._by_reason(result, "needs_parent")[0]
        self.assertEqual(cand["game_id"], child)

        # Replay the suggestion through the real update_game — the repair loop.
        await platforms.update_game(**cand["suggested_update"])

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT parent_game_id, content_type, manual_overrides FROM games WHERE id = ?",
                (child,),
            )
        self.assertEqual(row["parent_game_id"], base)
        self.assertEqual(row["content_type"], "dlc")
        self.assertIn("parent_game_id", json.loads(row["manual_overrides"]))

        related = await load_related_content_for_games([base])
        self.assertIn(child, [entry["game_id"] for entry in related[base]["dlc"]])

    async def test_full_run_is_read_only(self):
        await seed_game("Base Thing")
        await seed_game(
            "Base Thing: The Extra", content_type="dlc", is_primary_library_item=0
        )
        await seed_game("Elden Ring Season Pass")
        await make_steam_game("Mysterious Content", 555)

        async def snapshot() -> list[tuple]:
            async with db_module.get_db() as db:
                rows = await db.execute_fetchall(
                    "SELECT id, content_type, parent_game_id, is_primary_library_item, "
                    "manual_overrides FROM games ORDER BY id"
                )
            return [tuple(r) for r in rows]

        before = await snapshot()
        with patch.object(admin, "_fetch_steam_appdetails", AsyncMock(return_value=None)):
            await admin.detect_misclassified_dlc(limit=10, probe_steam=True)
        after = await snapshot()

        self.assertEqual(before, after)


class DeleteGameTests(ToolDBTestCase):
    async def test_preview_does_not_delete(self):
        gid = await seed_game("Preview Me")
        gpid = await add_platform(gid, "steam", playtime_minutes=100)
        await add_identifier(gpid, "steam_appid", "111")
        await add_rating(gid, "manual", 8.0, 8.0)

        result = await admin.delete_game(game_id=gid)

        self.assertFalse(result["deleted"])
        self.assertEqual(result["would_delete"]["platforms"], 1)
        self.assertEqual(result["would_delete"]["ratings"], 1)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games WHERE id = ?", (gid,))
        self.assertEqual(row["c"], 1)  # still present

    async def test_confirm_deletes_and_cascades(self):
        gid = await seed_game("Erase Me")
        gpid = await add_platform(gid, "steam", playtime_minutes=100)
        await add_identifier(gpid, "steam_appid", "222")
        await add_rating(gid, "manual", 7.0, 7.0)
        await db_module.upsert_wishlist_entry(gid, "steam", source="manual")
        await _insert_play_history(gid, "steam", "2026-01-01", 100)

        result = await admin.delete_game(game_id=gid, confirm=True)
        self.assertTrue(result["deleted"])

        async with db_module.get_db() as db:
            games = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games WHERE id = ?", (gid,))
            plats = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM game_platforms WHERE game_id = ?", (gid,)
            )
            idents = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM game_platform_identifiers WHERE game_platform_id = ?",
                (gpid,),
            )
            ratings = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM ratings WHERE game_id = ?", (gid,)
            )
            wishlist = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM game_wishlist WHERE game_id = ?", (gid,)
            )
            history = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM play_history WHERE game_id = ?", (gid,)
            )
        self.assertEqual(games["c"], 0)
        self.assertEqual(plats["c"], 0)
        self.assertEqual(idents["c"], 0)  # cascaded from game_platforms
        self.assertEqual(ratings["c"], 0)  # explicitly deleted (no cascade)
        self.assertEqual(wishlist["c"], 0)  # cascaded from games
        self.assertEqual(history["c"], 0)  # cascaded from games

    async def test_refuses_parent_with_children(self):
        parent = await seed_game("Base Game")
        await seed_game(
            "Base Game DLC", content_type="dlc", is_primary_library_item=0,
            parent_game_id=parent,
        )
        with self.assertRaisesRegex(ToolError, "nested item"):
            await admin.delete_game(game_id=parent, confirm=True)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM games WHERE id = ?", (parent,)
            )
        self.assertEqual(row["c"], 1)  # not deleted

    async def test_not_found(self):
        with self.assertRaisesRegex(ToolError, "not found|not in library"):
            await admin.delete_game(game_id=999999, confirm=True)

    async def test_recomputes_affinity_even_without_ratings(self):
        # An unrated but played game still contributes a playtime pseudo-rating to
        # tag_affinity, so deletion must recompute regardless of ratings.
        gid = await seed_game("Played Unrated", tags=["roguelike"])
        await add_platform(gid, "steam", playtime_minutes=600)
        with patch(
            "gamelib_mcp.data.db.recompute_tag_affinity", AsyncMock()
        ) as recompute:
            await admin.delete_game(game_id=gid, confirm=True)
        recompute.assert_awaited_once()

    async def test_preview_does_not_recompute_affinity(self):
        gid = await seed_game("Untouched", tags=["roguelike"])
        await add_platform(gid, "steam", playtime_minutes=600)
        with patch(
            "gamelib_mcp.data.db.recompute_tag_affinity", AsyncMock()
        ) as recompute:
            await admin.delete_game(game_id=gid)  # confirm=False preview
        recompute.assert_not_awaited()


class DeleteGamesBatchTests(ToolDBTestCase):
    async def _seed_two(self):
        a = await seed_game("Doomed A")
        gpid = await add_platform(a, "steam", playtime_minutes=100)
        await add_identifier(gpid, "steam_appid", "901")
        await add_rating(a, "manual", 8.0, 8.0)
        b = await seed_game("Doomed B")
        await add_platform(b, "gog")
        return a, b

    async def test_preview_totals_match_confirmed_deletes(self):
        a, b = await self._seed_two()
        items = [{"game_id": a}, {"game_id": b}]

        preview = await admin.delete_games_batch(items)
        self.assertFalse(preview["confirm"])
        self.assertEqual(
            [r["status"] for r in preview["results"]], ["previewed", "previewed"]
        )
        self.assertEqual(preview["previewed"], 2)
        self.assertIn("hint", preview)
        self.assertEqual(preview["would_delete_total"]["platforms"], 2)
        self.assertEqual(preview["would_delete_total"]["ratings"], 1)
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 2)  # nothing deleted

    async def test_confirm_deletes_and_counts_equal_preview(self):
        a, b = await self._seed_two()
        items = [{"game_id": a}, {"game_id": b}]
        preview = await admin.delete_games_batch(items)

        confirmed = await admin.delete_games_batch(items, confirm=True)
        self.assertEqual(
            [r["status"] for r in confirmed["results"]], ["deleted", "deleted"]
        )
        self.assertEqual(confirmed["deleted"], 2)
        self.assertEqual(
            confirmed["deleted_counts_total"], preview["would_delete_total"]
        )
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 0)

    async def test_refused_parent_never_aborts_the_rest(self):
        parent = await seed_game("Base Game")
        await seed_game(
            "Base DLC", content_type="dlc", parent_game_id=parent,
            is_primary_library_item=0,
        )
        plain = await seed_game("Plain Game")

        result = await admin.delete_games_batch(
            [{"game_id": parent}, {"game_id": plain}], confirm=True
        )
        self.assertEqual(
            [r["status"] for r in result["results"]], ["refused", "deleted"]
        )
        self.assertEqual(result["refused"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(
            result["results"][0]["children"],
            [{"game_id": result["results"][0]["children"][0]["game_id"], "name": "Base DLC"}],
        )
        async with db_module.get_db() as db:
            parent_row = await db.execute_fetchone(
                "SELECT id FROM games WHERE id = ?", (parent,)
            )
            plain_row = await db.execute_fetchone(
                "SELECT id FROM games WHERE id = ?", (plain,)
            )
        self.assertIsNotNone(parent_row)  # refused item untouched
        self.assertIsNone(plain_row)

    async def test_duplicate_item_errors_in_both_modes(self):
        gid = await seed_game("Once Only")
        items = [{"game_id": gid}, {"name": "Once Only"}]

        preview = await admin.delete_games_batch(items)
        self.assertEqual(
            [r["status"] for r in preview["results"]], ["previewed", "error"]
        )
        self.assertIn("already slated", preview["results"][1]["error"])

        confirmed = await admin.delete_games_batch(items, confirm=True)
        self.assertEqual(
            [r["status"] for r in confirmed["results"]], ["deleted", "error"]
        )

    async def test_same_name_items_never_drift_to_a_sibling_row(self):
        # Regression: names are pre-resolved BEFORE any deletion. Without that,
        # confirm-deleting "Dark Souls" made the second identical item
        # prefix-match "Dark Souls II" — deleting a game preview never showed.
        ds = await seed_game("Dark Souls")
        ds2 = await seed_game("Dark Souls II")
        items = [{"name": "Dark Souls"}, {"name": "Dark Souls"}]

        preview = await admin.delete_games_batch(items)
        self.assertEqual(
            [r["status"] for r in preview["results"]], ["previewed", "error"]
        )
        self.assertEqual(preview["results"][0]["game_id"], ds)
        self.assertIn("already slated", preview["results"][1]["error"])

        confirmed = await admin.delete_games_batch(items, confirm=True)
        self.assertEqual(
            [r["status"] for r in confirmed["results"]], ["deleted", "error"]
        )
        self.assertEqual(confirmed["results"][0]["game_id"], ds)
        async with db_module.get_db() as db:
            gone = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (ds,))
            kept = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (ds2,))
        self.assertIsNone(gone)
        self.assertIsNotNone(kept)  # the sibling row must survive

    async def test_child_then_parent_preview_matches_confirm(self):
        # Regression: the children guard runs net of ids earlier in the batch
        # in BOTH modes — preview must not refuse a parent whose child the
        # same batch deletes first.
        parent = await seed_game("Base Game")
        await add_platform(parent, "steam")
        child = await seed_game(
            "Base DLC", content_type="dlc", parent_game_id=parent,
            is_primary_library_item=0,
        )
        await add_platform(child, "steam")
        items = [{"game_id": child}, {"game_id": parent}]

        preview = await admin.delete_games_batch(items)
        self.assertEqual(
            [r["status"] for r in preview["results"]], ["previewed", "previewed"]
        )

        confirmed = await admin.delete_games_batch(items, confirm=True)
        self.assertEqual(
            [r["status"] for r in confirmed["results"]], ["deleted", "deleted"]
        )
        self.assertEqual(
            confirmed["deleted_counts_total"], preview["would_delete_total"]
        )
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 0)

    async def test_parent_then_child_order_still_refuses_parent(self):
        # The guard only ignores EARLIER batch items: [parent, child] refuses
        # the parent identically in preview and confirm.
        parent = await seed_game("Base Game")
        child = await seed_game(
            "Base DLC", content_type="dlc", parent_game_id=parent,
            is_primary_library_item=0,
        )
        items = [{"game_id": parent}, {"game_id": child}]
        preview = await admin.delete_games_batch(items)
        confirmed = await admin.delete_games_batch(items, confirm=True)
        self.assertEqual(
            [r["status"] for r in preview["results"]], ["refused", "previewed"]
        )
        self.assertEqual(
            [r["status"] for r in confirmed["results"]], ["refused", "deleted"]
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (parent,))
        self.assertIsNotNone(row)

    async def test_affinity_recomputed_once_after_confirmed_deletes(self):
        a, b = await self._seed_two()
        recompute = AsyncMock(return_value=0)
        with patch.object(db_module, "recompute_tag_affinity", recompute):
            await admin.delete_games_batch(
                [{"game_id": a}, {"game_id": b}], confirm=True
            )
        recompute.assert_awaited_once()

    async def test_preview_never_recomputes_affinity(self):
        a, b = await self._seed_two()
        recompute = AsyncMock(return_value=0)
        with patch.object(db_module, "recompute_tag_affinity", recompute):
            await admin.delete_games_batch([{"game_id": a}, {"game_id": b}])
        recompute.assert_not_awaited()

    async def test_empty_and_cap_raise(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaisesRegex(ToolError, "must not be empty"):
            await admin.delete_games_batch([])
        with self.assertRaisesRegex(ToolError, "capped at 200"):
            await admin.delete_games_batch([{"game_id": 1}] * 201)


class MergeGamesBatchTests(ToolDBTestCase):
    async def test_merges_and_flags_stale_ids(self):
        a = await seed_game("Dupe A")
        await add_platform(a, "steam", playtime_minutes=50)
        b = await seed_game("Canonical B")
        c = await seed_game("Other C")

        result = await admin.merge_games_batch(
            [
                {"source_game_id": a, "target_game_id": b},
                # a was merged away above — both directions must flag stale.
                {"source_game_id": a, "target_game_id": c},
                {"source_game_id": c, "target_game_id": a},
            ]
        )
        self.assertEqual(
            [r["status"] for r in result["results"]], ["ok", "stale_id", "stale_id"]
        )
        self.assertEqual(result["ok"], 1)
        self.assertEqual(result["stale_id"], 2)
        async with db_module.get_db() as db:
            gone = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (a,))
            gp = await db.execute_fetchone(
                "SELECT platform FROM game_platforms WHERE game_id = ?", (b,)
            )
            survivor = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (c,))
        self.assertIsNone(gone)
        self.assertEqual(gp["platform"], "steam")
        self.assertIsNotNone(survivor)  # stale items touched nothing

    async def test_dry_run_predicts_stale_and_writes_nothing(self):
        a = await seed_game("Dupe A")
        b = await seed_game("Canonical B")
        c = await seed_game("Other C")
        result = await admin.merge_games_batch(
            [
                {"source_game_id": a, "target_game_id": b},
                {"source_game_id": a, "target_game_id": c},
            ],
            dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(
            [r["status"] for r in result["results"]], ["ok", "stale_id"]
        )
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 3)

    async def test_dry_run_flags_chained_pairs(self):
        a = await seed_game("Dupe A")
        await add_platform(a, "steam", playtime_minutes=50)
        b = await seed_game("Middle B")
        c = await seed_game("Canonical C")

        chain = [
            {"source_game_id": a, "target_game_id": b},
            {"source_game_id": b, "target_game_id": c},
        ]
        preview = await admin.merge_games_batch(chain, dry_run=True)
        self.assertEqual(
            [r["status"] for r in preview["results"]], ["ok", "ok"]
        )
        self.assertNotIn("chained_preview", preview["results"][0])
        # B→C reads the pre-batch DB (A's rows not yet merged into B), so
        # its counts may understate the wet run and must say so.
        self.assertTrue(preview["results"][1]["chained_preview"])
        # A wet chained run reads real post-merge state — no flag, and the
        # second merge carries A's platform row through B into C.
        wet = await admin.merge_games_batch(chain)
        self.assertEqual([r["status"] for r in wet["results"]], ["ok", "ok"])
        self.assertNotIn("chained_preview", wet["results"][1])
        self.assertEqual(wet["results"][1]["platforms_moved"], ["steam"])
        async with db_module.get_db() as db:
            gp = await db.execute_fetchone(
                "SELECT platform, playtime_minutes FROM game_platforms"
                " WHERE game_id = ?",
                (c,),
            )
        self.assertEqual(gp["platform"], "steam")
        self.assertEqual(gp["playtime_minutes"], 50)

    async def test_error_isolation_and_missing_ids(self):
        a = await seed_game("Dupe A")
        b = await seed_game("Canonical B")
        result = await admin.merge_games_batch(
            [
                {"source_game_id": 99999, "target_game_id": b},
                {"source_game_id": a},  # target missing
                {"source_game_id": a, "target_game_id": a},  # same id
                {"source_game_id": a, "target_game_id": b},
            ]
        )
        self.assertEqual(
            [r["status"] for r in result["results"]],
            ["error", "error", "error", "ok"],
        )
        self.assertEqual(result["errors"], 3)
        self.assertEqual(result["ok"], 1)

    async def test_affinity_recomputed_once_when_ratings_move(self):
        a = await seed_game("Dupe A")
        await add_rating(a, "manual", 9.0, 9.0)
        b = await seed_game("Canonical B")
        c = await seed_game("Dupe C")
        await add_rating(c, "manual", 7.0, 7.0)
        d = await seed_game("Canonical D")
        recompute = AsyncMock(return_value=2)
        with patch.object(db_module, "recompute_tag_affinity", recompute):
            result = await admin.merge_games_batch(
                [
                    {"source_game_id": a, "target_game_id": b},
                    {"source_game_id": c, "target_game_id": d},
                ]
            )
        recompute.assert_awaited_once()
        self.assertEqual(result["tag_affinity_tags_updated"], 2)

    async def test_empty_and_cap_raise(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaisesRegex(ToolError, "must not be empty"):
            await admin.merge_games_batch([])
        with self.assertRaisesRegex(ToolError, "capped at 200"):
            await admin.merge_games_batch(
                [{"source_game_id": 1, "target_game_id": 2}] * 201
            )
