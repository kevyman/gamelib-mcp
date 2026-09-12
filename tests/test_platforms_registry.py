"""Tests for the single platform registry and everything derived from it."""

import unittest

from gamelib_mcp import platforms_registry as reg
from gamelib_mcp.integrations import inspectors
from gamelib_mcp.tools import admin, common


class RegistryDerivationTests(unittest.TestCase):
    def test_derived_sets_match_expected_vocabulary(self):
        self.assertEqual(
            reg.SYNCABLE_PLATFORMS, frozenset({"steam", "epic", "gog", "switch2", "ps5", "xbox"})
        )
        self.assertEqual(
            reg.LIBRARY_PLATFORMS,
            reg.SYNCABLE_PLATFORMS | {"itchio", "ea", "ubisoft", "other"},
        )
        self.assertEqual(reg.WISHLIST_SYNCABLE_PLATFORMS, frozenset({"steam", "switch2"}))
        self.assertEqual(
            reg.PLATFORM_ALIASES,
            {"nintendo": "switch2", "switch": "switch2", "uplay": "ubisoft", "origin": "ea"},
        )
        self.assertEqual(
            reg.SYNC_METADATA_PLATFORMS, ("steam", "epic", "gog", "switch2", "ps5", "xbox")
        )
        self.assertEqual(reg.INSPECTOR_PLATFORM_ALIASES, {"switch2": "nintendo"})

    def test_only_epic_lists_nested_content(self):
        # Epic's catalog lists add-ons as their own entries; Steam's
        # GetOwnedGames omits DLC, GOG lists per base product, PSN's title list
        # is games only, and Nintendo's VGCS feed is per title. That is what
        # makes a missing sync stamp meaningless on a nested row anywhere but
        # Epic (check_library's spend.unconfirmed_ownership).
        self.assertEqual(reg.NESTED_LISTING_PLATFORMS, frozenset({"epic"}))
        self.assertTrue(reg.PLATFORMS_BY_NAME["epic"].source_lists_nested)
        for name in ("steam", "gog", "ps5", "switch2", "xbox"):
            with self.subTest(platform=name):
                self.assertFalse(reg.PLATFORMS_BY_NAME[name].source_lists_nested)

    def test_consumers_reexport_the_registry_objects(self):
        self.assertIs(common.PLATFORM_ALIASES, reg.PLATFORM_ALIASES)
        self.assertIs(common.SYNCABLE_PLATFORMS, reg.SYNCABLE_PLATFORMS)
        self.assertIs(common.LIBRARY_PLATFORMS, reg.LIBRARY_PLATFORMS)
        self.assertIs(admin.WISHLIST_SYNCABLE_PLATFORMS, reg.WISHLIST_SYNCABLE_PLATFORMS)

        from gamelib_mcp import lifecycle

        self.assertIs(lifecycle.SYNC_METADATA_PLATFORMS, reg.SYNC_METADATA_PLATFORMS)
        self.assertIs(lifecycle.INSPECTOR_PLATFORM_ALIASES, reg.INSPECTOR_PLATFORM_ALIASES)

    def test_sync_resolution_covers_every_syncable_platform(self):
        syncs = reg.resolve_platform_functions("sync")
        self.assertEqual(set(syncs), set(reg.SYNCABLE_PLATFORMS))
        wishlist = reg.resolve_platform_functions("wishlist_sync")
        self.assertEqual(set(wishlist), set(reg.WISHLIST_SYNCABLE_PLATFORMS))

    def test_namespace_binding_wins_over_module_import(self):
        # The pattern tests rely on: patching the function on tools.admin must
        # be what the sync dict picks up.
        sentinel = object()

        class FakeNamespace:
            sync_epic = sentinel

        syncs = reg.resolve_platform_functions("sync", namespace=FakeNamespace)
        self.assertIs(syncs["epic"], sentinel)
        # Platforms not bound on the namespace still resolve via import.
        from gamelib_mcp.data.gog import sync_gog

        self.assertIs(syncs["gog"], sync_gog)

    def test_inspectors_follow_registry(self):
        expected = {
            spec.inspector_name or spec.name
            for spec in reg.PLATFORMS
            if spec.inspector_attr is not None
        }
        self.assertEqual(set(inspectors.inspect_all_integrations()), expected)
        for spec in reg.PLATFORMS:
            if spec.inspector_attr is not None:
                self.assertTrue(hasattr(inspectors, spec.inspector_attr))


if __name__ == "__main__":
    unittest.main()
