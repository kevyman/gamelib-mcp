"""Tag hygiene: feature flags stay out of tags, affinity, and merges."""

import json
import unittest

from conftest import ToolDBTestCase, add_rating, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import steamspy
from gamelib_mcp.data.steam_store import _extract_tags
from gamelib_mcp.data.tags import is_feature_flag, split_features


class FeatureFlagFamilyTests(unittest.TestCase):
    def test_igdb_metadata_keyword_families_are_flags(self) -> None:
        # IGDB mints one keyword per storefront/expo/award instance — the
        # prefix families must catch members never seen before.
        for tag in [
            "previously on - prime gaming",
            "Previously On - Netflix",
            "available on - luna plus",
            "pax west 2017",
            "pax prime 2013",
            "gamescom 2014",
            "e3 2015",
            "the game awards - best narrative - nominee",
            "the game awards 2016",
            "game developers choice awards 2016",
            "kickstarter",
            "steam greenlight",
            "controller recommendation",
            "xbox controller support for pc",
            "free demo",
        ]:
            self.assertTrue(is_feature_flag(tag), tag)

    def test_real_taste_tags_are_not_flags(self) -> None:
        # Near-misses of the junk families must survive: "demon" vs "demo",
        # gameplay modes, and ordinary content keywords.
        for tag in [
            "demon",
            "demons",
            "single-player",
            "co-op",
            "deck-building",
            "dinosaurs",
            "e3-adjacent robots",  # does not start with "e3 "
        ]:
            self.assertFalse(is_feature_flag(tag), tag)


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
        # Real tags are canonicalized (lowercased) so Steam/IGDB/SteamSpy share one
        # vocabulary; feature flags keep their original surface form.
        self.assertEqual(json.loads(tags_json), ["action", "single-player"])
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
        # Canonicalized + deduped: "Souls-like"/"souls-like" collapse to one tag.
        self.assertEqual(merged, ["souls-like", "difficult", "action"])


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
