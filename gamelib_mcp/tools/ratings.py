"""get_ratings, sync_ratings, rate_game, get_taste_profile tools."""

from datetime import datetime, timezone
from typing import Literal

from fastmcp.exceptions import ToolError

from ..data.backloggd import sync_backloggd
from ..data.db import fts_ready, get_db, load_platforms_for_games, recompute_tag_affinity, set_meta
from ..data.steam_reviews import sync_steam_reviews
from ..utils import _parse_json
from .batch import apply_batch_item, check_batch_items, count_status
from .common import (
    STEAM_APPID_SQL as _STEAM_APPID_SQL,
    clamp_limit as _clamp_limit,
    info as _info,
    report_progress,
)
from .search import (
    NORMALIZED_NAME_SQL,
    build_name_match,
    fuzzy_fallback_game_ids,
)

ResponseFormat = Literal["concise", "detailed"]

# Support shrinkage for taste-profile ranking: a tag seen on only one game can
# hit a maximal signed affinity from a single high/low rating and crowd out
# genuinely predictive multi-game tags. Rank by affinity damped toward zero by
# game_count / (game_count + k) so single-game outliers rank honestly. This is a
# display-only adjustment — the stored affinity_score is left untouched and
# still shown verbatim (discover_games applies the same damping in-query).
_SUPPORT_SHRINKAGE_K = 2.0
_SUPPORT_ADJUSTED_RANK_SQL = (
    "affinity_score * game_count / (game_count + ?)"
)
# A profile entry needs at least this many distinct games behind it — damping
# alone still let a 2-game 10/10 keyword ("cow") crack the displayed top 20,
# and half the affinity table sits at game_count <= 2.
_MIN_PROFILE_SUPPORT = 3


async def sync_ratings(ctx=None) -> dict:
    """
    Scrape Backloggd plus Steam reviews, upsert into ratings,
    then recompute tag_affinity.
    """
    await report_progress(ctx, 0, 3)
    await _info(ctx, "Syncing Backloggd ratings")
    bl_result = await sync_backloggd()
    await report_progress(ctx, 1, 3)
    await _info(ctx, "Syncing Steam review ratings")
    sr_result = await sync_steam_reviews()
    await report_progress(ctx, 2, 3)
    await _info(ctx, "Recomputing tag affinity")
    tag_count = await recompute_tag_affinity()
    await report_progress(ctx, 3, 3)
    await _info(ctx, "Finished rating sync")

    from datetime import datetime, timezone
    await set_meta("ratings_synced_at", datetime.now(timezone.utc).isoformat())

    return {
        "backloggd": bl_result,
        "steam_reviews": sr_result,
        "tag_affinity_tags_updated": tag_count,
        "status": "done",
    }


async def rate_game(
    name: str | None = None,
    game_id: int | None = None,
    score: float = 0.0,
    review_text: str | None = None,
    *,
    dry_run: bool = False,
    recompute_affinity: bool = True,
) -> dict:
    """
    Rate a game (0-10) directly, without an external rating source.

    Stored as source='manual' (re-rating overwrites); feeds tag affinity with
    full weight and immediately recomputes the taste profile.

    dry_run (internal, batch-only) validates and resolves without writing;
    recompute_affinity=False (internal, batch-only) defers the affinity
    recompute so a batch runs it once. In either case the
    tag_affinity_tags_updated key is omitted from the result.
    """
    if not 0 <= score <= 10:
        raise ToolError("score must be between 0 and 10")

    async with get_db() as db:
        if game_id is not None:
            row = await db.execute_fetchone(
                """SELECT g.id, g.name, g.tags, g.content_type, p.name AS parent_name
                   FROM games g
                   LEFT JOIN games p ON g.parent_game_id = p.id
                   WHERE g.id = ?""",
                (game_id,)
            )
        elif name is not None:
            match = build_name_match(name, column=NORMALIZED_NAME_SQL, use_fts=fts_ready())
            row = await db.execute_fetchone(
                f"""SELECT g.id, g.name, g.tags, g.content_type, p.name AS parent_name, {match.rank_sql} AS match_rank
                    FROM games g
                    LEFT JOIN games p ON g.parent_game_id = p.id
                    WHERE {match.where_sql}
                    ORDER BY match_rank ASC, length(g.name) ASC, g.id ASC
                    LIMIT 1""",
                (*match.rank_params, *match.where_params),
            )
        else:
            raise ToolError("Provide game_id or name")

    if row is None and name is not None:
        fuzzy_ids = await fuzzy_fallback_game_ids(name)
        if fuzzy_ids:
            async with get_db() as db:
                row = await db.execute_fetchone(
                    """SELECT g.id, g.name, g.tags, g.content_type, p.name AS parent_name
                       FROM games g
                       LEFT JOIN games p ON g.parent_game_id = p.id
                       WHERE g.id = ?""",
                    (fuzzy_ids[0],)
                )

    if row is None:
        raise ToolError("Game not found in library")

    if not dry_run:
        now = datetime.now(timezone.utc).isoformat()
        async with get_db() as db:
            await db.execute(
                """INSERT INTO ratings
                   (game_id, source, raw_score, normalized_score, review_text, synced_at)
                   VALUES (?, 'manual', ?, ?, ?, ?)
                   ON CONFLICT(game_id, source) DO UPDATE SET
                       raw_score = excluded.raw_score,
                       normalized_score = excluded.normalized_score,
                       review_text = excluded.review_text,
                       synced_at = excluded.synced_at""",
                (row["id"], score, score, review_text, now),
            )
            await db.commit()

    tag_count = None
    if not dry_run and recompute_affinity:
        tag_count = await recompute_tag_affinity()

    result = {
        "game_id": row["id"],
        "name": row["name"],
        "source": "manual",
        "score": score,
        "review_text": review_text,
        "tags_affected": _parse_json(row["tags"]) or [],
        "content_type": row["content_type"],
    }
    if tag_count is not None:
        result["tag_affinity_tags_updated"] = tag_count
    if row["parent_name"] is not None:
        result["parent_name"] = row["parent_name"]

    return result


