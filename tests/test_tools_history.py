"""Tests for gamelib_mcp.tools.history.get_play_history."""

from conftest import (
    ToolDBTestCase,
    add_identifier,
    add_platform,
    make_steam_game,
    seed_game,
)
from fastmcp.exceptions import ToolError

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


async def _set_last_played(game_id: int, platform: str, day: str | None) -> None:
    async with db_module.get_db() as db:
        await db.execute(
            "UPDATE game_platforms SET last_played = ? WHERE game_id = ? AND platform = ?",
            (day, game_id, platform),
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


class LastPlayedGateTests(ToolDBTestCase):
    """A stored-total correction must not read as play.

    Snapshots are cumulative, so a delta between two of them lands in whichever
    window the LATER snapshot fell in — regardless of when the game was actually
    played. The PSN cross-gen SKU fix stepped seven totals up at once and Ghost
    of Tsushima reported 81 hours "played" in a month it was never launched.
    game_platforms.last_played is the source's own statement of when play
    stopped, so it settles the question the totals cannot.
    """

    async def test_correction_after_last_played_is_suppressed(self):
        game_id = await seed_game("Ghost of Tsushima")
        await add_platform(game_id, "ps5", playtime_minutes=4933)
        await _set_last_played(game_id, "ps5", "2022-09-21")
        await _snapshot(game_id, "ps5", "2026-07-04", 46)
        await _snapshot(game_id, "ps5", "2026-08-02", 4933)  # the SKU-sum fix

        result = await history.get_play_history(
            start_date="2026-07-03", end_date="2026-08-02"
        )

        self.assertEqual(result["total_minutes"], 0)
        self.assertEqual(result["games"], [])
        # Suppressed, not silently dropped.
        self.assertEqual(result["excluded_stale_games"], 1)
        self.assertEqual(result["excluded_stale_minutes"], 4887)

    async def test_real_play_inside_window_is_kept(self):
        game_id = await seed_game("Blue Prince")
        await add_platform(game_id, "ps5", playtime_minutes=200)
        await _set_last_played(game_id, "ps5", "2026-07-20")
        await _snapshot(game_id, "ps5", "2026-07-04", 120)
        await _snapshot(game_id, "ps5", "2026-08-02", 200)

        result = await history.get_play_history(
            start_date="2026-07-03", end_date="2026-08-02"
        )

        self.assertEqual(result["total_minutes"], 80)
        self.assertEqual(result["excluded_stale_games"], 0)
        self.assertEqual(result["excluded_stale_minutes"], 0)

    async def test_last_played_on_the_window_start_still_counts(self):
        # The boundary is inclusive: played ON the first day of the window.
        game_id = await seed_game("Split Fiction")
        await add_platform(game_id, "ps5", playtime_minutes=200)
        await _set_last_played(game_id, "ps5", "2026-07-03")
        await _snapshot(game_id, "ps5", "2026-07-04", 120)
        await _snapshot(game_id, "ps5", "2026-08-02", 200)

        result = await history.get_play_history(
            start_date="2026-07-03", end_date="2026-08-02"
        )

        self.assertEqual(result["total_minutes"], 80)
        self.assertEqual(result["excluded_stale_games"], 0)

    async def test_null_last_played_is_unknown_not_stale(self):
        # GOG reports no last-played at all, and Steam rows predating the
        # backfill have none either. Unknown must never mean "suppress" —
        # that would silently zero most of the library's history.
        game_id = await seed_game("Hades II")
        await add_platform(game_id, "steam", playtime_minutes=400)
        await _set_last_played(game_id, "steam", None)
        await _snapshot(game_id, "steam", "2026-07-04", 85)
        await _snapshot(game_id, "steam", "2026-08-02", 400)

        result = await history.get_play_history(
            start_date="2026-07-03", end_date="2026-08-02"
        )

        self.assertEqual(result["total_minutes"], 315)
        self.assertEqual(result["excluded_stale_games"], 0)

    async def test_gate_is_per_row_not_all_or_nothing(self):
        stale = await seed_game("Assassin's Creed Valhalla")
        await add_platform(stale, "ps5", playtime_minutes=149)
        await _set_last_played(stale, "ps5", "2023-03-19")
        await _snapshot(stale, "ps5", "2026-07-04", 21)
        await _snapshot(stale, "ps5", "2026-08-02", 149)

        live = await seed_game("Rusty's Retirement")
        await add_platform(live, "steam", playtime_minutes=600)
        await _set_last_played(live, "steam", "2026-07-30")
        await _snapshot(live, "steam", "2026-07-04", 92)
        await _snapshot(live, "steam", "2026-08-02", 600)

        result = await history.get_play_history(
            start_date="2026-07-03", end_date="2026-08-02"
        )

        self.assertEqual([g["name"] for g in result["games"]], ["Rusty's Retirement"])
        self.assertEqual(result["total_minutes"], 508)
        self.assertEqual(result["by_platform"], {"steam": 508})
        self.assertEqual(result["excluded_stale_games"], 1)
        self.assertEqual(result["excluded_stale_minutes"], 128)

    async def test_historical_window_before_the_correction_is_unaffected(self):
        # A window that ends before the correcting sync never saw the step, and
        # the gate must not retroactively suppress the real growth it contains.
        game_id = await seed_game("Horizon Zero Dawn")
        await add_platform(game_id, "ps5", playtime_minutes=449)
        await _set_last_played(game_id, "ps5", "2026-06-04")
        await _snapshot(game_id, "ps5", "2026-05-01", 300)
        await _snapshot(game_id, "ps5", "2026-06-04", 395)
        await _snapshot(game_id, "ps5", "2026-08-02", 449)

        result = await history.get_play_history(
            start_date="2026-05-01", end_date="2026-06-30"
        )

        self.assertEqual(result["total_minutes"], 95)
        self.assertEqual(result["excluded_stale_games"], 0)

    async def test_platform_filter_scopes_the_excluded_counters(self):
        stale = await seed_game("Dreams")
        await add_platform(stale, "ps5", playtime_minutes=86)
        await _set_last_played(stale, "ps5", "2022-05-22")
        await _snapshot(stale, "ps5", "2026-07-04", 1)
        await _snapshot(stale, "ps5", "2026-08-02", 86)

        live = await seed_game("Slay the Spire 2")
        await add_platform(live, "steam", playtime_minutes=400)
        await _set_last_played(live, "steam", "2026-07-28")
        await _snapshot(live, "steam", "2026-07-04", 84)
        await _snapshot(live, "steam", "2026-08-02", 400)

        steam_only = await history.get_play_history(
            start_date="2026-07-03", end_date="2026-08-02", platform="steam"
        )
        self.assertEqual(steam_only["total_minutes"], 316)
        self.assertEqual(steam_only["excluded_stale_games"], 0)

        ps5_only = await history.get_play_history(
            start_date="2026-07-03", end_date="2026-08-02", platform="ps5"
        )
        self.assertEqual(ps5_only["total_minutes"], 0)
        self.assertEqual(ps5_only["excluded_stale_games"], 1)
        self.assertEqual(ps5_only["excluded_stale_minutes"], 85)
