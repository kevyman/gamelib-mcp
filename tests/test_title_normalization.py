import unittest

from gamelib_mcp.data.title_normalization import (
    normalize_search_text,
    prepare_catalog_title,
)


class NormalizeSearchTextTests(unittest.TestCase):
    def test_strips_punctuation_to_token_boundaries(self) -> None:
        self.assertEqual(
            normalize_search_text("Sekiro: Shadows Die Twice"),
            "sekiro shadows die twice",
        )
        self.assertEqual(normalize_search_text("Don't Starve"), "don t starve")
        self.assertEqual(normalize_search_text("Half-Life 2"), "half life 2")

    def test_folds_trademark_glyphs_and_accents(self) -> None:
        self.assertEqual(normalize_search_text("NieR™ Replicant"), "nier replicant")
        self.assertEqual(normalize_search_text("Pokémon"), "pokemon")

    def test_collapses_whitespace_and_casefolds(self) -> None:
        self.assertEqual(normalize_search_text("  HADES   II  "), "hades ii")

    def test_punctuation_only_input_normalizes_to_empty(self) -> None:
        self.assertEqual(normalize_search_text("%"), "")
        self.assertEqual(normalize_search_text("™:!_"), "")


class TitleNormalizationTests(unittest.TestCase):
    def test_prepare_catalog_title_skips_obvious_non_game_rows(self) -> None:
        self.assertIsNone(prepare_catalog_title("H1Z1: Test Server"))
        self.assertIsNone(prepare_catalog_title("Death Stranding Content"))
        self.assertIsNone(prepare_catalog_title("Q.U.B.E. 2 Soundtrack"))
        self.assertIsNone(prepare_catalog_title("Chivalry 2 - Public Testing"))
        self.assertIsNone(prepare_catalog_title("Conan Exiles - Public Beta Client"))
        self.assertIsNone(prepare_catalog_title("Hello Neighbor Demo"))
        self.assertIsNone(prepare_catalog_title("Civilization VI : Australia Civilization & Scenario Pack"))
        self.assertIsNone(prepare_catalog_title("Europa Universalis IV: Catholic Majors Unit Pack"))
        self.assertIsNone(prepare_catalog_title("Cyberpunk 2077 Goodies Collection"))
        self.assertIsNone(prepare_catalog_title("Blink and Dash VFX"))
        self.assertIsNone(prepare_catalog_title("FM21 Editor"))
        self.assertIsNone(prepare_catalog_title("Football Manager 2022 Editor"))
        self.assertIsNone(prepare_catalog_title("Model Builder: Expansion Pack no.1"))

    def test_prepare_catalog_title_keeps_real_titles_with_overlap_words(self) -> None:
        self.assertEqual(prepare_catalog_title("Content Warning"), "Content Warning")
        self.assertEqual(prepare_catalog_title("DLC Quest"), "DLC Quest")
        self.assertEqual(prepare_catalog_title("The Stanley Parable Demo"), "The Stanley Parable Demo")
        self.assertEqual(prepare_catalog_title("Beta Max"), "Beta Max")
        self.assertEqual(prepare_catalog_title("Hogwarts Legacy"), "Hogwarts Legacy")
        self.assertEqual(prepare_catalog_title("Squid Game"), "Squid Game")
        self.assertEqual(prepare_catalog_title("STREET FIGHTER 6"), "STREET FIGHTER 6")
        self.assertEqual(
            prepare_catalog_title("METAL GEAR SOLID - Master Collection Version"),
            "METAL GEAR SOLID - Master Collection Version",
        )

    def test_prepare_catalog_title_normalizes_storefront_variants(self) -> None:
        self.assertEqual(
            prepare_catalog_title("Batman: Arkham Asylum GOTY Edition"),
            "Batman: Arkham Asylum",
        )
        self.assertEqual(
            prepare_catalog_title("Grand Theft Auto V (PlayStation®5)"),
            "Grand Theft Auto V",
        )
        self.assertEqual(
            prepare_catalog_title("Hollow Knight – Nintendo Switch 2 Edition"),
            "Hollow Knight",
        )
        self.assertEqual(
            prepare_catalog_title("LEGO® Star Wars™: The Skywalker Saga"),
            "LEGO Star Wars: The Skywalker Saga",
        )
