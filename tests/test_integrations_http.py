import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

from conftest import add_platform, adopt_migrated_db, seed_game
from starlette.requests import Request

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import enrich_bg
from gamelib_mcp.http_admin import HttpSecurityMiddleware
from gamelib_mcp.main import mcp


def _get_route(path: str):
    for route in mcp._additional_http_routes:
        if route.path == path:
            return route
    raise AssertionError(f"Route {path} is not registered")


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


def _request_with_path_params(path: str, path_params: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "path_params": path_params,
            "headers": [],
        }
    )


async def _invoke_asgi_app(
    app,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    method: str = "GET",
    query_string: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    events: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        events.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query_string,
            "headers": headers or [],
        },
        receive,
        send,
    )

    start = next(
        message for message in events if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in events
        if message["type"] == "http.response.body"
    )
    response_headers = {
        key.decode(): value.decode() for key, value in start.get("headers", [])
    }
    return start["status"], response_headers, body


def test_http_security_middleware_blocks_admin_integrations_without_auth():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = HttpSecurityMiddleware(
        sentinel_app,
        admin_token="secret-token",
        allowed_origins=frozenset(),
    )
    status, headers, body = asyncio.run(_invoke_asgi_app(app, "/admin/integrations"))

    assert called is False
    assert status == 401
    assert headers["content-type"] == "text/plain"
    assert body == b"Unauthorized"


def test_http_security_middleware_allows_admin_with_valid_bearer_token():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = HttpSecurityMiddleware(
        sentinel_app,
        admin_token="secret-token",
        allowed_origins=frozenset(),
    )
    status, headers, body = asyncio.run(
        _invoke_asgi_app(
            app,
            "/admin/integrations",
            headers=[(b"authorization", b"Bearer secret-token")],
        )
    )

    assert called is True
    assert status == 204
    assert headers == {}
    assert body == b""


def test_http_security_middleware_leaves_mcp_auth_to_fastmcp():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = HttpSecurityMiddleware(
        sentinel_app,
        admin_token="secret-token",
        allowed_origins=frozenset(),
    )
    status, headers, body = asyncio.run(_invoke_asgi_app(app, "/mcp"))

    assert called is True
    assert status == 204
    assert headers == {}
    assert body == b""


def test_http_security_middleware_rejects_unknown_browser_origin():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = HttpSecurityMiddleware(
        sentinel_app,
        admin_token="secret-token",
        allowed_origins=frozenset(),
    )
    status, headers, body = asyncio.run(
        _invoke_asgi_app(
            app,
            "/mcp",
            headers=[(b"origin", b"https://evil.example")],
        )
    )

    assert called is False
    assert status == 403
    assert headers["content-type"] == "text/plain"
    assert body == b"Forbidden"


def test_http_security_middleware_allows_browser_origin_in_allowlist():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = HttpSecurityMiddleware(
        sentinel_app,
        admin_token="secret-token",
        allowed_origins=frozenset({"https://claude.ai"}),
    )
    status, headers, body = asyncio.run(
        _invoke_asgi_app(
            app,
            "/mcp",
            headers=[(b"origin", b"https://claude.ai")],
        )
    )

    assert called is True
    assert status == 204
    assert headers["access-control-allow-origin"] == "https://claude.ai"
    assert headers["access-control-allow-methods"] == "GET, POST, DELETE, OPTIONS"
    assert headers["access-control-allow-headers"] == (
        "authorization, content-type, accept, mcp-session-id, last-event-id, mcp-protocol-version"
    )
    assert "mcp-protocol-version" in headers["access-control-allow-headers"]
    assert headers["access-control-expose-headers"] == "mcp-session-id"
    assert headers["vary"] == "Origin"
    assert body == b""


def test_http_security_middleware_answers_allowed_browser_preflight_before_app():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = HttpSecurityMiddleware(
        sentinel_app,
        admin_token="secret-token",
        allowed_origins=frozenset({"https://chatgpt.com"}),
    )
    status, headers, body = asyncio.run(
        _invoke_asgi_app(
            app,
            "/mcp",
            headers=[
                (b"origin", b"https://chatgpt.com"),
                (b"access-control-request-method", b"POST"),
                (b"access-control-request-headers", b"content-type, accept"),
            ],
            method="OPTIONS",
        )
    )

    assert called is False
    assert status == 204
    assert headers["access-control-allow-origin"] == "https://chatgpt.com"
    assert headers["access-control-allow-methods"] == "GET, POST, DELETE, OPTIONS"
    assert headers["access-control-allow-headers"] == (
        "authorization, content-type, accept, mcp-session-id, last-event-id, mcp-protocol-version"
    )
    assert "mcp-protocol-version" in headers["access-control-allow-headers"]
    assert headers["access-control-expose-headers"] == "mcp-session-id"
    assert headers["access-control-max-age"] == "86400"
    assert headers["content-length"] == "0"
    assert body == b""


