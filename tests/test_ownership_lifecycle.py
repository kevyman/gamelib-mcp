"""Ownership that ENDS, and telling "not seen" apart from "not owned".

Covers the two halves of the v34 schema addition:

* ``unowned_at`` — a refund/revoked key/lapsed subscription retires an ownership
  row instead of deleting the game (which would cascade every OTHER platform's
  playtime away with it), and survives the next sync.
* ``last_seen_in_source`` — what the platform's own source returned this run, as
  opposed to what any write touched, plus the check that reads it.
"""

import json
import unittest
from datetime import UTC, datetime, timedelta

from conftest import ToolDBTestCase, add_platform, seed_game
from fastmcp.exceptions import ToolError

from gamelib_mcp import lifecycle
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import checks
from gamelib_mcp.tools.acquisition import get_spending_stats, set_acquisition
from gamelib_mcp.tools.platforms import (
    add_game_to_platform,
    get_platform_breakdown,
    set_playtime,
)


async def _platform_row(game_id: int, platform: str) -> dict:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone(
            """SELECT id, owned, unowned_at, last_seen_in_source, manual_overrides,
                      price_paid, price_currency
                 FROM game_platforms WHERE game_id = ? AND platform = ?""",
            (game_id, platform),
        )
    return dict(row)


class RetireOwnershipTests(ToolDBTestCase):
    async def _refunded_game(self) -> int:
        """RDR2 as production had it: bought and refunded on Epic the same day,
        still owned (and played) on Steam."""
        game_id = await seed_game("Red Dead Redemption 2")
        await add_platform(game_id, "epic")
        await add_platform(game_id, "steam", playtime_minutes=387)
        await set_acquisition(
            game_id=game_id,
            platform="epic",
            acquired_at="2020-10-23",
            price_paid=30.19,
            price_currency="EUR",
            purchase_source="epic",
        )
        await set_acquisition(
            game_id=game_id,
            platform="steam",
            acquired_at="2023-01-21",
            price_paid=21.60,
            purchase_source="steam",
        )
        return game_id

    async def test_retires_the_row_without_touching_other_platforms(self):
        game_id = await self._refunded_game()

        result = await add_game_to_platform(
            game_id=game_id, platform="epic", unowned_at="2020-10-23"
        )

        self.assertFalse(result["owned"])
        self.assertEqual(result["unowned_at"], "2020-10-23")
        epic = await _platform_row(game_id, "epic")
        self.assertEqual(epic["owned"], 0)
        self.assertEqual(epic["unowned_at"], "2020-10-23")
        # The acquisition record survives — this is history, not a deletion.
        self.assertEqual(epic["price_paid"], 30.19)
        steam = await _platform_row(game_id, "steam")
        self.assertEqual(steam["owned"], 1)

    async def test_retired_row_leaves_spending_and_platform_counts(self):
        game_id = await self._refunded_game()
        await add_game_to_platform(
            game_id=game_id, platform="epic", unowned_at="2020-10-23"
        )

        spending = await get_spending_stats()
        by_currency = {row["currency"]: row for row in spending["totals"]}
        self.assertNotIn("EUR", by_currency)
        self.assertAlmostEqual(by_currency["USD"]["total_spent"], 21.60, places=2)

        breakdown = await get_platform_breakdown()
        platforms = {row["platform"]: row for row in breakdown["by_platform"]}
        self.assertNotIn("epic", platforms)
        self.assertEqual(platforms["steam"]["owned_games"], 1)
        # One platform now, not two — the duplication count was inflated by
        # exactly the copy that never existed.
        self.assertEqual(breakdown["overlap_count"], 0)

    async def test_a_later_sync_does_not_re_own_a_retired_row(self):
        game_id = await self._refunded_game()
        await add_game_to_platform(
            game_id=game_id, platform="epic", unowned_at="2020-10-23"
        )

        # Xbox-shaped worst case: the source still lists the title (ownership
        # there is title HISTORY, which never forgets).
        await db_module.upsert_game_platform(
            game_id, "epic", owned=1, from_source=True
        )

        epic = await _platform_row(game_id, "epic")
        self.assertEqual(epic["owned"], 0)
        self.assertEqual(epic["unowned_at"], "2020-10-23")
        self.assertEqual(json.loads(epic["manual_overrides"]), ["owned"])
        # The stamp still records what the source SAID, which is the whole
        # point of keeping the two signals apart.
        self.assertIsNotNone(epic["last_seen_in_source"])

    async def test_none_restores_ownership_and_releases_the_pin(self):
        game_id = await self._refunded_game()
        await add_game_to_platform(
            game_id=game_id, platform="epic", unowned_at="2020-10-23"
        )

        result = await add_game_to_platform(
            game_id=game_id, platform="epic", unowned_at="none"
        )

        self.assertTrue(result["owned"])
        self.assertIsNone(result["unowned_at"])
        epic = await _platform_row(game_id, "epic")
        self.assertEqual(epic["owned"], 1)
        self.assertIsNone(epic["unowned_at"])
        self.assertIsNone(epic["manual_overrides"])

    async def test_set_playtime_clear_releases_the_ownership_pin(self):
        game_id = await self._refunded_game()
        await add_game_to_platform(
            game_id=game_id, platform="epic", unowned_at="2020-10-23"
        )

        await set_playtime(game_id=game_id, platform="epic", clear=["owned"])

        epic = await _platform_row(game_id, "epic")
        # Clearing the pin hands the column back to sync; it does NOT re-own.
        self.assertEqual(epic["owned"], 0)
        self.assertIsNone(epic["manual_overrides"])
        await db_module.upsert_game_platform(game_id, "epic", owned=1, from_source=True)
        epic = await _platform_row(game_id, "epic")
        self.assertEqual(epic["owned"], 1)
        self.assertIsNone(epic["unowned_at"])

    async def test_dry_run_writes_nothing(self):
        game_id = await self._refunded_game()

        result = await add_game_to_platform(
            game_id=game_id, platform="epic", unowned_at="2021-01-01", dry_run=True
        )

        self.assertFalse(result["owned"])
        self.assertEqual(result["unowned_at"], "2021-01-01")
        epic = await _platform_row(game_id, "epic")
        self.assertEqual(epic["owned"], 1)
        self.assertIsNone(epic["unowned_at"])

    async def test_refuses_to_mint_a_row_just_to_retire_it(self):
        with self.assertRaisesRegex(ToolError, "no game matches"):
            await add_game_to_platform(
                name="Never Existed", platform="epic", unowned_at="2021-01-01"
            )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT COUNT(*) AS n FROM games")
        self.assertEqual(row["n"], 0)

    async def test_refuses_a_platform_the_game_is_not_on(self):
        game_id = await seed_game("Only On Steam")
        await add_platform(game_id, "steam")
        with self.assertRaisesRegex(ToolError, "has no epic row"):
            await add_game_to_platform(
                game_id=game_id, platform="epic", unowned_at="2021-01-01"
            )

    async def test_rejects_a_wishlist_entry_and_a_bad_date(self):
        game_id = await seed_game("Some Game")
        await add_platform(game_id, "epic")
        with self.assertRaisesRegex(ToolError, "unowned_at requires owned=True"):
            await add_game_to_platform(
                game_id=game_id, platform="epic", owned=False, unowned_at="2021-01-01"
            )
        with self.assertRaisesRegex(ToolError, "unowned_at must be YYYY"):
            await add_game_to_platform(
                game_id=game_id, platform="epic", unowned_at="last tuesday"
            )


