"""Classifier foundation: Steam/category mappings, title splitting, and the
reusable apply_content_classification writer with its guard matrix.

The pure classifier tests are sync; the writer tests use ToolDBTestCase (temp
SQLite + real migrations) and exercise the production upsert paths.
"""

import json

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

    async def test_nesting_a_row_with_children_is_refused(self):
        # The Fallout: New Vegas shape — a store/IGDB pass calls the base game an
        # "edition". Nesting it would hide it from the rollups AND strand the DLC
        # hanging off it, so the whole classification write is dropped.
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        base_id = await seed_game("Fallout: New Vegas")
        await seed_game(
            "Fallout New Vegas: Dead Money",
            content_type=content.CONTENT_DLC,
            parent_game_id=base_id,
            is_primary_library_item=0,
        )

        wrote = await apply_content_classification(
            base_id, _nested(content.CONTENT_EDITION), source="test"
        )
        self.assertFalse(wrote)
        row = await _get_content_row(base_id)
        self.assertEqual(row["content_type"], content.CONTENT_BASE_GAME)
        self.assertEqual(row["is_primary_library_item"], 1)
        self.assertIsNone(row["parent_game_id"])

    async def test_manual_override_on_content_type_blocks_write(self):
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import (
            apply_content_classification,
            apply_manual_game_fields,
        )

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

    async def test_pinned_content_type_keeps_is_primary_derived(self):
        # A row pinned to 'dlc' whose is_primary override was later cleared
        # (update_game clear_overrides=["is_primary_library_item"]) must not be
        # flipped primary by a later non-default primary verdict: is_primary
        # derives from the content_type ACTUALLY stored (the pinned 'dlc').
        from gamelib_mcp.data.content import _primary
        from gamelib_mcp.data.db import apply_content_classification, get_db

        parent_id = await seed_game("Base Game")
        child_id = await seed_game(
            "Base Game: The DLC",
            content_type=content.CONTENT_DLC,
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        async with get_db() as db:
            await db.execute(
                "UPDATE games SET manual_overrides = ? WHERE id = ?",
                (json.dumps(["content_type"]), child_id),
            )
            await db.commit()

        # Non-default primary signal (remaster): content_type write is pinned
        # away, so is_primary must stay derived from the stored 'dlc'.
        await apply_content_classification(
            child_id, _primary(content.CONTENT_REMASTER), source="test"
        )
        row = await _get_content_row(child_id)
        self.assertEqual(row["content_type"], content.CONTENT_DLC)
        self.assertEqual(row["is_primary_library_item"], 0)

    async def test_concurrent_write_discards_stale_verdict(self):
        # The read->guard->write spans awaits and the Steam path has no claim
        # serialization: if another writer lands a fresh classification between
        # our snapshot and our UPDATE, the compare-and-swap must discard ours.
        from unittest.mock import patch

        from gamelib_mcp.data.content import _primary
        from gamelib_mcp.data.db import (
            apply_content_classification,
            get_db,
            get_manual_overrides,
        )
        from gamelib_mcp.data.db import upserts as upserts_module

        parent_id = await seed_game("Raced Base")
        child_id = await seed_game("Raced Base: DLC")

        async def overrides_with_concurrent_write(db, game_id):
            # Simulate the other enricher committing a nested verdict between
            # our snapshot read and our UPDATE.
            async with get_db() as other:
                await other.execute(
                    "UPDATE games SET content_type = 'dlc', parent_game_id = ?, "
                    "is_primary_library_item = 0 WHERE id = ?",
                    (parent_id, game_id),
                )
                await other.commit()
            return await get_manual_overrides(db, game_id)

        with patch.object(
            upserts_module, "get_manual_overrides", overrides_with_concurrent_write
        ):
            wrote = await apply_content_classification(
                child_id, _primary(content.CONTENT_REMASTER), source="test"
            )

        self.assertFalse(wrote)
        row = await _get_content_row(child_id)
        # The concurrent (fresher) verdict survives; the stale one is discarded.
        self.assertEqual(row["content_type"], content.CONTENT_DLC)
        self.assertEqual(row["parent_game_id"], parent_id)
        self.assertEqual(row["is_primary_library_item"], 0)


# --- parent_name_candidates -----------------------------------------------------

def test_parent_candidates_strip_addon_suffix_longest_first():
    # "Season Pass" is not separator-delimited, so split_addon_title alone
    # would offer only "Deus Ex" (the wrong, earliest franchise entry). The
    # suffix-stripped full title must come first.
    assert content.parent_name_candidates("Deus Ex: Mankind Divided Season Pass") == [
        "Deus Ex: Mankind Divided",
        "Deus Ex",
    ]


def test_parent_candidates_order_all_forms_longest_first():
    assert content.parent_name_candidates("Saints Row: The Third Season Pass") == [
        "Saints Row: The Third",
        "Saints Row",
    ]


def test_parent_candidates_plain_split_still_works():
    assert content.parent_name_candidates("The Binding of Isaac: Afterbirth") == [
        "The Binding of Isaac",
    ]


def test_parent_candidates_soundtrack_and_stacked_suffixes():
    assert content.parent_name_candidates("Two Worlds Soundtrack")[0] == "Two Worlds"
    # Stacked suffixes peel iteratively.
    assert (
        content.parent_name_candidates("Hollow Knight: Silksong Original Soundtrack")[0]
        == "Hollow Knight: Silksong"
    )


def test_parent_candidates_no_suffix_no_separator_is_empty():
    assert content.parent_name_candidates("Celeste") == []


# --- substance guard (nesting a real game under an empty shell) -----------------

class NestingSubstanceGuardTests(ToolDBTestCase):
    async def _real_game(self, name: str, appid: int, minutes: int) -> int:
        from gamelib_mcp.data import db as db_module

        game_id = await seed_game(name)
        gpid = await db_module.upsert_game_platform(
            game_id, "steam", playtime_minutes=minutes, owned=1
        )
        await db_module.upsert_game_platform_identifier(gpid, "steam_appid", appid)
        return game_id

    async def test_classifier_refuses_to_nest_real_game_under_empty_parent(self):
        # The Titanfall 2 shape: the real, played row (store id + playtime)
        # offered a parent that owns nothing — the demotion must be dropped.
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        empty_parent = await seed_game("Titanfall II")
        real_id = await self._real_game("Titanfall 2", 1237970, minutes=1200)

        wrote = await apply_content_classification(
            real_id, _nested(content.CONTENT_EDITION), source="test",
            parent_game_id=empty_parent,
        )
        self.assertFalse(wrote)
        row = await _get_content_row(real_id)
        self.assertEqual(row["content_type"], content.CONTENT_BASE_GAME)
        self.assertEqual(row["is_primary_library_item"], 1)
        self.assertIsNone(row["parent_game_id"])

    async def test_nesting_under_substantial_parent_still_works(self):
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        parent_id = await self._real_game("Fallout: New Vegas", 22380, minutes=2694)
        child_id = await self._real_game("Fallout New Vegas Ultimate Edition", 22381, minutes=5)

        wrote = await apply_content_classification(
            child_id, _nested(content.CONTENT_EDITION), source="test",
            parent_game_id=parent_id,
        )
        self.assertTrue(wrote)
        row = await _get_content_row(child_id)
        self.assertEqual(row["content_type"], content.CONTENT_EDITION)
        self.assertEqual(row["parent_game_id"], parent_id)

    async def test_insubstantial_child_may_nest_under_empty_parent(self):
        # A row with no identifier/playtime (e.g. an importer-minted soundtrack)
        # is exactly what nesting is for — the guard must not block it.
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        parent_id = await seed_game("Some Base Game")
        child_id = await seed_game("Some Base Game Soundtrack")

        wrote = await apply_content_classification(
            child_id, _nested(content.CONTENT_UNKNOWN_ADDON), source="test",
            parent_game_id=parent_id,
        )
        self.assertTrue(wrote)


class EditionOwnershipGuardTests(ToolDBTestCase):
    """Owned edition rows are never demoted under unowned parents.

    The substance guard above is playtime-keyed; owned-but-UNPLAYED edition
    SKUs slipped through it and got hidden behind empty shell parents.
    """

    async def _owned_unplayed(self, name: str, appid: int) -> int:
        from gamelib_mcp.data import db as db_module

        game_id = await seed_game(name)
        gpid = await db_module.upsert_game_platform(
            game_id, "steam", playtime_minutes=0, owned=1
        )
        await db_module.upsert_game_platform_identifier(gpid, "steam_appid", appid)
        return game_id

    async def test_owned_unplayed_edition_refused_under_unowned_parent(self):
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        shell = await seed_game("Crysis 2")
        real_id = await self._owned_unplayed("Crysis 2 Maximum Edition", 108800)

        wrote = await apply_content_classification(
            real_id, _nested(content.CONTENT_EDITION), source="test",
            parent_game_id=shell,
        )
        self.assertFalse(wrote)
        row = await _get_content_row(real_id)
        self.assertEqual(row["content_type"], content.CONTENT_BASE_GAME)
        self.assertEqual(row["is_primary_library_item"], 1)
        self.assertIsNone(row["parent_game_id"])

    async def test_owned_unplayed_edition_refused_parentless_demotion(self):
        # No parent resolved at all: demoting the owned row to a parentless
        # 'edition' would hide it from every view — refuse that too.
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        real_id = await self._owned_unplayed("Valkyria Chronicles 4 Complete Edition", 1489630)

        wrote = await apply_content_classification(
            real_id, _nested(content.CONTENT_EDITION), source="test"
        )
        self.assertFalse(wrote)
        row = await _get_content_row(real_id)
        self.assertEqual(row["content_type"], content.CONTENT_BASE_GAME)

    async def test_owned_edition_nests_under_owned_parent(self):
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        parent_id = await self._owned_unplayed("Fallout: New Vegas", 22380)
        child_id = await self._owned_unplayed("Fallout New Vegas Ultimate Edition", 22490)

        wrote = await apply_content_classification(
            child_id, _nested(content.CONTENT_EDITION), source="test",
            parent_game_id=parent_id,
        )
        self.assertTrue(wrote)
        row = await _get_content_row(child_id)
        self.assertEqual(row["content_type"], content.CONTENT_EDITION)
        self.assertEqual(row["parent_game_id"], parent_id)

    async def test_unowned_edition_row_still_nests_under_unowned_parent(self):
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        parent_id = await seed_game("Dying Light 2 Stay Human")
        child_id = await seed_game("Dying Light 2: Reloaded Edition")

        wrote = await apply_content_classification(
            child_id, _nested(content.CONTENT_EDITION), source="test",
            parent_game_id=parent_id,
        )
        self.assertTrue(wrote)
        row = await _get_content_row(child_id)
        self.assertEqual(row["content_type"], content.CONTENT_EDITION)
        self.assertEqual(row["parent_game_id"], parent_id)

    async def test_owned_dlc_still_nests_under_unowned_parent(self):
        # Guard is edition-scoped: owning a DLC without its base game is a real
        # shape (Epic giveaways) and must stay nestable.
        from gamelib_mcp.data.content import _nested
        from gamelib_mcp.data.db import apply_content_classification

        parent_id = await seed_game("Train Sim World 3")
        child_id = await self._owned_unplayed("Train Sim World 3: Bakerloo Line", 999901)

        wrote = await apply_content_classification(
            child_id, _nested(content.CONTENT_DLC), source="test",
            parent_game_id=parent_id,
        )
        self.assertTrue(wrote)
        row = await _get_content_row(child_id)
        self.assertEqual(row["content_type"], content.CONTENT_DLC)
        self.assertEqual(row["parent_game_id"], parent_id)


class PrimaryClassificationParentTests(ToolDBTestCase):
    async def test_primary_classification_clears_stored_wrong_parent(self):
        # A primary verdict detaches the parent link a wrong earlier nested
        # classification wrote — primary items must not keep parents.
        from gamelib_mcp.data.content import _primary
        from gamelib_mcp.data.db import apply_content_classification

        wrong_parent = await seed_game("Sid Meier's Colonization")
        game_id = await seed_game(
            "Sid Meier's Civilization IV: Colonization",
            content_type=content.CONTENT_STANDALONE_EXPANSION,
            parent_game_id=wrong_parent,
            is_primary_library_item=1,
        )

        wrote = await apply_content_classification(
            game_id, _primary(content.CONTENT_STANDALONE_EXPANSION), source="test"
        )
        self.assertTrue(wrote)
        row = await _get_content_row(game_id)
        self.assertEqual(row["content_type"], content.CONTENT_STANDALONE_EXPANSION)
        self.assertIsNone(row["parent_game_id"])
        self.assertEqual(row["is_primary_library_item"], 1)

    async def test_parent_kwarg_ignored_for_primary_classification(self):
        # A resolved parent handed alongside a primary verdict is dropped: the
        # write must not produce the "primary row with a parent" contradiction.
        from gamelib_mcp.data.content import _primary
        from gamelib_mcp.data.db import apply_content_classification

        other = await seed_game("Original Game")
        game_id = await seed_game("Original Game Remake")

        wrote = await apply_content_classification(
            game_id, _primary(content.CONTENT_REMAKE), source="test",
            parent_game_id=other,
        )
        self.assertTrue(wrote)
        row = await _get_content_row(game_id)
        self.assertEqual(row["content_type"], content.CONTENT_REMAKE)
        self.assertIsNone(row["parent_game_id"])
        self.assertEqual(row["is_primary_library_item"], 1)


# --- classify_igdb_game: primary overrides drop inherited parents ---------------

def test_primary_override_drops_igdb_parent_linkage():
    # IGDB lists Civ IV: Colonization with the 1994 "Sid Meier's Colonization"
    # as parent; the primary override must not inherit it (it minted a phantom
    # parent row and chained two distinct games together).
    result = content.classify_igdb_game(
        title="Sid Meier's Civilization IV: Colonization",
        category=4,
        parent_name="Sid Meier's Colonization",
        parent_igdb_id=4321,
    )
    assert result.content_type == content.CONTENT_STANDALONE_EXPANSION
    assert result.is_primary_library_item is True
    assert result.parent_name is None
    assert result.parent_igdb_id is None


def test_nested_override_still_inherits_igdb_parent_when_it_names_none():
    # The inheritance exists for nested overrides without their own parent —
    # that behavior must survive the primary-override fix.
    result = content.classify_igdb_game(
        title="Outlast: Whistleblower",
        category=1,
        parent_igdb_id=5678,
    )
    assert result.content_type == content.CONTENT_DLC
    assert result.parent_name == "Outlast"
    assert result.parent_igdb_id == 5678


class UpsertGameDerivesPrimaryTests(ToolDBTestCase):
    async def test_content_type_without_flag_derives_is_primary(self):
        # A caller passing content_type alone must never produce the
        # contradictory "nested type + primary flag" shape.
        from gamelib_mcp.data.db import upsert_game

        game_id = await upsert_game(None, "Passed Nested", content_type=content.CONTENT_DLC)
        row = await _get_content_row(game_id)
        self.assertEqual(row["content_type"], content.CONTENT_DLC)
        self.assertEqual(row["is_primary_library_item"], 0)

        game_id2 = await upsert_game(None, "Passed Primary", content_type=content.CONTENT_REMAKE)
        row2 = await _get_content_row(game_id2)
        self.assertEqual(row2["is_primary_library_item"], 1)
