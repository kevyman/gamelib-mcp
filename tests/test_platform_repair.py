import unittest

from conftest import ToolDBTestCase, add_platform, seed_game
from gamelib_mcp.data import db as db_module


class PlatformMisclassificationRepairTests(ToolDBTestCase):
    async def test_repair_moves_stale_platform_row_to_correct_game(self) -> None:
        source_game_id = await seed_game("Red Dead Redemption 2")
        target_game_id = await seed_game("Red Dead Redemption")
        source_platform_id = await add_platform(
            source_game_id,
            "switch2",
            playtime_minutes=180,
        )
        await db_module.upsert_game_platform_identifier(
            source_platform_id,
            "nintendo_title_id",
            "0100",
        )

        repaired = await db_module.repair_misclassified_platform_row(
            source_game_id=source_game_id,
            target_game_id=target_game_id,
            platform="switch2",
        )

        self.assertTrue(repaired)
        async with db_module.get_db() as db:
            source_row = await db.execute_fetchone(
                "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
                (source_game_id, "switch2"),
            )
            target_row = await db.execute_fetchone(
                """SELECT gp.id, gp.playtime_minutes, gpi.identifier_value
                   FROM game_platforms gp
                   JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
                   WHERE gp.game_id = ? AND gp.platform = ?""",
                (target_game_id, "switch2"),
            )

        self.assertIsNone(source_row)
        self.assertEqual(target_row["id"], source_platform_id)
        self.assertEqual(target_row["playtime_minutes"], 180)
        self.assertEqual(target_row["identifier_value"], "0100")

    async def test_repair_drops_stale_row_when_target_platform_exists(self) -> None:
        source_game_id = await seed_game("PowerWash Simulator")
        target_game_id = await seed_game("PowerWash Simulator 2")
        source_platform_id = await add_platform(source_game_id, "switch2")
        target_platform_id = await add_platform(target_game_id, "switch2")
        await db_module.upsert_game_platform_identifier(
            source_platform_id,
            "nintendo_title_id",
            "0101",
        )

        repaired = await db_module.repair_misclassified_platform_row(
            source_game_id=source_game_id,
            target_game_id=target_game_id,
            platform="switch2",
        )

        self.assertTrue(repaired)
        async with db_module.get_db() as db:
            source_row = await db.execute_fetchone(
                "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
                (source_game_id, "switch2"),
            )
            identifier_row = await db.execute_fetchone(
                "SELECT game_platform_id FROM game_platform_identifiers WHERE identifier_value = ?",
                ("0101",),
            )

        self.assertIsNone(source_row)
        self.assertEqual(identifier_row["game_platform_id"], target_platform_id)

    async def test_repair_collision_preserves_source_acquisition_fields(self) -> None:
        source_game_id = await seed_game("Dead Space")
        target_game_id = await seed_game("Dead Space Remake")
        source_platform_id = await add_platform(source_game_id, "steam")
        target_platform_id = await add_platform(target_game_id, "steam")
        await db_module.set_platform_acquisition(
            source_platform_id,
            {
                "acquired_at": "2024-03-01",
                "price_paid": 19.99,
                "price_currency": "EUR",
                "purchase_source": "steam",
                "bundle_name": "Spring Sale Haul",
            },
        )
        # The target already knows its own acquired_at — it must win.
        await db_module.set_platform_acquisition(
            target_platform_id, {"acquired_at": "2020-01-01"}
        )

        repaired = await db_module.repair_misclassified_platform_row(
            source_game_id=source_game_id,
            target_game_id=target_game_id,
            platform="steam",
        )

        self.assertTrue(repaired)
        async with db_module.get_db() as db:
            source_row = await db.execute_fetchone(
                "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
                (source_game_id, "steam"),
            )
            target_row = await db.execute_fetchone(
                f"""SELECT {', '.join(db_module.ACQUISITION_FIELDS)}
                    FROM game_platforms WHERE id = ?""",
                (target_platform_id,),
            )

        self.assertIsNone(source_row)
        # Conflicting column: target's own value survives.
        self.assertEqual(target_row["acquired_at"], "2020-01-01")
        # NULL target columns inherit the source's values.
        self.assertEqual(target_row["price_paid"], 19.99)
        self.assertEqual(target_row["price_currency"], "EUR")
        self.assertEqual(target_row["purchase_source"], "steam")
        self.assertEqual(target_row["bundle_name"], "Spring Sale Haul")


if __name__ == "__main__":
    unittest.main()