def test_http_security_middleware_rejects_admin_query_token():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True

    app = HttpSecurityMiddleware(
        sentinel_app,
        admin_token="secret-token",
        allowed_origins=frozenset(),
    )
    status, _, body = asyncio.run(
        _invoke_asgi_app(
            app,
            "/admin/integrations",
            query_string=b"token=secret-token",
        )
    )

    assert called is False
    assert status == 401
    assert body == b"Unauthorized"


def test_http_security_middleware_leaves_oauth_routes_open():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = HttpSecurityMiddleware(
        sentinel_app,
        admin_token="secret-token",
        allowed_origins=frozenset(),
    )

    for path in (
        "/.well-known/oauth-authorization-server",
        "/register",
        "/authorize",
        "/token",
        "/auth/callback",
    ):
        called = False
        status, _, _ = asyncio.run(_invoke_asgi_app(app, path))
        assert called is True
        assert status == 204


def test_http_security_middleware_rejects_non_utf8_origin_without_crashing():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = HttpSecurityMiddleware(
        sentinel_app,
        admin_token="secret-token",
        allowed_origins=frozenset({"https://claude.ai"}),
    )
    status, _, body = asyncio.run(
        _invoke_asgi_app(
            app,
            "/mcp",
            headers=[(b"origin", b"\xff\xfe not valid utf-8")],
        )
    )

    assert called is False
    assert status == 403
    assert body == b"Forbidden"


def test_http_security_middleware_rejects_non_utf8_admin_authorization_without_crashing():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True

    app = HttpSecurityMiddleware(
        sentinel_app,
        admin_token="secret-token",
        allowed_origins=frozenset(),
    )
    status, _, body = asyncio.run(
        _invoke_asgi_app(
            app,
            "/admin/integrations",
            headers=[(b"authorization", b"Bearer \xff\xfe not valid utf-8")],
        )
    )

    assert called is False
    assert status == 401
    assert body == b"Unauthorized"


def test_get_admin_integrations_returns_json_payload():
    payload = {
        "steam": {
            "platform": "steam",
            "overall_status": "ready",
            "summary": "Steam Web API credentials are configured.",
            "active_backend": "steam-web-api",
        }
    }

    route = _get_route("/admin/integrations")

    with patch(
        "gamelib_mcp.http_admin._integration_status_payload",
        new=AsyncMock(return_value=payload),
    ):
        response = asyncio.run(route.endpoint(_request("/admin/integrations")))

    assert route.methods == {"GET", "HEAD"}
    assert response.status_code == 200
    assert json.loads(response.body) == payload


def test_health_reports_platform_coverage_degraded_when_platforms_are_missing(tmp_path):
    async def run_test():
        db_path = tmp_path / "health.sqlite"
        db_module._DB_READY_PATH = None
        db_module._ENV_LOADED = True
        with patch.dict(os.environ, {"DATABASE_URL": f"file:{db_path}"}, clear=False):
            adopt_migrated_db(db_path)
            steam_game_id = await seed_game("Portal")
            epic_game_id = await seed_game("Alan Wake")
            await add_platform(steam_game_id, "steam")
            await add_platform(epic_game_id, "epic")
            await db_module.set_meta("library_synced_at", "2026-06-14T20:13:18+00:00")
            await db_module.set_meta("library_sync_status", "idle")
            await db_module.set_meta("library_sync_error", "previous transient failure")
            # gog has synced successfully before, so its 0-game count is a real gap.
            for platform in ("steam", "epic", "gog"):
                await db_module.set_meta(
                    f"integration_sync_{platform}_last_success_at", "2026-06-14T20:13:18+00:00"
                )

            route = _get_route("/health")
            response = await route.endpoint(_request("/health"))

        db_module._DB_READY_PATH = None
        db_module._FTS_READY_PATH = None
        return response

    response = asyncio.run(run_test())
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload == {"status": "degraded"}


