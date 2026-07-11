"""Tests for tag-affinity recomputation (mean-centering, shrinkage, playtime signal)."""

import unittest

from conftest import ToolDBTestCase, add_rating, add_platform, make_steam_game, seed_game
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.db.affinity import (
    MIN_PLAYTIME_SIGNAL_MINUTES,
    playtime_pseudo_score,
)


async def _affinity_rows() -> dict[str, dict]:
    async with db_module.get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT tag, affinity_score, avg_score, game_count FROM tag_affinity"
        )
    return {row["tag"]: dict(row) for row in rows}


class MeanCenteringTests(ToolDBTestCase):
    async def test_affinity_is_signed_around_the_users_own_mean(self):
        # Two 9s and two 5s: μ = 7. The loved tag must come out positive, the
        # disliked one negative, so a merely-average tag can't dominate ranking.
        for i, (name, score, tag) in enumerate(
            [
                ("Loved1", 9.0, "roguelike"),
                ("Loved2", 9.0, "roguelike"),
                ("Meh1", 5.0, "sports"),
                ("Meh2", 5.0, "sports"),
            ]
        ):
            gid = await make_steam_game(name, i + 1, tags=[tag])
            await add_rating(gid, "manual", raw_score=score, normalized_score=score)

        await db_module.recompute_tag_affinity()

        rows = await _affinity_rows()
        self.assertGreater(rows["roguelike"]["affinity_score"], 0)
        self.assertLess(rows["sports"]["affinity_score"], 0)
        # avg_score stays the plain uncentered average for display.
        self.assertEqual(rows["roguelike"]["avg_score"], 9.0)
        self.assertEqual(rows["sports"]["avg_score"], 5.0)

    async def test_ubiquitous_tag_at_user_mean_lands_near_zero(self):
        # "action" on both a 9 and a 5 sits at the user's mean — no signal.
        gid1 = await make_steam_game("Great", 1, tags=["action", "roguelike"])
        gid2 = await make_steam_game("Bad", 2, tags=["action", "sports"])
        await add_rating(gid1, "manual", raw_score=9.0, normalized_score=9.0)
        await add_rating(gid2, "manual", raw_score=5.0, normalized_score=5.0)

        await db_module.recompute_tag_affinity()

        rows = await _affinity_rows()
        self.assertAlmostEqual(rows["action"]["affinity_score"], 0.0, places=6)
        self.assertGreater(rows["roguelike"]["affinity_score"], 0)


class ShrinkageTests(ToolDBTestCase):
    async def test_small_sample_tag_is_damped_toward_neutral(self):
        # Same average score, but one 10/10 game vs three: the well-evidenced
        # tag must carry the larger affinity (Bayesian damped mean).
        gid = await make_steam_game("OneHit", 1, tags=["fishing"])
        await add_rating(gid, "manual", raw_score=10.0, normalized_score=10.0)
        for i in range(3):
            gid = await make_steam_game(f"Solid{i}", 10 + i, tags=["roguelike"])
            await add_rating(gid, "manual", raw_score=10.0, normalized_score=10.0)
        # Anchor the mean below 10 so the centered scores are positive.
        gid = await make_steam_game("Low", 99, tags=["sports"])
        await add_rating(gid, "manual", raw_score=4.0, normalized_score=4.0)

        await db_module.recompute_tag_affinity()

        rows = await _affinity_rows()
        self.assertGreater(
            rows["roguelike"]["affinity_score"], rows["fishing"]["affinity_score"]
        )