class LastSeenInSourceTests(ToolDBTestCase):
    async def test_only_a_source_write_stamps_the_column(self):
        game_id = await seed_game("Citizen Sleeper")

        # A manual add is not the source saying anything.
        await add_game_to_platform(game_id=game_id, platform="epic")
        self.assertIsNone((await _platform_row(game_id, "epic"))["last_seen_in_source"])

        await db_module.upsert_game_platform(game_id, "epic", owned=1, from_source=True)
        self.assertIsNotNone(
            (await _platform_row(game_id, "epic"))["last_seen_in_source"]
        )

    async def test_a_later_non_source_write_does_not_refresh_the_stamp(self):
        game_id = await seed_game("Machinarium")
        await db_module.upsert_game_platform(game_id, "epic", owned=1, from_source=True)
        stamped = (await _platform_row(game_id, "epic"))["last_seen_in_source"]

        await set_playtime(game_id=game_id, platform="epic", playtime_minutes=60)
        await db_module.upsert_game_platform(game_id, "epic", owned=1)

        self.assertEqual(
            (await _platform_row(game_id, "epic"))["last_seen_in_source"], stamped
        )

    async def test_steam_bulk_sync_stamps_new_and_existing_rows(self):
        synced_at = datetime.now(UTC).isoformat()
        await db_module.bulk_upsert_steam_library(
            [{"appid": 220, "name": "Half-Life 2", "playtime_minutes": 10}], synced_at
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT game_id, last_seen_in_source FROM game_platforms WHERE platform = 'steam'"
            )
        self.assertEqual(row["last_seen_in_source"], synced_at)

        later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        await db_module.bulk_upsert_steam_library(
            [{"appid": 220, "name": "Half-Life 2", "playtime_minutes": 20}], later
        )
        self.assertEqual(
            (await _platform_row(row["game_id"], "steam"))["last_seen_in_source"], later
        )

    async def test_steam_bulk_sync_respects_a_retired_row(self):
        synced_at = datetime.now(UTC).isoformat()
        await db_module.bulk_upsert_steam_library(
            [{"appid": 220, "name": "Half-Life 2"}], synced_at
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT game_id FROM game_platforms WHERE platform = 'steam'"
            )
        await add_game_to_platform(
            game_id=row["game_id"], platform="steam", unowned_at="2026-01-05"
        )

        await db_module.bulk_upsert_steam_library(
            [{"appid": 220, "name": "Half-Life 2"}],
            (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )

        steam = await _platform_row(row["game_id"], "steam")
        self.assertEqual(steam["owned"], 0)
        self.assertEqual(steam["unowned_at"], "2026-01-05")


class UnseenInSourceCheckTests(ToolDBTestCase):
    async def _record_successful_syncs(self, platform: str, timestamps: list[str]) -> None:
        for stamp in timestamps:
            await lifecycle.record_platform_sync_outcome(platform, {}, stamp)

    async def _seed_unseen_row(self, *, last_seen: str) -> int:
        game_id = await seed_game("Batman: Arkham Origins")
        gpid = await add_platform(game_id, "epic")
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET last_seen_in_source = ? WHERE id = ?",
                (last_seen, gpid),
            )
            await db.commit()
        return game_id

    async def test_reports_a_row_missing_from_three_successful_syncs(self):
        game_id = await self._seed_unseen_row(last_seen="2026-07-01T00:00:00+00:00")
        await self._record_successful_syncs(
            "epic",
            [
                "2026-07-10T00:00:00+00:00",
                "2026-07-11T00:00:00+00:00",
                "2026-07-12T00:00:00+00:00",
            ],
        )

        findings, extras = await checks._run_ownership_unseen_in_source(
            apply=False, options={}
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["game_id"], game_id)
        self.assertEqual(findings[0]["evidence"]["platform"], "epic")
        self.assertEqual(
            findings[0]["suggested_action"]["tool"], "add_game_to_platform"
        )
        self.assertIn("unowned_at", findings[0]["suggested_action"]["args"])
        self.assertEqual(extras["unseen_rows"], 1)

    async def test_failed_syncs_never_make_a_row_look_abandoned(self):
        await self._seed_unseen_row(last_seen="2026-07-01T00:00:00+00:00")
        # Three runs, all failed — the exact shape of the locked-database
        # outage that must not manufacture findings.
        for stamp in (
            "2026-07-10T00:00:00+00:00",
            "2026-07-11T00:00:00+00:00",
            "2026-07-12T00:00:00+00:00",
        ):
            await lifecycle.record_platform_sync_outcome(
                "epic", {"error": "database is locked"}, stamp
            )

        findings, extras = await checks._run_ownership_unseen_in_source(
            apply=False, options={}
        )

        self.assertEqual(findings, [])
        self.assertIn("epic", extras["platforms_insufficient_history"])

    async def test_one_missed_sync_is_not_enough(self):
        await self._seed_unseen_row(last_seen="2026-07-11T12:00:00+00:00")
        await self._record_successful_syncs(
            "epic",
            [
                "2026-07-10T00:00:00+00:00",
                "2026-07-11T00:00:00+00:00",
                "2026-07-12T00:00:00+00:00",
            ],
        )

        findings, _ = await checks._run_ownership_unseen_in_source(
            apply=False, options={}
        )

        self.assertEqual(findings, [])

    async def test_never_stamped_and_delisted_rows_are_not_judged(self):
        # NULL last_seen_in_source: hand-added, or predating v34.
        never_stamped = await seed_game("Physical Cart")
        await add_platform(never_stamped, "epic")
        delisted_game = await self._seed_unseen_row(last_seen="2026-07-01T00:00:00+00:00")
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET delisted = 1 WHERE game_id = ?",
                (delisted_game,),
            )
            await db.commit()
        await self._record_successful_syncs(
            "epic",
            [
                "2026-07-10T00:00:00+00:00",
                "2026-07-11T00:00:00+00:00",
                "2026-07-12T00:00:00+00:00",
            ],
        )

        findings, extras = await checks._run_ownership_unseen_in_source(
            apply=False, options={}
        )

        self.assertEqual(findings, [])
        self.assertEqual(extras["unseen_rows"], 0)

    async def test_a_retired_row_stops_being_reported(self):
        game_id = await self._seed_unseen_row(last_seen="2026-07-01T00:00:00+00:00")
        await self._record_successful_syncs(
            "epic",
            [
                "2026-07-10T00:00:00+00:00",
                "2026-07-11T00:00:00+00:00",
                "2026-07-12T00:00:00+00:00",
            ],
        )
        await add_game_to_platform(
            game_id=game_id, platform="epic", unowned_at="2026-07-12"
        )

        findings, _ = await checks._run_ownership_unseen_in_source(
            apply=False, options={}
        )

        self.assertEqual(findings, [])


