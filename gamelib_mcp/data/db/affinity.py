"""Tag-affinity recomputation from rated + heavily-played games (drives recommendations).

DLC/nested content handling: Explicit ratings on nested rows (DLC, expansions,
etc.) DO contribute to tag affinity — a deliberate 10/10 on an expansion like
"The Old Hunters" is a real taste signal for its parent game's tags. However,
implicit playtime pseudo-ratings remain primary-only (is_primary_library_item = 1
filter), since non-primary rows are excluded from discovery rollups and should
not shift how primary games rank in recommendations.
"""

import json
import math
from datetime import UTC, datetime
from typing import Any

from ..tag_synonyms import canonical_tag
from ..tags import is_feature_flag
from . import get_db

# Explicit ratings dominate; a playtime-derived pseudo-rating is a weak
# implicit signal (the user never said they liked it, they just played it).
SOURCE_WEIGHTS = {"backloggd": 1.0, "manual": 1.0, "steam_review": 0.5}
PLAYTIME_SIGNAL_WEIGHT = 0.3
# Below this total playtime an unrated game carries no taste signal at all —
# 2h is roughly "bounced off it", not "chose to keep playing".
MIN_PLAYTIME_SIGNAL_MINUTES = 120
# Pseudo-ratings cap below a true 10 so an explicit love always outranks
# inferred enthusiasm.
PLAYTIME_SCORE_CAP = 9.5

# ── Bayesian shrinkage ───────────────────────────────────────────────────────
# The prior weight k (in pseudo-observations at the user's own mean) is
# ESTIMATED from the data each recompute rather than hand-picked — see
# estimate_shrinkage_weight. A hardcoded k=2 systematically rewarded rare tags:
# a tag on one 10/10 game kept its full deviation from the mean while a tag on
# fifty games averaged across the whole rating range, so affinity was inversely
# correlated with evidence.
#
# Used only when the estimate can't be made (cold start): moderate, so a
# two-rating library doesn't hand a maximal affinity to every tag it saw.
DEFAULT_SHRINKAGE_WEIGHT = 10.0
# Guard rails on the estimate, not the operating range — a degenerate variance
# ratio (near-zero between-tag variance on a handful of ratings) must not
# produce a k that either zeroes every score or disables shrinkage entirely.
MIN_SHRINKAGE_WEIGHT = 1.0
MAX_SHRINKAGE_WEIGHT = 500.0
# Below this many distinct tags the variance decomposition has no between-tag
# signal to measure and the estimate is noise; fall back to the default.
MIN_TAGS_FOR_ESTIMATE = 30

# Bumped whenever the stored affinity_score changes MEANING (formula or scale),
# so init_db can rebuild rows left on the previous scale instead of serving
# numbers that silently mean something else. v1 = Σw(s−μ)/(Σw+2).
AFFINITY_FORMULA_VERSION = 2
# meta key holding the estimated scale: k, its variance components, and the
# "strong" cut consumers threshold against (acceptance: k is auditable).
AFFINITY_SCALE_META_KEY = "tag_affinity_scale"

# "Strong" is defined by RANK, not by an absolute number: the shrunk affinity
# scale compresses as k grows, so any hardcoded threshold silently re-tunes
# itself out of calibration. A tag is strong when it is among the user's top
# STRONG_AFFINITY_RANK tags carrying at least STRONG_AFFINITY_SUPPORT games.
STRONG_AFFINITY_RANK = 10
STRONG_AFFINITY_SUPPORT = 3


def playtime_pseudo_score(minutes: float) -> float:
    """Map hours played to a 1-10-scale pseudo-rating (log scale: 2h≈5.6, 10h≈7, 100h≈9)."""
    hours = minutes / 60.0
    return min(PLAYTIME_SCORE_CAP, 5.0 + 2.0 * math.log10(hours))


