"""get_backlog_stats tool."""

from ..data.db import get_db

_GAME_ROLLUP_CTE = """
WITH game_rollup AS (
    SELECT g.id AS game_id,
           g.name,
           g.genres,
           g.hltb_main,
           g.metacritic_score,
           g.is_farmed,
           COALESCE(SUM(COALESCE(gp.playtime_minutes, 0)), 0) AS total_playtime_minutes,
           COALESCE(SUM(COALESCE(gp.playtime_2weeks_minutes, 0)), 0) AS total_playtime_2weeks_minutes
    FROM games g
    LEFT JOIN game_platforms gp ON gp.game_id = g.id
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
                   SUM(CASE WHEN total_playtime_minutes > 0 AND is_farmed = 0 THEN 1 ELSE 0 END) AS played,
                   SUM(CASE WHEN total_playtime_minutes = 0 OR is_farmed = 1 THEN 1 ELSE 0 END) AS unplayed,
                   SUM(CASE WHEN is_farmed = 1 THEN 1 ELSE 0 END) AS farmed_games,
                   SUM(CASE
                           WHEN (total_playtime_minutes = 0 OR is_farmed = 1) AND hltb_main IS NOT NULL
                           THEN 1 ELSE 0
                       END) AS unplayed_with_hltb,
                   SUM(CASE
                           WHEN (total_playtime_minutes = 0 OR is_farmed = 1) AND hltb_main IS NOT NULL
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
            WHERE (total_playtime_minutes = 0 OR is_farmed = 1)
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
            WHERE (total_playtime_minutes = 0 OR is_farmed = 1)
              AND metacritic_score IS NOT NULL
            ORDER BY metacritic_score DESC
            LIMIT 1
            """
        )
        best_unplayed_rated = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + """
            SELECT gr.name, r.normalized_score
            FROM game_rollup gr
            JOIN ratings r ON r.game_id = gr.game_id
            WHERE (gr.total_playtime_minutes = 0 OR gr.is_farmed = 1)
            ORDER BY r.normalized_score DESC
            LIMIT 1
            """
        )

    total_count = summary["total_library"] or 0
    played_count = summary["played"] or 0
    unplayed_count = summary["unplayed"] or 0
    farmed_count = summary["farmed_games"] or 0
    played_pct = round(played_count / total_count * 100) if total_count else 0

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
        "unplayed_pct": 100 - played_pct,
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
        "highest_rated_unplayed_personal": (
            {
                "name": best_unplayed_rated["name"],
                "score": best_unplayed_rated["normalized_score"],
            }
            if best_unplayed_rated
            else None
        ),
    }
