"""Regression tests for content-classification persistence in _apply_igdb_metadata.

A later IGDB pass can return a bare main-game hit (default "base_game"/primary,
no parent). Applying that must not clobber a game already classified as nested
DLC/expansion, which would resurface it as its own primary library item.
"""

import json

from conftest import ToolDBTestCase, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import igdb


async def _read_tags(game_id: int) -> list[str]:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone("SELECT tags FROM games WHERE id = ?", (game_id,))
    return json.loads(row["tags"]) if row["tags"] else []


async def _read_classification(game_id: int) -> dict:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone(
            "SELECT content_type, parent_game_id, is_primary_library_item "
            "FROM games WHERE id = ?",
            (game_id,),
        )
    return dict(row)


class ApplyIgdbMetadataGuardTests(ToolDBTestCase):
    async def test_default_refetch_preserves_nested_classification(self) -> None:
        parent_id = await seed_game("Fallout: New Vegas")
        dlc_id = await seed_game(
            "Fallout New Vegas: Dead Money",
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )

        # A bare main-game re-fetch (the default classification) must not flip
        # the DLC back to a primary library item.
        await igdb._apply_igdb_metadata(
            dlc_id,
            igdb.IGDBGame(igdb_id=999, name="Fallout New Vegas: Dead Money", category=0, first_release_date=None),
        )

        after = await _read_classification(dlc_id)
        self.assertEqual(after["content_type"], "dlc")
        self.assertEqual(after["parent_game_id"], parent_id)
        self.assertEqual(after["is_primary_library_item"], 0)

    async def test_nested_classification_still_applies_to_default_row(self) -> None:
        parent_id = await seed_game("Sid Meier's Civilization IV")
        game_id = await seed_game("Sid Meier's Civilization IV: Warlords")

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=1000,
                name="Sid Meier's Civilization IV: Warlords",
                category=2,
                first_release_date=None,
                content_type="expansion",
                parent_name="Sid Meier's Civilization IV",
                is_primary_library_item=False,
            ),
        )

        after = await _read_classification(game_id)
        self.assertEqual(after["content_type"], "expansion")
        self.assertEqual(after["parent_game_id"], parent_id)
        self.assertEqual(after["is_primary_library_item"], 0)


    async def test_self_referential_parent_is_dropped_and_kept_primary(self) -> None:
        # IGDB returns an "edition" whose parent name resolves back to this same
        # row. We must not write parent_game_id = game_id (which would orphan the
        # row from both search and its parent's editions list); instead drop the
        # self-parent and keep it a primary library item.
        game_id = await seed_game("The House in Fata Morgana")

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=3000,
                name="The House in Fata Morgana",
                category=0,
                first_release_date=None,
                content_type="edition",
                parent_name="The House in Fata Morgana",
                is_primary_library_item=False,
            ),
        )

        after = await _read_classification(game_id)
        self.assertIsNone(after["parent_game_id"])
        self.assertEqual(after["is_primary_library_item"], 1)
        # is_primary is always DERIVED from content_type: forcing the row
        # primary must force the nested content_type back to base_game too —
        # a 'edition' + primary row would be invisible to both the games and
        # addons views.
        self.assertEqual(after["content_type"], "base_game")

    async def test_nested_verdict_on_a_parent_row_is_refused(self) -> None:
        # IGDB hands back an edition verdict (version_parent) for a base game that
        # other rows already nest under — the shape that hid BOTH Fallout: New
        # Vegas rows. The classification must be left alone; metadata still lands.
        base_id = await seed_game("Fallout: New Vegas")
        await seed_game(
            "Fallout New Vegas: Dead Money",
            content_type="dlc",
            parent_game_id=base_id,
            is_primary_library_item=0,
        )

        await igdb._apply_igdb_metadata(
            base_id,
            igdb.IGDBGame(
                igdb_id=5000,
                name="Fallout New Vegas Ultimate Edition",
                category=0,
                first_release_date=None,
                content_type="edition",
                is_primary_library_item=False,
                tags=["rpg"],
            ),
        )

        after = await _read_classification(base_id)
        self.assertEqual(after["content_type"], "base_game")
        self.assertEqual(after["is_primary_library_item"], 1)
        self.assertIsNone(after["parent_game_id"])
        # The guard skips only the classification — the rest of the fetch applies.
        self.assertEqual(await _read_tags(base_id), ["rpg"])

    async def test_refused_nesting_does_not_mint_the_unused_parent(self) -> None:
        # Same refusal, but the verdict names a parent that isn't in the library.
        # Resolving it would upsert_game() a row nothing ends up pointing at, since
        # the parent_game_id write is about to be dropped — so don't resolve at all.
        base_id = await seed_game("Fallout: New Vegas")
        await seed_game(
            "Fallout New Vegas: Dead Money",
            content_type="dlc",
            parent_game_id=base_id,
            is_primary_library_item=0,
        )

        await igdb._apply_igdb_metadata(
            base_id,
            igdb.IGDBGame(
                igdb_id=5001,
                name="Fallout New Vegas Ultimate Edition",
                category=0,
                first_release_date=None,
                content_type="edition",
                is_primary_library_item=False,
                parent_name="Fallout: New Vegas Collection",
                tags=["rpg"],
            ),
        )

        async with db_module.get_db() as db:
            minted = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ?", ("Fallout: New Vegas Collection",)
            )
        self.assertIsNone(minted)

        after = await _read_classification(base_id)
        self.assertEqual(after["content_type"], "base_game")
        self.assertEqual(after["is_primary_library_item"], 1)
        self.assertIsNone(after["parent_game_id"])
        self.assertEqual(await _read_tags(base_id), ["rpg"])

    async def test_pinned_content_type_keeps_is_primary_derived(self) -> None:
        # A row pinned to 'dlc' whose is_primary override was later cleared
        # (update_game clear_overrides=["is_primary_library_item"]) must NOT be
        # flipped primary by a later non-default primary verdict: is_primary
        # derives from the content_type that is ACTUALLY stored (the pinned
        # 'dlc'), not the incoming one.
        parent_id = await seed_game("Bloodborne")
        dlc_id = await seed_game(
            "Bloodborne: The Old Hunters",
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET manual_overrides = ? WHERE id = ?",
                (json.dumps(["content_type"]), dlc_id),
            )
            await db.commit()

        # Non-default primary verdict (remaster) — content_type write is pinned
        # away, so is_primary must stay derived from the stored 'dlc'.
        await igdb._apply_igdb_metadata(
            dlc_id,
            igdb.IGDBGame(
                igdb_id=4000,
                name="Bloodborne: The Old Hunters",
                category=9,
                first_release_date=None,
                content_type="remaster",
                is_primary_library_item=True,
            ),
        )

        after = await _read_classification(dlc_id)
        self.assertEqual(after["content_type"], "dlc")
        self.assertEqual(after["is_primary_library_item"], 0)