class SyncPlatformErrorCheckTests(ToolDBTestCase):
    async def test_reports_a_failed_platform_with_its_error(self):
        await lifecycle.record_platform_sync_outcome(
            "steam", {}, "2026-07-27T11:58:07+00:00"
        )
        await lifecycle.record_platform_sync_outcome(
            "steam", {"error": "database is locked"}, "2026-07-30T08:23:29+00:00"
        )

        findings, extras = await checks._run_sync_platform_error(
            apply=False, options={}
        )

        steam = [f for f in findings if f["evidence"]["platform"] == "steam"]
        self.assertEqual(len(steam), 1)
        self.assertEqual(steam[0]["severity"], "warning")
        self.assertIn("database is locked", steam[0]["message"])
        self.assertEqual(steam[0]["suggested_action"]["args"]["platforms"], ["steam"])
        self.assertIn("steam", extras["failing_platforms"])

    async def test_reports_a_platform_that_has_not_succeeded_in_days(self):
        stale = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        await lifecycle.record_platform_sync_outcome("epic", {}, stale)

        findings, _ = await checks._run_sync_platform_error(apply=False, options={})

        epic = [f for f in findings if f["evidence"]["platform"] == "epic"]
        self.assertEqual(len(epic), 1)
        self.assertIn("not synced successfully", epic[0]["message"])

    async def test_a_recent_success_reports_nothing(self):
        await lifecycle.record_platform_sync_outcome(
            "epic", {}, datetime.now(UTC).isoformat()
        )

        findings, extras = await checks._run_sync_platform_error(apply=False, options={})

        self.assertEqual([f for f in findings if f["evidence"]["platform"] == "epic"], [])
        self.assertEqual(extras["failing_platforms"], [])

    async def test_an_unconfigured_platform_is_summarized_not_flagged(self):
        await lifecycle.record_platform_sync_outcome(
            "xbox",
            {"sync_status": "unconfigured", "error_summary": "OPENXBL_API_KEY is not set"},
            datetime.now(UTC).isoformat(),
        )

        findings, extras = await checks._run_sync_platform_error(apply=False, options={})

        self.assertEqual([f for f in findings if f["evidence"]["platform"] == "xbox"], [])
        self.assertIn("xbox", extras["unconfigured_platforms"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
