"""get_backlog_stats tool."""

from ..data.db import get_db
from .common import (
    PLAY_STATE_SQL as _PLAY_STATE_SQL,
    PLAYTIME_SUM_SQL as _PLAYTIME_SUM_SQL,
)

# NOTE: stats-specific CTE — selects genres and omits the steam appid that the
# library/discover variants compute. Kept separate on purpose; do not merge.
_GAME_ROLLUP_CTE = f"""
WITH game_rollup AS (
    SELECT g.id AS game_id,
           g.name,
           g.genres,
           g.hltb_main,
           g.is_farmed,
           g.is_primary_library_item,
           {_PLAYTIME_SUM_SQL} AS total_playtime_minutes,
           {_PLAY_STATE_SQL} AS play_state,
           COALESCE(SUM(COALESCE(gp.playtime_2weeks_minutes, 0)), 0) AS total_playtime_2weeks_minutes,
           MAX(gpe.metacritic_score) AS metacritic_score,
           MAX(gpe.opencritic_score) AS opencritic_score
    FROM games g
    LEFT JOIN game_platforms gp ON gp.game_id = g.id
    LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
    WHERE g.is_primary_library_item = 1
    GROUP BY g.id
)
"""


async def get_backlog_stats() -> dict:
    """
    Backlog shame stats plus aggregate metrics.
    Calculates pace from recent 2-week playtime data across all platforms.
    """
    async with get_db() as db:
        summary = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + """
            SELECT COUNT(*) AS total_library,
                   SUM(CASE WHEN play_state = 'played' THEN 1 ELSE 0 END) AS played,
                   SUM(CASE WHEN play_state = 'unplayed' THEN 1 ELSE 0 END) AS unplayed,
                   SUM(CASE WHEN play_state = 'unknown' THEN 1 ELSE 0 END) AS unknown_playtime,
                   SUM(CASE WHEN is_farmed = 1 THEN 1 ELSE 0 END) AS farmed_games,
                   SUM(CASE
                           WHEN play_state = 'unplayed' AND hltb_main IS NOT NULL
                           THEN 1 ELSE 0
                       END) AS unplayed_with_hltb,
                   SUM(CASE
                           WHEN play_state = 'unplayed' AND hltb_main IS NOT NULL
                           THEN hltb_main ELSE 0
                       END) AS backlog_hours_hltb,
                   SUM(total_playtime_2weeks_minutes) AS recent_minutes
            FROM game_rollup
            """
        )
        top_genre = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + """
            SELECT je.value AS genre, COUNT(*) AS c
            FROM game_rollup, json_each(game_rollup.genres) je
            WHERE play_state = 'unplayed'
            GROUP BY genre
            ORDER BY c DESC
            LIMIT 1
            """
        )
        best_unplayed_metacritic = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + """
            SELECT name, metacritic_score
            FROM game_rollup
            WHERE play_state = 'unplayed'
              AND metacritic_score IS NOT NULL
            ORDER BY metacritic_score DESC
            LIMIT 1
            """
        )
        best_unplayed_opencritic = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + """
            SELECT name, opencritic_score
            FROM game_rollup
            WHERE play_state = 'unplayed'
              AND opencritic_score IS NOT NULL
            ORDER BY opencritic_score DESC
            LIMIT 1
            """
        )
        best_unplayed_rated = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + """
            SELECT gr.name, r.normalized_score
            FROM game_rollup gr
            JOIN ratings r ON r.game_id = gr.game_id
            WHERE gr.play_state = 'unplayed'
            ORDER BY r.normalized_score DESC
            LIMIT 1
            """
        )

    total_count = summary["total_library"] or 0
    played_count = summary["played"] or 0
    unplayed_count = summary["unplayed"] or 0
    unknown_count = summary["unknown_playtime"] or 0
    farmed_count = summary["farmed_games"] or 0
    played_pct = round(played_count / total_count * 100) if total_count else 0
    unplayed_pct = round(unplayed_count / total_count * 100) if total_count else 0
    unknown_pct = round(unknown_count / total_count * 100) if total_count else 0

    backlog_hours_hltb = round(summary["backlog_hours_hltb"] or 0)
    weekly_hours = round((summary["recent_minutes"] or 0) / 2 / 60, 1)

    if weekly_hours > 0 and backlog_hours_hltb > 0:
        years_to_clear = round((backlog_hours_hltb / weekly_hours) / 52, 1)
    else:
        years_to_clear = None

    return {
        "total_library": total_count,
        "played": played_count,
        "played_pct": played_pct,
        "unplayed": unplayed_count,
        "unplayed_pct": unplayed_pct,
        "unknown_playtime": unknown_count,
        "unknown_pct": unknown_pct,
        "farmed_games": farmed_count,
        "unplayed_with_hltb": summary["unplayed_with_hltb"] or 0,
        "backlog_hours_hltb": backlog_hours_hltb,
        "weekly_pace_hours": weekly_hours,
        "years_to_clear_backlog": years_to_clear,
        "most_played_genre_in_backlog": (
            {"genre": top_genre["genre"], "count": top_genre["c"]} if top_genre else None
        ),
        "highest_rated_unplayed_metacritic": (
            {
                "name": best_unplayed_metacritic["name"],
                "score": best_unplayed_metacritic["metacritic_score"],
            }
            if best_unplayed_metacritic
            else None
        ),
        "highest_rated_unplayed_opencritic": (
            {
                "name": best_unplayed_opencritic["name"],
                "score": best_unplayed_opencritic["opencritic_score"],
            }
            if best_unplayed_opencritic
            else None
        ),
        "highest_rated_unplayed_personal": (
            {
                "name": best_unplayed_rated["name"],
                "score": best_unplayed_rated["normalized_score"],
            }
            if best_unplayed_rated
            else None
        ),
    }