class PlaytimeSignalTests(ToolDBTestCase):
    async def test_heavily_played_unrated_game_contributes(self):
        # 100h in an unrated game is taste data — Steam's own recommender is
        # trained on playtime alone.
        await make_steam_game("Sunk100h", 1, playtime_minutes=6000, tags=["deckbuilder"])

        updated = await db_module.recompute_tag_affinity()

        self.assertEqual(updated, 1)
        rows = await _affinity_rows()
        self.assertIn("deckbuilder", rows)
        self.assertEqual(rows["deckbuilder"]["game_count"], 1)

    async def test_non_primary_item_playtime_carries_no_signal(self):
        # DLC/editions/bundles are excluded from the discover rollup, so their
        # playtime must not shift how primary games rank.
        gid = await make_steam_game("Big DLC", 1, playtime_minutes=6000, tags=["horror"])
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET is_primary_library_item = 0 WHERE id = ?", (gid,)
            )
            await db.commit()

        await db_module.recompute_tag_affinity()

        self.assertEqual(await _affinity_rows(), {})

    async def test_farmed_game_playtime_carries_no_signal(self):
        # Idle/card-farming games rack up huge playtime that says nothing
        # about taste.
        await make_steam_game(
            "IdleFarm", 1, playtime_minutes=60000, tags=["clicker"], is_farmed=1
        )

        await db_module.recompute_tag_affinity()

        self.assertEqual(await _affinity_rows(), {})

    async def test_barely_touched_and_rated_games_do_not_double_count(self):
        # Under the playtime floor -> no signal; already rated -> the explicit
        # rating wins and playtime adds nothing.
        await make_steam_game(
            "Bounced", 1, playtime_minutes=MIN_PLAYTIME_SIGNAL_MINUTES - 1, tags=["horror"]
        )
        gid = await make_steam_game("RatedAnyway", 2, playtime_minutes=6000, tags=["roguelike"])
        await add_rating(gid, "manual", raw_score=9.0, normalized_score=9.0)

        await db_module.recompute_tag_affinity()

        rows = await _affinity_rows()
        self.assertNotIn("horror", rows)
        # weight 1.0 (manual) only, not 1.0 + 0.3: with a single signal the
        # centered sum is 0 regardless, so assert via avg_score staying exact.
        self.assertEqual(rows["roguelike"]["avg_score"], 9.0)

    def test_pseudo_score_scale(self):
        self.assertAlmostEqual(playtime_pseudo_score(120), 5.6, places=1)
        self.assertAlmostEqual(playtime_pseudo_score(600), 7.0, places=1)
        # Capped below a true 10 so explicit loves outrank inferred ones.
        self.assertLessEqual(playtime_pseudo_score(600000), 9.5)


class DLCHandlingTests(ToolDBTestCase):
    async def test_explicit_rating_on_nested_row_contributes_to_affinity(self):
        # A user's explicit rating on a DLC/expansion is taste data — it should
        # contribute to tag affinity even though the row is not primary.
        parent_id = await make_steam_game("Bloodborne", 1, tags=["horror", "action"])
        dlc_id = await seed_game(
            "The Old Hunters",
            tags=["horror", "action"],
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        # Rate the DLC highly
        await add_rating(dlc_id, "manual", raw_score=9.0, normalized_score=9.0)
        # Rate the parent lower to establish a mean
        await add_rating(parent_id, "manual", raw_score=5.0, normalized_score=5.0)

        await db_module.recompute_tag_affinity()

        rows = await _affinity_rows()
        self.assertIn("horror", rows)
        self.assertIn("action", rows)
        # Both tags should appear (from both parent and dlc ratings).
        self.assertEqual(rows["horror"]["game_count"], 2)
        self.assertEqual(rows["action"]["game_count"], 2)

    async def test_unrated_nested_row_playtime_does_not_contribute(self):
        # A nested (non-primary) row with big playtime but no explicit rating
        # should NOT contribute a pseudo-rating to tag affinity, even though it's
        # owned and heavily played. Only primary rows' playtime signals count.
        parent_id = await make_steam_game("Portal", 1, tags=["puzzle"])
        dlc_id = await seed_game(
            "Portal 2: Peer Review",
            tags=["puzzle"],
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        # Add a platform with significant playtime but no rating
        await add_platform(dlc_id, "steam", playtime_minutes=6000)

        await db_module.recompute_tag_affinity()

        rows = await _affinity_rows()
        # puzzle tag should not appear because the DLC's playtime signal is ignored
        self.assertNotIn("puzzle", rows)


if __name__ == "__main__":
    unittest.main()
