"""Tests for pushing an appid to the real Steam wishlist (issue #110 phase 2).

No DB needed — these exercise only the two HTTP write routes in
steam_wishlist.push_to_steam_wishlist, fully mocked via httpx.MockTransport,
following the same style as SteamSessionMintTests in test_purchase_importers.py
(plain unittest.IsolatedAsyncioTestCase, no pytest-asyncio in this repo).
"""

import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx

from gamelib_mcp.data import steam_session, steam_wishlist

# steamid || JWT, with the JWT segment used verbatim in assertions below.
_JWT = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiIxIn0.sig"
COOKIES = {
    "steamLoginSecure": "76561198000000000%7C%7C" + _JWT,
    "sessionid": "aabbccdd",
}
COOKIES_NO_SESSIONID = {"steamLoginSecure": COOKIES["steamLoginSecure"]}

_WEBAPI_PATH = "/IWishlistService/AddToWishlist/v1/"
_STOREFRONT_PATH = "/api/addtowishlist"


class ExtractWebAccessTokenTests(unittest.TestCase):
    def test_plain_separator(self):
        self.assertEqual(
            steam_session.extract_web_access_token(f"76561198000000000||{_JWT}"), _JWT
        )

    def test_url_encoded_separator(self):
        self.assertEqual(
            steam_session.extract_web_access_token(f"76561198000000000%7C%7C{_JWT}"), _JWT
        )

    def test_bare_jwt_passthrough(self):
        self.assertEqual(steam_session.extract_web_access_token(_JWT), _JWT)


def _form(request: httpx.Request) -> dict[str, str]:
    """Decode an application/x-www-form-urlencoded request body to {k: v}."""
    parsed = parse_qs(request.content.decode())
    return {k: v[0] for k, v in parsed.items()}


class PushToSteamWishlistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The minted-session cache is module-level state; without a reset, one
        # test's cookies would leak into the next and bypass its patched loader.
        steam_wishlist._invalidate_session_cache()
        self.addCleanup(steam_wishlist._invalidate_session_cache)

    def _patched_cookies(self, cookies: dict[str, str] = COOKIES):
        return patch.object(
            steam_wishlist, "load_steam_web_cookies", AsyncMock(return_value=cookies)
        )

    async def test_webapi_route_success_storefront_never_called(self):
        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            if request.url.path == _WEBAPI_PATH:
                self.assertEqual(request.url.host, "api.steampowered.com")
                self.assertEqual(request.url.params.get("access_token"), _JWT)
                self.assertEqual(_form(request).get("appid"), "730")
                return httpx.Response(200, json={"response": {"wishlist_count": 42}})
            raise AssertionError(f"unexpected request: {request.url}")

        with self._patched_cookies():
            result = await steam_wishlist.push_to_steam_wishlist(
                730, transport=httpx.MockTransport(handler)
            )

        self.assertEqual(result, {"appid": 730, "via": "webapi", "wishlist_count": 42})
        self.assertNotIn(_STOREFRONT_PATH, seen_paths)

    async def test_webapi_401_falls_back_to_storefront_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == _WEBAPI_PATH:
                return httpx.Response(401)
            if request.url.path == _STOREFRONT_PATH:
                self.assertEqual(request.url.host, "store.steampowered.com")
                cookie_header = request.headers.get("cookie", "")
                self.assertIn(f"steamLoginSecure={COOKIES['steamLoginSecure']}", cookie_header)
                self.assertIn("sessionid=aabbccdd", cookie_header)
                form = _form(request)
                self.assertEqual(form.get("sessionid"), "aabbccdd")
                self.assertEqual(form.get("appid"), "730")
                return httpx.Response(200, json={"success": True, "wishlistCount": 7})
            raise AssertionError(f"unexpected request: {request.url}")

        with self._patched_cookies():
            result = await steam_wishlist.push_to_steam_wishlist(
                730, transport=httpx.MockTransport(handler)
            )

        self.assertEqual(result, {"appid": 730, "via": "storefront", "wishlist_count": 7})

    async def test_both_routes_auth_rejected_mentions_steam_refresh(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == _WEBAPI_PATH:
                return httpx.Response(401)
            if request.url.path == _STOREFRONT_PATH:
                return httpx.Response(200, json={"success": False, "wishlistCount": 0})
            raise AssertionError(f"unexpected request: {request.url}")

        with (
            self._patched_cookies(),
            self.assertRaises(steam_wishlist.SteamWishlistPushError) as ctx,
        ):
            await steam_wishlist.push_to_steam_wishlist(
                730, transport=httpx.MockTransport(handler)
            )

        self.assertIn("steam_refresh", str(ctx.exception))

    async def test_no_session_configured_preserves_message_verbatim(self):
        message = "No Steam session is configured — go paste a fresh cookie"
        with patch.object(
            steam_wishlist,
            "load_steam_web_cookies",
            AsyncMock(side_effect=RuntimeError(message)),
        ), self.assertRaises(steam_wishlist.SteamWishlistPushError) as ctx:
            await steam_wishlist.push_to_steam_wishlist(730)

        self.assertEqual(str(ctx.exception), message)

    async def test_both_routes_network_failure_is_transient_not_reingest(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with (
            self._patched_cookies(),
            self.assertRaises(steam_wishlist.SteamWishlistPushError) as ctx,
        ):
            await steam_wishlist.push_to_steam_wishlist(
                730, transport=httpx.MockTransport(handler)
            )

        message = str(ctx.exception)
        self.assertIn("retry", message.lower())
        self.assertNotIn("steam_refresh", message)

    async def test_missing_sessionid_cookie_self_mints_matching_pair(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == _WEBAPI_PATH:
                return httpx.Response(401)
            if request.url.path == _STOREFRONT_PATH:
                cookie_header = request.headers.get("cookie", "")
                form = _form(request)
                minted = form.get("sessionid")
                self.assertIsNotNone(minted)
                self.assertIn(f"sessionid={minted}", cookie_header)
                return httpx.Response(200, json={"success": True, "wishlistCount": 1})
            raise AssertionError(f"unexpected request: {request.url}")

        with self._patched_cookies(COOKIES_NO_SESSIONID):
            result = await steam_wishlist.push_to_steam_wishlist(
                730, transport=httpx.MockTransport(handler)
            )

        self.assertEqual(result["via"], "storefront")

    async def test_session_cache_reuses_one_mint_across_pushes(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == _WEBAPI_PATH:
                return httpx.Response(200, json={"response": {}})
            raise AssertionError(f"unexpected request: {request.url}")

        with self._patched_cookies() as mock_load:
            transport = httpx.MockTransport(handler)
            await steam_wishlist.push_to_steam_wishlist(730, transport=transport)
            await steam_wishlist.push_to_steam_wishlist(440, transport=transport)

        # The whole point of the cache: a batch of pushes authenticates once.
        self.assertEqual(mock_load.await_count, 1)

    async def test_auth_rejected_drops_session_cache(self):
        fail_auth = False

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == _WEBAPI_PATH:
                if fail_auth:
                    return httpx.Response(401)
                return httpx.Response(200, json={"response": {}})
            if request.url.path == _STOREFRONT_PATH:
                return httpx.Response(200, json={"success": False})
            raise AssertionError(f"unexpected request: {request.url}")

        with self._patched_cookies() as mock_load:
            transport = httpx.MockTransport(handler)
            await steam_wishlist.push_to_steam_wishlist(730, transport=transport)  # mints
            fail_auth = True
            with self.assertRaises(steam_wishlist.SteamWishlistPushError):
                await steam_wishlist.push_to_steam_wishlist(440, transport=transport)
            fail_auth = False
            await steam_wishlist.push_to_steam_wishlist(570, transport=transport)  # re-mints

        # Mint 1 served pushes 1-2; the auth rejection dropped the cache, so
        # push 3 minted again. A transient failure would NOT have (see below).
        self.assertEqual(mock_load.await_count, 2)

    async def test_transient_failure_keeps_session_cache(self):
        fail_network = False

        def handler(request: httpx.Request) -> httpx.Response:
            if fail_network:
                raise httpx.ConnectError("connection refused", request=request)
            if request.url.path == _WEBAPI_PATH:
                return httpx.Response(200, json={"response": {}})
            raise AssertionError(f"unexpected request: {request.url}")

        with self._patched_cookies() as mock_load:
            transport = httpx.MockTransport(handler)
            await steam_wishlist.push_to_steam_wishlist(730, transport=transport)
            fail_network = True
            with self.assertRaises(steam_wishlist.SteamWishlistPushError):
                await steam_wishlist.push_to_steam_wishlist(440, transport=transport)
            fail_network = False
            await steam_wishlist.push_to_steam_wishlist(570, transport=transport)

        # A network blip says nothing about the session — no re-mint.
        self.assertEqual(mock_load.await_count, 1)


if __name__ == "__main__":
    unittest.main()