class ApplyIgdbMetadataSubstanceGuardTests(ToolDBTestCase):
    async def _real_game(self, name: str, appid: int, minutes: int) -> int:
        game_id = await seed_game(name)
        gpid = await db_module.upsert_game_platform(
            game_id, "steam", playtime_minutes=minutes, owned=1
        )
        await db_module.upsert_game_platform_identifier(gpid, "steam_appid", appid)
        return game_id

    async def test_wrong_version_parent_cannot_hide_real_game(self) -> None:
        # A bad IGDB version_parent match must not demote a played,
        # identifier-bearing row under an empty shell (the Titanfall 2 shape).
        empty_parent = await seed_game("Titanfall II")
        real_id = await self._real_game("Titanfall 2", 1237970, minutes=1200)

        await igdb._apply_igdb_metadata(
            real_id,
            igdb.IGDBGame(
                igdb_id=5000,
                name="Titanfall 2",
                category=0,
                first_release_date=None,
                content_type="edition",
                parent_name="Titanfall II",
                is_primary_library_item=False,
            ),
        )

        after = await _read_classification(real_id)
        self.assertEqual(after["content_type"], "base_game")
        self.assertEqual(after["is_primary_library_item"], 1)
        self.assertIsNone(after["parent_game_id"])
        # Metadata (the igdb link) still lands — only the demotion is dropped.
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_id FROM games WHERE id = ?", (real_id,)
            )
        self.assertEqual(row["igdb_id"], 5000)
        _ = empty_parent

    async def test_substance_refusal_does_not_mint_the_missing_parent(self) -> None:
        # The "A Hat in Time" shape: a fuzzy match landed on another game's
        # DLC, whose parent isn't in the library. The substance guard refuses
        # the demotion (the child has identifier+playtime; a freshly minted
        # parent would have neither) — and with the write refused, the parent
        # must not be minted either, or an orphan phantom row is left behind.
        real_id = await self._real_game("A Hat in Time", 253230, minutes=1198)

        await igdb._apply_igdb_metadata(
            real_id,
            igdb.IGDBGame(
                igdb_id=7000,
                name="A Hat in Time",
                category=1,
                first_release_date=None,
                content_type="dlc",
                parent_name="Among Us 3D: VR",
                is_primary_library_item=False,
            ),
        )

        async with db_module.get_db() as db:
            minted = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ?", ("Among Us 3D: VR",)
            )
        self.assertIsNone(minted)

        after = await _read_classification(real_id)
        self.assertEqual(after["content_type"], "base_game")
        self.assertEqual(after["is_primary_library_item"], 1)
        self.assertIsNone(after["parent_game_id"])

    async def test_applied_nesting_still_mints_the_missing_parent(self) -> None:
        # The legitimate mint: an owned, insubstantial DLC row whose base game
        # isn't in the library needs a parent row to nest under. Deferring the
        # mint until the guards pass must not lose this behavior.
        child_id = await seed_game("Cool Game: Season Pass")

        await igdb._apply_igdb_metadata(
            child_id,
            igdb.IGDBGame(
                igdb_id=7001,
                name="Cool Game: Season Pass",
                category=1,
                first_release_date=None,
                content_type="dlc",
                parent_name="Cool Game",
                is_primary_library_item=False,
            ),
        )

        async with db_module.get_db() as db:
            minted = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ?", ("Cool Game",)
            )
        self.assertIsNotNone(minted)

        after = await _read_classification(child_id)
        self.assertEqual(after["content_type"], "dlc")
        self.assertEqual(after["parent_game_id"], minted["id"])
        self.assertEqual(after["is_primary_library_item"], 0)

    async def test_nesting_under_substantial_parent_still_applies(self) -> None:
        parent_id = await self._real_game("Fallout: New Vegas", 22380, minutes=2694)
        child_id = await self._real_game(
            "Fallout New Vegas Ultimate Edition", 22381, minutes=10
        )

        await igdb._apply_igdb_metadata(
            child_id,
            igdb.IGDBGame(
                igdb_id=6000,
                name="Fallout New Vegas Ultimate Edition",
                category=0,
                first_release_date=None,
                content_type="edition",
                parent_name="Fallout: New Vegas",
                is_primary_library_item=False,
            ),
        )

        after = await _read_classification(child_id)
        self.assertEqual(after["content_type"], "edition")
        self.assertEqual(after["parent_game_id"], parent_id)
        self.assertEqual(after["is_primary_library_item"], 0)


