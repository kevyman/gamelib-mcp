import sys
import types
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    import aiosqlite  # type: ignore
except ModuleNotFoundError:
    aiosqlite = types.ModuleType("aiosqlite")

    class Connection:  # minimal stub for db package import-time polyfill
        pass

    class Row(dict):
        pass

    async def connect(*_args, **_kwargs):
        raise ModuleNotFoundError("aiosqlite is not installed")

    aiosqlite.Connection = Connection
    aiosqlite.Row = Row
    aiosqlite.connect = connect
    sys.modules["aiosqlite"] = aiosqlite

try:
    import httpx  # type: ignore
except ModuleNotFoundError:
    httpx = types.ModuleType("httpx")

    class Response:
        pass

    class HTTPStatusError(Exception):
        pass

    class TimeoutException(Exception):
        pass

    class TransportError(Exception):
        pass

    class AsyncClient:
        pass

    httpx.Response = Response
    httpx.HTTPStatusError = HTTPStatusError
    httpx.TimeoutException = TimeoutException
    httpx.TransportError = TransportError
    httpx.AsyncClient = AsyncClient
    sys.modules["httpx"] = httpx

try:
    from bs4 import BeautifulSoup  # type: ignore
except ModuleNotFoundError:
    bs4 = types.ModuleType("bs4")

    class BeautifulSoup:  # pragma: no cover - import stub only
        def __init__(self, *_args, **_kwargs):
            pass

        def find(self, *_args, **_kwargs):
            return None

    bs4.BeautifulSoup = BeautifulSoup
    sys.modules["bs4"] = bs4

from gamelib_mcp.data import igdb, nintendo


