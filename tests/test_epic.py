import sys
import types
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import aiosqlite  # type: ignore
except ModuleNotFoundError:
    aiosqlite = types.ModuleType("aiosqlite")

    class Connection:  # minimal stub for db.py import-time polyfill
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

from gamelib_mcp.data import epic, igdb


class EpicHelpersTests(unittest.TestCase):
    def test_fetch_epic_library_reads_cached_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_dir = Path(tmpdir) / "metadata"
            metadata_dir.mkdir(parents=True)
            (metadata_dir / "game.json").write_text(
                json.dumps(
                    {
                        "app_name": "artifact-1",
                        "app_title": "Test Game",
                        "asset_infos": {
                            "Windows": {
                                "asset_id": "artifact-1",
                                "app_name": "artifact-1",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"EPIC_LEGENDARY_PATH": tmpdir}, clear=False):
                games = asyncio.run(epic.fetch_epic_library())

        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["app_title"], "Test Game")

    def test_extract_epic_artifact_id_prefers_asset_id(self) -> None:
        artifact_id = epic._extract_epic_artifact_id(
            {
                "app_name": "launcher-name",
                "asset_infos": {
                    "Windows": {
                        "asset_id": "artifact-123",
                        "app_name": "launcher-name",
                    }
                },
            }
        )

        self.assertEqual(artifact_id, "artifact-123")

    def test_fetch_epic_playtime_maps_artifact_ids(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"artifactId": "artifact-1", "totalTime": 123},
            {"artifactId": "artifact-2", "totalTime": "456"},
            {"artifactId": "artifact-3", "totalTime": 3600},
        ]
        mock_response.raise_for_status.return_value = None

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, *_args, **_kwargs):
                return mock_response

        with (
            patch(
                "gamelib_mcp.data.epic._get_epic_session",
                AsyncMock(
                    return_value={
                        "account_id": "acct-1",
                        "access_token": "token-1",
                        "refresh_token": "refresh-1",
                    }
                ),
            ),
            patch("gamelib_mcp.data.epic.httpx.AsyncClient", return_value=_FakeClient()),
        ):
            playtime = asyncio.run(epic.fetch_epic_playtime())

        self.assertEqual(playtime, {"artifact-1": 2, "artifact-2": 7, "artifact-3": 60})


class SyncEpicTests(unittest.TestCase):
    def _run_sync(self, games, playtime_by_artifact=None, resolve_result=(42, None), candidates=None):
        mock_resolve = AsyncMock(return_value=resolve_result)
        mock_upsert_platform = AsyncMock(return_value=99)
        mock_enrichment = AsyncMock()
        mock_identifier = AsyncMock()

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("gamelib_mcp.data.epic.fetch_epic_library", AsyncMock(return_value=games)),
            patch("gamelib_mcp.data.epic.fetch_epic_playtime", AsyncMock(return_value=playtime_by_artifact or {})),
            patch("gamelib_mcp.data.epic.load_fuzzy_candidates", AsyncMock(return_value=candidates or {})),
            patch("gamelib_mcp.data.epic.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.epic.upsert_game_platform", mock_upsert_platform),
            patch("gamelib_mcp.data.epic.upsert_game_platform_enrichment", mock_enrichment),
            patch("gamelib_mcp.data.epic.upsert_game_platform_identifier", mock_identifier),
        ):
            result = asyncio.run(epic.sync_epic())

        return result, mock_resolve, mock_upsert_platform, mock_enrichment

    def test_unmatched_game_still_syncs_when_igdb_returns_no_result(self) -> None:
        games = [
            {
                "app_title": "Celeste",
                "asset_infos": {"Windows": {"asset_id": "artifact-1"}},
            }
        ]

        result, mock_resolve, mock_upsert_platform, mock_enrichment = self._run_sync(
            games,
            playtime_by_artifact={"artifact-1": 45},
            resolve_result=(42, None),
        )

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0})
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Celeste", igdb.PLATFORM_TO_IGDB["epic"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="epic",
            playtime_minutes=45,
            owned=1,
        )
        mock_enrichment.assert_not_called()

    def test_matched_game_triggers_platform_release_date_enrichment(self) -> None:
        games = [
            {
                "title": "Hades",
                "asset_infos": {"Windows": {"asset_id": "artifact-2"}},
            }
        ]
        mock_game = igdb.IGDBGame(
            igdb_id=99,
            name="Hades",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2020-09-17",
            platform_release_dates={igdb.PLATFORM_TO_IGDB["epic"]: "2020-09-17"},
        )

        result, mock_resolve, mock_upsert_platform, mock_enrichment = self._run_sync(
            games,
            playtime_by_artifact={"artifact-2": 60},
            resolve_result=(7, mock_game),
            candidates={7: "Hades"},
        )

        self.assertEqual(result["matched"], 1)
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Hades", igdb.PLATFORM_TO_IGDB["epic"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=7,
            platform="epic",
            playtime_minutes=60,
            owned=1,
        )
        mock_enrichment.assert_awaited_once_with(99, platform_release_date="2020-09-17")

    def test_sync_skips_non_game_rows_and_normalizes_titles_before_resolving(self) -> None:
        games = [
            {"title": "Q.U.B.E. 2 Soundtrack", "asset_infos": {"Windows": {"asset_id": "artifact-1"}}},
            {"title": "Grand Theft Auto V (PlayStation®5)", "asset_infos": {"Windows": {"asset_id": "artifact-2"}}},
        ]

        result, mock_resolve, mock_upsert_platform, _ = self._run_sync(
            games,
            playtime_by_artifact={"artifact-2": 5},
            resolve_result=(42, None),
        )

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 1})
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Grand Theft Auto V", igdb.PLATFORM_TO_IGDB["epic"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="epic",
            playtime_minutes=5,
            owned=1,
        )
