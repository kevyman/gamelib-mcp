"""HTTP security middleware, /health, /admin/*, and /ingest/{nonce} routes.

Kept separate from main.py so the entrypoint stays focused on the MCP tool
surface. Routes are registered through ``register_http_routes(mcp)`` rather than
importing the FastMCP instance, which keeps the dependency one-way
(``main -> http_admin``) with no import cycle.
"""

import hmac
import html
import logging
import time
from typing import cast

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .lifecycle import INSPECTOR_PLATFORM_ALIASES, SYNC_METADATA_PLATFORMS

logger = logging.getLogger(__name__)

_CORS_ALLOW_METHODS = b"GET, POST, DELETE, OPTIONS"
# mcp-protocol-version is required: the Streamable HTTP spec has clients send it
# on every post-initialize request, so the preflight must allow it.
_CORS_ALLOW_HEADERS = b"authorization, content-type, accept, mcp-session-id, last-event-id, mcp-protocol-version"
_CORS_EXPOSE_HEADERS = b"mcp-session-id"
_CORS_MAX_AGE = b"86400"  # cache preflight for a day to cut repeat OPTIONS


class HttpSecurityMiddleware:
    """Validate browser origins and protect operator-only HTTP routes.

    FastMCP's OAuth provider owns authentication for ``/mcp`` and its OAuth
    protocol routes.  The independent admin token is intentionally accepted
    only in the Authorization header and only for ``/admin/*``.
    """

    def __init__(self, app, *, admin_token: str, allowed_origins: frozenset[str]):
        self.app = app
        self.admin_token = admin_token
        self.allowed_origins = allowed_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}

        # Origin header validation (MCP spec MUST for Streamable HTTP, DNS-rebinding defense).
        # Requests without an Origin header pass (CLI tools and native MCP clients don't send one).
        # Browser-origin requests must be explicitly allowlisted via MCP_ALLOWED_ORIGINS.
        # errors="replace": malformed header bytes must never crash the
        # middleware — decode to a string that can't match a real origin/token
        # and let the normal allow/deny logic below reject it cleanly.
        origin = headers.get(b"origin", b"").decode("utf-8", errors="replace")
        if origin and origin not in self.allowed_origins:
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"text/plain"),
                        (b"content-length", b"9"),
                    ],
                }
            )
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
                await send(
                    {
                        "type": "http.response.start",
                        "status": 204,
                        "headers": [
                            *cors_headers,
                            (b"access-control-max-age", _CORS_MAX_AGE),
                            (b"content-length", b"0"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return

        async def send_with_cors(message):
            if cors_headers and message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [*message.get("headers", []), *cors_headers],
                }
            await send(message)

        downstream_send = send_with_cors if cors_headers else send

        path = scope.get("path", "")
        if path != "/admin" and not path.startswith("/admin/"):
            await self.app(scope, receive, downstream_send)
            return

        auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
        if hmac.compare_digest(auth.encode(), f"Bearer {self.admin_token}".encode()):
            await self.app(scope, receive, downstream_send)
            return

        auth_headers = [
            (b"content-type", b"text/plain"),
            (b"content-length", b"12"),
            *cors_headers,
        ]
        await send(
            {"type": "http.response.start", "status": 401, "headers": auth_headers}
        )
        await send({"type": "http.response.body", "body": b"Unauthorized"})


# The inspectors probe binaries/config mounts on every call, which is too
# expensive to repeat per request; status changes on the order of syncs.
_INTEGRATION_STATUS_TTL_SECONDS = 60.0
_integration_status_cache: tuple[float, dict] | None = None


async def _integration_status_payload(force_refresh: bool = False) -> dict[str, dict]:
    global _integration_status_cache

    from .data.db import get_meta_prefix
    from .integrations.inspectors import LastSyncMeta, inspect_all_integrations_dict

    if not force_refresh and _integration_status_cache is not None:
        cached_at, payload = _integration_status_cache
        if time.monotonic() - cached_at < _INTEGRATION_STATUS_TTL_SECONDS:
            return payload

    last_sync_by_platform: dict[str, LastSyncMeta] = {}
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
                # The meta writer (build_platform_sync_metadata) emits exactly
                # the LastSyncMeta key set, so this narrowing is sound.
                last_sync_by_platform[inspector_name] = cast(LastSyncMeta, platform_meta)
    except Exception:
        logger.exception("Failed to load integration sync metadata")

    payload = inspect_all_integrations_dict(last_sync_by_platform=last_sync_by_platform)

    # Surface scrape-config drift alongside the platform integrations: which
    # scrape providers run on DB overrides (AI heals / manual edits) instead
    # of code-level defaults, and whether proposals are pending approval.
    try:
        from .tools.scrape_admin import scrape_config_status_payload

        payload["scrapers"] = await scrape_config_status_payload()
    except Exception:
        logger.exception("Failed to build scrape-config status")

    _integration_status_cache = (time.monotonic(), payload)
    return payload


async def _health_payload() -> dict:
    from .data.db import SCHEMA_VERSION, _db_path, get_db, get_meta, get_meta_prefix
    from .data.enrich_bg import last_run_stats

    db_path = _db_path()
    async with get_db() as db:
        user_version_row = await db.execute_fetchone("PRAGMA user_version")
        user_version = int(user_version_row[0]) if user_version_row else None
        platform_rows = await db.execute_fetchall(
            """SELECT platform, COUNT(*) AS count
               FROM game_platforms
               WHERE owned = 1
               GROUP BY platform
               ORDER BY platform"""
        )
        platform_counts = {row["platform"]: row["count"] for row in platform_rows}

    last_sync = await get_meta("library_synced_at")
    library_sync_status = await get_meta("library_sync_status") or "idle"
    library_sync_error = await get_meta("library_sync_error")

    # A platform is only expected to have games once it has synced successfully
    # at least once; platforms that were never configured don't degrade health.
    sync_meta = await get_meta_prefix("integration_sync_")
    expected_platforms = sorted(
        platform
        for platform in SYNC_METADATA_PLATFORMS
        if sync_meta.get(f"integration_sync_{platform}_last_success_at")
    )
    missing_platforms = [
        platform
        for platform in expected_platforms
        if platform_counts.get(platform, 0) == 0
    ]
    db_status = "ok" if user_version == SCHEMA_VERSION else "degraded"
    platform_status = "ok" if not missing_platforms else "degraded"
    sync_status = "running" if library_sync_status == "in_progress" else "ok"
    overall_status = "ok" if {db_status, platform_status, sync_status} == {"ok"} else "degraded"

    return {
        "status": overall_status,
        "library_synced_at": last_sync,
        # Per-provider outcome of the LAST background enrichment pass. Reported,
        # never scored: the providers are best-effort by contract, so a dead
        # HowLongToBeat is an operator's cue to look, not a degraded server —
        # it deliberately does not feed overall_status. All-zero counters mean
        # no pass has run in this process yet.
        "enrichment": last_run_stats(),
        "checks": {
            "database": {
                "status": db_status,
                "path": db_path,
                "schema_version": user_version,
                "expected_schema_version": SCHEMA_VERSION,
            },
            "library_sync": {
                "status": sync_status,
                "state": library_sync_status,
                "error": library_sync_error,
            },
            "platform_coverage": {
                "status": platform_status,
                "expected_platforms": expected_platforms,
                "platform_counts": platform_counts,
                "missing_platforms": missing_platforms,
            },
        },
    }


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
        try:
            payload = await _health_payload()
            return JSONResponse({"status": payload["status"]})
        except Exception:
            logger.exception("Health check failed")
            return JSONResponse({"status": "error"}, status_code=503)

    @mcp.custom_route("/admin/health", methods=["GET"])
    async def admin_health(request: Request) -> JSONResponse:
        return JSONResponse(await _health_payload())

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

    # Deliberately outside /admin/: a browser navigation can't send the
    # Authorization header the /admin bearer gate requires. The single-use
    # nonce (minted by the OAuth-gated create_session_ingest_link tool) is
    # the credential here.
    @mcp.custom_route("/ingest/{nonce}", methods=["GET", "POST"])
    async def session_ingest(request: Request) -> HTMLResponse:
        from .session_ingest import handle_ingest_request

        return await handle_ingest_request(request)
