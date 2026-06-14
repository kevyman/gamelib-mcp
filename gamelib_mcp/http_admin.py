"""HTTP surface: bearer-auth ASGI middleware, /health and /admin/* routes.

Kept separate from main.py so the entrypoint stays focused on the MCP tool
surface. Routes are registered through ``register_http_routes(mcp)`` rather than
importing the FastMCP instance, which keeps the dependency one-way
(``main -> http_admin``) with no import cycle.
"""

import html
import logging
import os
import time
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .lifecycle import INSPECTOR_PLATFORM_ALIASES, SYNC_METADATA_PLATFORMS

logger = logging.getLogger(__name__)

MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")
_ALLOWED_ORIGINS: frozenset[str] = frozenset(
    o.strip().rstrip("/") for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
)
_CORS_ALLOW_METHODS = b"GET, POST, DELETE, OPTIONS"
# mcp-protocol-version is required: the Streamable HTTP spec has clients send it
# on every post-initialize request, so the preflight must allow it.
_CORS_ALLOW_HEADERS = (
    b"authorization, content-type, accept, mcp-session-id, last-event-id, mcp-protocol-version"
)
_CORS_EXPOSE_HEADERS = b"mcp-session-id"
_CORS_MAX_AGE = b"86400"  # cache preflight for a day to cut repeat OPTIONS

# Paths/prefixes that must work without auth
_OPEN_PATHS = {"/health", "/"}
_OPEN_PREFIXES = ("/.well-known/",)


