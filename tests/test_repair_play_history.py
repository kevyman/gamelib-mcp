"""Tests for scripts/repair_play_history_corrections.py.

The read-time gate (tools/history.py) hides a stored-total correction; this
script removes it from the stored series so anything reading play_history
directly stops seeing an 81-hour step that was never played.
"""

import importlib.util
from pathlib import Path

from conftest import ToolDBTestCase, add_platform, seed_game

from gamelib_mcp.data import db as db_module

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "repair_play_history_corrections.py"
_spec = importlib.util.spec_from_file_location("repair_play_history_corrections", _SCRIPT)
assert _spec is not None and _spec.loader is not None
repair = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair)


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


async def _series(game_id: int, platform: str) -> list[tuple[str, int]]:
    async with db_module.get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT snapshot_date, playtime_minutes FROM play_history
               WHERE game_id = ? AND platform = ? ORDER BY snapshot_date""",
            (game_id, platform),
        )
    return [(r["snapshot_date"], r["playtime_minutes"]) for r in rows]


class RepairPlayHistoryCorrectionsTests(ToolDBTestCase):
    async def _seed_tsushima(self) -> int:
        game_id = await seed_game("Ghost of Tsushima")
        await add_platform(game_id, "ps5", playtime_minutes=4933)
        await _set_last_played(game_id, "ps5", "2022-09-21")
        await _snapshot(game_id, "ps5", "2026-07-04", 46)
        await _snapshot(game_id, "ps5", "2026-08-02", 4933)
        return game_id

    async def test_detects_growth_recorded_after_last_played(self):
        game_id = await self._seed_tsushima()

        found = await repair.find_corrections(None)

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["game_id"], game_id)
        self.assertEqual(found[0]["baseline_total"], 46)
        self.assertEqual(found[0]["corrected_total"], 4933)

    async def test_level_shift_removes_the_step_without_lowering_the_total(self):
        game_id = await self._seed_tsushima()

        await repair.apply_corrections(await repair.find_corrections(None))

        # The pre-correction snapshot is lifted to the corrected level, so the
        # delta across the correction date is now zero and the latest snapshot
        # still matches game_platforms.playtime_minutes.
        self.assertEqual(
            await _series(game_id, "ps5"),
            [("2026-07-04", 4933), ("2026-08-02", 4933)],
        )

    async def test_shape_of_real_growth_before_the_correction_is_preserved(self):
        # A level shift, not a flatten: the 300 -> 395 growth the user really
        # played must survive, just re-based onto the corrected level.
        game_id = await seed_game("Horizon Zero Dawn")
        await add_platform(game_id, "ps5", playtime_minutes=449)
        await _set_last_played(game_id, "ps5", "2026-06-04")
        await _snapshot(game_id, "ps5", "2026-05-01", 300)
        await _snapshot(game_id, "ps5", "2026-06-30", 395)
        await _snapshot(game_id, "ps5", "2026-08-02", 449)

        await repair.apply_corrections(await repair.find_corrections(None))

        series = await _series(game_id, "ps5")
        self.assertEqual(series, [("2026-05-01", 354), ("2026-06-30", 449), ("2026-08-02", 449)])
        # The real 95-minute growth is intact.
        self.assertEqual(series[1][1] - series[0][1], 95)

    async def test_growth_while_still_playing_is_left_alone(self):
        game_id = await seed_game("Blue Prince")
        await add_platform(game_id, "ps5", playtime_minutes=200)
        await _set_last_played(game_id, "ps5", "2026-07-30")
        await _snapshot(game_id, "ps5", "2026-07-04", 120)
        await _snapshot(game_id, "ps5", "2026-08-02", 200)

        self.assertEqual(await repair.find_corrections(None), [])

    async def test_null_last_played_is_never_guessed_at(self):
        game_id = await seed_game("Some GOG Game")
        await add_platform(game_id, "gog", playtime_minutes=500)
        await _set_last_played(game_id, "gog", None)
        await _snapshot(game_id, "gog", "2026-07-04", 10)
        await _snapshot(game_id, "gog", "2026-08-02", 500)

        self.assertEqual(await repair.find_corrections(None), [])

    async def test_decrease_is_not_a_correction(self):
        game_id = await seed_game("Downward Revision")
        await add_platform(game_id, "ps5", playtime_minutes=40)
        await _set_last_played(game_id, "ps5", "2022-01-01")
        await _snapshot(game_id, "ps5", "2026-07-04", 100)
        await _snapshot(game_id, "ps5", "2026-08-02", 40)

        self.assertEqual(await repair.find_corrections(None), [])

    async def test_platform_filter(self):
        ps5_game = await self._seed_tsushima()
        steam_game = await seed_game("Old Steam Game")
        await add_platform(steam_game, "steam", playtime_minutes=900)
        await _set_last_played(steam_game, "steam", "2021-01-01")
        await _snapshot(steam_game, "steam", "2026-07-04", 5)
        await _snapshot(steam_game, "steam", "2026-08-02", 900)

        found = await repair.find_corrections("ps5")

        self.assertEqual([c["game_id"] for c in found], [ps5_game])

    async def test_is_idempotent(self):
        game_id = await self._seed_tsushima()

        await repair.apply_corrections(await repair.find_corrections(None))
        first = await _series(game_id, "ps5")
        # A repaired series no longer matches the rule, so a second run is a
        # no-op rather than shifting the level again.
        self.assertEqual(await repair.find_corrections(None), [])
        await repair.apply_corrections(await repair.find_corrections(None))

        self.assertEqual(await _series(game_id, "ps5"), first)
