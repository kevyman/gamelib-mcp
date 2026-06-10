"""FastMCP server — app definition, MCP tool registration, auth, HTTP transport.

Startup/shutdown and background-task orchestration live in ``lifecycle.py``; the
bearer-auth middleware and HTTP admin routes live in ``http_admin.py``. This
module stays deliberately thin: the FastMCP instance, the tool passthrough
decorators (whose signatures and docstrings are the MCP wire schema), and the
ASGI entry point.
"""

import logging
import os
from typing import Literal

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from .env import load_project_dotenv

load_project_dotenv()

from .http_admin import BearerAuthMiddleware, register_http_routes
from .lifecycle import lifespan
from .tools.integrations import get_integration_status as _filter_integration_status
from .tools.models import (
    AddGameToPlatformResponse,
    BacklogStatsResponse,
    DetectFarmedGamesResponse,
    GameDetailResponse,
    HardwarePreferenceResponse,
    IntegrationStatusResponse,
    LibraryStatsResponse,
    NintendoSessionResponse,
    PaginatedGamesResponse,
    PlatformBreakdownResponse,
    RatingsResponse,
    RefreshLibraryResponse,
    SearchGamesBatchResponse,
    SyncPlatformResponse,
    SyncRatingsResponse,
    TasteProfileResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_display_name = os.getenv("STEAM_PROFILE_ID") or os.getenv("BACKLOGGD_USER") or "the configured user"

READ_ONLY_TOOL = ToolAnnotations(readOnlyHint=True, idempotentHint=True)
FARM_DETECTION_TOOL = ToolAnnotations(destructiveHint=False, idempotentHint=True)
NETWORK_SYNC_TOOL = ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=True)
MUTATION_TOOL = ToolAnnotations(readOnlyHint=False, idempotentHint=True)

