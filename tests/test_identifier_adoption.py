"""Identifier adoption: syncs attach store ids to identifier-less same-name rows.

Prod audit (2026-07): when a platform sync item's store identifier matched no
row, the fuzzy fallback's anti-collapse guard refused to attach onto the
existing same-name row (which simply predated identifier tracking) and created
a stranded duplicate — 6 PS5 pairs and 4 GOG pairs. These tests cover the
adopt_platform_identifier helper, its guards, the PSN sync integration, and
the GOG normalized-name pre-match.
"""

import os
from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, add_identifier, add_platform, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import psn


async def _insert_duplicate_game(name: str) -> int:
    """Raw insert of a same-name games row (seed_game would name-match it)."""
    from gamelib_mcp.data.title_normalization import normalize_search_text

    async with db_module.get_db() as db:
        cursor = await db.execute(
            "INSERT INTO games (name, name_normalized) VALUES (?, ?)",
            (name, normalize_search_text(name)),
        )
        await db.commit()
        return cursor.lastrowid


async def _identifiers_for_game(game_id: int, platform: str) -> list[tuple[str, str]]:
    async with db_module.get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT gpi.identifier_type, gpi.identifier_value
               FROM game_platform_identifiers gpi
               JOIN game_platforms gp ON gp.id = gpi.game_platform_id
               WHERE gp.game_id = ? AND gp.platform = ?""",
            (game_id, platform),
        )
    return [(row["identifier_type"], row["identifier_value"]) for row in rows]


class AdoptPlatformIdentifierTests(ToolDBTestCase):
    async def test_adopts_identifier_onto_same_name_identifierless_row(self) -> None:
        game_id = await seed_game("Tiny Tina's Wonderlands")
        await add_platform(game_id, "ps5", playtime_minutes=671)

        adopted = await db_module.adopt_platform_identifier(
            name="Tiny Tina's Wonderlands",
            platform="ps5",
            identifier_type="psn_title_id",
            identifier_value="PPSA01492_00",
        )

        self.assertEqual(adopted, game_id)
        self.assertEqual(
            await _identifiers_for_game(game_id, "ps5"),
            [("psn_title_id", "PPSA01492_00")],
        )

    async def test_refuses_when_row_already_has_identifier_of_that_type(self) -> None:
        # Two identifier-bearing rows are distinct store entries and must stay
        # separate (anti-collapse invariant).
        game_id = await seed_game("Dead Space")
        gpid = await add_platform(game_id, "ps5")
        await add_identifier(gpid, "psn_title_id", "PPSA_OTHER_00")

        adopted = await db_module.adopt_platform_identifier(
            name="Dead Space",
            platform="ps5",
            identifier_type="psn_title_id",
            identifier_value="PPSA01492_00",
        )

        self.assertIsNone(adopted)
        self.assertEqual(
            await _identifiers_for_game(game_id, "ps5"),
            [("psn_title_id", "PPSA_OTHER_00")],
        )

    async def test_adopts_when_only_other_identifier_types_present(self) -> None:
        # A gog_product_id on the row does not block adopting a psn_title_id —
        # only an identifier of the SAME type marks the row as a distinct
        # store entry.
        game_id = await seed_game("Cross Store Game")
        gpid = await add_platform(game_id, "ps5")
        await add_identifier(gpid, "some_other_id", "abc")

        adopted = await db_module.adopt_platform_identifier(
            name="Cross Store Game",
            platform="ps5",
            identifier_type="psn_title_id",
            identifier_value="PPSA00001_00",
        )

        self.assertEqual(adopted, game_id)

    async def test_refuses_on_release_year_conflict(self) -> None:
        game_id = await seed_game("Agony", release_date="2017-02-08")
        await add_platform(game_id, "ps5")

        adopted = await db_module.adopt_platform_identifier(
            name="Agony",
            platform="ps5",
            identifier_type="psn_title_id",
            identifier_value="PPSA09999_00",
            reference_release_date="2000-02-18",
        )

        self.assertIsNone(adopted)
        self.assertEqual(await _identifiers_for_game(game_id, "ps5"), [])

    async def test_adopts_when_release_years_agree(self) -> None:
        game_id = await seed_game("Agony", release_date="2017-02-08")
        await add_platform(game_id, "ps5")

        adopted = await db_module.adopt_platform_identifier(
            name="Agony",
            platform="ps5",
            identifier_type="psn_title_id",
            identifier_value="PPSA09999_00",
            reference_release_date="2017-11-01",
        )

        self.assertEqual(adopted, game_id)

    async def test_refuses_when_ambiguous(self) -> None:
        first = await seed_game("Twin Game")
        await add_platform(first, "ps5")
        second = await _insert_duplicate_game("Twin Game")
        await add_platform(second, "ps5")

        adopted = await db_module.adopt_platform_identifier(
            name="Twin Game",
            platform="ps5",
            identifier_type="psn_title_id",
            identifier_value="PPSA00002_00",
        )

        self.assertIsNone(adopted)

    async def test_refuses_when_no_same_platform_row_exists(self) -> None:
        game_id = await seed_game("Steam Only Game")
        await add_platform(game_id, "steam")

        adopted = await db_module.adopt_platform_identifier(
            name="Steam Only Game",
            platform="ps5",
            identifier_type="psn_title_id",
            identifier_value="PPSA00003_00",
        )

        self.assertIsNone(adopted)


class PsnSyncAdoptionTests(ToolDBTestCase):
    async def test_sync_adopts_identifier_instead_of_forking_duplicate(self) -> None:
        # The prod "Tiny Tina's Wonderlands" shape: a ps5 row ingested before
        # psn_title_id tracking (no identifier), then a sync carrying the
        # title id. It must attach to the existing row, not create a twin.
        game_id = await seed_game("Tiny Tina's Wonderlands")
        await add_platform(game_id, "ps5", playtime_minutes=671)

        entries = [
            {
                "name": "Tiny Tina's Wonderlands",
                "title_id": "PPSA01492_00",
                "playtime_minutes": 700,
                "last_played": "2026-07-01",
            }
        ]

        with (
            patch.dict(os.environ, {"PSN_NPSSO": "test-npsso"}, clear=False),
            patch("gamelib_mcp.data.psn.fetch_psn_library", AsyncMock(return_value=entries)),
            patch(
                "gamelib_mcp.data.psn.resolve_and_link_game",
                AsyncMock(side_effect=AssertionError("adoption must bypass name resolution")),
            ),
        ):
            result = await psn.sync_psn()

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["added"], 0)

        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT id FROM games WHERE name = 'Tiny Tina''s Wonderlands'"
            )
            playtime = await db.execute_fetchone(
                "SELECT playtime_minutes FROM game_platforms WHERE game_id = ? AND platform = 'ps5'",
                (game_id,),
            )
        self.assertEqual([row["id"] for row in rows], [game_id])
        self.assertEqual(playtime["playtime_minutes"], 700)
        self.assertEqual(
            await _identifiers_for_game(game_id, "ps5"),
            [("psn_title_id", "PPSA01492_00")],
        )


class GogNormalizedNamePrematchTests(ToolDBTestCase):
    async def test_helper_matches_same_normalized_name_on_platform(self) -> None:
        # The prod "Sigma Theory" shape: stored name has punctuation, the
        # incoming slug-derived title does not — normalized forms are equal.
        game_id = await seed_game("Sigma Theory: Global Cold War")
        await add_platform(game_id, "gog")

        row = await db_module.get_platform_game_by_normalized_name(
            "Sigma Theory Global Cold War", "gog"
        )

        self.assertIsNotNone(row)
        self.assertEqual(row["id"], game_id)

    async def test_helper_ignores_rows_on_other_platforms(self) -> None:
        game_id = await seed_game("Agony")
        await add_platform(game_id, "steam")

        row = await db_module.get_platform_game_by_normalized_name("Agony", "gog")

        self.assertIsNone(row)
