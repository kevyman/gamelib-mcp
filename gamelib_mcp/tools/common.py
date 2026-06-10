"""Shared helpers for tool handlers: platform aliasing + the steam-appid subquery.

Only code that was verified byte-identical across tool modules lives here. The
``_GAME_ROLLUP_CTE`` definitions are intentionally NOT centralized — they differ
between modules (e.g. library includes total_playtime_2weeks_minutes, discover
adds tag handling, stats selects genres and omits the steam appid), and merging
them would change query output.
"""

from fastmcp.exceptions import ToolError

from ..data.db import STEAM_APP_ID

# Public alias → internal DB platform name
PLATFORM_ALIASES = {
    "nintendo": "switch2",
    "switch": "switch2",
}

# Platforms with an automated sync backend (canonical, post-alias names).
SYNCABLE_PLATFORMS = frozenset({"steam", "epic", "gog", "switch2", "ps5"})

# Every platform a game can be recorded against in the library (post-alias).
# Superset of SYNCABLE_PLATFORMS plus manual-only stores.
LIBRARY_PLATFORMS = SYNCABLE_PLATFORMS | {"itchio", "xbox", "other"}

# Result-count ceiling shared by all list-returning tools. Keeps a single tool
# call from blowing the client's context with a multi-megabyte response.
MAX_RESULT_LIMIT = 200


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


async def report_progress(ctx, progress: int, total: int) -> None:
    if ctx is not None:
        await ctx.report_progress(progress, total)


async def info(ctx, message: str) -> None:
    if ctx is not None:
        await ctx.info(message)
