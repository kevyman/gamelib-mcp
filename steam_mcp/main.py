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
    from .data.db import init_db, get_meta
    from .data.steam_xml import fetch_library, STALE_HOURS
    from .data.enrich_bg import background_enrich

    await init_db()
    logger.info("Database initialized")

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
        f"You have access to {_display_name}'s Steam library. "
        "Use the tools to search, filter, and get details about games. "
        "Ratings are synced from Backloggd and Steam reviews (read-only). "
        "Call sync_ratings to refresh ratings and taste profile data."
    ),
    lifespan=lifespan,
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def search_games(query: str, limit: int = 20) -> list[dict]:
    """Find games in the Steam library by name substring."""
    from .tools.library import search_games as _search
    return await _search(query, limit)


@mcp.tool()
async def get_library_stats(
    filter: str = "all",
    max_hltb_hours: float | None = None,
    min_metacritic: int | None = None,
    protondb_tier: str | None = None,
    sort_by: str = "playtime",
    limit: int = 50,
) -> dict:
    """
    Get filtered/sorted library list + aggregate stats.

    filter: all | unplayed | played | recent | farmed
    sort_by: playtime | name | metacritic | hltb
    protondb_tier: native | platinum | gold | silver | bronze | borked
    """
    from .tools.library import get_library_stats as _stats
    return await _stats(filter, max_hltb_hours, min_metacritic, protondb_tier, sort_by, limit)


@mcp.tool()
async def get_game_detail(name: str | None = None, appid: int | None = None) -> dict:
    """
    Get full details for a single game, including HLTB, OpenCritic, ProtonDB,
    and any personal ratings. Triggers lazy data fetches.
    Provide either name (partial match) or appid.
    """
    from .tools.detail import get_game_detail as _detail
    return await _detail(name, appid)


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
async def refresh_library() -> dict:
    """Force re-sync the Steam library from the public XML feed."""
    from .tools.admin import refresh_library as _refresh
    return await _refresh()


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


# ── Health endpoint ────────────────────────────────────────────────────────────

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not MCP_AUTH_TOKEN or request.url.path == "/health":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {MCP_AUTH_TOKEN}":
            return await call_next(request)
        if request.query_params.get("token") == MCP_AUTH_TOKEN:
            return await call_next(request)
        return Response("Unauthorized", status_code=401)


mcp.http_app().add_middleware(BearerAuthMiddleware)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    from .data.db import get_meta
    last_sync = await get_meta("library_synced_at")
    return JSONResponse({"status": "ok", "library_synced_at": last_sync})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