class SyncNintendoTests(unittest.TestCase):
    def _run_sync(self, entries, resolve_result=(42, None), candidates=None):
        mock_resolve = AsyncMock(return_value=resolve_result)
        mock_upsert_platform = AsyncMock(return_value=99)
        mock_upsert_identifier = AsyncMock()
        mock_upsert_enrichment = AsyncMock()

        with (
            patch.dict("os.environ", {"NINTENDO_SESSION_TOKEN": ""}, clear=False),
            patch("gamelib_mcp.data.nintendo._nxapi_available", return_value=False),
            patch("gamelib_mcp.data.nintendo._load_vgcs_cookies", return_value={"session": "cookie"}),
            patch("gamelib_mcp.data.nintendo.fetch_nintendo_library_vgcs", AsyncMock(return_value=entries)),
            patch("gamelib_mcp.data.nintendo.load_fuzzy_candidates", AsyncMock(return_value=candidates or {})),
            patch("gamelib_mcp.data.nintendo.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.nintendo.upsert_game_platform", mock_upsert_platform),
            patch("gamelib_mcp.data.nintendo.upsert_game_platform_identifier", mock_upsert_identifier),
            patch("gamelib_mcp.data.nintendo.upsert_game_platform_enrichment", mock_upsert_enrichment),
        ):
            # Characterize the VGCS ownership layer directly; the Parental Controls
            # playtime layer (wired into the public sync_nintendo wrapper) is
            # covered separately in test_nintendo_pctl.
            result = asyncio.run(nintendo._sync_nintendo_ownership())

        return result, mock_resolve, mock_upsert_platform, mock_upsert_identifier, mock_upsert_enrichment

    def test_vgcs_fallback_still_syncs_when_igdb_returns_no_result(self) -> None:
        entries = [
            {
                "name": "Fire Emblem Engage",
                "playtime_minutes": None,
                "title_id": "0100a5c00d9a2000",
            }
        ]

        result, mock_resolve, mock_upsert_platform, mock_upsert_identifier, mock_enrichment = self._run_sync(entries)

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0})
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Fire Emblem Engage", igdb.PLATFORM_TO_IGDB["switch2"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="switch2",
            playtime_minutes=None,
            owned=1,
        )
        mock_upsert_identifier.assert_awaited_once_with(99, nintendo.NINTENDO_TITLE_ID, "0100a5c00d9a2000")
        mock_enrichment.assert_not_called()

    def test_skips_when_no_credentials_are_available(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("gamelib_mcp.data.nintendo._load_vgcs_cookies", return_value=None),
            patch("gamelib_mcp.data.nintendo._nxapi_available", return_value=False),
        ):
            result = asyncio.run(nintendo.sync_nintendo())

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["sync_status"], "unconfigured")
        self.assertEqual(result["error_classification"], "missing_configuration")

    def test_returns_stale_metadata_when_nxapi_auth_fails_without_fallback(self) -> None:
        with (
            patch.dict("os.environ", {"NINTENDO_SESSION_TOKEN": "token"}, clear=True),
            patch("gamelib_mcp.data.nintendo._load_vgcs_cookies", return_value=None),
            patch("gamelib_mcp.data.nintendo._nxapi_available", return_value=True),
            patch(
                "gamelib_mcp.data.nintendo.fetch_nintendo_play_history",
                AsyncMock(side_effect=RuntimeError("Nintendo auth expired")),
            ),
        ):
            result = asyncio.run(nintendo.sync_nintendo())

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["sync_status"], "stale")
        self.assertEqual(result["error_classification"], "auth_stale")
        self.assertIn("Nintendo auth expired", result["error_summary"])

    def test_load_vgcs_cookies_falls_back_to_local_default_file_when_env_path_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            local_data_dir = tmp_path / "data"
            local_data_dir.mkdir()
            fallback_file = local_data_dir / "nintendo_cookies.json"
            fallback_file.write_text(json.dumps({"session": "cookie"}), encoding="utf-8")

            with patch.dict(
                os.environ,
                {"NINTENDO_COOKIES_FILE": "/data/nintendo_cookies.json"},
                clear=False,
            ):
                original_cwd = os.getcwd()
                try:
                    os.chdir(tmp_path)
                    cookies = nintendo._load_vgcs_cookies()
                finally:
                    os.chdir(original_cwd)

        self.assertEqual(cookies, {"session": "cookie"})

    def test_sync_normalizes_titles_and_skips_non_game_rows(self) -> None:
        entries = [
            {"name": "Hollow Knight – Nintendo Switch 2 Edition", "playtime_minutes": None, "title_id": "0101"},
            {"name": "METAL GEAR SOLID: MASTER COLLECTION Vol.1 BONUS CONTENT", "playtime_minutes": None, "title_id": "0102"},
        ]

        result, mock_resolve, mock_upsert_platform, mock_upsert_identifier, _ = self._run_sync(entries)

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 1})
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Hollow Knight", igdb.PLATFORM_TO_IGDB["switch2"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="switch2",
            playtime_minutes=None,
            owned=1,
        )
        mock_upsert_identifier.assert_awaited_once_with(99, nintendo.NINTENDO_TITLE_ID, "0101")

    def test_repairs_stale_conflicting_platform_row(self) -> None:
        entries = [
            {
                "name": "PowerWash Simulator 2",
                "playtime_minutes": None,
                "title_id": "0101",
            }
        ]
        mock_repair = AsyncMock()

        with (
            patch.dict("os.environ", {"NINTENDO_SESSION_TOKEN": ""}, clear=False),
            patch("gamelib_mcp.data.nintendo._nxapi_available", return_value=False),
            patch("gamelib_mcp.data.nintendo._load_vgcs_cookies", return_value={"session": "cookie"}),
            patch("gamelib_mcp.data.nintendo.fetch_nintendo_library_vgcs", AsyncMock(return_value=entries)),
            patch("gamelib_mcp.data.nintendo.load_fuzzy_candidates", AsyncMock(return_value={7: "PowerWash Simulator"})),
            patch("gamelib_mcp.data.nintendo.find_conflicting_fuzzy_key", return_value=7),
            patch("gamelib_mcp.data.nintendo.resolve_and_link_game", AsyncMock(return_value=(42, None))),
            patch("gamelib_mcp.data.nintendo.repair_misclassified_platform_row", mock_repair),
            patch("gamelib_mcp.data.nintendo.upsert_game_platform", AsyncMock(return_value=99)),
            patch("gamelib_mcp.data.nintendo.upsert_game_platform_identifier", AsyncMock()),
        ):
            asyncio.run(nintendo.sync_nintendo())

        mock_repair.assert_awaited_once_with(
            source_game_id=7,
            target_game_id=42,
            platform="switch2",
        )

    def test_does_not_repair_when_conflicting_source_title_is_in_current_payload(self) -> None:
        entries = [
            {
                "name": "PowerWash Simulator",
                "playtime_minutes": None,
                "title_id": "0101",
            },
            {
                "name": "PowerWash Simulator 2",
                "playtime_minutes": None,
                "title_id": "0102",
            },
        ]
        mock_repair = AsyncMock()

        with (
            patch.dict("os.environ", {"NINTENDO_SESSION_TOKEN": ""}, clear=False),
            patch("gamelib_mcp.data.nintendo._nxapi_available", return_value=False),
            patch("gamelib_mcp.data.nintendo._load_vgcs_cookies", return_value={"session": "cookie"}),
            patch("gamelib_mcp.data.nintendo.fetch_nintendo_library_vgcs", AsyncMock(return_value=entries)),
            patch("gamelib_mcp.data.nintendo.load_fuzzy_candidates", AsyncMock(return_value={7: "PowerWash Simulator"})),
            patch("gamelib_mcp.data.nintendo.find_conflicting_fuzzy_key", side_effect=[None, 7]),
            patch(
                "gamelib_mcp.data.nintendo.resolve_and_link_game",
                AsyncMock(side_effect=[(7, None), (42, None)]),
            ),
            patch("gamelib_mcp.data.nintendo.repair_misclassified_platform_row", mock_repair),
            patch("gamelib_mcp.data.nintendo.upsert_game_platform", AsyncMock(return_value=99)),
            patch("gamelib_mcp.data.nintendo.upsert_game_platform_identifier", AsyncMock()),
        ):
            asyncio.run(nintendo.sync_nintendo())

        mock_repair.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
