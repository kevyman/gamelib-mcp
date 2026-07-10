"""Classifier foundation: Steam/category mappings, title splitting, and the
reusable apply_content_classification writer with its guard matrix.

The pure classifier tests are sync; the writer tests use ToolDBTestCase (temp
SQLite + real migrations) and exercise the production upsert paths.
"""

from conftest import ToolDBTestCase, seed_game

from gamelib_mcp.data import content


# --- category 5/6/7 mappings --------------------------------------------------

def test_category_5_mod_is_primary_base_game():
    # A mod is independently playable, so it stays a primary library item.
    assert content.content_type_from_igdb_category(5) == content.CONTENT_BASE_GAME


def test_category_6_episode_is_nested_dlc():
    # An episode is a sub-purchase of a parent — nested like DLC.
    assert content.content_type_from_igdb_category(6) == content.CONTENT_DLC


def test_category_7_season_is_primary_base_game():
    # A season (Telltale-style) is the sellable, playable unit.
    assert content.content_type_from_igdb_category(7) == content.CONTENT_BASE_GAME


# --- classify_steam_app_type --------------------------------------------------

def test_steam_game_type_is_primary_base_game():
    result = content.classify_steam_app_type("game")
    assert result is not None
    assert result.content_type == content.CONTENT_BASE_GAME
    assert result.is_primary_library_item is True


def test_steam_dlc_type_is_nested_with_fullgame_threaded():
    result = content.classify_steam_app_type(
        "dlc", fullgame_name="Terraria", fullgame_appid=105600
    )
    assert result is not None
    assert result.content_type == content.CONTENT_DLC
    assert result.is_primary_library_item is False
    assert result.parent_name == "Terraria"
    assert result.parent_steam_appid == 105600


def test_steam_music_and_demo_are_unknown_addon_noise():
    music = content.classify_steam_app_type("music", fullgame_name="Celeste")
    demo = content.classify_steam_app_type("demo", fullgame_name="Celeste")
    for result in (music, demo):
        assert result is not None
        assert result.content_type == content.CONTENT_UNKNOWN_ADDON
        assert result.is_primary_library_item is False
        assert result.parent_name == "Celeste"


def test_steam_unknown_and_none_types_return_none():
    # Only map types we understand; everything else is "no signal".
    assert content.classify_steam_app_type(None) is None
    assert content.classify_steam_app_type("video") is None
    assert content.classify_steam_app_type("hardware") is None
    assert content.classify_steam_app_type("mod") is None
    assert content.classify_steam_app_type("series") is None


def test_steam_title_override_wins_over_type():
    # A known title override takes precedence over the raw Steam type, matching
    # classify_igdb_game's override-first behavior.
    result = content.classify_steam_app_type(
        "game", title="Fallout New Vegas: Dead Money"
    )
    assert result is not None
    assert result.content_type == content.CONTENT_DLC
    assert result.parent_name == "Fallout: New Vegas"
    assert result.is_primary_library_item is False


def test_steam_compilation_title_with_dlc_type_stays_nested():
    # Steam's own type is authoritative for its store — the "A + B" compilation
    # escape (an IGDB-only fix) does NOT apply here, so type dlc stays nested.
    result = content.classify_steam_app_type(
        "dlc", title="Portal + Portal 2", fullgame_name="Portal"
    )
    assert result is not None
    assert result.content_type == content.CONTENT_DLC
    assert result.is_primary_library_item is False


# --- split_addon_title --------------------------------------------------------

def test_split_addon_title_colon():
    assert content.split_addon_title("The Witcher 3: Blood and Wine") == ["The Witcher 3"]


def test_split_addon_title_dash_variants():
    assert content.split_addon_title("Game - Expansion") == ["Game"]
    assert content.split_addon_title("Game – Expansion") == ["Game"]
    assert content.split_addon_title("Game — Expansion") == ["Game"]


def test_split_addon_title_multiple_separators_longest_first():
    assert content.split_addon_title("A: B: C") == ["A: B", "A"]


def test_split_addon_title_mixed_separators_ordering():
    # Colon candidates come before dash candidates.
    assert content.split_addon_title("A: B - C") == ["A", "A: B"]


def test_split_addon_title_no_separator_is_empty():
    assert content.split_addon_title("Hollow Knight") == []


