"""Tests for the Parental Controls playtime sync (data/nintendo_pctl.py).

Follows the repo pattern: unittest.IsolatedAsyncioTestCase over a real migrated
temp SQLite DB, with the network-bound fetch + IGDB resolution patched out so the
DB write/match/auto-create logic is exercised for real.
"""

import os
import unittest
from unittest import mock

from conftest import ToolDBTestCase, add_identifier, add_platform, seed_game

import gamelib_mcp.data.igdb as igdb_module
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import nintendo as nintendo_module
from gamelib_mcp.data import nintendo_pctl


async def _no_igdb(name, platform_id):
    """Force resolve_and_link_game down its offline create-by-name path."""
    return


def _row(app_id, date, minutes, name="Game"):
    return {
        "device_id": "device-1",
        "application_id": app_id,
        "period_type": "day",
        "period_key": date,
        "playtime_minutes": minutes,
        "app_name": name,
    }


class TestPctlTokenLoading(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_when_file_missing(self):
        with mock.patch.dict(
            os.environ, {"NINTENDO_PCTL_SESSION_FILE": "/nonexistent/pctl.json"}
        ):
            self.assertFalse(nintendo_pctl.is_pctl_configured())


class TestPctlSync(ToolDBTestCase):
    async def _run_sync(self, rows):
        with mock.patch.object(nintendo_pctl, "is_pctl_configured", return_value=True), \
             mock.patch.object(nintendo_pctl, "fetch_pctl_play_summaries", new=lambda: _async(rows)), \
             mock.patch.object(igdb_module, "resolve_game", new=_no_igdb):
            return await nintendo_pctl.sync_nintendo_pctl()

    async def _switch2_playtime(self, application_id):
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                """SELECT gp.playtime_minutes AS m, gp.owned AS owned, g.name AS name
                   FROM game_platforms gp
                   JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
                   JOIN games g ON g.id = gp.game_id
                   WHERE gp.platform = 'switch2'
                     AND gpi.identifier_type = 'nintendo_title_id'
                     AND gpi.identifier_value = ?""",
                (application_id,),
            )
        return row

    async def test_updates_existing_game_matched_by_title_id(self):
        gid = await seed_game("Mario Kart World")
        pid = await add_platform(gid, "switch2", owned=1)
        await add_identifier(pid, "nintendo_title_id", "0100AAA")

        result = await self._run_sync([
            _row("0100AAA", "2026-06-21", 30, "Mario Kart World"),
            _row("0100AAA", "2026-06-22", 15, "Mario Kart World"),
        ])

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["added"], 0)
        row = await self._switch2_playtime("0100AAA")
        self.assertEqual(row["m"], 45)  # 30 + 15 summed

    async def test_resync_is_idempotent(self):
        gid = await seed_game("Mario Kart World")
        pid = await add_platform(gid, "switch2", owned=1)
        await add_identifier(pid, "nintendo_title_id", "0100AAA")

        rows = [_row("0100AAA", "2026-06-21", 30), _row("0100AAA", "2026-06-22", 15)]
        await self._run_sync(rows)
        await self._run_sync(rows)  # same days again

        row = await self._switch2_playtime("0100AAA")
        self.assertEqual(row["m"], 45)  # not 90

    async def test_auto_creates_played_not_owned_game(self):
        # No game seeded — a title played on the console but owned on another
        # account. It should be created and linked as a normal owned switch2 game.
        result = await self._run_sync([
            _row("0100RBD", "2026-06-22", 120, "Mario + Rabbids Sparks of Hope"),
        ])

        self.assertEqual(result["added"], 1)
        row = await self._switch2_playtime("0100RBD")
        self.assertIsNotNone(row)
        self.assertEqual(row["m"], 120)
        self.assertEqual(row["owned"], 1)
        self.assertEqual(row["name"], "Mario + Rabbids Sparks of Hope")

    async def test_unconfigured_returns_classified_skip(self):
        with mock.patch.object(nintendo_pctl, "is_pctl_configured", return_value=False):
            result = await nintendo_pctl.sync_nintendo_pctl()
        self.assertEqual(result["sync_status"], "unconfigured")
        self.assertEqual(result["error_classification"], "missing_configuration")


