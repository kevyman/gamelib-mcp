import asyncio
import json
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from gamelib_mcp.main import BearerAuthMiddleware, mcp


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


async def _invoke_asgi_app(app, path: str, headers: list[tuple[bytes, bytes]] | None = None) -> tuple[int, dict[str, str], bytes]:
    events: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        events.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": headers or [],
        },
        receive,
        send,
    )

    start = next(message for message in events if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in events if message["type"] == "http.response.body")
    response_headers = {
        key.decode(): value.decode()
        for key, value in start.get("headers", [])
    }
    return start["status"], response_headers, body


def test_bearer_auth_middleware_blocks_admin_integrations_without_auth():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BearerAuthMiddleware(sentinel_app)

    with patch("gamelib_mcp.main.MCP_AUTH_TOKEN", "secret-token"):
        status, headers, body = asyncio.run(_invoke_asgi_app(app, "/admin/integrations"))

    assert called is False
    assert status == 401
    assert headers["content-type"] == "text/plain"
    assert body == b"Unauthorized"


def test_bearer_auth_middleware_allows_admin_integrations_with_valid_bearer_token():
    called = False

    async def sentinel_app(scope, receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BearerAuthMiddleware(sentinel_app)

    with patch("gamelib_mcp.main.MCP_AUTH_TOKEN", "secret-token"):
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
        "gamelib_mcp.main._integration_status_payload",
        new=AsyncMock(return_value=payload),
    ):
        response = asyncio.run(route.endpoint(_request("/admin/integrations")))

    assert route.methods == {"GET", "HEAD"}
    assert response.status_code == 200
    assert json.loads(response.body) == payload


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
        "gamelib_mcp.main._integration_status_payload",
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
        "gamelib_mcp.main._integration_status_payload",
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
        "gamelib_mcp.main._integration_status_payload",
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
