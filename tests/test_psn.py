import asyncio
import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from enum import Enum
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


def _run_async(coro):
    # Python 3.14 in this environment hangs in asyncio.run() when shutting down
    # the default executor after fetch_psn_library() offloads sync PSNAWP work.
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _make_entry(name, title_id="PPSA12345_00", category=PlatformCategory.PS5, play_duration=timedelta(minutes=90), last_played=None):
    entry = MagicMock()
    entry.name = name
    entry.title_id = title_id
    entry.category = category
    entry.play_duration = play_duration
    entry.last_played_date_time = last_played
    return entry


class FetchPsnLibraryFilterTests(unittest.TestCase):
    def _run_fetch(self, entries):
        mock_client = MagicMock()
        mock_client.title_stats.return_value = iter(entries)
        mock_psnawp = MagicMock()
        mock_psnawp.me.return_value = mock_client

        with patch("gamelib_mcp.data.psn._get_psnawp", return_value=mock_psnawp):
            return _run_async(psn.fetch_psn_library())

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

    def test_last_played_date_time_converted_to_iso_date(self) -> None:
        entries = [
            _make_entry(
                "Elden Ring",
                last_played=datetime(2026, 6, 20, 14, 30, tzinfo=UTC),
            )
        ]
        result = self._run_fetch(entries)
        self.assertEqual(result[0]["last_played"], "2026-06-20")

    def test_missing_last_played_is_none(self) -> None:
        entries = [_make_entry("Elden Ring", last_played=None)]
        result = self._run_fetch(entries)
        self.assertIsNone(result[0]["last_played"])


class SyncPsnSkipTests(unittest.TestCase):
    def test_skips_when_npsso_not_set(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("PSN_NPSSO", None)
            result = _run_async(psn.sync_psn())
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["sync_status"], "unconfigured")
        self.assertEqual(result["error_classification"], "missing_configuration")

    def test_returns_zeros_on_fetch_exception(self) -> None:
        with (
            patch.dict("os.environ", {"PSN_NPSSO": "fake"}, clear=False),
            patch("gamelib_mcp.data.psn.fetch_psn_library", AsyncMock(side_effect=Exception("auth failed"))),
        ):
            result = _run_async(psn.sync_psn())
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["sync_status"], "failed")
        self.assertEqual(result["error_summary"], "PSN sync failed: auth failed")


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
            result = _run_async(psn.sync_psn())

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
            last_played=None,
            owned=1,
            from_source=True,
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
            last_played=None,
            owned=1,
            from_source=True,
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
            _run_async(psn.sync_psn())

        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Elden Ring", igdb.PLATFORM_TO_IGDB["ps5"]),
        )

    def test_sync_normalizes_titles_and_skips_non_game_rows(self) -> None:
        entries = [
            {"name": "Grand Theft Auto V (PlayStation®5)", "playtime_minutes": 30},
            {"name": "Q.U.B.E. 2 Soundtrack", "playtime_minutes": 0},
        ]
        result, mock_resolve, mock_upsert_platform = self._run_sync(entries, resolve_result=(42, None))

        self.assertEqual(
            result, {"added": 1, "matched": 0, "skipped": 1, "merged_skus": 0}
        )
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Grand Theft Auto V", igdb.PLATFORM_TO_IGDB["ps5"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="ps5",
            playtime_minutes=30,
            last_played=None,
            owned=1,
            from_source=True,
        )

    def test_repairs_stale_conflicting_platform_row(self) -> None:
        entries = [{"name": "Borderlands 4", "playtime_minutes": 45}]
        mock_repair = AsyncMock()

        with (
            patch.dict("os.environ", {"PSN_NPSSO": "fake"}, clear=False),
            patch("gamelib_mcp.data.psn.fetch_psn_library", AsyncMock(return_value=entries)),
            patch("gamelib_mcp.data.psn.load_fuzzy_candidates", AsyncMock(return_value={7: "Borderlands 2"})),
            patch("gamelib_mcp.data.psn.find_conflicting_fuzzy_key", return_value=7),
            patch("gamelib_mcp.data.psn.resolve_and_link_game", AsyncMock(return_value=(42, None))),
            patch("gamelib_mcp.data.psn.repair_misclassified_platform_row", mock_repair),
            patch("gamelib_mcp.data.psn.upsert_game_platform", AsyncMock(return_value=99)),
        ):
            _run_async(psn.sync_psn())

        mock_repair.assert_awaited_once_with(
            source_game_id=7,
            target_game_id=42,
            platform="ps5",
        )

    def test_does_not_repair_when_conflicting_source_title_is_in_current_payload(self) -> None:
        entries = [
            {"name": "Borderlands 2", "playtime_minutes": 120},
            {"name": "Borderlands 4", "playtime_minutes": 45},
        ]
        mock_repair = AsyncMock()

        with (
            patch.dict("os.environ", {"PSN_NPSSO": "fake"}, clear=False),
            patch("gamelib_mcp.data.psn.fetch_psn_library", AsyncMock(return_value=entries)),
            patch("gamelib_mcp.data.psn.load_fuzzy_candidates", AsyncMock(return_value={7: "Borderlands 2"})),
            patch("gamelib_mcp.data.psn.find_conflicting_fuzzy_key", side_effect=[None, 7]),
            patch(
                "gamelib_mcp.data.psn.resolve_and_link_game",
                AsyncMock(side_effect=[(7, None), (42, None)]),
            ),
            patch("gamelib_mcp.data.psn.repair_misclassified_platform_row", mock_repair),
            patch("gamelib_mcp.data.psn.upsert_game_platform", AsyncMock(return_value=99)),
        ):
            _run_async(psn.sync_psn())

        mock_repair.assert_not_awaited()


