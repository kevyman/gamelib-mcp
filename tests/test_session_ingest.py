"""Tests for the single-use session-cookie ingest links (/ingest/{nonce})."""

import json
import os
import shutil
import tempfile
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from fastmcp.exceptions import ToolError
from starlette.requests import Request

from gamelib_mcp import session_ingest
from gamelib_mcp.http_admin import HttpSecurityMiddleware
from gamelib_mcp.main import mcp


def _get_route(path: str):
    for route in mcp._additional_http_routes:
        if route.path == path:
            return route
    raise AssertionError(f"Route {path} is not registered")


def _get_request(nonce: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/ingest/{nonce}",
            "path_params": {"nonce": nonce},
            "headers": [],
        }
    )


def _post_request(nonce: str, body: bytes, content_length: str | None = None) -> Request:
    length = content_length if content_length is not None else str(len(body))
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/ingest/{nonce}",
        "path_params": {"nonce": nonce},
        "headers": [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", length.encode()),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _form_body(cookies: object) -> bytes:
    return urlencode({"cookies": json.dumps(cookies)}).encode()


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SessionIngestTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        session_ingest._ingest_links.clear()
        self.clock = _FakeClock()
        patcher = patch(
            "gamelib_mcp.session_ingest.time.monotonic",
            side_effect=self.clock.monotonic,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(session_ingest._ingest_links.clear)


class MintIngestLinkTests(SessionIngestTestCase):
    def test_url_built_from_public_base_url(self) -> None:
        with patch.dict(os.environ, {"MCP_PUBLIC_BASE_URL": "https://gamelib.example/"}):
            result = session_ingest.mint_ingest_link("humble")
        self.assertEqual(result["provider"], "humble")
        self.assertEqual(result["expires_in_minutes"], 15)
        self.assertTrue(result["url"].startswith("https://gamelib.example/ingest/"))
        nonce = result["url"].rsplit("/", 1)[1]
        self.assertIn(nonce, session_ingest._ingest_links)

    def test_localhost_fallback_without_public_base_url(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "MCP_PUBLIC_BASE_URL"}
        env["PORT"] = "9999"
        with patch.dict(os.environ, env, clear=True):
            result = session_ingest.mint_ingest_link("nintendo")
        self.assertTrue(result["url"].startswith("http://localhost:9999/ingest/"))

    def test_unknown_provider_rejected(self) -> None:
        with self.assertRaisesRegex(ToolError, "Unknown provider 'psn'"):
            session_ingest.mint_ingest_link("psn")
        with self.assertRaisesRegex(ToolError, "set_nintendo_pctl_session"):
            session_ingest.mint_ingest_link("nintendo_pctl")

    def test_pending_links_capped(self) -> None:
        for _ in range(session_ingest._MAX_PENDING_LINKS + 3):
            self.clock.advance(1)  # distinct expiries so eviction order is stable
            session_ingest.mint_ingest_link("humble")
        self.assertEqual(
            len(session_ingest._ingest_links), session_ingest._MAX_PENDING_LINKS
        )


class IngestRouteTests(SessionIngestTestCase):
    def _mint(self, provider: str = "humble") -> str:
        with patch.dict(os.environ, {"MCP_PUBLIC_BASE_URL": "https://gamelib.example"}):
            return session_ingest.mint_ingest_link(provider)["url"].rsplit("/", 1)[1]

    def test_route_registered_for_get_and_post(self) -> None:
        route = _get_route("/ingest/{nonce}")
        self.assertLessEqual({"GET", "POST"}, set(route.methods))

    async def test_get_valid_nonce_shows_form(self) -> None:
        nonce = self._mint()
        response = await session_ingest.handle_ingest_request(_get_request(nonce))
        self.assertEqual(response.status_code, 200)
        body = response.body.decode()
        self.assertIn("Humble Bundle", body)
        self.assertIn('name="cookies"', body)
        self.assertIn('autocomplete="off"', body)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    async def test_get_unknown_nonce_is_generic_404(self) -> None:
        response = await session_ingest.handle_ingest_request(_get_request("nope"))
        self.assertEqual(response.status_code, 404)
        self.assertIn("invalid or has expired", response.body.decode())

    async def test_get_expired_nonce_is_404_and_pruned(self) -> None:
        nonce = self._mint()
        self.clock.advance(session_ingest._INGEST_TTL_SECONDS + 1)
        response = await session_ingest.handle_ingest_request(_get_request(nonce))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(session_ingest._ingest_links, {})

    async def test_post_valid_saves_and_consumes_nonce(self) -> None:
        nonce = self._mint("humble")
        cookies_path = os.path.join(self._tmp_dir(), "humble.json")
        export = [
            {"name": "_simpleauth_sess", "value": "sekrit-value", "domain": "x"},
            {"name": "csrf_cookie", "value": "other"},
        ]
        with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": cookies_path}):
            response = await session_ingest.handle_ingest_request(
                _post_request(nonce, _form_body(export))
            )
        self.assertEqual(response.status_code, 200)
        body = response.body.decode()
        self.assertIn("saved 2 cookies", body)
        # The page must never echo submitted cookie values.
        self.assertNotIn("sekrit-value", body)
        with open(cookies_path, encoding="utf-8") as f:
            self.assertEqual(
                json.load(f),
                {"_simpleauth_sess": "sekrit-value", "csrf_cookie": "other"},
            )

        # Single-use: the same nonce is now indistinguishable from unknown.
        second = await session_ingest.handle_ingest_request(
            _post_request(nonce, _form_body({"a": "b"}))
        )
        self.assertEqual(second.status_code, 404)
        get_again = await session_ingest.handle_ingest_request(_get_request(nonce))
        self.assertEqual(get_again.status_code, 404)
        unknown = await session_ingest.handle_ingest_request(_get_request("never-existed"))
        self.assertEqual(unknown.body, get_again.body)

    async def test_post_invalid_json_keeps_nonce_alive(self) -> None:
        nonce = self._mint("humble")
        cookies_path = os.path.join(self._tmp_dir(), "humble.json")
        with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": cookies_path}):
            bad = await session_ingest.handle_ingest_request(
                _post_request(nonce, urlencode({"cookies": "not json"}).encode())
            )
            self.assertEqual(bad.status_code, 400)
            self.assertIn("Invalid JSON", bad.body.decode())
            self.assertFalse(os.path.exists(cookies_path))

            good = await session_ingest.handle_ingest_request(
                _post_request(nonce, _form_body({"_simpleauth_sess": "v"}))
            )
        self.assertEqual(good.status_code, 200)
        self.assertTrue(os.path.exists(cookies_path))

    async def test_post_empty_cookies_field_is_400(self) -> None:
        nonce = self._mint()
        response = await session_ingest.handle_ingest_request(
            _post_request(nonce, urlencode({"cookies": "   "}).encode())
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(nonce, session_ingest._ingest_links)

    async def test_post_oversized_body_is_413(self) -> None:
        nonce = self._mint("humble")
        cookies_path = os.path.join(self._tmp_dir(), "humble.json")
        with patch.dict(os.environ, {"HUMBLE_COOKIES_FILE": cookies_path}):
            response = await session_ingest.handle_ingest_request(
                _post_request(
                    nonce,
                    _form_body({"a": "b"}),
                    content_length=str(session_ingest._MAX_BODY_BYTES + 1),
                )
            )
        self.assertEqual(response.status_code, 413)
        self.assertFalse(os.path.exists(cookies_path))
        self.assertIn(nonce, session_ingest._ingest_links)

    async def test_provider_dispatch_writes_matching_file(self) -> None:
        nonce = self._mint("steam_store")
        cookies_path = os.path.join(self._tmp_dir(), "steam.json")
        with patch.dict(os.environ, {"STEAM_STORE_COOKIES_FILE": cookies_path}):
            response = await session_ingest.handle_ingest_request(
                _post_request(nonce, _form_body({"steamLoginSecure": "tok"}))
            )
        self.assertEqual(response.status_code, 200)
        with open(cookies_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"steamLoginSecure": "tok"})

    def _tmp_dir(self) -> str:
        tmp = tempfile.mkdtemp(prefix="ingest-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp


class IngestMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    """Pins the security-boundary behavior: /ingest/* needs no bearer token,
    but the origin allowlist still applies (browser form POSTs send Origin,
    so local disabled-auth mode must allowlist http://localhost:PORT)."""

    def _app(self, allowed_origins: frozenset[str] = frozenset()):
        self.called = False

        async def sentinel(scope, receive, send):
            self.called = True
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        return HttpSecurityMiddleware(
            sentinel, admin_token="secret-token", allowed_origins=allowed_origins
        )

    async def _invoke(self, app, headers: list[tuple[bytes, bytes]]) -> int:
        events: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            events.append(message)

        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/ingest/abc",
                "query_string": b"",
                "headers": headers,
            },
            receive,
            send,
        )
        return next(m for m in events if m["type"] == "http.response.start")["status"]

    async def test_no_bearer_required_outside_admin(self) -> None:
        status = await self._invoke(self._app(), headers=[])
        self.assertTrue(self.called)
        self.assertEqual(status, 204)

    async def test_disallowed_origin_still_403(self) -> None:
        status = await self._invoke(
            self._app(), headers=[(b"origin", b"https://evil.example")]
        )
        self.assertFalse(self.called)
        self.assertEqual(status, 403)

    async def test_allowlisted_origin_passes(self) -> None:
        status = await self._invoke(
            self._app(frozenset({"http://localhost:8000"})),
            headers=[(b"origin", b"http://localhost:8000")],
        )
        self.assertTrue(self.called)
        self.assertEqual(status, 204)
