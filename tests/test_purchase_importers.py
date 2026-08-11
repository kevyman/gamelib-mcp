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
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import httpx
from conftest import (
    ToolDBTestCase,
    add_game_alias,
    add_identifier,
    add_platform,
    seed_game,
)
from fastmcp.exceptions import ToolError

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import steam_licenses, steam_session
from gamelib_mcp.data.purchases import (
    IDENTIFIER_TYPES,
    PURCHASE_IMPORTERS,
    PurchaseRecord,
    epic_orders,
    gog_orders,
    nintendo_ec,
    steam_history,
)
from gamelib_mcp.data.purchases import humble as humble_module
from gamelib_mcp.data.scrape_validate import FIXTURES_DIR
from gamelib_mcp.tools import acquisition, admin

_FETCHER_ATTRS = (
    "fetch_epic_purchases",
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

        self.assertEqual(
            set(PURCHASE_IMPORTERS), {"epic", "eshop", "gog", "humble", "steam"}
        )
        for module_path, attr in PURCHASE_IMPORTERS.values():
            fn = getattr(importlib.import_module(module_path), attr)
            self.assertTrue(callable(fn))

    def test_identifier_types_align_with_provider_constants(self):
        self.assertEqual(
            IDENTIFIER_TYPES,
            {
                "gog": db_module.GOG_PRODUCT_ID,
                # A Humble key carries `steam_app_id` and no other id, so the
                # source maps to steam_appid; only its steam records ever set
                # store_identifier (enforced below).
                "humble": db_module.STEAM_APP_ID,
                "steam": db_module.STEAM_APP_ID,
            },
        )
        # eShop transactions carry no store identifier (the GraphQL API
        # exposes no product id) — deliberately no entry.
        self.assertNotIn("eshop", IDENTIFIER_TYPES)
        # Epic order items carry an offerId, but the library stores
        # epic_artifact_id — different id spaces, so deliberately no entry.
        self.assertNotIn("epic", IDENTIFIER_TYPES)


def _ec_transactions_fixture() -> list:
    """Inner transactionHistories list from the captured real API response."""
    with open(
        os.path.join(FIXTURES_DIR, "nintendo_ec_transactions.json"), encoding="utf-8"
    ) as f:
        envelope = json.load(f)
    return envelope["data"]["account"]["transactionHistories"]["transactionHistories"]


class NintendoEcParserTests(unittest.TestCase):
    def test_parse_transactions_real_fixture(self):
        # Trimmed from a real ec.nintendo.com GraphQL response: paid app,
        # bundle, free DLC upgrade, a Switch-2 (BEE) title, a REDEEM (voucher),
        # and an ExternalEcTransactionHistory grant carrying two granted items.
        records, skipped = nintendo_ec.parse_transactions(_ec_transactions_fixture())

        # Four PURCHASE rows import; REDEEM + the two grant items are skipped.
        self.assertEqual([r.title for r in records], [
            "Dead Cells",
            "Dead Cells: DLC Bundle",
            "Hollow Knight – Nintendo Switch 2 Edition-upgradepack",
            "Xenoblade Chronicles: Definitive Edition – Nintendo Switch 2 Edition",
        ])

        dead_cells = records[0]
        self.assertEqual(dead_cells.platform, "switch2")
        self.assertEqual(dead_cells.purchase_source, "eshop")
        self.assertEqual(dead_cells.acquired_at, "2026-07-08")
        self.assertEqual(dead_cells.price_paid, 12.49)
        self.assertEqual(dead_cells.price_currency, "EUR")
        # The API exposes no product id, so eShop records match by title only.
        self.assertIsNone(dead_cells.store_identifier)

        # Free DLC upgrade ("€ 0,00") → price 0.0, not None.
        self.assertEqual(records[2].price_paid, 0.0)
        self.assertEqual(records[2].price_currency, "EUR")
        self.assertEqual(records[3].price_paid, 69.99)

        # itemType BUNDLE is flagged so import routes it to bundles_needing_split;
        # single-game rows are not.
        self.assertEqual(
            [r.is_bundle for r in records], [False, True, False, False]
        )

        # Only itemType DLC carries a content_type hint; APPLICATION/BUNDLE
        # rows leave it unset (base games and bundles aren't DLC).
        self.assertEqual(
            [r.content_type for r in records], [None, None, "dlc", None]
        )

        reasons = [s["reason"] for s in skipped]
        titles = [s["title"] for s in skipped]
        self.assertEqual(len(skipped), 3)
        self.assertIn("transaction_type 'REDEEM' is not a purchase", reasons)
        self.assertIn("Mario Kart World", titles)
        # The multi-game external grant is reported item-by-item, never dropped.
        self.assertEqual(
            reasons.count("external eShop grant (not a purchase)"), 2
        )
        self.assertIn("Super Mario 3D World + Bowser's Fury", titles)
        self.assertIn("Luigi's Mansion 3", titles)

    def test_missing_item_type_tolerated_as_application(self):
        records, skipped = nintendo_ec.parse_transactions(
            [{
                "__typename": "TransactionHistory",
                "transactionType": "PURCHASE",
                "title": "Minimal",
                "datetime": "2022-05-05T00:00:00+00:00",
                "amount": {"formattedValue": "€ 5,00"},
            }]
        )
        self.assertEqual(skipped, [])
        self.assertEqual(records[0].title, "Minimal")
        self.assertEqual(records[0].price_paid, 5.0)

    def test_null_amount_on_purchase_keeps_record_unpriced(self):
        records, skipped = nintendo_ec.parse_transactions(
            [{
                "__typename": "TransactionHistory",
                "transactionType": "PURCHASE",
                "itemType": "APPLICATION",
                "title": "No Price",
                "datetime": "2024-06-06T00:00:00+00:00",
                "amount": None,
            }]
        )
        self.assertEqual(skipped, [])
        self.assertIsNone(records[0].price_paid)
        self.assertIsNone(records[0].price_currency)

    def test_content_type_hint_only_for_dlc(self):
        # itemType is compared case-insensitively; content_type should only be
        # set for DLC, never for APPLICATION or BUNDLE rows.
        records, skipped = nintendo_ec.parse_transactions([
            {
                "__typename": "TransactionHistory",
                "transactionType": "PURCHASE",
                "itemType": "APPLICATION",
                "title": "Some Game",
                "datetime": "2024-01-01T00:00:00+00:00",
                "amount": {"formattedValue": "€ 19,99"},
            },
            {
                "__typename": "TransactionHistory",
                "transactionType": "PURCHASE",
                "itemType": "BUNDLE",
                "title": "Some Bundle",
                "datetime": "2024-01-02T00:00:00+00:00",
                "amount": {"formattedValue": "€ 29,99"},
            },
            {
                "__typename": "TransactionHistory",
                "transactionType": "PURCHASE",
                "itemType": "DLC",
                "title": "Some DLC",
                "datetime": "2024-01-03T00:00:00+00:00",
                "amount": {"formattedValue": "€ 4,99"},
            },
            {
                "__typename": "TransactionHistory",
                "transactionType": "PURCHASE",
                "itemType": "dlc",
                "title": "Some Lowercase DLC",
                "datetime": "2024-01-04T00:00:00+00:00",
                "amount": {"formattedValue": "€ 2,99"},
            },
        ])
        self.assertEqual(skipped, [])
        by_title = {r.title: r.content_type for r in records}
        self.assertEqual(by_title["Some Game"], None)
        self.assertEqual(by_title["Some Bundle"], None)
        self.assertEqual(by_title["Some DLC"], "dlc")
        self.assertEqual(by_title["Some Lowercase DLC"], "dlc")

    def test_non_importable_item_type_is_skipped(self):
        records, skipped = nintendo_ec.parse_transactions(
            [{
                "__typename": "TransactionHistory",
                "transactionType": "PURCHASE",
                "itemType": "CONSUMABLE",
                "title": "500 Gold Bars",
                "datetime": "2024-02-02T00:00:00+00:00",
                "amount": {"formattedValue": "€ 4,99"},
            }]
        )
        self.assertEqual(records, [])
        self.assertEqual(skipped[0]["title"], "500 Gold Bars")
        self.assertIn("is not importable", skipped[0]["reason"])

    def test_missing_title_or_date_is_skipped_with_reason(self):
        records, skipped = nintendo_ec.parse_transactions([
            {  # missing title
                "__typename": "TransactionHistory",
                "transactionType": "PURCHASE",
                "datetime": "2024-04-01T00:00:00+00:00",
            },
            {  # missing datetime
                "__typename": "TransactionHistory",
                "transactionType": "PURCHASE",
                "title": "No Date Game",
            },
        ])
        self.assertEqual(records, [])
        reasons = [s["reason"] for s in skipped]
        self.assertIn("missing title", reasons)
        self.assertIn("missing or unparseable date", reasons)

    def test_amount_parsing_across_locales(self):
        cases = {
            "€ 12,49": (12.49, "EUR"),
            "€ 0,00": (0.0, "EUR"),
            "€ 1.234,56": (1234.56, "EUR"),
            "$9.99": (9.99, "USD"),
            "US$ 1,234.56": (1234.56, "USD"),
            "£19.99": (19.99, "GBP"),
            "¥1,480": (1480.0, "JPY"),
        }
        for formatted, expected in cases.items():
            self.assertEqual(
                nintendo_ec._parse_amount({"formattedValue": formatted}),
                expected,
                msg=formatted,
            )
        # Unparseable value keeps the currency but drops the price.
        self.assertEqual(
            nintendo_ec._parse_amount({"formattedValue": "€ N/A"}), (None, "EUR")
        )
        self.assertEqual(nintendo_ec._parse_amount(None), (None, None))
        self.assertEqual(nintendo_ec._parse_amount({}), (None, None))


def _ec_envelope(rows: list, total: int | None = None) -> dict:
    """Wrap transaction rows in the GraphQL response envelope shape."""
    return {"data": {"account": {"transactionHistories": {
        "offsetInfo": {
            "length": len(rows),
            "offset": 0,
            "total": len(rows) if total is None else total,
            "__typename": "OffsetInfo",
        },
        "transactionHistories": rows,
        "__typename": "TransactionHistoriesSegment",
    }, "__typename": "Account"}}}


_SESSION_OK = {"idToken": "tok-xyz", "country": "BE", "localeInfo": {"language": "nl"}}


class NintendoEcFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # No ambient Nintendo Account cookie by default — each test points
        # NINTENDO_COOKIES_FILE at its own temp export.
        env = patch.dict(os.environ, {"NINTENDO_COOKIES_FILE": "/nonexistent/default-acc.json"})
        env.start()
        self.addCleanup(env.stop)

    def test_has_session_cookie_accepts_unsuffixed_and_chunked(self):
        # NextAuth splits a large session cookie into .0/.1 chunks; the jar check
        # in _establish_ec_session must accept either the unsuffixed name or any
        # chunk (browsers send every chunk; the server reassembles them).
        self.assertTrue(
            nintendo_ec._has_session_cookie({nintendo_ec._SESSION_COOKIE: "s"})
        )
        self.assertTrue(
            nintendo_ec._has_session_cookie({
                f"{nintendo_ec._SESSION_COOKIE}.0": "a",
                f"{nintendo_ec._SESSION_COOKIE}.1": "b",
            })
        )
        self.assertFalse(nintendo_ec._has_session_cookie({"NASID": "x"}))

    async def test_fetches_and_parses_real_response(self):
        captured: dict = {}

        def on_graphql(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(
                200,
                json=_ec_envelope(_ec_transactions_fixture()),
                headers={"content-type": "application/graphql-response+json"},
            )

        records, skipped = await self._fetch_via_accounts(
            self._accounts_sso_handler(on_graphql=on_graphql)
        )

        self.assertEqual([r.title for r in records][:1], ["Dead Cells"])
        self.assertEqual(len(records), 4)
        self.assertEqual(len(skipped), 3)

        # The GraphQL step carried the session idToken + pinned query identity.
        req = captured["request"]
        variables = json.loads(req.url.params["variables"])
        self.assertEqual(variables["idToken"], "tok-xyz")
        self.assertEqual(variables["country"], "BE")
        self.assertEqual(variables["language"], "nl")
        extensions = json.loads(req.url.params["extensions"])
        self.assertEqual(
            extensions["persistedQuery"]["sha256Hash"], nintendo_ec._DEFAULT_QUERY_HASH
        )
        self.assertEqual(
            req.headers["x-nintendo-savanna-client-id"], nintendo_ec._DEFAULT_CLIENT_ID
        )

    async def test_pagination_advances_offset(self):
        def _tx(i: int) -> dict:
            return {
                "__typename": "TransactionHistory",
                "transactionType": "PURCHASE",
                "itemType": "APPLICATION",
                "title": f"Game {i}",
                "datetime": "2024-01-02T03:04:05+00:00",
                "amount": {"formattedValue": "€ 9,99"},
            }

        offsets: list[int] = []

        def on_graphql(request: httpx.Request) -> httpx.Response:
            offset = json.loads(request.url.params["variables"])["offset"]
            offsets.append(offset)
            rows = [_tx(offset + i) for i in range(min(50, 53 - offset))]
            return httpx.Response(200, json=_ec_envelope(rows, total=53))

        records, skipped = await self._fetch_via_accounts(
            self._accounts_sso_handler(on_graphql=on_graphql)
        )

        self.assertEqual(offsets, [0, 50])
        self.assertEqual(len(records), 53)
        self.assertEqual(skipped, [])

    async def test_expired_session_without_token_raises(self):
        def on_session(request: httpx.Request) -> httpx.Response:
            # An expired session returns 200 with an empty user / no idToken.
            return httpx.Response(200, json={"user": {}})

        with self.assertRaisesRegex(RuntimeError, r"create_session_ingest_link\b"):
            await self._fetch_via_accounts(
                self._accounts_sso_handler(on_session=on_session)
            )

    async def test_session_auth_failure_status_raises(self):
        def on_session(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "forbidden"})

        with self.assertRaisesRegex(RuntimeError, r"create_session_ingest_link\b"):
            await self._fetch_via_accounts(
                self._accounts_sso_handler(on_session=on_session)
            )

    async def test_html_login_session_raises(self):
        def on_session(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html><body>Log in to your Nintendo Account</body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )

        with self.assertRaisesRegex(RuntimeError, r"create_session_ingest_link\b"):
            await self._fetch_via_accounts(
                self._accounts_sso_handler(on_session=on_session)
            )

    async def test_graphql_error_payload_raises(self):
        def on_graphql(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"errors": [{"message": "INVALID_PARAM"}]})

        with self.assertRaisesRegex(RuntimeError, "GraphQL error"):
            await self._fetch_via_accounts(
                self._accounts_sso_handler(on_graphql=on_graphql)
            )

    # ── Accounts SSO re-auth path (the only, long-lived credential) ──

    def _accounts_sso_handler(self, *, expired: bool = False, on_graphql=None, on_session=None):
        """MockTransport handler replaying the browser's silent OAuth handshake.

        csrf → signin → authorize (302 → callback or /login) → callback (sets a
        fresh session-token) → transactions → session → graphql. ``expired`` makes
        authorize bounce to accounts.nintendo.com/login (dead account session);
        ``on_session``/``on_graphql`` override the session and GraphQL responses.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            host, path = request.url.host, request.url.path
            if host == "ec.nintendo.com" and path == "/api/auth/csrf":
                return httpx.Response(200, json={"csrfToken": "csrf-abc"})
            if host == "ec.nintendo.com" and path == "/api/auth/signin/nintendo":
                return httpx.Response(
                    200,
                    json={"url": "https://accounts.nintendo.com/connect/1.0.0/authorize?client_id=x&state=st"},
                    headers={"set-cookie": "__Secure-next-auth.state=state-jwe; Path=/; Secure; HttpOnly"},
                )
            if host == "accounts.nintendo.com" and path == "/connect/1.0.0/authorize":
                if expired:
                    return httpx.Response(302, headers={"location": "https://accounts.nintendo.com/login"})
                return httpx.Response(
                    302,
                    headers={"location": "https://ec.nintendo.com/api/auth/callback/nintendo?code=c&state=st"},
                )
            if host == "accounts.nintendo.com" and path == "/login":
                return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})
            if host == "ec.nintendo.com" and path == "/api/auth/callback/nintendo":
                return httpx.Response(
                    302,
                    headers={
                        "location": "https://ec.nintendo.com/my/transactions/1",
                        "set-cookie": f"{nintendo_ec._SESSION_COOKIE}=fresh-session; Path=/; Secure; HttpOnly",
                    },
                )
            if host == "ec.nintendo.com" and path == "/my/transactions/1":
                return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})
            if host == "ec.nintendo.com" and path == "/api/auth/session":
                if on_session is not None:
                    return on_session(request)
                return httpx.Response(200, json=_SESSION_OK)
            if on_graphql is not None:
                return on_graphql(request)
            return httpx.Response(200, json=_ec_envelope([]))

        return handler

    def _write_accounts_cookies(self, tmp: str) -> str:
        path = os.path.join(tmp, "accounts_cookies.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"NASID": "s", "NATID": "t", "NAID": "id"}, f)
        return path

    async def _fetch_via_accounts(self, handler):
        """Write an accounts.nintendo.com export, point NINTENDO_COOKIES_FILE at
        it, and run the eShop importer against ``handler``."""
        with tempfile.TemporaryDirectory() as tmp:
            acc = self._write_accounts_cookies(tmp)
            with patch.dict(os.environ, {"NINTENDO_COOKIES_FILE": acc}):
                return await nintendo_ec.fetch_eshop_purchases(
                    transport=httpx.MockTransport(handler)
                )

    async def test_account_session_scoped_off_savanna_and_reaches_authorize(self):
        # The long-lived account cookies must reach the accounts.nintendo.com
        # authorize hop but never the Savanna GraphQL host (nintendo.NET) — a
        # domainless seed would leak them to every request in the shared jar.
        sent: dict[str, str] = {}
        base = self._accounts_sso_handler()

        def handler(request: httpx.Request) -> httpx.Response:
            sent[request.url.host] = request.headers.get("cookie", "")
            return base(request)

        await self._fetch_via_accounts(handler)
        self.assertIn("NASID", sent.get("accounts.nintendo.com", ""))
        self.assertNotIn("NASID", sent.get("wb.lp1.savanna.srv.nintendo.net", ""))

    async def test_accounts_session_expired_raises_reexport(self):
        with self.assertRaisesRegex(RuntimeError, "accounts.nintendo.com"):
            await self._fetch_via_accounts(self._accounts_sso_handler(expired=True))

    async def test_no_session_configured_raises_accounts_hint(self):
        with (
            patch.dict(os.environ, {"NINTENDO_COOKIES_FILE": "/nonexistent/acc.json"}),
            self.assertRaisesRegex(RuntimeError, r"create_session_ingest_link\b"),
        ):
            await nintendo_ec.fetch_eshop_purchases()


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

    def test_key_delivery_suffixes_stripped_from_titles(self):
        order = {
            "product": {"human_name": "Humble Indie Bundle 3", "category": "bundle"},
            "amount_spent": 5.00,
            "created": "2011-07-26T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Dynamite Jack Steam Key", "key_type": "steam"},
                    {"human_name": "Organ Trail: Director's Cut Steam key", "key_type": "steam"},
                    {"human_name": "Galcon Fusion Registration Key", "key_type": "generic"},
                    {"human_name": "Multiwinia Multiplayer Key", "key_type": "generic"},
                    {"human_name": "Frozen Synapse Steam/Multiplayer Key", "key_type": "steam"},
                    {"human_name": "Hero Academy Gold Pack Content Code", "key_type": "generic"},
                    {"human_name": "Destiny 2 - Expansion Pass - Blizzard Key", "key_type": "generic"},
                    {
                        "human_name": "RPG Maker - Adventurer's Journey DLC<br />(DLC Bundle #1)",
                        "key_type": "steam",
                    },
                ]
            },
        }
        records, _ = humble_module.records_from_order(order)
        self.assertEqual(
            [r.title for r in records],
            [
                "Dynamite Jack",
                "Organ Trail: Director's Cut",
                "Galcon Fusion",
                "Multiwinia",
                "Frozen Synapse",
                "Hero Academy Gold Pack",
                "Destiny 2 - Expansion Pass",
                "RPG Maker - Adventurer's Journey DLC (DLC Bundle #1)",
            ],
        )

    def test_ebook_only_order_excluded_not_minted(self):
        # Humble Book Bundles have subproducts (the old "no tpks → skip"
        # assumption doesn't hold) — every novel must be excluded, not become
        # a platform-"other" library game.
        order = {
            "product": {"human_name": "Humble Book Bundle: Epic Fantasy", "category": "bundle"},
            "amount_spent": 16.41,
            "currency": "EUR",
            "created": "2020-05-01T00:00:00",
            "subproducts": [
                {
                    "human_name": "Guns of the Dawn",
                    "downloads": [{"platform": "ebook"}],
                },
                {
                    "human_name": "Empire in Black and Gold",
                    "downloads": [{"platform": "ebook"}, {"platform": "audio"}],
                },
            ],
        }
        records, skipped = humble_module.records_from_order(order)
        self.assertEqual(records, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["description"], "Humble Book Bundle: Epic Fantasy")
        self.assertIn("2 non-game item(s) excluded", skipped[0]["reason"])
        self.assertIn("Guns of the Dawn", skipped[0]["reason"])

    def test_mixed_subproducts_price_splits_across_games_only(self):
        order = {
            "product": {"human_name": "DRM-Free Mixed Bundle", "category": "bundle"},
            "amount_spent": 10.00,
            "created": "2021-03-03T00:00:00",
            "subproducts": [
                {"human_name": "Actual Game", "downloads": [{"platform": "windows"}]},
                {"human_name": "The Making Of", "downloads": [{"platform": "video"}]},
            ],
        }
        records, skipped = humble_module.records_from_order(order)
        self.assertEqual([r.title for r in records], ["Actual Game"])
        # The excluded video gets no share — the full price lands on the game.
        self.assertEqual(records[0].price_paid, 10.00)
        # Single remaining game → not a multi-game bundle.
        self.assertIsNone(records[0].bundle_name)
        self.assertEqual(len(skipped), 1)
        self.assertIn("non-game item(s) excluded", skipped[0]["reason"])

    def test_choice_month_keeps_drm_free_only_subproducts(self):
        # Shape taken from a real August 2019 Humble Monthly order: Steam keys
        # for most of the month, plus a DRM-free-only title ("DON'T GIVE UP")
        # that exists solely as a subproduct. Treating tpks as authoritative
        # dropped it with no record and no skip entry.
        order = {
            "product": {
                "human_name": "August 2019 Humble Monthly",
                "category": "subscriptioncontent",
            },
            "amount_spent": 12.00,
            "currency": "USD",
            "created": "2019-07-18T13:58:15",
            "tpkd_dict": {
                "all_tpks": [
                    {
                        "human_name": "Kingdom Come: Deliverance",
                        "key_type": "steam",
                        "machine_name": "kingdomcome_deliverance_monthly_steam",
                    },
                    {
                        "human_name": "Almost There: The Platformer",
                        "key_type": "steam",
                        "machine_name": "almostthere_theplatformer_monthly_steam",
                    },
                ]
            },
            "subproducts": [
                # The DRM-free half of a key already collected — same game.
                {
                    "human_name": "Almost There: The Platformer",
                    "machine_name": "almostthere_theplatformer",
                    "downloads": [{"platform": "windows"}, {"platform": "linux"}],
                },
                {
                    "human_name": "DON'T GIVE UP",
                    "machine_name": "dontgiveup",
                    "downloads": [{"platform": "windows"}],
                },
            ],
        }

        records, _ = humble_module.records_from_order(order)

        self.assertEqual(
            [(r.title, r.platform) for r in records],
            [
                ("Kingdom Come: Deliverance", "steam"),
                ("Almost There: The Platformer", "steam"),
                # No platform signal on a subproduct — never guessed as steam.
                ("DON'T GIVE UP", "other"),
            ],
        )
        self.assertEqual({r.purchase_source for r in records}, {"subscription"})
        self.assertEqual(sum(r.price_paid for r in records), 12.00)

    def test_same_game_listed_per_download_platform_counted_once(self):
        # A PC-and-Android bundle lists every game twice in subproducts. Each
        # collected item takes a share of the order price, so counting the
        # duplicate halved every real game's recorded spend.
        order = {
            "product": {"human_name": "PC and Android 8", "category": "bundle"},
            "amount_spent": 10.00,
            "currency": "USD",
            "created": "2013-11-19T00:00:00",
            "subproducts": [
                {
                    "human_name": "Gemini Rue",
                    "machine_name": "geminirue",
                    "downloads": [{"platform": "windows"}],
                },
                {
                    "human_name": "Gemini Rue",
                    "machine_name": "geminirue_android",
                    "downloads": [{"platform": "android"}],
                },
                {
                    "human_name": "Little Inferno",
                    "machine_name": "littleinferno",
                    "downloads": [{"platform": "windows"}],
                },
            ],
        }

        records, _ = humble_module.records_from_order(order)

        self.assertEqual([r.title for r in records], ["Gemini Rue", "Little Inferno"])
        self.assertEqual([r.price_paid for r in records], [5.00, 5.00])

    def test_edition_named_key_dedupes_against_its_base_named_twin(self):
        # Humble Choice April 2023: the key is "Life Is Strange 2 Complete
        # Edition" and the second entry is "Life is Strange 2". Neither the
        # title nor the machine_name bridges them ("completeedition" is not a
        # delivery-channel tail), so the month counted the game twice, wrote
        # two acquisition rows for it, and diluted every sibling's share.
        order = {
            "product": {
                "human_name": "April 2023 Humble Choice",
                "category": "subscriptioncontent",
            },
            "amount_spent": 10.75,
            "currency": "USD",
            "created": "2023-04-04T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {
                        "human_name": "Life Is Strange 2 Complete Edition",
                        "key_type": "steam",
                        "machine_name": "lifeisstrange2_completeedition_choice_steam",
                        "redeemed_key_val": "AAAAA-BBBBB-CCCCC",
                    },
                    {
                        "human_name": "Revita",
                        "key_type": "steam",
                        "machine_name": "revita_choice_steam",
                        "redeemed_key_val": "DDDDD-EEEEE-FFFFF",
                    },
                ]
            },
            "subproducts": [
                {
                    "human_name": "Life is Strange 2",
                    "machine_name": "lifeisstrange2",
                    "downloads": [{"platform": "windows"}],
                }
            ],
        }

        records, _ = humble_module.records_from_orders([order])

        # The survivor keeps the key's platform but adopts the BASE title —
        # the un-suffixed name matches a base-named library row at rank 0.
        self.assertEqual(
            [(r.title, r.platform) for r in records],
            [("Life is Strange 2", "steam"), ("Revita", "steam")],
        )
        # Two entries, not three — so the split is 5.38/5.37, not thirds.
        self.assertEqual([r.price_paid for r in records], [5.38, 5.37])
        self.assertEqual(sum(r.price_paid for r in records), 10.75)

    def test_edition_dedupe_keeps_separately_sold_re_releases_apart(self):
        # The edition key must use the CURATED phrase list, not
        # normalize_edition_comparison_title — that one strips a qualifier
        # plus up to two riding words and any <=3 words ending in "edition",
        # which merges pairs that are separate store products the account can
        # own separately. Dropping one would be the same silent loss this
        # union was written to end, with the survivors' prices overstated
        # instead of diluted.
        order = {
            "product": {"human_name": "Devolver Bundle", "category": "bundle"},
            "amount_spent": 10.00,
            "currency": "USD",
            "created": "2015-01-01T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Shadow Warrior", "key_type": "steam",
                     "machine_name": "shadowwarrior_steam", "redeemed_key_val": "AAA"},
                    {"human_name": "Tomb Raider", "key_type": "steam",
                     "machine_name": "tombraider_steam", "redeemed_key_val": "BBB"},
                    {"human_name": "Metro 2033", "key_type": "steam",
                     "machine_name": "metro2033_steam", "redeemed_key_val": "CCC"},
                    {"human_name": "DOOM", "key_type": "steam",
                     "machine_name": "doom_steam", "redeemed_key_val": "DDD"},
                ]
            },
            "subproducts": [
                {"human_name": "Shadow Warrior Classic Redux",
                 "machine_name": "shadowwarrior_classicredux",
                 "downloads": [{"platform": "windows"}]},
                {"human_name": "Tomb Raider: Anniversary",
                 "machine_name": "tombraider_anniversary",
                 "downloads": [{"platform": "windows"}]},
                {"human_name": "Metro 2033 Redux", "machine_name": "metro2033_redux",
                 "downloads": [{"platform": "windows"}]},
                {"human_name": "DOOM Classic Complete",
                 "machine_name": "doom_classiccomplete",
                 "downloads": [{"platform": "windows"}]},
            ],
        }

        records, _ = humble_module.records_from_orders([order])

        self.assertEqual(
            [r.title for r in records],
            [
                "Shadow Warrior", "Tomb Raider", "Metro 2033", "DOOM",
                "Shadow Warrior Classic Redux", "Tomb Raider: Anniversary",
                "Metro 2033 Redux", "DOOM Classic Complete",
            ],
        )

    def test_approximate_fold_is_reported_exact_ones_are_not(self):
        # An approximate fold is the one de-duplication decision that can be
        # wrong, and a wrong one deletes a game and redistributes its money.
        # Exact-title and machine-name folds are unambiguous and routine —
        # reporting those would bury this in hundreds of per-platform dupes.
        order = {
            "product": {"human_name": "Choice", "category": "subscriptioncontent"},
            "amount_spent": 8.00,
            "currency": "USD",
            "created": "2023-04-04T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Life Is Strange 2 Complete Edition",
                     "key_type": "steam", "machine_name": "lis2ce_steam",
                     "redeemed_key_val": "AAA"},
                    {"human_name": "Revita", "key_type": "steam",
                     "machine_name": "revita_steam", "redeemed_key_val": "BBB"},
                ]
            },
            "subproducts": [
                # Approximate: bridged only by the edition key.
                {"human_name": "Life is Strange 2", "machine_name": "lis2",
                 "downloads": [{"platform": "windows"}]},
                # Exact title match — routine, not reported.
                {"human_name": "Revita", "machine_name": "revita",
                 "downloads": [{"platform": "windows"}]},
            ],
        }

        _, skipped = humble_module.records_from_orders([order])

        folds = [s for s in skipped if "folded into an existing one" in s["reason"]]
        self.assertEqual(len(folds), 1)
        # Reported as (dropped SKU title) -> (kept base title).
        self.assertIn(
            "Life Is Strange 2 Complete Edition -> Life is Strange 2",
            folds[0]["reason"],
        )
        self.assertNotIn("Revita", folds[0]["reason"])

    def test_exact_title_match_outranks_an_earlier_edition_match(self):
        # Scanned strongest-key-first, never first-index-wins. The subproduct
        # here matches index 1 EXACTLY by title and index 0 only approximately
        # by the edition key. First-match-by-any-key took index 0 — the
        # unrevealed entry — and replaced it, destroying that key's identity
        # (title AND its Steam appid) and leaving two entries under the same
        # name, which is the duplicate this de-duplication exists to prevent.
        order = {
            "product": {
                "human_name": "April 2023 Humble Choice",
                "category": "subscriptioncontent",
            },
            "amount_spent": 10.00,
            "currency": "USD",
            "created": "2023-04-04T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {
                        "human_name": "Life is Strange 2",
                        "key_type": "steam",
                        "machine_name": "lifeisstrange2_choice_steam",
                        "steam_app_id": "532210",
                        # Unrevealed, so it is the one the slot-replacement
                        # path would overwrite if it were matched.
                    },
                    {
                        "human_name": "Life Is Strange 2 Complete Edition",
                        "key_type": "steam",
                        "machine_name": "lifeisstrange2completeedition_choice_steam",
                        "steam_app_id": "999999",
                        "redeemed_key_val": "AAAAA-BBBBB",
                    },
                ]
            },
            "subproducts": [
                {
                    "human_name": "Life Is Strange 2 Complete Edition",
                    "machine_name": "lifeisstrange2completeedition",
                    "downloads": [{"platform": "windows"}],
                }
            ],
        }

        records, _ = humble_module.records_from_orders([order])

        self.assertEqual(
            [(r.title, r.platform, r.store_identifier) for r in records],
            [
                ("Life is Strange 2", "steam", "532210"),
                ("Life Is Strange 2 Complete Edition", "steam", "999999"),
            ],
        )

    def test_key_and_subproduct_halves_dedupe_by_machine_name(self):
        # Humble renames the DRM-free half often enough that titles alone
        # don't always bridge the two ("reussteam" vs "reus"). The
        # machine_name stem does — but only when everything left over is a
        # delivery-channel tail, so a numbered sequel never gets swallowed.
        order = {
            "product": {"human_name": "Store Purchase", "category": "storefront"},
            "amount_spent": 8.00,
            "currency": "USD",
            "created": "2014-05-05T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Reus", "key_type": "steam", "machine_name": "reussteam"},
                    {"human_name": "Fez II", "key_type": "steam", "machine_name": "fez_2_steam"},
                ]
            },
            "subproducts": [
                {
                    "human_name": "Reus: DRM-Free Edition",
                    "machine_name": "reus",
                    "downloads": [{"platform": "windows"}],
                },
                {
                    "human_name": "Fez",
                    "machine_name": "fez",
                    "downloads": [{"platform": "windows"}],
                },
            ],
        }

        records, _ = humble_module.records_from_order(order)

        self.assertEqual([r.title for r in records], ["Reus", "Fez II", "Fez"])

    def test_steam_key_carries_its_appid_as_the_store_identifier(self):
        # The one exact identity signal in a Humble order. Without it a key
        # titled with an edition suffix matches by name/alias and can land on
        # a same-game edition row that owns the title but not the Steam copy,
        # so the acquisition goes nowhere.
        order = {
            "product": {
                "human_name": "Humble Choice April 2023",
                "category": "subscriptioncontent",
            },
            "amount_spent": 10.75,
            "currency": "USD",
            "created": "2023-04-04T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {
                        "human_name": "DEATH STRANDING DIRECTOR'S CUT",
                        "key_type": "steam",
                        "steam_app_id": "1850570",
                    },
                    # Humble sends "" for a Steam key with no appid recorded.
                    {"human_name": "Revita", "key_type": "steam", "steam_app_id": ""},
                    # The value is meaningless off a Steam key, so it is
                    # never carried — the writer would read it as an appid.
                    {
                        "human_name": "Some GOG Game",
                        "key_type": "gog",
                        "steam_app_id": "999999",
                    },
                ]
            },
        }

        records, _ = humble_module.records_from_order(order)

        self.assertEqual(
            [(r.title, r.platform, r.store_identifier) for r in records],
            [
                ("DEATH STRANDING DIRECTOR'S CUT", "steam", "1850570"),
                ("Revita", "steam", None),
                ("Some GOG Game", "gog", None),
            ],
        )

    def test_only_steam_records_carry_a_store_identifier(self):
        # IDENTIFIER_TYPES maps the whole humble source to steam_appid, so a
        # non-Steam record carrying an identifier would be matched against
        # Steam appids. Nothing may set one.
        order = {
            "product": {"human_name": "Mixed Bundle", "category": "bundle"},
            "amount_spent": 6.00,
            "created": "2020-01-01T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "A Steam Game", "key_type": "steam",
                     "steam_app_id": "123"},
                    {"human_name": "A GOG Game", "key_type": "gog",
                     "steam_app_id": "456"},
                    {"human_name": "An Origin Game", "key_type": "origin",
                     "steam_app_id": "789"},
                ]
            },
            "subproducts": [
                {"human_name": "A DRM-Free Game", "machine_name": "drmfreegame",
                 "downloads": [{"platform": "windows"}]},
            ],
        }

        records, _ = humble_module.records_from_order(order)

        for record in records:
            if record.platform != "steam":
                self.assertIsNone(
                    record.store_identifier, f"{record.title} ({record.platform})"
                )

    def test_same_platform_edition_key_pair_folds_to_one_share(self):
        # The REAL April 2023 shape, which the subproduct fold cannot see:
        # Humble delivered "Life Is Strange 2 Complete Edition" and "Life is
        # Strange 2" as two Steam KEYS. Keys were wholly exempt from
        # de-duplication, so the month split ten ways instead of nine and
        # every game recorded 1.07 instead of its real share. Two
        # same-platform keys can never become two library rows
        # (UNIQUE(game_id, platform)), so folding cannot lose ownership —
        # it can only stop the dilution.
        order = {
            "product": {
                "human_name": "April 2023 Humble Choice",
                "category": "subscriptioncontent",
            },
            "amount_spent": 10.75,
            "currency": "USD",
            "created": "2023-04-04T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Life Is Strange 2 Complete Edition",
                     "key_type": "steam", "machine_name": "lis2ce_choice_steam",
                     "steam_app_id": "532210", "redeemed_key_val": "AAA"},
                    {"human_name": "Life is Strange 2", "key_type": "steam",
                     "machine_name": "lis2_choice_steam"},
                    {"human_name": "Revita", "key_type": "steam",
                     "machine_name": "revita_choice_steam",
                     "redeemed_key_val": "BBB"},
                ]
            },
        }

        records, skipped = humble_module.records_from_orders([order])

        # The BASE title survives whichever key Humble listed first — it is
        # the one that matches a base-named library row at rank 0 — while the
        # appid rides over from the edition-named twin.
        self.assertEqual(
            [(r.title, r.platform, r.store_identifier) for r in records],
            [
                ("Life is Strange 2", "steam", "532210"),
                ("Revita", "steam", None),
            ],
        )
        # Two entries → the split halves, no dilution by the folded twin.
        self.assertEqual([r.price_paid for r in records], [5.38, 5.37])
        folds = [s for s in skipped if "folded into an existing one" in s["reason"]]
        self.assertEqual(len(folds), 1)
        self.assertIn(
            "Life Is Strange 2 Complete Edition -> Life is Strange 2",
            folds[0]["reason"],
        )

    def test_key_fold_merges_revealed_and_appid_from_either_twin(self):
        # The survivor keeps the union of what the pair knew: the appid from
        # whichever key carried it, and revealed if EITHER copy was revealed —
        # one redeemed key is proof the game was granted, however its twin
        # reads. Without the merge, folding into an unrevealed survivor would
        # gate a game the account demonstrably redeemed.
        order = {
            "product": {"human_name": "Choice", "category": "subscriptioncontent"},
            "amount_spent": 6.00,
            "currency": "USD",
            "created": "2023-04-04T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    # Unrevealed, no appid — collected first.
                    {"human_name": "Life Is Strange 2 Complete Edition",
                     "key_type": "steam", "machine_name": "lis2ce_steam"},
                    # Revealed, carries the appid.
                    {"human_name": "Life is Strange 2", "key_type": "steam",
                     "machine_name": "lis2_steam", "steam_app_id": "532210",
                     "redeemed_key_val": "AAA"},
                    # Second revealed key so the reveal gate is armed.
                    {"human_name": "Revita", "key_type": "steam",
                     "machine_name": "revita_steam", "redeemed_key_val": "BBB"},
                ]
            },
        }

        records, _ = humble_module.records_from_orders([order])

        survivor = records[0]
        # Base title adopted from the folded twin — the un-suffixed name is
        # the one the acquisition writer can match when no appid rescues it.
        self.assertEqual(survivor.title, "Life is Strange 2")
        self.assertEqual(survivor.store_identifier, "532210")
        # Revealed via the folded twin → not restricted.
        self.assertTrue(survivor.mint_allowed)

    def test_key_pair_named_like_a_re_release_never_folds(self):
        # Adversarial-review finding: the key-vs-key fold must use the narrow
        # same-product SKU list, not the wider edition list the subproduct
        # fold uses. Remastered / Director's Cut / Anniversary / Legendary /
        # Definitive routinely ship as their own Steam appid — BioShock and
        # BioShock Remastered are two apps — and the appid guard cannot save
        # this: a fifth of real Steam tpks carry no appid, and non-Steam keys
        # never do. Folding here deletes a separately-owned product and
        # overstates every survivor's price.
        order = {
            "product": {"human_name": "2K Bundle", "category": "bundle"},
            "amount_spent": 10.00,
            "currency": "USD",
            "created": "2019-01-01T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    # Deliberately NO steam_app_id on either half of any pair.
                    {"human_name": "BioShock", "key_type": "steam",
                     "machine_name": "bioshock_steam", "redeemed_key_val": "A"},
                    {"human_name": "BioShock Remastered", "key_type": "steam",
                     "machine_name": "bioshockremastered_steam",
                     "redeemed_key_val": "B"},
                    {"human_name": "Death Stranding", "key_type": "steam",
                     "machine_name": "deathstranding_steam",
                     "redeemed_key_val": "C"},
                    {"human_name": "Death Stranding Director's Cut",
                     "key_type": "steam", "machine_name": "dsdc_steam",
                     "redeemed_key_val": "D"},
                ]
            },
        }

        records, skipped = humble_module.records_from_orders([order])

        self.assertEqual(
            [r.title for r in records],
            [
                "BioShock", "BioShock Remastered",
                "Death Stranding", "Death Stranding Director's Cut",
            ],
        )
        self.assertEqual([r.price_paid for r in records], [2.5, 2.5, 2.5, 2.5])
        self.assertEqual(
            [s for s in skipped if "folded into" in s["reason"]], []
        )

    def test_fold_survivor_title_does_not_depend_on_payload_order(self):
        # Adversarial-review finding: keeping "whichever key Humble listed
        # first" made the record's identity depend on payload order — the
        # edition-titled survivor with no appid matches nothing in the
        # library, so the acquisition went from recorded to unrecorded based
        # on ordering alone. The base title must survive both ways.
        def order_with(tpks):
            return {
                "product": {"human_name": "Choice", "category": "subscriptioncontent"},
                "amount_spent": 4.00,
                "currency": "USD",
                "created": "2023-04-04T00:00:00",
                "tpkd_dict": {"all_tpks": tpks},
            }

        ce = {"human_name": "Life Is Strange 2 Complete Edition",
              "key_type": "steam", "machine_name": "lis2ce_steam",
              "redeemed_key_val": "A"}
        base = {"human_name": "Life is Strange 2", "key_type": "steam",
                "machine_name": "lis2_steam", "redeemed_key_val": "B"}

        for tpks in ([ce, base], [base, ce]):
            with self.subTest(first=tpks[0]["human_name"]):
                records, _ = humble_module.records_from_orders([order_with(tpks)])
                self.assertEqual(
                    [(r.title, r.price_paid) for r in records],
                    [("Life is Strange 2", 4.00)],
                )

    def test_unkeyed_junk_tail_is_excluded_not_minted(self):
        # The un-keyed tail of old bundles: builds and marketing artifacts
        # that are not purchasable games. Under create_missing each minted a
        # phantom owned game (170 on the live library), and each ate a share
        # of its order's price split.
        order = {
            "product": {"human_name": "Frozenbyte Bundle", "category": "bundle"},
            "amount_spent": 6.00,
            "currency": "USD",
            "created": "2011-04-01T00:00:00",
            "subproducts": [
                {"human_name": "Trine", "machine_name": "trine",
                 "downloads": [{"platform": "windows"}]},
                {"human_name": "Splot Beta", "machine_name": "splotbeta",
                 "downloads": [{"platform": "windows"}]},
                {"human_name": "City Generator Tech Demo",
                 "machine_name": "citygen", "downloads": [{"platform": "windows"}]},
                {"human_name": "Hollow Knight (Sneak Peek)",
                 "machine_name": "hksneak", "downloads": [{"platform": "windows"}]},
                {"human_name": "Wandersong Sneak Peek (DEMO)",
                 "machine_name": "wandersneak", "downloads": [{"platform": "windows"}]},
                {"human_name": "Dungeon Baller Playtest",
                 "machine_name": "dbplaytest", "downloads": [{"platform": "windows"}]},
                {"human_name": "Darwinia, Multiwinia, DEFCON, Uplink Sourcecodes",
                 "machine_name": "sourcecodes", "downloads": [{"platform": "windows"}]},
            ],
        }

        records, skipped = humble_module.records_from_order(order)

        self.assertEqual([r.title for r in records], ["Trine"])
        # The one real game keeps the whole price — junk takes no share.
        self.assertEqual(records[0].price_paid, 6.00)
        self.assertIn("6 non-game item(s) excluded", skipped[0]["reason"])

    def test_drm_free_build_marker_is_packaging_not_identity(self):
        # "Bleed 2 (DRM-Free)" beside the "Bleed 2" key is the same grant's
        # download half — the marker strips, so exact-title de-duplication
        # catches it. A standalone DRM-free game keeps a matchable name, and
        # a game whose NAME merely starts with "DRM-Free" is untouched.
        order = {
            "product": {"human_name": "Bundle", "category": "bundle"},
            "amount_spent": 9.00,
            "currency": "USD",
            "created": "2018-01-01T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Bleed 2", "key_type": "steam",
                     "machine_name": "bleed2_steam", "redeemed_key_val": "A"},
                ]
            },
            "subproducts": [
                {"human_name": "Bleed 2 (DRM-Free)", "machine_name": "bleed2_drmfree_x",
                 "downloads": [{"platform": "windows"}]},
                {"human_name": "Standalone Game DRM Free Build",
                 "machine_name": "standalone", "downloads": [{"platform": "windows"}]},
                {"human_name": "DRM-Free Delight", "machine_name": "delight",
                 "downloads": [{"platform": "windows"}]},
            ],
        }

        records, _ = humble_module.records_from_order(order)

        self.assertEqual(
            [r.title for r in records],
            ["Bleed 2", "Standalone Game", "DRM-Free Delight"],
        )

    def test_discount_coupons_and_store_notices_take_no_price_share(self):
        # The real April 2023 divisor bug: two coupons ("40% off …",
        # "50% off … DLC Bundle") rode the order as ordinary tpk lines, each
        # eating a full share — so 8 games split $10.75 ten ways. The "50%
        # off … Bundle" line was even diverted to bundles_needing_split as if
        # it were a game bundle. Excluded like any promo, their share
        # redistributes to the real games.
        order = {
            "product": {
                "human_name": "April 2023 Humble Choice",
                "category": "subscriptioncontent",
            },
            "amount_spent": 10.75,
            "currency": "USD",
            "created": "2023-04-04T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Revita", "key_type": "steam",
                     "machine_name": "revita_steam", "redeemed_key_val": "A"},
                    {"human_name": "Rollerdrome", "key_type": "steam",
                     "machine_name": "rollerdrome_steam", "redeemed_key_val": "B"},
                    {"human_name": "40% off Aliens: Fireteam Elite - Pathogen Expansion",
                     "key_type": "generic", "machine_name": "aliens_coupon"},
                    {"human_name": "50% off Monster Camp: Camp Forever DLC Bundle",
                     "key_type": "generic", "machine_name": "monstercamp_coupon"},
                ]
            },
        }

        records, skipped = humble_module.records_from_orders([order])

        self.assertEqual(
            [(r.title, r.price_paid) for r in records],
            [("Revita", 5.38), ("Rollerdrome", 5.37)],
        )
        self.assertFalse(any(r.is_bundle for r in records))
        excluded = next(s for s in skipped if "non-game item(s) excluded" in s["reason"])
        self.assertIn("40% off Aliens", excluded["reason"])

    def test_appid_guard_refusal_is_reported_not_silent(self):
        # When two same-platform keys share a SKU-collapsed title but carry
        # different appids, the guard keeps them apart — correctly, but until
        # now silently, which made "why didn't these fold?" unanswerable from
        # the import report. (Diagnosed on prod: April 2023's LiS2 pair.)
        order = {
            "product": {"human_name": "Choice", "category": "subscriptioncontent"},
            "amount_spent": 9.00,
            "currency": "USD",
            "created": "2023-04-04T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Life is Strange 2", "key_type": "steam",
                     "machine_name": "lis2_steam", "steam_app_id": "532210",
                     "redeemed_key_val": "A"},
                    {"human_name": "Life Is Strange 2 Complete Edition",
                     "key_type": "steam", "machine_name": "lis2ce_steam",
                     "steam_app_id": "999999", "redeemed_key_val": "B"},
                    {"human_name": "Revita", "key_type": "steam",
                     "machine_name": "revita_steam", "redeemed_key_val": "C"},
                ]
            },
        }

        records, skipped = humble_module.records_from_orders([order])

        # Kept apart: three entries, three shares.
        self.assertEqual(len(records), 3)
        apart = [s for s in skipped if "kept apart by differing" in s["reason"]]
        self.assertEqual(len(apart), 1)
        self.assertIn("Life is Strange 2 [532210]", apart[0]["reason"])
        self.assertIn("Life Is Strange 2 Complete Edition [999999]", apart[0]["reason"])

    def test_keys_with_different_appids_never_fold(self):
        # Two Steam appids are Humble's own statement that the keys unlock
        # different store products, whatever the titles suggest.
        order = {
            "product": {"human_name": "Bundle", "category": "bundle"},
            "amount_spent": 8.00,
            "currency": "USD",
            "created": "2020-01-01T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Life is Strange 2", "key_type": "steam",
                     "machine_name": "lis2_steam", "steam_app_id": "532210",
                     "redeemed_key_val": "AAA"},
                    {"human_name": "Life Is Strange 2 Complete Edition",
                     "key_type": "steam", "machine_name": "lis2ce_steam",
                     "steam_app_id": "999999", "redeemed_key_val": "BBB"},
                ]
            },
        }

        records, _ = humble_module.records_from_orders([order])

        self.assertEqual(
            [(r.title, r.store_identifier) for r in records],
            [
                ("Life is Strange 2", "532210"),
                ("Life Is Strange 2 Complete Edition", "999999"),
            ],
        )

    def test_third_key_folds_into_its_platform_twin_not_the_first_match(self):
        # With a Steam+GOG pair already collected, a second Steam key must
        # fold into the STEAM twin even though the GOG entry sits earlier in
        # the list — a platform-blind first-match would refuse the fold (or
        # fold into the wrong grant).
        order = {
            "product": {"human_name": "Two-Store Bundle", "category": "bundle"},
            "amount_spent": 6.00,
            "currency": "USD",
            "created": "2018-01-01T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Torchlight", "key_type": "gog",
                     "machine_name": "torchlight_gog", "redeemed_key_val": "AAA"},
                    {"human_name": "Torchlight", "key_type": "steam",
                     "machine_name": "torchlight_steam", "redeemed_key_val": "BBB"},
                    {"human_name": "Torchlight Complete Edition",
                     "key_type": "steam", "machine_name": "torchlightce_steam",
                     "redeemed_key_val": "CCC"},
                ]
            },
        }

        records, _ = humble_module.records_from_orders([order])

        # The GOG grant survives; the edition-named Steam key folded into the
        # Steam twin, not past it.
        self.assertEqual(
            [(r.title, r.platform) for r in records],
            [("Torchlight", "gog"), ("Torchlight", "steam")],
        )
        self.assertEqual([r.price_paid for r in records], [3.00, 3.00])

    def test_two_key_types_for_one_game_stay_two_records(self):
        # De-duplication is a subproduct-side concern. An order handing out
        # both a Steam and a GOG key for one game granted it on two platforms,
        # and each needs its own acquisition row.
        order = {
            "product": {"human_name": "Two-Store Bundle", "category": "storefront"},
            "amount_spent": 4.00,
            "currency": "USD",
            "created": "2018-06-06T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Torchlight", "key_type": "steam",
                     "machine_name": "torchlight_steam"},
                    {"human_name": "Torchlight", "key_type": "gog",
                     "machine_name": "torchlight_gog"},
                ]
            },
            "subproducts": [
                {
                    "human_name": "Torchlight",
                    "machine_name": "torchlight",
                    "downloads": [{"platform": "windows"}],
                }
            ],
        }

        records, _ = humble_module.records_from_order(order)

        self.assertEqual(
            [(r.title, r.platform) for r in records],
            [("Torchlight", "steam"), ("Torchlight", "gog")],
        )

    def test_launcher_subproduct_is_not_a_game(self):
        order = {
            "product": {"human_name": "Rayman Legends", "category": "storefront"},
            "amount_spent": 15.00,
            "currency": "USD",
            "created": "2015-01-01T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {
                        "human_name": "Rayman Legends",
                        "key_type": "uplay",
                        "machine_name": "raymanlegends_uplay",
                    }
                ]
            },
            "subproducts": [
                {
                    "human_name": "Uplay Client (will download latest version)",
                    "machine_name": "uplayclient",
                    "downloads": [{"platform": "windows"}],
                }
            ],
        }

        records, skipped = humble_module.records_from_order(order)

        self.assertEqual([r.title for r in records], ["Rayman Legends"])
        self.assertEqual(records[0].price_paid, 15.00)
        self.assertIn("Uplay Client", skipped[0]["reason"])

    def test_monthly_plan_payment_funds_next_choice_drop(self):
        plan = {
            "product": {
                "human_name": "Month-to-Month Classic Plan",
                "category": "subscriptionplan",
            },
            "amount_spent": 11.99,
            "currency": "USD",
            "created": "2020-03-01T00:00:00",
        }
        drop = {
            "product": {"human_name": "Humble Choice March 2020", "category": "subscriptioncontent"},
            "amount_spent": 0,
            "currency": "USD",
            "created": "2020-03-27T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Choice Game 1", "key_type": "steam"},
                    {"human_name": "Choice Game 2", "key_type": "steam"},
                    {"human_name": "Choice Game 3", "key_type": "steam"},
                ]
            },
        }
        # Deliberately out of order — attribution must sort chronologically.
        records, skipped = humble_module.records_from_orders([drop, plan])

        self.assertEqual([r.price_paid for r in records], [4.0, 4.0, 3.99])
        self.assertEqual({r.purchase_source for r in records}, {"subscription"})
        plan_notes = [s for s in skipped if "plan payment" in s["reason"]]
        self.assertEqual(len(plan_notes), 1)
        self.assertIn("11.99 USD", plan_notes[0]["reason"])
        self.assertIn("1 month credit", plan_notes[0]["reason"])

    def test_annual_plans_stack_and_leftover_credits_reported(self):
        def annual(created):
            return {
                "product": {"human_name": "Annual Plan", "category": "subscriptionplan"},
                "amount_spent": 93.00,
                "currency": "EUR",
                "created": created,
            }

        def drop(created, title):
            return {
                "product": {"human_name": f"Choice {title}", "category": "subscriptioncontent"},
                "amount_spent": 0,
                "currency": "EUR",
                "created": created,
                "tpkd_dict": {"all_tpks": [{"human_name": title, "key_type": "steam"}]},
            }

        # Two annual plans bought three days apart (a gifted deal), then 13
        # drops: the first 12 consume plan #1's credits, the 13th starts on
        # plan #2, leaving 11 credits unconsumed.
        orders = [annual("2024-11-29T00:00:00"), annual("2024-12-02T00:00:00")]
        orders += [drop(f"2025-{m:02d}-25T00:00:00", f"Month {m}") for m in range(1, 13)]
        orders += [drop("2026-01-25T00:00:00", "Month 13")]

        records, skipped = humble_module.records_from_orders(orders)

        self.assertEqual(len(records), 13)
        self.assertEqual({r.price_paid for r in records}, {7.75})  # 93 / 12
        self.assertEqual({r.price_currency for r in records}, {"EUR"})
        leftovers = [s for s in skipped if s["description"] == "subscription plan credits"]
        self.assertEqual(len(leftovers), 1)
        self.assertIn("11 unconsumed month credit(s) (85.25 EUR)", leftovers[0]["reason"])

    def test_drop_preceding_its_charge_is_still_funded(self):
        # The bundle is revealed the first Tuesday of the month; subscriber
        # auto-billing runs the last Tuesday — the drop's content order
        # routinely PREDATES the charge that pays for it. Attribution must
        # not require credit-before-drop ordering.
        drop = {
            "product": {"human_name": "Humble Choice May 2023", "category": "subscriptioncontent"},
            "amount_spent": 0,
            "created": "2023-05-02T00:00:00",
            "tpkd_dict": {"all_tpks": [{"human_name": "May Game", "key_type": "steam"}]},
        }
        late_charge = {
            "product": {"human_name": "Month-to-Month Classic Plan", "category": "subscriptionplan"},
            "amount_spent": 11.99,
            "currency": "USD",
            "created": "2023-05-30T00:00:00",
        }
        records, _ = humble_module.records_from_orders([drop, late_charge])
        self.assertEqual(records[0].price_paid, 11.99)

    def test_unfunded_drop_stays_zero_and_gift_plan_reported(self):
        gift_plan = {
            "product": {"human_name": "Annual Plan", "category": "subscriptionplan"},
            "amount_spent": 0,
            "currency": "EUR",
            "created": "2024-11-29T00:00:00",
        }
        trial_drop = {
            "product": {"human_name": "Trial Month", "category": "subscriptioncontent"},
            "amount_spent": 0,
            "created": "2019-12-06T00:00:00",
            "tpkd_dict": {"all_tpks": [{"human_name": "Trial Game", "key_type": "steam"}]},
        }
        records, skipped = humble_module.records_from_orders([gift_plan, trial_drop])

        # A zero-amount plan adds no credits, so the drop stays price 0.
        self.assertEqual(records[0].price_paid, 0.0)
        gift_notes = [s for s in skipped if "no recorded payment" in s["reason"]]
        self.assertEqual(len(gift_notes), 1)
        self.assertEqual(gift_notes[0]["description"], "Annual Plan")
        self.assertIn("2024-11-29", gift_notes[0]["reason"])

    @staticmethod
    def _key(name, *, revealed=False, **extra):
        tpk = {"human_name": name, "key_type": "steam", **extra}
        if revealed:
            tpk["redeemed_key_val"] = "AAAAA-BBBBB-CCCCC"
        return tpk

    def test_key_with_no_reported_value_is_restricted_not_dropped(self):
        # Humble reporting no value for a key USUALLY means it was never
        # redeemed — already owned elsewhere, or held to gift — so it must
        # never mint. But it is not proof (Humble stops reporting a value for
        # keys it calls expired, including ones redeemed years earlier), so
        # the record survives with mint_allowed False and can still fill a
        # row an ownership sync confirmed. Every key keeps its share of the
        # split either way.
        order = {
            "product": {"human_name": "Humble Choice", "category": "subscriptioncontent"},
            "amount_spent": 12.00,
            "currency": "USD",
            "created": "2023-04-04T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    self._key("Redeemed Game", revealed=True),
                    self._key("Already Owned Elsewhere"),
                    self._key("Held To Gift"),
                ]
            },
        }

        records, skipped = humble_module.records_from_orders([order])

        self.assertEqual(
            [(r.title, r.mint_allowed) for r in records],
            [
                ("Redeemed Game", True),
                ("Already Owned Elsewhere", False),
                ("Held To Gift", False),
            ],
        )
        # 12.00 split three ways — the redeemed game keeps its own 4.00 share
        # rather than inheriting the two it never paid for separately.
        self.assertEqual([r.price_paid for r in records], [4.00, 4.00, 4.00])
        held = [s for s in skipped if "Humble reports no value for" in s["reason"]]
        self.assertEqual(len(held), 1)
        self.assertIn("2 key(s)", held[0]["reason"])
        self.assertIn("Already Owned Elsewhere", held[0]["reason"])

    def test_expiry_is_not_evidence_a_key_went_unredeemed(self):
        # Death Stranding Director's Cut, Humble Choice April 2023: Steam
        # Support confirmed the activation on 2023-04-05, and Humble STILL
        # reports the key expired — its UI shows "this key has expired and
        # can no longer be redeemed" over a CLAIMED banner. The expiry date
        # is a deadline for redeeming and it passes either way, so reading it
        # as non-redemption discarded a game the account demonstrably owns.
        order = {
            "product": {
                "human_name": "April 2023 Humble Choice",
                "category": "subscriptioncontent",
            },
            "amount_spent": 4.00,
            "currency": "USD",
            "created": "2023-04-04T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    self._key("Live Key", revealed=True),
                    self._key(
                        "DEATH STRANDING DIRECTOR'S CUT",
                        revealed=True,
                        is_expired=True,
                    ),
                ]
            },
        }

        records, _ = humble_module.records_from_orders([order])

        self.assertEqual(
            [(r.title, r.mint_allowed) for r in records],
            [("Live Key", True), ("DEATH STRANDING DIRECTOR'S CUT", True)],
        )

    def test_no_revealed_key_anywhere_means_the_field_is_gone_not_the_keys(self):
        # The failure this guard exists for: Humble is under no obligation to
        # keep sending redeemed_key_val. Reading its disappearance as "every
        # key is unredeemed" would import NOTHING while reporting success —
        # exactly the silent no-op that hid the original gap. With no reveal
        # signal in the whole history, import everything and say so.
        orders = [
            {
                "product": {"human_name": "Bundle A", "category": "bundle"},
                "amount_spent": 5.00,
                "currency": "USD",
                "created": "2020-01-01T00:00:00",
                "tpkd_dict": {"all_tpks": [self._key("Game A"), self._key("Game B")]},
            },
            {
                "product": {"human_name": "Bundle B", "category": "bundle"},
                "amount_spent": 5.00,
                "currency": "USD",
                "created": "2021-01-01T00:00:00",
                "tpkd_dict": {"all_tpks": [self._key("Game C")]},
            },
        ]

        records, skipped = humble_module.records_from_orders(orders)

        self.assertEqual([r.title for r in records], ["Game A", "Game B", "Game C"])
        notes = [s for s in skipped if s["description"] == "unrevealed-key detection"]
        self.assertEqual(len(notes), 1)
        self.assertIn("treating redeemed_key_val as absent", notes[0]["reason"])

    def test_one_revealed_key_anywhere_arms_the_gate_for_the_whole_history(self):
        orders = [
            {
                "product": {"human_name": "Bundle A", "category": "bundle"},
                "amount_spent": 5.00,
                "currency": "USD",
                "created": "2020-01-01T00:00:00",
                "tpkd_dict": {"all_tpks": [self._key("Game A", revealed=True)]},
            },
            {
                "product": {"human_name": "Bundle B", "category": "bundle"},
                "amount_spent": 5.00,
                "currency": "USD",
                "created": "2021-01-01T00:00:00",
                "tpkd_dict": {"all_tpks": [self._key("Game C")]},
            },
        ]

        records, skipped = humble_module.records_from_orders(orders)

        self.assertEqual(
            [(r.title, r.mint_allowed) for r in records],
            [("Game A", True), ("Game C", False)],
        )
        self.assertEqual(
            [s for s in skipped if s["description"] == "unrevealed-key detection"], []
        )

    def test_unrevealed_key_does_not_take_its_drm_free_twin_down_with_it(self):
        # A Monthly/Choice title often ships as BOTH a Steam key and a
        # DRM-free download. If the key was never revealed but the download
        # is there, the game is genuinely owned — so the key must not hold
        # the de-duplication slot and then take the download with it when the
        # gate drops it. The subproduct takes the slot instead: still ONE
        # entry, so the order's price split is unchanged.
        order = {
            "product": {"human_name": "Monthly", "category": "subscriptioncontent"},
            "amount_spent": 10.00,
            "currency": "USD",
            "created": "2019-07-18T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    self._key("Redeemed Game", revealed=True,
                              machine_name="redeemed_monthly_steam"),
                    self._key("Owned Download Only", machine_name="owneddownload_steam"),
                ]
            },
            "subproducts": [
                {
                    "human_name": "Owned Download Only",
                    "machine_name": "owneddownload",
                    "downloads": [{"platform": "windows"}],
                }
            ],
        }

        records, skipped = humble_module.records_from_orders([order])

        self.assertEqual(
            [(r.title, r.platform) for r in records],
            [("Redeemed Game", "steam"), ("Owned Download Only", "other")],
        )
        # Two entries, so the split is still 5.00/5.00 — the unrevealed key
        # did not become an unattributed share.
        self.assertEqual([r.price_paid for r in records], [5.00, 5.00])
        self.assertEqual([s for s in skipped if "unrevealed key(s)" in s["reason"]], [])

    def test_unrevealed_key_with_no_drm_free_twin_stays_restricted(self):
        # The counterpart to the test above: with nothing but the key, there
        # is no download proving ownership, so the record stays restricted
        # and can only ever fill a row a sync already confirmed.
        order = {
            "product": {"human_name": "Monthly", "category": "subscriptioncontent"},
            "amount_spent": 10.00,
            "currency": "USD",
            "created": "2019-07-18T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    self._key("Redeemed Game", revealed=True),
                    self._key("Never Touched"),
                ]
            },
        }

        records, skipped = humble_module.records_from_orders([order])

        self.assertEqual(
            [(r.title, r.mint_allowed) for r in records],
            [("Redeemed Game", True), ("Never Touched", False)],
        )
        held = next(s for s in skipped if "Humble reports no value for" in s["reason"])
        self.assertIn("1 key(s)", held["reason"])

    def test_drm_free_subproducts_are_never_gated_on_a_key_value(self):
        # A subproduct is a direct download, not a key — it has no reveal
        # state and is owned outright.
        order = {
            "product": {"human_name": "Monthly", "category": "subscriptioncontent"},
            "amount_spent": 6.00,
            "currency": "USD",
            "created": "2019-07-18T00:00:00",
            "tpkd_dict": {"all_tpks": [self._key("Keyed Game", revealed=True)]},
            "subproducts": [
                {
                    "human_name": "DRM-Free Game",
                    "machine_name": "drmfreegame",
                    "downloads": [{"platform": "windows"}],
                }
            ],
        }

        records, _ = humble_module.records_from_orders([order])

        self.assertEqual([r.title for r in records], ["Keyed Game", "DRM-Free Game"])

    def test_content_order_without_games_is_not_read_as_a_plan_payment(self):
        # A Choice month whose content we failed to extract must surface as a
        # drop that produced nothing — not as a plan payment. Classifying any
        # game-less subscription order as a plan let an unparsed month push
        # month credits that a DIFFERENT month then consumed, so the money
        # landed on the wrong games and the gap never showed up anywhere.
        unparsed_drop = {
            "product": {
                "human_name": "Humble Choice April 2023",
                "category": "subscriptioncontent",
            },
            "amount_spent": 11.99,
            "currency": "USD",
            "created": "2023-04-04T00:00:00",
        }
        later_drop = {
            "product": {
                "human_name": "Humble Choice May 2023",
                "category": "subscriptioncontent",
            },
            "amount_spent": 0,
            "currency": "USD",
            "created": "2023-05-02T00:00:00",
            "tpkd_dict": {"all_tpks": [{"human_name": "May Game", "key_type": "steam"}]},
        }

        records, skipped = humble_module.records_from_orders(
            [unparsed_drop, later_drop]
        )

        # No phantom credit was minted, so May stays unfunded rather than
        # quietly billing April's charge to it.
        self.assertEqual([(r.title, r.price_paid) for r in records], [("May Game", 0.0)])
        april = [s for s in skipped if s["description"] == "Humble Choice April 2023"]
        self.assertEqual(len(april), 1)
        self.assertIn("no game keys or subproducts", april[0]["reason"])
        self.assertEqual([s for s in skipped if "plan payment" in s["reason"]], [])

    def test_pre_choice_monthly_orders_keep_their_own_price(self):
        # Old Humble Monthly charged the content order itself — a plan credit
        # must not override a drop that already carries a payment.
        plan = {
            "product": {"human_name": "Month-to-Month Classic Plan", "category": "subscriptionplan"},
            "amount_spent": 12.00,
            "currency": "USD",
            "created": "2016-02-01T00:00:00",
        }
        paid_drop = {
            "product": {"human_name": "Humble Monthly March 2016", "category": "subscriptioncontent"},
            "amount_spent": 12.00,
            "currency": "USD",
            "created": "2016-03-04T00:00:00",
            "tpkd_dict": {"all_tpks": [{"human_name": "Monthly Game", "key_type": "steam"}]},
        }
        records, skipped = humble_module.records_from_orders([plan, paid_drop])

        self.assertEqual(records[0].price_paid, 12.00)
        # The plan credit stays queued and is reported, not silently dropped.
        leftovers = [s for s in skipped if s["description"] == "subscription plan credits"]
        self.assertEqual(len(leftovers), 1)

    def test_addon_named_keys_carry_content_type_hint(self):
        order = {
            "product": {"human_name": "Board Game Night", "category": "bundle"},
            "amount_spent": 9.00,
            "created": "2019-06-01T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Ticket to Ride", "key_type": "steam"},
                    {"human_name": "Ticket to Ride Europe DLC", "key_type": "steam"},
                    {"human_name": "Ticket to Ride Original Soundtrack", "key_type": "steam"},
                    {"human_name": "Crusader Kings II - Norse Unit Pack", "key_type": "steam"},
                ]
            },
        }
        records, _ = humble_module.records_from_order(order)
        self.assertEqual(
            [r.content_type for r in records], [None, "dlc", "unknown_addon", "dlc"]
        )

    def test_title_override_dlc_gets_content_hint(self):
        # "Outlast: Whistleblower" has no addon-ish word — the shared
        # title-override table supplies the nested hint instead.
        order = {
            "product": {"human_name": "Humble Monthly", "category": "subscriptioncontent"},
            "amount_spent": 12.00,
            "created": "2016-05-06T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Outlast: Whistleblower", "key_type": "steam"},
                ]
            },
        }
        records, _ = humble_module.records_from_order(order)
        self.assertEqual(records[0].content_type, "dlc")

    def test_promo_tpks_excluded_and_price_redistributes(self):
        order = {
            "product": {"human_name": "Humble Monthly February 2016", "category": "subscriptioncontent"},
            "amount_spent": 12.00,
            "created": "2016-02-05T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Real Game", "key_type": "steam"},
                    {"human_name": "Tomb Raider Monthly Outlast Deluxe Edition Cross-Promo", "key_type": "steam"},
                    {"human_name": "Tropico 3 Free Key Expiration", "key_type": "steam"},
                    {
                        "human_name": "The Elder Scrolls: Legends: 2 Card Packs (Skyrim) 1 Event Ticket",
                        "key_type": "steam",
                    },
                ]
            },
        }
        records, skipped = humble_module.records_from_order(order)
        self.assertEqual([r.title for r in records], ["Real Game"])
        self.assertEqual(records[0].price_paid, 12.00)
        self.assertEqual(len(skipped), 1)
        self.assertIn("3 non-game item(s) excluded", skipped[0]["reason"])

    def test_enumerated_multi_game_sku_flagged_as_bundle(self):
        order = {
            "product": {"human_name": "THQ Bundle", "category": "bundle"},
            "amount_spent": 5.00,
            "created": "2012-12-01T00:00:00",
            "tpkd_dict": {
                "all_tpks": [
                    {
                        "human_name": (
                            "Supreme Commander, Supreme Commander: Forged Alliance, "
                            "The Guild 2, and Red Faction: Armageddon"
                        ),
                        "key_type": "steam",
                    },
                    {"human_name": "Warhammer 40,000: Dawn of War", "key_type": "steam"},
                ]
            },
        }
        records, _ = humble_module.records_from_order(order)
        self.assertEqual([r.is_bundle for r in records], [True, False])


class HumbleFetchTests(unittest.IsolatedAsyncioTestCase):
    def _write_cookies(self, tmp: str) -> str:
        path = os.path.join(tmp, "humble_cookies.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"_simpleauth_sess": "sess-token"}, f)
        return path

    async def test_missing_cookie_file_raises_clear_error(self):
        with (
            patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": "/nonexistent/humble.json"}),
            self.assertRaisesRegex(RuntimeError, "create_session_ingest_link"),
        ):
            await humble_module.fetch_humble_purchases()

    async def test_auth_failure_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with (
                patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": path}),
                self.assertRaisesRegex(RuntimeError, "create_session_ingest_link"),
            ):
                await humble_module.fetch_humble_purchases(
                    transport=httpx.MockTransport(handler)
                )

    @staticmethod
    def _order_detail(title: str) -> dict:
        return {
            "product": {"human_name": title, "category": "storefront"},
            "amount_spent": 14.99,
            "currency": "USD",
            "created": "2024-02-10T00:00:00",
            "tpkd_dict": {"all_tpks": [{"human_name": title, "key_type": "steam"}]},
        }

    async def test_orders_fetched_in_chunked_bulk_requests(self):
        # One request per order does not scale with account age: several
        # hundred orders at one round trip each ran past the caller's timeout
        # long before the import finished, so the source stopped completing at
        # all. Detail comes from the bulk endpoint, chunked.
        gamekeys = [f"key{n:03d}" for n in range(40)]
        bulk_requests: list[list[str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/user/order":
                return httpx.Response(
                    200,
                    json=[{"gamekey": key} for key in gamekeys],
                    headers={"content-type": "application/json"},
                )
            self.assertEqual(request.url.path, "/api/v1/orders")
            self.assertEqual(request.url.params["all_tpkds"], "true")
            requested = request.url.params.get_list("gamekeys")
            bulk_requests.append(requested)
            return httpx.Response(
                200,
                json={key: self._order_detail(f"Game {key}") for key in requested},
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": path}):
                records, _ = await humble_module.fetch_humble_purchases(
                    transport=httpx.MockTransport(handler)
                )

        # 40 orders in 2 requests, not 40.
        self.assertEqual([len(chunk) for chunk in bulk_requests], [35, 5])
        self.assertEqual(len(records), 40)

    async def test_null_bulk_entry_is_skipped_not_fatal(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/user/order":
                return httpx.Response(
                    200,
                    json=[{"gamekey": "abc123"}, {"gamekey": "gone456"}],
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                200,
                json={"abc123": self._order_detail("Hollow Knight"), "gone456": None},
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": path}):
                records, _ = await humble_module.fetch_humble_purchases(
                    transport=httpx.MockTransport(handler)
                )

        self.assertEqual([r.title for r in records], ["Hollow Knight"])

    async def test_bulk_endpoint_failure_falls_back_to_per_order_fetch(self):
        # The bulk endpoint is undocumented. If Humble changes it, a slow
        # import still beats a source that imports nothing.
        per_order_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/user/order":
                return httpx.Response(
                    200,
                    json=[{"gamekey": "abc123"}, {"gamekey": "def456"}],
                    headers={"content-type": "application/json"},
                )
            if request.url.path == "/api/v1/orders":
                return httpx.Response(500, text="nope")
            per_order_paths.append(request.url.path)
            title = "Hollow Knight" if request.url.path.endswith("abc123") else "Celeste"
            return httpx.Response(
                200,
                json=self._order_detail(title),
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": path}):
                records, _ = await humble_module.fetch_humble_purchases(
                    transport=httpx.MockTransport(handler)
                )

        self.assertEqual(
            per_order_paths, ["/api/v1/order/abc123", "/api/v1/order/def456"]
        )
        self.assertEqual([r.title for r in records], ["Hollow Knight", "Celeste"])

    async def test_unexpected_bulk_payload_shape_reaches_the_fallback(self):
        # The fallback exists for exactly this: the undocumented endpoint
        # answering 200 with valid JSON of a different shape. Validating that
        # with an error the handler re-raises would take the whole import
        # down instead of retrying the way that still works.
        per_order_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/user/order":
                return httpx.Response(
                    200,
                    json=[{"gamekey": "abc123"}],
                    headers={"content-type": "application/json"},
                )
            if request.url.path == "/api/v1/orders":
                # A list, not the gamekey-keyed object.
                return httpx.Response(
                    200, json=[], headers={"content-type": "application/json"}
                )
            per_order_paths.append(request.url.path)
            return httpx.Response(
                200,
                json=self._order_detail("Hollow Knight"),
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": path}):
                records, _ = await humble_module.fetch_humble_purchases(
                    transport=httpx.MockTransport(handler)
                )

        self.assertEqual(per_order_paths, ["/api/v1/order/abc123"])
        self.assertEqual([r.title for r in records], ["Hollow Knight"])

    async def test_stale_session_is_not_retried_per_order(self):
        # An auth failure means the session expired. Falling back would be
        # several hundred more ways to fail with the same error.
        attempted: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/user/order":
                return httpx.Response(
                    200,
                    json=[{"gamekey": "abc123"}],
                    headers={"content-type": "application/json"},
                )
            attempted.append(request.url.path)
            return httpx.Response(403, json={"error": "forbidden"})

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with (
                patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": path}),
                self.assertRaisesRegex(RuntimeError, "create_session_ingest_link"),
            ):
                await humble_module.fetch_humble_purchases(
                    transport=httpx.MockTransport(handler)
                )

        self.assertEqual(attempted, ["/api/v1/orders"])


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
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"LGOGDOWNLOADER_CONFIG_PATH": tmp}),
            self.assertRaisesRegex(RuntimeError, "lgogdownloader --login"),
        ):
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
            with (
                patch.dict(os.environ, {"LGOGDOWNLOADER_CONFIG_PATH": tmp}),
                self.assertRaisesRegex(RuntimeError, "lgogdownloader --login"),
            ):
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


class EpicOrderParserTests(unittest.TestCase):
    def test_completed_order_with_formatted_amount_and_offer_id(self):
        order = {
            "orderId": "F2001",
            "createdAtMillis": 1615464000000,  # 2021-03-11 UTC
            "orderStatus": "COMPLETED",
            "orderType": "PURCHASE",
            "items": [
                {"description": "Control", "amount": "$19.99", "offerId": "offer-abc"},
            ],
        }

        records, skipped = epic_orders.parse_order(order)

        self.assertEqual(skipped, [])
        record = records[0]
        self.assertEqual(record.title, "Control")
        self.assertEqual(record.platform, "epic")
        self.assertEqual(record.purchase_source, "epic")
        self.assertEqual(record.acquired_at, "2021-03-11")
        self.assertEqual(record.price_paid, 19.99)
        self.assertEqual(record.price_currency, "USD")
        self.assertEqual(record.store_identifier, "offer-abc")
        self.assertIsNone(record.content_type)
        self.assertFalse(record.is_bundle)

    def test_live_v2_minor_units_with_sibling_currency(self):
        # The live payload: integer ISO-4217 minor units + sibling ISO code,
        # order total as an {amount, currency} object (719 ≡ €7.19).
        order = {
            "orderId": "F-v2",
            "createdAtMillis": 1784227739149,
            "orderType": "PURCHASE",
            "total": {"amount": 719, "currency": "EUR"},
            "items": [
                {"description": "Luto", "amount": 719, "currency": "EUR",
                 "offerId": "offer-x", "quantity": 1},
            ],
        }

        records, skipped = epic_orders.parse_order(order)

        self.assertEqual(skipped, [])
        self.assertEqual(records[0].price_paid, 7.19)
        self.assertEqual(records[0].price_currency, "EUR")
        self.assertEqual(records[0].purchase_source, "epic")

    def test_zero_decimal_currency_integer_is_face_value(self):
        order = {
            "orderId": "F-jpy",
            "total": {"amount": 1980, "currency": "JPY"},
            "items": [{"description": "Ikaruga", "amount": 1980, "currency": "JPY"}],
        }

        records, _ = epic_orders.parse_order(order)

        self.assertEqual(records[0].price_paid, 1980.0)
        self.assertEqual(records[0].price_currency, "JPY")

    def test_giveaway_claim_lists_full_price_but_costs_nothing(self):
        # Weekly-freebie claims carry the full LIST price per item with a 100%
        # promotions discount and total 0 — they must import as free, not as
        # €56.76 of phantom spend (the exact real-payload trap, 2026-07-20).
        order = {
            "orderId": "F-claim",
            "createdAtMillis": 1784227739149,
            "orderType": "PURCHASE",
            "total": {"amount": 0, "currency": "EUR"},
            "subtotal": {"amount": 0, "currency": "EUR"},
            "promotions": [{"amount": 5676, "currency": "EUR"}],
            "items": [
                {"description": "The Ouroboros King", "amount": 719, "currency": "EUR"},
                {"description": "Echo Generation: Midnight Edition", "amount": 2239, "currency": "EUR"},
                {"description": "Luto", "amount": 1999, "currency": "EUR"},
                {"description": "Princess Farmer", "amount": 719, "currency": "EUR"},
            ],
        }

        records, skipped = epic_orders.parse_order(order)

        self.assertEqual(skipped, [])
        self.assertEqual(len(records), 4)
        for record in records:
            self.assertEqual(record.price_paid, 0.0)
            self.assertEqual(record.purchase_source, "free")

    def test_coupon_order_allocates_paid_total_by_list_price(self):
        # €10 + €30 list, €20 paid → €5/€15, cent-preserving.
        order = {
            "orderId": "F-coupon",
            "total": {"amount": 2000, "currency": "EUR"},
            "items": [
                {"description": "Small Game", "amount": 1000, "currency": "EUR"},
                {"description": "Big Game", "amount": 3000, "currency": "EUR"},
            ],
        }

        records, _ = epic_orders.parse_order(order)

        self.assertEqual([r.price_paid for r in records], [5.0, 15.0])
        self.assertEqual([r.purchase_source for r in records], ["epic", "epic"])
        self.assertEqual([r.price_currency for r in records], ["EUR", "EUR"])

    def test_bare_number_without_sibling_currency_stays_face_value(self):
        # Legacy defensive behavior: no ISO code anywhere → no minor-unit
        # reinterpretation.
        order = {
            "orderId": "F-bare",
            "items": [{"description": "Mystery", "amount": 19.99}],
        }

        records, _ = epic_orders.parse_order(order)

        self.assertEqual(records[0].price_paid, 19.99)

    def test_decimal_comma_locale_and_symbol_currency(self):
        order = {
            "orderId": "F2002",
            "createdAtMillis": "1600000000000",  # string millis
            "items": [{"description": "Alan Wake", "amount": "R$ 29,99"}],
        }

        records, _ = epic_orders.parse_order(order)

        self.assertEqual(records[0].price_paid, 29.99)
        self.assertEqual(records[0].price_currency, "BRL")
        self.assertEqual(records[0].acquired_at, "2020-09-13")

    def test_thousands_separators_both_locales(self):
        for formatted, expected in (("1.234,56 zł", 1234.56), ("$1,234.56", 1234.56)):
            records, _ = epic_orders.parse_order(
                {"orderId": "F-big", "items": [{"description": "Big Cart", "amount": formatted}]}
            )
            self.assertEqual(records[0].price_paid, expected)

    def test_explicit_order_currency_outranks_ambiguous_dollar_symbol(self):
        # "$" formats USD, CAD, AUD, … — the order's ISO field is authoritative.
        order = {
            "orderId": "F-cad",
            "createdAtMillis": 1650000000000,
            "currency": "CAD",
            "items": [{"description": "Control", "amount": "$19.99"}],
        }

        records, _ = epic_orders.parse_order(order)

        self.assertEqual(records[0].price_currency, "CAD")

    def test_multi_character_dollar_symbols_map_without_iso_field(self):
        for formatted, expected in (
            ("CA$19.99", "CAD"),
            ("A$29.99", "AUD"),
            ("NZ$9.99", "NZD"),
        ):
            records, _ = epic_orders.parse_order(
                {"orderId": "F-sym", "items": [{"description": "Game", "amount": formatted}]}
            )
            self.assertEqual(records[0].price_currency, expected)

    def test_in_game_currency_packs_are_skipped_not_minted(self):
        order = {
            "orderId": "F-vbucks",
            "createdAtMillis": 1650000000000,
            "orderStatus": "COMPLETED",
            "items": [
                {"description": "1,000 V-Bucks", "amount": "$7.99"},
                {"description": "Rocket League® - Credits x1100", "amount": "$9.99"},
                {"description": "2,800 Apex Coins", "amount": "$19.99"},
                {"description": "Alan Wake 2", "amount": "$49.99"},
            ],
        }

        records, skipped = epic_orders.parse_order(order)

        self.assertEqual([r.title for r in records], ["Alan Wake 2"])
        self.assertEqual(len(skipped), 3)
        for entry in skipped:
            self.assertIn("consumable", entry["reason"])

    def test_currency_noun_without_a_count_is_not_a_consumable(self):
        # Games legitimately named with currency nouns must not be filtered.
        order = {
            "orderId": "F-notcons",
            "items": [{"description": "Coin Crypt", "amount": "$4.99"}],
        }

        records, skipped = epic_orders.parse_order(order)

        self.assertEqual([r.title for r in records], ["Coin Crypt"])
        self.assertEqual(skipped, [])

    def test_zero_amount_is_a_free_giveaway_claim(self):
        order = {
            "orderId": "F2003",
            "createdAtMillis": 1650000000000,
            "orderStatus": "COMPLETED",
            "items": [{"description": "Death Stranding", "amount": "$0.00"}],
        }

        records, _ = epic_orders.parse_order(order)

        self.assertEqual(records[0].purchase_source, "free")
        self.assertEqual(records[0].price_paid, 0.0)

    def test_refund_and_incomplete_orders_skip_with_reason(self):
        refund = {
            "orderId": "R1",
            "orderType": "REFUND",
            "items": [{"description": "Control", "amount": "$19.99"}],
        }
        pending = {
            "orderId": "P1",
            "orderStatus": "PENDING",
            "items": [{"description": "Control", "amount": "$19.99"}],
        }

        for order, fragment in ((refund, "refund"), (pending, "PENDING")):
            records, skipped = epic_orders.parse_order(order)
            self.assertEqual(records, [])
            self.assertEqual(skipped[0]["description"], order["orderId"])
            self.assertIn(fragment, skipped[0]["reason"])

    def test_multi_item_order_keeps_per_item_amounts(self):
        order = {
            "orderId": "F2004",
            "createdAtMillis": 1650000000000,
            "currency": "EUR",
            "items": [
                {"description": "Game A", "amount": "9,99 €"},
                {"description": "Game B", "amount": "4,99 €"},
            ],
        }

        records, skipped = epic_orders.parse_order(order)

        self.assertEqual(skipped, [])
        self.assertEqual([r.price_paid for r in records], [9.99, 4.99])
        self.assertEqual({r.price_currency for r in records}, {"EUR"})

    def test_items_without_amounts_split_order_total_evenly(self):
        order = {
            "orderId": "F2005",
            "createdAtMillis": 1650000000000,
            "total": "$25.00",
            "items": [
                {"description": "Game A"},
                {"description": "Game B"},
                {"description": "Game C"},
            ],
        }

        records, skipped = epic_orders.parse_order(order)

        self.assertEqual(skipped, [])
        self.assertEqual([r.price_paid for r in records], [8.33, 8.33, 8.34])
        self.assertAlmostEqual(sum(r.price_paid for r in records), 25.00)
        self.assertEqual({r.price_currency for r in records}, {"USD"})

    def test_addon_named_item_gets_content_type_hint(self):
        order = {
            "orderId": "F2006",
            "createdAtMillis": 1650000000000,
            "items": [{"description": "Borderlands 3 Season Pass", "amount": "$49.99"}],
        }

        records, _ = epic_orders.parse_order(order)

        self.assertEqual(records[0].content_type, "dlc")

    def test_order_without_items_is_skipped_with_reason(self):
        records, skipped = epic_orders.parse_order({"orderId": "E1"})
        self.assertEqual(records, [])
        self.assertEqual(skipped[0]["description"], "E1")
        self.assertIn("no items", skipped[0]["reason"])


class EpicFetchTests(unittest.IsolatedAsyncioTestCase):
    def _write_cookies(self, tmp: str) -> str:
        path = os.path.join(tmp, "epic_cookies.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"EPIC_BEARER_TOKEN": "tok", "EPIC_SSO_RM": "rm"}, f)
        return path

    @staticmethod
    def _order(order_id: str, title: str) -> dict:
        return {
            "orderId": order_id,
            "createdAtMillis": 1615464000000,
            "orderStatus": "COMPLETED",
            "items": [{"description": title, "amount": "$9.99"}],
        }

    async def test_missing_cookie_file_raises_clear_error(self):
        with (
            patch.dict(os.environ, {"EPIC_COOKIES_FILE": "/nonexistent/epic.json"}),
            self.assertRaisesRegex(RuntimeError, "create_session_ingest_link"),
        ):
            await epic_orders.fetch_epic_purchases()

    async def test_auth_failure_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with (
                patch.dict(os.environ, {"EPIC_COOKIES_FILE": path}),
                self.assertRaisesRegex(RuntimeError, "create_session_ingest_link"),
            ):
                await epic_orders.fetch_epic_purchases(
                    transport=httpx.MockTransport(handler)
                )

    async def test_real_path_impersonates_and_warms_up(self):
        # Without a test transport the fetch must go through curl_cffi
        # impersonation: browser profile, cookies on .epicgames.com, a
        # transactions-page warm-up GET before the ajax pagination.
        class FakeResponse:
            status_code = 200
            text = ""
            headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

            def raise_for_status(self):
                return self

        calls: list[tuple[str, dict]] = []
        session_kwargs: dict = {}

        class FakeSession:
            def __init__(self, **kwargs):
                session_kwargs.update(kwargs)
                self.cookies = SimpleNamespace(set=lambda *a, **k: None)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, **kwargs):
                calls.append((url, kwargs))
                if "ajaxGetOrderHistory" not in url:
                    return FakeResponse({})  # the warm-up page walk
                return FakeResponse(
                    {"orders": [EpicFetchTests._order("o-1", "Alan Wake")]}
                )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with (
                patch.dict(os.environ, {"EPIC_COOKIES_FILE": path}),
                patch.object(epic_orders, "AsyncSession", FakeSession),
            ):
                records, skipped = await epic_orders.fetch_epic_purchases()

        self.assertEqual(session_kwargs.get("impersonate"), epic_orders._IMPERSONATE_PROFILE)
        self.assertEqual(calls[0][0], "https://www.epicgames.com/account/transactions")
        self.assertIn("ajaxGetOrderHistory", calls[1][0])
        self.assertEqual(
            calls[1][1]["headers"]["Accept"], "application/json"
        )
        self.assertEqual([r.title for r in records], ["Alan Wake"])
        self.assertEqual(skipped, [])

    async def test_cloudflare_challenge_does_not_blame_cookies(self):
        # A challenge 403 never reaches Epic's auth layer, so the error must not
        # send the user off to re-paste perfectly good cookies.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                text="<html><script src='/cdn-cgi/challenge-platform/x.js'></script></html>",
                headers={"content-type": "text/html", "cf-mitigated": "challenge"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with (
                patch.dict(os.environ, {"EPIC_COOKIES_FILE": path}),
                self.assertRaisesRegex(RuntimeError, "Cloudflare bot protection"),
            ):
                await epic_orders.fetch_epic_purchases(
                    transport=httpx.MockTransport(handler)
                )

    async def test_challenge_detected_without_cf_mitigated_header(self):
        # Not every challenge variant sets the header; the injected script is
        # the fallback signal.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                text="<html><script src='/cdn-cgi/challenge-platform/x.js'></script></html>",
                headers={"content-type": "text/html"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with (
                patch.dict(os.environ, {"EPIC_COOKIES_FILE": path}),
                self.assertRaisesRegex(RuntimeError, "Cloudflare bot protection"),
            ):
                await epic_orders.fetch_epic_purchases(
                    transport=httpx.MockTransport(handler)
                )

    async def test_login_page_html_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text="<!doctype html><title>Sign in</title>",
                headers={"content-type": "text/html"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with (
                patch.dict(os.environ, {"EPIC_COOKIES_FILE": path}),
                self.assertRaisesRegex(RuntimeError, "create_session_ingest_link"),
            ):
                await epic_orders.fetch_epic_purchases(
                    transport=httpx.MockTransport(handler)
                )

    async def test_happy_path_paginates_via_next_page_token(self):
        seen: list[tuple[str | None, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            token = request.url.params.get("nextPageToken")
            seen.append((token, request.headers.get("Cookie", "")))
            if token is None:
                payload = {"orders": [self._order("F1", "Game 1")], "nextPageToken": "tok-2"}
            else:
                payload = {"orders": [self._order("F2", "Game 2")]}
            return httpx.Response(
                200, json=payload, headers={"content-type": "application/json"}
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_cookies(tmp)
            with patch.dict(os.environ, {"EPIC_COOKIES_FILE": path}):
                records, skipped = await epic_orders.fetch_epic_purchases(
                    transport=httpx.MockTransport(handler)
                )

        self.assertEqual([token for token, _ in seen], [None, "tok-2"])
        for _, cookie_header in seen:
            self.assertIn("EPIC_BEARER_TOKEN=tok", cookie_header)
        self.assertEqual([r.title for r in records], ["Game 1", "Game 2"])
        self.assertEqual(skipped, [])


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
        purchases, refunds, skipped, cursor = steam_history.parse_wallet_history(
            _fixture("steam_history_sample.html")
        )

        self.assertEqual(len(purchases), 3)
        single = purchases[0]
        self.assertEqual(single["date"], "2021-03-12")
        self.assertEqual(single["items"], ["Total War: WARHAMMER"])
        self.assertEqual(single["total"], 59.99)
        self.assertEqual(single["currency"], "USD")

        cart = purchases[1]
        self.assertEqual(cart["items"], ["Hollow Knight", "Celeste", "Dead Cells"])
        self.assertEqual(cart["total"], 25.00)
        self.assertEqual(cart["currency"], "EUR")

        # Refunds are their own category, not skipped rows — apply_refunds needs them.
        self.assertEqual(
            [(r["date"], r["items"], r["total"]) for r in refunds],
            [("2021-01-22", ["Dicey Dungeons"], 14.99),
             ("2021-01-15", ["Cyberpunk 2077"], 59.99)],
        )

        reasons = " | ".join(s["reason"] for s in skipped)
        self.assertEqual(len(skipped), 3)
        self.assertIn("Market Transaction", reasons)
        self.assertIn("In-Game Purchase", reasons)
        self.assertIn("Gift Purchase", reasons)
        self.assertNotIn("Refund", reasons)

        self.assertEqual(cursor["wallet_txnid"], "9990001")
        self.assertEqual(cursor["timestamp_newest"], 1615500000)

    def test_refunded_badge_is_not_parsed_as_an_item(self):
        # Steam renders <div class="wth_item_refunded">Refund</div> inside the items
        # cell of a refunded line. It is decoration, not a second purchased item.
        html = (
            '<tr><td class="wht_date">22 Jan, 2021</td>'
            '<td class="wht_items">'
            '<div style="clear: both">Dicey Dungeons</div>'
            '<div class="wth_item_refunded">Refund</div>'
            "</td>"
            '<td class="wht_type"><div>Refund</div></td>'
            '<td class="wht_total">$14.99</td></tr>'
        )

        purchases, refunds, _ = steam_history.parse_history_fragment(html)

        self.assertEqual(purchases, [])
        self.assertEqual(refunds[0]["items"], ["Dicey Dungeons"])

    def test_refunded_badge_on_a_purchase_row_does_not_steal_a_price_share(self):
        # Defensive: were the badge ever to render on a purchase row, leaving it in
        # would split the total across a phantom "Refund" item and halve the real price.
        html = (
            '<tr><td class="wht_date">20 Jan, 2021</td>'
            '<td class="wht_items">'
            '<div style="clear: both">Dicey Dungeons</div>'
            '<div class="wth_item_refunded">Refund</div>'
            "</td>"
            '<td class="wht_type"><div>Purchase</div></td>'
            '<td class="wht_total">$14.99</td></tr>'
        )

        purchases, _, _ = steam_history.parse_history_fragment(html)

        self.assertEqual(purchases[0]["items"], ["Dicey Dungeons"])
        records = steam_history._purchase_records(purchases)
        self.assertEqual([(r.title, r.price_paid) for r in records], [("Dicey Dungeons", 14.99)])

    def test_wallet_credit_topup_is_skipped_not_minted(self):
        # A "Purchased €50 Wallet Credit" row files as a Purchase but acquires no
        # game — left in, it mints a phantom game and double-counts the spend.
        html = (
            '<tr><td class="wht_date">3 Feb, 2021</td>'
            '<td class="wht_items"><div>Purchased 50,--&euro; Wallet Credit</div></td>'
            '<td class="wht_type"><div>Purchase</div></td>'
            '<td class="wht_total">50,--&euro;</td></tr>'
            '<tr><td class="wht_date">4 Feb, 2021</td>'
            '<td class="wht_items"><div>Celeste</div></td>'
            '<td class="wht_type"><div>Purchase</div></td>'
            '<td class="wht_total">$14.99</td></tr>'
        )

        purchases, _, skipped = steam_history.parse_history_fragment(html)

        self.assertEqual([p["items"] for p in purchases], [["Celeste"]])
        reasons = " | ".join(s["reason"] for s in skipped)
        self.assertIn("wallet credit top-up", reasons)

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


class SteamRefundTests(unittest.TestCase):
    def _row(self, items, date, total, currency="USD"):
        return {"date": date, "items": list(items), "total": total, "currency": currency}

    def _priced(self, rows):
        """Rows → {title: price} the way the importer books them."""
        return {
            r.title: r.price_paid for r in steam_history._purchase_records(rows)
        }

    def test_refund_removes_the_purchase_from_the_spend_totals(self):
        rows = [
            self._row(["Dicey Dungeons"], "2021-02-23", 5.55),
            self._row(["Hades"], "2021-03-01", 24.99),
        ]

        kept, skipped = steam_history.apply_refunds(
            rows, [self._row(["Dicey Dungeons"], "2021-03-05", 5.55)]
        )

        self.assertEqual(self._priced(kept), {"Hades": 24.99})
        self.assertEqual(len(skipped), 1)
        self.assertIn("removed from the matching purchase row", skipped[0]["reason"])
        self.assertIn("2021-03-05", skipped[0]["reason"])

    def test_partial_cart_refund_subtracts_the_real_amount_not_the_even_share(self):
        # A $30 two-item cart splits to $15/$15, but those shares are only an
        # estimate. Steam returned $5, so $25 of real spend must remain — dropping
        # the built record instead would wrongly leave $15.
        rows = [self._row(["Cheap Game", "Pricey Game"], "2021-02-02", 30.00)]

        kept, _ = steam_history.apply_refunds(
            rows, [self._row(["Cheap Game"], "2021-02-09", 5.00)]
        )

        self.assertEqual(self._priced(kept), {"Pricey Game": 25.00})

    def test_refund_of_one_cart_item_leaves_the_others_splitting_the_remainder(self):
        rows = [self._row(["Hollow Knight", "Celeste", "Dead Cells"], "2021-02-02", 25.00)]

        kept, _ = steam_history.apply_refunds(
            rows, [self._row(["Celeste"], "2021-02-09", 7.00)]
        )

        # $25 cart - $7 returned = $18 across the two survivors.
        self.assertEqual(self._priced(kept), {"Hollow Knight": 9.00, "Dead Cells": 9.00})

    def test_every_item_refunded_drops_the_row_entirely(self):
        rows = [self._row(["Hollow Knight", "Celeste"], "2021-02-02", 20.00)]

        kept, _ = steam_history.apply_refunds(
            rows,
            [
                self._row(["Hollow Knight"], "2021-02-09", 12.00),
                self._row(["Celeste"], "2021-02-09", 8.00),
            ],
        )

        self.assertEqual(kept, [])

    def test_refund_in_a_different_currency_falls_back_to_the_even_share(self):
        # Can't subtract EUR from a USD cart total; the even share is all we have.
        rows = [self._row(["Game A", "Game B"], "2021-02-02", 30.00, currency="USD")]

        kept, _ = steam_history.apply_refunds(
            rows, [self._row(["Game A"], "2021-02-09", 5.00, currency="EUR")]
        )

        self.assertEqual(self._priced(kept), {"Game B": 15.00})

    def test_refund_never_drives_a_cart_total_negative(self):
        rows = [self._row(["Game A", "Game B"], "2021-02-02", 10.00)]

        kept, _ = steam_history.apply_refunds(
            rows, [self._row(["Game A"], "2021-02-09", 99.00)]
        )

        self.assertEqual(self._priced(kept), {"Game B": 0.0})

    def test_refund_without_a_matching_purchase_is_reported_not_silent(self):
        rows = [self._row(["Hades"], "2021-03-01", 24.99)]

        kept, skipped = steam_history.apply_refunds(
            rows, [self._row(["Cyberpunk 2077"], "2021-01-15", 59.99)]
        )

        self.assertEqual(self._priced(kept), {"Hades": 24.99})
        self.assertEqual(len(skipped), 1)
        self.assertIn("no matching purchase row", skipped[0]["reason"])

    def test_repurchase_after_a_refund_keeps_its_row(self):
        rows = [
            self._row(["Dicey Dungeons"], "2021-01-20", 14.99),
            self._row(["Dicey Dungeons"], "2021-06-01", 9.99),  # bought again later
        ]

        kept, _ = steam_history.apply_refunds(
            rows, [self._row(["Dicey Dungeons"], "2021-01-22", 14.99)]
        )

        # Only the pre-refund purchase goes; the later re-buy survives.
        self.assertEqual([(r["date"], r["total"]) for r in kept], [("2021-06-01", 9.99)])

    def test_refund_cancels_only_the_latest_purchase_at_or_before_it(self):
        rows = [
            self._row(["Hades"], "2021-01-01", 24.99),
            self._row(["Hades"], "2021-02-01", 19.99),
        ]

        kept, _ = steam_history.apply_refunds(
            rows, [self._row(["Hades"], "2021-02-10", 19.99)]
        )

        # Bought twice, refunded once → the most recent goes, one row remains.
        self.assertEqual([(r["date"], r["total"]) for r in kept], [("2021-01-01", 24.99)])

    def test_two_refunds_consume_two_separate_rows(self):
        rows = [
            self._row(["Hades"], "2021-01-01", 24.99),
            self._row(["Hades"], "2021-02-01", 19.99),
        ]

        kept, skipped = steam_history.apply_refunds(
            rows,
            [
                self._row(["Hades"], "2021-02-10", 19.99),
                self._row(["Hades"], "2021-02-11", 24.99),
            ],
        )

        # Each refund consumes a distinct row rather than double-cancelling one.
        self.assertEqual(kept, [])
        self.assertEqual(len(skipped), 2)

    def test_refund_matching_ignores_punctuation_and_case(self):
        rows = [self._row(["Total War: WARHAMMER"], "2021-01-01", 59.99)]

        kept, _ = steam_history.apply_refunds(
            rows, [self._row(["total war warhammer"], "2021-01-05", 59.99)]
        )

        self.assertEqual(kept, [])

    def test_refund_does_not_cancel_a_different_game(self):
        rows = [self._row(["Hades"], "2021-01-01", 24.99)]

        kept, skipped = steam_history.apply_refunds(
            rows, [self._row(["Hades II"], "2021-01-05", 29.99)]
        )

        # Substring-ish neighbours must not collide — exact normalized match only.
        self.assertEqual(self._priced(kept), {"Hades": 24.99})
        self.assertIn("no matching purchase row", skipped[0]["reason"])

    def test_no_refunds_leaves_rows_untouched(self):
        rows = [self._row(["Hades"], "2021-03-01", 24.99)]

        kept, skipped = steam_history.apply_refunds(rows, [])

        self.assertEqual(kept, rows)
        self.assertEqual(skipped, [])

    def test_apply_refunds_does_not_mutate_the_caller_rows(self):
        rows = [self._row(["Hollow Knight", "Celeste"], "2021-02-02", 20.00)]

        steam_history.apply_refunds(rows, [self._row(["Celeste"], "2021-02-09", 8.00)])

        self.assertEqual(rows[0]["items"], ["Hollow Knight", "Celeste"])
        self.assertEqual(rows[0]["total"], 20.00)


class SteamFetchTests(unittest.IsolatedAsyncioTestCase):
    def _write_cookies(self, tmp: str) -> str:
        path = os.path.join(tmp, "steam_cookies.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"steamLoginSecure": "765-abc", "sessionid": "sess-1"}, f)
        return path

    async def test_missing_cookie_file_raises_clear_error(self):
        with (
            patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": "/nonexistent/steam.json"}),
            self.assertRaisesRegex(RuntimeError, "create_session_ingest_link"),
        ):
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
        # Bought then refunded end-to-end: its $14.99 must not survive as spend.
        self.assertNotIn("Dicey Dungeons", by_title)

        reasons = " | ".join(s["reason"] for s in skipped)
        # 3 non-purchase row types + 2 refunds (one cancelling, one unmatched).
        self.assertEqual(len(skipped), 5)
        self.assertIn("removed from the matching purchase row", reasons)
        self.assertIn("no matching purchase row", reasons)

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
            with (
                patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": path}),
                self.assertRaisesRegex(RuntimeError, "create_session_ingest_link"),
            ):
                await steam_history.fetch_steam_purchases(
                    transport=httpx.MockTransport(handler)
                )


def _make_refresh_token(sub: str | None = None, exp: int | None = None) -> str:
    """A minimal unsigned JWT whose claim segment carries sub/exp (no signature)."""
    import base64 as _b64

    payload: dict = {}
    if sub is not None:
        payload["sub"] = sub
    if exp is not None:
        payload["exp"] = exp
    body = _b64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"eyJ0eXAiOiJKV1QifQ.{body}.sig"


def _make_login_secure(audience: list[str], steamid: str = "76561198000000000") -> str:
    """A steamLoginSecure cookie value (``steamid||<JWT>``) carrying an ``aud`` claim."""
    import base64 as _b64

    body = (
        _b64.urlsafe_b64encode(json.dumps({"aud": audience}).encode()).rstrip(b"=").decode()
    )
    return f"{steamid}||eyJ0eXAiOiJKV1QifQ.{body}.sig"


class SteamSessionMintTests(unittest.IsolatedAsyncioTestCase):
    def _write_refresh_token(self, tmp: str, token: str) -> str:
        path = os.path.join(tmp, "steam_refresh_token.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"steamRefresh_steam": token}, f)
        return path

    def _mint_handler(self, captured: list, *, echo_steamid: str | None = None):
        """finalizelogin → per-domain transfer; each transfer sets steamLoginSecure."""
        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            host, path = request.url.host, request.url.path
            if host == "login.steampowered.com" and path == "/jwt/finalizelogin":
                body: dict = {
                    "transfer_info": [
                        {"url": "https://steamcommunity.com/login/settoken",
                         "params": {"nonce": "cnonce", "auth": "cauth"}},
                        {"url": "https://store.steampowered.com/login/settoken",
                         "params": {"nonce": "snonce", "auth": "sauth"}},
                    ],
                }
                if echo_steamid is not None:
                    body["steamID"] = echo_steamid
                return httpx.Response(200, json=body)
            if path == "/login/settoken":
                value = "765-store" if host == "store.steampowered.com" else "765-comm"
                return httpx.Response(200, headers={"set-cookie": f"steamLoginSecure={value}; Path=/"})
            raise AssertionError(f"unexpected request: {request.url}")
        return handler

    async def test_successful_mint_returns_store_cookies(self):
        captured: list = []
        token = _make_refresh_token(sub="76561198000000000", exp=9999999999)
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_refresh_token(tmp, token)
            with patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path, "STEAM_ID": "76561198000000000"}):
                cookies = await steam_session.load_steam_web_cookies(
                    transport=httpx.MockTransport(self._mint_handler(captured, echo_steamid="76561198000000000"))
                )
        # Store-domain steamLoginSecure is preferred over the community one.
        self.assertEqual(cookies["steamLoginSecure"], "765-store")
        self.assertRegex(cookies["sessionid"], r"^[0-9a-f]{24}$")
        # finalizelogin must be multipart and carry the refresh token as the nonce.
        finalize = next(r for r in captured if r.url.path == "/jwt/finalizelogin")
        self.assertTrue(finalize.headers.get("content-type", "").startswith("multipart/form-data"))
        self.assertIn(token.encode(), finalize.content)
        # Transfer POSTs carry steamID plus the entry's params verbatim.
        store_transfer = next(r for r in captured if r.url.host == "store.steampowered.com")
        self.assertIn(b"76561198000000000", store_transfer.content)
        self.assertIn(b"sauth", store_transfer.content)

    async def test_expired_refresh_token_detected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/jwt/finalizelogin":
                return httpx.Response(200, json={"transfer_info": []})
            raise AssertionError(request.url)

        token = _make_refresh_token(sub="76561198000000000", exp=9999999999)
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_refresh_token(tmp, token)
            with (
                patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path, "STEAM_ID": "76561198000000000"}),
                self.assertRaisesRegex(RuntimeError, "refresh token has expired"),
            ):
                await steam_session.load_steam_web_cookies(transport=httpx.MockTransport(handler))

    async def test_transient_mint_failure_is_distinct(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="steam is having a moment")

        token = _make_refresh_token(sub="76561198000000000", exp=9999999999)
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_refresh_token(tmp, token)
            with (
                patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path, "STEAM_ID": "76561198000000000"}),
                self.assertRaisesRegex(RuntimeError, "transiently") as ctx,
            ):
                await steam_session.load_steam_web_cookies(transport=httpx.MockTransport(handler))
        # The transient message must NOT tell the user to re-paste the token.
        self.assertNotIn("expired", str(ctx.exception))

    async def test_transfer_hop_5xx_is_transient(self):
        # finalizelogin validated the token (returned transfer_info), so a failing
        # transfer hop is transient — it must NOT be reported as an expired token.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/jwt/finalizelogin":
                return httpx.Response(200, json={
                    "steamID": "76561198000000000",
                    "transfer_info": [{"url": "https://store.steampowered.com/login/settoken",
                                       "params": {"nonce": "n", "auth": "a"}}],
                })
            if request.url.path == "/login/settoken":
                return httpx.Response(500, text="store is down")
            raise AssertionError(request.url)

        token = _make_refresh_token(sub="76561198000000000", exp=9999999999)
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_refresh_token(tmp, token)
            with (
                patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path, "STEAM_ID": "76561198000000000"}),
                self.assertRaisesRegex(RuntimeError, "transiently") as ctx,
            ):
                await steam_session.load_steam_web_cookies(transport=httpx.MockTransport(handler))
        self.assertNotIn("expired", str(ctx.exception))

    async def test_transfer_missing_store_cookie_is_transient(self):
        # A transfer hop that 200s without Set-Cookie, and only a community cookie
        # present: the store-domain cookie is required, and its absence is transient.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/jwt/finalizelogin":
                return httpx.Response(200, json={
                    "steamID": "76561198000000000",
                    "transfer_info": [
                        {"url": "https://steamcommunity.com/login/settoken",
                         "params": {"nonce": "n", "auth": "a"}},
                        {"url": "https://store.steampowered.com/login/settoken",
                         "params": {"nonce": "n", "auth": "a"}},
                    ],
                })
            if request.url.host == "steamcommunity.com":
                return httpx.Response(200, headers={"set-cookie": "steamLoginSecure=comm; Path=/"})
            if request.url.host == "store.steampowered.com":
                return httpx.Response(200, text="ok, but no cookie for you")
            raise AssertionError(request.url)

        token = _make_refresh_token(sub="76561198000000000", exp=9999999999)
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_refresh_token(tmp, token)
            with (
                patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path, "STEAM_ID": "76561198000000000"}),
                self.assertRaisesRegex(RuntimeError, "transiently") as ctx,
            ):
                await steam_session.load_steam_web_cookies(transport=httpx.MockTransport(handler))
        # The community cookie must not be substituted for the store one.
        self.assertNotIn("expired", str(ctx.exception))

    async def test_steamid_falls_back_to_jwt_sub(self):
        captured: list = []
        token = _make_refresh_token(sub="76561198000000123", exp=9999999999)
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_refresh_token(tmp, token)
            with patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path}):
                os.environ.pop("STEAM_ID", None)
                await steam_session.load_steam_web_cookies(
                    transport=httpx.MockTransport(self._mint_handler(captured))
                )
        # With no STEAM_ID and no echoed steamID, the transfer uses the token's sub.
        store_transfer = next(r for r in captured if r.url.host == "store.steampowered.com")
        self.assertIn(b"76561198000000123", store_transfer.content)

    async def test_fallback_to_legacy_static_cookies(self):
        called: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request.url)
            raise AssertionError("legacy path must not hit login.steampowered.com")

        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.join(tmp, "steam_store_cookies.json")
            with open(legacy, "w", encoding="utf-8") as f:
                json.dump({"steamLoginSecure": "legacy-abc", "sessionid": "s"}, f)
            with patch.dict(os.environ, {
                "STEAM_STORE_COOKIES_FILE": legacy,
                "STEAM_REFRESH_TOKEN_FILE": os.path.join(tmp, "absent.json"),
            }):
                cookies = await steam_session.load_steam_web_cookies(
                    transport=httpx.MockTransport(handler)
                )
        self.assertEqual(cookies["steamLoginSecure"], "legacy-abc")
        self.assertEqual(called, [])

    async def test_no_session_configured_raises_both_hints(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "STEAM_REFRESH_TOKEN_FILE": os.path.join(tmp, "absent-refresh.json"),
            "STEAM_STORE_COOKIES_FILE": os.path.join(tmp, "absent-store.json"),
        }), self.assertRaisesRegex(RuntimeError, "steam_refresh") as ctx:
            await steam_session.load_steam_web_cookies(
                transport=httpx.MockTransport(lambda r: httpx.Response(200))
            )
        self.assertIn("create_session_ingest_link", str(ctx.exception))

    async def test_fetch_steam_purchases_mints_from_refresh_token(self):
        token = _make_refresh_token(sub="76561198000000000", exp=9999999999)
        cookie_seen: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            host, path = request.url.host, request.url.path
            if host == "login.steampowered.com" and path == "/jwt/finalizelogin":
                return httpx.Response(200, json={
                    "steamID": "76561198000000000",
                    "transfer_info": [{"url": "https://store.steampowered.com/login/settoken",
                                       "params": {"nonce": "n", "auth": "a"}}],
                })
            if path == "/login/settoken":
                return httpx.Response(200, headers={"set-cookie": "steamLoginSecure=765-minted; Path=/"})
            if path == "/account/licenses/":
                cookie_seen.append(request.headers.get("cookie", ""))
                return httpx.Response(200, text=_fixture("steam_licenses_sample.html"))
            if path == "/account/history/":
                return httpx.Response(200, text=_fixture("steam_history_sample.html"))
            if path == "/account/AjaxLoadMoreHistory/":
                return httpx.Response(200, json={"html": "", "cursor": None},
                                      headers={"content-type": "application/json"})
            raise AssertionError(f"unexpected request: {request.url}")

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_refresh_token(tmp, token)
            with patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path, "STEAM_ID": "76561198000000000"}):
                records, _ = await steam_history.fetch_steam_purchases(
                    transport=httpx.MockTransport(handler)
                )
        self.assertTrue(records)
        # The scrape authenticated with the freshly minted cookie, not a stored one.
        self.assertTrue(any("steamLoginSecure=765-minted" in c for c in cookie_seen))

    async def test_fetch_owned_steam_appids_mints_from_refresh_token(self):
        token = _make_refresh_token(sub="76561198000000000", exp=9999999999)

        def handler(request: httpx.Request) -> httpx.Response:
            host, path = request.url.host, request.url.path
            if host == "login.steampowered.com" and path == "/jwt/finalizelogin":
                return httpx.Response(200, json={
                    "steamID": "76561198000000000",
                    "transfer_info": [{"url": "https://store.steampowered.com/login/settoken",
                                       "params": {"nonce": "n", "auth": "a"}}],
                })
            if path == "/login/settoken":
                return httpx.Response(200, headers={"set-cookie": "steamLoginSecure=765-minted; Path=/"})
            if path == "/dynamicstore/userdata/":
                return httpx.Response(200, json={"rgOwnedApps": [10, 20, 30]},
                                      headers={"content-type": "application/json"})
            raise AssertionError(f"unexpected request: {request.url}")

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_refresh_token(tmp, token)
            with patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path, "STEAM_ID": "76561198000000000"}):
                appids = await steam_licenses.fetch_owned_steam_appids(
                    transport=httpx.MockTransport(handler)
                )
        self.assertEqual(appids, {10, 20, 30})


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
            # create_missing=False keeps the unmatched-reporting behavior under test.
            result = await acquisition.import_purchases(create_missing=False)

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["status"], "ok")
        self.assertEqual(eshop["fetched"], 2)
        self.assertEqual(eshop["filled"], 1)
        self.assertEqual(eshop["created"], 0)
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

    async def test_create_missing_default_mints_new_owned_game(self):
        # A purchase that matches nothing becomes an owned library game — a
        # purchase is a stronger ownership signal than inferred playtime.
        gid = await seed_game("Hades")
        await add_platform(gid, "switch2")

        records = [_eshop_record("Hades"), _eshop_record("F-ZERO 99")]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["filled"], 1)
        self.assertEqual(eshop["created"], 1)
        self.assertEqual(eshop["unmatched"], [])
        self.assertEqual(result["totals"]["created"], 1)
        detail = eshop["created_details"]
        self.assertEqual(len(detail), 1)
        self.assertEqual(detail[0]["name"], "F-ZERO 99")
        self.assertEqual(detail[0]["platform"], "switch2")

        # The new game is owned on switch2 and carries its acquisition (eShop
        # transactions expose no title id, so it reconciles by name later).
        new_id = detail[0]["game_id"]
        row = await _acquisition_row(new_id, "switch2")
        self.assertEqual(row["price_paid"], 19.99)
        self.assertEqual(row["purchase_source"], "eshop")
        async with db_module.get_db() as db:
            owned = await db.execute_fetchone(
                "SELECT owned FROM game_platforms WHERE game_id = ? AND platform = ?",
                (new_id, "switch2"),
            )
        self.assertEqual(owned["owned"], 1)

    async def test_created_game_uses_edition_stripped_title(self):
        # A suffix-laden storefront title that matches nothing is created under
        # its CLEAN name, so a later name-based ownership sync ("Hollow Knight")
        # reconciles onto this row instead of stranding a duplicate.
        records = [
            _eshop_record("Hollow Knight – Nintendo Switch 2 Edition-upgradepack"),
        ]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["created"], 1)
        self.assertEqual(eshop["created_details"][0]["name"], "Hollow Knight")
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT name FROM games WHERE id = ?",
                (eshop["created_details"][0]["game_id"],),
            )
        self.assertEqual(row["name"], "Hollow Knight")

    async def test_dry_run_previews_would_create(self):
        gid = await seed_game("Hades")
        await add_platform(gid, "switch2")
        records = [_eshop_record("Hades"), _eshop_record("F-ZERO 99")]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"], dry_run=True)

        eshop = result["sources"]["eshop"]
        names = [item["name"] for item in eshop["would_create"]]
        self.assertEqual(names, ["F-ZERO 99"])
        # Nothing was written — the game does not exist yet.
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ?", ("F-ZERO 99",)
            )
        self.assertIsNone(row)

    async def test_bundle_diverted_to_needs_split_not_unmatched(self):
        gid = await seed_game("Hades")
        await add_platform(gid, "switch2")
        records = [
            _eshop_record("Hades"),
            _eshop_record(
                "BioShock: The Collection", is_bundle=True, price_paid=9.99
            ),
        ]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        eshop = result["sources"]["eshop"]
        # The bundle never reaches the single-game matcher, so unmatched is empty.
        self.assertEqual(eshop["unmatched"], [])
        self.assertEqual(eshop["filled"], 1)
        self.assertEqual(
            eshop["bundles_needing_split"],
            [{
                "bundle_name": "BioShock: The Collection",
                "platform": "switch2",
                "total_price": 9.99,
                "price_currency": "USD",
                "acquired_at": "2024-03-01",
                "purchase_source": "eshop",
                "already_recorded": False,
                "platforms": ["switch2"],
            }],
        )
        self.assertEqual(result["totals"]["bundles_needing_split"], 1)
        self.assertEqual(result["totals"]["unmatched"], 0)

    async def test_reimport_flags_bundle_already_recorded(self):
        # After split_bundle_acquisition writes the bundle, a repeat import
        # re-surfaces it (the fetch can't know) but flags it as handled.
        gid = await seed_game("BioShock")
        await add_platform(gid, "switch2")
        await acquisition.split_bundle_acquisition(
            bundle_name="BioShock: The Collection",
            platform="switch2",
            games=[{"game_id": gid}],
            total_price=9.99,
        )

        records = [
            _eshop_record(
                "BioShock: The Collection", is_bundle=True, price_paid=9.99
            ),
        ]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        entry = result["sources"]["eshop"]["bundles_needing_split"][0]
        self.assertTrue(entry["already_recorded"])

    async def test_dry_run_surfaces_bundles_and_excludes_from_proposed(self):
        records = [
            _eshop_record("Hades"),
            _eshop_record("Some Bundle", is_bundle=True),
        ]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(
                sources=["eshop"], dry_run=True
            )

        eshop = result["sources"]["eshop"]
        proposed_names = [item["name"] for item in eshop["proposed"]]
        self.assertEqual(proposed_names, ["Hades"])  # bundle not a proposed item
        self.assertEqual(len(eshop["bundles_needing_split"]), 1)
        self.assertEqual(
            eshop["bundles_needing_split"][0]["bundle_name"], "Some Bundle"
        )

    async def test_identifier_first_match_fills_renamed_library_title(self):
        # The library title differs from the store transaction title (renamed/
        # localized), but the seeded steam_appid matches exactly. Steam is used
        # because it carries a store identifier; eShop matches by title only.
        gid = await seed_game("Dragon Quest III HD-2D Remake")
        gpid = await add_platform(gid, "steam")
        await add_identifier(gpid, db_module.STEAM_APP_ID, "1234567")

        records = [
            PurchaseRecord(
                title="DRAGON QUEST III (localized)",
                platform="steam",
                purchase_source="steam",
                acquired_at="2024-03-01",
                price_paid=19.99,
                price_currency="USD",
                store_identifier="1234567",
            )
        ]
        with _patch_fetchers(
            fetch_steam_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["steam"])

        steam = result["sources"]["steam"]
        self.assertEqual(steam["status"], "ok")
        self.assertEqual(steam["filled"], 1)
        self.assertEqual(steam["unmatched"], [])
        self.assertEqual(result["totals"]["unmatched"], 0)

        row = await _acquisition_row(gid, "steam")
        self.assertEqual(row["acquired_at"], "2024-03-01")
        self.assertEqual(row["price_paid"], 19.99)
        self.assertEqual(row["purchase_source"], "steam")

    async def test_dry_run_proposed_item_carries_identifier_keys(self):
        records = [
            PurchaseRecord(
                title="Hades",
                platform="steam",
                purchase_source="steam",
                acquired_at="2024-03-01",
                price_paid=19.99,
                price_currency="USD",
                store_identifier="99999",
            )
        ]
        with _patch_fetchers(
            fetch_steam_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(
                sources=["steam"], dry_run=True
            )

        proposed = result["sources"]["steam"]["proposed"][0]
        self.assertEqual(proposed["identifier_type"], db_module.STEAM_APP_ID)
        self.assertEqual(proposed["identifier_value"], "99999")

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

    async def test_all_sources_aggregate_totals(self):
        hades = await seed_game("Hades")
        await add_platform(hades, "switch2")
        hollow = await seed_game("Hollow Knight")
        await add_platform(hollow, "steam")
        witcher = await seed_game("The Witcher 3 Wild Hunt")
        await add_platform(witcher, "gog")
        celeste = await seed_game("Celeste")
        await add_platform(celeste, "steam")
        alan = await seed_game("Alan Wake")
        await add_platform(alan, "epic")

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
            fetch_epic_purchases=AsyncMock(
                return_value=([_rec("Alan Wake", "epic", "epic")], []),
            ),
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
            result = await acquisition.import_purchases(create_missing=False)

        self.assertEqual(
            set(result["sources"]), {"epic", "eshop", "gog", "humble", "steam"}
        )
        for source in ("epic", "eshop", "gog", "humble", "steam"):
            self.assertEqual(result["sources"][source]["status"], "ok")
        self.assertEqual(result["totals"]["fetched"], 6)
        self.assertEqual(result["totals"]["filled"], 5)
        self.assertEqual(result["totals"]["created"], 0)
        self.assertEqual(result["totals"]["unmatched"], 1)
        self.assertEqual(result["totals"]["errors"], 0)
        self.assertEqual(len(result["sources"]["gog"]["skipped"]), 1)

        row = await _acquisition_row(witcher, "gog")
        self.assertEqual(row["purchase_source"], "gog")
        row = await _acquisition_row(celeste, "steam")
        self.assertEqual(row["purchase_source"], "steam")
        row = await _acquisition_row(alan, "epic")
        self.assertEqual(row["purchase_source"], "epic")


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

    async def test_nintendo_session_shared_between_ownership_and_eshop(self):
        # The one accounts.nintendo.com session set_nintendo_session stores is the
        # same one the eShop importer's account loader reads — no separate export.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nintendo_cookies.json")
            with patch.dict(os.environ, {"NINTENDO_COOKIES_FILE": path}):
                result = await admin.set_nintendo_session(
                    json.dumps({"NASID": "s", "NATID": "t"})
                )
                self.assertEqual(result, {"cookie_count": 2, "path": path})
                self.assertEqual(
                    nintendo_ec._load_account_cookies(), {"NASID": "s", "NATID": "t"}
                )

    async def test_set_epic_session_round_trips_through_importer_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "epic.json")
            with patch.dict(os.environ, {"EPIC_COOKIES_FILE": path}):
                result = await admin.set_epic_session(
                    json.dumps({"EPIC_BEARER_TOKEN": "tok", "EPIC_SSO_RM": "rm"})
                )
                self.assertEqual(result, {"cookie_count": 2, "path": path})
                self.assertEqual(
                    epic_orders._load_epic_cookies(),
                    {"EPIC_BEARER_TOKEN": "tok", "EPIC_SSO_RM": "rm"},
                )

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

    async def test_set_steam_refresh_session_writes_to_refresh_path(self):
        token = _make_refresh_token(sub="76561198000000000", exp=9999999999)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "steam_refresh_token.json")
            with patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path}):
                result = await admin.set_steam_refresh_session(
                    json.dumps({"steamRefresh_steam": token})
                )
                self.assertEqual(result, {"cookie_count": 1, "path": path})
                # The saved token round-trips through the session module's loader.
                self.assertEqual(steam_session._load_steam_refresh_token(), token)

    async def test_set_steam_store_session_accepts_store_audience(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "steam.json")
            value = _make_login_secure(["web:store"])
            with patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": path}):
                result = await admin.set_steam_store_session(
                    json.dumps({"steamLoginSecure": value, "sessionid": "s1"})
                )
            self.assertEqual(result["cookie_count"], 2)
            self.assertTrue(os.path.exists(path))

    async def test_set_steam_store_session_rejects_wrong_domain_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "steam.json")
            value = _make_login_secure(["web:community"])
            with (
                patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": path}),
                self.assertRaisesRegex(ToolError, "wrong Steam domain"),
            ):
                await admin.set_steam_store_session(
                    json.dumps({"steamLoginSecure": value})
                )
            # A known-bad cookie must never be written to disk.
            self.assertFalse(os.path.exists(path))

    async def test_set_steam_store_session_rejects_missing_login_secure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "steam.json")
            with (
                patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": path}),
                self.assertRaisesRegex(ToolError, "steamLoginSecure"),
            ):
                await admin.set_steam_store_session(
                    json.dumps({"sessionid": "s1", "browserid": "b"})
                )
            self.assertFalse(os.path.exists(path))

    async def test_set_steam_refresh_session_accepts_bare_token_value(self):
        # The user pastes the raw cookie value from DevTools, no JSON formatting.
        token = _make_refresh_token(sub="76561198000000000", exp=9999999999)
        raw = f"76561198000000000||{token}"  # steamRefresh_steam is steamid||JWT
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "steam_refresh_token.json")
            with patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path}):
                result = await admin.set_steam_refresh_session(raw)
                self.assertEqual(result["cookie_count"], 1)
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(json.load(f), {"steamRefresh_steam": raw})
                # The loader hands callers the bare JWT (steamid|| prefix stripped),
                # which is what finalizelogin's nonce requires.
                self.assertEqual(steam_session._load_steam_refresh_token(), token)

    async def test_load_steam_refresh_token_strips_steamid_prefix(self):
        token = _make_refresh_token(sub="76561198000000000", exp=9999999999)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "steam_refresh_token.json")
            with patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path}):
                # URL-encoded separator (%7C%7C) and object form both normalize.
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"steamRefresh_steam": f"76561198000000000%7C%7C{token}"}, f)
                self.assertEqual(steam_session._load_steam_refresh_token(), token)

    async def test_set_steam_store_session_accepts_bare_login_secure(self):
        raw = _make_login_secure(["web:store"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "steam.json")
            with patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": path}):
                result = await admin.set_steam_store_session(raw)
                self.assertEqual(result["cookie_count"], 1)
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(json.load(f), {"steamLoginSecure": raw})

    async def test_set_steam_refresh_session_rejects_missing_refresh_cookie(self):
        # The bug that bit the owner: pasting a store/community export (no
        # steamRefresh_steam) silently "saved" and then fell back to a dead cookie.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "steam_refresh_token.json")
            with (
                patch.dict(os.environ, {"STEAM_REFRESH_TOKEN_FILE": path}),
                self.assertRaisesRegex(ToolError, "steamRefresh_steam"),
            ):
                await admin.set_steam_refresh_session(
                    json.dumps(
                        {"steamLoginSecure": _make_login_secure(["web:store"])}
                    )
                )
            self.assertFalse(os.path.exists(path))

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


class SteamNPackTests(unittest.TestCase):
    def test_n_pack_suffix_stripped_in_records(self):
        # "Terraria 4-Pack" is 4 gift copies of ONE game, not a 4-game bundle:
        # the record must match (and book its full price against) Terraria.
        purchases = [
            {
                "date": "2015-06-20",
                "items": ["Terraria 4-Pack"],
                "total": 29.99,
                "currency": "USD",
            }
        ]
        records = steam_history._purchase_records(purchases)
        self.assertEqual(records[0].title, "Terraria")
        self.assertEqual(records[0].price_paid, 29.99)

    def test_strip_n_pack_variants(self):
        self.assertEqual(steam_history.strip_n_pack_suffix("Castle Crashers 4-pack"), "Castle Crashers")
        self.assertEqual(steam_history.strip_n_pack_suffix("Magicka 2 Pack"), "Magicka")
        self.assertEqual(steam_history.strip_n_pack_suffix("Left 4 Dead"), "Left 4 Dead")
        self.assertEqual(steam_history.strip_n_pack_suffix("LEGO Star Wars"), "LEGO Star Wars")


class ImportBundleDiversionTests(ToolDBTestCase):
    """Compilation-named purchases that miss every matching tier are diverted
    to bundles_needing_split — never minted as phantom base games, never
    buried in unmatched."""

    def _steam_record(self, title: str, price: float = 19.99) -> PurchaseRecord:
        return PurchaseRecord(
            title=title,
            platform="steam",
            purchase_source="steam",
            acquired_at="2014-06-20",
            price_paid=price,
            price_currency="EUR",
        )

    async def test_unmatched_compilation_names_divert(self):
        records = [
            self._steam_record("Hitman Collection"),
            self._steam_record("Far Cry Franchise Pack"),
            self._steam_record("Hexcells Complete"),
        ]
        with _patch_fetchers(
            fetch_steam_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["steam"])

        steam = result["sources"]["steam"]
        diverted = {b["bundle_name"] for b in steam["bundles_needing_split"]}
        self.assertEqual(
            diverted, {"Hitman Collection", "Far Cry Franchise Pack", "Hexcells Complete"}
        )
        self.assertEqual(steam["created"], 0)
        self.assertEqual(steam["unmatched"], [])
        self.assertEqual(result["totals"]["bundles_needing_split"], 3)
        # Nothing minted: the phantom "Hitman Collection" base game must not exist.
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 0)

    async def test_matched_compilation_name_is_not_diverted(self):
        gid = await seed_game("Halo: The Master Chief Collection")
        await add_platform(gid, "steam")
        records = [self._steam_record("Halo: The Master Chief Collection", 39.99)]
        with _patch_fetchers(
            fetch_steam_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["steam"])

        steam = result["sources"]["steam"]
        self.assertEqual(steam["bundles_needing_split"], [])
        self.assertEqual(steam["filled"], 1)


class ImportUnmatchedFreeSplitTests(ToolDBTestCase):
    async def test_zero_price_promo_misses_land_in_unmatched_free(self):
        records = [
            _eshop_record("Kingdom Come HD Pack", price_paid=0.0, purchase_source="free"),
            _eshop_record("Really Missing Paid Game", price_paid=12.5),
        ]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(
                sources=["eshop"], create_missing=False
            )

        eshop = result["sources"]["eshop"]
        self.assertEqual(
            [i["name"] for i in eshop["unmatched"]], ["Really Missing Paid Game"]
        )
        self.assertEqual(
            [i["name"] for i in eshop["unmatched_free"]], ["Kingdom Come HD Pack"]
        )
        self.assertEqual(result["totals"]["unmatched"], 1)
        self.assertEqual(result["totals"]["unmatched_free"], 1)


class ImportDryRunParityTests(ToolDBTestCase):
    async def test_dry_run_reports_same_counters_as_wet_run(self):
        gid = await seed_game("Hades")
        await add_platform(gid, "switch2")
        records = [
            _eshop_record("Hades"),
            _eshop_record("Totally Absent Game", price_paid=9.99),
        ]

        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            dry = await acquisition.import_purchases(
                sources=["eshop"], dry_run=True, create_missing=False
            )

        # Nothing written by the dry run.
        row = await _acquisition_row(gid, "switch2")
        self.assertIsNone(row["price_paid"])

        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            wet = await acquisition.import_purchases(
                sources=["eshop"], create_missing=False
            )

        for key in ("filled", "created", "no_change", "applied"):
            self.assertEqual(
                dry["sources"]["eshop"][key], wet["sources"]["eshop"][key], key
            )
        self.assertEqual(
            [i["name"] for i in dry["sources"]["eshop"]["unmatched"]],
            [i["name"] for i in wet["sources"]["eshop"]["unmatched"]],
        )
        self.assertEqual(dry["totals"]["unmatched"], wet["totals"]["unmatched"])

    async def test_dry_run_create_missing_previews_created(self):
        records = [_eshop_record("Brand New Game")]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"], dry_run=True)

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["created"], 1)
        self.assertEqual(eshop["would_create"][0]["name"], "Brand New Game")
        self.assertIsNone(eshop["would_create"][0]["game_id"])
        # Preview minted nothing.
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 0)


class HumbleSteamAppidMatchingTests(ToolDBTestCase):
    """The appid on a Humble key resolves what its title cannot (issue: Humble
    Choice acquisitions landing on the wrong row, or on none)."""

    @staticmethod
    def _humble_record(title, appid, **overrides):
        fields = {
            "title": title,
            "platform": "steam",
            "purchase_source": "subscription",
            "acquired_at": "2023-04-04",
            "price_paid": 1.34,
            "price_currency": "USD",
            "store_identifier": appid,
        }
        fields.update(overrides)
        return PurchaseRecord(**fields)

    async def test_edition_named_key_lands_on_the_row_holding_the_steam_copy(self):
        # The real shape this was found in. The library holds TWO rows for the
        # game: "Disco Elysium" (which owns the Steam copy) and "Disco Elysium:
        # The Final Cut" (epic/switch only), and the second carries the exact
        # alias Humble's key is titled with. Matching by name/alias therefore
        # landed the Steam acquisition on the row with no Steam platform row,
        # where — imports not creating platform rows — it was silently
        # discarded. The appid points at the right row regardless.
        steam_row_game = await seed_game("Disco Elysium")
        gp = await add_platform(steam_row_game, "steam", playtime_minutes=3)
        await add_identifier(gp, db_module.STEAM_APP_ID, "632470")

        edition_row = await seed_game("Disco Elysium: The Final Cut")
        await add_platform(edition_row, "epic")
        await add_game_alias(edition_row, "Disco Elysium - The Final Cut")

        records = [self._humble_record("Disco Elysium - The Final Cut", "632470")]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["humble"])

        humble = result["sources"]["humble"]
        self.assertEqual(humble["filled"], 1)
        self.assertEqual(humble["no_platform_row"], 0)
        self.assertEqual(humble["created"], 0)

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                """SELECT game_id, acquired_at, price_paid, purchase_source
                   FROM game_platforms WHERE platform = 'steam'""",
            )
        self.assertEqual(row["game_id"], steam_row_game)
        self.assertEqual(row["acquired_at"], "2023-04-04")
        self.assertEqual(row["purchase_source"], "subscription")

    async def test_appid_beats_a_title_the_library_never_carried(self):
        # "DEATH STRANDING DIRECTOR'S CUT" against a row named "DEATH
        # STRANDING": the appid makes the match exact instead of relying on
        # edition-suffix stripping.
        game_id = await seed_game("DEATH STRANDING")
        gp = await add_platform(game_id, "steam", playtime_minutes=513)
        await add_identifier(gp, db_module.STEAM_APP_ID, "1850570")

        records = [self._humble_record("DEATH STRANDING DIRECTOR'S CUT", "1850570")]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["humble"])

        humble = result["sources"]["humble"]
        self.assertEqual((humble["filled"], humble["created"]), (1, 0))
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT price_paid, purchase_source FROM game_platforms "
                "WHERE game_id = ? AND platform = 'steam'",
                (game_id,),
            )
        self.assertEqual(row["price_paid"], 1.34)
        self.assertEqual(row["purchase_source"], "subscription")

    async def test_appid_miss_still_falls_through_to_name_matching(self):
        # A first import can predate the sync that attaches the appid, so an
        # identifier miss must not be terminal.
        game_id = await seed_game("Revita")
        await add_platform(game_id, "steam")

        records = [self._humble_record("Revita", "1124260")]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["humble"])

        self.assertEqual(result["sources"]["humble"]["filled"], 1)


class RestrictedRecordTests(ToolDBTestCase):
    """A record the source can't confirm was granted fills a sync-confirmed
    row but never mints one (PurchaseRecord.mint_allowed)."""

    @staticmethod
    def _rec(title, *, mint_allowed, **overrides):
        fields = {
            "title": title,
            "platform": "steam",
            "purchase_source": "subscription",
            "acquired_at": "2023-04-04",
            "price_paid": 1.07,
            "price_currency": "USD",
            "mint_allowed": mint_allowed,
        }
        fields.update(overrides)
        return PurchaseRecord(**fields)

    async def test_restricted_record_fills_a_row_the_sync_confirmed(self):
        # Death Stranding: the Steam sync returns it with playtime, so
        # ownership is established independently of anything Humble says.
        # The purchase is then the best provenance available for that row.
        game_id = await seed_game("DEATH STRANDING")
        gp = await add_platform(
            game_id, "steam", playtime_minutes=513, from_source=True
        )
        await add_identifier(gp, db_module.STEAM_APP_ID, "1850570")

        records = [
            self._rec(
                "DEATH STRANDING DIRECTOR'S CUT",
                mint_allowed=False,
                store_identifier="1850570",
            )
        ]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["humble"])

        humble = result["sources"]["humble"]
        self.assertEqual((humble["filled"], humble["created"]), (1, 0))
        self.assertEqual(humble["unconfirmed_unmatched"], [])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT acquired_at, price_paid, purchase_source FROM game_platforms "
                "WHERE game_id = ? AND platform = 'steam'",
                (game_id,),
            )
        self.assertEqual(row["acquired_at"], "2023-04-04")
        self.assertEqual(row["purchase_source"], "subscription")

    async def test_restricted_record_never_mints_a_game(self):
        # The vault: a key for a game the account does not have. No row, no
        # mint, and reported apart from real unmatched spend.
        records = [self._rec("Some Game Never Redeemed", mint_allowed=False)]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["humble"])

        humble = result["sources"]["humble"]
        self.assertEqual((humble["created"], humble["filled"]), (0, 0))
        self.assertEqual(humble["unmatched"], [])
        self.assertEqual(
            [i["name"] for i in humble["unconfirmed_unmatched"]],
            ["Some Game Never Redeemed"],
        )
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 0)

    async def test_restricted_record_never_mints_a_platform_row(self):
        # The game exists but the Steam sync has never returned it — that
        # missing row is the evidence the key was not redeemed.
        game_id = await seed_game("Owned On GOG Only")
        await add_platform(game_id, "gog", from_source=True)

        records = [self._rec("Owned On GOG Only", mint_allowed=False)]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(
                sources=["humble"], create_platform_rows=True
            )

        humble = result["sources"]["humble"]
        self.assertEqual(humble["created"], 0)
        self.assertEqual(
            [i["name"] for i in humble["unconfirmed_unmatched"]], ["Owned On GOG Only"]
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM game_platforms WHERE platform = 'steam'"
            )
        self.assertEqual(row["c"], 0)

    async def test_restricted_record_will_not_legitimize_an_unconfirmed_row(self):
        # The phantom shape: a steam row exists, but only because an earlier
        # import minted it — no ownership sync has ever returned it
        # (last_seen_in_source IS NULL). Switching creation off does not
        # catch this; only the source stamp does. Filling it would let an
        # unrevealed key vouch for the very rows the restriction exists to
        # stop being created.
        game_id = await seed_game("Never Confirmed By Steam")
        await add_platform(game_id, "steam")  # minted, not sync-returned

        records = [self._rec("Never Confirmed By Steam", mint_allowed=False)]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["humble"])

        humble = result["sources"]["humble"]
        self.assertEqual((humble["filled"], humble["applied"]), (0, 0))
        self.assertEqual(
            [i["name"] for i in humble["unconfirmed_unmatched"]],
            ["Never Confirmed By Steam"],
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT acquired_at, purchase_source FROM game_platforms "
                "WHERE game_id = ? AND platform = 'steam'",
                (game_id,),
            )
        self.assertIsNone(row["acquired_at"])
        self.assertIsNone(row["purchase_source"])

    async def test_restricted_bundle_is_withheld_from_the_split_handoff(self):
        # split_bundle_acquisition creates a platform row for every matched
        # constituent unconditionally, so handing it a key that was never
        # redeemed would mint ownership for the whole SKU by way of the
        # documented workflow.
        records = [
            self._rec("Game A, Game B, and Game C", mint_allowed=False, is_bundle=True),
            self._rec("Real Bundle, Other, and More", mint_allowed=True, is_bundle=True),
        ]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["humble"])

        humble = result["sources"]["humble"]
        self.assertEqual(
            [b["bundle_name"] for b in humble["bundles_needing_split"]],
            ["Real Bundle, Other, and More"],
        )
        self.assertEqual(
            [i["name"] for i in humble["unconfirmed_unmatched"]],
            ["Game A, Game B, and Game C"],
        )

    async def test_dry_run_echo_includes_restricted_items(self):
        # Restricted items move the counters, so a preview that omitted them
        # reported mutations its own proposal could not explain.
        game_id = await seed_game("Confirmed Game")
        await add_platform(game_id, "steam", from_source=True)

        records = [self._rec("Confirmed Game", mint_allowed=False)]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(
                sources=["humble"], dry_run=True
            )

        humble = result["sources"]["humble"]
        self.assertEqual(humble["filled"], 1)
        self.assertEqual([i["name"] for i in humble["proposed"]], ["Confirmed Game"])
        self.assertFalse(humble["truncated"])

    async def test_unrestricted_records_still_mint(self):
        records = [self._rec("Brand New Game", mint_allowed=True)]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["humble"])

        self.assertEqual(result["sources"]["humble"]["created"], 1)


class CreateMissingQualityTests(ToolDBTestCase):
    """Refusals that keep create_missing from minting junk rows (BUG-9)."""

    async def test_edition_sibling_is_not_minted(self):
        await seed_game("STRAFE: Gold Edition")
        records = [_eshop_record("STRAFE: Millennium Edition", price_paid=4.99)]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["created"], 0)
        self.assertEqual([i["name"] for i in eshop["unmatched"]],
                         ["STRAFE: Millennium Edition"])
        refusal = eshop["create_refused_details"][0]
        self.assertEqual(refusal["reason"], "edition_variant")
        self.assertEqual(refusal["existing_name"], "STRAFE: Gold Edition")

    async def test_plus_decorated_edition_suffix_is_not_minted(self):
        # Bug-report §3: "Digital+ Edition" wasn't a recognized suffix shape,
        # so the guard that refused "STRAFE: Millennium Edition" minted
        # "Marvel's Midnight Suns Digital+ Edition" beside the base row.
        existing = await seed_game("Marvel's Midnight Suns")
        records = [
            _eshop_record("Marvel's Midnight Suns Digital+ Edition", price_paid=19.99)
        ]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["created"], 0)
        refusal = eshop["create_refused_details"][0]
        self.assertEqual(refusal["reason"], "edition_variant")
        self.assertEqual(refusal["existing_game_id"], existing)

    async def test_alias_of_an_existing_row_matches_instead_of_minting(self):
        # Bug-report §4 (Orwell): the storefront's full title is an alias of an
        # existing row. The matcher's alias tier must ATTACH the purchase to
        # that row — not mint a duplicate, and not merely refuse the mint and
        # strand the spend in unmatched.
        existing = await seed_game("Orwell")
        await add_platform(existing, "switch2")
        await db_module.upsert_game_alias(
            existing, "Orwell: Keeping an Eye On You", alias_type="igdb"
        )
        records = [_eshop_record("Orwell: Keeping an Eye On You", price_paid=2.99)]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["created"], 0)
        self.assertEqual(eshop["unmatched"], [])
        self.assertEqual(eshop["filled"], 1)
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
            acq = await db.execute_fetchone(
                """SELECT gp.price_paid FROM game_platforms gp
                   WHERE gp.game_id = ? AND gp.platform = 'switch2'""",
                (existing,),
            )
        self.assertEqual(count["c"], 1)
        self.assertEqual(acq["price_paid"], 2.99)

    async def test_alias_of_a_platformless_row_is_still_not_minted(self):
        # Same alias shape, but the matched row has no platform row and
        # create_platform_rows is off: the item must land in no_platform_row —
        # visible for triage — and still never mint a duplicate.
        existing = await seed_game("Orwell")
        await db_module.upsert_game_alias(
            existing, "Orwell: Keeping an Eye On You", alias_type="igdb"
        )
        records = [_eshop_record("Orwell: Keeping an Eye On You", price_paid=2.99)]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["created"], 0)
        self.assertEqual(eshop["no_platform_row"], 1)
        self.assertEqual(
            eshop["no_platform_row_details"][0]["game_id"], existing
        )
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 1)

    async def test_parentless_nested_purchase_is_not_minted(self):
        records = [
            _eshop_record(
                "The Binding of Isaac + Wrath of the Lamb DLC",
                price_paid=3.99,
                content_type="dlc",
            )
        ]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["created"], 0)
        self.assertEqual(
            eshop["create_refused_details"][0]["reason"], "nested_without_parent"
        )
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 0)

    async def test_nested_purchase_with_a_parent_still_mints(self):
        base = await seed_game("Hollow Knight")
        await add_platform(base, "switch2")
        records = [
            _eshop_record(
                "Hollow Knight: Silksong Season Pass",
                price_paid=3.99,
                content_type="dlc",
            )
        ]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["created"], 1)
        self.assertEqual(eshop["create_refused_details"], [])
        self.assertEqual(eshop["created_details"][0]["parent_game_id"], base)

    async def test_marketing_tagline_matches_the_existing_game(self):
        rive = await seed_game("RIVE")
        await add_platform(rive, "switch2")
        records = [_eshop_record("RIVE: Wreck, Hack, Die, Retry", price_paid=1.99)]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["eshop"])

        eshop = result["sources"]["eshop"]
        self.assertEqual(eshop["created"], 0)
        self.assertEqual(eshop["unmatched"], [])
        self.assertEqual(eshop["filled"], 1)
        row = await _acquisition_row(rive, "switch2")
        self.assertEqual(row["price_paid"], 1.99)

    async def test_subtitled_dlc_is_not_treated_as_a_tagline(self):
        # The tagline rule needs an enumeration in the tail — a real subtitle
        # must never collapse a DLC's spend onto the base game.
        base = await seed_game("Half-Life 2")
        await add_platform(base, "switch2")
        records = [_eshop_record("Half-Life 2: Episode One", price_paid=4.99)]
        with _patch_fetchers(
            fetch_eshop_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(
                sources=["eshop"], create_missing=False
            )

        self.assertEqual(
            [i["name"] for i in result["sources"]["eshop"]["unmatched"]],
            ["Half-Life 2: Episode One"],
        )
        self.assertEqual(await _acquisition_row(base, "switch2"), {
            **await _acquisition_row(base, "switch2")
        })
        self.assertIsNone((await _acquisition_row(base, "switch2"))["price_paid"])


class BundleDeduplicationTests(ToolDBTestCase):
    async def test_same_bundle_on_two_platforms_collapses_to_one_entry(self):
        # BUG-10: a Humble order with a Steam key and an Android ("other") key
        # surfaced the same bundle twice, and the "other" half could never
        # become already_recorded.
        records = [
            PurchaseRecord(
                title="Humble Indie Bundle #3",
                platform=platform,
                purchase_source="humble",
                acquired_at="2011-08-04",
                price_paid=1.13,
                price_currency="USD",
                is_bundle=True,
            )
            for platform in ("steam", "other")
        ]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["humble"])

        bundles = result["sources"]["humble"]["bundles_needing_split"]
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0]["platform"], "steam")
        self.assertEqual(bundles[0]["platforms"], ["other", "steam"])
        self.assertEqual(result["totals"]["bundles_needing_split"], 1)

    async def test_already_recorded_is_platform_agnostic(self):
        gid = await seed_game("Trine")
        await add_platform(gid, "steam")
        await acquisition.split_bundle_acquisition(
            bundle_name="Humble Frozenbyte Bundle",
            platform="steam",
            games=[{"game_id": gid}],
            total_price=1.00,
        )
        records = [
            PurchaseRecord(
                title="Humble Frozenbyte Bundle",
                platform="other",
                purchase_source="humble",
                acquired_at="2011-09-30",
                price_paid=1.00,
                price_currency="USD",
                is_bundle=True,
            )
        ]
        with _patch_fetchers(
            fetch_humble_purchases=AsyncMock(return_value=(records, [])),
        ):
            result = await acquisition.import_purchases(sources=["humble"])

        bundle = result["sources"]["humble"]["bundles_needing_split"][0]
        self.assertTrue(bundle["already_recorded"])
