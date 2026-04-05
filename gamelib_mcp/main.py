"""FastMCP server — lifespan, auth, SSE transport, all 10 tools."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")


@asynccontextmanager
async def lifespan(app):
    """Startup: init DB, sync library if stale, kick off HLTB pre-warm."""
    from .data.db import init_db, get_meta, set_meta
    from .data.steam_xml import fetch_library, STALE_HOURS
    from .data.enrich_bg import background_enrich

    await init_db()
    logger.info("Database initialized")

    # Seed hardware preference from env if not yet set
    hw_pref_env = os.getenv("HARDWARE_PREFERENCE")
    if hw_pref_env and not await get_meta("hardware_preference"):
        import json
        await set_meta("hardware_preference", json.dumps(hw_pref_env.split(",")))
        logger.info("Seeded hardware_preference from HARDWARE_PREFERENCE env var")

    # Refresh library if stale or missing
    last_sync = await get_meta("library_synced_at")
    needs_refresh = True
    if last_sync:
        try:
            dt = datetime.fromisoformat(last_sync)
            age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            needs_refresh = age_hours > STALE_HOURS
        except ValueError:
            pass

    if needs_refresh:
        logger.info("Library stale or missing — fetching Steam XML feed...")
        try:
            result = await fetch_library()
            logger.info("Library sync: %s", result)
        except Exception as e:
            logger.error("Library sync failed: %s", e)

    # Background enrichment: Steam Store, HLTB, ProtonDB (non-blocking)
    asyncio.create_task(background_enrich())

    yield

    logger.info("Shutdown")


_display_name = os.getenv("STEAM_PROFILE_ID") or "the configured user"

mcp = FastMCP(
    name="steam-library",
    instructions=(
        f"You have access to {_display_name}'s game library across Steam and any synced stores. "
        "Use the tools to search, filter, and get details about games and platforms. "
        "Ratings are synced from Backloggd and Steam reviews (read-only). "
        "Call sync_ratings to refresh ratings and taste profile data."
    ),
    lifespan=lifespan,
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def search_games(query: str, limit: int = 20, platform: str | None = None) -> list[dict]:
    """Find games in the library by name substring. platform: steam|epic|gog|nintendo|ps5"""
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
    platform: steam | epic | gog | nintendo | ps5 (optional — filter to games on that platform)
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
    Provide game_id, name (partial match), or Steam appid.
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
    Valid platforms: steam, epic, gog, nintendo, ps5
    """
    from .tools.admin import refresh_library as _refresh
    return await _refresh(platforms)


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
    platform: steam | epic | gog | nintendo | ps5
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
    platform: steam | epic | gog | nintendo | ps5 | itchio | xbox | other
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


# ── Health endpoint ────────────────────────────────────────────────────────────

from urllib.parse import parse_qs

from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

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


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="sse", host="0.0.0.0", port=port, middleware=[Middleware(BearerAuthMiddleware)])
