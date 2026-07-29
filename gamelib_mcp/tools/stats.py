"""Backlog rollup behind get_stats(report="backlog")."""

from ..data.db import get_db
from .common import (
    OWNED_SQL as _OWNED_SQL,
)
from .common import (
    PLAY_STATE_SQL as _PLAY_STATE_SQL,
)
from .common import (
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
           g.completion_status,
           g.is_primary_library_item,
           {_PLAYTIME_SUM_SQL} AS total_playtime_minutes,
           {_PLAY_STATE_SQL} AS play_state,
           COALESCE(SUM(COALESCE(gp.playtime_2weeks_minutes, 0)), 0) AS total_playtime_2weeks_minutes,
           MAX(gpe.metacritic_score) AS metacritic_score,
           MAX(gpe.opencritic_score) AS opencritic_score
    FROM games g
    -- owned = 1: an owned=0 stub's playtime/enrichment must not feed the
    -- aggregates (play_state, backlog hours, best-unplayed) — it isn't real
    -- playtime anywhere. The OWNED_SQL guard below only admits the game; this
    -- join condition keeps the unowned rows out of its rollup.
    LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
    LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
    WHERE g.is_primary_library_item = 1
      -- A wishlist-only games row (games + game_wishlist, zero game_platforms
      -- rows) must not inflate backlog totals/hours or "best unplayed" picks —
      -- it was never actually owned.
      AND {_OWNED_SQL}
    GROUP BY g.id
)
"""


async def get_backlog_stats() -> dict:
    """
    Backlog shame stats plus aggregate metrics.
    Calculates pace from recent 2-week playtime data across all platforms.
    Scoped to actually-owned games only — a wishlist-only title never counts
    toward the backlog.
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
                   SUM(CASE WHEN completion_status = 'playing'   THEN 1 ELSE 0 END) AS playing,
                   SUM(CASE WHEN completion_status = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN completion_status = 'abandoned' THEN 1 ELSE 0 END) AS abandoned,
                   SUM(CASE WHEN completion_status = 'evergreen' THEN 1 ELSE 0 END) AS evergreen,
                   SUM(CASE
                           WHEN play_state = 'unplayed' AND hltb_main IS NOT NULL
                                AND (completion_status IS NULL
                                     OR completion_status NOT IN ('completed', 'abandoned', 'evergreen'))
                           THEN 1 ELSE 0
                       END) AS unplayed_with_hltb,
                   SUM(CASE
                           WHEN play_state = 'unplayed' AND hltb_main IS NOT NULL
                                AND (completion_status IS NULL
                                     OR completion_status NOT IN ('completed', 'abandoned', 'evergreen'))
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
              AND (completion_status IS NULL OR completion_status NOT IN ('completed', 'abandoned', 'evergreen'))
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
              AND (completion_status IS NULL OR completion_status NOT IN ('completed', 'abandoned', 'evergreen'))
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
              AND (completion_status IS NULL OR completion_status NOT IN ('completed', 'abandoned', 'evergreen'))
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
              AND (gr.completion_status IS NULL
                   OR gr.completion_status NOT IN ('completed', 'abandoned', 'evergreen'))
            ORDER BY r.normalized_score DESC
            LIMIT 1
            """
        )

        # Money recorded (via set_acquisition) on effectively-unplayed games:
        # play_state unplayed (summed playtime 0) or unknown (all NULL — the
        # priced copy was still never played), mirroring the backlog-hours
        # exclusions above (explicit completed is already play_state='played';
        # abandoned/evergreen are written off, not backlog). Priced rows join
        # on owned=1 like the rollup — an owned=0 stub's price is not real
        # spend on this game. Grouped per currency, never summed across.
        unplayed_spend_where = """
            WHERE gr.play_state IN ('unplayed', 'unknown')
              AND (gr.completion_status IS NULL
                   OR gr.completion_status NOT IN ('completed', 'abandoned', 'evergreen'))
              AND gp.price_paid IS NOT NULL
              AND gp.price_paid > 0
        """
        unplayed_spend_totals = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT gp.price_currency AS currency,
                   ROUND(SUM(gp.price_paid), 2) AS spent,
                   COUNT(*) AS count
            FROM game_rollup gr
            JOIN game_platforms gp ON gp.game_id = gr.game_id AND gp.owned = 1
            {unplayed_spend_where}
            GROUP BY gp.price_currency
            ORDER BY spent DESC
            """
        )
        unplayed_spend_top = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT gr.game_id, gr.name, gp.platform,
                   gp.price_paid, gp.price_currency AS currency
            FROM game_rollup gr
            JOIN game_platforms gp ON gp.game_id = gr.game_id AND gp.owned = 1
            {unplayed_spend_where}
            ORDER BY gp.price_paid DESC
            LIMIT 5
            """
        )

    total_count = summary["total_library"] or 0
    played_count = summary["played"] or 0
    unplayed_count = summary["unplayed"] or 0
    unknown_count = summary["unknown_playtime"] or 0
    farmed_count = summary["farmed_games"] or 0
    playing_count = summary["playing"] or 0
    completed_count = summary["completed"] or 0
    abandoned_count = summary["abandoned"] or 0
    evergreen_count = summary["evergreen"] or 0
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
        "playing": playing_count,
        "completed": completed_count,
        "abandoned": abandoned_count,
        "evergreen": evergreen_count,
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
        "unplayed_spend": {
            "totals": [dict(r) for r in unplayed_spend_totals],
            "top": [dict(r) for r in unplayed_spend_top],
        },
    }
