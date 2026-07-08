"""Tests for the purchase-importer framework.

Parsers are tested pure (inline fixture dicts); fetch plumbing is tested with
httpx.MockTransport so no real HTTP ever runs; import_purchases patches the
fetch functions as imported INTO tools.acquisition (this repo's established
patching convention — see tests/test_tools_deals.py).
"""

import contextlib
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastmcp.exceptions import ToolError

from conftest import ToolDBTestCase, add_identifier, add_platform, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.purchases import (
    IDENTIFIER_TYPES,
    PURCHASE_IMPORTERS,
    PurchaseRecord,
)
from gamelib_mcp.data.purchases import gog_orders
from gamelib_mcp.data.purchases import humble as humble_module
from gamelib_mcp.data.purchases import nintendo_ec
from gamelib_mcp.data.purchases import steam_history
from gamelib_mcp.data.scrape_validate import FIXTURES_DIR
from gamelib_mcp.tools import acquisition, admin

_FETCHER_ATTRS = (
    "fetch_eshop_purchases",
    "fetch_gog_purchases",
    "fetch_humble_purchases",
    "fetch_steam_purchases",
)


@contextlib.contextmanager
def _patch_fetchers(**overrides):
    """Patch every registered fetcher on tools.acquisition; unnamed ones
    return ([], []) so no real fetcher (filesystem/HTTP) ever runs."""
    with contextlib.ExitStack() as stack:
        mocks = {}
        for attr in _FETCHER_ATTRS:
            mock = overrides.get(attr) or AsyncMock(return_value=([], []))
            stack.enter_context(patch.object(acquisition, attr, mock))
            mocks[attr] = mock
        yield mocks


def _eshop_record(title: str, **overrides) -> PurchaseRecord:
    fields = {
        "title": title,
        "platform": "switch2",
        "purchase_source": "eshop",
        "acquired_at": "2024-03-01",
        "price_paid": 19.99,
        "price_currency": "USD",
    }
    fields.update(overrides)
    return PurchaseRecord(**fields)


async def _acquisition_row(game_id: int, platform: str) -> dict:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone(
            f"""SELECT {', '.join(db_module.ACQUISITION_FIELDS)}
                FROM game_platforms WHERE game_id = ? AND platform = ?""",
            (game_id, platform),
        )
    return dict(row) if row is not None else {}


class PurchaseRegistryTests(unittest.TestCase):
    def test_registry_entries_resolve_to_real_callables(self):
        import importlib

        self.assertEqual(set(PURCHASE_IMPORTERS), {"eshop", "gog", "humble", "steam"})
        for module_path, attr in PURCHASE_IMPORTERS.values():
            fn = getattr(importlib.import_module(module_path), attr)
            self.assertTrue(callable(fn))

    def test_identifier_types_align_with_provider_constants(self):
        # "eshop" is a literal in data/purchases/__init__.py (importing
        # data.nintendo there would drag httpx/bs4/igdb into the package);
        # this guards it against drifting from the real constant.
        from gamelib_mcp.data.nintendo import NINTENDO_TITLE_ID

        self.assertEqual(
            IDENTIFIER_TYPES,
            {
                "eshop": NINTENDO_TITLE_ID,
                "gog": db_module.GOG_PRODUCT_ID,
                "steam": db_module.STEAM_APP_ID,
            },
        )
        # Humble orders carry no store identifiers — deliberately no entry.
        self.assertNotIn("humble", IDENTIFIER_TYPES)


