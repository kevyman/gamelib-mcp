"""Steam license audit tests — ownership healed from the account license list.

The audit exists because GetOwnedGames silently omits some retired/delisted
apps the account still holds licenses for (observed in prod: Burnout Paradise:
The Ultimate Box and ~75 friends stranded as platformless "orphan" rows).
Network is fully mocked: dynamicstore userdata via httpx.MockTransport, the
appdetails/SteamSpy probes via patched module bindings.
"""

from unittest.mock import AsyncMock, patch

import httpx

from conftest import ToolDBTestCase, make_steam_game, seed_game
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import steam_licenses

COOKIES = {"steamLoginSecure": "x", "sessionid": "y"}


def _userdata_transport(owned_apps):
    def handler(request):
        assert request.url.path == "/dynamicstore/userdata/"
        return httpx.Response(
            200, json={"rgOwnedApps": owned_apps, "rgOwnedPackages": []}
        )

    return httpx.MockTransport(handler)


async def _steam_row_by_appid(appid: int) -> dict | None:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone(
            """SELECT g.id AS game_id, g.name, gp.owned, gp.delisted,
                      gp.playtime_minutes
               FROM game_platform_identifiers gpi
               JOIN game_platforms gp ON gp.id = gpi.game_platform_id
               JOIN games g ON g.id = gp.game_id
               WHERE gpi.identifier_type = 'steam_appid'
                 AND gpi.identifier_value = ?""",
            (str(appid),),
        )
    return dict(row) if row is not None else None


class FetchOwnedAppidsTests(ToolDBTestCase):
    async def test_returns_appid_set(self):
        with patch.object(steam_licenses, "_load_steam_cookies", return_value=COOKIES):
            owned = await steam_licenses.fetch_owned_steam_appids(
                transport=_userdata_transport([10, 24740, "220"])
            )
        self.assertEqual(owned, {10, 220, 24740})

    async def test_empty_owned_apps_is_auth_failure(self):
        # A logged-out userdata request "succeeds" with empty arrays; the
        # configured account always owns games, so empty == expired session.
        with patch.object(steam_licenses, "_load_steam_cookies", return_value=COOKIES):
            with self.assertRaises(RuntimeError):
                await steam_licenses.fetch_owned_steam_appids(
                    transport=_userdata_transport([])
                )


class AuditSteamLicensesTests(ToolDBTestCase):
    async def _audit(self, owned, appdetails=None, steamspy=None, **kwargs):
        appdetails = appdetails or {}
        steamspy = steamspy or {}
        with (
            patch.object(steam_licenses, "_load_steam_cookies", return_value=COOKIES),
            patch.object(
                steam_licenses,
                "fetch_store_appdetails",
                AsyncMock(side_effect=lambda appid: appdetails.get(appid)),
            ),
            patch.object(
                steam_licenses,
                "fetch_steamspy_name",
                AsyncMock(side_effect=lambda appid: steamspy.get(appid)),
            ),
        ):
            return await steam_licenses.audit_steam_licenses(
                transport=_userdata_transport(sorted(owned)), **kwargs
            )

    async def test_unconfigured_without_cookies(self):
        with patch.object(steam_licenses, "_load_steam_cookies", return_value=None):
            result = await steam_licenses.audit_steam_licenses()
        self.assertEqual(result["status"], "unconfigured")

    async def test_retired_game_adopts_orphan_row_and_flags_delisted(self):
        # The prod shape: a platformless games row survives while the retired
        # app never syncs. The audit must attach ownership onto THAT row (via
        # bulk_upsert's same-name adoption), not fork a duplicate.
        orphan_id = await seed_game("Crysis 2")
        result = await self._audit(owned={471100}, steamspy={471100: "Crysis 2"})

        self.assertEqual(
            [entry["appid"] for entry in result["minted_delisted"]], [471100]
        )
        row = await _steam_row_by_appid(471100)
        self.assertIsNotNone(row)
        self.assertEqual(row["game_id"], orphan_id)
        self.assertEqual(row["owned"], 1)
        self.assertEqual(row["delisted"], 1)
        self.assertIsNone(row["playtime_minutes"])

    async def test_live_game_missing_from_owned_api_is_minted(self):
        result = await self._audit(
            owned={4000},
            appdetails={4000: {"type": "game", "name": "Garry's Mod"}},
        )
        self.assertEqual([entry["appid"] for entry in result["minted"]], [4000])
        row = await _steam_row_by_appid(4000)
        self.assertEqual(row["name"], "Garry's Mod")
        # Still flagged: the flag means "absent from GetOwnedGames", and the
        # primary sync clears it if the API ever returns the app.
        self.assertEqual(row["delisted"], 1)

    async def test_dlc_never_mints_a_games_row(self):
        result = await self._audit(
            owned={1234},
            appdetails={1234: {"type": "dlc", "name": "Some Season Pass"}},
        )
        self.assertEqual(
            [entry["appid"] for entry in result["skipped_non_game"]], [1234]
        )
        self.assertIsNone(await _steam_row_by_appid(1234))
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 0)

    async def test_already_synced_appids_are_not_probed(self):
        await make_steam_game("Portal 2", 620, playtime_minutes=100)
        result = await self._audit(owned={620})
        self.assertEqual(result["probed"], 0)
        self.assertEqual(result["unclassified"], 0)

    async def test_unresolved_is_remembered_and_retriable(self):
        first = await self._audit(owned={999})
        self.assertEqual(first["unresolved"], [999])

        second = await self._audit(owned={999})
        self.assertEqual(second["probed"], 0)

        third = await self._audit(
            owned={999}, steamspy={999: "Recovered Name"}, retry_unresolved=True
        )
        self.assertEqual(
            [entry["appid"] for entry in third["minted_delisted"]], [999]
        )

    async def test_limit_caps_probes_and_reports_remaining(self):
        result = await self._audit(
            owned={101, 102, 103},
            steamspy={101: "A", 102: "B", 103: "C"},
            limit=2,
        )
        self.assertEqual(result["probed"], 2)
        self.assertEqual(result["remaining"], 1)
        follow_up = await self._audit(
            owned={101, 102, 103}, steamspy={101: "A", 102: "B", 103: "C"}
        )
        self.assertEqual(follow_up["probed"], 1)
        self.assertEqual(follow_up["remaining"], 0)


class SetSteamDelistedTests(ToolDBTestCase):
    async def test_flag_set_and_cleared_by_appid(self):
        await make_steam_game("Relisted", 777)

        self.assertEqual(await db_module.set_steam_delisted([777], True), 1)
        row = await _steam_row_by_appid(777)
        self.assertEqual(row["delisted"], 1)

        # The primary sync path: an app returned by GetOwnedGames again gets
        # its flag cleared; already-clear rows are not counted.
        self.assertEqual(await db_module.set_steam_delisted([777], False), 1)
        self.assertEqual(await db_module.set_steam_delisted([777], False), 0)
        row = await _steam_row_by_appid(777)
        self.assertEqual(row["delisted"], 0)
