"""Fixture-driven tests for the pure scrape parsers.

These are the first tests to exercise the actual HTML/JSON extraction layer
(previously only the orchestration around it was covered). The fixtures live
inside the package (gamelib_mcp/data/scrape_fixtures/) because
scrape_validate.py replays them at runtime to vet proposed config heals; the
FIXTURES dir constant below is the same one the validator uses.
"""

import json
import unittest

from gamelib_mcp.data import backloggd, dekudeals, metacritic, steam_reviews
from gamelib_mcp.data.scrape_config import (
    BackloggdScrapeConfig,
    DekuDealsScrapeConfig,
    MetacriticScrapeConfig,
    SteamReviewsScrapeConfig,
)
from gamelib_mcp.data.scrape_validate import FIXTURES_DIR


def _fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


class BackloggdParserTests(unittest.TestCase):
    def test_parses_fixture_reviews(self):
        reviews = backloggd._parse_page(_fixture_text("backloggd_reviews.html"))

        self.assertEqual(
            [(r["title"], r["score"]) for r in reviews],
            [("Hades", 4.5), ("Celeste", 5.0), ("Anthem", 1.5)],
        )
        self.assertIn("narrative delivery", reviews[0]["text"])

    def test_unrated_card_is_skipped(self):
        reviews = backloggd._parse_page(_fixture_text("backloggd_reviews.html"))
        self.assertNotIn("Unrated Game", [r["title"] for r in reviews])

    def test_broken_card_selector_yields_no_rows(self):
        config = BackloggdScrapeConfig(review_card_selector=".no-such-card")
        self.assertEqual(backloggd._parse_page(_fixture_text("backloggd_reviews.html"), config), [])

    def test_custom_score_regex_is_honored(self):
        # A regex that matches nothing numeric drops every card (score None).
        config = BackloggdScrapeConfig(score_style_regex=r"height:\s*([\d.]+)%")
        self.assertEqual(backloggd._parse_page(_fixture_text("backloggd_reviews.html"), config), [])


class SteamReviewsParserTests(unittest.TestCase):
    def test_parses_fixture_reviews(self):
        reviews = steam_reviews._parse_page(_fixture_text("steam_reviews.html"))

        self.assertEqual(
            [(r["appid"], r["vote"]) for r in reviews],
            [(1145360, 1), (504230, 1), (261570, -1)],
        )
        self.assertIn("roguelite", reviews[0]["text"])

    def test_text_fallback_handles_not_recommended(self):
        reviews = steam_reviews._parse_page(_fixture_text("steam_reviews.html"))
        negative = [r for r in reviews if r["vote"] == -1]
        self.assertEqual(len(negative), 1)
        self.assertEqual(negative[0]["appid"], 261570)

    def test_broken_box_selector_yields_no_rows(self):
        config = SteamReviewsScrapeConfig(review_box_selector=".no-such-box")
        self.assertEqual(steam_reviews._parse_page(_fixture_text("steam_reviews.html"), config), [])


class MetacriticParserTests(unittest.TestCase):
    def test_jsonld_returns_critic_score_not_user_score(self):
        # The fixture lists the user-score block (bestRating=10) FIRST; the
        # bestRating==100 guard must skip it and return the Metascore.
        score = metacritic._extract_score(_fixture_text("metacritic_game.html"))
        self.assertEqual(score, 88)

    def test_css_fallback_avoids_user_score_widget(self):
        score = metacritic._extract_score(_fixture_text("metacritic_game_no_jsonld.html"))
        self.assertEqual(score, 84)

    def test_broken_fallback_selectors_yield_none(self):
        config = MetacriticScrapeConfig(critic_score_selectors=(".no-such-score",))
        score = metacritic._extract_score(
            _fixture_text("metacritic_game_no_jsonld.html"), config
        )
        self.assertIsNone(score)

    def test_candidate_urls_use_config_slug_map(self):
        config = MetacriticScrapeConfig()
        self.assertEqual(
            metacritic._candidate_urls("metal-slug-tactics", "ps5", config),
            [
                "https://www.metacritic.com/game/metal-slug-tactics/?platform=playstation-5",
                "https://www.metacritic.com/game/playstation-5/metal-slug-tactics/",
                "https://www.metacritic.com/game/metal-slug-tactics/",
            ],
        )


class DekuDealsParserTests(unittest.TestCase):
    def test_parses_fixture_wishlist(self):
        payload = json.loads(_fixture_text("dekudeals_wishlist.json"))
        items = dekudeals._parse_wishlist_payload(payload)

        self.assertEqual(
            [i["title"] for i in items],
            ["Pikmin 4", "Metroid Prime 4: Beyond", "Hollow Knight: Silksong"],
        )
        self.assertEqual(items[0]["added_at"], "2026-05-14T18:02:11Z")

    def test_wrong_items_key_yields_no_rows(self):
        payload = json.loads(_fixture_text("dekudeals_wishlist.json"))
        config = DekuDealsScrapeConfig(items_keys=("entries",))
        self.assertEqual(dekudeals._parse_wishlist_payload(payload, config), [])

    def test_plain_title_list_still_parses(self):
        items = dekudeals._parse_wishlist_payload(["Pikmin 4", "Celeste"])
        self.assertEqual(
            items,
            [{"title": "Pikmin 4", "added_at": None}, {"title": "Celeste", "added_at": None}],
        )

    def test_dekudeals_wishlist_price_parse(self):
        html = _fixture_text("dekudeals_wishlist_page.html")
        prices = dekudeals._parse_wishlist_prices(html, DekuDealsScrapeConfig())
        self.assertIn("Pikmin 4", prices)
        entry = prices["Pikmin 4"]
        self.assertAlmostEqual(entry["price"], 59.99)
        self.assertEqual(entry["currency"], "EUR")
        self.assertIsNone(entry["cut_pct"])  # undiscounted in the fixture
        self.assertAlmostEqual(entry["regular_price"], 59.99)
        self.assertTrue(entry["deal_url"].startswith("https://www.dekudeals.com/items/"))

        discounted = prices["Kirby and the Forgotten Land"]
        self.assertAlmostEqual(discounted["price"], 24.99)
        self.assertAlmostEqual(discounted["regular_price"], 49.99)
        self.assertEqual(discounted["cut_pct"], 50)
        self.assertEqual(discounted["currency"], "EUR")

    def test_broken_wishlist_item_selector_yields_no_rows(self):
        html = _fixture_text("dekudeals_wishlist_page.html")
        config = DekuDealsScrapeConfig(wishlist_item_selector=".no-such-card")
        self.assertEqual(dekudeals._parse_wishlist_prices(html, config), {})


if __name__ == "__main__":
    unittest.main()
