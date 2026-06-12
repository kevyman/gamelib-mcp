"""Tag hygiene: feature flags stay out of tags, affinity, and merges."""

import json
import unittest

from conftest import ToolDBTestCase, add_rating, seed_game
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import steamspy
from gamelib_mcp.data.steam_store import _extract_tags
from gamelib_mcp.data.tags import split_features


class SplitFeaturesTests(unittest.TestCase):
    def test_splits_preserving_order(self) -> None:
        tags, features = split_features(
            ["Action", "Steam Trading Cards", "Roguelike", "Family Sharing"]
        )
        self.assertEqual(tags, ["Action", "Roguelike"])
        self.assertEqual(features, ["Steam Trading Cards", "Family Sharing"])

    def test_keeps_gameplay_mode_categories_as_tags(self) -> None:
        tags, features = split_features(
            ["Single-player", "Co-op", "Online PvP", "Shared/Split Screen", "Steam Cloud"]
        )
        self.assertEqual(
            tags, ["Single-player", "Co-op", "Online PvP", "Shared/Split Screen"]
        )
        self.assertEqual(features, ["Steam Cloud"])


class ExtractTagsTests(unittest.TestCase):
    def test_feature_categories_go_to_features(self) -> None:
        data = {
            "genres": [{"description": "Action"}],
            "categories": [
                {"description": "Single-player"},
                {"description": "Steam Achievements"},
                {"description": "Full controller support"},
            ],
        }
        tags_json, features_json = _extract_tags(data)
        self.assertEqual(json.loads(tags_json), ["Action", "Single-player"])
        self.assertEqual(
            json.loads(features_json),
            ["Steam Achievements", "Full controller support"],
        )


class MergeTagsTests(unittest.TestCase):
    def test_drops_lingering_feature_flags_from_existing(self) -> None:
        merged = steamspy._merge_tags(
            ["Souls-like", "Difficult"],
            ["Action", "Steam Trading Cards", "souls-like"],
        )
        self.assertEqual(merged, ["Souls-like", "Difficult", "Action"])


class AffinityHygieneTests(ToolDBTestCase):
    async def test_recompute_skips_feature_flags(self) -> None:
        gid = await seed_game(
            "Hades", tags=["Roguelike", "Steam Achievements", "Action"]
        )
        await add_rating(gid, "backloggd", raw_score=5.0, normalized_score=10.0)

        updated = await db_module.recompute_tag_affinity()

        async with db_module.get_db() as db:
            rows = await db.execute_fetchall("SELECT tag FROM tag_affinity")
        tags = {row["tag"] for row in rows}
        self.assertEqual(tags, {"roguelike", "action"})
        self.assertEqual(updated, 2)


if __name__ == "__main__":
    unittest.main()
