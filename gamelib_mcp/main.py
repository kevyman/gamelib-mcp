"""FastMCP server — app definition, MCP tool registration, auth, SSE transport.

Startup/shutdown and background-task orchestration live in ``lifecycle.py``; the
HTTP admin routes' helpers live alongside them here for now. This module stays
deliberately thin: the FastMCP instance, the tool passthrough decorators (whose
signatures and docstrings are the MCP wire schema), and the ASGI entry point.
"""

import html
import logging
import os

from fastmcp import FastMCP

from .env import load_project_dotenv

load_project_dotenv()

from .lifecycle import SYNC_METADATA_PLATFORMS, lifespan
from .tools.integrations import get_integration_status as _filter_integration_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")

_display_name = os.getenv("STEAM_PROFILE_ID") or os.getenv("BACKLOGGD_USER") or "the configured user"

mcp = FastMCP(
    name="game-library",
    instructions=(
        f"You have access to {_display_name}'s game library across synced platforms and stores. "
        "Use the tools to search, filter, and get details about games and platforms. "
        "Ratings are synced from connected sources such as Backloggd and Steam reviews (read-only). "
        "Call sync_ratings to refresh ratings and taste profile data."
    ),
    lifespan=lifespan,
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def search_games(query: str, limit: int = 20, platform: str | None = None) -> list[dict]:
    """Find games in the library by name substring. platform: steam|epic|gog|nintendo|switch2|ps5"""
    from .tools.library import search_games as _search
    return await _search(query, limit, platform)


@mcp.tool()
async def search_games_batch(queries: list[str], limit_per_query: int = 5) -> dict[str, list[dict]]:
    """
    Look up multiple games by name in a single call.
    Returns a dict keyed by each query with matching games as values.
    Use this instead of calling search_games repeatedly.
    """
    from .tools.library import search_games_batch as _batch
    return await _batch(queries, limit_per_query)


@mcp.tool()
async def get_library_stats(
    filter: str = "all",
    max_hltb_hours: float | None = None,
    min_metacritic: int | None = None,
    protondb_tier: str | None = None,
    sort_by: str = "playtime",
    limit: int = 50,
    platform: str | None = None,
) -> dict:
    """
    Get filtered/sorted library list plus aggregate stats.

    filter: all | unplayed | played | recent | farmed
    sort_by: playtime | name | metacritic | hltb
    protondb_tier: native | platinum | gold | silver | bronze | borked
    platform: steam | epic | gog | nintendo | switch2 | ps5 (optional — filter to games on that platform)
    """
    from .tools.library import get_library_stats as _stats
    return await _stats(filter, max_hltb_hours, min_metacritic, protondb_tier, sort_by, limit, platform)


@mcp.tool()
async def get_game_detail(
    name: str | None = None,
    appid: int | None = None,
    game_id: int | None = None,
) -> dict:
    """
    Get full details for a single game, including platform ownership, HLTB,
    Metacritic, ProtonDB, and any personal ratings. Triggers lazy data fetches.
    Provide game_id, name (partial match), or Steam appid when available.
    """
    from .tools.detail import get_game_detail as _detail
    return await _detail(name, appid, game_id)


@mcp.tool()
async def find_games_by_vibe(
    vibe: str,
    max_hltb_hours: float | None = None,
    unplayed_only: bool = True,
    protondb_min_tier: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Find games matching a vibe using tag intersection search.

    vibe options: roguelike, cozy, horror, metroidvania, souls, open world,
    crafting, puzzle, platformer, rpg, strategy, simulation, stealth,
    narrative, co-op, shooter, survival, indie, cyberpunk, fantasy.
    Or pass a raw tag string.
    """
    from .tools.discover import find_games_by_vibe as _vibe
    return await _vibe(vibe, max_hltb_hours, unplayed_only, protondb_min_tier, limit)


@mcp.tool()
async def get_recommendations(
    max_hltb_hours: float | None = None,
    unplayed_only: bool = True,
    limit: int = 20,
) -> list[dict]:
    """
    Get ranked unplayed games by tag affinity score (based on your rated games).
    Requires sync_ratings to have been run at least once.
    """
    from .tools.discover import get_recommendations as _rec
    return await _rec(max_hltb_hours, unplayed_only, limit)


@mcp.tool()
async def get_taste_profile() -> dict:
    """
    Show your tag affinity profile — which genres/tags you love and avoid,
    plus rating stats summary.
    """
    from .tools.ratings import get_taste_profile as _profile
    return await _profile()


@mcp.tool()
async def get_ratings(
    source: str | None = None,
    min_score: float | None = None,
    sort_by: str = "score",
    limit: int = 50,
) -> list[dict]:
    """
    View synced ratings.
    source: backloggd | steam_review | None (all)
    sort_by: score | name
    """
    from .tools.ratings import get_ratings as _ratings
    return await _ratings(source, min_score, sort_by, limit)


@mcp.tool()
async def sync_ratings() -> dict:
    """
    Scrape Backloggd reviews and Steam community reviews,
    upsert into ratings table, then recompute tag affinity.
    This may take 1-2 minutes depending on review count.
    """
    from .tools.ratings import sync_ratings as _sync
    return await _sync()


@mcp.tool()
async def get_backlog_stats() -> dict:
    """
    Get backlog shame stats: total games, played %, HLTB hours,
    weekly pace, years to clear, and top unplayed highlights.
    """
    from .tools.stats import get_backlog_stats as _bstats
    return await _bstats()


@mcp.tool()
async def refresh_library(platforms: list[str] | None = None) -> dict:
    """
    Re-sync game library. platforms: list like ['steam','epic'] or omit for all configured.
    Valid platforms: steam, epic, gog, nintendo, switch2, ps5
    """
    from .tools.admin import refresh_library as _refresh
    return await _refresh(platforms)


@mcp.tool()
async def get_integration_status(platforms: list[str] | None = None, verbose: bool = True) -> dict:
    """
    Inspect integration readiness for configured platforms.
    platforms: optional subset like ['steam', 'epic']; verbose=False returns a compact summary.
    """
    return _filter_integration_status(
        await _integration_status_payload(), platforms, verbose
    )


@mcp.tool()
async def detect_farmed_games(
    dry_run: bool = True,
    threshold_hours: float = 8.0,
    min_games_per_day: int = 8,
) -> dict:
    """
    Auto-detect ArchiSteamFarm card-farming sessions and mark affected games as is_farmed.

    Farming sessions appear as dozens–hundreds of games all with their last-played
    date on the same day(s), each with a tight cluster of low playtime (~2h, Steam's
    card drop cap). Farmed games are excluded from backlog stats and recommendations.

    Workflow: call with dry_run=True first to preview detected farming days and
    candidate count, then call with dry_run=False to commit the is_farmed flags.

    threshold_hours: max playtime to consider a game as a candidate (default 4h)
    min_games_per_day: minimum games on one day to flag it as a farming day (default 20)
    """
    from .tools.admin import detect_farmed_games as _detect
    return await _detect(dry_run, threshold_hours, min_games_per_day)


@mcp.tool()
async def get_platform_breakdown() -> dict:
    """
    Show game counts per platform, total unique games, and the overlap list
    (games you own on multiple platforms).
    """
    from .tools.platforms import get_platform_breakdown as _breakdown
    return await _breakdown()


@mcp.tool()
async def sync_platform(platform: str) -> dict:
    """
    Sync a single platform on demand.
    platform: steam | epic | gog | nintendo | switch | switch2 | ps5
    """
    from .tools.platforms import sync_platform as _sync
    return await _sync(platform)


@mcp.tool()
async def set_hardware_preference(platforms: list[str]) -> dict:
    """
    Set your hardware preference order used by get_recommendations to pick suggested_platform.
    Ordered list, highest priority first. e.g. ["switch2", "steam_deck", "ps5"]
    """
    from .tools.platforms import set_hardware_preference as _set_hw
    return await _set_hw(platforms)


@mcp.tool()
async def add_game_to_platform(
    name: str,
    platform: str,
    identifier_type: str | None = None,
    identifier_value: str | None = None,
    playtime_minutes: int | None = None,
) -> dict:
    """
    Manually add a game to a platform — for games that aren't synced automatically
    (e.g. physical copies, unreported digital titles, itch.io purchases).

    name: Game name (matches existing game by exact name or creates new entry)
    platform: steam | epic | gog | nintendo | switch2 | ps5 | itchio | xbox | other
    identifier_type: Optional store ID type (e.g. 'steam_appid', 'gog_product_id')
    identifier_value: Optional store ID value
    playtime_minutes: Optional known playtime
    """
    from .tools.platforms import add_game_to_platform as _add
    return await _add(name, platform, identifier_type, identifier_value, playtime_minutes)


@mcp.tool()
async def set_nintendo_session(cookies: str) -> dict:
    """
    Store Nintendo Account session cookies for VGCS library fallback sync.

    Used when nxapi is unavailable. Fetches your full digital library
    (including unplayed titles) from accounts.nintendo.com — no playtime data.

    How to get cookies:
    1. Open https://accounts.nintendo.com/portal/vgcs/ (stay logged in)
    2. Install the "Cookie Editor" browser extension
    3. Click the extension → Export → copy the JSON
    4. Pass that JSON string here

    Cookies are saved to NINTENDO_COOKIES_FILE (default: data/nintendo_cookies.json).
    """
    from .tools.admin import set_nintendo_session as _set_session
    return await _set_session(cookies)


# ── Health + admin endpoints ─────────────────────────────────────────────────

from urllib.parse import parse_qs

from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

# Paths/prefixes that must work without auth
_OPEN_PATHS = {"/health", "/"}
_OPEN_PREFIXES = ("/messages/", "/.well-known/")


class BearerAuthMiddleware:
    """Pure ASGI middleware — safe for SSE streaming (no response buffering)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not MCP_AUTH_TOKEN:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
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


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    from .data.db import get_meta
    last_sync = await get_meta("library_synced_at")
    return JSONResponse({"status": "ok", "library_synced_at": last_sync})


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


@mcp.custom_route("/admin/integrations", methods=["GET"])
async def admin_integrations(request: Request) -> JSONResponse:
    return JSONResponse(await _integration_status_payload())


@mcp.custom_route("/admin/integrations/ui", methods=["GET"])
async def admin_integrations_ui(request: Request) -> HTMLResponse:
    payload = await _integration_status_payload()
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
    return HTMLResponse(
        "<!doctype html>"
        "<html><head><title>Integration Status</title></head>"
        "<body><h1>Integration Status</h1><ul>"
        f"{body}"
        "</ul></body></html>"
    )


@mcp.custom_route("/admin/integrations/{platform}", methods=["GET"])
async def admin_integration_detail(request: Request) -> JSONResponse:
    platform = request.path_params["platform"]
    payload = await _integration_status_payload()
    if platform not in payload:
        return JSONResponse({"error": f"Unknown integration: {platform}"}, status_code=404)
    return JSONResponse(payload[platform])


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="sse", host="0.0.0.0", port=port, middleware=[Middleware(BearerAuthMiddleware)])
