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
    return urlencode({"payload": json.dumps(cookies)}).encode()


def _raw_body(text: str) -> bytes:
    """A submission that is not a cookie export (the nintendo_pctl flow)."""
    return urlencode({"payload": text}).encode()


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
        self.assertIn('name="payload"', body)
        self.assertIn('autocomplete="off"', body)
        self.assertEqual(response.headers["cache-control"], "no-store")
        # same-origin (not no-referrer): a no-referrer form page makes the browser
        # POST with Origin: null, which the security middleware rejects; same-origin
        # sends the real, allowlisted Origin without leaking the nonce cross-origin.
        self.assertEqual(response.headers["referrer-policy"], "same-origin")

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
                _post_request(nonce, urlencode({"payload": "not json"}).encode())
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
            _post_request(nonce, urlencode({"payload": "   "}).encode())
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


class _FakeAuthenticator:
    """Stands in for pynintendoparental's Authenticator (no network, no PKCE)."""

    instances: list["_FakeAuthenticator"] = []
    fail_with: Exception | None = None

    def __init__(self, client_session=None) -> None:
        self._auth_code_verifier = f"verifier-{len(self.instances)}"
        self.login_url = f"https://accounts.nintendo.com/authorize?state=s{len(self.instances)}"
        self._session_token: str | None = None
        self.completed_with: str | None = None
        type(self).instances.append(self)

    async def async_complete_login(self, response_token: str) -> None:
        if type(self).fail_with is not None:
            raise type(self).fail_with
        self.completed_with = response_token
        self._session_token = "pctl-session-token"


class NintendoPctlIngestTests(SessionIngestTestCase):
    """The Parental Controls login runs through the same single-use link.

    It is not a cookie paste — the npf:// link the user copies carries a
    one-time code redeemable for a long-lived token, so it gets the same
    never-in-the-chat treatment (ADR 0004's fourth amendment).
    """

    NPF_LINK = "npf54789befb391a838://auth#session_token_code=SECRET-CODE&state=s0"

    def setUp(self) -> None:
        super().setUp()
        _FakeAuthenticator.instances = []
        _FakeAuthenticator.fail_with = None
        patcher = patch(
            "pynintendoparental.authenticator.Authenticator", _FakeAuthenticator
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token_path = os.path.join(self._tmp_dir(), "pctl.json")
        env = patch.dict(os.environ, {"NINTENDO_PCTL_SESSION_FILE": self.token_path})
        env.start()
        self.addCleanup(env.stop)

    def _tmp_dir(self) -> str:
        tmp = tempfile.mkdtemp(prefix="pctl-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def _mint(self) -> str:
        with patch.dict(os.environ, {"MCP_PUBLIC_BASE_URL": "https://gamelib.example"}):
            return session_ingest.mint_ingest_link("nintendo_pctl")["url"].rsplit("/", 1)[1]

    async def test_form_offers_the_sign_in_link(self) -> None:
        nonce = self._mint()
        response = await session_ingest.handle_ingest_request(_get_request(nonce))
        body = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sign in to Nintendo", body)
        self.assertIn("accounts.nintendo.com/authorize", body)
        self.assertIn('name="payload"', body)

    async def test_reload_keeps_the_same_verifier(self) -> None:
        # Re-preparing on reload would silently break the flow: the code the
        # user is about to paste can only be redeemed by the verifier that
        # built the URL they already opened.
        nonce = self._mint()
        first = await session_ingest.handle_ingest_request(_get_request(nonce))
        second = await session_ingest.handle_ingest_request(_get_request(nonce))
        self.assertEqual(first.body, second.body)
        self.assertEqual(len(_FakeAuthenticator.instances), 1)
        self.assertEqual(
            session_ingest._ingest_links[nonce].state["verifier"], "verifier-0"
        )

    async def test_pasted_link_is_redeemed_with_the_prepared_verifier(self) -> None:
        nonce = self._mint()
        await session_ingest.handle_ingest_request(_get_request(nonce))
        response = await session_ingest.handle_ingest_request(
            _post_request(nonce, _raw_body(self.NPF_LINK))
        )
        body = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("saved your session to", body)
        # Never echo the submitted link — it carries the one-time code.
        self.assertNotIn("SECRET-CODE", body)
        with open(self.token_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"session_token": "pctl-session-token"})
        # The exchange used the verifier minted for THIS link, not a fresh one.
        exchanging = _FakeAuthenticator.instances[-1]
        self.assertEqual(exchanging._auth_code_verifier, "verifier-0")
        self.assertEqual(exchanging.completed_with, self.NPF_LINK)
        # Single-use, like every other provider.
        self.assertNotIn(nonce, session_ingest._ingest_links)

    async def test_rejected_link_keeps_the_link_alive_and_hides_the_code(self) -> None:
        nonce = self._mint()
        await session_ingest.handle_ingest_request(_get_request(nonce))
        _FakeAuthenticator.fail_with = ValueError(
            f"invalid_grant for session_token_code=SECRET-CODE in {self.NPF_LINK}"
        )
        response = await session_ingest.handle_ingest_request(
            _post_request(nonce, _raw_body(self.NPF_LINK))
        )
        body = response.body.decode()
        self.assertEqual(response.status_code, 400)
        # The upstream error text quotes the submitted token, so it must not be
        # interpolated into the page — a generic retry message goes out instead.
        self.assertNotIn("SECRET-CODE", body)
        self.assertIn("sign in again", body)
        self.assertIn(nonce, session_ingest._ingest_links)
        self.assertIn("Sign in to Nintendo", body)  # retry link still rendered
        self.assertFalse(os.path.exists(self.token_path))

    async def test_bare_session_token_is_stored_as_is(self) -> None:
        nonce = self._mint()
        await session_ingest.handle_ingest_request(_get_request(nonce))
        response = await session_ingest.handle_ingest_request(
            _post_request(nonce, _raw_body("eyJhbGciOi.already-a-token"))
        )
        self.assertEqual(response.status_code, 200)
        with open(self.token_path, encoding="utf-8") as f:
            self.assertEqual(
                json.load(f), {"session_token": "eyJhbGciOi.already-a-token"}
            )

    async def test_link_without_a_prepared_verifier_is_refused(self) -> None:
        # POSTing straight to a link whose form was never rendered (so no
        # verifier exists) must fail loudly rather than redeem with a fresh one.
        nonce = self._mint()
        response = await session_ingest.handle_ingest_request(
            _post_request(nonce, _raw_body(self.NPF_LINK))
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.body.decode())
        self.assertFalse(os.path.exists(self.token_path))


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
