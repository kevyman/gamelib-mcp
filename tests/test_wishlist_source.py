"""Characterization tests for add_game_to_platform(wishlist_source=...).

Issue #110 phase 1 (assessment-verdict wishlist promotion): a game-quality
skill's "wishlist for sale" verdict promotes a game onto the wishlist with
source="assessment" rather than "manual", so it stays distinct from
hand-curated entries and is bulk-removable later by source. The whole point of
the distinct source is reconciliation safety — an assessment-promoted row must
never be swept away by a sync's source-scoped removal reconciliation
(delete_stale_wishlist_entries), which is scoped to (platform, source) exactly
so it can't touch rows from another source.
"""

from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, seed_game
from fastmcp.exceptions import ToolError

from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import platforms


async def _wishlist_row(game_id: int, platform: str):
    async with db_module.get_db() as db:
        return await db.execute_fetchone(
            "SELECT * FROM game_wishlist WHERE game_id = ? AND platform = ?",
            (game_id, platform),
        )


class WishlistSourceDefaultTests(ToolDBTestCase):
    async def test_omitted_defaults_to_manual(self):
        gid = await seed_game("Default Source Game")
        result = await platforms.add_game_to_platform(
            game_id=gid, platform="steam", owned=False
        )
        self.assertEqual(result["wishlist_source"], "manual")
        row = await _wishlist_row(gid, "steam")
        self.assertEqual(row["source"], "manual")


class WishlistSourceAssessmentTests(ToolDBTestCase):
    async def test_assessment_source_recorded_and_echoed(self):
        gid = await seed_game("Assessment Promoted Game")
        result = await platforms.add_game_to_platform(
            game_id=gid, platform="steam", owned=False, wishlist_source="assessment"
        )
        self.assertEqual(result["wishlist_source"], "assessment")
        row = await _wishlist_row(gid, "steam")
        self.assertEqual(row["source"], "assessment")

    async def test_case_normalized(self):
        gid = await seed_game("Capitalized Source Game")
        result = await platforms.add_game_to_platform(
            game_id=gid, platform="steam", owned=False, wishlist_source="Assessment"
        )
        self.assertEqual(result["wishlist_source"], "assessment")
        row = await _wishlist_row(gid, "steam")
        self.assertEqual(row["source"], "assessment")


class WishlistSourceValidationTests(ToolDBTestCase):
    async def test_owned_true_with_wishlist_source_raises(self):
        with self.assertRaisesRegex(ToolError, "requires owned=False"):
            await platforms.add_game_to_platform(
                "Some Game", "steam", owned=True, wishlist_source="assessment"
            )

    async def test_reserved_source_steam_rejected(self):
        gid = await seed_game("Rejected Steam Source Game")
        with self.assertRaises(ToolError) as ctx:
            await platforms.add_game_to_platform(
                game_id=gid, platform="steam", owned=False, wishlist_source="steam"
            )
        message = str(ctx.exception)
        self.assertIn("manual", message)
        self.assertIn("assessment", message)

    async def test_reserved_source_dekudeals_rejected(self):
        gid = await seed_game("Rejected DekuDeals Source Game")
        with self.assertRaises(ToolError):
            await platforms.add_game_to_platform(
                game_id=gid, platform="switch2", owned=False, wishlist_source="dekudeals"
            )

    async def test_unknown_source_rejected(self):
        gid = await seed_game("Rejected Bogus Source Game")
        with self.assertRaises(ToolError):
            await platforms.add_game_to_platform(
                game_id=gid, platform="steam", owned=False, wishlist_source="bogus"
            )


class WishlistSourceReconciliationSafetyTests(ToolDBTestCase):
    async def test_assessment_row_survives_steam_reconciliation(self):
        gid = await seed_game("Assessment Row Survives Reconciliation")
        await platforms.add_game_to_platform(
            game_id=gid, platform="steam", owned=False, wishlist_source="assessment"
        )

        # Simulate a full steam-source wishlist reconciliation that found
        # nothing this round — only "steam"-sourced rows should be swept.
        deleted = await db_module.delete_stale_wishlist_entries("steam", "steam", set())

        self.assertEqual(deleted, 0)
        row = await _wishlist_row(gid, "steam")
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "assessment")


class WishlistSourceFulfillmentTests(ToolDBTestCase):
    async def test_ownership_clears_assessment_row_regardless_of_source(self):
        gid = await seed_game("Assessment Row Gets Fulfilled")
        await platforms.add_game_to_platform(
            game_id=gid, platform="steam", owned=False, wishlist_source="assessment"
        )
        row = await _wishlist_row(gid, "steam")
        self.assertIsNotNone(row)

        await platforms.add_game_to_platform(game_id=gid, platform="steam", owned=True)

        row = await _wishlist_row(gid, "steam")
        self.assertIsNone(row)


class WishlistSourceComposesWithPushTests(ToolDBTestCase):
    async def test_assessment_source_composes_with_push_to_store(self):
        gid = await seed_game("Assessment Push Game")
        with patch.object(
            platforms,
            "push_to_steam_wishlist",
            new=AsyncMock(return_value={"appid": 314, "via": "webapi", "wishlist_count": 1}),
        ) as mock_push:
            result = await platforms.add_game_to_platform(
                game_id=gid,
                platform="steam",
                owned=False,
                wishlist_source="assessment",
                push_to_store=True,
                identifier_type="steam_appid",
                identifier_value="314",
            )
        mock_push.assert_awaited_once_with(314)
        self.assertEqual(result["wishlist_source"], "assessment")
        self.assertTrue(result["store_push"]["pushed"])
        row = await _wishlist_row(gid, "steam")
        self.assertEqual(row["source"], "assessment")


class WishlistSourceBatchTests(ToolDBTestCase):
    async def test_batch_per_item_sources(self):
        result = await platforms.add_games_to_platform_batch(
            [
                {
                    "name": "Batch Manual Source Game",
                    "platform": "steam",
                    "owned": False,
                },
                {
                    "name": "Batch Assessment Source Game",
                    "platform": "steam",
                    "owned": False,
                    "wishlist_source": "assessment",
                },
            ]
        )
        self.assertEqual(result["ok"], 2)
        manual_item, assessment_item = result["results"]
        self.assertEqual(manual_item["wishlist_source"], "manual")
        self.assertEqual(assessment_item["wishlist_source"], "assessment")

        manual_row = await _wishlist_row(manual_item["game_id"], "steam")
        assessment_row = await _wishlist_row(assessment_item["game_id"], "steam")
        self.assertEqual(manual_row["source"], "manual")
        self.assertEqual(assessment_row["source"], "assessment")


class WishlistSourceDryRunTests(ToolDBTestCase):
    async def test_dry_run_echoes_without_writing(self):
        result = await platforms.add_game_to_platform(
            name="__WISHLIST_SOURCE_DRY_RUN__",
            platform="steam",
            owned=False,
            wishlist_source="assessment",
            dry_run=True,
        )
        self.assertEqual(result["wishlist_source"], "assessment")
        async with db_module.get_db() as db:
            game = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ?", ("__WISHLIST_SOURCE_DRY_RUN__",)
            )
            wishlist_count = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM game_wishlist"
            )
        self.assertIsNone(game)
        self.assertEqual(wishlist_count["c"], 0)

    async def test_dry_run_owned_true_wishlist_source_none(self):
        gid = await seed_game("Owned Dry Run Game")
        result = await platforms.add_game_to_platform(
            game_id=gid, platform="steam", owned=True, dry_run=True
        )
        self.assertIsNone(result["wishlist_source"])
