"""Tests for split_game and detect_cross_platform_collapses (cleanup tools)."""

import os
from unittest.mock import AsyncMock, patch

from fastmcp.exceptions import ToolError

from conftest import (
    ToolDBTestCase,
    add_identifier,
    add_platform,
    make_steam_game,
    seed_game,
)
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import admin


async def _appids_by_game(platform: str = "steam") -> dict[int, list[str]]:
    async with db_module.get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT gp.game_id, gpi.identifier_value
               FROM game_platform_identifiers gpi
               JOIN game_platforms gp ON gp.id = gpi.game_platform_id
               WHERE gp.platform = ?""",
            (platform,),
        )
    out: dict[int, list[str]] = {}
    for row in rows:
        out.setdefault(row["game_id"], []).append(row["identifier_value"])
    return {gid: sorted(v) for gid, v in out.items()}


async def _platforms_of(game_id: int) -> set[str]:
    async with db_module.get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT platform FROM game_platforms WHERE game_id = ?", (game_id,)
        )
    return {r["platform"] for r in rows}


async def _set_igdb_id(game_id: int, igdb_id: int) -> None:
    async with db_module.get_db() as db:
        await db.execute("UPDATE games SET igdb_id = ? WHERE id = ?", (igdb_id, game_id))
        await db.commit()


class SplitGameWholePlatformTests(ToolDBTestCase):
    async def _dead_space_collapse(self) -> int:
        # One row carrying Steam 2008 + PS5 2023 (a cross-platform collapse).
        game_id = await make_steam_game("Dead Space", 17470, playtime_minutes=273)
        ps5_pid = await add_platform(game_id, "ps5", playtime_minutes=43)
        await add_identifier(ps5_pid, "psn_title_id", "PPSA03845_00")
        return game_id

    async def test_peels_whole_platform_into_new_game(self):
        game_id = await self._dead_space_collapse()

        result = await admin.split_game(
            game_id, "ps5", ["PPSA03845_00"], new_name="Dead Space (2023)"
        )

        self.assertTrue(result["moved_whole_platform"])
        new_id = result["new_game_id"]
        self.assertNotEqual(new_id, game_id)
        # Source keeps Steam; the PS5 row (with its identifier + playtime) moved.
        self.assertEqual(await _platforms_of(game_id), {"steam"})
        self.assertEqual(await _platforms_of(new_id), {"ps5"})
        self.assertEqual(await _appids_by_game("ps5"), {new_id: ["PPSA03845_00"]})
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT name, playtime_minutes FROM games g "
                "JOIN game_platforms gp ON gp.game_id = g.id "
                "WHERE g.id = ? AND gp.platform = 'ps5'",
                (new_id,),
            )
        self.assertEqual(row["name"], "Dead Space (2023)")
        self.assertEqual(row["playtime_minutes"], 43)

    async def test_dry_run_writes_nothing(self):
        game_id = await self._dead_space_collapse()
        before = await _platforms_of(game_id)

        result = await admin.split_game(
            game_id, "ps5", ["PPSA03845_00"], dry_run=True
        )

        self.assertTrue(result["dry_run"])
        self.assertIsNone(result["new_game_id"])
        self.assertEqual(await _platforms_of(game_id), before)

    async def test_unknown_platform_raises(self):
        game_id = await self._dead_space_collapse()
        with self.assertRaises(ToolError):
            await admin.split_game(game_id, "gog", ["x"])

    async def test_identifier_not_owned_raises(self):
        game_id = await self._dead_space_collapse()
        with self.assertRaises(ToolError):
            await admin.split_game(game_id, "ps5", ["NOT_OWNED"])


class SplitGameSubsetTests(ToolDBTestCase):
    async def _two_appid_collapse(self) -> int:
        # One Steam row holding two appids (a within-platform collapse).
        game_id = await seed_game("Dead Space")
        gpid = await add_platform(game_id, "steam", playtime_minutes=100)
        await add_identifier(gpid, db_module.STEAM_APP_ID, 17470)
        await add_identifier(gpid, db_module.STEAM_APP_ID, 1693980, is_primary=False)
        return game_id

    async def test_peels_one_appid_to_new_steam_row(self):
        game_id = await self._two_appid_collapse()

        result = await admin.split_game(
            game_id, "steam", ["1693980"], new_name="Dead Space (2023)"
        )

        self.assertFalse(result["moved_whole_platform"])
        new_id = result["new_game_id"]
        self.assertEqual(result["identifiers_remaining_on_source"], ["17470"])
        by_game = await _appids_by_game("steam")
        self.assertEqual(by_game[game_id], ["17470"])
        self.assertEqual(by_game[new_id], ["1693980"])
        # Both games own a steam row now (a fresh one was created for the new game).
        self.assertEqual(await _platforms_of(new_id), {"steam"})


class DetectCrossPlatformCollapsesTests(ToolDBTestCase):
    async def _multi_platform_steam_game(self, name, appid, igdb_id) -> int:
        game_id = await make_steam_game(name, appid)
        await add_platform(game_id, "ps5")  # make it multi-platform
        await _set_igdb_id(game_id, igdb_id)
        return game_id

    async def test_flags_appid_whose_true_igdb_differs(self):
        # Row claims igdb 999 (the 2023 remake) but its Steam appid is the 2008 game.
        game_id = await self._multi_platform_steam_game("Dead Space", 17470, 999)

        with (
            patch.dict(os.environ, {"TWITCH_CLIENT_ID": "x"}),
            patch(
                "gamelib_mcp.data.igdb.resolve_steam_appids_to_igdb",
                AsyncMock(return_value={"17470": 222}),
            ),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_names",
                AsyncMock(return_value={999: "Dead Space (2023)", 222: "Dead Space"}),
            ),
        ):
            result = await admin.detect_cross_platform_collapses()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["collapsed_count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["game_id"], game_id)
        self.assertEqual(candidate["steam_appid"], "17470")
        self.assertEqual(candidate["row_igdb_id"], 999)
        self.assertEqual(candidate["steam_true_igdb_id"], 222)
        self.assertEqual(candidate["steam_true_igdb_name"], "Dead Space")

    async def test_matching_igdb_is_not_flagged(self):
        await self._multi_platform_steam_game("Resident Evil 2", 883710, 555)

        with (
            patch.dict(os.environ, {"TWITCH_CLIENT_ID": "x"}),
            patch(
                "gamelib_mcp.data.igdb.resolve_steam_appids_to_igdb",
                AsyncMock(return_value={"883710": 555}),
            ),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_names",
                AsyncMock(return_value={}),
            ),
        ):
            result = await admin.detect_cross_platform_collapses()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["collapsed_count"], 0)

    async def test_unconfigured_igdb_returns_empty(self):
        await self._multi_platform_steam_game("Dead Space", 17470, 999)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TWITCH_CLIENT_ID", None)
            result = await admin.detect_cross_platform_collapses()
        self.assertFalse(result["igdb_configured"])
        self.assertEqual(result["checked"], 0)
