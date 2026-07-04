import sys
import types
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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

    class Request:
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
    httpx.Request = Request
    httpx.HTTPStatusError = HTTPStatusError
    httpx.TimeoutException = TimeoutException
    httpx.TransportError = TransportError
    httpx.AsyncClient = AsyncClient
    sys.modules["httpx"] = httpx

from gamelib_mcp.data import xbox, igdb
from gamelib_mcp.data.xbox import _extract_title


_SAMPLE_TITLE_HISTORY = {
    "titles": [
        {
            "titleId": "1030027286",
            "name": "Halo Infinite",
            "titleHistory": {"lastTimePlayed": "2026-06-01T10:00:00Z"},
        },
        {"titleId": None, "name": "Broken Entry"},
        {"name": "No Id Entry"},
    ]
}


class ExtractTitleTests(unittest.TestCase):
    def test_extract_title_reads_id_and_name(self):
        title_id, name = _extract_title(_SAMPLE_TITLE_HISTORY["titles"][0])
        self.assertEqual((title_id, name), ("1030027286", "Halo Infinite"))

    def test_extract_title_missing_id_returns_none_id(self):
        title_id, name = _extract_title(_SAMPLE_TITLE_HISTORY["titles"][2])
        self.assertIsNone(title_id)
        self.assertEqual(name, "No Id Entry")

    def test_extract_title_null_id_returns_none_id(self):
        title_id, name = _extract_title(_SAMPLE_TITLE_HISTORY["titles"][1])
        self.assertIsNone(title_id)
        self.assertEqual(name, "Broken Entry")

    def test_extract_title_non_dict_returns_none_none(self):
        title_id, name = _extract_title("not-a-dict")
        self.assertIsNone(title_id)
        self.assertIsNone(name)


class IsXboxConfiguredTests(unittest.TestCase):
    def test_unconfigured_without_api_key(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("OPENXBL_API_KEY", None)
            self.assertFalse(xbox.is_xbox_configured())

    def test_configured_with_api_key(self):
        with patch.dict("os.environ", {"OPENXBL_API_KEY": "test-key"}, clear=False):
            self.assertTrue(xbox.is_xbox_configured())


class FetchXboxTitlesTests(unittest.TestCase):
    def test_fetch_xbox_titles_returns_dict_entries(self):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = _SAMPLE_TITLE_HISTORY

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, *_args, **_kwargs):
                return mock_response

        with patch("gamelib_mcp.data.xbox.httpx.AsyncClient", return_value=_FakeClient()):
            titles = asyncio.run(xbox.fetch_xbox_titles())

        self.assertEqual(len(titles), 3)

    def test_fetch_xbox_titles_raises_on_unexpected_payload(self):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"unexpected": "shape"}

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, *_args, **_kwargs):
                return mock_response

        with patch("gamelib_mcp.data.xbox.httpx.AsyncClient", return_value=_FakeClient()):
            with self.assertRaises(RuntimeError):
                asyncio.run(xbox.fetch_xbox_titles())


class FetchXboxPlaytimeTests(unittest.TestCase):
    def test_fetch_xbox_playtime_parses_minutes_played(self):
        account_response = MagicMock()
        account_response.raise_for_status.return_value = None
        account_response.json.return_value = {"profileUsers": [{"id": "2535473210914202"}]}

        stats_response = MagicMock()
        stats_response.raise_for_status.return_value = None
        stats_response.json.return_value = {
            "groups": [
                {
                    "titleId": "1030027286",
                    "statlistscollection": [
                        {"stats": [{"name": "MinutesPlayed", "value": "300"}]}
                    ],
                }
            ]
        }

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, *_args, **_kwargs):
                return account_response

            async def post(self, *_args, **_kwargs):
                return stats_response

        with patch("gamelib_mcp.data.xbox.httpx.AsyncClient", return_value=_FakeClient()):
            playtime = asyncio.run(xbox.fetch_xbox_playtime(["1030027286"]))

        self.assertEqual(playtime, {"1030027286": 300})

    def test_fetch_xbox_playtime_returns_empty_on_failure(self):
        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, *_args, **_kwargs):
                raise RuntimeError("boom")

        with (
            patch("gamelib_mcp.data.xbox.httpx.AsyncClient", return_value=_FakeClient()),
            self.assertLogs("gamelib_mcp.data.xbox", level="WARNING") as logs,
        ):
            playtime = asyncio.run(xbox.fetch_xbox_playtime(["1030027286"]))

        self.assertEqual(playtime, {})
        self.assertIn("Xbox playtime unavailable", logs.output[0])

    def test_fetch_xbox_playtime_empty_title_ids_short_circuits(self):
        playtime = asyncio.run(xbox.fetch_xbox_playtime([]))
        self.assertEqual(playtime, {})