class NintendoEcParserTests(unittest.TestCase):
    def test_parse_transactions_fixture(self):
        transactions = [
            {  # normal paid purchase, amount as object
                "title": "Hades",
                "transaction_type": "purchase",
                "content_type": "title",
                "date": "2024-03-01T12:34:56Z",
                "amount": {"currency": "EUR", "raw_value": "24.99", "formatted_value": "€24.99"},
                "title_id": "70010000012345",
                "some_unknown_field": {"nested": True},
            },
            {  # free download: purchase with no amount block
                "title": "Fortnite",
                "transaction_type": "purchase",
                "content_type": "title",
                "date": "2023-11-20T08:00:00.000+09:00",
            },
            {  # refund → skipped
                "title": "Bad Port",
                "transaction_type": "refund",
                "content_type": "title",
                "date": "2024-01-05T00:00:00Z",
                "amount": {"currency": "EUR", "raw_value": "59.99"},
            },
            {  # consumable → skipped
                "title": "500 Gold Bars",
                "transaction_type": "purchase",
                "content_type": "consumable",
                "date": "2024-02-02T00:00:00Z",
                "amount": {"currency": "EUR", "raw_value": "4.99"},
            },
            {  # DLC with zero raw_value → price 0.0
                "title": "Hades - Artbook DLC",
                "transaction_type": "purchase",
                "content_type": "aoc",
                "date": "2024-03-02T12:00:00Z",
                "amount": {"currency": "EUR", "raw_value": 0},
            },
            {  # missing title → skipped
                "transaction_type": "purchase",
                "content_type": "title",
                "date": "2024-04-01T00:00:00Z",
            },
            {  # missing date → skipped
                "title": "No Date Game",
                "transaction_type": "purchase",
                "content_type": "title",
            },
        ]

        records, skipped = nintendo_ec.parse_transactions(transactions)

        self.assertEqual(len(records), 3)
        hades = records[0]
        self.assertEqual(hades.title, "Hades")
        self.assertEqual(hades.platform, "switch2")
        self.assertEqual(hades.purchase_source, "eshop")
        self.assertEqual(hades.acquired_at, "2024-03-01")
        self.assertEqual(hades.price_paid, 24.99)
        self.assertEqual(hades.price_currency, "EUR")
        self.assertEqual(hades.store_identifier, "70010000012345")

        free = records[1]
        self.assertEqual(free.title, "Fortnite")
        self.assertEqual(free.price_paid, 0.0)
        self.assertEqual(free.acquired_at, "2023-11-20")

        dlc = records[2]
        self.assertEqual(dlc.price_paid, 0.0)
        self.assertEqual(dlc.price_currency, "EUR")

        reasons = [s["reason"] for s in skipped]
        self.assertEqual(len(skipped), 4)
        self.assertIn("transaction_type 'refund' is not a purchase", reasons)
        self.assertIn("content_type 'consumable' is not importable", reasons)
        self.assertIn("missing title", reasons)
        self.assertIn("missing or unparseable date", reasons)
        self.assertEqual(skipped[0]["title"], "Bad Port")

    def test_missing_type_fields_are_tolerated_as_purchase(self):
        records, skipped = nintendo_ec.parse_transactions(
            [{"title": "Minimal", "date": "2022-05-05T00:00:00Z"}]
        )
        self.assertEqual(skipped, [])
        self.assertEqual(records[0].title, "Minimal")
        self.assertEqual(records[0].price_paid, 0.0)

    def test_unparseable_amount_keeps_record_unpriced(self):
        records, skipped = nintendo_ec.parse_transactions(
            [
                {
                    "title": "Weird Amount",
                    "transaction_type": "purchase",
                    "content_type": "title",
                    "date": "2024-06-06T00:00:00Z",
                    "amount": {"currency": "USD", "raw_value": "n/a"},
                }
            ]
        )
        self.assertEqual(skipped, [])
        self.assertIsNone(records[0].price_paid)


class NintendoEcFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_cookie_file_raises_clear_error(self):
        with patch.dict(os.environ, {"NINTENDO_EC_COOKIES_FILE": "/nonexistent/ec.json"}):
            with self.assertRaisesRegex(RuntimeError, "set_nintendo_ec_session"):
                await nintendo_ec.fetch_eshop_purchases()

    def _write_cookies(self, tmp: str) -> str:
        path = os.path.join(tmp, "ec_cookies.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"NASID": "abc"}, f)
        return path

    async def test_pagination_stops_on_short_page(self):
        def _transaction(i: int) -> dict:
            return {
                "title": f"Game {i}",
                "transaction_type": "purchase",
                "content_type": "title",
                "date": "2024-01-02T03:04:05Z",
                "amount": {"currency": "USD", "raw_value": "9.99"},
            }

        requested_offsets: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params["offset"])
            requested_offsets.append(offset)
            page = [_transaction(offset + i) for i in range(min(50, 53 - offset))]
            return httpx.Response(
                200,
                json={"transactions": page, "total": 53},
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"NINTENDO_EC_COOKIES_FILE": path}):
                records, skipped = await nintendo_ec.fetch_eshop_purchases(
                    transport=httpx.MockTransport(handler)
                )

        self.assertEqual(requested_offsets, [0, 50])
        self.assertEqual(len(records), 53)
        self.assertEqual(skipped, [])

    async def test_bare_list_payload_is_accepted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[{
                    "title": "Solo",
                    "transaction_type": "purchase",
                    "content_type": "title",
                    "date": "2024-01-01T00:00:00Z",
                }],
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"NINTENDO_EC_COOKIES_FILE": path}):
                records, _ = await nintendo_ec.fetch_eshop_purchases(
                    transport=httpx.MockTransport(handler)
                )
        self.assertEqual([r.title for r in records], ["Solo"])

    async def test_auth_failure_status_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "forbidden"})

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"NINTENDO_EC_COOKIES_FILE": path}):
                with self.assertRaisesRegex(RuntimeError, "set_nintendo_ec_session"):
                    await nintendo_ec.fetch_eshop_purchases(
                        transport=httpx.MockTransport(handler)
                    )

    async def test_html_login_redirect_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html><body>Log in to your Nintendo Account</body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"NINTENDO_EC_COOKIES_FILE": path}):
                with self.assertRaisesRegex(RuntimeError, "set_nintendo_ec_session"):
                    await nintendo_ec.fetch_eshop_purchases(
                        transport=httpx.MockTransport(handler)
                    )


