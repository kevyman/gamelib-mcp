"""Characterization tests for gamelib_mcp.tools.deals.get_wishlist_deals.

Patches the fetcher/config functions as imported INTO tools.deals (this
repo's established patching convention — see tests/test_tools_admin.py),
not their origin modules (data.itad / data.dekudeals).
"""

from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.itad import PriceInfo
from gamelib_mcp.tools import deals


async def _seed_wishlist(
    game_id: int,
    platform: str,
    *,
    store_identifier: str | None = None,
    source: str = "steam",
) -> None:
    await db_module.upsert_wishlist_entry(
        game_id, platform, source=source, store_identifier=store_identifier
    )


async def _seed_price(
    game_id: int,
    platform: str,
    shop: str,
    price: float,
    *,
    regular_price: float | None = None,
    cut_pct: int | None = None,
    currency: str = "USD",
    deal_url: str = "https://example.com/deal",
    fetched_at: str | None = None,
) -> None:
    await db_module.upsert_game_prices(
        [
            {
                "game_id": game_id,
                "platform": platform,
                "shop": shop,
                "price": price,
                "regular_price": regular_price if regular_price is not None else price,
                "cut_pct": cut_pct,
                "currency": currency,
                "deal_url": deal_url,
            }
        ]
    )
    if fetched_at is not None:
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_prices SET fetched_at = ? "
                "WHERE game_id = ? AND platform = ? AND shop = ?",
                (fetched_at, game_id, platform, shop),
            )
            await db.commit()


class GetWishlistDealsTests(ToolDBTestCase):
    async def test_cached_fresh_path_does_not_call_fetchers(self):
        game_id = await seed_game("Hollow Knight")
        await _seed_wishlist(game_id, "steam", store_identifier="123")
        await _seed_price(game_id, "steam", "steam", 9.99, cut_pct=50)

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ) as mock_itad, patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ) as mock_deku, patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals()

        mock_itad.assert_not_awaited()
        mock_deku.assert_not_awaited()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["deals"][0]["price"], 9.99)
        self.assertEqual(result["deals"][0]["game_id"], game_id)
        self.assertEqual(result["unpriced"], [])

    async def test_refresh_true_calls_fetchers_even_if_cache_fresh(self):
        game_id = await seed_game("Hollow Knight")
        await _seed_wishlist(game_id, "steam", store_identifier="123")
        await _seed_price(game_id, "steam", "steam", 9.99, cut_pct=50)

        switch_id = await seed_game("Pikmin 4")
        await _seed_wishlist(switch_id, "switch2", source="dekudeals")
        await _seed_price(switch_id, "switch2", "dekudeals", 39.99)

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices",
            AsyncMock(return_value={123: PriceInfo("steam", 4.99, 9.99, 50, "USD", "url")}),
        ) as mock_itad, patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices",
            AsyncMock(
                return_value={
                    "Pikmin 4": {
                        "price": 19.99,
                        "regular_price": 39.99,
                        "cut_pct": 50,
                        "currency": "USD",
                        "deal_url": "https://dekudeals.com/x",
                    }
                }
            ),
        ) as mock_deku, patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals(refresh=True)

        mock_itad.assert_awaited_once()
        mock_deku.assert_awaited_once()
        by_game = {d["game_id"]: d for d in result["deals"]}
        self.assertEqual(by_game[game_id]["price"], 4.99)
        self.assertEqual(by_game[switch_id]["price"], 19.99)

    async def test_max_price_and_min_cut_pct_filter_without_moving_to_unpriced(self):
        cheap_id = await seed_game("Cheap Game")
        await _seed_wishlist(cheap_id, "steam", store_identifier="1")
        await _seed_price(cheap_id, "steam", "steam", 5.00, cut_pct=10)

        pricey_id = await seed_game("Pricey Game")
        await _seed_wishlist(pricey_id, "steam", store_identifier="2")
        await _seed_price(pricey_id, "steam", "steam", 59.99, cut_pct=90)

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals(max_price=10.0)

        names = {d["name"] for d in result["deals"]}
        self.assertEqual(names, {"Cheap Game"})
        # Filtered-out game must NOT appear in unpriced (it has a price, just
        # doesn't pass the filter).
        self.assertEqual(result["unpriced"], [])
        self.assertEqual(result["count"], 1)

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals(min_cut_pct=50)

        names = {d["name"] for d in result["deals"]}
        self.assertEqual(names, {"Pricey Game"})
        self.assertEqual(result["unpriced"], [])

    async def test_fetcher_exception_returns_cached_data_with_error(self):
        game_id = await seed_game("Stale Game")
        await _seed_wishlist(game_id, "steam", store_identifier="42")
        await _seed_price(
            game_id,
            "steam",
            "steam",
            14.99,
            cut_pct=25,
            fetched_at="2020-01-01T00:00:00+00:00",  # far past the 12h TTL
        )

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices",
            AsyncMock(side_effect=RuntimeError("boom")),
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals()

        self.assertIn("price_refresh_errors", result)
        self.assertTrue(result["price_refresh_errors"])
        # Cached (stale) price must still be served.
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["deals"][0]["price"], 14.99)

    async def test_unconfigured_itad_leaves_steam_rows_unpriced(self):
        game_id = await seed_game("No ITAD Game")
        await _seed_wishlist(game_id, "steam", store_identifier="7")
        # No cached price at all -> needs refresh, but ITAD is unconfigured.

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ) as mock_itad, patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=False
        ):
            result = await deals.get_wishlist_deals()

        mock_itad.assert_not_awaited()
        self.assertEqual(result["unpriced"], ["No ITAD Game"])
        self.assertEqual(result["itad"], "unconfigured")
        self.assertEqual(result["deals"], [])


if __name__ == "__main__":
    import unittest

    unittest.main()
