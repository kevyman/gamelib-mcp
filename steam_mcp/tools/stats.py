"""get_backlog_stats tool."""

from ..data.db import get_db


async def get_backlog_stats() -> dict:
    """
    Backlog shame stats + aggregate metrics.
    Calculates pace from recent 2-week playtime data.
    """
    async with get_db() as db:
        total = await db.execute_fetchone("SELECT COUNT(*) as c FROM games")
        played = await db.execute_fetchone(
            """SELECT COUNT(*) as c FROM games g
               LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               WHERE COALESCE(gp.playtime_minutes, 0) > 0 AND g.is_farmed = 0"""
        )
        unplayed = await db.execute_fetchone(
            """SELECT COUNT(*) as c FROM games g
               LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               WHERE COALESCE(gp.playtime_minutes, 0) = 0 OR g.is_farmed = 1"""
        )
        farmed = await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM games WHERE is_farmed = 1"
        )
        unplayed_hltb = await db.execute_fetchone(
            """SELECT COUNT(*) as c, SUM(g.hltb_main) as total_hours
               FROM games g
               LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               WHERE (COALESCE(gp.playtime_minutes, 0) = 0 OR g.is_farmed = 1)
                 AND g.hltb_main IS NOT NULL"""
        )
        recent_minutes = await db.execute_fetchone(
            """SELECT SUM(gp.playtime_2weeks_minutes) as s FROM game_platforms gp
               WHERE gp.platform = 'steam' AND gp.playtime_2weeks_minutes > 0"""
        )
        # Most-played genre in unplayed backlog
        top_genre = await db.execute_fetchone(
            """SELECT je.value as genre, COUNT(*) as c
               FROM games g
               LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               , json_each(g.genres) je
               WHERE (COALESCE(gp.playtime_minutes, 0) = 0 OR g.is_farmed = 1)
               GROUP BY genre
               ORDER BY c DESC
               LIMIT 1"""
        )
        # Highest-rated unplayed game by Metacritic
        best_unplayed_oc = await db.execute_fetchone(
            """SELECT g.name, g.metacritic_score FROM games g
               LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               WHERE (COALESCE(gp.playtime_minutes, 0) = 0 OR g.is_farmed = 1)
                 AND g.metacritic_score IS NOT NULL
               ORDER BY g.metacritic_score DESC
               LIMIT 1"""
        )
        # Highest-rated unplayed by my ratings
        best_unplayed_rated = await db.execute_fetchone(
            """SELECT g.name, r.normalized_score FROM games g
               LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               JOIN ratings r ON r.game_id = g.id
               WHERE (COALESCE(gp.playtime_minutes, 0) = 0 OR g.is_farmed = 1)
               ORDER BY r.normalized_score DESC
               LIMIT 1"""
        )

    total_count = total["c"]
    played_count = played["c"]
    unplayed_count = unplayed["c"]
    farmed_count = farmed["c"]
    played_pct = round(played_count / total_count * 100) if total_count else 0

    hltb_count = unplayed_hltb["c"] or 0
    hltb_total_hours = round(unplayed_hltb["total_hours"] or 0)

    # Weekly pace: 2-week total / 2
    two_week_minutes = recent_minutes["s"] or 0
    weekly_hours = round(two_week_minutes / 2 / 60, 1)

    # Years to clear backlog
    if weekly_hours > 0 and hltb_total_hours > 0:
        weeks_to_clear = hltb_total_hours / weekly_hours
        years_to_clear = round(weeks_to_clear / 52, 1)
    else:
        years_to_clear = None

    return {
        "total_library": total_count,
        "played": played_count,
        "played_pct": played_pct,
        "unplayed": unplayed_count,
        "unplayed_pct": 100 - played_pct,
        "farmed_games": farmed_count,
        "unplayed_with_hltb": hltb_count,
        "backlog_hours_hltb": hltb_total_hours,
        "weekly_pace_hours": weekly_hours,
        "years_to_clear_backlog": years_to_clear,
        "most_played_genre_in_backlog": (
            {"genre": top_genre["genre"], "count": top_genre["c"]} if top_genre else None
        ),
        "highest_rated_unplayed_metacritic": (
            {"name": best_unplayed_oc["name"], "score": best_unplayed_oc["metacritic_score"]}
            if best_unplayed_oc
            else None
        ),
        "highest_rated_unplayed_personal": (
            {"name": best_unplayed_rated["name"], "score": best_unplayed_rated["normalized_score"]}
            if best_unplayed_rated
            else None
        ),
    }