class ApplyIgdbMetadataEditionOwnershipGuardTests(ToolDBTestCase):
    """An edition of a game the user OWNS is the ownership record itself.

    The substance guard is playtime-keyed, so owned-but-UNPLAYED Steam edition
    SKUs ("Burnout Paradise: The Ultimate Box", "Crysis 2 Maximum Edition")
    slipped through it and got demoted under freshly minted, unowned parents —
    hiding 17 owned games from every rollup and surfacing the shells as false
    orphans. The edition-ownership guard closes that gap.
    """

    async def _owned_unplayed(self, name: str, appid: int) -> int:
        game_id = await seed_game(name)
        gpid = await db_module.upsert_game_platform(
            game_id, "steam", playtime_minutes=0, owned=1
        )
        await db_module.upsert_game_platform_identifier(gpid, "steam_appid", appid)
        return game_id

    async def test_owned_unplayed_edition_not_demoted_under_minted_parent(self) -> None:
        real_id = await self._owned_unplayed("Burnout Paradise: The Ultimate Box", 24740)

        await igdb._apply_igdb_metadata(
            real_id,
            igdb.IGDBGame(
                igdb_id=8000,
                name="Burnout Paradise: The Ultimate Box",
                category=0,
                first_release_date=None,
                content_type="edition",
                parent_name="Burnout Paradise",
                is_primary_library_item=False,
            ),
        )

        after = await _read_classification(real_id)
        self.assertEqual(after["content_type"], "base_game")
        self.assertEqual(after["is_primary_library_item"], 1)
        self.assertIsNone(after["parent_game_id"])
        # The refused write must not mint the phantom parent either.
        async with db_module.get_db() as db:
            minted = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ?", ("Burnout Paradise",)
            )
        self.assertIsNone(minted)

    async def test_owned_unplayed_edition_not_demoted_under_unowned_parent(self) -> None:
        shell_id = await seed_game("Pathfinder: Wrath of the Righteous")
        real_id = await self._owned_unplayed(
            "Pathfinder: WotR - Enhanced Edition", 1184370
        )

        await igdb._apply_igdb_metadata(
            real_id,
            igdb.IGDBGame(
                igdb_id=8001,
                name="Pathfinder: WotR - Enhanced Edition",
                category=0,
                first_release_date=None,
                content_type="edition",
                parent_name="Pathfinder: Wrath of the Righteous",
                is_primary_library_item=False,
            ),
        )

        after = await _read_classification(real_id)
        self.assertEqual(after["content_type"], "base_game")
        self.assertEqual(after["is_primary_library_item"], 1)
        self.assertIsNone(after["parent_game_id"])
        _ = shell_id

    async def test_owned_edition_still_nests_under_owned_parent(self) -> None:
        parent_id = await self._owned_unplayed("Fallout: New Vegas", 22380)
        child_id = await self._owned_unplayed("Fallout New Vegas Ultimate Edition", 22490)

        await igdb._apply_igdb_metadata(
            child_id,
            igdb.IGDBGame(
                igdb_id=8002,
                name="Fallout New Vegas Ultimate Edition",
                category=0,
                first_release_date=None,
                content_type="edition",
                parent_name="Fallout: New Vegas",
                is_primary_library_item=False,
            ),
        )

        after = await _read_classification(child_id)
        self.assertEqual(after["content_type"], "edition")
        self.assertEqual(after["parent_game_id"], parent_id)
        self.assertEqual(after["is_primary_library_item"], 0)

    async def test_unowned_edition_row_still_nests(self) -> None:
        # Guard is ownership-scoped: an unowned edition row (e.g. minted from a
        # wishlist or catalog source) may still nest under an unowned parent.
        parent_id = await seed_game("Dying Light 2 Stay Human")
        child_id = await seed_game("Dying Light 2: Reloaded Edition")

        await igdb._apply_igdb_metadata(
            child_id,
            igdb.IGDBGame(
                igdb_id=8003,
                name="Dying Light 2: Reloaded Edition",
                category=0,
                first_release_date=None,
                content_type="edition",
                parent_name="Dying Light 2 Stay Human",
                is_primary_library_item=False,
            ),
        )

        after = await _read_classification(child_id)
        self.assertEqual(after["content_type"], "edition")
        self.assertEqual(after["parent_game_id"], parent_id)
        self.assertEqual(after["is_primary_library_item"], 0)


