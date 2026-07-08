"""Tests for the purchase-importer framework.

Parsers are tested pure (inline fixture dicts); fetch plumbing is tested with
httpx.MockTransport so no real HTTP ever runs; import_purchases patches the
fetch functions as imported INTO tools.acquisition (this repo's established
patching convention — see tests/test_tools_deals.py).
"""

import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastmcp.exceptions import ToolError

from conftest import ToolDBTestCase, add_platform, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.purchases import PURCHASE_IMPORTERS, PurchaseRecord
from gamelib_mcp.data.purchases import humble as humble_module
from gamelib_mcp.data.purchases import nintendo_ec
from gamelib_mcp.tools import acquisition, admin


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

        self.assertEqual(set(PURCHASE_IMPORTERS), {"eshop", "humble"})
        for module_path, attr in PURCHASE_IMPORTERS.values():
            fn = getattr(importlib.import_module(module_path), attr)
            self.assertTrue(callable(fn))


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


class ImportPurchasesTests(ToolDBTestCase):
    async def test_unknown_source_raises_tool_error(self):
        with self.assertRaisesRegex(ToolError, "Unknown purchase source"):
            await acquisition.import_purchases(sources=["gog"])

    async def test_dry_run_returns_proposed_and_writes_nothing(self):
        gid = await seed_game("Hades")
        await add_platform(gid, "switch2")
        records = [_eshop_record("Hades")]
        skipped = [{"title": "Bad Port", "reason": "transaction_type 'refund' is not a purchase"}]

        with (
            patch.object(
                acquisition, "fetch_eshop_purchases",
                AsyncMock(return_value=(records, skipped)),
            ),
            patch.object(
                acquisition, "fetch_humble_purchases", AsyncMock(return_value=([], [])),
            ),
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
        with (
            patch.object(
                acquisition, "fetch_eshop_purchases",
                AsyncMock(return_value=(records, [])),
            ),
            patch.object(
                acquisition, "fetch_humble_purchases", AsyncMock(return_value=([], [])),
            ),
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
        with (
            patch.object(
                acquisition, "fetch_eshop_purchases",
                AsyncMock(side_effect=RuntimeError("cookies expired")),
            ),
            patch.object(
                acquisition, "fetch_humble_purchases",
                AsyncMock(return_value=(humble_records, [])),
            ),
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
        eshop_mock = AsyncMock(return_value=([], []))
        humble_mock = AsyncMock(return_value=([], []))
        with (
            patch.object(acquisition, "fetch_eshop_purchases", eshop_mock),
            patch.object(acquisition, "fetch_humble_purchases", humble_mock),
        ):
            result = await acquisition.import_purchases(sources=["humble"])

        self.assertEqual(set(result["sources"]), {"humble"})
        eshop_mock.assert_not_awaited()
        humble_mock.assert_awaited_once()


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
