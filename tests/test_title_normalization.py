import unittest

from gamelib_mcp.data.title_normalization import prepare_catalog_title


class TitleNormalizationTests(unittest.TestCase):
    def test_prepare_catalog_title_skips_obvious_non_game_rows(self) -> None:
        self.assertIsNone(prepare_catalog_title("H1Z1: Test Server"))
        self.assertIsNone(prepare_catalog_title("Death Stranding Content"))
        self.assertIsNone(prepare_catalog_title("Q.U.B.E. 2 Soundtrack"))

    def test_prepare_catalog_title_keeps_real_titles_with_overlap_words(self) -> None:
        self.assertEqual(prepare_catalog_title("Content Warning"), "Content Warning")
        self.assertEqual(prepare_catalog_title("DLC Quest"), "DLC Quest")

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