mcp = FastMCP(
    name="game-library",
    instructions=(
        f"You have access to {_display_name}'s game library across synced platforms and stores. "
        "Use sync_ratings first when recommendations or vibe discovery should reflect current "
        "Backloggd and Steam review data, then use get_recommendations or find_games_by_vibe "
        "to discover what to play next. Use search and detail tools for known games, and prefer "
        "concise list responses with offset pagination when available for larger result sets."
    ),
    lifespan=lifespan,
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool(annotations=READ_ONLY_TOOL)
async def search_games(
    query: str,
    limit: int = 20,
    offset: int = 0,
    platform: str | None = None,
    response_format: Literal["concise", "detailed"] = "concise",
) -> PaginatedGamesResponse:
    """
    Find games in the library by name substring.

    Use this for quick lookup when you know part of a title; prefer get_game_detail
    after selecting one result. platform can filter to steam, epic, gog, nintendo,
    switch2, or ps5. response_format=concise omits platform arrays; detailed
    includes them. Returns results, total_matches, and has_more.
    """
    from .tools.library import search_games as _search
    return await _search(query, limit, offset, platform, response_format)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def search_games_batch(queries: list[str], limit_per_query: int = 5) -> SearchGamesBatchResponse:
    """
    Look up multiple game names in one read-only call.

    Use this instead of repeatedly calling search_games when comparing or resolving
    several titles. limit_per_query caps matches per query. Returns a dictionary
    keyed by the original query, with matching game summary lists as values.
    """
    from .tools.library import search_games_batch as _batch
    return await _batch(queries, limit_per_query)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def get_library_stats(
    filter: str = "all",
    max_hltb_hours: float | None = None,
    min_metacritic: int | None = None,
    protondb_tier: str | None = None,
    sort_by: str = "playtime",
    limit: int = 50,
    offset: int = 0,
    platform: str | None = None,
    response_format: Literal["concise", "detailed"] = "concise",
) -> LibraryStatsResponse:
    """
    Get aggregate library stats plus a filtered and sorted game list.

    Use this for backlog slices, unplayed lists, recent activity, or farmed-game
    audits; prefer get_game_detail for one selected game. filter accepts all,
    unplayed, played, recent, or farmed. sort_by accepts playtime, name,
    metacritic, or hltb. protondb_tier accepts native, platinum, gold, silver,
    bronze, or borked. platform can filter to steam, epic, gog, nintendo,
    switch2, or ps5. response_format=concise omits platform arrays. Returns
    aggregate counts, paged results, total_matches, and has_more.
    """
    from .tools.library import get_library_stats as _stats
    return await _stats(
        filter,
        max_hltb_hours,
        min_metacritic,
        protondb_tier,
        sort_by,
        limit,
        offset,
        platform,
        response_format,
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
async def get_game_detail(
    name: str | None = None,
    appid: int | None = None,
    game_id: int | None = None,
) -> GameDetailResponse:
    """
    Get full details for one game.

    Use this after search_games or recommendations when you need platform
    ownership, HLTB, Metacritic, ProtonDB, tags, and personal ratings. Provide
    game_id, name as a partial match, or Steam appid when available. This may
    trigger lazy metadata fetches. Returns one detailed game dictionary.
    """
    from .tools.detail import get_game_detail as _detail
    return await _detail(name, appid, game_id)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def find_games_by_vibe(
    vibe: str,
    max_hltb_hours: float | None = None,
    unplayed_only: bool = True,
    protondb_min_tier: str | None = None,
    limit: int = 20,
    offset: int = 0,
    response_format: Literal["concise", "detailed"] = "concise",
) -> PaginatedGamesResponse:
    """
    Find games matching a genre, mood, or tag vibe.

    Use this for discovery when the desired feel is known; prefer
    get_recommendations for personalized ranking from synced ratings. vibe can
    be roguelike, cozy, horror, metroidvania, souls, open world, crafting,
    puzzle, platformer, rpg, strategy, simulation, stealth, narrative, co-op,
    shooter, survival, indie, cyberpunk, fantasy, or a raw tag string.
    protondb_min_tier filters PC compatibility. response_format=concise omits
    platform arrays and tags. Returns results, total_matches, and has_more.
    """
    from .tools.discover import find_games_by_vibe as _vibe
    return await _vibe(
        vibe,
        max_hltb_hours,
        unplayed_only,
        protondb_min_tier,
        limit,
        offset,
        response_format,
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
async def get_recommendations(
    max_hltb_hours: float | None = None,
    unplayed_only: bool = True,
    limit: int = 20,
    offset: int = 0,
    response_format: Literal["concise", "detailed"] = "concise",
) -> PaginatedGamesResponse:
    """
    Get personalized game recommendations from synced rating taste data.

    Use this after sync_ratings when you want ranked games to play next; prefer
    find_games_by_vibe when the request is about a specific mood or genre.
    max_hltb_hours limits completion length, unplayed_only defaults to true, and
    limit caps returned rows. response_format=concise omits platform arrays and
    tags. Returns results, total_matches, and has_more.
    """
    from .tools.discover import get_recommendations as _rec
    return await _rec(max_hltb_hours, unplayed_only, limit, offset, response_format)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def get_taste_profile() -> TasteProfileResponse:
    """
    Show the current tag affinity profile.

    Use this to explain why recommendations rank certain genres or tags highly;
    call sync_ratings first if the profile may be stale. Returns loved and
    avoided tags plus rating source and score summaries.
    """
    from .tools.ratings import get_taste_profile as _profile
    return await _profile()


@mcp.tool(annotations=READ_ONLY_TOOL)
async def get_ratings(
    source: str | None = None,
    min_score: float | None = None,
    sort_by: str = "score",
    limit: int = 50,
    offset: int = 0,
    response_format: Literal["concise", "detailed"] = "concise",
) -> RatingsResponse:
    """
    View synced personal ratings.

    Use this to inspect the raw rating inputs behind taste profile and
    recommendations. source can be backloggd, steam_review, or omitted for all
    sources. sort_by accepts score or name. response_format=concise omits
    platform arrays and review_text. Returns results, total_matches, and has_more.
    """
    from .tools.ratings import get_ratings as _ratings
    return await _ratings(source, min_score, sort_by, limit, offset, response_format)


@mcp.tool(annotations=NETWORK_SYNC_TOOL)
async def sync_ratings(ctx: Context) -> SyncRatingsResponse:
    """
    Refresh ratings and recompute the taste profile.

    Use this before get_recommendations, find_games_by_vibe comparisons, or
    get_taste_profile when external ratings may have changed. It scrapes
    Backloggd and Steam community reviews, upserts ratings, and recalculates tag
    affinity. This may take 1-2 minutes. Returns a sync summary dictionary.
    """
    from .tools.ratings import sync_ratings as _sync
    return await _sync(ctx=ctx)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def get_backlog_stats() -> BacklogStatsResponse:
    """
    Get backlog completion and time-to-clear stats.

    Use this for high-level backlog health, weekly pace, years to clear, and top
    unplayed highlights; prefer get_library_stats for the underlying filtered
    game list. Returns aggregate backlog metrics and highlight games.
    """
    from .tools.stats import get_backlog_stats as _bstats
    return await _bstats()


@mcp.tool(annotations=NETWORK_SYNC_TOOL)
async def refresh_library(
    ctx: Context,
    platforms: list[str] | None = None,
) -> RefreshLibraryResponse:
    """
    Re-sync the owned game library from configured platforms.

    Use this when platform libraries may have changed; prefer sync_platform for
    one specific service. platforms can be omitted for all configured platforms
    or set to steam, epic, gog, nintendo, switch2, or ps5. Returns a per-platform
    sync summary dictionary.
    """
    from .tools.admin import refresh_library as _refresh
    return await _refresh(platforms, ctx=ctx)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def get_integration_status(
    platforms: list[str] | None = None,
    verbose: bool = True,
) -> IntegrationStatusResponse:
    """
    Inspect platform integration readiness.

    Use this before syncing to see which credentials or integrations are
    configured. platforms can be an optional subset such as steam or epic.
    verbose=False returns a compact summary. Returns platform status details.
    """
    from .http_admin import _integration_status_payload
    return _filter_integration_status(
        await _integration_status_payload(), platforms, verbose
    )


@mcp.tool(annotations=FARM_DETECTION_TOOL)
async def detect_farmed_games(
    dry_run: bool = True,
    threshold_hours: float = 8.0,
    min_games_per_day: int = 8,
) -> DetectFarmedGamesResponse:
    """
    Detect ArchiSteamFarm card-farming sessions and optionally mark games as farmed.

    Use dry_run=True first to preview detected farming days and candidates, then
    call with dry_run=False only when the candidates should be marked is_farmed.
    Farmed games are excluded from backlog stats and recommendations.

    Farming sessions appear as many games with the same last-played date and a
    tight low-playtime cluster. threshold_hours is the max candidate playtime
    (default 8.0h). min_games_per_day flags days with at least that many games
    (default 8). Returns candidate counts, detected days, and update counts.
    """
    from .tools.admin import detect_farmed_games as _detect
    return await _detect(dry_run, threshold_hours, min_games_per_day)


@mcp.tool(annotations=READ_ONLY_TOOL)
async def get_platform_breakdown() -> PlatformBreakdownResponse:
    """
    Show ownership counts and overlap by platform.

    Use this to compare platform coverage or find duplicate ownership. Returns
    per-platform game counts, total unique games, and games owned on multiple
    platforms.
    """
    from .tools.platforms import get_platform_breakdown as _breakdown
    return await _breakdown()


@mcp.tool(annotations=NETWORK_SYNC_TOOL)
async def sync_platform(platform: str, ctx: Context) -> SyncPlatformResponse:
    """
    Sync one platform on demand.

    Use this when only one service needs refresh; prefer refresh_library when
    syncing all configured services. platform accepts steam, epic, gog,
    nintendo, switch, switch2, or ps5. Returns that platform's sync result.
    """
    from .tools.platforms import sync_platform as _sync
    return await _sync(platform, ctx=ctx)


@mcp.tool(annotations=MUTATION_TOOL)
async def set_hardware_preference(platforms: list[str]) -> HardwarePreferenceResponse:
    """
    Set the hardware preference order used for recommendations.

    Use this when suggested_platform should prioritize specific hardware.
    platforms is an ordered list from highest priority to lowest, for example
    ["switch2", "ps5", "steam"]. Returns the saved preference order.
    """
    from .tools.platforms import set_hardware_preference as _set_hw
    return await _set_hw(platforms)


@mcp.tool(annotations=MUTATION_TOOL)
async def add_game_to_platform(
    name: str,
    platform: str,
    identifier_type: str | None = None,
    identifier_value: str | None = None,
    playtime_minutes: int | None = None,
) -> AddGameToPlatformResponse:
    """
    Manually add a game to a platform.

    Use this for physical copies, unreported digital titles, itch.io purchases,
    or other games that are not synced automatically. name matches an existing
    game by exact name or creates a new entry. platform accepts steam, epic, gog,
    nintendo, switch2, ps5, itchio, xbox, or other. identifier_type and
    identifier_value can store an external ID. playtime_minutes is optional.
    Returns the created or updated platform ownership record.
    """
    from .tools.platforms import add_game_to_platform as _add
    return await _add(name, platform, identifier_type, identifier_value, playtime_minutes)


@mcp.tool(annotations=MUTATION_TOOL)
async def set_nintendo_session(cookies: str) -> NintendoSessionResponse:
    """
    Store Nintendo Account session cookies for VGCS fallback sync.

    Use this when nxapi is unavailable and Nintendo digital ownership should be
    synced from accounts.nintendo.com. The cookie JSON comes from an authenticated
    browser session; no playtime data is available through this fallback. Returns
    a session storage status dictionary.

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

register_http_routes(mcp)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from starlette.middleware import Middleware

    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="http", host="0.0.0.0", port=port, middleware=[Middleware(BearerAuthMiddleware)])
