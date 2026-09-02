"""Regression tests for the editions/remakes anti-collapse fix.

Two distinct store entries that share a name (e.g. Dead Space 2008 vs the 2023
remake) must resolve to separate ``games`` rows. These tests exercise the real
SQLite paths through the conftest harness.
"""

import functools
import operator
from datetime import UTC, datetime

from conftest import (
    ToolDBTestCase,
    add_platform,
    make_steam_game,
    seed_game,
)

from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import detectors


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _game_count() -> int:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone("SELECT COUNT(*) AS n FROM games")
    return row["n"]


async def _steam_appids_by_game() -> dict[int, list[str]]:
    """Map game_id -> sorted list of its steam_appid identifier values."""
    async with db_module.get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT gp.game_id, gpi.identifier_value
               FROM game_platform_identifiers gpi
               JOIN game_platforms gp ON gp.id = gpi.game_platform_id
               WHERE gpi.identifier_type = ?""",
            (db_module.STEAM_APP_ID,),
        )
    out: dict[int, list[str]] = {}
    for row in rows:
        out.setdefault(row["game_id"], []).append(row["identifier_value"])
    return {gid: sorted(vals) for gid, vals in out.items()}


class SteamBulkCollapseTests(ToolDBTestCase):
    async def test_sequential_same_name_distinct_appids_stay_separate(self):
        # Dead Space 2008 already in the library (has a Steam row).
        await make_steam_game("Dead Space", 17470)

        # The 2023 remake arrives on a later sync under a different appid.
        await db_module.bulk_upsert_steam_library(
            [{"appid": 1693980, "name": "Dead Space", "playtime_minutes": 0}],
            _now(),
        )

        self.assertEqual(await _game_count(), 2)
        by_game = await _steam_appids_by_game()
        # Two games, each owning exactly one (distinct) appid.
        self.assertEqual(len(by_game), 2)
        self.assertEqual(sorted(functools.reduce(operator.iadd, by_game.values(), [])), ["1693980", "17470"])
        for appids in by_game.values():
            self.assertEqual(len(appids), 1)

    async def test_simultaneous_same_name_distinct_appids_stay_separate(self):
        # A first-ever sync where the user owns both Dead Spaces at once.
        await db_module.bulk_upsert_steam_library(
            [
                {"appid": 17470, "name": "Dead Space", "playtime_minutes": 0},
                {"appid": 1693980, "name": "Dead Space", "playtime_minutes": 0},
            ],
            _now(),
        )

        self.assertEqual(await _game_count(), 2)
        by_game = await _steam_appids_by_game()
        self.assertEqual(len(by_game), 2)
        self.assertEqual(sorted(functools.reduce(operator.iadd, by_game.values(), [])), ["1693980", "17470"])

    async def test_cross_platform_same_name_attaches_to_existing_row(self):
        # An Epic-only "Portal" (no Steam row yet) should gain a Steam row rather
        # than fork a duplicate when the Steam library is synced.
        epic_game_id = await seed_game("Portal")
        await add_platform(epic_game_id, "epic")

        await db_module.bulk_upsert_steam_library(
            [{"appid": 400, "name": "Portal", "playtime_minutes": 120}],
            _now(),
        )

        self.assertEqual(await _game_count(), 1)
        by_game = await _steam_appids_by_game()
        self.assertEqual(by_game, {epic_game_id: ["400"]})

    async def test_two_appids_dont_both_batch_onto_one_cross_platform_row(self):
        # A Steam-less "Portal" exists (Epic) and a single sync chunk carries two
        # distinct Portal appids. Only one may claim the cross-platform row; the
        # other must fork its own game rather than collapse onto the same row.
        epic_game_id = await seed_game("Portal")
        await add_platform(epic_game_id, "epic")

        await db_module.bulk_upsert_steam_library(
            [
                {"appid": 400, "name": "Portal", "playtime_minutes": 0},
                {"appid": 401, "name": "Portal", "playtime_minutes": 0},
            ],
            _now(),
        )

        # One pre-existing Epic game + one brand-new game = two rows total.
        self.assertEqual(await _game_count(), 2)
        by_game = await _steam_appids_by_game()
        self.assertEqual(len(by_game), 2)
        # The lowest row_order appid (400) claims the Epic row; 401 forks a new game.
        self.assertEqual(by_game[epic_game_id], ["400"])
        self.assertEqual(sorted(functools.reduce(operator.iadd, by_game.values(), [])), ["400", "401"])

    async def test_resync_is_idempotent(self):
        rows = [{"appid": 17470, "name": "Dead Space", "playtime_minutes": 30}]
        await db_module.bulk_upsert_steam_library(rows, _now())
        await db_module.bulk_upsert_steam_library(rows, _now())

        self.assertEqual(await _game_count(), 1)
        by_game = await _steam_appids_by_game()
        self.assertEqual(by_game, {1: ["17470"]})


class FuzzyGuardTests(ToolDBTestCase):
    async def test_exclude_platform_drops_same_platform_candidate(self):
        existing = await make_steam_game("Dead Space", 17470)
        candidates = {existing: "Dead Space"}

        # Without exclusion the name matches; with it the Steam-owning row is dropped.
        matched = await db_module.find_game_by_name_fuzzy("Dead Space", candidates=candidates)
        self.assertIsNotNone(matched)

        excluded = await db_module.find_game_by_name_fuzzy(
            "Dead Space", candidates=candidates, exclude_platform="steam"
        )
        self.assertIsNone(excluded)

    async def test_reference_release_date_rejects_year_conflict(self):
        existing = await seed_game("Dead Space", release_date="2008-10-13")
        candidates = {existing: "Dead Space"}

        # Same year -> match; conflicting year -> rejected.
        same_year = await db_module.find_game_by_name_fuzzy(
            "Dead Space", candidates=candidates, reference_release_date="2008-01-01"
        )
        self.assertIsNotNone(same_year)

        other_year = await db_module.find_game_by_name_fuzzy(
            "Dead Space", candidates=candidates, reference_release_date="2023-01-27"
        )
        self.assertIsNone(other_year)

    async def test_year_filter_picks_matching_candidate_over_best_name_match(self):
        # Both editions exist as separate rows. A 2023 cross-platform sync must
        # resolve to the 2023 row, not return None because the 2008 row ranked first.
        old = await seed_game("Dead Space", release_date="2008-10-13")
        new = await seed_game("Dead Space", release_date="2023-01-27")
        candidates = {old: "Dead Space", new: "Dead Space"}

        matched = await db_module.find_game_by_name_fuzzy(
            "Dead Space", candidates=candidates, reference_release_date="2023-01-27"
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched["id"], new)


class DetectCollapsedGamesTests(ToolDBTestCase):
    async def test_reports_row_with_multiple_same_type_identifiers(self):
        # Force a collapse: one game's Steam platform row holding two appids.
        game_id = await make_steam_game("Dead Space", 17470)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id FROM game_platforms WHERE game_id = ? AND platform = 'steam'",
                (game_id,),
            )
            await db.execute(
                """INSERT INTO game_platform_identifiers
                   (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at)
                   VALUES (?, ?, ?, 0, ?)""",
                (row["id"], db_module.STEAM_APP_ID, "1693980", _now()),
            )
            await db.commit()

        result = await detectors.detect_collapsed_games()
        self.assertEqual(result["collapsed_count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["game_id"], game_id)
        self.assertEqual(candidate["identifier_type"], db_module.STEAM_APP_ID)
        self.assertEqual(candidate["identifier_count"], 2)
        self.assertEqual(sorted(candidate["identifier_values"]), ["1693980", "17470"])

    async def test_clean_library_reports_nothing(self):
        await make_steam_game("Dead Space", 17470)
        await make_steam_game("Portal", 400)

        result = await detectors.detect_collapsed_games()
        self.assertEqual(result["collapsed_count"], 0)
        self.assertEqual(result["candidates"], [])
