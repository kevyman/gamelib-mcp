"""Tests for tag-affinity recomputation (mean-centering, shrinkage, playtime signal)."""

import unittest

from conftest import (
    ToolDBTestCase,
    add_platform,
    add_rating,
    make_steam_game,
    seed_game,
)

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.db.affinity import (
    AFFINITY_FORMULA_VERSION,
    DEFAULT_SHRINKAGE_WEIGHT,
    MAX_SHRINKAGE_WEIGHT,
    MIN_PLAYTIME_SIGNAL_MINUTES,
    MIN_SHRINKAGE_WEIGHT,
    estimate_shrinkage_weight,
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


def _tag_data(observations: dict[str, list[tuple[float, float]]]) -> dict[str, dict]:
    """Build estimate_shrinkage_weight input from {tag: [(weight, score), ...]}."""
    data: dict[str, dict] = {}
    for tag, signals in observations.items():
        entry = {
            "weighted_sum": 0.0,
            "centered_sum": 0.0,
            "square_sum": 0.0,
            "weight_sum": 0.0,
            "weight_square_sum": 0.0,
            "game_ids": set(),
        }
        for i, (weight, score) in enumerate(signals):
            entry["weighted_sum"] += weight * score
            entry["square_sum"] += weight * score * score
            entry["weight_sum"] += weight
            entry["weight_square_sum"] += weight * weight
            entry["game_ids"].add(f"{tag}-{i}")
        data[tag] = entry
    return data


class ShrinkageWeightEstimateTests(unittest.TestCase):
    """The prior weight k is measured, not hand-picked."""

    def test_too_few_tags_falls_back_to_the_default(self):
        estimate = estimate_shrinkage_weight(_tag_data({"a": [(1.0, 9.0)]}))
        self.assertEqual(estimate["shrinkage_weight"], DEFAULT_SHRINKAGE_WEIGHT)
        self.assertEqual(estimate["reason"], "insufficient_data")

    def test_tags_that_only_differ_by_noise_shrink_hard(self):
        # Every tag drawn from the same distribution: sigma2_between is zero
        # or negative, so there is nothing to trust and k pins to the ceiling.
        observations = {
            f"tag{i}": [(1.0, 4.0), (1.0, 10.0)] for i in range(60)
        }
        estimate = estimate_shrinkage_weight(_tag_data(observations))
        self.assertEqual(estimate["shrinkage_weight"], MAX_SHRINKAGE_WEIGHT)
        self.assertEqual(estimate["reason"], "no_measurable_between_tag_variance")

    def test_perfectly_consistent_tags_are_barely_shrunk(self):
        # Zero within-tag variance with separated means is the OPPOSITE of the
        # no-signal case: every tag mean is measured exactly, so k = 0. Sharing
        # a branch with sigma2_between <= 0 would pin these to the ceiling and
        # erase the strongest evidence the estimator can see.
        observations = {}
        for i in range(60):
            centre = 3.0 if i % 2 else 9.0
            observations[f"tag{i}"] = [(1.0, centre), (1.0, centre)]
        estimate = estimate_shrinkage_weight(_tag_data(observations))
        self.assertEqual(estimate["sigma2_within"], 0.0)
        self.assertGreater(estimate["sigma2_between"], 0)
        self.assertEqual(estimate["shrinkage_weight"], MIN_SHRINKAGE_WEIGHT)
        self.assertEqual(estimate["reason"], "no_within_tag_variance")

    def test_no_variance_at_all_still_shrinks_hard(self):
        # Identical scores everywhere: neither component is measurable, so
        # there is no information and the safe read stays "trust nothing".
        observations = {f"tag{i}": [(1.0, 7.0), (1.0, 7.0)] for i in range(60)}
        estimate = estimate_shrinkage_weight(_tag_data(observations))
        self.assertEqual(estimate["shrinkage_weight"], MAX_SHRINKAGE_WEIGHT)
        self.assertEqual(estimate["reason"], "no_measurable_between_tag_variance")

    def test_separated_tags_yield_a_small_prior_weight(self):
        # Tight clusters far apart: within-tag variance is tiny next to the
        # between-tag spread, so k is small and tags keep their deviations.
        observations = {}
        for i in range(60):
            centre = 3.0 if i % 2 else 9.0
            observations[f"tag{i}"] = [(1.0, centre - 0.1), (1.0, centre + 0.1)]
        estimate = estimate_shrinkage_weight(_tag_data(observations))
        self.assertGreater(estimate["sigma2_between"], estimate["sigma2_within"])
        self.assertLess(estimate["shrinkage_weight"], 1.0 + 1e-9)

    def test_within_variance_drives_the_weight_up(self):
        # Same tag centres, but each tag's own observations now spread widely:
        # the marginal evidence per tag is weaker, so k must rise.
        tight, loose = {}, {}
        for i in range(60):
            centre = 3.0 if i % 2 else 9.0
            tight[f"tag{i}"] = [(1.0, centre - 0.5), (1.0, centre + 0.5)]
            loose[f"tag{i}"] = [(1.0, centre - 3.0), (1.0, centre + 3.0)]
        self.assertLess(
            estimate_shrinkage_weight(_tag_data(tight))["shrinkage_weight"],
            estimate_shrinkage_weight(_tag_data(loose))["shrinkage_weight"],
        )


class RarityBiasTests(ToolDBTestCase):
    """Regression: affinity used to be inversely correlated with evidence."""

    async def _seed_library(self):
        # 40 games spread across the rating range so the estimator has a
        # population to measure, all sharing a well-supported tag on the good
        # half. Plus the reported failure shape: two 10/10 games carrying an
        # incidental keyword nothing else has.
        for i in range(20):
            gid = await make_steam_game(f"Good{i}", 1000 + i, tags=["3d platformer", "action"])
            await add_rating(gid, "backloggd", raw_score=8.6, normalized_score=8.6)
        for i in range(20):
            gid = await make_steam_game(f"Mixed{i}", 2000 + i, tags=["action", f"filler{i}"])
            await add_rating(gid, "backloggd", raw_score=5.0 + (i % 5), normalized_score=5.0 + (i % 5))
        for i in range(2):
            gid = await make_steam_game(f"Cow{i}", 3000 + i, tags=["cow", "action"])
            await add_rating(gid, "manual", raw_score=10.0, normalized_score=10.0)

    async def test_two_game_keyword_does_not_outrank_a_twenty_game_tag(self):
        await self._seed_library()
        await db_module.recompute_tag_affinity()

        rows = await _affinity_rows()
        self.assertEqual(rows["cow"]["game_count"], 2)
        self.assertEqual(rows["3d platformer"]["game_count"], 20)
        # "cow" has the higher raw average (10.0 vs 8.6) and used to win on it.
        self.assertGreater(rows["cow"]["avg_score"], rows["3d platformer"]["avg_score"])
        self.assertGreater(
            rows["3d platformer"]["affinity_score"], rows["cow"]["affinity_score"]
        )

    async def test_affinity_no_longer_declines_with_support(self):
        await self._seed_library()
        await db_module.recompute_tag_affinity()

        rows = await _affinity_rows()
        # Same underlying deviation, different evidence: the single-game filler
        # tags must not average out above the well-supported tags.
        singles = [r["affinity_score"] for r in rows.values() if r["game_count"] == 1]
        supported = [r["affinity_score"] for r in rows.values() if r["game_count"] >= 20]
        self.assertTrue(singles and supported)
        self.assertGreater(
            max(supported), max(abs(a) for a in singles)
        )

    async def test_scale_record_is_written_for_auditability(self):
        await self._seed_library()
        await db_module.recompute_tag_affinity()

        scale = await db_module.get_affinity_scale()
        self.assertEqual(scale["formula_version"], AFFINITY_FORMULA_VERSION)
        self.assertGreater(scale["shrinkage_weight"], 0)
        self.assertIn("sigma2_within", scale)
        self.assertIn("sigma2_between", scale)
        self.assertTrue(await db_module.affinity_scale_is_current())


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