def estimate_shrinkage_weight(tag_data: dict[str, dict]) -> dict[str, Any]:
    """Estimate the shrinkage prior weight k from the signal set itself.

    Model: a signal's score for tag i is μ + a_i + e, with tag effects
    a_i ~ (0, σ²_between) and per-game noise e ~ (0, σ²_within). The posterior
    mean of a_i shrinks the observed deviation by W_i / (W_i + k) with

        k = σ²_within / σ²_between

    — the classic variance ratio, so k is a property of the library rather than
    a tuned constant. Both components come from the weighted unbalanced one-way
    ANOVA decomposition over (tag, signal) observations, where a "count" is a
    sum of signal weights (a Steam review at 0.5 really is half an observation):

        σ²_within  = SS_within / (W − Σ_i Σ_j w_ij² / W_i)
        σ²_between = (SS_between / (m − 1) − σ²_within) / W₀
        W₀         = (W − Σ_i W_i² / W) / (m − 1)

    Note that σ²_within here is the variance of a game's score around its tag's
    mean, which for a marginal per-tag estimate legitimately includes every
    OTHER tag's contribution to that game — a game carries ~15 tags, so the
    noise a single tag is estimated against is large and k comes out large.
    That is the finding, not a bug in the estimator: individually, tag means
    are weak evidence, and only well-supported tags earn much of their raw
    deviation.

    Returns the estimate plus its inputs for the meta record; falls back to
    DEFAULT_SHRINKAGE_WEIGHT (with `reason` set) when the decomposition is
    undefined — too few tags, no residual degrees of freedom, or a between-tag
    mean square that does not exceed the within-tag one (no measurable
    between-tag signal at all).
    """
    tags = [data for data in tag_data.values() if data["weight_sum"] > 0]
    n_tags = len(tags)
    total_weight = sum(data["weight_sum"] for data in tags)

    estimate: dict[str, Any] = {
        "shrinkage_weight": DEFAULT_SHRINKAGE_WEIGHT,
        "sigma2_within": None,
        "sigma2_between": None,
        "n_tags": n_tags,
        "total_signal_weight": round(total_weight, 3),
        "reason": None,
    }
    if n_tags < MIN_TAGS_FOR_ESTIMATE or total_weight <= 0:
        estimate["reason"] = "insufficient_data"
        return estimate

    # Grand mean over the tag-exploded observations — NOT the per-signal mean
    # used to centre affinity. A game with 30 tags contributes 30 observations
    # here, and the decomposition below only balances against its own centre.
    grand = sum(data["weighted_sum"] for data in tags) / total_weight

    ss_within = 0.0
    ss_between = 0.0
    # Weighted analogue of (N − m): each tag's own mean absorbs one observation
    # worth of weight, which for unequal weights is Σw²/Σw, not 1.
    within_df = total_weight
    sum_weight_sq = 0.0
    for data in tags:
        weight_sum = data["weight_sum"]
        tag_mean = data["weighted_sum"] / weight_sum
        # Σw(s − m_i)² expanded, so no second pass over the observations.
        ss_within += max(0.0, data["square_sum"] - weight_sum * tag_mean * tag_mean)
        ss_between += weight_sum * (tag_mean - grand) ** 2
        within_df -= data["weight_square_sum"] / weight_sum
        sum_weight_sq += weight_sum * weight_sum

    between_df = n_tags - 1
    w0 = (total_weight - sum_weight_sq / total_weight) / between_df
    if within_df <= 0 or w0 <= 0:
        estimate["reason"] = "no_residual_degrees_of_freedom"
        return estimate

    sigma2_within = ss_within / within_df
    sigma2_between = (ss_between / between_df - sigma2_within) / w0
    estimate["sigma2_within"] = round(sigma2_within, 6)
    estimate["sigma2_between"] = round(sigma2_between, 6)

    # The two degenerate ends point in OPPOSITE directions, so they cannot
    # share a branch. Between-tag first: when neither component is measurable
    # there is no information at all, and "trust nothing" is the safe read.
    if sigma2_between <= 0:
        # Tag means are indistinguishable from sampling noise. Shrink as hard
        # as the guard rail allows rather than pretending k is infinite.
        estimate["shrinkage_weight"] = MAX_SHRINKAGE_WEIGHT
        estimate["reason"] = "no_measurable_between_tag_variance"
        return estimate
    if sigma2_within <= 0:
        # Every tag's own observations agree exactly, so each tag mean is
        # measured without noise: k = 0/σ²_between = 0, i.e. nothing to shrink.
        # Shrinking hardest here would erase the strongest evidence the
        # estimator can ever see.
        estimate["shrinkage_weight"] = MIN_SHRINKAGE_WEIGHT
        estimate["reason"] = "no_within_tag_variance"
        return estimate

    raw = sigma2_within / sigma2_between
    clamped = min(MAX_SHRINKAGE_WEIGHT, max(MIN_SHRINKAGE_WEIGHT, raw))
    estimate["shrinkage_weight"] = round(clamped, 3)
    if clamped != raw:
        estimate["reason"] = "clamped"
        estimate["raw_shrinkage_weight"] = round(raw, 3)
    return estimate


