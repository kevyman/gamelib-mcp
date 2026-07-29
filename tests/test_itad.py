import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gamelib_mcp.data.itad import (
    PriceInfo,
    _best_deal,
    fetch_steam_prices,
    is_itad_configured,
)

_SAMPLE_DEALS = [
    {
        "shop": {"id": 61, "name": "Steam"},
        "price": {"amount": 9.99, "currency": "USD"},
        "regular": {"amount": 19.99, "currency": "USD"},
        "cut": 50,
        "url": "https://example/steam",
    },
    {
        "shop": {"id": 35, "name": "GOG"},
        "price": {"amount": 8.99, "currency": "USD"},
        "regular": {"amount": 19.99, "currency": "USD"},
        "cut": 55,
        "url": "https://example/gog",
    },
]


class ItadBestDealTests(unittest.TestCase):
    def test_best_deal_picks_lowest_price(self) -> None:
        best = _best_deal(_SAMPLE_DEALS)
        self.assertEqual(
            best,
            PriceInfo(
                shop="GOG",
                price=8.99,
                regular_price=19.99,
                cut_pct=55,
                currency="USD",
                deal_url="https://example/gog",
            ),
        )

    def test_best_deal_empty_returns_none(self) -> None:
        self.assertIsNone(_best_deal([]))

    def test_best_deal_skips_malformed_entries(self) -> None:
        result = _best_deal([{"shop": None}, _SAMPLE_DEALS[0]])
        assert result is not None
        self.assertEqual(result.shop, "Steam")

    def test_best_deal_skips_malformed_cut_and_regular_amount(self) -> None:
        malformed_cut = {
            "shop": {"id": 61, "name": "Steam"},
            "price": {"amount": 1.0, "currency": "USD"},
            "cut": "fifty",
        }
        malformed_regular = {
            "shop": {"id": 61, "name": "Steam"},
            "price": {"amount": 2.0, "currency": "USD"},
            "regular": {"amount": "nope"},
        }
        result = _best_deal([malformed_cut, malformed_regular, _SAMPLE_DEALS[0]])
        assert result is not None
        self.assertEqual(result.shop, "Steam")
        self.assertEqual(result.price, 9.99)


class ItadConfiguredTests(unittest.TestCase):
    def test_is_itad_configured_false_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ITAD_API_KEY", None)
            self.assertFalse(is_itad_configured())

    def test_is_itad_configured_true_when_set(self) -> None:
        with patch.dict(os.environ, {"ITAD_API_KEY": "test-key"}):
            self.assertTrue(is_itad_configured())


_SAMPLE_PRICES_RESPONSE = [
    {
        "id": "01234567-89ab-cdef-0123-456789abcdef",
        "deals": _SAMPLE_DEALS,
        "historyLow": {"amount": 5.0, "currency": "USD"},
    }
]


class ItadFetchStreamPricesTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_steam_prices_returns_empty_dict_without_api_key(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ITAD_API_KEY", None)
            result = await fetch_steam_prices([220])
        self.assertEqual(result, {})

    async def test_fetch_steam_prices_maps_appid_to_best_deal(self) -> None:
        lookup_response = Mock(
            status_code=200,
            json=Mock(return_value={"app/220": "01234567-89ab-cdef-0123-456789abcdef", "app/440": None}),
        )
        lookup_response.raise_for_status = Mock(return_value=None)

        prices_response = Mock(status_code=200, json=Mock(return_value=_SAMPLE_PRICES_RESPONSE))
        prices_response.raise_for_status = Mock(return_value=None)

        client = AsyncMock()
        client.post.side_effect = [lookup_response, prices_response]
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch.dict(os.environ, {"ITAD_API_KEY": "test-key"}),
            patch("gamelib_mcp.data.itad.httpx.AsyncClient", return_value=client),
        ):
            result = await fetch_steam_prices([220, 440])

        self.assertIn(220, result)
        self.assertNotIn(440, result)
        self.assertEqual(result[220].shop, "GOG")
        self.assertEqual(result[220].price, 8.99)

        self.assertEqual(client.post.call_count, 2)
        lookup_call, prices_call = client.post.call_args_list

        lookup_url = lookup_call.args[0]
        self.assertIn("/lookup/id/shop/61/v1", lookup_url)
        self.assertEqual(lookup_call.kwargs["json"], ["app/220", "app/440"])

        prices_url = prices_call.args[0]
        self.assertIn("/games/prices/v3", prices_url)
        self.assertEqual(
            prices_call.kwargs["json"], ["01234567-89ab-cdef-0123-456789abcdef"]
        )


if __name__ == "__main__":
    unittest.main()