class TestExtractRows(unittest.TestCase):
    """Locks the live API shape: playedGames[].meta.{applicationId,title} + top-level playingTime."""

    def test_parses_meta_nested_games_and_sums_across_players(self):
        summaries = [
            {
                "date": "2026-06-22",
                "result": "CALCULATING",  # current day is still included
                "players": [
                    {"profile": {"nickname": "A"}, "playedGames": [
                        {"meta": {"applicationId": "010067300059A000",
                                  "title": "Mario + Rabbids Kingdom Battle"}, "playingTime": 4},
                    ]},
                    {"profile": {"nickname": "B"}, "playedGames": [
                        {"meta": {"applicationId": "010067300059A000",
                                  "title": "Mario + Rabbids Kingdom Battle"}, "playingTime": 6},
                        {"meta": {"applicationId": "0100ZELDA", "title": "Zelda"}, "playingTime": 5},
                    ]},
                ],
            }
        ]
        rows = nintendo_pctl._extract_rows("dev1", summaries)
        by_app = {r["application_id"]: r for r in rows}
        self.assertEqual(by_app["010067300059A000"]["playtime_minutes"], 10)  # 4 + 6
        self.assertEqual(by_app["010067300059A000"]["app_name"], "Mario + Rabbids Kingdom Battle")
        self.assertEqual(by_app["0100ZELDA"]["playtime_minutes"], 5)
        self.assertTrue(all(r["device_id"] == "dev1" and r["period_type"] == "day" for r in rows))

    def test_skips_entries_without_an_application_id(self):
        summaries = [{"date": "2026-06-22", "players": [
            {"playedGames": [{"meta": {}, "playingTime": 4}]},
        ]}]
        self.assertEqual(nintendo_pctl._extract_rows("d", summaries), [])


class TestSyncWrapperErrorPropagation(unittest.IsolatedAsyncioTestCase):
    """sync_nintendo must surface real playtime failures so the control plane records them,
    without letting an unconfigured/working playtime layer flip switch2 to errored."""

    async def _run(self, ownership: dict, playtime: dict) -> dict:
        async def fake_own():
            return ownership

        async def fake_pctl():
            return playtime

        with mock.patch.object(nintendo_module, "_sync_nintendo_ownership", new=fake_own), \
             mock.patch("gamelib_mcp.data.nintendo_pctl.sync_nintendo_pctl", new=fake_pctl):
            return await nintendo_module.sync_nintendo()

    async def test_stale_playtime_surfaces_at_top_level_when_ownership_ok(self):
        result = await self._run(
            {"added": 0, "matched": 1, "skipped": 0},
            {"sync_status": "stale", "error_summary": "token expired",
             "error_classification": "auth_stale", "added": 0, "matched": 0, "titles": 0},
        )
        self.assertEqual(result["sync_status"], "stale")
        self.assertEqual(result["error_classification"], "auth_stale")
        self.assertIn("Parental Controls", result["error_summary"])
        self.assertEqual(result["playtime"]["sync_status"], "stale")  # still nested too

    async def test_unconfigured_playtime_does_not_mark_switch2_errored(self):
        result = await self._run(
            {"added": 1, "matched": 0, "skipped": 0},
            {"sync_status": "unconfigured", "error_summary": "not configured",
             "error_classification": "missing_configuration", "titles": 0},
        )
        self.assertNotIn("error_summary", result)
        self.assertNotIn("sync_status", result)

    async def test_ownership_failure_is_not_overwritten_by_playtime(self):
        result = await self._run(
            {"added": 0, "matched": 0, "skipped": 0, "sync_status": "stale",
             "error_summary": "vgcs stale", "error_classification": "auth_stale"},
            {"sync_status": "failed", "error_summary": "pctl boom",
             "error_classification": "unexpected"},
        )
        self.assertEqual(result["error_summary"], "vgcs stale")
        self.assertEqual(result["sync_status"], "stale")


async def _async(value):
    return value


if __name__ == "__main__":
    unittest.main()
