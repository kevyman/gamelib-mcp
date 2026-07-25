"""Steam license audit tests — ownership healed from the account license list.

The audit exists because GetOwnedGames silently omits some retired/delisted
apps the account still holds licenses for (observed in prod: Burnout Paradise:
The Ultimate Box and ~75 friends stranded as platformless "orphan" rows).
Network is fully mocked: dynamicstore userdata via httpx.MockTransport, the
appdetails/SteamSpy probes via patched module bindings.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx

from conftest import ToolDBTestCase, make_steam_game, seed_game
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import steam_licenses, steam_session
from gamelib_mcp.data.db import get_meta

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
        with patch.object(
            steam_session, "load_steam_web_cookies", AsyncMock(return_value=COOKIES)
        ):
            owned = await steam_licenses.fetch_owned_steam_appids(
                transport=_userdata_transport([10, 24740, "220"])
            )
        self.assertEqual(owned, {10, 220, 24740})

    async def test_empty_owned_apps_is_auth_failure(self):
        # A logged-out userdata request "succeeds" with empty arrays; the
        # configured account always owns games, so empty == expired session.
        with patch.object(
            steam_session, "load_steam_web_cookies", AsyncMock(return_value=COOKIES)
        ):
            with self.assertRaises(RuntimeError):
                await steam_licenses.fetch_owned_steam_appids(
                    transport=_userdata_transport([])
                )


class AuditSteamLicensesTests(ToolDBTestCase):
    async def _audit(self, owned, appdetails=None, steamspy=None, **kwargs):
        appdetails = appdetails or {}
        steamspy = steamspy or {}
        with (
            patch.object(
                steam_session, "load_steam_web_cookies", AsyncMock(return_value=COOKIES)
            ),
            patch.object(steam_session, "is_steam_session_configured", return_value=True),
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
        with patch.object(steam_session, "is_steam_session_configured", return_value=False):
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

    async def test_appid_on_unowned_stub_is_healed_to_owned(self):
        # An appid stuck on an owned=0 manual/legacy stub is NOT "already in
        # the library" — the licence says it's owned, so the audit must
        # reprocess it and flip the stub owned instead of skipping it.
        stub_game = await seed_game("Shelved Stub")
        async with db_module.get_db() as db:
            cursor = await db.execute(
                """INSERT INTO game_platforms (game_id, platform, owned)
                   VALUES (?, 'steam', 0)""",
                (stub_game,),
            )
            stub_gpid = cursor.lastrowid
            await db.commit()
        await db_module.upsert_game_platform_identifier(
            stub_gpid, "steam_appid", 4321
        )

        result = await self._audit(owned={4321}, steamspy={4321: "Shelved Stub"})

        self.assertEqual(
            [entry["appid"] for entry in result["minted_delisted"]], [4321]
        )
        row = await _steam_row_by_appid(4321)
        self.assertEqual(row["game_id"], stub_game)
        self.assertEqual(row["owned"], 1)
        self.assertEqual(row["delisted"], 1)

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


class AuditSteamLicensesReportModeTests(ToolDBTestCase):
    """mint=False (check_library's ownership.license_gap report mode)."""

    async def _audit(self, owned, appdetails=None, steamspy=None, **kwargs):
        appdetails = appdetails or {}
        steamspy = steamspy or {}
        with (
            patch.object(
                steam_session, "load_steam_web_cookies", AsyncMock(return_value=COOKIES)
            ),
            patch.object(steam_session, "is_steam_session_configured", return_value=True),
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
                transport=_userdata_transport(sorted(owned)), mint=False, **kwargs
            )

    async def test_live_game_lands_in_would_mint_not_minted(self):
        result = await self._audit(
            owned={4000},
            appdetails={4000: {"type": "game", "name": "Garry's Mod"}},
        )
        self.assertEqual(result["mint"], False)
        self.assertEqual(result["minted"], [])
        self.assertEqual([e["appid"] for e in result["would_mint"]], [4000])
        self.assertIsNone(await _steam_row_by_appid(4000))
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 0)

    async def test_retired_game_lands_in_would_mint_delisted_not_minted(self):
        result = await self._audit(owned={471100}, steamspy={471100: "Crysis 2"})
        self.assertEqual(result["minted_delisted"], [])
        self.assertEqual(
            [e["appid"] for e in result["would_mint_delisted"]], [471100]
        )
        self.assertIsNone(await _steam_row_by_appid(471100))

    async def test_classification_cached_but_not_settled(self):
        # A report-mode run caches the probe result (classified_game) so later
        # report runs advance instead of re-probing, but the appid is NOT
        # settled: it keeps re-appearing in would_mint until a mint run heals it.
        await self._audit(
            owned={4000}, appdetails={4000: {"type": "game", "name": "Garry's Mod"}}
        )
        audit_map = json.loads(await get_meta(steam_licenses.AUDIT_META_KEY))
        self.assertEqual(audit_map["4000"]["outcome"], "classified_game")

        second = await self._audit(owned={4000})  # no appdetails: a re-probe would fail
        self.assertEqual(second["probed"], 0)
        self.assertEqual(second["classified_from_cache"], 1)
        self.assertEqual([e["appid"] for e in second["would_mint"]], [4000])
        self.assertIsNone(await _steam_row_by_appid(4000))

    async def test_report_scan_advances_past_skipped_batch(self):
        # The Codex-review scenario: a first batch of DLC must not block the
        # scan — report mode persists the skips, so the next report call's
        # probe budget reaches the appids behind them.
        first = await self._audit(
            owned={1000, 5000},
            appdetails={1000: {"type": "dlc", "name": "Some Season Pass"}},
            limit=1,
        )
        self.assertEqual([e["appid"] for e in first["skipped_non_game"]], [1000])
        self.assertEqual(first["remaining"], 1)
        self.assertIsNone(await _steam_row_by_appid(1000))

        second = await self._audit(
            owned={1000, 5000},
            appdetails={5000: {"type": "game", "name": "Cyberdeck"}},
            limit=1,
        )
        self.assertEqual(second["probed"], 1)
        self.assertEqual([e["appid"] for e in second["would_mint"]], [5000])
        self.assertEqual(second["remaining"], 0)

    async def test_mint_run_heals_cached_classifications_without_reprobe(self):
        # Report run classifies a live and a retired game; the next MINT run
        # heals both from the cache without spending store/SteamSpy probes.
        await self._audit(
            owned={4000, 471100},
            appdetails={4000: {"type": "game", "name": "Garry's Mod"}},
            steamspy={471100: "Crysis 2"},
        )
        with (
            patch.object(
                steam_session, "load_steam_web_cookies", AsyncMock(return_value=COOKIES)
            ),
            patch.object(steam_session, "is_steam_session_configured", return_value=True),
            patch.object(
                steam_licenses,
                "fetch_store_appdetails",
                AsyncMock(side_effect=AssertionError("cached appids must not re-probe")),
            ),
            patch.object(
                steam_licenses,
                "fetch_steamspy_name",
                AsyncMock(side_effect=AssertionError("cached appids must not re-probe")),
            ),
        ):
            result = await steam_licenses.audit_steam_licenses(
                transport=_userdata_transport([4000, 471100])
            )
        self.assertEqual([e["appid"] for e in result["minted"]], [4000])
        self.assertEqual([e["appid"] for e in result["minted_delisted"]], [471100])
        row = await _steam_row_by_appid(4000)
        self.assertIsNotNone(row)
        retired = await _steam_row_by_appid(471100)
        self.assertEqual(retired["delisted"], 1)
        audit_map = json.loads(await get_meta(steam_licenses.AUDIT_META_KEY))
        self.assertEqual(audit_map["4000"]["outcome"], "minted")
        self.assertEqual(audit_map["471100"]["outcome"], "minted_delisted")

    async def test_dlc_skip_persists_but_mints_nothing(self):
        result = await self._audit(
            owned={1234}, appdetails={1234: {"type": "dlc", "name": "Some Season Pass"}}
        )
        self.assertEqual([e["appid"] for e in result["skipped_non_game"]], [1234])
        self.assertIsNone(await _steam_row_by_appid(1234))
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 0)
        audit_map = json.loads(await get_meta(steam_licenses.AUDIT_META_KEY))
        self.assertEqual(audit_map["1234"]["outcome"], "skipped_dlc")


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
