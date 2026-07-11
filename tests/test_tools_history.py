"""Tests for gamelib_mcp.tools.history.get_play_history."""

from fastmcp.exceptions import ToolError

from conftest import ToolDBTestCase, add_identifier, add_platform, seed_game, make_steam_game
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import history


async def _snapshot(game_id: int, platform: str, day: str, minutes: int) -> None:
    async with db_module.get_db() as db:
        await db.execute(
            """INSERT INTO play_history (game_id, platform, snapshot_date, playtime_minutes)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(game_id, platform, snapshot_date)
                   DO UPDATE SET playtime_minutes = excluded.playtime_minutes""",
            (game_id, platform, day, minutes),
        )
        await db.commit()


def _nps_row(app_id: str, day: str, minutes: int) -> dict:
    return {
        "device_id": "device-1",
        "application_id": app_id,
        "period_type": "day",
        "period_key": day,
        "playtime_minutes": minutes,
        "app_name": None,
    }


class GetPlayHistoryTests(ToolDBTestCase):
    async def test_delta_with_baseline_before_window(self):
        game_id = await seed_game("Hades")
        await add_platform(game_id, "steam", playtime_minutes=100)
        await _snapshot(game_id, "steam", "2026-06-25", 100)  # baseline before window
        await _snapshot(game_id, "steam", "2026-07-02", 160)  # inside window

        result = await history.get_play_history(
            start_date="2026-07-01", end_date="2026-07-03", platform="steam"
        )

        self.assertEqual(result["total_minutes"], 60)
        self.assertEqual(result["games"][0]["game_id"], game_id)
        self.assertEqual(result["games"][0]["minutes_played"], 60)
        self.assertAlmostEqual(result["games"][0]["hours_played"], 1.0)
        self.assertEqual(result["by_platform"], {"steam": 60})
        self.assertEqual(result["window"], {"start": "2026-07-01", "end": "2026-07-03"})

    async def test_delta_when_first_snapshot_falls_inside_window(self):
        # No snapshot before the window: only growth after the first in-window
        # snapshot is attributable, so the first snapshot itself contributes 0.
        game_id = await seed_game("Celeste")
        await add_platform(game_id, "steam", playtime_minutes=200)
        await _snapshot(game_id, "steam", "2026-07-01", 50)
        await _snapshot(game_id, "steam", "2026-07-03", 90)

        result = await history.get_play_history(
            start_date="2026-07-01", end_date="2026-07-03", platform="steam"
        )

        self.assertEqual(result["total_minutes"], 40)

    async def test_no_growth_produces_no_entry(self):
        game_id = await seed_game("Dormant Game")
        await add_platform(game_id, "steam", playtime_minutes=10)
        await _snapshot(game_id, "steam", "2026-06-20", 10)
        await _snapshot(game_id, "steam", "2026-07-02", 10)

        result = await history.get_play_history(
            start_date="2026-07-01", end_date="2026-07-03", platform="steam"
        )

        self.assertEqual(result["total_minutes"], 0)
        self.assertEqual(result["games"], [])

    async def test_switch2_uses_nintendo_play_summary(self):
        game_id = await seed_game("Mario Kart World")
        pid = await add_platform(game_id, "switch2", owned=1)
        await add_identifier(pid, "nintendo_title_id", "0100AAA")
        await db_module.upsert_nintendo_play_summary([
            _nps_row("0100AAA", "2026-07-01", 30),
            _nps_row("0100AAA", "2026-07-02", 15),
            _nps_row("0100AAA", "2026-06-15", 999),  # outside window
        ])

        result = await history.get_play_history(
            start_date="2026-07-01", end_date="2026-07-03", platform="switch2"
        )

        self.assertEqual(result["total_minutes"], 45)
        self.assertEqual(result["games"][0]["platform"], "switch2")
        self.assertEqual(result["switch2_unmatched_minutes"], 0)

    async def test_switch2_unmatched_minutes_reported(self):
        await db_module.upsert_nintendo_play_summary([
            _nps_row("UNKNOWN_APP", "2026-07-01", 25),
        ])

        result = await history.get_play_history(start_date="2026-07-01", end_date="2026-07-03")

        self.assertEqual(result["switch2_unmatched_minutes"], 25)
        self.assertEqual(result["total_minutes"], 0)

    async def test_platform_filter_excludes_other_platforms(self):
        steam_game = await seed_game("Steam Game")
        await add_platform(steam_game, "steam", playtime_minutes=100)
        await _snapshot(steam_game, "steam", "2026-07-01", 50)
        await _snapshot(steam_game, "steam", "2026-07-02", 100)

        switch_game = await seed_game("Switch Game")
        pid = await add_platform(switch_game, "switch2", owned=1)
        await add_identifier(pid, "nintendo_title_id", "0100BBB")
        await db_module.upsert_nintendo_play_summary([
            _nps_row("0100BBB", "2026-07-01", 60),
        ])

        result = await history.get_play_history(
            start_date="2026-07-01", end_date="2026-07-03", platform="steam"
        )

        self.assertEqual(result["by_platform"], {"steam": 50})
        self.assertNotIn("switch2", result["by_platform"])

    async def test_combines_platforms_when_unfiltered(self):
        steam_game = await seed_game("Steam Game")
        await add_platform(steam_game, "steam", playtime_minutes=100)
        await _snapshot(steam_game, "steam", "2026-07-01", 50)
        await _snapshot(steam_game, "steam", "2026-07-02", 100)

        switch_game = await seed_game("Switch Game")
        pid = await add_platform(switch_game, "switch2", owned=1)
        await add_identifier(pid, "nintendo_title_id", "0100BBB")
        await db_module.upsert_nintendo_play_summary([
            _nps_row("0100BBB", "2026-07-01", 60),
        ])

        result = await history.get_play_history(
            start_date="2026-07-01", end_date="2026-07-03"
        )

        self.assertEqual(result["by_platform"], {"steam": 50, "switch2": 60})
        self.assertEqual(result["total_minutes"], 110)
        self.assertEqual(result["games"][0]["name"], "Switch Game")  # most-played first

    async def test_empty_window_returns_zeros(self):
        result = await history.get_play_history(start_date="2026-07-01", end_date="2026-07-03")

        self.assertEqual(result["total_minutes"], 0)
        self.assertEqual(result["total_hours"], 0)
        self.assertEqual(result["by_platform"], {})
        self.assertEqual(result["games"], [])
        self.assertEqual(result["switch2_unmatched_minutes"], 0)

    async def test_invalid_start_date_raises_tool_error(self):
        with self.assertRaises(ToolError):
            await history.get_play_history(start_date="not-a-date", end_date="2026-07-03")

    async def test_invalid_end_date_raises_tool_error(self):
        with self.assertRaises(ToolError):
            await history.get_play_history(start_date="2026-07-01", end_date="not-a-date")

    async def test_start_after_end_raises_tool_error(self):
        with self.assertRaises(ToolError):
            await history.get_play_history(start_date="2026-07-05", end_date="2026-07-01")

    async def test_unknown_platform_raises_tool_error(self):
        with self.assertRaises(ToolError):
            await history.get_play_history(platform="playstation")

    async def test_limit_truncates_games_but_not_totals(self):
        for i in range(3):
            gid = await seed_game(f"Game {i}")
            await add_platform(gid, "steam", playtime_minutes=(i + 1) * 10)
            await _snapshot(gid, "steam", "2026-07-01", 0)
            await _snapshot(gid, "steam", "2026-07-02", (i + 1) * 10)

        result = await history.get_play_history(
            start_date="2026-07-01", end_date="2026-07-03", platform="steam", limit=1
        )

        self.assertEqual(len(result["games"]), 1)
        self.assertEqual(result["total_minutes"], 60)  # 10 + 20 + 30
        self.assertEqual(result["games"][0]["minutes_played"], 30)  # most-played first

    async def test_days_default_computes_start_from_end(self):
        result = await history.get_play_history(days=7)

        self.assertEqual(result["total_minutes"], 0)
        window = result["window"]
        self.assertTrue(window["start"] <= window["end"])

    async def test_nested_dlc_row_with_playtime_appears_in_history(self):
        # Regression: a nested (dlc) row with owned platform and playtime
        # snapshots must appear in get_play_history deltas like any other row.
        # This tests the pass-through behavior: platforms report what they
        # report without special handling.
        parent_id = await make_steam_game("Portal 2", 100, playtime_minutes=100)
        dlc_id = await seed_game(
            "Portal 2: Peer Review",
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        await add_platform(dlc_id, "steam", playtime_minutes=50)
        await _snapshot(dlc_id, "steam", "2026-06-25", 20)  # baseline before window
        await _snapshot(dlc_id, "steam", "2026-07-02", 50)  # inside window

        result = await history.get_play_history(
            start_date="2026-07-01", end_date="2026-07-03", platform="steam"
        )

        self.assertEqual(result["total_minutes"], 30)  # 50 - 20
        self.assertEqual(len(result["games"]), 1)
        self.assertEqual(result["games"][0]["game_id"], dlc_id)
        self.assertEqual(result["games"][0]["name"], "Portal 2: Peer Review")
        self.assertEqual(result["games"][0]["minutes_played"], 30)