class FetchPsnEnglishResolutionTests(unittest.TestCase):
    def _run_fetch(self, entries, game_title=None):
        mock_client = MagicMock()
        mock_client.title_stats.return_value = iter(entries)
        mock_psnawp = MagicMock()
        mock_psnawp.me.return_value = mock_client
        if game_title is not None:
            mock_psnawp.game_title.return_value = game_title
        with patch("gamelib_mcp.data.psn._get_psnawp", return_value=mock_psnawp):
            return _run_async(psn.fetch_psn_library()), mock_psnawp

    def test_non_latin_name_resolved_to_english(self) -> None:
        gt = MagicMock()
        gt.get_details.return_value = [{"name": "Hogwarts Legacy"}]
        result, mock_psnawp = self._run_fetch(
            [_make_entry("霍格沃茨之遗", title_id="PPSA01")], game_title=gt
        )
        self.assertEqual(result[0]["name"], "Hogwarts Legacy")
        self.assertEqual(result[0]["title_id"], "PPSA01")
        mock_psnawp.game_title.assert_called_once()

    def test_latin_name_skips_lookup_and_keeps_title_id(self) -> None:
        result, mock_psnawp = self._run_fetch([_make_entry("Elden Ring", title_id="PPSA02")])
        self.assertEqual(result[0]["name"], "Elden Ring")
        self.assertEqual(result[0]["title_id"], "PPSA02")
        mock_psnawp.game_title.assert_not_called()

    def test_lookup_failure_keeps_original_name(self) -> None:
        result, _ = self._run_fetch(
            [_make_entry("霍格沃茨之遗", title_id="PPSA03")],
            game_title=MagicMock(get_details=MagicMock(side_effect=Exception("api down"))),
        )
        self.assertEqual(result[0]["name"], "霍格沃茨之遗")

    def test_empty_details_keeps_original_name(self) -> None:
        gt = MagicMock()
        gt.get_details.return_value = []
        result, _ = self._run_fetch([_make_entry("波斯王子", title_id="PPSA04")], game_title=gt)
        self.assertEqual(result[0]["name"], "波斯王子")


class PsnTitleIdMatchingTests(unittest.TestCase):
    def test_existing_title_id_matches_without_fuzzy_resolution(self) -> None:
        entries = [{"name": "Hogwarts Legacy", "title_id": "PPSA10", "playtime_minutes": 30}]
        mock_resolve = AsyncMock(return_value=(99, None))
        mock_get_by_id = AsyncMock(return_value={"id": 5})
        mock_upsert_id = AsyncMock()
        with (
            patch.dict("os.environ", {"PSN_NPSSO": "fake"}, clear=False),
            patch("gamelib_mcp.data.psn.fetch_psn_library", AsyncMock(return_value=entries)),
            patch("gamelib_mcp.data.psn.load_fuzzy_candidates", AsyncMock(return_value={})),
            patch("gamelib_mcp.data.psn.get_game_by_identifier", mock_get_by_id),
            patch("gamelib_mcp.data.psn.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.psn.upsert_game_platform", AsyncMock(return_value=77)),
            patch("gamelib_mcp.data.psn.upsert_game_platform_identifier", mock_upsert_id),
        ):
            result = _run_async(psn.sync_psn())
        mock_get_by_id.assert_awaited_once_with(psn.PSN_TITLE_ID, "PPSA10")
        mock_resolve.assert_not_awaited()  # stable id short-circuits fuzzy matching
        self.assertEqual(result["matched"], 1)
        mock_upsert_id.assert_awaited_once_with(77, psn.PSN_TITLE_ID, "PPSA10")

    def test_first_ingest_stores_title_id(self) -> None:
        entries = [{"name": "New PS5 Game", "title_id": "PPSA11", "playtime_minutes": 10}]
        mock_upsert_id = AsyncMock()
        with (
            patch.dict("os.environ", {"PSN_NPSSO": "fake"}, clear=False),
            patch("gamelib_mcp.data.psn.fetch_psn_library", AsyncMock(return_value=entries)),
            patch("gamelib_mcp.data.psn.load_fuzzy_candidates", AsyncMock(return_value={})),
            patch("gamelib_mcp.data.psn.get_game_by_identifier", AsyncMock(return_value=None)),
            patch("gamelib_mcp.data.psn.resolve_and_link_game", AsyncMock(return_value=(42, None))),
            patch("gamelib_mcp.data.psn.upsert_game_platform", AsyncMock(return_value=88)),
            patch("gamelib_mcp.data.psn.upsert_game_platform_identifier", mock_upsert_id),
        ):
            result = _run_async(psn.sync_psn())
        self.assertEqual(result["added"], 1)
        mock_upsert_id.assert_awaited_once_with(88, psn.PSN_TITLE_ID, "PPSA11")


