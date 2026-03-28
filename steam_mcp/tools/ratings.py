"""get_ratings, sync_ratings, get_taste_profile tools."""

from ..data.backloggd import sync_backloggd
from ..data.steam_reviews import sync_steam_reviews
from ..data.db import get_db, recompute_tag_affinity


async def sync_ratings() -> dict:
    """
    Scrape Backloggd + Steam reviews, upsert into ratings,
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
            f"""SELECT g.appid, g.name, r.source, r.raw_score, r.normalized_score,
                       r.review_text, r.synced_at
                FROM ratings r
                JOIN games g ON g.id = r.game_id
                {where}
                ORDER BY {order}
                LIMIT ?""",
            (*params, limit),
        )

    return [
        {
            "appid": r["appid"],
            "name": r["name"],
            "source": r["source"],
            "raw_score": r["raw_score"],
            "normalized_score": r["normalized_score"],
            "review_text": r["review_text"],
            "synced_at": r["synced_at"],
        }
        for r in rows
    ]


async def get_taste_profile() -> dict:
    """Show tag affinities + rating stats summary."""
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
                "tag": r["tag"],
                "affinity_score": round(r["affinity_score"], 3),
                "avg_score": round(r["avg_score"], 2),
                "game_count": r["game_count"],
            }
            for r in top_tags
        ],
        "bottom_tags": [
            {
                "tag": r["tag"],
                "affinity_score": round(r["affinity_score"], 3),
                "avg_score": round(r["avg_score"], 2),
                "game_count": r["game_count"],
            }
            for r in bottom_tags
        ],
    }