class SyncXboxTests(unittest.TestCase):
    def _run_sync(self, titles, playtime_by_title=None, resolve_result=(42, None), candidates=None):
        mock_resolve = AsyncMock(return_value=resolve_result)
        mock_upsert_platform = AsyncMock(return_value=99)
        mock_enrichment = AsyncMock()
        mock_identifier = AsyncMock()
        mock_alias = AsyncMock()
        mock_get_by_identifier = AsyncMock(return_value=None)

        with (
            patch.dict("os.environ", {"OPENXBL_API_KEY": "test-key"}, clear=False),
            patch("gamelib_mcp.data.xbox.fetch_xbox_titles", AsyncMock(return_value=titles)),
            patch("gamelib_mcp.data.xbox.fetch_xbox_playtime", AsyncMock(return_value=playtime_by_title or {})),
            patch("gamelib_mcp.data.xbox.load_fuzzy_candidates", AsyncMock(return_value=candidates or {})),
            patch("gamelib_mcp.data.xbox.get_game_by_identifier", mock_get_by_identifier),
            patch("gamelib_mcp.data.xbox.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.xbox.upsert_game_platform", mock_upsert_platform),
            patch("gamelib_mcp.data.xbox.upsert_game_alias", mock_alias),
            patch("gamelib_mcp.data.xbox.upsert_game_platform_enrichment", mock_enrichment),
            patch("gamelib_mcp.data.xbox.upsert_game_platform_identifier", mock_identifier),
        ):
            result = asyncio.run(xbox.sync_xbox())

        return result, mock_resolve, mock_upsert_platform, mock_enrichment, mock_get_by_identifier

    def test_sync_xbox_unconfigured(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("OPENXBL_API_KEY", None)
            result = asyncio.run(xbox.sync_xbox())

        self.assertEqual(result["sync_status"], "unconfigured")
        self.assertEqual(result["error_classification"], "missing_configuration")
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["skipped"], 0)

    def test_sync_xbox_empty_history_reports_unconfigured(self):
        with (
            patch.dict("os.environ", {"OPENXBL_API_KEY": "test-key"}, clear=False),
            patch("gamelib_mcp.data.xbox.fetch_xbox_titles", AsyncMock(return_value=[])),
        ):
            result = asyncio.run(xbox.sync_xbox())

        self.assertEqual(result["sync_status"], "unconfigured")
        self.assertEqual(result["error_classification"], "missing_configuration")

    def test_sync_xbox_reports_failed_on_fetch_exception(self):
        with (
            patch.dict("os.environ", {"OPENXBL_API_KEY": "test-key"}, clear=False),
            patch("gamelib_mcp.data.xbox.fetch_xbox_titles", AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            result = asyncio.run(xbox.sync_xbox())

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["sync_status"], "failed")
        self.assertIn("boom", result["error_summary"])

    def test_sync_xbox_adds_new_game_by_title_id(self):
        titles = [{"titleId": "1030027286", "name": "Halo Infinite"}]

        result, mock_resolve, mock_upsert_platform, mock_enrichment, mock_get_by_identifier = self._run_sync(
            titles,
            playtime_by_title={"1030027286": 300},
            resolve_result=(42, None),
        )

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0})
        mock_get_by_identifier.assert_awaited_once_with(xbox.XBOX_TITLE_ID, "1030027286")
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Halo Infinite", igdb.PLATFORM_TO_IGDB["xbox"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="xbox",
            playtime_minutes=300,
            owned=1,
        )
        mock_enrichment.assert_not_called()

    def test_sync_xbox_rematches_existing_game_by_title_id(self):
        titles = [{"titleId": "1030027286", "name": "Halo Infinite"}]
        mock_resolve = AsyncMock()
        mock_upsert_platform = AsyncMock(return_value=99)
        mock_get_by_identifier = AsyncMock(return_value={"id": 42})

        with (
            patch.dict("os.environ", {"OPENXBL_API_KEY": "test-key"}, clear=False),
            patch("gamelib_mcp.data.xbox.fetch_xbox_titles", AsyncMock(return_value=titles)),
            patch("gamelib_mcp.data.xbox.fetch_xbox_playtime", AsyncMock(return_value={"1030027286": 300})),
            patch("gamelib_mcp.data.xbox.load_fuzzy_candidates", AsyncMock(return_value={})),
            patch("gamelib_mcp.data.xbox.get_game_by_identifier", mock_get_by_identifier),
            patch("gamelib_mcp.data.xbox.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.xbox.upsert_game_platform", mock_upsert_platform),
            patch("gamelib_mcp.data.xbox.upsert_game_alias", AsyncMock()),
            patch("gamelib_mcp.data.xbox.upsert_game_platform_enrichment", AsyncMock()),
            patch("gamelib_mcp.data.xbox.upsert_game_platform_identifier", AsyncMock()),
        ):
            result = asyncio.run(xbox.sync_xbox())

        self.assertEqual(result, {"added": 0, "matched": 1, "skipped": 0})
        mock_resolve.assert_not_awaited()
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="xbox",
            playtime_minutes=300,
            owned=1,
        )

    def test_sync_xbox_skips_titleless_entries(self):
        titles = [{"titleId": "1", "name": None}, {"name": ""}]

        result, mock_resolve, mock_upsert_platform, mock_enrichment, _ = self._run_sync(titles)

        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 2})
        mock_resolve.assert_not_awaited()
        mock_upsert_platform.assert_not_awaited()

    def test_sync_xbox_playtime_failure_still_syncs_ownership(self):
        titles = [{"titleId": "1030027286", "name": "Halo Infinite"}]

        result, mock_resolve, mock_upsert_platform, mock_enrichment, _ = self._run_sync(
            titles,
            playtime_by_title={},  # fetch_xbox_playtime is best-effort and never raises
            resolve_result=(42, None),
        )

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0})
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="xbox",
            playtime_minutes=None,
            owned=1,
        )

    def test_sync_xbox_matched_game_triggers_platform_release_date_enrichment(self):
        titles = [{"titleId": "99", "name": "Hades"}]
        mock_game = igdb.IGDBGame(
            igdb_id=99,
            name="Hades",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2020-09-17",
            platform_release_dates={igdb.PLATFORM_TO_IGDB["xbox"]: "2020-09-17"},
        )

        result, mock_resolve, mock_upsert_platform, mock_enrichment, _ = self._run_sync(
            titles,
            playtime_by_title={"99": 60},
            resolve_result=(7, mock_game),
            candidates={7: "Hades"},
        )

        self.assertEqual(result["matched"], 1)
        mock_enrichment.assert_awaited_once_with(99, platform_release_date="2020-09-17")


if __name__ == "__main__":
    unittest.main()