def test_split_addon_title_dedupes_and_excludes_whole_title():
    # A leading separator yields an empty/whitespace left side that is dropped.
    assert content.split_addon_title(": Subtitle") == []


# --- derive_is_primary --------------------------------------------------------

def test_derive_is_primary_matches_primary_set():
    assert content.derive_is_primary(content.CONTENT_BASE_GAME) is True
    assert content.derive_is_primary(content.CONTENT_STANDALONE_EXPANSION) is True
    assert content.derive_is_primary(content.CONTENT_DLC) is False
    assert content.derive_is_primary(content.CONTENT_EXPANSION) is False
    assert content.derive_is_primary(content.CONTENT_UNKNOWN_ADDON) is False


# --- resolve_parent_game ------------------------------------------------------

class ResolveParentGameTest(ToolDBTestCase):
    async def test_resolves_by_steam_appid(self):
        from conftest import add_platform, add_steam_appid
        from gamelib_mcp.data.db import resolve_parent_game

        parent_id = await seed_game("Parent Game")
        gpid = await add_platform(parent_id, "steam")
        await add_steam_appid(gpid, 4242)

        resolved = await resolve_parent_game(None, steam_appid=4242)
        self.assertEqual(resolved, parent_id)

    async def test_resolves_by_exact_name(self):
        from gamelib_mcp.data.db import resolve_parent_game

        parent_id = await seed_game("Some Base Game")
        resolved = await resolve_parent_game("some base game")  # case-insensitive
        self.assertEqual(resolved, parent_id)

    async def test_resolves_by_normalized_name(self):
        from gamelib_mcp.data.db import resolve_parent_game

        # Trademark noise differs from the exact name but normalizes to the same
        # key, so the normalized fallback (after the exact-name miss) finds it.
        parent_id = await seed_game("Hollow Knight™")
        resolved = await resolve_parent_game("Hollow Knight")
        self.assertEqual(resolved, parent_id)

    async def test_excludes_child_game_id(self):
        from gamelib_mcp.data.db import resolve_parent_game

        game_id = await seed_game("Self Reference")
        resolved = await resolve_parent_game("Self Reference", exclude_game_id=game_id)
        self.assertIsNone(resolved)

    async def test_create_true_mints_primary_row(self):
        from gamelib_mcp.data.db import get_db, resolve_parent_game

        resolved = await resolve_parent_game("Brand New Parent", create=True)
        self.assertIsNotNone(resolved)
        async with get_db() as db:
            row = await db.execute_fetchone(
                "SELECT name FROM games WHERE id = ?", (resolved,)
            )
        self.assertEqual(row["name"], "Brand New Parent")

    async def test_create_false_returns_none_when_missing(self):
        from gamelib_mcp.data.db import resolve_parent_game

        resolved = await resolve_parent_game("Nonexistent Parent", create=False)
        self.assertIsNone(resolved)


# --- apply_content_classification guard matrix --------------------------------

async def _get_content_row(game_id: int):
    from gamelib_mcp.data.db import get_db

    async with get_db() as db:
        return await db.execute_fetchone(
            "SELECT content_type, parent_game_id, is_primary_library_item "
            "FROM games WHERE id = ?",
            (game_id,),
        )