class ApplyIgdbMetadataPrimaryParentTests(ToolDBTestCase):
    """A primary verdict must neither link nor mint a parent (and clears one).

    IGDB records for remakes/standalone expansions carry a parent_game of the
    original title. Writing that parent onto a primary row violated the
    "a primary library item must not keep a parent" invariant and minted an
    unowned phantom — "Sid Meier's Colonization" (1994) minted above the
    primary "Sid Meier's Civilization IV: Colonization" (2008).
    """

    async def test_primary_verdict_does_not_mint_or_link_igdb_parent(self) -> None:
        game_id = await seed_game("Sid Meier's Civilization IV: Colonization")

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=9000,
                name="Sid Meier's Civilization IV: Colonization",
                category=4,
                first_release_date=None,
                content_type="standalone_expansion",
                parent_name="Sid Meier's Colonization",
                is_primary_library_item=True,
            ),
        )

        after = await _read_classification(game_id)
        self.assertEqual(after["content_type"], "standalone_expansion")
        self.assertEqual(after["is_primary_library_item"], 1)
        self.assertIsNone(after["parent_game_id"])
        async with db_module.get_db() as db:
            minted = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ?", ("Sid Meier's Colonization",)
            )
        self.assertIsNone(minted)

    async def test_primary_verdict_clears_wrong_stored_parent(self) -> None:
        # Retroactive heal: a re-fetch with a primary verdict detaches the
        # wrong parent an earlier pass wrote, instead of leaving the row
        # chained to an unrelated game.
        wrong_parent = await seed_game("Sid Meier's Colonization")
        game_id = await seed_game(
            "Sid Meier's Civilization IV: Colonization",
            content_type="standalone_expansion",
            parent_game_id=wrong_parent,
            is_primary_library_item=1,
        )

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=9001,
                name="Sid Meier's Civilization IV: Colonization",
                category=4,
                first_release_date=None,
                content_type="standalone_expansion",
                parent_name="Sid Meier's Colonization",
                is_primary_library_item=True,
            ),
        )

        after = await _read_classification(game_id)
        self.assertEqual(after["content_type"], "standalone_expansion")
        self.assertIsNone(after["parent_game_id"])
        self.assertEqual(after["is_primary_library_item"], 1)

    async def test_pinned_parent_survives_primary_verdict(self) -> None:
        # A hand-pinned parent link is never cleared by an enrichment pass.
        parent_id = await seed_game("Some Base Game")
        game_id = await seed_game(
            "Some Remaster",
            content_type="remaster",
            parent_game_id=parent_id,
            is_primary_library_item=1,
        )
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET manual_overrides = ? WHERE id = ?",
                (json.dumps(["parent_game_id"]), game_id),
            )
            await db.commit()

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=9002,
                name="Some Remaster",
                category=9,
                first_release_date=None,
                content_type="remaster",
                is_primary_library_item=True,
            ),
        )

        after = await _read_classification(game_id)
        self.assertEqual(after["parent_game_id"], parent_id)


