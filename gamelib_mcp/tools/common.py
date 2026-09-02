"""Shared helpers for tool handlers: platform aliasing + the steam-appid subquery.

Only code that was verified byte-identical across tool modules lives here. The
``_GAME_ROLLUP_CTE`` definitions are intentionally NOT centralized — they differ
between modules (e.g. library includes total_playtime_2weeks_minutes, discover
adds tag handling, stats selects genres and omits the steam appid), and merging
them would change query output.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from fastmcp.exceptions import ToolError

from ..data.db import STEAM_APP_ID

# The platform vocabulary is defined once in platforms_registry.PLATFORMS;
# these re-exports keep the long-standing import surface for tool modules.
from ..platforms_registry import (  # noqa: F401
    LIBRARY_PLATFORMS,
    PLATFORM_ALIASES,
    SYNCABLE_PLATFORMS,
    resolve_platform_functions,
)

# Result-count ceiling shared by all list-returning tools. Keeps a single tool
# call from blowing the client's context with a multi-megabyte response.
MAX_RESULT_LIMIT = 200

# --- Three-state playtime classification ------------------------------------
# NULL-aware total playtime. Unlike COALESCE(SUM(COALESCE(x, 0)), 0), this
# yields NULL when EVERY contributing platform row is NULL (playtime genuinely
# unknown: GOG, manual adds, Nintendo VGCS, Epic outage), preserving the
# distinction from an authoritative 0 (e.g. Steam never-launched).
PLAYTIME_SUM_SQL = "SUM(gp.playtime_minutes)"

# Shared CASE deriving the play_state enum inside each rollup CTE. It references
# the raw NULL-aware SUM (not a column alias) because SQLite cannot see sibling
# column aliases within the same SELECT list. Requires the games table aliased
# `g` and game_platforms aliased `gp` — matches all three rollup CTEs.
#
# An explicit completion_status='completed' counts as played even when
# playtime is unknown (e.g. a GOG game with no playtime tracking). Deliberately
# NOT branching on 'abandoned' here — an abandoned-at-0-minutes game stays
# 'unplayed' by this purely playtime-derived signal; backlog/discovery exclude
# abandoned games via their own filters instead (see get_backlog_stats,
# discover_games, get_library_stats filter=abandoned).
PLAY_STATE_SQL = f"""CASE
        WHEN g.completion_status = 'completed' THEN 'played'
        WHEN g.is_farmed = 1            THEN 'unplayed'
        WHEN {PLAYTIME_SUM_SQL} IS NULL THEN 'unknown'
        WHEN {PLAYTIME_SUM_SQL} = 0     THEN 'unplayed'
        ELSE 'played'
    END"""


def resolve_platform(platform: str | None) -> str | None:
    if platform is None:
        return None
    return PLATFORM_ALIASES.get(platform.lower(), platform.lower())


def validate_platform(platform: str, allowed: frozenset[str]) -> str:
    """Resolve aliases and confirm the platform is in ``allowed``.

    Returns the canonical platform name; raises ToolError with a consistent
    message (and the valid set) otherwise.
    """
    resolved = resolve_platform(platform)
    if resolved not in allowed:
        raise ToolError(
            f"Unknown platform '{platform}'. Valid: {sorted(allowed | set(PLATFORM_ALIASES))}"
        )
    return resolved


def clamp_limit(limit: int, maximum: int = MAX_RESULT_LIMIT) -> int:
    """Clamp a user-supplied LIMIT into [0, maximum].

    Guards against context-blowing huge limits and SQLite's "negative LIMIT =
    unbounded" behaviour, which would otherwise return the whole table.
    """
    if limit < 0:
        return maximum
    return min(limit, maximum)


def like_escape(value: str) -> str:
    r"""Escape LIKE wildcards so user input matches literally.

    Use with ``LIKE ? ESCAPE '\'``. Escapes backslash first, then % and _.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Correlated subquery selecting a game's primary Steam appid, for use inside a
# query where the games table is aliased ``g``.
STEAM_APPID_SQL = f"""
(
    SELECT CAST(gpi.identifier_value AS INTEGER)
    FROM game_platform_identifiers gpi
    JOIN game_platforms sgp ON sgp.id = gpi.game_platform_id
    WHERE sgp.game_id = g.id AND gpi.identifier_type = '{STEAM_APP_ID}'
    ORDER BY gpi.is_primary DESC, gpi.id ASC
    LIMIT 1
)
"""

# Correlated subquery returning a JSON array of a game's series names (IGDB
# collections + franchises), for use where the games table is aliased ``g``.
SERIES_NAMES_SQL = """
(
    SELECT json_group_array(s.name)
    FROM (
        SELECT DISTINCT s.name
        FROM game_series_membership m
        JOIN game_series s ON s.id = m.series_id
        WHERE m.game_id = g.id
    ) s
)
"""

