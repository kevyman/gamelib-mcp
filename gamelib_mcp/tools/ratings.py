"""Ratings implementations: read, sync, manual rate, and the taste profile."""

import json
import math
from datetime import UTC, date, datetime
from typing import Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError

from ..data.backloggd import sync_backloggd
from ..data.db import (
    fts_ready,
    get_affinity_scale,
    get_db,
    load_platforms_for_games,
    recompute_tag_affinity,
    set_meta,
    strong_affinity_cut,
)
from ..data.steam_reviews import sync_steam_reviews
from ..data.tag_synonyms import canonical_tag
from ..data.tags import is_feature_flag
from ..utils import _parse_json
from .batch import apply_batch_item, check_batch_items, count_status
from .common import (
    STEAM_APPID_SQL as _STEAM_APPID_SQL,
)
from .common import (
    clamp_limit as _clamp_limit,
)
from .common import (
    info as _info,
)
from .common import (
    report_progress,
)
from .search import (
    NORMALIZED_NAME_SQL,
    build_name_match,
    fuzzy_fallback_game_ids,
)

ResponseFormat = Literal["concise", "detailed"]

# rate_next: how many candidates the taste report shows. The list is a
# suggestion queue, not a report — ten is more than anyone rates in one
# sitting, and the true candidate count rides alongside it
# (rate_next_candidates) so the cap never reads as "that's all there is".
RATE_NEXT_LIMIT = 10
# A tag this thinly covered by the rated sample is what makes a candidate
# informative; at or below this many rated games it is named in `reasons`.
_RARE_TAG_MAX_RATED = 2
# Recency bonus window, in days.
_RATE_NEXT_RECENT_DAYS = 90



