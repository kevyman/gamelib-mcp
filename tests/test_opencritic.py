import unittest

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
