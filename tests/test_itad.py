import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gamelib_mcp.data.itad import (
    PriceInfo,
    _best_deal,
    _history_low,
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
        "expiry": "2026-09-15T17:00:00+00:00",
    },
    {
        "shop": {"id": 35, "name": "GOG"},
        "price": {"amount": 8.99, "currency": "USD"},
        "regular": {"amount": 19.99, "currency": "USD"},
        "cut": 55,
        "url": "https://example/gog",
        "expiry": "2026-09-12T17:00:00+00:00",
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
                deal_ends_at="2026-09-12T17:00:00+00:00",
            ),
        )

    def test_best_deal_carries_the_winning_deals_own_expiry(self) -> None:
        # The expiry is a property of the deal that WON, not of the payload:
        # a reader told "ends the 15th" about the GOG price would act on the
        # Steam deal's deadline.
        best = _best_deal(_SAMPLE_DEALS)
        assert best is not None
        self.assertEqual(best.shop, "GOG")
        self.assertEqual(best.deal_ends_at, "2026-09-12T17:00:00+00:00")

    def test_best_deal_open_ended_price_has_no_expiry(self) -> None:
        open_ended = dict(_SAMPLE_DEALS[0])
        open_ended.pop("expiry")
        null_expiry = {**_SAMPLE_DEALS[1], "expiry": None}
        for deal in (open_ended, null_expiry):
            with self.subTest(deal=deal["shop"]["name"]):
                result = _best_deal([deal])
                assert result is not None
                self.assertIsNone(result.deal_ends_at)

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


# ITAD v2 nests the low under three windows; only `all` is read.
_SAMPLE_PRICES_RESPONSE = [
    {
        "id": "01234567-89ab-cdef-0123-456789abcdef",
        "deals": _SAMPLE_DEALS,
        "historyLow": {
            "all": {"amount": 4.99, "amountInt": 499, "currency": "USD"},
            "y1": {"amount": 6.99, "amountInt": 699, "currency": "USD"},
            "m3": {"amount": 8.99, "amountInt": 899, "currency": "USD"},
        },
    }
]

# The same game as ITAD reports it when it has no recorded low at all.
_NO_HISTORY_PRICES_RESPONSE = [
    {"id": "01234567-89ab-cdef-0123-456789abcdef", "deals": _SAMPLE_DEALS}
]


class ItadHistoryLowTests(unittest.TestCase):
    def test_reads_the_all_time_window_only(self) -> None:
        self.assertEqual(
            _history_low(_SAMPLE_PRICES_RESPONSE[0]), (4.99, "USD")
        )

    def test_missing_or_malformed_history_degrades_to_none(self) -> None:
        for entry in (
            {},
            None,
            {"historyLow": None},
            {"historyLow": {"y1": {"amount": 6.99}}},
            {"historyLow": {"all": None}},
            {"historyLow": {"all": {"currency": "USD"}}},
            {"historyLow": {"all": {"amount": "cheap", "currency": "USD"}}},
        ):
            with self.subTest(entry=entry):
                self.assertEqual(_history_low(entry), (None, None))

    def test_history_low_without_a_currency_keeps_the_amount(self) -> None:
        # The amount is still true; the missing currency is what every reader
        # branches on before comparing it to a price.
        self.assertEqual(
            _history_low({"historyLow": {"all": {"amount": 4.99}}}), (4.99, None)
        )


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
        # The low belongs to the GAME and is attached to whichever deal won.
        self.assertEqual(result[220].history_low, 4.99)
        self.assertEqual(result[220].history_low_currency, "USD")
        self.assertEqual(result[220].deal_ends_at, "2026-09-12T17:00:00+00:00")

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


    async def test_fetch_steam_prices_without_history_low_still_prices(self) -> None:
        # No recorded low is normal (a brand-new release, or a shop ITAD has
        # never tracked); the deal must still come back, with the low absent
        # rather than the price dropped.
        lookup_response = Mock(
            status_code=200,
            json=Mock(return_value={"app/220": "01234567-89ab-cdef-0123-456789abcdef"}),
        )
        lookup_response.raise_for_status = Mock(return_value=None)
        prices_response = Mock(
            status_code=200, json=Mock(return_value=_NO_HISTORY_PRICES_RESPONSE)
        )
        prices_response.raise_for_status = Mock(return_value=None)

        client = AsyncMock()
        client.post.side_effect = [lookup_response, prices_response]
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch.dict(os.environ, {"ITAD_API_KEY": "test-key"}),
            patch("gamelib_mcp.data.itad.httpx.AsyncClient", return_value=client),
        ):
            result = await fetch_steam_prices([220])

        self.assertEqual(result[220].price, 8.99)
        self.assertIsNone(result[220].history_low)
        self.assertIsNone(result[220].history_low_currency)
        # The deal's own expiry is independent of the history block.
        self.assertEqual(result[220].deal_ends_at, "2026-09-12T17:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