class UpsertGameSelfParentTests(ToolDBTestCase):
    async def test_upsert_game_drops_self_referencing_parent(self) -> None:
        game_id = await seed_game("Some Edition")

        returned_id = await db_module.upsert_game(
            appid=None,
            name="Some Edition",
            content_type="edition",
            parent_game_id=game_id,
            is_primary_library_item=0,
        )

        self.assertEqual(returned_id, game_id)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT parent_game_id, is_primary_library_item, content_type "
                "FROM games WHERE id = ?",
                (game_id,),
            )
        self.assertIsNone(row["parent_game_id"])
        self.assertEqual(row["is_primary_library_item"], 1)
        # Forcing primary forces the nested content_type back to base_game so
        # the pair stays consistent (is_primary is derived from content_type).
        self.assertEqual(row["content_type"], "base_game")


class ApplyIgdbMetadataTagUnionTests(ToolDBTestCase):
    async def test_igdb_tags_union_into_existing_steam_tags(self) -> None:
        # Existing (SteamSpy) tags must be kept, IGDB tags appended canonicalized,
        # with case-insensitive dedup — not replaced or skipped.
        game_id = await seed_game("Sekiro", tags=["souls-like", "difficult"])

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=2000,
                name="Sekiro",
                category=0,
                first_release_date=None,
                tags=["Parrying", "Difficult", "Action-Adventure"],
            ),
        )

        tags = await _read_tags(game_id)
        # existing first, then new IGDB tags (canonicalized, "Difficult" deduped)
        self.assertEqual(tags, ["souls-like", "difficult", "parrying", "action-adventure"])

    async def test_igdb_tags_seed_when_empty(self) -> None:
        game_id = await seed_game("Hollow Knight")  # tags NULL

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=2001,
                name="Hollow Knight",
                category=0,
                first_release_date=None,
                tags=["Metroidvania", "Atmospheric"],
            ),
        )

        self.assertEqual(await _read_tags(game_id), ["metroidvania", "atmospheric"])

    async def test_igdb_tag_union_filters_feature_flags_and_caps(self) -> None:
        existing = [f"tag{i}" for i in range(igdb.MERGED_TAG_CAP - 1)]
        game_id = await seed_game("Big", tags=existing)

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=2002,
                name="Big",
                category=0,
                first_release_date=None,
                tags=["Steam Trading Cards", "metroidvania", "atmospheric", "exploration"],
            ),
        )

        tags = await _read_tags(game_id)
        self.assertEqual(len(tags), igdb.MERGED_TAG_CAP)
        self.assertNotIn("steam trading cards", tags)
        self.assertEqual(tags[: len(existing)], existing)  # existing kept, in order
        self.assertEqual(tags[-1], "metroidvania")  # first IGDB tag fills last slot


class ApplyIgdbMetadataPlatformsTests(ToolDBTestCase):
    async def test_writes_igdb_platforms_json(self) -> None:
        game_id = await seed_game("Crossplay Game")
        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=901,
                name="Crossplay Game",
                category=0,
                first_release_date=None,
                platforms=[6, 130, 508],
            ),
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_platforms FROM games WHERE id = ?", (game_id,)
            )
        self.assertEqual(json.loads(row["igdb_platforms"]), [6, 130, 508])

    async def test_empty_platforms_leaves_column_null(self) -> None:
        game_id = await seed_game("No Platform Data")
        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=902,
                name="No Platform Data",
                category=0,
                first_release_date=None,
                platforms=[],
            ),
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_platforms FROM games WHERE id = ?", (game_id,)
            )
        self.assertIsNone(row["igdb_platforms"])
