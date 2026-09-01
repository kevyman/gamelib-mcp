"""The taste-profile backtest harness (evals/taste-profile-eval/eval_profile.py).

The harness is the only measurement of `tag_affinity` + discover's match score,
so it needs its own guard: on a fixture where the taste IS learnable it must
find the signal, the neutralised baseline must NOT, and it must never write to
the database it was pointed at (the real input is a personal snapshot).
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

from conftest import ToolDBTestCase, add_platform, add_rating, seed_game

from gamelib_mcp.data.db import get_db

_EVAL_PATH = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "taste-profile-eval"
    / "eval_profile.py"
)


def _load_eval_module():
    """Import the eval script by path — evals/ is not an installed package."""
    spec = importlib.util.spec_from_file_location("eval_profile", _EVAL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the module's dataclasses resolve their (PEP 563,
    # string) annotations through sys.modules, and blow up if it is absent.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


eval_profile = _load_eval_module()


# Loved tags, hated tags, and filler that carries no signal either way.
LOVED_TAGS = ["roguelike", "deckbuilder", "turn-based"]
HATED_TAGS = ["sports", "racing", "arcade"]
FILLER_TAGS = ["indie", "singleplayer", "colorful"]


class EvalProfileHarnessTests(ToolDBTestCase):
    """~40 seeded games with a deliberately learnable taste."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self._seed_library()

    async def _seed_library(self) -> None:
        # 14 loved: roguelike/deckbuilder, rated 8-10.
        for index in range(14):
            game_id = await seed_game(
                f"Loved Roguelike {index}",
                tags=[*LOVED_TAGS, FILLER_TAGS[index % len(FILLER_TAGS)]],
            )
            await add_platform(game_id, "steam", playtime_minutes=600 + index * 30)
            score = 8.0 + (index % 3)
            await add_rating(game_id, "manual", score, score)

        # 14 disliked: sports/racing, rated 1-3.
        for index in range(14):
            game_id = await seed_game(
                f"Hated Sportsball {index}",
                tags=[*HATED_TAGS, FILLER_TAGS[index % len(FILLER_TAGS)]],
            )
            await add_platform(game_id, "steam", playtime_minutes=30 + index)
            score = 1.0 + (index % 3)
            await add_rating(game_id, "manual", score, score)

        # 6 mixed: one tag from each camp, middling ratings.
        for index in range(6):
            game_id = await seed_game(
                f"Mixed Bag {index}",
                tags=[LOVED_TAGS[index % 3], HATED_TAGS[index % 3], "indie"],
            )
            await add_platform(game_id, "steam", playtime_minutes=200 + index * 10)
            score = 5.0 + (index % 2)
            await add_rating(game_id, "manual", score, score)

        # 10 unrated owned games: playtime tracks the same taste, which is what
        # the rating-free control signal is supposed to pick up.
        for index in range(5):
            game_id = await seed_game(
                f"Unrated Roguelike {index}", tags=[*LOVED_TAGS, "indie"]
            )
            await add_platform(game_id, "steam", playtime_minutes=1200 + index * 100)
        for index in range(5):
            game_id = await seed_game(
                f"Unrated Sportsball {index}", tags=[*HATED_TAGS, "indie"]
            )
            await add_platform(game_id, "steam", playtime_minutes=10 + index)

    async def _run(self, **kwargs):
        return await eval_profile.run_eval(self._db_path, folds=5, seed=0, **kwargs)

    async def test_reports_every_metric_and_finds_the_signal(self) -> None:
        metrics = await self._run(baseline=False)

        self.assertEqual(
            set(metrics),
            {"config", "counts", "pooled", "playtime_control", "folds", "wall_seconds"},
        )
        config = metrics["config"]
        for key in (
            "match_prior",
            "idf_df_floor",
            "vibe_tag_prominence_cutoff",
            "full_model_shrinkage_k",
            "rating_target",
            "seed",
            "baseline",
        ):
            self.assertIn(key, config)
        self.assertEqual(metrics["counts"]["n_rated"], 34)
        self.assertEqual(metrics["counts"]["n_rated_scored"], 34)
        self.assertEqual(metrics["counts"]["n_unrated_scored"], 10)
        self.assertEqual(len(metrics["folds"]), 5)
        for fold in metrics["folds"]:
            self.assertIn("shrinkage_k", fold)
            self.assertIn("spearman", fold)
            self.assertGreater(fold["n_scored"], 0)

        pooled = metrics["pooled"]
        self.assertIsNotNone(pooled["spearman"])
        self.assertGreater(pooled["spearman"], 0.3)
        # Loved games should dominate the top of the ranking, and the two
        # rating camps must not sit on top of each other.
        self.assertGreaterEqual(pooled["precision_at_k"], 0.5)
        separation = pooled["separation"]
        self.assertGreater(separation["gap"], 0)

        control = metrics["playtime_control"]
        self.assertIsNotNone(control["spearman"])
        self.assertGreater(control["spearman"], 0)
        self.assertGreater(metrics["wall_seconds"], 0)

        # Rendering the report must not blow up on any of these shapes.
        self.assertIn("Spearman rho", eval_profile.render_markdown(metrics))

    async def test_baseline_scores_worse_than_the_model(self) -> None:
        model = await self._run(baseline=False)
        baseline = await self._run(baseline=True)

        self.assertTrue(baseline["config"]["baseline"])
        self.assertLess(baseline["pooled"]["spearman"], model["pooled"]["spearman"])

    async def test_input_database_is_never_mutated(self) -> None:
        async with get_db() as db:
            before_row = await db.execute_fetchone("SELECT COUNT(*) AS c FROM ratings")
        before_count = before_row["c"]
        before_stat = os.stat(self._db_path)

        await self._run(baseline=False)

        after_stat = os.stat(self._db_path)
        self.assertEqual(before_stat.st_size, after_stat.st_size)
        self.assertEqual(before_stat.st_mtime, after_stat.st_mtime)
        async with get_db() as db:
            after_row = await db.execute_fetchone("SELECT COUNT(*) AS c FROM ratings")
        self.assertEqual(before_count, after_row["c"])
        # DATABASE_URL must be handed back exactly as it was found.
        self.assertEqual(os.environ["DATABASE_URL"], f"file:{self._db_path}")

    async def test_refuses_a_library_with_too_few_ratings(self) -> None:
        with self.assertRaises(eval_profile.EvalError) as caught:
            await eval_profile.run_eval(self._db_path, folds=5, min_ratings=500)
        self.assertIn("at least 500", str(caught.exception))


class SpearmanTests(unittest.TestCase):
    """The one hand-rolled statistic in the harness (no scipy in this project)."""

    def test_monotonic_and_tied_inputs(self) -> None:
        self.assertAlmostEqual(
            eval_profile.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0
        )
        self.assertAlmostEqual(
            eval_profile.spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0
        )
        # Ties averaged: this is the textbook rho for these ranks.
        self.assertAlmostEqual(
            eval_profile.spearman([1, 2, 2, 3], [1, 2, 3, 4]), 0.9486832980505138
        )
        # Undefined rather than an exception.
        self.assertIsNone(eval_profile.spearman([1, 1, 1], [1, 2, 3]))
        self.assertIsNone(eval_profile.spearman([1, 2], [1, 2]))