def test_admin_health_reports_platform_coverage_details_when_platforms_are_missing(tmp_path):
    async def run_test():
        db_path = tmp_path / "admin-health.sqlite"
        db_module._DB_READY_PATH = None
        db_module._ENV_LOADED = True
        with patch.dict(os.environ, {"DATABASE_URL": f"file:{db_path}"}, clear=False):
            adopt_migrated_db(db_path)
            steam_game_id = await seed_game("Portal")
            epic_game_id = await seed_game("Alan Wake")
            await add_platform(steam_game_id, "steam")
            await add_platform(epic_game_id, "epic")
            await db_module.set_meta("library_synced_at", "2026-06-14T20:13:18+00:00")
            await db_module.set_meta("library_sync_status", "idle")
            await db_module.set_meta("library_sync_error", "previous transient failure")
            # gog has synced successfully before, so its 0-game count is a real gap.
            for platform in ("steam", "epic", "gog"):
                await db_module.set_meta(
                    f"integration_sync_{platform}_last_success_at", "2026-06-14T20:13:18+00:00"
                )

            route = _get_route("/admin/health")
            response = await route.endpoint(_request("/admin/health"))

        db_module._DB_READY_PATH = None
        db_module._FTS_READY_PATH = None
        return response

    response = asyncio.run(run_test())
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["status"] == "degraded"
    assert payload["checks"]["database"]["status"] == "ok"
    assert payload["checks"]["library_sync"]["status"] == "ok"
    assert payload["checks"]["library_sync"]["error"] == "previous transient failure"
    assert payload["checks"]["platform_coverage"]["status"] == "degraded"
    assert payload["checks"]["platform_coverage"]["expected_platforms"] == ["epic", "gog", "steam"]
    assert payload["checks"]["platform_coverage"]["platform_counts"] == {"epic": 1, "steam": 1}
    assert payload["checks"]["platform_coverage"]["missing_platforms"] == ["gog"]


def test_health_ok_when_never_synced_platforms_have_no_games(tmp_path):
    async def run_test():
        db_path = tmp_path / "health-ok.sqlite"
        db_module._DB_READY_PATH = None
        db_module._ENV_LOADED = True
        with patch.dict(os.environ, {"DATABASE_URL": f"file:{db_path}"}, clear=False):
            adopt_migrated_db(db_path)
            steam_game_id = await seed_game("Portal")
            await add_platform(steam_game_id, "steam")
            await db_module.set_meta("library_synced_at", "2026-06-14T20:13:18+00:00")
            # Only Steam has ever synced; the other platforms are unconfigured
            # and must NOT count as missing coverage.
            await db_module.set_meta(
                "integration_sync_steam_last_success_at", "2026-06-14T20:13:18+00:00"
            )

            route = _get_route("/health")
            response = await route.endpoint(_request("/health"))

        db_module._DB_READY_PATH = None
        db_module._FTS_READY_PATH = None
        return response

    response = asyncio.run(run_test())

    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ok"}


def test_health_returns_503_when_database_is_unavailable():
    route = _get_route("/health")

    with patch("gamelib_mcp.http_admin._health_payload", new=AsyncMock(side_effect=RuntimeError("db boom"))):
        response = asyncio.run(route.endpoint(_request("/health")))

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload == {"status": "error"}


def test_get_admin_integration_detail_returns_requested_platform():
    payload = {
        "steam": {
            "platform": "steam",
            "overall_status": "ready",
            "summary": "Steam Web API credentials are configured.",
            "active_backend": "steam-web-api",
        },
        "gog": {
            "platform": "gog",
            "overall_status": "unconfigured",
            "summary": "No gog backend is configured.",
            "active_backend": None,
        },
    }

    route = _get_route("/admin/integrations/{platform}")

    with patch(
        "gamelib_mcp.http_admin._integration_status_payload",
        new=AsyncMock(return_value=payload),
    ):
        response = asyncio.run(
            route.endpoint(
                _request_with_path_params(
                    "/admin/integrations/steam",
                    {"platform": "steam"},
                )
            )
        )

    assert route.methods == {"GET", "HEAD"}
    assert response.status_code == 200
    assert json.loads(response.body) == payload["steam"]