async def sync_ratings(ctx: Context | None = None) -> dict:
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

    from datetime import datetime
    await set_meta("ratings_synced_at", datetime.now(UTC).isoformat())

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
        now = datetime.now(UTC).isoformat()
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

    async def _one(**kwargs: Any) -> dict:
        return await rate_game(**kwargs, dry_run=dry_run, recompute_affinity=False)

    results: list[dict] = []
    tag_count = 0
    try:
        for item in items:
            results.append(await apply_batch_item(item, _RATE_BATCH_ITEM_KEYS, _one))
    finally:
        # Even an unexpected escape mid-loop must not leave committed ratings
        # without their deferred recompute.
        if count_status(results, "ok") and not dry_run:
            tag_count = await recompute_tag_affinity()

    return {
        "results": results,
        "total": len(items),
        "ok": count_status(results, "ok"),
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


def _tag_keys(tags_json: str | None) -> list[str]:
    """Canonical, taste-bearing tags of one games row (feature flags dropped).

    Same two rules the affinity recompute applies, for the same reason: a
    storefront capability flag ("Steam Cloud") is not taste, and synonym
    variants have to collapse onto one key or the rated sample and the
    candidate would be counted against different vocabularies."""
    try:
        tags = json.loads(tags_json or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(tags, list):
        return []
    keys: list[str] = []
    for tag in tags:
        if not isinstance(tag, str) or is_feature_flag(tag):
            continue
        key = canonical_tag(tag)
        if key and key not in keys:
            keys.append(key)
    return keys


def _rate_next_entry(
    row: Any, rated_per_tag: dict[str, int], today: date
) -> dict[str, Any]:
    """Score one unrated owned game by how much rating it would TEACH.

    The heuristic (documented because it is a heuristic, not a measurement):

        log1p(total playtime minutes)
      + Σ over the game's tags of 1 / (1 + rated games carrying that tag)
      + 0.5 when the platform last reported play within 90 days

    Playtime is the evidence the user already has and the ranker does not —
    a game with 200 hours and no rating is the largest single gap in the
    profile — and it is logged so a 900-hour outlier cannot own the whole
    list. The rarity term is the information-value half: a tag the rated
    sample barely covers has almost no affinity evidence behind it, so one
    rating there moves the profile far more than a twentieth roguelike does,
    and 1/(1+n) decays exactly that way. The recency bonus is a tiebreak
    about the RATER, not the game: an opinion about something played last
    week is easier to give and more likely to be right.
    """
    minutes = row["playtime_minutes"] or 0
    tag_keys = _tag_keys(row["tags"])
    rarity = sum(1.0 / (1 + rated_per_tag.get(key, 0)) for key in tag_keys)
    score = math.log1p(minutes) + rarity

    hours = round(minutes / 60, 1)
    reasons = [
        f"{hours:g}h played, unrated" if minutes else "owned but never played, unrated"
    ]
    rare = sorted(
        (key for key in tag_keys if rated_per_tag.get(key, 0) <= _RARE_TAG_MAX_RATED),
        key=lambda key: (rated_per_tag.get(key, 0), key),
    )
    if rare:
        shown = rare[:3]
        reasons.append(f"{len(rare)} rarely-rated tags: {', '.join(shown)}")

    last_played = row["last_played"]
    recent = False
    if last_played:
        try:
            recent = (today - date.fromisoformat(last_played)).days <= _RATE_NEXT_RECENT_DAYS
        except ValueError:
            recent = False
    if recent:
        score += 0.5
        reasons.append(f"played in the last {_RATE_NEXT_RECENT_DAYS} days")

    return {
        "game_id": row["game_id"],
        "name": row["name"],
        "playtime_hours": hours,
        "last_played": last_played,
        "score": round(score, 3),
        "reasons": reasons,
    }


async def get_rate_next(limit: int = RATE_NEXT_LIMIT) -> tuple[list[dict], int]:
    """Owned, unrated games worth rating next — (capped list, true count).

    Candidates are owned primary library items with tags, no rating row, and
    a completion_status that is not completed/abandoned (an abandoned game's
    rating is the one the user already declined to give). Ranked by
    ``_rate_next_entry``'s information-value heuristic.
    """
    async with get_db() as db:
        rated_rows = await db.execute_fetchall(
            """SELECT g.tags
               FROM games g
               WHERE g.tags IS NOT NULL
                 AND EXISTS (SELECT 1 FROM ratings r WHERE r.game_id = g.id)"""
        )
        candidate_rows = await db.execute_fetchall(
            """SELECT g.id AS game_id, g.name, g.tags,
                      SUM(gp.playtime_minutes) AS playtime_minutes,
                      MAX(gp.last_played) AS last_played
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
               WHERE g.is_primary_library_item = 1
                 AND g.tags IS NOT NULL
                 AND (g.completion_status IS NULL
                      OR g.completion_status NOT IN ('completed', 'abandoned'))
                 AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.game_id = g.id)
               GROUP BY g.id"""
        )

    rated_per_tag: dict[str, int] = {}
    for row in rated_rows:
        for key in _tag_keys(row["tags"]):
            rated_per_tag[key] = rated_per_tag.get(key, 0) + 1

    today = datetime.now(UTC).date()
    entries = [_rate_next_entry(row, rated_per_tag, today) for row in candidate_rows]
    # A tagless-after-filtering candidate scores on playtime alone rather than
    # being dropped: it is still an unrated owned game, just an uninformative
    # one, and it sorts itself to the bottom.
    entries.sort(key=lambda e: (-e["score"], e["name"]))
    return entries[:limit], len(entries)


async def get_taste_profile(include_rate_next: bool = True) -> dict:
    """Show tag affinities plus rating stats summary.

    top/bottom tags rank on affinity_score directly. No display-time damping or
    support floor: affinity_score is already the shrunk posterior deviation
    (data/db/affinity.py), so a tag seen on one high rating sorts near zero on
    its own merits rather than needing to be filtered out afterwards.

    `shrinkage` reports the estimated scale behind those numbers — the prior
    weight k, the variance components it came from, and the `strong_affinity`
    rank cut consumers should threshold against instead of a constant.

    `rate_next` (capped at RATE_NEXT_LIMIT, with the true `rate_next_candidates`
    count) is the coverage half of the same picture: owned, unrated games
    ranked by how much rating them would teach the profile. See
    ``_rate_next_entry`` for the heuristic and what each term is for.
    """
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
                SUM(CASE WHEN source = 'steam_review' THEN 1 ELSE 0 END) as steam_count,
                SUM(CASE WHEN source = 'manual' THEN 1 ELSE 0 END) as manual_count
               FROM ratings"""
        )

    scale = await get_affinity_scale()
    strong_cut = await strong_affinity_cut()
    # get_assessment_context reads this profile only for its fit block and
    # passes include_rate_next=False: the two rate_next scans are the taste
    # report's business, not every assessment's.
    if include_rate_next:
        rate_next, rate_next_candidates = await get_rate_next()
    else:
        rate_next, rate_next_candidates = [], 0

    return {
        "shrinkage": {
            "prior_weight": scale.get("shrinkage_weight"),
            "sigma2_within": scale.get("sigma2_within"),
            "sigma2_between": scale.get("sigma2_between"),
            "strong_affinity": strong_cut,
            "note": (
                "affinity = Σw(score − mean) / (Σw + prior_weight); prior_weight "
                "is estimated per recompute as σ²_within/σ²_between, so scores "
                "are comparable to each other and to strong_affinity, not to a "
                "fixed threshold."
            ),
        },
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
        # Coverage, not taste: the owned games whose ratings would teach the
        # profile the most. Capped at RATE_NEXT_LIMIT with the true candidate
        # count beside it.
        "rate_next": rate_next,
        "rate_next_candidates": rate_next_candidates,
    }
