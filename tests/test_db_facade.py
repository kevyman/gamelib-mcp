"""Freezes the public import surface of the gamelib_mcp.data.db package.

~29 modules import names from gamelib_mcp.data.db, and several tests patch
db_module.<name> by string. As db.py is split into submodules, the package
__init__ must keep re-exporting every one of these. This test fails loudly if an
extraction drops a name from the facade.

The migration chain is the one deliberate exception: it lives in
gamelib_mcp.data.db.migrations and is NOT re-exported wholesale, since nothing
outside migrate_db calls a step. Only `_run_migrations` stays bound on the
facade (init_db's first-touch path reaches it by that name); the rest is frozen
against the migrations module instead, below.
"""

import unittest

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.db import migrations as migrations_module

# Names consumers and tests import or patch via gamelib_mcp.data.db.<name>.
EXPECTED_NAMES = {
    # constants
    "STEAM_PLATFORM",
    "STEAM_APP_ID",
    "EPIC_ARTIFACT_ID",
    "GOG_PRODUCT_ID",
    "SCHEMA_VERSION",
    "MigrationResult",
    # schema DDL (referenced by migration tests)
    "_V1_SCHEMA_DDL",
    "_V2_SCHEMA_DDL",
    "_V3_SCHEMA_DDL",
    "_V4_SCHEMA_DDL",
    "_V5_SCHEMA_DDL",
    # connection / init
    "get_db",
    "init_db",
    "migrate_db",
    "_db_path",
    "_configure_connection",
    "_DB_READY_PATH",
    # migration entry point (the chain itself lives in .migrations)
    "_run_migrations",
    # claims + batch loaders
    "clear_claim",
    "clear_all_enrichment_claims",
    "invalidate_name_derived_enrichment",
    "release_game_claim",
    "claim_game_ids_for_igdb",
    "claim_game_ids_for_hltb",
    "claim_steam_platform_ids_for_store",
    "claim_steam_platform_ids_for_protondb",
    "claim_steam_platform_ids_for_steamspy",
    "claim_game_platform_ids_for_opencritic",
    "claim_game_platform_ids_for_metacritic",
    "load_games_for_igdb_backfill",
    "load_store_batch_rows",
    "load_hltb_batch_rows",
    "load_steam_platform_batch_rows",
    "load_opencritic_batch_rows",
    "load_metacritic_batch_rows",
    # affinity
    "recompute_tag_affinity",
    # meta
    "get_meta",
    "get_meta_prefix",
    "set_meta",
    "set_meta_many",
    # lookups
    "get_game_by_identifier",
    "get_game_by_appid",
    "get_game_by_igdb_id",
    "get_game_by_name_exact",
    "get_steam_appid_for_game",
    "get_steam_platform_row_by_appid",
    # upserts
    "upsert_game",
    "upsert_game_platform",
    "upsert_game_platform_identifier",
    "upsert_steam_platform_data",
    "bulk_upsert_steam_library",
    "upsert_game_platform_enrichment",
    # fuzzy
    "extract_best_fuzzy_key",
    "load_fuzzy_candidates",
    "find_game_by_name_fuzzy",
    "titles_conflict_on_identity",
    # platform assembly
    "load_platforms_for_games",
    "_platform_dict",
}


# Migration internals tests and repair scripts reach as
# gamelib_mcp.data.db.migrations.<name>.
EXPECTED_MIGRATION_NAMES = {
    "_detect_schema_state",
    "_run_migrations",
    "_snapshot_before_migration",
    "_MIGRATION_STEPS",
    "_get_user_version",
    "_set_user_version",
    "_table_names",
    "_table_columns",
    "_repair_identifier_primary_flags",
    "_normalize_nintendo_title_ids",
    "_GAMES_TABLE_INDEXES",
}


class DbFacadeTests(unittest.TestCase):
    def test_all_expected_names_importable(self):
        missing = {name for name in EXPECTED_NAMES if not hasattr(db_module, name)}
        self.assertEqual(missing, set(), f"db facade dropped names: {sorted(missing)}")

    def test_migration_internals_importable(self):
        missing = {
            name for name in EXPECTED_MIGRATION_NAMES if not hasattr(migrations_module, name)
        }
        self.assertEqual(missing, set(), f"migrations dropped names: {sorted(missing)}")

    def test_migration_chain_is_not_re_exported(self):
        """Only _run_migrations crosses back onto the facade."""
        leaked = sorted(
            name
            for name in EXPECTED_MIGRATION_NAMES - {"_run_migrations"}
            if hasattr(db_module, name)
        )
        self.assertEqual(leaked, [], f"migration internals re-exported by the facade: {leaked}")


if __name__ == "__main__":
    unittest.main()
