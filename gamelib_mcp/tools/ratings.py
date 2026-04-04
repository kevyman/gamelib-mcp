"""get_ratings, sync_ratings, get_taste_profile tools."""

from ..data.backloggd import sync_backloggd
from ..data.db import STEAM_APP_ID, get_db, load_platforms_for_games, recompute_tag_affinity
from ..data.steam_reviews import sync_steam_reviews

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


async def sync_ratings() -> dict:
    """
    Scrape Backloggd plus Steam reviews, upsert into ratings,
    then recompute tag_affinity.
    """
    bl_result = await sync_backloggd()
    sr_result = await sync_steam_reviews()
    tag_count = await recompute_tag_affinity()

    return {
        "backloggd": bl_result,
        "steam_reviews": sr_result,
        "tag_affinity_tags_updated": tag_count,
        "status": "done",
    }


async def get_ratings(
    source: str | None = None,
    min_score: float | None = None,
    sort_by: str = "score",
    limit: int = 50,
) -> list[dict]:
    """
    View synced ratings.
    source: 'backloggd' | 'steam_review' | None (all)
    sort_by: 'score' | 'name'
    """
    conditions = []
    params: list = []

    if source:
        conditions.append("r.source = ?")
        params.append(source)

    if min_score is not None:
        conditions.append("r.normalized_score >= ?")
        params.append(min_score)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    order = "r.normalized_score DESC" if sort_by == "score" else "g.name ASC"

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT g.id AS game_id,
                       {_STEAM_APPID_SQL} AS steam_appid,
                       g.name,
                       r.source,
                       r.raw_score,
                       r.normalized_score,
                       r.review_text,
                       r.synced_at
                FROM ratings r
                JOIN games g ON g.id = r.game_id
                {where}
                ORDER BY {order}
                LIMIT ?""",
            (*params, limit),
        )

    platforms_by_game = await load_platforms_for_games(row["game_id"] for row in rows)
    return [
        {
            "game_id": row["game_id"],
            "appid": row["steam_appid"],
            "name": row["name"],
            "platforms": platforms_by_game.get(row["game_id"], []),
            "source": row["source"],
            "raw_score": row["raw_score"],
            "normalized_score": row["normalized_score"],
            "review_text": row["review_text"],
            "synced_at": row["synced_at"],
        }
        for row in rows
    ]


async def get_taste_profile() -> dict:
    """Show tag affinities plus rating stats summary."""
    async with get_db() as db:
        top_tags = await db.execute_fetchall(
            """SELECT tag, affinity_score, avg_score, game_count
               FROM tag_affinity
               ORDER BY affinity_score DESC
               LIMIT 20"""
        )
        bottom_tags = await db.execute_fetchall(
            """SELECT tag, affinity_score, avg_score, game_count
               FROM tag_affinity
               ORDER BY affinity_score ASC
               LIMIT 10"""
        )
        rating_stats = await db.execute_fetchone(
            """SELECT
                COUNT(*) as total_rated,
                AVG(normalized_score) as avg_score,
                MIN(normalized_score) as min_score,
                MAX(normalized_score) as max_score,
                SUM(CASE WHEN source = 'backloggd' THEN 1 ELSE 0 END) as backloggd_count,
                SUM(CASE WHEN source = 'steam_review' THEN 1 ELSE 0 END) as steam_count
               FROM ratings"""
        )

    return {
        "summary": {
            "total_rated": rating_stats["total_rated"],
            "avg_score": round(rating_stats["avg_score"], 2) if rating_stats["avg_score"] else None,
            "min_score": rating_stats["min_score"],
            "max_score": rating_stats["max_score"],
            "backloggd_ratings": rating_stats["backloggd_count"],
            "steam_review_ratings": rating_stats["steam_count"],
        },
        "top_tags": [
            {
                "tag": row["tag"],
                "affinity_score": round(row["affinity_score"], 3),
                "avg_score": round(row["avg_score"], 2),
                "game_count": row["game_count"],
            }
            for row in top_tags
        ],
        "bottom_tags": [
            {
                "tag": row["tag"],
                "affinity_score": round(row["affinity_score"], 3),
                "avg_score": round(row["avg_score"], 2),
                "game_count": row["game_count"],
            }
            for row in bottom_tags
        ],
    }
