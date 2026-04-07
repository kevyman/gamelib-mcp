import unittest
from unittest.mock import AsyncMock, patch

from gamelib_mcp.data import opencritic


class OpenCriticMatcherTests(unittest.TestCase):
    def test_normalize_match_title_folds_accents_and_ampersands(self) -> None:
        self.assertEqual(
            opencritic._normalize_match_title("Pokémon Mystery & Dungeon"),
            opencritic._normalize_match_title("Pokemon Mystery and Dungeon"),
        )

    def test_normalize_match_title_removes_punctuation_only_noise(self) -> None:
        self.assertEqual(
            opencritic._normalize_match_title("Ghost Recon: Breakpoint"),
            opencritic._normalize_match_title("Ghost Recon Breakpoint"),
        )

    def test_extract_edition_tokens_finds_distinguishing_tokens(self) -> None:
        self.assertEqual(
            opencritic._extract_edition_tokens("Resident Evil 4 Remake"),
            {"remake"},
        )

    def test_choose_match_prefers_exact_edition_match(self) -> None:
        candidates = [
            {"title": "Resident Evil 4", "url": "/game/1/re4", "opencritic_id": 1},
            {"title": "Resident Evil 4 Remake", "url": "/game/2/re4-remake", "opencritic_id": 2},
        ]
        match = opencritic._choose_match("Resident Evil 4 Remake", candidates)
        self.assertEqual(match["opencritic_id"], 2)

    def test_choose_match_prefers_exact_base_title_over_variant(self) -> None:
        candidates = [
            {"title": "Persona 3", "url": "/game/1/persona-3", "opencritic_id": 1},
            {"title": "Persona 3 Portable", "url": "/game/2/persona-3-portable", "opencritic_id": 2},
        ]
        match = opencritic._choose_match("Persona 3", candidates)
        self.assertEqual(match["opencritic_id"], 1)

    def test_choose_match_returns_none_for_ambiguous_candidates(self) -> None:
        candidates = [
            {"title": "Persona 3 Portable", "url": "/game/1/persona-3-portable", "opencritic_id": 1},
            {"title": "Persona 3 Reload", "url": "/game/2/persona-3-reload", "opencritic_id": 2},
        ]
        self.assertIsNone(opencritic._choose_match("Persona 3", candidates))


class OpenCriticParserTests(unittest.TestCase):
    def test_candidate_to_export_url_normalizes_relative_urls(self) -> None:
        self.assertEqual(
            opencritic._candidate_to_export_url({"url": "/game/120/portal-2"}),
            "https://opencritic.com/game/120/portal-2/export",
        )

    def test_parse_export_page_extracts_required_fields(self) -> None:
        html = '''
        <script id="__NEXT_DATA__" type="application/json"></script>
        <script>
        window.__STATE__ = {"id":120,"name":"Portal 2","topCriticScore":95,
        "tier":"Mighty","percentRecommended":98,"numReviews":69,
        "url":"https://opencritic.com/game/120/portal-2"};
        </script>
        '''
        record = opencritic._parse_opencritic_record(html, "https://opencritic.com/game/120/portal-2/export")
        self.assertEqual(record["opencritic_id"], 120)
        self.assertEqual(record["opencritic_url"], "https://opencritic.com/game/120/portal-2")
        self.assertEqual(record["opencritic_score"], 95)
        self.assertEqual(record["opencritic_tier"], "Mighty")
        self.assertEqual(record["opencritic_percent_rec"], 98.0)
        self.assertEqual(record["opencritic_num_reviews"], 69)

    def test_parse_opencritic_record_returns_none_when_required_fields_missing(self) -> None:
        self.assertIsNone(opencritic._parse_opencritic_record("<html></html>", "https://opencritic.com/game/1/test/export"))


class OpenCriticDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discover_candidates_returns_primary_results_without_fallback(self) -> None:
        primary = [{"title": "Portal 2", "url": "https://opencritic.com/game/120/portal-2", "opencritic_id": 120}]
        with (
            patch("gamelib_mcp.data.opencritic._discover_from_opencritic", AsyncMock(return_value=primary)),
            patch("gamelib_mcp.data.opencritic._discover_from_search_fallback", AsyncMock()) as fallback,
        ):
            candidates = await opencritic.discover_candidates("Portal 2")

        self.assertEqual(candidates, primary)
        fallback.assert_not_awaited()

    async def test_discover_candidates_uses_search_fallback_when_primary_is_empty(self) -> None:
        with (
            patch("gamelib_mcp.data.opencritic._discover_from_opencritic", AsyncMock(return_value=[])),
            patch(
                "gamelib_mcp.data.opencritic._discover_from_search_fallback",
                AsyncMock(return_value=[{"title": "Portal 2", "url": "https://opencritic.com/game/120/portal-2", "opencritic_id": 120}]),
            ),
        ):
            candidates = await opencritic.discover_candidates("Portal 2")

        self.assertEqual(candidates[0]["opencritic_id"], 120)
