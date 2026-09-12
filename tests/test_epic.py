import asyncio
import contextlib
import itertools
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
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
            playtime, _last_played = asyncio.run(epic.fetch_epic_playtime())

        self.assertEqual(playtime, {"artifact-1": 2, "artifact-2": 7, "artifact-3": 60})

    def test_fetch_epic_playtime_logs_info_for_stale_credentials(self) -> None:
        with (
            patch(
                "gamelib_mcp.data.epic._get_epic_session",
                AsyncMock(
                    side_effect=epic.EpicConfigurationError(
                        "Legendary refresh token rejected; rerun legendary auth"
                    )
                ),
            ),
            self.assertLogs("gamelib_mcp.data.epic", level="INFO") as logs,
        ):
            playtime, _last_played = asyncio.run(epic.fetch_epic_playtime())

        self.assertEqual(playtime, {})
        self.assertIn("INFO:gamelib_mcp.data.epic:Epic playtime unavailable", logs.output[0])


class SyncEpicTests(unittest.TestCase):
    def _run_sync(self, games, playtime_by_artifact=None, resolve_result=(42, None),
                  candidates=None, last_played_by_artifact=None, *,
                  identifier_rows=None, adopt_result=None, parent_result=None,
                  first_minted_id=500):
        """Run sync_epic against mocked writers.

        ``resolve_result`` may be a single (game_id, igdb_game) tuple or a list
        used as consecutive side effects. ``identifier_rows`` maps an identifier
        VALUE (an epic artifact id) to the games row get_game_by_identifier
        should return for it — it backs both the per-item artifact lookup and
        the parent lookup by the base item's releaseInfo appIds. Minted rows get
        consecutive ids from ``first_minted_id`` so two mints are telling apart.
        """
        if isinstance(resolve_result, list):
            mock_resolve = AsyncMock(side_effect=resolve_result)
        else:
            mock_resolve = AsyncMock(return_value=resolve_result)
        mock_upsert_platform = AsyncMock(return_value=99)
        mock_enrichment = AsyncMock()
        mock_identifier = AsyncMock()
        mock_alias = AsyncMock()
        rows = dict(identifier_rows or {})
        mock_get_by_identifier = AsyncMock(
            side_effect=lambda _identifier_type, value: rows.get(value)
        )
        minted = itertools.count(first_minted_id)
        mock_upsert_game = AsyncMock(side_effect=lambda **_kwargs: next(minted))
        mock_adopt = AsyncMock(return_value=adopt_result)
        mock_classify = AsyncMock(return_value=True)
        mock_parent = AsyncMock(return_value=parent_result)
        mock_rename = AsyncMock(return_value=False)

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("gamelib_mcp.data.epic.fetch_epic_library", AsyncMock(return_value=games)),
            patch("gamelib_mcp.data.epic.fetch_epic_playtime", AsyncMock(return_value=(playtime_by_artifact or {}, last_played_by_artifact or {}))),
            patch("gamelib_mcp.data.epic.load_fuzzy_candidates", AsyncMock(return_value=candidates or {})),
            patch("gamelib_mcp.data.epic.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.epic.get_game_by_identifier", mock_get_by_identifier),
            patch("gamelib_mcp.data.epic.adopt_platform_identifier", mock_adopt),
            patch("gamelib_mcp.data.epic.upsert_game", mock_upsert_game),
            patch("gamelib_mcp.data.epic.apply_content_classification", mock_classify),
            patch("gamelib_mcp.data.epic.resolve_parent_game", mock_parent),
            patch("gamelib_mcp.data.epic._apply_epic_catalog_title", mock_rename),
            patch("gamelib_mcp.data.epic.upsert_game_platform", mock_upsert_platform),
            patch("gamelib_mcp.data.epic.upsert_game_alias", mock_alias),
            patch("gamelib_mcp.data.epic.upsert_game_platform_enrichment", mock_enrichment),
            patch("gamelib_mcp.data.epic.upsert_game_platform_identifier", mock_identifier),
        ):
            result = asyncio.run(epic.sync_epic())

        self.mocks = {
            "resolve": mock_resolve,
            "platform": mock_upsert_platform,
            "enrichment": mock_enrichment,
            "identifier": mock_identifier,
            "alias": mock_alias,
            "get_by_identifier": mock_get_by_identifier,
            "upsert_game": mock_upsert_game,
            "adopt": mock_adopt,
            "classify": mock_classify,
            "resolve_parent": mock_parent,
            "rename": mock_rename,
        }
        return result, mock_resolve, mock_upsert_platform, mock_enrichment

    def test_returns_failure_metadata_when_config_missing(self) -> None:
        missing_path = Path("/nonexistent/legendary")

        with patch("gamelib_mcp.data.epic._legendary_config_path", return_value=missing_path):
            result = asyncio.run(epic.sync_epic())

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["sync_status"], "unconfigured")
        self.assertEqual(result["error_classification"], "missing_configuration")

    def test_returns_failure_metadata_when_metadata_cache_empty(self) -> None:
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("gamelib_mcp.data.epic.fetch_epic_library", AsyncMock(return_value=[])),
            patch("gamelib_mcp.data.epic.fetch_epic_playtime", AsyncMock(return_value=({}, {}))),
        ):
            result = asyncio.run(epic.sync_epic())

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["sync_status"], "unconfigured")
        self.assertEqual(result["error_classification"], "missing_configuration")

    def test_returns_failure_metadata_on_sync_exception(self) -> None:
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("gamelib_mcp.data.epic.fetch_epic_library", AsyncMock(side_effect=RuntimeError("boom"))),
            patch("gamelib_mcp.data.epic.fetch_epic_playtime", AsyncMock(return_value=({}, {}))),
        ):
            result = asyncio.run(epic.sync_epic())

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["sync_status"], "failed")
        self.assertEqual(result["error_summary"], "Epic sync failed: boom")

    def test_returns_stale_metadata_but_still_syncs_ownership_when_playtime_auth_is_stale(self) -> None:
        mock_resolve = AsyncMock(return_value=(42, None))
        mock_upsert_platform = AsyncMock(return_value=99)
        mock_identifier = AsyncMock()

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch(
                "gamelib_mcp.data.epic.fetch_epic_library",
                AsyncMock(
                    return_value=[
                        {
                            "app_title": "Celeste",
                            "asset_infos": {"Windows": {"asset_id": "artifact-1"}},
                        }
                    ]
                ),
            ),
            patch(
                "gamelib_mcp.data.epic._get_epic_session",
                AsyncMock(
                    side_effect=epic.EpicConfigurationError(
                        "Legendary refresh token rejected; rerun legendary auth"
                    )
                ),
            ),
            patch("gamelib_mcp.data.epic.load_fuzzy_candidates", AsyncMock(return_value={})),
            patch("gamelib_mcp.data.epic.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.epic.upsert_game_platform", mock_upsert_platform),
            patch("gamelib_mcp.data.epic.upsert_game_platform_enrichment", AsyncMock()),
            patch("gamelib_mcp.data.epic.upsert_game_platform_identifier", mock_identifier),
        ):
            result = asyncio.run(epic.sync_epic())

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["sync_status"], "stale")
        self.assertEqual(result["error_classification"], "auth_stale")
        self.assertIn("Legendary refresh token rejected", result["error_summary"])
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="epic",
            playtime_minutes=None,
            last_played=None,
            owned=1,
            from_source=True,
        )

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

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0, "dlc": 0})
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Celeste", igdb.PLATFORM_TO_IGDB["epic"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="epic",
            playtime_minutes=45,
            last_played=None,
            owned=1,
            from_source=True,
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
            last_played=None,
            owned=1,
            from_source=True,
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

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 1, "dlc": 0})
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Grand Theft Auto V", igdb.PLATFORM_TO_IGDB["epic"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="epic",
            playtime_minutes=5,
            last_played=None,
            owned=1,
            from_source=True,
        )


    def test_skips_unreal_engine_marketplace_assets(self) -> None:
        games = [
            {
                "app_title": "Advanced Flock System - Multithreaded Fish AI",
                "asset_infos": {"Windows": {"asset_id": "artifact-ue", "namespace": "ue"}},
            },
            {
                "app_title": "Advanced Flock System - Multithreaded Fish AI v2",
                "asset_infos": {"Windows": {"asset_id": "artifact-ue-2"}},
                "metadata": {"id": "cat-ue-2", "namespace": "ue"},
            },
            {
                "app_title": "Celeste",
                "asset_infos": {"Windows": {"asset_id": "artifact-1"}},
            },
        ]

        result, mock_resolve, mock_upsert_platform, _ = self._run_sync(games)

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 2, "dlc": 0})
        mock_resolve.assert_awaited_once()
        self.assertEqual(mock_resolve.await_args.args[0], "Celeste")
        mock_upsert_platform.assert_awaited_once()

    def test_skips_mod_items(self) -> None:
        games = [
            {
                "app_title": "Some Community Mod",
                "asset_infos": {"Windows": {"asset_id": "artifact-mod"}},
                "metadata": {"id": "cat-mod", "categories": [{"path": "mods"}, {"path": "games"}]},
            },
            {
                "app_title": "Celeste",
                "asset_infos": {"Windows": {"asset_id": "artifact-1"}},
            },
        ]

        result, mock_resolve, _, _ = self._run_sync(games)

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 1, "dlc": 0})
        mock_resolve.assert_awaited_once()
        self.assertEqual(mock_resolve.await_args.args[0], "Celeste")

    def test_dlc_mints_nested_row_under_base_game_from_same_run(self) -> None:
        games = [
            {
                "app_title": "Control Ultra HD Texture Pack",
                "asset_infos": {"Windows": {"asset_id": "artifact-dlc"}},
                "metadata": {
                    "id": "cat-dlc",
                    "title": "Control Ultra HD Texture Pack",
                    "mainGameItem": {"id": "cat-control", "title": "Control"},
                },
            },
            {
                "app_title": "Control",
                "asset_infos": {"Windows": {"asset_id": "artifact-control"}},
                "metadata": {"id": "cat-control", "title": "Control"},
            },
        ]

        result, mock_resolve, mock_upsert_platform, _ = self._run_sync(
            games, resolve_result=(42, None)
        )

        self.assertEqual(result, {"added": 2, "matched": 0, "skipped": 0, "dlc": 1})
        # The base game is the only item that may reach IGDB/fuzzy resolution.
        mock_resolve.assert_awaited_once()
        self.assertEqual(mock_resolve.await_args.args[0], "Control")
        # adopt/IGDB resolution is reachable for the base game only — the DLC
        # artifact never touches either.
        self.assertEqual(
            [call.kwargs["identifier_value"] for call in self.mocks["adopt"].await_args_list],
            ["artifact-control"],
        )
        self.mocks["upsert_game"].assert_awaited_once_with(
            appid=None,
            name="Control Ultra HD Texture Pack",
            match_existing_by_name=False,
            content_type="dlc",
            parent_game_id=42,
        )
        self.assertEqual(mock_upsert_platform.await_count, 2)
        self.assertEqual(
            [call.kwargs["game_id"] for call in mock_upsert_platform.await_args_list],
            [42, 500],
        )

    def test_dlc_parent_resolves_via_main_game_release_artifact(self) -> None:
        games = [
            {
                "app_title": "Ultra HD Texture Pack",
                "asset_infos": {"Windows": {"asset_id": "artifact-dlc"}},
                "metadata": {
                    "id": "cat-dlc",
                    "mainGameItem": {
                        "id": "cat-unknown",
                        "title": "Some Base Game",
                        "releaseInfo": [{"appId": "artifact-base"}],
                    },
                },
            }
        ]

        result, mock_resolve, _, _ = self._run_sync(
            games,
            identifier_rows={"artifact-base": {"id": 11}},
        )

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0, "dlc": 1})
        mock_resolve.assert_not_awaited()
        self.mocks["resolve_parent"].assert_not_awaited()
        self.assertEqual(self.mocks["upsert_game"].await_args.kwargs["parent_game_id"], 11)

    def test_same_named_dlcs_of_different_games_mint_separate_rows(self) -> None:
        games = [
            {
                "app_title": "Game A",
                "asset_infos": {"Windows": {"asset_id": "artifact-a"}},
                "metadata": {"id": "cat-a"},
            },
            {
                "app_title": "Game B",
                "asset_infos": {"Windows": {"asset_id": "artifact-b"}},
                "metadata": {"id": "cat-b"},
            },
            {
                "app_title": "Ultra HD Texture Pack",
                "asset_infos": {"Windows": {"asset_id": "artifact-dlc-a"}},
                "metadata": {
                    "id": "cat-dlc-a",
                    "mainGameItem": {"id": "cat-a", "title": "Game A"},
                },
            },
            {
                "app_title": "Ultra HD Texture Pack",
                "asset_infos": {"Windows": {"asset_id": "artifact-dlc-b"}},
                "metadata": {
                    "id": "cat-dlc-b",
                    "mainGameItem": {"id": "cat-b", "title": "Game B"},
                },
            },
        ]

        result, _, mock_upsert_platform, _ = self._run_sync(
            games, resolve_result=[(1, None), (2, None)]
        )

        self.assertEqual(result, {"added": 4, "matched": 0, "skipped": 0, "dlc": 2})
        mint_calls = self.mocks["upsert_game"].await_args_list
        self.assertEqual(len(mint_calls), 2)
        self.assertEqual(
            [call.kwargs["name"] for call in mint_calls],
            ["Ultra HD Texture Pack", "Ultra HD Texture Pack"],
        )
        self.assertEqual([call.kwargs["parent_game_id"] for call in mint_calls], [1, 2])
        self.assertTrue(all(call.kwargs["match_existing_by_name"] is False for call in mint_calls))
        # Two distinct nested rows, not one collapsed row.
        self.assertEqual(
            [call.kwargs["game_id"] for call in mock_upsert_platform.await_args_list],
            [1, 2, 500, 501],
        )

    def test_existing_default_row_holding_the_dlc_artifact_is_reclassified(self) -> None:
        games = [
            {
                "app_title": "Control",
                "asset_infos": {"Windows": {"asset_id": "artifact-control"}},
                "metadata": {"id": "cat-control"},
            },
            {
                "app_title": "Ultra HD Texture Pack",
                "asset_infos": {"Windows": {"asset_id": "artifact-dlc"}},
                "metadata": {
                    "id": "cat-dlc",
                    "mainGameItem": {"id": "cat-control", "title": "Control"},
                },
            },
        ]

        result, _, _, _ = self._run_sync(
            games,
            identifier_rows={
                "artifact-dlc": {
                    "id": 7,
                    "content_type": "base_game",
                    "is_primary_library_item": 1,
                    "parent_game_id": None,
                }
            },
        )

        self.assertEqual(result, {"added": 1, "matched": 1, "skipped": 0, "dlc": 1})
        self.mocks["upsert_game"].assert_not_awaited()
        self.mocks["classify"].assert_awaited_once()
        call = self.mocks["classify"].await_args
        self.assertEqual(call.args[0], 7)
        classification = call.args[1]
        self.assertEqual(classification.content_type, "dlc")
        self.assertFalse(classification.is_primary_library_item)
        self.assertEqual(classification.parent_name, "Control")
        self.assertEqual(call.kwargs["source"], "epic")
        self.assertEqual(call.kwargs["parent_game_id"], 42)

    def test_existing_parentless_dlc_row_is_reparented_once_its_base_resolves(self) -> None:
        """A giveaway DLC that arrived before its base game links up later."""
        games = [
            {
                "app_title": "Control",
                "asset_infos": {"Windows": {"asset_id": "artifact-control"}},
                "metadata": {"id": "cat-control"},
            },
            {
                "app_title": "Ultra HD Texture Pack",
                "asset_infos": {"Windows": {"asset_id": "artifact-dlc"}},
                "metadata": {
                    "id": "cat-dlc",
                    "mainGameItem": {"id": "cat-control", "title": "Control"},
                },
            },
        ]

        result, _, _, _ = self._run_sync(
            games,
            identifier_rows={
                "artifact-dlc": {
                    "id": 7,
                    "content_type": "dlc",
                    "is_primary_library_item": 0,
                    "parent_game_id": None,
                }
            },
        )

        self.assertEqual(result, {"added": 1, "matched": 1, "skipped": 0, "dlc": 1})
        self.mocks["classify"].assert_awaited_once()
        call = self.mocks["classify"].await_args
        self.assertEqual(call.args[0], 7)
        self.assertEqual(call.args[1].content_type, "dlc")
        self.assertEqual(call.kwargs["parent_game_id"], 42)

    def test_existing_non_default_row_is_not_reclassified(self) -> None:
        games = [
            {
                "app_title": "Ultra HD Texture Pack",
                "asset_infos": {"Windows": {"asset_id": "artifact-dlc"}},
                "metadata": {
                    "id": "cat-dlc",
                    "mainGameItem": {"id": "cat-control", "title": "Control"},
                },
            }
        ]

        result, _, _, _ = self._run_sync(
            games,
            identifier_rows={
                "artifact-dlc": {
                    "id": 7,
                    "content_type": "expansion",
                    "is_primary_library_item": 0,
                    "parent_game_id": 3,
                }
            },
        )

        self.assertEqual(result, {"added": 0, "matched": 1, "skipped": 0, "dlc": 1})
        self.mocks["classify"].assert_not_awaited()
        self.mocks["upsert_game"].assert_not_awaited()


