"""Tag-affinity recomputation from rated + heavily-played games (drives recommendations)."""

import json
import math
from datetime import datetime, timezone

from . import get_db
from ..tag_synonyms import canonical_tag
from ..tags import STEAM_FEATURE_FLAGS

# Explicit ratings dominate; a playtime-derived pseudo-rating is a weak
# implicit signal (the user never said they liked it, they just played it).
SOURCE_WEIGHTS = {"backloggd": 1.0, "manual": 1.0, "steam_review": 0.5}
PLAYTIME_SIGNAL_WEIGHT = 0.3
# Below this total playtime an unrated game carries no taste signal at all —
# 2h is roughly "bounced off it", not "chose to keep playing".
MIN_PLAYTIME_SIGNAL_MINUTES = 120
# Bayesian shrinkage: phantom weight at the neutral point (the user's own mean
# rating), so a tag seen on one 10/10 game can't outrank a tag consistently
# loved across five games.
SHRINKAGE_WEIGHT = 2.0
# Pseudo-ratings cap below a true 10 so an explicit love always outranks
# inferred enthusiasm.
PLAYTIME_SCORE_CAP = 9.5


def playtime_pseudo_score(minutes: float) -> float:
    """Map hours played to a 1-10-scale pseudo-rating (log scale: 2h≈5.6, 10h≈7, 100h≈9)."""
    hours = minutes / 60.0
    return min(PLAYTIME_SCORE_CAP, 5.0 + 2.0 * math.log10(hours))


async def recompute_tag_affinity() -> int:
    """
    Recompute tag_affinity from all rated games plus playtime-implied signals.

    Per tag: affinity_score = Σ w·(score − μ) / (Σ w + SHRINKAGE_WEIGHT)

    where μ is the user's global weighted mean score, so affinity is *signed*:
    tags on games scored above the user's own average are positive, tags on
    below-average games are negative, and a tag whose games sit at the user's
    mean (e.g. a ubiquitous tag like "action") lands near zero instead of
    inheriting a big positive score from rating inflation. The shrinkage term
    damps small-sample tags toward neutral (Bayesian damped mean).

    Signals: explicit ratings (Backloggd/manual weight 1.0, Steam 0.5) plus a
    low-weight pseudo-rating for owned games with ≥2h playtime and no explicit
    rating — choosing to keep playing something is taste data too.

    avg_score stays the plain (uncentered) weighted average for display.
    Returns number of tags updated.
    """
    async with get_db() as db:
        rated_rows = await db.execute_fetchall(
            """
            SELECT r.game_id, r.source, r.normalized_score, g.tags
            FROM ratings r
            JOIN games g ON g.id = r.game_id
            WHERE g.tags IS NOT NULL AND r.normalized_score IS NOT NULL
            """
        )
        played_rows = await db.execute_fetchall(
            """
            SELECT g.id AS game_id, g.tags,
                   SUM(gp.playtime_minutes) AS total_minutes
            FROM games g
            JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
            -- Farmed games (idle/card farming) rack up huge playtime that says
            -- nothing about taste; an explicit rating on one still counts.
            -- Non-primary rows (DLC/editions/bundles) are excluded to match
            -- the discover rollup — a row that can never be recommended
            -- shouldn't shift how primary games rank.
            WHERE g.is_farmed = 0
              AND g.is_primary_library_item = 1
              AND g.tags IS NOT NULL
              AND gp.playtime_minutes IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM ratings r
                  WHERE r.game_id = g.id AND r.normalized_score IS NOT NULL
              )
            GROUP BY g.id
            HAVING total_minutes >= ?
            """,
            (MIN_PLAYTIME_SIGNAL_MINUTES,),
        )

    # (game_id, tags_json, weight, score)
    signals: list[tuple[int, str, float, float]] = [
        (
            row["game_id"],
            row["tags"],
            SOURCE_WEIGHTS.get(row["source"], 0.5),
            row["normalized_score"],
        )
        for row in rated_rows
    ] + [
        (
            row["game_id"],
            row["tags"],
            PLAYTIME_SIGNAL_WEIGHT,
            playtime_pseudo_score(row["total_minutes"]),
        )
        for row in played_rows
    ]

    total_weight = sum(w for _, _, w, _ in signals)
    global_mean = (
        sum(w * s for _, _, w, s in signals) / total_weight if total_weight else 0.0
    )

    tag_data: dict[str, dict] = {}
    for game_id, tags_json, weight, score in signals:
        try:
            tags = json.loads(tags_json)
        except (ValueError, TypeError):
            continue

        for tag in tags:
            # Storefront feature flags carry no taste signal; rows written
            # before the tags/features split may still contain them.
            if tag.lower() in STEAM_FEATURE_FLAGS:
                continue
            # Key on the canonical form so synonym variants accumulate together and
            # match the discover/library lower(value) joins against canonical tags.
            tag_key = canonical_tag(tag)
            if tag_key not in tag_data:
                tag_data[tag_key] = {
                    "weighted_sum": 0.0,
                    "centered_sum": 0.0,
                    "weight_sum": 0.0,
                    "game_ids": set(),
                }
            tag_data[tag_key]["weighted_sum"] += score * weight
            tag_data[tag_key]["centered_sum"] += (score - global_mean) * weight
            tag_data[tag_key]["weight_sum"] += weight
            tag_data[tag_key]["game_ids"].add(game_id)

    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        await db.execute("DELETE FROM tag_affinity")
        for tag, data in tag_data.items():
            if data["weight_sum"] == 0:
                continue
            avg_score = data["weighted_sum"] / data["weight_sum"]
            affinity_score = data["centered_sum"] / (
                data["weight_sum"] + SHRINKAGE_WEIGHT
            )
            await db.execute(
                """INSERT INTO tag_affinity (tag, affinity_score, avg_score, game_count, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (tag, affinity_score, avg_score, len(data["game_ids"]), now),
            )
        await db.commit()

    return len(tag_data)
