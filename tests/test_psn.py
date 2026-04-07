import sys
import types
import asyncio
import unittest
from datetime import timedelta
from enum import Enum
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
    from psnawp_api.models.title_stats import PlatformCategory  # type: ignore
except ModuleNotFoundError:
    psnawp_api = types.ModuleType("psnawp_api")
    models = types.ModuleType("psnawp_api.models")
    title_stats = types.ModuleType("psnawp_api.models.title_stats")

    class PlatformCategory(Enum):
        UNKNOWN = 0
        PS5 = 1
        PS4 = 2

    class PSNAWP:  # pragma: no cover - import stub only
        def __init__(self, *_args, **_kwargs):
            pass

    title_stats.PlatformCategory = PlatformCategory
    models.title_stats = title_stats
    psnawp_api.models = models
    psnawp_api.PSNAWP = PSNAWP
    sys.modules["psnawp_api"] = psnawp_api
    sys.modules["psnawp_api.models"] = models
    sys.modules["psnawp_api.models.title_stats"] = title_stats

from gamelib_mcp.data import igdb, psn


def _make_entry(name, title_id="PPSA12345_00", category=PlatformCategory.PS5, play_duration=timedelta(minutes=90)):
    entry = MagicMock()
    entry.name = name
    entry.title_id = title_id
    entry.category = category
    entry.play_duration = play_duration
    return entry


class FetchPsnLibraryFilterTests(unittest.TestCase):
    def _run_fetch(self, entries):
        mock_client = MagicMock()
        mock_client.title_stats.return_value = iter(entries)
        mock_psnawp = MagicMock()
        mock_psnawp.me.return_value = mock_client

        with patch("gamelib_mcp.data.psn._get_psnawp", return_value=mock_psnawp):
            return asyncio.run(psn.fetch_psn_library())

    def test_normal_ps5_game_passes_through(self) -> None:
        entries = [_make_entry("Elden Ring")]
        result = self._run_fetch(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Elden Ring")

    def test_ppsa_unknown_entry_filtered(self) -> None:
        """Primary heuristic: PPSA prefix + UNKNOWN category = streaming app."""
        entries = [_make_entry("Netflix", title_id="PPSA99999_00", category=PlatformCategory.UNKNOWN)]
        result = self._run_fetch(entries)
        self.assertEqual(result, [])

    def test_ppsa_non_unknown_category_not_filtered(self) -> None:
        """PPSA prefix alone is not enough — category must be UNKNOWN."""
        entries = [_make_entry("Some PS5 Game", title_id="PPSA12345_00", category=PlatformCategory.PS5)]
        result = self._run_fetch(entries)
        self.assertEqual(len(result), 1)

    def test_name_blocklist_filters_legacy_cusa_app(self) -> None:
        """Secondary heuristic: blocklisted name catches PS4-era CUSA apps."""
        entries = [_make_entry("Spotify", title_id="CUSA12345_00", category=PlatformCategory.UNKNOWN)]
        result = self._run_fetch(entries)
        self.assertEqual(result, [])

    def test_play_duration_converted_to_minutes(self) -> None:
        entries = [_make_entry("God of War", play_duration=timedelta(hours=2, minutes=30))]
        result = self._run_fetch(entries)
        self.assertEqual(result[0]["playtime_minutes"], 150)

    def test_zero_play_duration_produces_zero_minutes(self) -> None:
        entries = [_make_entry("New Game", play_duration=timedelta(0))]
        result = self._run_fetch(entries)
        self.assertEqual(result[0]["playtime_minutes"], 0)

    def test_entry_with_no_name_skipped(self) -> None:
        entries = [_make_entry(None)]
        result = self._run_fetch(entries)
        self.assertEqual(result, [])


class SyncPsnSkipTests(unittest.TestCase):
    def test_skips_when_npsso_not_set(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("PSN_NPSSO", None)
            result = asyncio.run(psn.sync_psn())
        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})

    def test_returns_zeros_on_fetch_exception(self) -> None:
        with (
            patch.dict("os.environ", {"PSN_NPSSO": "fake"}, clear=False),
            patch("gamelib_mcp.data.psn.fetch_psn_library", AsyncMock(side_effect=Exception("auth failed"))),
        ):
            result = asyncio.run(psn.sync_psn())
        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})


class SyncPsnSyncTests(unittest.TestCase):
    def _run_sync(self, entries, resolve_result, candidates=None):
        mock_resolve = AsyncMock(return_value=resolve_result)
        mock_upsert_platform = AsyncMock(return_value=99)
        mock_load_candidates = AsyncMock(return_value=candidates or {})

        with (
            patch.dict("os.environ", {"PSN_NPSSO": "fake"}, clear=False),
            patch("gamelib_mcp.data.psn.fetch_psn_library", AsyncMock(return_value=entries)),
            patch("gamelib_mcp.data.psn.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.psn.upsert_game_platform", mock_upsert_platform),
            patch("gamelib_mcp.data.psn.load_fuzzy_candidates", mock_load_candidates),
        ):
            result = asyncio.run(psn.sync_psn())

        return result, mock_resolve, mock_upsert_platform

    def test_matched_game_increments_matched(self) -> None:
        entries = [{"name": "Elden Ring", "playtime_minutes": 120}]
        result, mock_resolve, mock_upsert_platform = self._run_sync(
            entries,
            resolve_result=(7, None),
            candidates={7: "Elden Ring"},
        )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["added"], 0)
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Elden Ring", igdb.PLATFORM_TO_IGDB["ps5"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=7,
            platform="ps5",
            playtime_minutes=120,
            owned=1,
        )

    def test_unmatched_game_increments_added(self) -> None:
        entries = [{"name": "Unknown Indie", "playtime_minutes": 60}]
        result, mock_resolve, mock_upsert_platform = self._run_sync(
            entries,
            resolve_result=(42, None),
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["matched"], 0)
        mock_resolve.assert_awaited_once()
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="ps5",
            playtime_minutes=60,
            owned=1,
        )

    def test_upsert_platform_called_with_playtime(self) -> None:
        entries = [{"name": "Elden Ring", "playtime_minutes": 150}]
        _, _, mock_upsert_platform = self._run_sync(entries, resolve_result=(1, None))
        call_kwargs = mock_upsert_platform.call_args.kwargs
        self.assertEqual(call_kwargs["playtime_minutes"], 150)
        self.assertEqual(call_kwargs["platform"], "ps5")

    def test_resolver_patch_receives_platform_id(self) -> None:
        entries = [{"name": "Elden Ring", "playtime_minutes": 150}]
        mock_game = igdb.IGDBGame(
            igdb_id=1,
            name="Elden Ring",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2022-02-25",
        )

        mock_resolve = AsyncMock(return_value=(1, mock_game))

        with (
            patch.dict("os.environ", {"PSN_NPSSO": "fake"}, clear=False),
            patch("gamelib_mcp.data.psn.fetch_psn_library", AsyncMock(return_value=entries)),
            patch("gamelib_mcp.data.psn.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.psn.upsert_game_platform", AsyncMock(return_value=99)),
            patch("gamelib_mcp.data.psn.load_fuzzy_candidates", AsyncMock(return_value={})),
        ):
            asyncio.run(psn.sync_psn())

        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Elden Ring", igdb.PLATFORM_TO_IGDB["ps5"]),
        )


if __name__ == "__main__":
    unittest.main()
