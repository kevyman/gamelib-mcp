"""HTTP surface: bearer-auth ASGI middleware, /health and /admin/* routes.

Kept separate from main.py so the entrypoint stays focused on the MCP tool
surface. Routes are registered through ``register_http_routes(mcp)`` rather than
importing the FastMCP instance, which keeps the dependency one-way
(``main -> http_admin``) with no import cycle.
"""

import html
import logging
import os
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .lifecycle import SYNC_METADATA_PLATFORMS

logger = logging.getLogger(__name__)

MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")
_ALLOWED_ORIGINS: frozenset[str] = frozenset(
    o.strip().rstrip("/") for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
)

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
        # Only enforced when MCP_ALLOWED_ORIGINS is configured; requests without an Origin
        # header always pass (CLI tools and MCP desktop clients don't send one).
        if _ALLOWED_ORIGINS:
            origin = headers.get(b"origin", b"").decode()
            if origin and origin not in _ALLOWED_ORIGINS:
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain"), (b"content-length", b"9")]})
                await send({"type": "http.response.body", "body": b"Forbidden"})
                return

        if not MCP_AUTH_TOKEN:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            await self.app(scope, receive, send)
            return

        auth = headers.get(b"authorization", b"").decode()
        if auth == f"Bearer {MCP_AUTH_TOKEN}":
            await self.app(scope, receive, send)
            return

        params = parse_qs(scope.get("query_string", b"").decode())
        if params.get("token", [None])[0] == MCP_AUTH_TOKEN:
            await self.app(scope, receive, send)
            return

        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain"), (b"content-length", b"12")]})
        await send({"type": "http.response.body", "body": b"Unauthorized"})


async def _integration_status_payload() -> dict[str, dict]:
    from .data.db import get_meta_prefix
    from .integrations.inspectors import inspect_all_integrations_dict

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
                last_sync_by_platform[platform] = platform_meta
    except Exception:
        logger.exception("Failed to load integration sync metadata")

    return inspect_all_integrations_dict(last_sync_by_platform=last_sync_by_platform)


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
