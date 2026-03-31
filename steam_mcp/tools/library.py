"""search_games and get_library_stats tools."""

from ..data.db import STEAM_APP_ID, get_db, load_platforms_for_games

SORT_COLUMNS = {
    "playtime": "total_playtime_minutes",
    "name": "name",
    "metacritic": "metacritic_score",
    "hltb": "hltb_main",
}

_STEAM_APPID_SQL = f"""
(
    SELECT CAST(gpi.identifier_value AS INTEGER)
    FROM game_platform_identifiers gpi
    JOIN game_platforms sgp ON sgp.id = gpi.game_platform_id
    WHERE sgp.game_id = g.id AND gpi.identifier_type = '{STEAM_APP_ID}'
    ORDER BY gpi.is_primary DESC, gpi.id ASC
    LIMIT 1
)
"""

_GAME_ROLLUP_CTE = f"""
WITH game_rollup AS (
    SELECT g.id AS game_id,
           g.name,
           {_STEAM_APPID_SQL} AS steam_appid,
           g.tags,
           g.hltb_main,
           g.metacritic_score,
           g.is_farmed,
           COALESCE(SUM(COALESCE(gp.playtime_minutes, 0)), 0) AS total_playtime_minutes,
           COALESCE(SUM(COALESCE(gp.playtime_2weeks_minutes, 0)), 0) AS total_playtime_2weeks_minutes,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.protondb_tier END) AS protondb_tier,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.steam_review_desc END) AS steam_review_desc
    FROM games g
    LEFT JOIN game_platforms gp ON gp.game_id = g.id
    LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
    GROUP BY g.id
)
"""


async def search_games(query: str, limit: int = 20) -> list[dict]:
    """Find games in the library by name substring match."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + """
            SELECT *
            FROM game_rollup
            WHERE lower(name) LIKE lower(?)
            ORDER BY total_playtime_minutes DESC, name ASC
            LIMIT ?
            """,
            (f"%{query}%", limit),
        )
    return await _format_rows(rows)


async def search_games_batch(
    queries: list[str],
    limit_per_query: int = 5,
) -> dict[str, list[dict]]:
    """Look up multiple game names in one call. Returns dict keyed by query."""
    async with get_db() as db:
        results = {}
        for query in queries:
            rows = await db.execute_fetchall(
                _GAME_ROLLUP_CTE
                + """
                SELECT *
                FROM game_rollup
                WHERE lower(name) LIKE lower(?)
                ORDER BY total_playtime_minutes DESC, name ASC
                LIMIT ?
                """,
                (f"%{query}%", limit_per_query),
            )
            results[query] = await _format_rows(rows)
    return results


async def get_library_stats(
    filter: str = "all",
    max_hltb_hours: float | None = None,
    min_metacritic: int | None = None,
    protondb_tier: str | None = None,
    sort_by: str = "playtime",
    limit: int = 50,
) -> dict:
    """
    Return filtered/sorted game list plus aggregate stats.

    filter: all | unplayed | played | recent | farmed
    sort_by: playtime | name | metacritic | hltb
    """
    conditions = []
    params: list = []

    if filter == "unplayed":
        conditions.append("(total_playtime_minutes = 0 OR is_farmed = 1)")
    elif filter == "played":
        conditions.append("(total_playtime_minutes > 0 AND is_farmed = 0)")
    elif filter == "recent":
        conditions.append("total_playtime_2weeks_minutes > 0")
    elif filter == "farmed":
        conditions.append("is_farmed = 1")

    if max_hltb_hours is not None:
        conditions.append("hltb_main <= ?")
        params.append(max_hltb_hours)

    if min_metacritic is not None:
        conditions.append("metacritic_score >= ?")
        params.append(min_metacritic)

    if protondb_tier is not None:
        from ..data.protondb import TIER_ORDER

        tier_lower = protondb_tier.lower()
        min_rank = TIER_ORDER.index(tier_lower) if tier_lower in TIER_ORDER else 999
        allowed = [tier for index, tier in enumerate(TIER_ORDER) if index <= min_rank]
        placeholders = ",".join("?" * len(allowed))
        conditions.append(f"lower(COALESCE(protondb_tier, '')) IN ({placeholders})")
        params.extend(allowed)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sort_col = SORT_COLUMNS.get(sort_by, "total_playtime_minutes")
    sort_dir = "ASC" if sort_by == "name" else "DESC"

    async with get_db() as db:
        rows = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT *
            FROM game_rollup
            {where}
            ORDER BY {sort_col} {sort_dir} NULLS LAST, name ASC
            LIMIT ?
            """,
            (*params, limit),
        )
        summary = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + """
            SELECT COUNT(*) AS total_games,
                   SUM(CASE WHEN total_playtime_minutes > 0 AND is_farmed = 0 THEN 1 ELSE 0 END) AS played,
                   SUM(CASE WHEN total_playtime_minutes = 0 OR is_farmed = 1 THEN 1 ELSE 0 END) AS unplayed,
                   SUM(CASE WHEN is_farmed = 1 THEN 1 ELSE 0 END) AS farmed_games,
                   SUM(total_playtime_minutes) AS total_minutes
            FROM game_rollup
            """
        )

    return {
        "total_games": summary["total_games"],
        "played": summary["played"] or 0,
        "unplayed": summary["unplayed"] or 0,
        "farmed_games": summary["farmed_games"] or 0,
        "total_playtime_hours": round((summary["total_minutes"] or 0) / 60, 1),
        "filter": filter,
        "sort_by": sort_by,
        "results": await _format_rows(rows),
    }


async def _format_rows(rows) -> list[dict]:
    platforms_by_game = await load_platforms_for_games(row["game_id"] for row in rows)
    return [
        _format_game(row, platforms_by_game.get(row["game_id"], []))
        for row in rows
    ]


def _format_game(row, platforms: list[dict]) -> dict:
    return {
        "game_id": row["game_id"],
        "appid": row["steam_appid"],
        "name": row["name"],
        "platforms": platforms,
        "playtime_hours": round((row["total_playtime_minutes"] or 0) / 60, 1),
        "playtime_2weeks_hours": round((row["total_playtime_2weeks_minutes"] or 0) / 60, 1),
        "hltb_main": row["hltb_main"],
        "metacritic_score": row["metacritic_score"],
        "protondb_tier": row["protondb_tier"],
        "steam_review_desc": row["steam_review_desc"],
        "is_farmed": bool(row["is_farmed"]),
    }