# Correlated ownership/wishlist EXISTS checks, for use where the games table is
# aliased ``g``. A row can exist in ``games`` with neither true (a wishlist-only
# sync creates the games row + a game_wishlist row but zero game_platforms
# rows; a manual owned=0 stub has a game_platforms row but owned=0) — callers
# that assume every ``games`` row is owned (library/discover/backlog rollups)
# must gate on OWNED_SQL explicitly rather than inferring it from
# is_primary_library_item, which is a content-type flag (game vs DLC), not an
# ownership signal. A distinct alias (gp2) from a rollup CTE's own
# game_platforms join (gp) avoids ambiguity.
OWNED_SQL = """
(
    EXISTS (
        SELECT 1 FROM game_platforms gp2
        WHERE gp2.game_id = g.id AND gp2.owned = 1
    )
)
"""

WISHLISTED_SQL = """
(
    EXISTS (
        SELECT 1 FROM game_wishlist w
        WHERE w.game_id = g.id
    )
)
"""


# Cover art is assembled at read time, never stored as a URL: IGDB's cover
# slug (games.cover_image_id, backfilled by IGDB enrichment) is preferred, and
# Steam games fall back to the store's library capsule by appid so they render
# a cover even before that backfill lands. Both hosts are public CDNs that
# need no auth; the MCP Apps widget declares them in its resource CSP.
IGDB_COVER_URL = "https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.jpg"
STEAM_CAPSULE_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"


def cover_url(cover_image_id: str | None, steam_appid: int | None) -> str | None:
    if cover_image_id:
        return IGDB_COVER_URL.format(image_id=cover_image_id)
    if steam_appid:
        return STEAM_CAPSULE_URL.format(appid=steam_appid)
    return None


async def report_progress(ctx, progress: int, total: int) -> None:
    if ctx is not None:
        await ctx.report_progress(progress, total)


async def info(ctx, message: str) -> None:
    if ctx is not None:
        await ctx.info(message)


class PlatformSyncFanout:
    """The resolve → validate → dispatch → gather skeleton of a sync fan-out.

    ``run_library_sync`` and ``sync_wishlist`` differ in what they do with each
    platform's outcome, not in how they reach it: both alias-resolve the
    caller's platform list, reject anything the selected target cannot sync,
    look the sync callables up in the platform registry, run them concurrently
    and tick the progress counter once per finished platform. Only that
    skeleton lives here — the per-outcome bookkeeping (sync-state records, play
    history, log lines) stays with each caller, which is where the two really
    diverge.

    ``unknown_message`` builds the rejection text from the unresolved names and
    the sorted valid vocabulary: the two callers word it differently (the
    wishlist one adds the PSN hint) and both wordings are asserted by tests, so
    the message stays the caller's to write.
    """

    def __init__(
        self,
        platforms: list[str] | None,
        supported: frozenset[str],
        *,
        unknown_message: Callable[[list[str], list[str]], str],
    ) -> None:
        def _resolve(p: str) -> str:
            return PLATFORM_ALIASES.get(p.lower(), p.lower())

        self.requested = list(platforms) if platforms else sorted(supported)
        unknown = [p for p in self.requested if _resolve(p) not in supported]
        if unknown:
            raise ToolError(
                unknown_message(unknown, sorted(supported | set(PLATFORM_ALIASES)))
            )
        self.targets = {_resolve(p) for p in self.requested}
        # Results echo the spelling the caller used, so an alias ("switch2" for
        # "nintendo") comes back under the alias.
        self.display_names: dict[str, str] = {name: name for name in self.targets}
        for requested in self.requested:
            self.display_names[_resolve(requested)] = requested
        self.selected: list[tuple[str, Callable[[], Awaitable[dict]]]] = []

    def dispatch(self, kind: str, *, namespace) -> list[tuple[str, Callable[[], Awaitable[dict]]]]:
        """Bind the selected targets to their registry functions.

        Resolution prefers names bound on ``namespace`` (the calling module), so
        the established ``patch("gamelib_mcp.tools.admin.sync_epic", ...)`` seam
        keeps intercepting the sync.
        """
        registry = resolve_platform_functions(kind, namespace=namespace)
        self.selected = [(name, fn) for name, fn in registry.items() if name in self.targets]
        return self.selected

    async def gather(self, ctx) -> AsyncIterator[tuple[str, object]]:
        """Run every selected sync concurrently, yielding (name, outcome).

        ``outcome`` is either the sync's result or the exception it raised —
        callers branch on ``isinstance(outcome, BaseException)``. The progress
        counter is ticked *after* each yielded outcome has been handled, which
        is where the callers' own loops ticked it.
        """
        outcomes = await asyncio.gather(
            *(fn() for _, fn in self.selected),
            return_exceptions=True,
        )
        total = len(self.selected)
        for index, ((name, _), outcome) in enumerate(
            zip(self.selected, outcomes, strict=True), start=1
        ):
            yield name, outcome
            await report_progress(ctx, index, total)
