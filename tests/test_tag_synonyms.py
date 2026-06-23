"""Canonical tag normalization: synonym mapping, lowercase fallback, idempotency."""

import unittest

from gamelib_mcp.data.tag_synonyms import canonical_tag


class CanonicalTagTests(unittest.TestCase):
    def test_maps_known_synonym_variants(self) -> None:
        for variant in ("Souls-like", "soulslike", "SOULS LIKE", "souls_like"):
            self.assertEqual(canonical_tag(variant), "souls-like")
        for variant in ("Co-op", "COOP", "co op"):
            self.assertEqual(canonical_tag(variant), "co-op")
        self.assertEqual(canonical_tag("rogue-lite"), "roguelite")
        self.assertEqual(canonical_tag("metroid vania"), "metroidvania")
        self.assertEqual(canonical_tag("ARPG"), "action rpg")

    def test_unknown_tag_falls_through_as_plain_lowercase(self) -> None:
        # No separator collapsing on a miss — must match SQLite lower(value).
        self.assertEqual(canonical_tag("Action-Adventure"), "action-adventure")
        self.assertEqual(canonical_tag("Story Rich"), "story rich")
        self.assertEqual(canonical_tag("  Atmospheric  "), "atmospheric")

    def test_idempotent(self) -> None:
        samples = [
            "Souls-like", "co-op", "first-person", "side-scrolling", "roguelite",
            "roguelike", "action rpg", "online co-op", "Action-Adventure",
            "hack n slash", "random unmapped tag",
        ]
        for s in samples:
            once = canonical_tag(s)
            self.assertEqual(canonical_tag(once), once, f"not idempotent: {s!r}")


if __name__ == "__main__":
    unittest.main()