_RATE_BATCH_ITEM_KEYS = frozenset({"name", "game_id", "score", "review_text"})


async def rate_games_batch(items: list[dict], dry_run: bool = False) -> dict:
    """
    Rate many games in one call; per-item errors never fail the whole batch.

    Each item takes exactly rate_game's parameters ({name or game_id} + score,
    optional review_text). All ratings are written first and tag affinity is
    recomputed ONCE at the end (rate_game recomputes per call — a 30-game batch
    would otherwise rebuild the full affinity table 30 times); the single
    recompute's tag count is reported top-level in tag_affinity_tags_updated
    (0 when nothing was written or dry_run). dry_run=True validates and
    resolves every item without writing.
    """
    check_batch_items(items)

    async def _one(**kwargs):
        return await rate_game(**kwargs, dry_run=dry_run, recompute_affinity=False)

    results = [
        await apply_batch_item(item, _RATE_BATCH_ITEM_KEYS, _one) for item in items
    ]

    ok = count_status(results, "ok")
    tag_count = 0
    if ok and not dry_run:
        tag_count = await recompute_tag_affinity()

    return {
        "results": results,
        "total": len(items),
        "ok": ok,
        "errors": count_status(results, "error"),
        "dry_run": dry_run,
        "tag_affinity_tags_updated": tag_count,
    }


async def get_ratings(
    source: str | None = None,
    min_score: float | None = None,
    sort_by: str = "score",
    limit: int = 50,
    offset: int = 0,
    response_format: ResponseFormat = "concise",
) -> dict:
    """
    View synced ratings.
    source: 'backloggd' | 'steam_review' | None (all)
    sort_by: 'score' | 'name'
    """
    limit = _clamp_limit(limit)
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
        total = await db.execute_fetchone(
            f"""SELECT COUNT(*) AS c
                FROM ratings r
                JOIN games g ON g.id = r.game_id
                {where}""",
            tuple(params),
        )
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
                LIMIT ?
                OFFSET ?""",
            (*params, limit, offset),
        )

    platforms_by_game = (
        await load_platforms_for_games(row["game_id"] for row in rows)
        if response_format == "detailed"
        else {}
    )
    results = []
    for row in rows:
        rating = {
            "game_id": row["game_id"],
            "appid": row["steam_appid"],
            "steam_appid": row["steam_appid"],
            "name": row["name"],
            "source": row["source"],
            "raw_score": row["raw_score"],
            "normalized_score": row["normalized_score"],
            "synced_at": row["synced_at"],
        }
        if response_format == "detailed":
            rating["platforms"] = platforms_by_game.get(row["game_id"], [])
            rating["review_text"] = row["review_text"]
        results.append(rating)

    return {
        "results": results,
        "total_matches": total["c"],
        "has_more": offset + len(results) < total["c"],
    }


async def get_taste_profile() -> dict:
    """Show tag affinities plus rating stats summary.

    top/bottom tags are ranked by support-shrunk affinity (affinity damped by
    game_count) and require at least _MIN_PROFILE_SUPPORT distinct games, so
    a tag seen on one or two high/low ratings doesn't outrank tags backed by
    several games. A cold-start profile where NO tag reaches the floor falls
    back to the unfloored list — a couple of ratings should still show
    something. The displayed affinity_score is the raw stored value; only
    the ordering and floor are adjusted.
    """
    async with get_db() as db:
        support_floor = f"game_count >= {_MIN_PROFILE_SUPPORT}"
        floored = await db.execute_fetchone(
            f"SELECT COUNT(*) AS c FROM tag_affinity WHERE {support_floor}"
        )
        where = support_floor if floored["c"] else "1=1"
        top_tags = await db.execute_fetchall(
            f"""SELECT tag, affinity_score, avg_score, game_count
               FROM tag_affinity
               WHERE {where}
               ORDER BY {_SUPPORT_ADJUSTED_RANK_SQL} DESC
               LIMIT 20""",
            (_SUPPORT_SHRINKAGE_K,),
        )
        bottom_tags = await db.execute_fetchall(
            f"""SELECT tag, affinity_score, avg_score, game_count
               FROM tag_affinity
               WHERE {where}
               ORDER BY {_SUPPORT_ADJUSTED_RANK_SQL} ASC
               LIMIT 10""",
            (_SUPPORT_SHRINKAGE_K,),
        )
        rating_stats = await db.execute_fetchone(
            """SELECT
                COUNT(*) as total_rated,
                AVG(normalized_score) as avg_score,
                MIN(normalized_score) as min_score,
                MAX(normalized_score) as max_score,
                SUM(CASE WHEN source = 'backloggd' THEN 1 ELSE 0 END) as backloggd_count,
                SUM(CASE WHEN source = 'steam_review' THEN 1 ELSE 0 END) as steam_count,
                SUM(CASE WHEN source = 'manual' THEN 1 ELSE 0 END) as manual_count
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
            "manual_ratings": rating_stats["manual_count"],
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