async def recompute_tag_affinity() -> int:
    """
    Recompute tag_affinity from all rated games plus playtime-implied signals.

    Per tag: affinity_score = Σ w·(score − μ) / (Σ w + k)

    which is the posterior-mean deviation under a prior of k pseudo-obser-
    vations sitting at μ, the user's global weighted mean score — identically
    (Σw·score + k·μ)/(Σw + k) − μ. Affinity is therefore *signed*: tags on
    games scored above the user's own average are positive, tags on
    below-average games are negative, and a tag whose games sit at the user's
    mean (e.g. a ubiquitous tag like "action") lands near zero instead of
    inheriting a big positive score from rating inflation.

    k is ESTIMATED from the data (estimate_shrinkage_weight) and recorded in
    meta under AFFINITY_SCALE_META_KEY, so a tag with one observation is pulled
    almost entirely to the prior and contributes ≈0 while a well-supported tag
    keeps its signal. Because k is large on a real library, the resulting scale
    is compressed — read affinities relative to each other (or to
    strong_affinity_cut()), never against an absolute constant, and never damp
    them a second time by game_count.

    Signals: explicit ratings (Backloggd/manual weight 1.0, Steam 0.5) plus a
    low-weight pseudo-rating for owned games with ≥2h playtime and no explicit
    rating — choosing to keep playing something is taste data too.

    avg_score stays the plain (uncentered) weighted average for display.
    Returns number of tags updated.
    """
    async with get_db() as db:
        # Explicit ratings include nested rows (DLC, expansions, etc.) — the user's
        # direct 10/10 on an expansion is taste data even if it's not primary.
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
            if is_feature_flag(tag):
                continue
            # Key on the canonical form so synonym variants accumulate together and
            # match the discover/library lower(value) joins against canonical tags.
            tag_key = canonical_tag(tag)
            if tag_key not in tag_data:
                tag_data[tag_key] = {
                    "weighted_sum": 0.0,
                    "centered_sum": 0.0,
                    "square_sum": 0.0,
                    "weight_sum": 0.0,
                    "weight_square_sum": 0.0,
                    "game_ids": set(),
                }
            tag_data[tag_key]["weighted_sum"] += score * weight
            tag_data[tag_key]["centered_sum"] += (score - global_mean) * weight
            # Σw·s² and Σw² feed the variance decomposition below; accumulating
            # them here keeps the estimate a single pass over the signals.
            tag_data[tag_key]["square_sum"] += score * score * weight
            tag_data[tag_key]["weight_sum"] += weight
            tag_data[tag_key]["weight_square_sum"] += weight * weight
            tag_data[tag_key]["game_ids"].add(game_id)

    scale = estimate_shrinkage_weight(tag_data)
    shrinkage_weight = scale["shrinkage_weight"]
    now = datetime.now(UTC).isoformat()

    rows: list[tuple[str, float, float, int]] = []
    for tag, data in tag_data.items():
        if data["weight_sum"] == 0:
            continue
        rows.append(
            (
                tag,
                data["centered_sum"] / (data["weight_sum"] + shrinkage_weight),
                data["weighted_sum"] / data["weight_sum"],
                len(data["game_ids"]),
            )
        )

    scale.update(
        {
            "formula_version": AFFINITY_FORMULA_VERSION,
            "global_mean": round(global_mean, 4),
            "computed_at": now,
        }
    )

    async with get_db() as db:
        await db.execute("DELETE FROM tag_affinity")
        for tag, affinity_score, avg_score, game_count in rows:
            await db.execute(
                """INSERT INTO tag_affinity (tag, affinity_score, avg_score, game_count, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (tag, affinity_score, avg_score, game_count, now),
            )
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (AFFINITY_SCALE_META_KEY, json.dumps(scale)),
        )
        await db.commit()

    return len(tag_data)


async def get_affinity_scale() -> dict[str, Any]:
    """The recorded shrinkage estimate ({} when affinity was never computed).

    Read this instead of assuming a scale: `shrinkage_weight` and
    `strong_affinity` both move with the library.
    """
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT value FROM meta WHERE key = ?", (AFFINITY_SCALE_META_KEY,)
        )
    if not row or not row["value"]:
        return {}
    try:
        scale = json.loads(row["value"])
    except (ValueError, TypeError):
        return {}
    return scale if isinstance(scale, dict) else {}


async def affinity_scale_is_current() -> bool:
    """Whether stored affinity_score values were computed by THIS formula.

    False after a formula/scale change, which is what makes init_db rebuild the
    table instead of serving numbers whose meaning silently moved.
    """
    scale = await get_affinity_scale()
    return scale.get("formula_version") == AFFINITY_FORMULA_VERSION


async def strong_affinity_cut() -> float | None:
    """Affinity of the STRONG_AFFINITY_RANK-th best supported tag, or None.

    The bar for "this tag is a strong signal for him". Rank-based so it tracks
    the scale automatically — the shrunk affinity scale compresses as the
    estimated prior weight k grows, so any constant threshold falls out of
    calibration on its own. Read from the live table rather than snapshotted
    into the scale record: it is a pure function of the current rows, and a
    stored copy would go stale the moment anything touched them.

    None when the profile is too thin to rank that far, or when the cut would
    not be positive — a cold-start profile has no strong tags, and returning
    the weakest of four would make "strong" mean nothing.
    """
    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT affinity_score FROM tag_affinity
               WHERE game_count >= ?
               ORDER BY affinity_score DESC
               LIMIT 1 OFFSET ?""",
            (STRONG_AFFINITY_SUPPORT, STRONG_AFFINITY_RANK - 1),
        )
    if not row or row["affinity_score"] is None or row["affinity_score"] <= 0:
        return None
    return round(row["affinity_score"], 6)
