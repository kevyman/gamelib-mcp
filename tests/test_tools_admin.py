"""Characterization tests for gamelib_mcp.tools.admin.

Covers detect_farmed_games (pure DB) and set_nintendo_session, including its
successful file-write path (writes to a temp NINTENDO_COOKIES_FILE).
"""

import asyncio
import json

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

        self.assertEqual(result["steam"], {"platform": "steam"})
        self.assertEqual(result["epic"], {"platform": "epic"})
        self.assertEqual(ctx.progress, [(0, 2), (1, 2), (2, 2)])
        self.assertIn("Refreshing 2 platform(s)", ctx.infos)
        self.assertIn("Finished steam refresh", ctx.infos)
        self.assertIn("Finished epic refresh", ctx.infos)


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

        self.assertEqual(result["steam"], {"games_upserted": 3})
        self.assertEqual(await get_meta("sync_platform_state_steam"), "done")
        self.assertEqual(await get_meta("library_sync_status"), "idle")

    async def test_marks_platform_error_on_failure(self):
        with patch("gamelib_mcp.tools.admin.fetch_library", AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("gamelib_mcp.tools.admin._schedule_background_enrich", AsyncMock()):
            result = await admin.run_library_sync(["steam"])

        self.assertIn("error", result["steam"])
        self.assertEqual(await get_meta("sync_platform_state_steam"), "error")
        self.assertEqual(await get_meta("library_sync_status"), "idle")


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


class GetSyncStatusTests(ToolDBTestCase):
    async def test_reports_idle_with_pending_platforms_when_never_synced(self):
        status = await admin.get_sync_status()
        self.assertEqual(status["status"], "idle")
        self.assertEqual(
            set(status["platforms"]), {"steam", "epic", "gog", "switch2", "ps5"}
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
            "source_deleted",
        }
        self.assertEqual(set(result.keys()), expected_keys)
        self.assertEqual(result["source"]["game_id"], src)
        self.assertEqual(result["target"]["game_id"], tgt)