class HumbleParserTests(unittest.TestCase):
    def test_bundle_order_splits_price_with_remainder_on_last(self):
        order = {
            "product": {"human_name": "Humble Indie Bundle 99", "category": "bundle"},
            "amount_spent": 25.00,
            "currency": "EUR",
            "created": "2023-07-15T18:00:00.000000",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Game A", "key_type": "steam"},
                    {"human_name": "Game B", "key_type": "gog"},
                    {"human_name": "Game C", "key_type": "generic"},
                ]
            },
        }

        records, skipped = humble_module.records_from_order(order)

        self.assertEqual(skipped, [])
        self.assertEqual([r.price_paid for r in records], [8.33, 8.33, 8.34])
        self.assertAlmostEqual(sum(r.price_paid for r in records), 25.00)
        self.assertEqual([r.platform for r in records], ["steam", "gog", "other"])
        self.assertEqual({r.bundle_name for r in records}, {"Humble Indie Bundle 99"})
        self.assertEqual({r.purchase_source for r in records}, {"humble"})
        self.assertEqual({r.price_currency for r in records}, {"EUR"})
        self.assertEqual({r.acquired_at for r in records}, {"2023-07-15"})

    def test_storefront_single_order_full_amount_no_bundle(self):
        order = {
            "product": {"human_name": "Hollow Knight", "category": "storefront"},
            "amount_spent": 14.99,
            "created": "2024-02-10T00:00:00",
            "tpkd_dict": {"all_tpks": [{"human_name": "Hollow Knight", "key_type": "steam"}]},
        }

        records, _ = humble_module.records_from_order(order)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.price_paid, 14.99)
        self.assertIsNone(record.bundle_name)
        self.assertEqual(record.platform, "steam")
        self.assertEqual(record.purchase_source, "humble")
        # No currency in the order → assumed USD.
        self.assertEqual(record.price_currency, "USD")

    def test_subscription_order_maps_to_subscription_source(self):
        order = {
            "product": {"human_name": "March 2024 Humble Choice", "category": "subscriptioncontent"},
            "amount_spent": 11.99,
            "currency": "USD",
            "created": "2024-03-05T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Choice Game 1", "key_type": "steam"},
                    {"human_name": "Choice Game 2", "key_type": "steam"},
                ]
            },
        }

        records, _ = humble_module.records_from_order(order)

        self.assertEqual({r.purchase_source for r in records}, {"subscription"})
        # Not category "bundle" → no bundle_name, but the price still splits.
        self.assertEqual({r.bundle_name for r in records}, {None})
        self.assertEqual([r.price_paid for r in records], [6.0, 5.99])

    def test_zero_amount_order_records_zero_prices(self):
        order = {
            "product": {"human_name": "Freebie", "category": "storefront"},
            "amount_spent": 0,
            "created": "2024-01-01T00:00:00",
            "tpkd_dict": {"all_tpks": [{"human_name": "Freebie", "key_type": "steam"}]},
        }
        records, _ = humble_module.records_from_order(order)
        self.assertEqual(records[0].price_paid, 0.0)

    def test_subproducts_fallback_lands_on_other(self):
        order = {
            "product": {"human_name": "DRM-Free Delight", "category": "storefront"},
            "amount_spent": 5.00,
            "created": "2022-09-09T00:00:00",
            "subproducts": [{"human_name": "DRM-Free Delight"}],
        }
        records, _ = humble_module.records_from_order(order)
        self.assertEqual(records[0].platform, "other")

    def test_order_without_games_is_skipped_with_reason(self):
        order = {
            "product": {"human_name": "Soundtrack Only", "category": "storefront"},
            "amount_spent": 3.00,
            "created": "2022-01-01T00:00:00",
        }
        records, skipped = humble_module.records_from_order(order)
        self.assertEqual(records, [])
        self.assertEqual(skipped[0]["description"], "Soundtrack Only")
        self.assertIn("no game keys or subproducts", skipped[0]["reason"])