class PsnCrossGenSkuTests(unittest.TestCase):
    """PSN returns one entry per SKU; a cross-gen game has two or three.

    They share one game_platforms row (UNIQUE(game_id, platform)), so before
    aggregation the last SKU written silently overwrote the others' playtime —
    and since PSN sorts by last-played descending, the last writer was the
    STALEST SKU. Prod lost AC Valhalla's and Ghost of Tsushima's PS5 hours to
    their PS4 entries this way.
    """

    def _run_sync(self, entries, *, resolve_game_id=42, existing_by_id=None):
        mock_upsert_platform = AsyncMock(return_value=77)
        mock_upsert_id = AsyncMock()
        mock_upsert_game = AsyncMock(side_effect=lambda **kw: 900 + len(kw))
        with (
            patch.dict("os.environ", {"PSN_NPSSO": "fake"}, clear=False),
            patch("gamelib_mcp.data.psn.fetch_psn_library", AsyncMock(return_value=entries)),
            patch("gamelib_mcp.data.psn.load_fuzzy_candidates", AsyncMock(return_value={})),
            patch(
                "gamelib_mcp.data.psn.get_game_by_identifier",
                AsyncMock(side_effect=existing_by_id or (lambda *_a: None)),
            ),
            patch("gamelib_mcp.data.psn.adopt_platform_identifier", AsyncMock(return_value=None)),
            patch(
                "gamelib_mcp.data.psn.resolve_and_link_game",
                AsyncMock(return_value=(resolve_game_id, None)),
            ),
            patch("gamelib_mcp.data.psn.upsert_game", mock_upsert_game),
            patch("gamelib_mcp.data.psn.upsert_game_platform", mock_upsert_platform),
            patch("gamelib_mcp.data.psn.upsert_game_platform_identifier", mock_upsert_id),
        ):
            result = _run_async(psn.sync_psn())
        return result, mock_upsert_platform, mock_upsert_id, mock_upsert_game

    def test_ps4_and_ps5_skus_sum_into_one_row(self) -> None:
        # PSN order: most-recently-played first, so the PS4 entry lands last and
        # used to clobber the PS5 playtime with its own.
        entries = [
            {"name": "Assassin's Creed Valhalla", "title_id": "PPSA01490_00",
             "playtime_minutes": 2400, "last_played": "2023-01-15"},
            {"name": "Assassin's Creed Valhalla", "title_id": "CUSA18522_00",
             "playtime_minutes": 48, "last_played": "2022-06-20"},
        ]
        result, mock_upsert_platform, _ids, _mint = self._run_sync(entries)

        mock_upsert_platform.assert_awaited_once()
        kwargs = mock_upsert_platform.await_args.kwargs
        self.assertEqual(kwargs["playtime_minutes"], 2448)
        # last_played takes the MAX — a newer session is never walked backwards.
        self.assertEqual(kwargs["last_played"], "2023-01-15")
        # One game, not two: the extra SKU is reported separately.
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["merged_skus"], 1)

    def test_edition_suffix_still_counts_as_the_same_game(self) -> None:
        entries = [
            {"name": "Ghost of Tsushima DIRECTOR'S CUT", "title_id": "PPSA05030_00",
             "playtime_minutes": 4814, "last_played": "2022-08-13"},
            {"name": "Ghost of Tsushima", "title_id": "CUSA32708_00",
             "playtime_minutes": 46, "last_played": "2021-09-01"},
        ]
        _result, mock_upsert_platform, _ids, _mint = self._run_sync(entries)

        mock_upsert_platform.assert_awaited_once()
        self.assertEqual(
            mock_upsert_platform.await_args.kwargs["playtime_minutes"], 4860
        )

    def test_most_played_sku_becomes_primary_identifier(self) -> None:
        entries = [
            {"name": "Assassin's Creed Valhalla", "title_id": "PPSA01490_00",
             "playtime_minutes": 2400},
            {"name": "Assassin's Creed Valhalla", "title_id": "CUSA18522_00",
             "playtime_minutes": 48},
        ]
        _result, _platform, mock_upsert_id, _mint = self._run_sync(entries)

        calls = mock_upsert_id.await_args_list
        self.assertEqual(len(calls), 2)
        # Lesser SKU written first and explicitly non-primary; the most-played
        # one last, so the write's demotion pass leaves it is_primary.
        self.assertEqual(calls[0].args, (77, psn.PSN_TITLE_ID, "CUSA18522_00"))
        self.assertFalse(calls[0].kwargs["is_primary"])
        self.assertEqual(calls[1].args, (77, psn.PSN_TITLE_ID, "PPSA01490_00"))
        self.assertEqual(calls[1].kwargs, {})

    def test_different_game_is_not_folded_in(self) -> None:
        """The gate: resolution collapsing two distinct games must not sum them.

        Prod shape — the 2016 "Ratchet & Clank" (CUSA01047_00) resolved onto
        "Ratchet & Clank: Rift Apart" and rode its platform row.
        """
        entries = [
            {"name": "Ratchet & Clank: Rift Apart", "title_id": "PPSA01473_00",
             "playtime_minutes": 600},
            {"name": "Ratchet & Clank", "title_id": "CUSA01047_00",
             "playtime_minutes": 65},
        ]
        result, mock_upsert_platform, _ids, mock_upsert_game = self._run_sync(entries)

        # Two platform writes — the second SKU got its own games row.
        self.assertEqual(mock_upsert_platform.await_count, 2)
        written = [c.kwargs["playtime_minutes"] for c in mock_upsert_platform.await_args_list]
        self.assertEqual(sorted(written), [65, 600])
        self.assertEqual(result["merged_skus"], 0)
        mock_upsert_game.assert_awaited_once()
        self.assertEqual(mock_upsert_game.await_args.kwargs["name"], "Ratchet & Clank")
        # Never re-collapse onto the row the gate just rejected.
        self.assertIs(mock_upsert_game.await_args.kwargs["match_existing_by_name"], False)

    def test_single_sku_write_is_unchanged(self) -> None:
        entries = [{"name": "Elden Ring", "title_id": "PPSA02", "playtime_minutes": 150,
                    "last_played": "2024-03-02"}]
        result, mock_upsert_platform, mock_upsert_id, mock_upsert_game = self._run_sync(entries)

        mock_upsert_platform.assert_awaited_once_with(
            game_id=42, platform="ps5", playtime_minutes=150,
            last_played="2024-03-02", owned=1, from_source=True,
        )
        mock_upsert_id.assert_awaited_once_with(77, psn.PSN_TITLE_ID, "PPSA02")
        mock_upsert_game.assert_not_awaited()
        self.assertEqual(result["merged_skus"], 0)

    def test_null_playtime_skus_do_not_become_zero(self) -> None:
        entries = [
            {"name": "Sackboy", "title_id": "PPSA03", "playtime_minutes": None},
            {"name": "Sackboy", "title_id": "CUSA03", "playtime_minutes": None},
        ]
        _result, mock_upsert_platform, _ids, _mint = self._run_sync(entries)
        self.assertIsNone(mock_upsert_platform.await_args.kwargs["playtime_minutes"])


class PsnHelperTests(unittest.TestCase):
    def test_is_probably_non_latin(self) -> None:
        self.assertTrue(psn._is_probably_non_latin("霍格沃茨之遗"))
        self.assertTrue(psn._is_probably_non_latin("波斯王子:Rogue"))
        self.assertFalse(psn._is_probably_non_latin("The Rogue Prince of Persia"))
        self.assertFalse(psn._is_probably_non_latin("NieR: Automata"))

    def test_is_probably_non_latin_covers_non_cjk_scripts(self) -> None:
        # Cyrillic, Arabic, Hebrew, Thai — locales outside CJK that also fork
        # localized-name duplicates without resolution.
        self.assertTrue(psn._is_probably_non_latin("Ведьмак 3"))  # Cyrillic
        self.assertTrue(psn._is_probably_non_latin("بريق الشمس"))  # Arabic
        self.assertTrue(psn._is_probably_non_latin("שדים"))  # Hebrew
        self.assertTrue(psn._is_probably_non_latin("เกม"))  # Thai
        # Latin-with-accents must still read as Latin (no false positives).
        self.assertFalse(psn._is_probably_non_latin("Pokémon Légends"))


if __name__ == "__main__":
    unittest.main()
