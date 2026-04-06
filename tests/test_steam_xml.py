import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

class _DummyConnection:
    pass


sys.modules.setdefault("httpx", types.SimpleNamespace(AsyncClient=None))
sys.modules.setdefault(
    "aiosqlite",
    types.SimpleNamespace(Connection=_DummyConnection, Row=dict),
)

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


class SteamXmlTests(unittest.IsolatedAsyncioTestCase):
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
                create=True,
            ) as bulk_upsert,
            patch.object(steam_xml, "set_meta", AsyncMock()) as set_meta,
            patch.object(steam_xml, "upsert_game", AsyncMock(), create=True) as upsert_game,
            patch.object(
                steam_xml,
                "upsert_game_platform",
                AsyncMock(),
                create=True,
            ) as upsert_game_platform,
            patch.object(
                steam_xml,
                "upsert_game_platform_identifier",
                AsyncMock(),
                create=True,
            ) as upsert_game_platform_identifier,
            patch.object(
                steam_xml,
                "upsert_steam_platform_data",
                AsyncMock(),
                create=True,
            ) as upsert_steam_platform_data,
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
        upsert_game.assert_not_awaited()
        upsert_game_platform.assert_not_awaited()
        upsert_game_platform_identifier.assert_not_awaited()
        upsert_steam_platform_data.assert_not_awaited()
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
                create=True,
            ),
            patch.object(steam_xml, "set_meta", AsyncMock()) as set_meta,
            patch.object(steam_xml, "upsert_game", AsyncMock(return_value=1), create=True),
            patch.object(
                steam_xml,
                "upsert_game_platform",
                AsyncMock(return_value=2),
                create=True,
            ),
            patch.object(
                steam_xml,
                "upsert_game_platform_identifier",
                AsyncMock(),
                create=True,
            ),
            patch.object(
                steam_xml,
                "upsert_steam_platform_data",
                AsyncMock(),
                create=True,
            ),
            patch.object(steam_xml, "datetime", _FixedDatetime),
        ):
            result = await steam_xml.fetch_library()

        self.assertEqual(result["games_upserted"], 7)
        self.assertEqual(result["synced_at"], synced_at)
        set_meta.assert_awaited_once_with("library_synced_at", synced_at)


if __name__ == "__main__":
    unittest.main()