class HumbleFetchTests(unittest.IsolatedAsyncioTestCase):
    def _write_cookies(self, tmp: str) -> str:
        path = os.path.join(tmp, "humble_cookies.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"_simpleauth_sess": "sess-token"}, f)
        return path

    async def test_missing_cookie_file_raises_clear_error(self):
        with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": "/nonexistent/humble.json"}):
            with self.assertRaisesRegex(RuntimeError, "set_humble_session"):
                await humble_module.fetch_humble_purchases()

    async def test_auth_failure_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": path}):
                with self.assertRaisesRegex(RuntimeError, "set_humble_session"):
                    await humble_module.fetch_humble_purchases(
                        transport=httpx.MockTransport(handler)
                    )

    async def test_orders_fetched_per_gamekey(self):
        order_detail = {
            "product": {"human_name": "Hollow Knight", "category": "storefront"},
            "amount_spent": 14.99,
            "currency": "USD",
            "created": "2024-02-10T00:00:00",
            "tpkd_dict": {"all_tpks": [{"human_name": "Hollow Knight", "key_type": "steam"}]},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/user/order":
                return httpx.Response(
                    200,
                    json=[{"gamekey": "abc123"}],
                    headers={"content-type": "application/json"},
                )
            self.assertEqual(request.url.path, "/api/v1/order/abc123")
            self.assertEqual(request.url.params["all_tpkds"], "true")
            return httpx.Response(
                200, json=order_detail, headers={"content-type": "application/json"}
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": path}):
                records, skipped = await humble_module.fetch_humble_purchases(
                    transport=httpx.MockTransport(handler)
                )

        self.assertEqual(skipped, [])
        self.assertEqual([r.title for r in records], ["Hollow Knight"])


class GogOrderParserTests(unittest.TestCase):
    def test_price_amount_shape_int_unix_date_and_product_id(self):
        order = {
            "publicId": "order-1",
            "date": 1615464000,  # 2021-03-11 UTC
            "products": [
                {
                    "id": 1207658924,
                    "title": "The Witcher 3: Wild Hunt",
                    "price": {"amount": "9.99", "baseAmount": "39.99", "symbol": "€"},
                },
            ],
        }

        records, skipped = gog_orders.parse_order(order)

        self.assertEqual(skipped, [])
        record = records[0]
        self.assertEqual(record.title, "The Witcher 3: Wild Hunt")
        self.assertEqual(record.platform, "gog")
        self.assertEqual(record.purchase_source, "gog")
        self.assertEqual(record.acquired_at, "2021-03-11")
        self.assertEqual(record.price_paid, 9.99)
        self.assertEqual(record.price_currency, "EUR")
        self.assertEqual(record.store_identifier, "1207658924")
        # GOG orders are carts, never bundles.
        self.assertIsNone(record.bundle_name)

    def test_cash_value_shape_and_string_unix_date(self):
        order = {
            "publicId": "order-2",
            "date": "1391212800",  # 2014-02-01 UTC, string form
            "products": [
                {"id": "42", "title": "Papers, Please", "cashValue": {"amount": 4.99, "symbol": "$"}},
            ],
        }

        records, _ = gog_orders.parse_order(order)

        self.assertEqual(records[0].acquired_at, "2014-02-01")
        self.assertEqual(records[0].price_paid, 4.99)
        self.assertEqual(records[0].price_currency, "USD")

    def test_free_giveaway_product_records_zero_price(self):
        order = {
            "publicId": "order-3",
            "date": 1600000000,
            "currency": "PLN",
            "products": [{"title": "Gwent Giveaway", "price": {"amount": "0.00", "symbol": "zł"}}],
        }

        records, _ = gog_orders.parse_order(order)

        self.assertEqual(records[0].price_paid, 0.0)
        self.assertEqual(records[0].price_currency, "PLN")
        self.assertEqual(records[0].purchase_source, "gog")

    def test_multi_product_order_with_only_total_splits_evenly(self):
        order = {
            "publicId": "order-4",
            "date": 1650000000,
            "total": {"amount": "25.00", "symbol": "$"},
            "products": [
                {"id": 1, "title": "Game A"},
                {"id": 2, "title": "Game B"},
                {"id": 3, "title": "Game C"},
            ],
        }

        records, skipped = gog_orders.parse_order(order)

        self.assertEqual(skipped, [])
        self.assertEqual([r.price_paid for r in records], [8.33, 8.33, 8.34])
        self.assertAlmostEqual(sum(r.price_paid for r in records), 25.00)
        self.assertEqual({r.price_currency for r in records}, {"USD"})

    def test_order_without_products_is_skipped_with_reason(self):
        records, skipped = gog_orders.parse_order({"publicId": "empty-1", "date": 1600000000})
        self.assertEqual(records, [])
        self.assertEqual(skipped[0]["description"], "empty-1")
        self.assertIn("no products", skipped[0]["reason"])


class GogFetchTests(unittest.IsolatedAsyncioTestCase):
    def _write_config(self, tmp: str, tokens: dict | None = None, cookies_txt: str | None = None):
        if tokens is not None:
            with open(os.path.join(tmp, "galaxy_tokens.json"), "w", encoding="utf-8") as f:
                json.dump(tokens, f)
        if cookies_txt is not None:
            with open(os.path.join(tmp, "cookies.txt"), "w", encoding="utf-8") as f:
                f.write(cookies_txt)

    @staticmethod
    def _order(public_id: str, title: str) -> dict:
        return {
            "publicId": public_id,
            "date": 1615464000,
            "products": [{"id": 7, "title": title, "price": {"amount": "9.99", "symbol": "$"}}],
        }

    async def test_missing_session_files_raise_lgogdownloader_advice(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"LGOGDOWNLOADER_CONFIG_PATH": tmp}):
                with self.assertRaisesRegex(RuntimeError, "lgogdownloader --login"):
                    await gog_orders.fetch_gog_purchases()

    async def test_bearer_token_happy_path_paginates(self):
        seen: list[tuple[int, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            seen.append((page, request.headers.get("Authorization")))
            orders = [self._order(f"order-p{page}", f"Game {page}")]
            return httpx.Response(
                200,
                json={"orders": orders, "totalPages": 2},
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            # Nested lgogdownloader token shape: keyed by OAuth client id.
            self._write_config(tmp, tokens={"46899977096215655": {"access_token": "tok123"}})
            with patch.dict(os.environ, {"LGOGDOWNLOADER_CONFIG_PATH": tmp}):
                records, skipped = await gog_orders.fetch_gog_purchases(
                    transport=httpx.MockTransport(handler)
                )

        self.assertEqual(seen, [(1, "Bearer tok123"), (2, "Bearer tok123")])
        self.assertEqual([r.title for r in records], ["Game 1", "Game 2"])
        self.assertEqual(skipped, [])

    async def test_401_with_token_and_no_cookies_raises_advice(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(tmp, tokens={"access_token": "stale"})
            with patch.dict(os.environ, {"LGOGDOWNLOADER_CONFIG_PATH": tmp}):
                with self.assertRaisesRegex(RuntimeError, "lgogdownloader --login"):
                    await gog_orders.fetch_gog_purchases(
                        transport=httpx.MockTransport(handler)
                    )

    async def test_401_with_token_retries_once_with_cookie_jar(self):
        cookies_txt = (
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.gog.com\tTRUE\t/\tTRUE\t1999999999\tgog-al\tcookie-session\n"
            ".gog.com\tTRUE\t/\tFALSE\t1999999999\tcsrf\tabc\n"
            ".example.com\tTRUE\t/\tFALSE\t1999999999\tunrelated\tnope\n"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers.get("Authorization"):
                return httpx.Response(401, json={"error": "unauthorized"})
            cookie_header = request.headers.get("Cookie", "")
            assert "gog-al=cookie-session" in cookie_header
            assert "unrelated" not in cookie_header
            return httpx.Response(
                200,
                json={"orders": [self._order("order-c", "Cookie Game")], "totalPages": 1},
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            self._write_config(
                tmp, tokens={"access_token": "stale"}, cookies_txt=cookies_txt
            )
            with patch.dict(os.environ, {"LGOGDOWNLOADER_CONFIG_PATH": tmp}):
                records, _ = await gog_orders.fetch_gog_purchases(
                    transport=httpx.MockTransport(handler)
                )

        self.assertEqual([r.title for r in records], ["Cookie Game"])


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


class SteamLicensesParserTests(unittest.TestCase):
    def test_parse_licenses_fixture(self):
        licenses = steam_history.parse_licenses(_fixture("steam_licenses_sample.html"))

        self.assertEqual(len(licenses), 4)
        self.assertEqual(
            licenses[0],
            {
                # The "Remove" free-license link div is stripped from the name.
                "name": "Total War: WARHAMMER",
                "date": "2021-03-12",
                "acquisition_type": "Store Purchase",
            },
        )
        self.assertEqual(licenses[1]["name"], "Portal 2 Beta")
        self.assertEqual(licenses[1]["date"], "2020-01-03")
        self.assertEqual(licenses[1]["acquisition_type"], "Complimentary")
        self.assertEqual(licenses[2]["acquisition_type"], "Gift/Guest Pass")
        self.assertEqual(licenses[3]["acquisition_type"], "Retail")

    def test_strip_package_suffix(self):
        self.assertEqual(steam_history.strip_package_suffix("Left 4 Dead 2 Retail"), "Left 4 Dead 2")
        self.assertEqual(steam_history.strip_package_suffix("Portal 2 Beta"), "Portal 2")
        self.assertEqual(steam_history.strip_package_suffix("Half-Life 2"), "Half-Life 2")
        self.assertEqual(
            steam_history.strip_package_suffix("Dota 2 Steam Store and Retail Key"),
            "Dota 2",
        )

    def test_parse_steam_date(self):
        self.assertEqual(steam_history.parse_steam_date("12 Mar, 2021"), "2021-03-12")
        self.assertEqual(steam_history.parse_steam_date("3 Jan, 2020"), "2020-01-03")
        self.assertIsNone(steam_history.parse_steam_date("not a date"))


class SteamHistoryParserTests(unittest.TestCase):
    def test_parse_wallet_history_fixture(self):
        purchases, skipped, cursor = steam_history.parse_wallet_history(
            _fixture("steam_history_sample.html")
        )

        self.assertEqual(len(purchases), 2)
        single = purchases[0]
        self.assertEqual(single["date"], "2021-03-12")
        self.assertEqual(single["items"], ["Total War: WARHAMMER"])
        self.assertEqual(single["total"], 59.99)
        self.assertEqual(single["currency"], "USD")

        cart = purchases[1]
        self.assertEqual(cart["items"], ["Hollow Knight", "Celeste", "Dead Cells"])
        self.assertEqual(cart["total"], 25.00)
        self.assertEqual(cart["currency"], "EUR")

        reasons = " | ".join(s["reason"] for s in skipped)
        self.assertEqual(len(skipped), 4)
        self.assertIn("Refund", reasons)
        self.assertIn("Market Transaction", reasons)
        self.assertIn("In-Game Purchase", reasons)
        self.assertIn("Gift Purchase", reasons)

        self.assertEqual(cursor["wallet_txnid"], "9990001")
        self.assertEqual(cursor["timestamp_newest"], 1615500000)

    def test_price_string_currency_variants(self):
        cases = [
            ("$19.99", 19.99, "USD"),
            ("19,99€", 19.99, "EUR"),
            ("CDN$ 12.00", 12.00, "CAD"),
            ("£3.99", 3.99, "GBP"),
            ("1 234,56 zł", 1234.56, "PLN"),
            ("12.00 USD", 12.00, "USD"),
            ("R$ 1.234,56", 1234.56, "BRL"),
        ]
        for text, amount, currency in cases:
            with self.subTest(text=text):
                self.assertEqual(steam_history.parse_price_string(text), (amount, currency))

    def test_merge_adds_free_and_gift_records_only_when_unmatched(self):
        licenses = steam_history.parse_licenses(_fixture("steam_licenses_sample.html"))
        purchase_records = [
            PurchaseRecord(
                title="Total War: WARHAMMER",
                platform="steam",
                purchase_source="steam",
                acquired_at="2021-03-12",
                price_paid=59.99,
                price_currency="USD",
            )
        ]

        merged = steam_history.merge_license_records(licenses, purchase_records)

        by_title = {r.title: r for r in merged}
        # Suffix-stripped Complimentary license → "free"; Gift/Guest Pass → "gift".
        self.assertEqual(set(by_title), {"Portal 2", "Left 4 Dead 2"})
        self.assertEqual(by_title["Portal 2"].purchase_source, "free")
        self.assertEqual(by_title["Portal 2"].price_paid, 0.0)
        self.assertEqual(by_title["Portal 2"].acquired_at, "2020-01-03")
        self.assertEqual(by_title["Left 4 Dead 2"].purchase_source, "gift")
        self.assertEqual(by_title["Left 4 Dead 2"].price_paid, 0.0)

    def test_merge_skips_license_already_covered_by_history(self):
        licenses = [
            {"name": "Portal 2 Beta", "date": "2020-01-03", "acquisition_type": "Complimentary"}
        ]
        purchase_records = [
            PurchaseRecord(
                title="Portal 2",
                platform="steam",
                purchase_source="steam",
                acquired_at="2020-01-02",
                price_paid=4.99,
                price_currency="USD",
            )
        ]
        self.assertEqual(
            steam_history.merge_license_records(licenses, purchase_records), []
        )


class SteamFetchTests(unittest.IsolatedAsyncioTestCase):
    def _write_cookies(self, tmp: str) -> str:
        path = os.path.join(tmp, "steam_cookies.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"steamLoginSecure": "765-abc", "sessionid": "sess-1"}, f)
        return path

    async def test_missing_cookie_file_raises_clear_error(self):
        with patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": "/nonexistent/steam.json"}):
            with self.assertRaisesRegex(RuntimeError, "set_steam_store_session"):
                await steam_history.fetch_steam_purchases()

    async def test_full_fetch_with_ajax_follow_up_and_license_merge(self):
        from urllib.parse import parse_qs

        ajax_calls: list[dict] = []
        ajax_fragment = (
            '<tr class="wallet_table_row">'
            '<td class="wht_date">20 Nov, 2020</td>'
            '<td class="wht_items">Hades</td>'
            '<td class="wht_type"><div>Purchase</div></td>'
            '<td class="wht_total">$24.99</td>'
            "</tr>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/account/licenses/":
                return httpx.Response(200, text=_fixture("steam_licenses_sample.html"))
            if request.url.path == "/account/history/":
                return httpx.Response(200, text=_fixture("steam_history_sample.html"))
            self.assertEqual(request.url.path, "/account/AjaxLoadMoreHistory/")
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            ajax_calls.append(form)
            return httpx.Response(
                200,
                json={"html": ajax_fragment, "cursor": None},
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": path}):
                records, skipped = await steam_history.fetch_steam_purchases(
                    transport=httpx.MockTransport(handler)
                )

        # One follow-up call, carrying the cursor fields + sessionid.
        self.assertEqual(len(ajax_calls), 1)
        self.assertEqual(ajax_calls[0]["sessionid"], "sess-1")
        self.assertEqual(ajax_calls[0]["cursor[wallet_txnid]"], "9990001")

        by_title = {r.title: r for r in records}
        self.assertEqual(
            set(by_title),
            {
                "Total War: WARHAMMER", "Hollow Knight", "Celeste", "Dead Cells",
                "Hades", "Portal 2", "Left 4 Dead 2",
            },
        )
        # Multi-item cart split, last share absorbs the rounding remainder.
        self.assertEqual(by_title["Hollow Knight"].price_paid, 8.33)
        self.assertEqual(by_title["Dead Cells"].price_paid, 8.34)
        self.assertEqual(by_title["Celeste"].price_currency, "EUR")
        # AJAX-loaded row is a normal purchase record.
        self.assertEqual(by_title["Hades"].price_paid, 24.99)
        self.assertEqual(by_title["Hades"].acquired_at, "2020-11-20")
        # License-only rows: complimentary → free, gift pass → gift, price 0.
        self.assertEqual(by_title["Portal 2"].purchase_source, "free")
        self.assertEqual(by_title["Left 4 Dead 2"].purchase_source, "gift")
        # The "Retail" license is deliberately not imported.
        self.assertNotIn("Counter-Strike: Source", by_title)
        self.assertEqual(len(skipped), 4)

    async def test_login_redirect_raises_session_advice(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=(
                    "<html><body><a href=\"https://store.steampowered.com/login"
                    "/?redir=account\">Sign In</a></body></html>"
                ),
                headers={"content-type": "text/html; charset=utf-8"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": path}):
                with self.assertRaisesRegex(RuntimeError, "set_steam_store_session"):
                    await steam_history.fetch_steam_purchases(
                        transport=httpx.MockTransport(handler)
                    )


class ImportPurchasesTests(ToolDBTestCase):
    async def test_unknown_source_raises_tool_error(self):
        with self.assertRaisesRegex(ToolError, "Unknown purchase source"):
            await acquisition.import_purchases(sources=["origin"])

    async def test_dry_run_returns_proposed_and_writes_nothing(self):
        gid = await seed_game("Hades")
        await add_platform(gid, "switch2")
        records = [_eshop_record("Hades")]
        skipped = [{"title": "Bad Port", "reason": "transaction_type 'refund' is not a purchase"}]

        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, skipped)),
        ):
            result = await acquisition.import_purchases(dry_run=True)

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["status"], "ok")
        self.assertTrue(eshop["dry_run"])
        self.assertFalse(eshop["truncated"])
        self.assertEqual(eshop["fetched"], 1)
        self.assertEqual(eshop["skipped"], skipped)
        self.assertEqual(
            eshop["proposed"],
            [{
                "name": "Hades",
                "platform": "switch2",
                "purchase_source": "eshop",
                "acquired_at": "2024-03-01",
                "price_paid": 19.99,
                "price_currency": "USD",
            }],
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["totals"]["fetched"], 1)

        row = await _acquisition_row(gid, "switch2")
        self.assertIsNone(row["acquired_at"])
        self.assertIsNone(row["price_paid"])
        self.assertIsNone(row["purchase_source"])

    async def test_real_run_fills_matched_and_reports_unmatched(self):
        gid = await seed_game("Hades")
        await add_platform(gid, "switch2")

        records = [_eshop_record("Hades"), _eshop_record("Totally Absent Game")]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases()

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["status"], "ok")
        self.assertEqual(eshop["fetched"], 2)
        self.assertEqual(eshop["filled"], 1)
        self.assertEqual(len(eshop["unmatched"]), 1)
        self.assertEqual(eshop["unmatched"][0]["name"], "Totally Absent Game")
        self.assertEqual(result["totals"]["filled"], 1)
        self.assertEqual(result["totals"]["unmatched"], 1)
        self.assertEqual(result["totals"]["errors"], 0)

        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["acquired_at"], "2024-03-01")
        self.assertEqual(row["price_paid"], 19.99)
        self.assertEqual(row["price_currency"], "USD")
        self.assertEqual(row["purchase_source"], "eshop")

    async def test_identifier_first_match_fills_renamed_library_title(self):
        # The library title differs from the eShop transaction title (renamed/
        # localized), but the seeded nintendo_title_id matches exactly.
        gid = await seed_game("Dragon Quest III HD-2D Remake")
        gpid = await add_platform(gid, "switch2")
        await add_identifier(gpid, "nintendo_title_id", "70010000012345")

        records = [
            _eshop_record(
                "DRAGON QUEST III (localized)",
                store_identifier="70010000012345",
            )
        ]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["status"], "ok")
        self.assertEqual(eshop["filled"], 1)
        self.assertEqual(eshop["unmatched"], [])
        self.assertEqual(result["totals"]["unmatched"], 0)

        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["acquired_at"], "2024-03-01")
        self.assertEqual(row["price_paid"], 19.99)
        self.assertEqual(row["purchase_source"], "eshop")

    async def test_dry_run_proposed_item_carries_identifier_keys(self):
        records = [_eshop_record("Hades", store_identifier="70010000099999")]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(
                sources=["eshop"], dry_run=True
            )

        proposed = result["sources"]["eshop"]["proposed"][0]
        self.assertEqual(proposed["identifier_type"], "nintendo_title_id")
        self.assertEqual(proposed["identifier_value"], "70010000099999")

    async def test_source_error_does_not_block_other_source(self):
        gid = await seed_game("Hollow Knight")
        await add_platform(gid, "steam")

        humble_records = [
            PurchaseRecord(
                title="Hollow Knight",
                platform="steam",
                purchase_source="humble",
                acquired_at="2023-05-05",
                price_paid=9.99,
                price_currency="USD",
            )
        ]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(side_effect=RuntimeError("cookies expired")),
            fetch_humble_purchases=AsyncMock(return_value=(humble_records, [])),
        ):
            result = await acquisition.import_purchases()

        self.assertEqual(result["sources"]["eshop"]["status"], "error")
        self.assertIn("cookies expired", result["sources"]["eshop"]["error"])
        self.assertEqual(result["sources"]["humble"]["status"], "ok")
        self.assertEqual(result["sources"]["humble"]["filled"], 1)
        self.assertEqual(result["totals"]["errors"], 1)

        row = await _acquisition_row(gid, "steam")
        self.assertEqual(row["acquired_at"], "2023-05-05")

    async def test_overwrite_passthrough(self):
        gid = await seed_game("Hades")
        await add_platform(gid, "switch2")
        await acquisition.set_acquisition(
            game_id=gid, platform="switch2", acquired_at="2020-01-01"
        )

        records = [_eshop_record("Hades", acquired_at="2024-03-01")]
        fetch_mock = AsyncMock(return_value=(records, []))
        with patch.object(acquisition, "fetch_eshop_purchases", fetch_mock):
            default_result = await acquisition.import_purchases(sources=["eshop"])
        # Default fill-only mode: acquired_at already set → untouched, but the
        # previously-NULL price/source fields still count the item as filled.
        self.assertEqual(default_result["sources"]["eshop"]["filled"], 1)
        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["acquired_at"], "2020-01-01")

        with patch.object(acquisition, "fetch_eshop_purchases", fetch_mock):
            overwrite_result = await acquisition.import_purchases(
                sources=["eshop"], overwrite=True
            )
        self.assertEqual(overwrite_result["sources"]["eshop"]["applied"], 1)
        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["acquired_at"], "2024-03-01")

    async def test_selected_source_only_runs_that_fetcher(self):
        with _patch_fetchers() as mocks:
            result = await acquisition.import_purchases(sources=["humble"])

        self.assertEqual(set(result["sources"]), {"humble"})
        mocks["fetch_eshop_purchases"].assert_not_awaited()
        mocks["fetch_gog_purchases"].assert_not_awaited()
        mocks["fetch_steam_purchases"].assert_not_awaited()
        mocks["fetch_humble_purchases"].assert_awaited_once()

    async def test_all_four_sources_aggregate_totals(self):
        hades = await seed_game("Hades")
        await add_platform(hades, "switch2")
        hollow = await seed_game("Hollow Knight")
        await add_platform(hollow, "steam")
        witcher = await seed_game("The Witcher 3 Wild Hunt")
        await add_platform(witcher, "gog")
        celeste = await seed_game("Celeste")
        await add_platform(celeste, "steam")

        def _rec(title, platform, source, **overrides):
            fields = {
                "title": title,
                "platform": platform,
                "purchase_source": source,
                "acquired_at": "2024-01-01",
                "price_paid": 10.0,
                "price_currency": "USD",
            }
            fields.update(overrides)
            return PurchaseRecord(**fields)

        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(
                return_value=([_rec("Hades", "switch2", "eshop")], []),
            ),
            fetch_gog_purchases=AsyncMock(
                return_value=(
                    [_rec("The Witcher 3 Wild Hunt", "gog", "gog")],
                    [{"description": "order-1", "reason": "order has no products"}],
                ),
            ),
            fetch_humble_purchases=AsyncMock(
                return_value=([_rec("Hollow Knight", "steam", "humble")], []),
            ),
            fetch_steam_purchases=AsyncMock(
                return_value=(
                    [
                        _rec("Celeste", "steam", "steam"),
                        _rec("Totally Absent Game", "steam", "steam"),
                    ],
                    [],
                ),
            ),
        ):
            result = await acquisition.import_purchases()

        self.assertEqual(set(result["sources"]), {"eshop", "gog", "humble", "steam"})
        for source in ("eshop", "gog", "humble", "steam"):
            self.assertEqual(result["sources"][source]["status"], "ok")
        self.assertEqual(result["totals"]["fetched"], 5)
        self.assertEqual(result["totals"]["filled"], 4)
        self.assertEqual(result["totals"]["unmatched"], 1)
        self.assertEqual(result["totals"]["errors"], 0)
        self.assertEqual(len(result["sources"]["gog"]["skipped"]), 1)

        row = await _acquisition_row(witcher, "gog")
        self.assertEqual(row["purchase_source"], "gog")
        row = await _acquisition_row(celeste, "steam")
        self.assertEqual(row["purchase_source"], "steam")


class SessionCookieToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_humble_session_accepts_json_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "humble.json")
            with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": path}):
                result = await admin.set_humble_session(
                    json.dumps({"_simpleauth_sess": "abc"})
                )
            self.assertEqual(result, {"cookie_count": 1, "path": path})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"_simpleauth_sess": "abc"})

    async def test_set_humble_session_accepts_cookie_editor_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "humble.json")
            with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": path}):
                result = await admin.set_humble_session(
                    json.dumps([
                        {"name": "_simpleauth_sess", "value": "abc", "domain": ".humblebundle.com"},
                        {"name": "csrf_cookie", "value": "xyz"},
                    ])
                )
            self.assertEqual(result["cookie_count"], 2)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(
                    json.load(f), {"_simpleauth_sess": "abc", "csrf_cookie": "xyz"}
                )

    async def test_set_humble_session_rejects_invalid_json(self):
        with self.assertRaisesRegex(ToolError, "Invalid JSON"):
            await admin.set_humble_session("{not json")

    async def test_set_nintendo_ec_session_writes_to_ec_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ec.json")
            with patch.dict(os.environ, {"NINTENDO_EC_COOKIES_FILE": path}):
                result = await admin.set_nintendo_ec_session(
                    json.dumps({"NASID": "token"})
                )
            self.assertEqual(result, {"cookie_count": 1, "path": path})
            # The saved file round-trips through the eShop fetcher's loader.
            with patch.dict(os.environ, {"NINTENDO_EC_COOKIES_FILE": path}):
                self.assertEqual(nintendo_ec._load_ec_cookies(), {"NASID": "token"})

    async def test_set_steam_store_session_writes_to_steam_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "steam.json")
            with patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": path}):
                result = await admin.set_steam_store_session(
                    json.dumps({"steamLoginSecure": "765-abc", "sessionid": "s1"})
                )
            self.assertEqual(result, {"cookie_count": 2, "path": path})
            # The saved file round-trips through the Steam fetcher's loader.
            with patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": path}):
                self.assertEqual(
                    steam_history._load_steam_cookies(),
                    {"steamLoginSecure": "765-abc", "sessionid": "s1"},
                )

    async def test_set_nintendo_session_still_works_after_refactor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nintendo.json")
            with patch.dict(os.environ, {"NINTENDO_COOKIES_FILE": path}):
                result = await admin.set_nintendo_session(
                    json.dumps([{"name": "id_token", "value": "abc"}])
                )
            self.assertEqual(result, {"cookie_count": 1, "path": path})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"id_token": "abc"})


if __name__ == "__main__":
    unittest.main()
