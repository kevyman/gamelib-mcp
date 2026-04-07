import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
import os

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import steam_xml


class _DummyResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _DummyClient:
    def __init__(self, response: _DummyResponse):
        self._response = response
        self.get = AsyncMock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FixedDatetime:
    @staticmethod
    def now(_tz):
        return datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc)


class SteamXmlFetchLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_library_uses_bulk_upsert_for_normalized_rows(self) -> None:
        response = _DummyResponse(
            {
                "response": {
                    "game_count": 2,
                    "games": [
                        {
                            "appid": 10,
                            "name": "Portal",
                            "playtime_forever": 123,
                            "playtime_2weeks": 5,
                            "rtime_last_played": 111,
                        },
                        {
                            "appid": 20,
                            "playtime_forever": 0,
                        },
                    ],
                }
            }
        )
        client = _DummyClient(response)
        synced_at = "2026-04-07T12:00:00+00:00"

        with (
            patch.object(steam_xml, "STEAM_API_KEY", "key"),
            patch.object(steam_xml, "STEAM_ID", "steam-id"),
            patch.object(steam_xml.httpx, "AsyncClient", return_value=client),
            patch.object(
                steam_xml,
                "bulk_upsert_steam_library",
                AsyncMock(return_value=2),
            ) as bulk_upsert,
            patch.object(steam_xml, "set_meta", AsyncMock()) as set_meta,
            patch.object(steam_xml, "datetime", _FixedDatetime),
        ):
            result = await steam_xml.fetch_library()

        bulk_upsert.assert_awaited_once_with(
            [
                {
                    "appid": 10,
                    "name": "Portal",
                    "playtime_minutes": 123,
                    "playtime_2weeks_minutes": 5,
                    "rtime_last_played": 111,
                },
                {
                    "appid": 20,
                    "name": "App 20",
                    "playtime_minutes": 0,
                    "playtime_2weeks_minutes": 0,
                    "rtime_last_played": None,
                },
            ],
            synced_at=synced_at,
        )
        set_meta.assert_awaited_once_with("library_synced_at", synced_at)
        self.assertEqual(result, {"games_upserted": 2, "synced_at": synced_at})

    async def test_fetch_library_preserves_bulk_count_and_synced_at(self) -> None:
        response = _DummyResponse(
            {
                "response": {
                    "game_count": 1,
                    "games": [
                        {
                            "appid": 30,
                            "name": "Half-Life",
                            "playtime_forever": 50,
                        }
                    ],
                }
            }
        )
        client = _DummyClient(response)
        synced_at = "2026-04-07T12:00:00+00:00"

        with (
            patch.object(steam_xml, "STEAM_API_KEY", "key"),
            patch.object(steam_xml, "STEAM_ID", "steam-id"),
            patch.object(steam_xml.httpx, "AsyncClient", return_value=client),
            patch.object(
                steam_xml,
                "bulk_upsert_steam_library",
                AsyncMock(return_value=7),
            ),
            patch.object(steam_xml, "set_meta", AsyncMock()) as set_meta,
            patch.object(steam_xml, "datetime", _FixedDatetime),
        ):
            result = await steam_xml.fetch_library()

        self.assertEqual(result["games_upserted"], 7)
        self.assertEqual(result["synced_at"], synced_at)
        set_meta.assert_awaited_once_with("library_synced_at", synced_at)

    async def test_fetch_library_reads_credentials_from_environment_at_call_time(self) -> None:
        response = _DummyResponse({"response": {"game_count": 0, "games": []}})
        client = _DummyClient(response)

        with (
            patch.object(steam_xml, "STEAM_API_KEY", ""),
            patch.object(steam_xml, "STEAM_ID", ""),
            patch.dict(os.environ, {"STEAM_API_KEY": "runtime-key", "STEAM_ID": "runtime-id"}, clear=False),
            patch.object(steam_xml.httpx, "AsyncClient", return_value=client),
            patch.object(steam_xml, "bulk_upsert_steam_library", AsyncMock(return_value=0)),
            patch.object(steam_xml, "set_meta", AsyncMock()),
            patch.object(steam_xml, "datetime", _FixedDatetime),
        ):
            await steam_xml.fetch_library()

        params = client.get.await_args.kwargs["params"]
        self.assertEqual(params["key"], "runtime-key")
        self.assertEqual(params["steamid"], "runtime-id")


class SteamBulkUpsertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "steam-bulk.sqlite"
        self.original_ready_path = db_module._DB_READY_PATH
        self.original_init_lock = db_module._DB_INIT_LOCK
        db_module._DB_READY_PATH = None
        db_module._DB_INIT_LOCK = None
        self.env = patch.dict(os.environ, {"DATABASE_URL": f"file:{self.db_path}"}, clear=False)
        self.env.start()

    async def asyncSetUp(self) -> None:
        await db_module.init_db()

    async def asyncTearDown(self) -> None:
        db_module._DB_READY_PATH = self.original_ready_path
        db_module._DB_INIT_LOCK = self.original_init_lock
        self.env.stop()
        self.tmpdir.cleanup()

    async def test_bulk_upsert_is_idempotent_for_duplicate_appids_and_uses_stable_name_resolution(
        self,
    ) -> None:
        synced_at = "2026-04-07T12:00:00+00:00"

        upserted = await db_module.bulk_upsert_steam_library(
            [
                {
                    "appid": 10,
                    "name": "Portal",
                    "playtime_minutes": 120,
                    "playtime_2weeks_minutes": 15,
                    "rtime_last_played": 1000,
                },
                {
                    "appid": 10,
                    "name": "Portal Reloaded",
                    "playtime_minutes": 180,
                    "playtime_2weeks_minutes": 25,
                    "rtime_last_played": 2000,
                },
            ],
            synced_at=synced_at,
        )

        game = await db_module.get_game_by_appid(10)
        platform_row = await db_module.get_steam_platform_row_by_appid(10)
        async with db_module.get_db() as db:
            identifier_count_row = await db.execute_fetchone(
                """SELECT COUNT(*) AS count
                   FROM game_platform_identifiers
                   WHERE identifier_type = ? AND identifier_value = ?""",
                (db_module.STEAM_APP_ID, "10"),
            )

        self.assertEqual(upserted, 2)
        self.assertIsNotNone(game)
        self.assertEqual(game["name"], "Portal Reloaded")
        self.assertIsNotNone(platform_row)
        self.assertEqual(platform_row["playtime_minutes"], 180)
        self.assertEqual(platform_row["playtime_2weeks_minutes"], 25)
        self.assertEqual(platform_row["rtime_last_played"], 2000)
        self.assertEqual(platform_row["last_synced"], synced_at)
        self.assertEqual(platform_row["library_updated_at"], synced_at)
        self.assertEqual(identifier_count_row["count"], 1)


if __name__ == "__main__":
    unittest.main()