class _FakeDB:
    """Minimal DBConnection stand-in for the catalog-title rename helper."""

    def __init__(self, *, name: str, other_platform: bool) -> None:
        self.name = name
        self.other_platform = other_platform
        self.writes: list[tuple[str, tuple]] = []

    async def execute_fetchone(self, sql, params=()):
        if "FROM games" in sql:
            return {"name": self.name}
        if "FROM game_platforms" in sql:
            return {"1": 1} if self.other_platform else None
        return None

    async def execute(self, sql, params=()):
        self.writes.append((sql, params))

    async def commit(self):
        return None


@contextlib.asynccontextmanager
async def _fake_get_db(db):
    yield db


class EpicCatalogTitleRenameTests(unittest.TestCase):
    def _run_rename(self, *, stored_name, other_platform, overrides=frozenset()):
        db = _FakeDB(name=stored_name, other_platform=other_platform)
        with (
            patch("gamelib_mcp.data.epic.get_db", lambda: _fake_get_db(db)),
            patch(
                "gamelib_mcp.data.epic.get_manual_overrides",
                AsyncMock(return_value=set(overrides)),
            ),
        ):
            renamed = asyncio.run(epic._apply_epic_catalog_title(5, "Hades II"))
        return renamed, db

    def test_renames_when_epic_is_the_only_platform(self) -> None:
        renamed, db = self._run_rename(stored_name="Hades 2", other_platform=False)

        self.assertTrue(renamed)
        self.assertEqual(len(db.writes), 1)
        sql, params = db.writes[0]
        self.assertIn("UPDATE games SET name", sql)
        self.assertEqual(params[0], "Hades II")
        self.assertEqual(params[2], 5)
        self.assertEqual(params[1], epic.normalize_search_text("Hades II"))

    def test_skips_rename_when_another_platform_owns_the_game(self) -> None:
        renamed, db = self._run_rename(stored_name="Hades 2", other_platform=True)

        self.assertFalse(renamed)
        self.assertEqual(db.writes, [])

    def test_skips_rename_when_name_is_manually_overridden(self) -> None:
        renamed, db = self._run_rename(
            stored_name="Hades 2", other_platform=False, overrides={"name"}
        )

        self.assertFalse(renamed)
        self.assertEqual(db.writes, [])

    def test_no_write_when_name_already_matches(self) -> None:
        renamed, db = self._run_rename(stored_name="Hades II", other_platform=False)

        self.assertFalse(renamed)
        self.assertEqual(db.writes, [])