class BearerAuthMiddleware:
    """Pure ASGI middleware — safe for Streamable HTTP (no response buffering)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}

        # Origin header validation (MCP spec MUST for Streamable HTTP, DNS-rebinding defense).
        # Requests without an Origin header pass (CLI tools and native MCP clients don't send one).
        # Browser-origin requests must be explicitly allowlisted via MCP_ALLOWED_ORIGINS.
        origin = headers.get(b"origin", b"").decode()
        if origin and origin not in _ALLOWED_ORIGINS:
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"text/plain"), (b"content-length", b"9")]})
            await send({"type": "http.response.body", "body": b"Forbidden"})
            return

        cors_headers = []
        if origin:
            cors_headers = [
                (b"access-control-allow-origin", origin.encode()),
                (b"access-control-allow-methods", _CORS_ALLOW_METHODS),
                (b"access-control-allow-headers", _CORS_ALLOW_HEADERS),
                (b"access-control-expose-headers", _CORS_EXPOSE_HEADERS),
                (b"vary", b"Origin"),
            ]

            if scope.get("method") == "OPTIONS":
                await send({
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        *cors_headers,
                        (b"access-control-max-age", _CORS_MAX_AGE),
                        (b"content-length", b"0"),
                    ],
                })
                await send({"type": "http.response.body", "body": b""})
                return

        async def send_with_cors(message):
            if cors_headers and message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *cors_headers]}
            await send(message)

        downstream_send = send_with_cors if cors_headers else send

        if not MCP_AUTH_TOKEN:
            await self.app(scope, receive, downstream_send)
            return

        path = scope.get("path", "")
        if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            await self.app(scope, receive, downstream_send)
            return

        auth = headers.get(b"authorization", b"").decode()
        if auth == f"Bearer {MCP_AUTH_TOKEN}":
            await self.app(scope, receive, downstream_send)
            return

        params = parse_qs(scope.get("query_string", b"").decode())
        if params.get("token", [None])[0] == MCP_AUTH_TOKEN:
            await self.app(scope, receive, downstream_send)
            return

        auth_headers = [(b"content-type", b"text/plain"), (b"content-length", b"12"), *cors_headers]
        await send({"type": "http.response.start", "status": 401,
                    "headers": auth_headers})
        await send({"type": "http.response.body", "body": b"Unauthorized"})


# The inspectors probe binaries/config mounts on every call, which is too
# expensive to repeat per request; status changes on the order of syncs.
_INTEGRATION_STATUS_TTL_SECONDS = 60.0
_integration_status_cache: tuple[float, dict] | None = None


async def _integration_status_payload(force_refresh: bool = False) -> dict[str, dict]:
    global _integration_status_cache

    from .data.db import get_meta_prefix
    from .integrations.inspectors import inspect_all_integrations_dict

    if not force_refresh and _integration_status_cache is not None:
        cached_at, payload = _integration_status_cache
        if time.monotonic() - cached_at < _INTEGRATION_STATUS_TTL_SECONDS:
            return payload

    last_sync_by_platform: dict[str, dict[str, str]] = {}
    try:
        all_meta = await get_meta_prefix("integration_sync_")
        for platform in SYNC_METADATA_PLATFORMS:
            prefix = f"integration_sync_{platform}_"
            platform_meta = {
                key[len(prefix):]: value
                for key, value in all_meta.items()
                if key.startswith(prefix)
            }
            if platform_meta:
                inspector_name = INSPECTOR_PLATFORM_ALIASES.get(platform, platform)
                last_sync_by_platform[inspector_name] = platform_meta
    except Exception:
        logger.exception("Failed to load integration sync metadata")

    payload = inspect_all_integrations_dict(last_sync_by_platform=last_sync_by_platform)
    _integration_status_cache = (time.monotonic(), payload)
    return payload


def _render_integrations_ui(payload: dict[str, dict]) -> str:
    items = []
    for platform, status in payload.items():
        summary = html.escape(status.get("summary") or "No summary available.")
        overall_status = html.escape(status.get("overall_status") or "unknown")
        backend = html.escape(status.get("active_backend") or "none")
        capabilities = status.get("capabilities") or []
        checks = status.get("checks") or []
        last_sync = status.get("last_sync") or {}
        remediation_steps = status.get("remediation_steps") or []

        capability_list = "".join(
            "<li>"
            f"{html.escape(item.get('name') or 'unknown')}: "
            f"{html.escape(item.get('status') or 'unknown')} "
            f"- {html.escape(item.get('summary') or '')}"
            "</li>"
            for item in capabilities
        ) or "<li>None</li>"

        failing_checks = [item for item in checks if item.get("status") != "pass"]
        failing_check_list = "".join(
            "<li>"
            f"{html.escape(item.get('name') or 'unknown')}: "
            f"{html.escape(item.get('status') or 'unknown')} "
            f"- {html.escape(item.get('summary') or '')}"
            "</li>"
            for item in failing_checks
        ) or "<li>None</li>"

        last_sync_list = "".join(
            "<li>"
            f"{html.escape(str(key))}: {html.escape(str(value))}"
            "</li>"
            for key, value in last_sync.items()
        ) or "<li>None</li>"

        remediation_list = "".join(
            "<li><code>"
            f"{html.escape(step)}"
            "</code></li>"
            for step in remediation_steps
        ) or "<li>None</li>"
        items.append(
            "<li><section>"
            f"<h2>{html.escape(platform)}</h2>"
            f"<p><strong>Status:</strong> {overall_status} ({backend})</p>"
            f"<p>{summary}</p>"
            "<h3>Capabilities</h3><ul>"
            f"{capability_list}"
            "</ul>"
            "<h3>Failing Checks</h3><ul>"
            f"{failing_check_list}"
            "</ul>"
            "<h3>Last Sync</h3><ul>"
            f"{last_sync_list}"
            "</ul>"
            "<h3>Remediation</h3><ul>"
            f"{remediation_list}"
            "</ul>"
            "</section></li>"
        )

    body = "".join(items) or "<li>No integrations detected.</li>"
    return (
        "<!doctype html>"
        "<html><head><title>Integration Status</title></head>"
        "<body><h1>Integration Status</h1><ul>"
        f"{body}"
        "</ul></body></html>"
    )


def register_http_routes(mcp) -> None:
    """Register /health and /admin/* routes on the given FastMCP instance."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        from .data.db import get_meta
        last_sync = await get_meta("library_synced_at")
        return JSONResponse({"status": "ok", "library_synced_at": last_sync})

    @mcp.custom_route("/admin/integrations", methods=["GET"])
    async def admin_integrations(request: Request) -> JSONResponse:
        return JSONResponse(await _integration_status_payload())

    @mcp.custom_route("/admin/integrations/ui", methods=["GET"])
    async def admin_integrations_ui(request: Request) -> HTMLResponse:
        payload = await _integration_status_payload()
        return HTMLResponse(_render_integrations_ui(payload))

    @mcp.custom_route("/admin/integrations/{platform}", methods=["GET"])
    async def admin_integration_detail(request: Request) -> JSONResponse:
        platform = request.path_params["platform"]
        payload = await _integration_status_payload()
        if platform not in payload:
            return JSONResponse({"error": f"Unknown integration: {platform}"}, status_code=404)
        return JSONResponse(payload[platform])
