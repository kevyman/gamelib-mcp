"""Shared helpers for tool handlers: platform aliasing + the steam-appid subquery.

Only code that was verified byte-identical across tool modules lives here. The
``_GAME_ROLLUP_CTE`` definitions are intentionally NOT centralized — they differ
between modules (e.g. library includes total_playtime_2weeks_minutes, discover
adds tag handling, stats selects genres and omits the steam appid), and merging
them would change query output.
"""

from fastmcp.exceptions import ToolError

from ..data.db import STEAM_APP_ID

# The platform vocabulary is defined once in platforms_registry.PLATFORMS;
# these re-exports keep the long-standing import surface for tool modules.
from ..platforms_registry import (  # noqa: F401
    LIBRARY_PLATFORMS,
    PLATFORM_ALIASES,
    SYNCABLE_PLATFORMS,
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


async def report_progress(ctx, progress: int, total: int) -> None:
    if ctx is not None:
        await ctx.report_progress(progress, total)


async def info(ctx, message: str) -> None:
    if ctx is not None:
        await ctx.info(message)