def test_get_admin_integration_detail_returns_404_for_unknown_platform():
    route = _get_route("/admin/integrations/{platform}")

    with patch(
        "gamelib_mcp.http_admin._integration_status_payload",
        new=AsyncMock(return_value={"steam": {"platform": "steam", "overall_status": "ready"}}),
    ):
        response = asyncio.run(
            route.endpoint(
                _request_with_path_params(
                    "/admin/integrations/unknown",
                    {"platform": "unknown"},
                )
            )
        )

    assert response.status_code == 404
    assert json.loads(response.body) == {"error": "Unknown integration: unknown"}


def test_get_admin_integrations_ui_renders_summary_text_and_escapes_unsafe_fields():
    payload = {
        "<script>alert(1)</script>": {
            "platform": "<script>alert(1)</script>",
            "overall_status": "degraded",
            "summary": "<b>Ownership</b> is ready but <script>auth</script> is stale.",
            "active_backend": "<b>legendary-cache</b>",
            "capabilities": [
                {"name": "ownership", "status": "ready", "summary": "<i>cached</i>"},
                {"name": "playtime", "status": "stale", "summary": "<script>expired</script>"},
            ],
            "checks": [
                {"name": "legendary_user_json", "status": "pass", "summary": "user.json found"},
                {"name": "epic_playtime_token", "status": "warn", "summary": "<script>refresh</script> required"},
            ],
            "remediation_steps": [
                "Run `<legendary auth>`.",
                "Run `<legendary list --force-refresh>`.",
            ],
            "last_sync": {
                "last_success_at": "2026-04-13T12:00:00+00:00",
                "last_error_classification": "auth_stale",
            },
        }
    }

    route = _get_route("/admin/integrations/ui")

    with patch(
        "gamelib_mcp.http_admin._integration_status_payload",
        new=AsyncMock(return_value=payload),
    ):
        response = asyncio.run(route.endpoint(_request("/admin/integrations/ui")))

    assert route.methods == {"GET", "HEAD"}
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    text = response.body.decode()
    assert "degraded" in text.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "&lt;b&gt;Ownership&lt;/b&gt; is ready but &lt;script&gt;auth&lt;/script&gt; is stale." in text
    assert "(&lt;b&gt;legendary-cache&lt;/b&gt;)" in text
    assert "playtime" in text
    assert "epic_playtime_token" in text
    assert "last_error_classification" in text
    assert "Run `&lt;legendary auth&gt;`." in text
    assert "<script>alert(1)</script>" not in text
    assert "<b>Ownership</b>" not in text


def test_admin_health_carries_the_last_enrichment_run_per_provider(tmp_path):
    """The counters exist to make a dead provider visible to an OPERATOR.

    They are logged at the end of each pass, which is exactly where nobody is
    looking three days later; /health is where someone checks. Reported, not
    scored: a provider outage is not a degraded server.
    """

    async def run_test():
        db_path = tmp_path / "enrichment-health.sqlite"
        db_module._DB_READY_PATH = None
        db_module._ENV_LOADED = True
        with patch.dict(os.environ, {"DATABASE_URL": f"file:{db_path}"}, clear=False):
            await db_module.init_db()
            steam_game_id = await seed_game("Portal")
            await add_platform(steam_game_id, "steam")
            await db_module.set_meta(
                "integration_sync_steam_last_success_at", "2026-06-14T20:13:18+00:00"
            )

            enrich_bg._reset_run_stats()
            enrich_bg._record_processed("store", 41)
            enrich_bg._record_failure("hltb", RuntimeError("hltb markup changed"))

            route = _get_route("/admin/health")
            response = await route.endpoint(_request("/admin/health"))

        enrich_bg._reset_run_stats()
        db_module._DB_READY_PATH = None
        return response

    response = asyncio.run(run_test())
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["enrichment"]["store"] == {
        "processed": 41,
        "failed": 0,
        "last_error": None,
    }
    assert payload["enrichment"]["hltb"]["failed"] == 1
    assert "hltb markup changed" in payload["enrichment"]["hltb"]["last_error"]
    # Every provider is present, so an operator sees a silent family too.
    assert set(payload["enrichment"]) == set(enrich_bg._PROVIDERS)
    # Reported, never scored.
    assert payload["status"] == "ok"