class ApplyContentClassificationTest(ToolDBTestCase):
    async def test_writes_nested_dlc_with_pre_resolved_parent(self):
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        parent_id = await seed_game("Parent")
        child_id = await seed_game("Parent DLC")

        wrote = await apply_content_classification(
            child_id, _nested(content.CONTENT_DLC), source="test",
            parent_game_id=parent_id,
        )
        self.assertTrue(wrote)
        row = await _get_content_row(child_id)
        self.assertEqual(row["content_type"], content.CONTENT_DLC)
        self.assertEqual(row["parent_game_id"], parent_id)
        self.assertEqual(row["is_primary_library_item"], 0)

    async def test_manual_override_on_content_type_blocks_write(self):
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification, apply_manual_game_fields

        game_id = await seed_game("Pinned Game")
        # update_game pins content_type together with its derived
        # is_primary_library_item, so both columns are protected — the classifier
        # must not overwrite either.
        await apply_manual_game_fields(
            game_id,
            {"content_type": content.CONTENT_BASE_GAME, "is_primary_library_item": 1},
        )

        wrote = await apply_content_classification(
            game_id, _nested(content.CONTENT_DLC), source="test"
        )
        self.assertFalse(wrote)
        row = await _get_content_row(game_id)
        self.assertEqual(row["content_type"], content.CONTENT_BASE_GAME)
        self.assertEqual(row["is_primary_library_item"], 1)

    async def test_default_signal_does_not_clobber_stored_dlc(self):
        from gamelib_mcp.data.content import _primary
        from gamelib_mcp.data.db import apply_content_classification

        parent_id = await seed_game("Base")
        child_id = await seed_game(
            "Base DLC",
            content_type=content.CONTENT_DLC,
            parent_game_id=None,
            is_primary_library_item=0,
        )
        # A bare base_game/primary/no-parent signal must not resurface the DLC.
        wrote = await apply_content_classification(
            child_id, _primary(content.CONTENT_BASE_GAME), source="test"
        )
        self.assertFalse(wrote)
        row = await _get_content_row(child_id)
        self.assertEqual(row["content_type"], content.CONTENT_DLC)
        self.assertEqual(row["is_primary_library_item"], 0)
        # parent_id unused beyond establishing an unrelated base row.
        self.assertNotEqual(parent_id, child_id)

    async def test_non_default_overwrites_stored_default(self):
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        parent_id = await seed_game("Parent")
        # Default stored state (base_game/primary/no parent) is overwritable.
        child_id = await seed_game("Parent Expansion")
        wrote = await apply_content_classification(
            child_id, _nested(content.CONTENT_EXPANSION), source="test",
            parent_game_id=parent_id,
        )
        self.assertTrue(wrote)
        row = await _get_content_row(child_id)
        self.assertEqual(row["content_type"], content.CONTENT_EXPANSION)
        self.assertEqual(row["parent_game_id"], parent_id)
        self.assertEqual(row["is_primary_library_item"], 0)

    async def test_self_parent_is_dropped(self):
        from gamelib_mcp.data.content import _primary
        from gamelib_mcp.data.db import apply_content_classification

        game_id = await seed_game("Solo")
        wrote = await apply_content_classification(
            game_id, _primary(content.CONTENT_REMAKE), source="test",
            parent_game_id=game_id,
        )
        self.assertTrue(wrote)
        row = await _get_content_row(game_id)
        self.assertEqual(row["content_type"], content.CONTENT_REMAKE)
        self.assertIsNone(row["parent_game_id"])
        self.assertEqual(row["is_primary_library_item"], 1)

    async def test_is_primary_always_derived_from_content_type(self):
        from dataclasses import replace

        from gamelib_mcp.data.content import _primary
        from gamelib_mcp.data.db import apply_content_classification

        # Even if a classification wrongly claims primary for a nested type, the
        # written is_primary_library_item is derived from content_type.
        game_id = await seed_game("Mislabeled")
        parent_id = await seed_game("Its Parent")
        bogus = replace(_primary(content.CONTENT_DLC), is_primary_library_item=True)
        wrote = await apply_content_classification(
            game_id, bogus, source="test", parent_game_id=parent_id
        )
        self.assertTrue(wrote)
        row = await _get_content_row(game_id)
        self.assertEqual(row["is_primary_library_item"], 0)

    async def test_resolves_parent_by_name_without_minting(self):
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification, get_db

        parent_id = await seed_game("Named Parent")
        child_id = await seed_game("Named Parent DLC")
        wrote = await apply_content_classification(
            child_id, _nested(content.CONTENT_DLC, "Named Parent"), source="test"
        )
        self.assertTrue(wrote)
        row = await _get_content_row(child_id)
        self.assertEqual(row["parent_game_id"], parent_id)
        # Never mints: only the two seeded rows exist.
        async with get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS n FROM games")
        self.assertEqual(count["n"], 2)

    async def test_unresolved_parent_name_writes_content_type_only(self):
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        child_id = await seed_game("Orphan DLC")
        wrote = await apply_content_classification(
            child_id, _nested(content.CONTENT_DLC, "No Such Parent"), source="test"
        )
        self.assertTrue(wrote)
        row = await _get_content_row(child_id)
        self.assertEqual(row["content_type"], content.CONTENT_DLC)
        self.assertIsNone(row["parent_game_id"])
        self.assertEqual(row["is_primary_library_item"], 0)
