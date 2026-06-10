"""Shared helpers for tool handlers: platform aliasing + the steam-appid subquery.

Only code that was verified byte-identical across tool modules lives here. The
``_GAME_ROLLUP_CTE`` definitions are intentionally NOT centralized — they differ
between modules (e.g. library includes total_playtime_2weeks_minutes, discover
adds tag handling, stats selects genres and omits the steam appid), and merging
them would change query output.
"""

from ..data.db import STEAM_APP_ID

# Public alias → internal DB platform name
PLATFORM_ALIASES = {
    "nintendo": "switch2",
    "switch": "switch2",
}


def resolve_platform(platform: str | None) -> str | None:
    if platform is None:
        return None
    return PLATFORM_ALIASES.get(platform.lower(), platform.lower())


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
