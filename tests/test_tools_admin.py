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
