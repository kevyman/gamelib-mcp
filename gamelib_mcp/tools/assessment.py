"""get_assessment_context: one pure-DB gathering call for game-quality assessment.

Absorbs the mechanical layer of the client-side game-quality skill (ADR 0006,
decision 3): the sample-adjusted craft-score formula (formerly
``skills/game-quality/scripts/craft_score.py``), the tag-affinity fit check
(``fit_check.py``), anchor-candidate lookup, and the last-30-day play pace —
one call instead of four tool calls plus two local scripts. Judgment (anchor
reasoning, genre calibration, context gates, the verdict) deliberately stays
client-side in the skill.

Everything here is a local DB read — no provider HTTP ever. Steam review
COUNTS are not stored server-side (only the 1–9 review-score enum and its
description survive enrichment; see data/steam_store.py), so the full craft
formula runs only on caller-supplied numbers; the server cache is surfaced
honestly with a ``limitations`` note instead of fabricated counts.
"""

import math
import re
from typing import Any

from fastmcp.exceptions import ToolError

from ..data.db import fts_ready, get_db, get_game_by_appid
from ..data.tag_synonyms import canonical_tag
from ..utils import _parse_json
from .common import PLAY_STATE_SQL, PLAYTIME_SUM_SQL
from .detail import get_game_detail
from .history import get_play_history
from .ratings import get_taste_profile
from .search import NORMALIZED_NAME_SQL, build_name_match, fuzzy_fallback_game_ids

# ── Craft score (ported verbatim from craft_score.py) ─────────────────────────

_CRAFT_BANDS = [
    (0.92, "elite", "Elite — top tier of all of Steam"),
    (0.85, "excellent", "Excellent"),
    (0.78, "very_good", "Very good"),
    (0.70, "divisive", "Good but divisive — read why the negatives exist"),
    (0.00, "caution", "Caution — negatives are usually structural, not taste"),
]

_EA_ORDER = ["caution", "divisive", "very_good", "excellent", "elite"]

# Below this all-time review count the adjusted score is not meaningful.
_MIN_MEANINGFUL_REVIEWS = 50


def _norm_pct(value: float) -> float:
    """88 → 0.88; 0.88 stays 0.88 (same rule as the retired script)."""
    return value / 100.0 if value > 1 else value


def _adjust(p: float, n: int) -> float:
    """SteamDB sample adjustment: p − (p − 0.5) · 2^(−log₁₀(n + 1))."""
    return p - (p - 0.5) * 2 ** (-math.log10(n + 1))


def _band_for(score: float) -> tuple[str, str]:
    for threshold, key, label in _CRAFT_BANDS:
        if score >= threshold:
            return key, label
    return _CRAFT_BANDS[-1][1], _CRAFT_BANDS[-1][2]


def compute_craft_score(
    positive_pct: float,
    total_reviews: int,
    recent_positive_pct: float | None = None,
    recent_total_reviews: int | None = None,
    early_access: bool = False,
) -> dict[str, Any]:
    """Sample-adjusted craft score — same outputs the bundled script printed."""
    p = _norm_pct(positive_pct)
    out: dict[str, Any] = {
        "raw_positive_pct": round(p * 100, 1),
        "total_reviews": total_reviews,
        "adjusted": round(_adjust(p, total_reviews), 4),
    }

    if total_reviews < _MIN_MEANINGFUL_REVIEWS:
        out["insufficient_data"] = True
        out["note"] = (
            "Fewer than 50 reviews — adjusted score is not meaningful. "
            "Lean entirely on Fit + demo."
        )

    key, label = _band_for(out["adjusted"])
    if early_access:
        key = _EA_ORDER[max(0, _EA_ORDER.index(key) - 1)]
        label = next(lbl for _, k, lbl in _CRAFT_BANDS if k == key)
        out["early_access_discount_applied"] = True
    out["band"] = key
    out["band_label"] = label

    if recent_positive_pct is not None and recent_total_reviews:
        rp = _norm_pct(recent_positive_pct)
        out["recent"] = {
            "raw_positive_pct": round(rp * 100, 1),
            "total_reviews": recent_total_reviews,
            "adjusted": round(_adjust(rp, recent_total_reviews), 4),
        }
        delta_pp = (rp - p) * 100
        out["recent"]["delta_pp_vs_alltime"] = round(delta_pp, 1)
        if delta_pp >= 5:
            out["trajectory"] = (
                "improving — patches/content landed; weight recent higher"
            )
        elif delta_pp <= -7:
            out["trajectory"] = (
                "REGRESSING — search for the cause (bad update, monetization, "
                "review bomb) before trusting the all-time number"
            )
        else:
            out["trajectory"] = "stable"

    out["formatted_line"] = (
        f"Craft: {out['adjusted'] * 100:.1f}% adjusted "
        f"({out['raw_positive_pct']:.0f}% of {total_reviews:,} reviews"
        + (f", recent {out['trajectory'].split(' ')[0]}" if "trajectory" in out else "")
        + f") — {label}"
    )
    return out


# ── Fit check (ported from fit_check.py, rescaled to the server's affinity) ──

# fit_check.py called a tag "strong" at affinity >= 20 on the legacy MySteam
# profile scale. The server's affinity_score is mean-centered and shrunk
# (Σw·(score − μ) / (Σw + 2), data/db/affinity.py), realistically spanning
# about ±3 — 1.0 marks a tag consistently rated well above the user's own
# mean across several games, the same rung of the ladder the old 20 marked.
_STRONG_AFFINITY = 1.0
# Steam orders tags by relevance: the first 4 describe the core loop.
_CORE_TAG_COUNT = 4
_CORE_GAP_MISSES = 3
_CALL_LADDER = ["probable miss", "coin flip", "probable fit", "strong fit"]

_NON_ALNUM = re.compile(r"[^a-z0-9]")

# Candidate tag list bound: Steam shows at most ~20; anything past that is
# caller error, not signal.
_MAX_CANDIDATE_TAGS = 40

FIT_REMINDER = (
    "Suggestion only. Check the anchors block next: rated/played library "
    "games sharing the core tags. Anchor reactions and sequel history "
    "override this call. Unmatched tags describing the CORE loop (e.g. "
    "'survival', 'crafting') are themselves evidence of a gap in his taste "
    "data."
)


def _fit_key(tag: str) -> str:
    """Collapse a tag so 'single-player'/'Singleplayer'/'Single Player' collide.

    Runs through canonical_tag first so the curated synonym map (souls-like /
    soulslike / soulsborne) applies before the aggressive alnum collapse.
    """
    return _NON_ALNUM.sub("", canonical_tag(tag))


def _dedupe_candidate_tags(tags: list[str]) -> list[tuple[str, str]]:
    """(display, fit_key) pairs, order-preserving, deduped on the fit key."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tag in tags:
        tag = tag.strip()
        key = _fit_key(tag) if tag else ""
        if key and key not in seen:
            seen.add(key)
            pairs.append((tag, key))
    return pairs


def compute_fit(candidate_tags: list[str], profile: dict) -> dict[str, Any]:
    """Cross candidate tags against a taste profile (get_taste_profile shape).

    Same thresholds the retired fit_check.py applied, with "strong" rescaled
    to the server's affinity scale (_STRONG_AFFINITY above).
    """
    cand = {key: display for display, key in _dedupe_candidate_tags(candidate_tags)}

    top = {_fit_key(t["tag"]): t for t in profile.get("top_tags", [])}
    bottom = {_fit_key(t["tag"]): t for t in profile.get("bottom_tags", [])}
    # Cold-start profiles (fewer eligible affinity rows than the top-20 +
    # bottom-10 window) return overlapping lists; membership alone can't
    # classify those tags, so the affinity's sign decides which side keeps
    # them — a negative tag must never pass as a top match.
    for key in top.keys() & bottom.keys():
        if (top[key].get("affinity_score") or 0) < 0:
            del top[key]
        else:
            del bottom[key]

    matched_top: list[dict[str, Any]] = []
    matched_bottom: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for key, original in cand.items():
        if key in top:
            entry = top[key]
            matched_top.append(
                {
                    "tag": original,
                    "affinity": entry.get("affinity_score"),
                    "avg_score": entry.get("avg_score"),
                    "game_count": entry.get("game_count"),
                }
            )
        elif key in bottom:
            entry = bottom[key]
            matched_bottom.append(
                {
                    "tag": original,
                    "affinity": entry.get("affinity_score"),
                    "avg_score": entry.get("avg_score"),
                    "game_count": entry.get("game_count"),
                }
            )
        else:
            unmatched.append(original)

    matched_top.sort(key=lambda m: -(m["affinity"] or 0))
    strong_top = [m for m in matched_top if (m["affinity"] or 0) >= _STRONG_AFFINITY]
    coverage = len(matched_top) / max(1, len(cand))

    core_keys = list(cand.keys())[:_CORE_TAG_COUNT]
    core_misses = sum(1 for key in core_keys if key not in top)

    if matched_bottom and not strong_top:
        call = "probable miss"
    elif len(strong_top) >= 3 and coverage >= 0.4:
        call = "strong fit"
    elif len(strong_top) >= 2 or coverage >= 0.4:
        call = "probable fit"
    elif matched_top:
        call = "coin flip"
    else:
        call = "probable miss"

    core_gap = core_misses >= _CORE_GAP_MISSES
    if core_gap:
        call = _CALL_LADDER[max(0, _CALL_LADDER.index(call) - 1)]

    return {
        "candidate_tags": len(cand),
        "matched_top_tags": matched_top,
        "matched_bottom_tags": matched_bottom,
        "unmatched_tags": unmatched,
        "top_coverage": round(coverage, 2),
        "core_gap": core_gap,
        "suggested_call": call,
        "reminder": FIT_REMINDER,
    }


# ── Anchors ──────────────────────────────────────────────────────────────────

# Bounded response (CLAUDE.md pattern): the anchors list is capped here, with
# anchor_count the true total and anchors_truncated the flag — registered in
# tests/test_tool_dispatch.py::ResponseSizeGuardTests.
ANCHOR_CAP = 8

# Owned, primary, non-farmed games with tags — the pool anchors come from.
# Deliberately its own rollup (see tools/common.py: the per-module rollup CTEs
# differ and stay separate); this one needs only play-state + playtime.
_ANCHOR_ROLLUP_CTE = f"""
WITH anchor_rollup AS (
    SELECT g.id AS game_id,
           g.name,
           g.tags,
           g.completion_status,
           {PLAYTIME_SUM_SQL} AS total_playtime_minutes,
           {PLAY_STATE_SQL} AS play_state
    FROM games g
    JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
    WHERE g.is_primary_library_item = 1
      AND g.is_farmed = 0
      AND g.tags IS NOT NULL
    GROUP BY g.id
)
"""

# Best explicit rating for a game, preferring full-weight sources (manual /
# backloggd 1.0 over steam_review 0.5 — data/db/affinity.py::SOURCE_WEIGHTS).
_RATING_SOURCE_PRIORITY = {"manual": 0, "backloggd": 1}
_RATING_SOURCE_FALLBACK = 2


async def _load_anchors(
    core_pairs: list[tuple[str, str]],
    exclude_game_id: int | None,
) -> tuple[list[dict[str, Any]], int]:
    """Owned library games sharing the candidate's core (display, fit_key) tags.

    Matching runs on ``_fit_key`` on BOTH sides — the same collapsed key
    ``compute_fit`` matches on — so a caller-supplied "Single Player" still
    anchors against a stored "singleplayer" tag. An exact-string SQL match on
    the canonical form silently missed those transcription variants, making
    fit and anchors contradict each other on the same tags; that is why the
    match runs in Python over the rollup rather than in SQL.
    """
    exclude = exclude_game_id if exclude_game_id is not None else -1

    async with get_db() as db:
        rows = await db.execute_fetchall(
            _ANCHOR_ROLLUP_CTE
            + """
            SELECT ar.game_id,
                   ar.name,
                   ar.tags,
                   ar.completion_status,
                   ar.total_playtime_minutes,
                   ar.play_state
            FROM anchor_rollup ar
            WHERE ar.game_id != ?
            """,
            (exclude,),
        )
        rating_rows = await db.execute_fetchall(
            """SELECT game_id, source, normalized_score, id FROM ratings
               WHERE normalized_score IS NOT NULL"""
        )

    # Best explicit rating per game (source priority above, then lowest id).
    best_rating: dict[int, Any] = {}
    for rating in rating_rows:
        rank = (
            _RATING_SOURCE_PRIORITY.get(rating["source"], _RATING_SOURCE_FALLBACK),
            rating["id"],
        )
        held = best_rating.get(rating["game_id"])
        if held is None or rank < held[0]:
            best_rating[rating["game_id"]] = (rank, rating)

    matched_rows: list[tuple[Any, list[str]]] = []
    for row in rows:
        anchor_keys = {_fit_key(str(tag)) for tag in _parse_json(row["tags"]) or []}
        matched = [display for display, key in core_pairs if key in anchor_keys]
        if matched:
            matched_rows.append((row, matched))

    matched_rows.sort(
        key=lambda item: (
            -len(item[1]),
            item[0]["game_id"] not in best_rating,
            -(item[0]["total_playtime_minutes"] or 0),
            item[0]["name"],
        )
    )

    anchors: list[dict[str, Any]] = []
    for row, matched in matched_rows[:ANCHOR_CAP]:
        held = best_rating.get(row["game_id"])
        playtime_minutes = row["total_playtime_minutes"]
        anchors.append(
            {
                "game_id": row["game_id"],
                "name": row["name"],
                "matched_core_tags": matched,
                "rating": (
                    {
                        "source": held[1]["source"],
                        "score": held[1]["normalized_score"],
                    }
                    if held is not None
                    else None
                ),
                "playtime_hours": (
                    round(playtime_minutes / 60, 1)
                    if playtime_minutes is not None
                    else None
                ),
                "completion_status": row["completion_status"],
                "play_state": row["play_state"],
            }
        )
    return anchors, len(matched_rows)


# ── Input validation (every mode, before any work) ───────────────────────────


def _validate_inputs(
    name: str | None,
    appid: int | None,
    game_id: int | None,
    tags: list[str] | None,
    steam_positive_pct: float | None,
    steam_total_reviews: int | None,
    steam_recent_positive_pct: float | None,
    steam_recent_total_reviews: int | None,
) -> list[tuple[str, str]] | None:
    """Validate everything up front; returns the deduped candidate tag pairs."""
    if name is None and appid is None and game_id is None and tags is None:
        raise ToolError(
            "Provide a game identity (name, game_id, or appid) and/or candidate "
            "tags — there is nothing to assess otherwise."
        )

    cand_pairs: list[tuple[str, str]] | None = None
    if tags is not None:
        if len(tags) > _MAX_CANDIDATE_TAGS:
            raise ToolError(
                f"tags accepts at most {_MAX_CANDIDATE_TAGS} entries "
                f"(got {len(tags)}); pass the candidate's Steam tags in display order."
            )
        cand_pairs = _dedupe_candidate_tags(tags)
        if not cand_pairs:
            raise ToolError("tags must contain at least one non-empty tag")

    if (steam_positive_pct is None) != (steam_total_reviews is None):
        raise ToolError(
            "steam_positive_pct and steam_total_reviews go together — the "
            "sample adjustment needs both the percentage and the count."
        )
    if (steam_recent_positive_pct is None) != (steam_recent_total_reviews is None):
        raise ToolError(
            "steam_recent_positive_pct and steam_recent_total_reviews go together."
        )
    if steam_recent_positive_pct is not None and steam_positive_pct is None:
        raise ToolError(
            "Recent review numbers need the all-time numbers too — the "
            "trajectory is recent vs. all-time."
        )
    for label, pct in (
        ("steam_positive_pct", steam_positive_pct),
        ("steam_recent_positive_pct", steam_recent_positive_pct),
    ):
        if pct is not None and not 0 <= pct <= 100:
            raise ToolError(f"{label} must be between 0 and 100 (or 0.0–1.0)")
    if steam_total_reviews is not None and steam_total_reviews < 0:
        raise ToolError("steam_total_reviews must be >= 0")
    if steam_recent_total_reviews is not None and steam_recent_total_reviews < 1:
        raise ToolError(
            "steam_recent_total_reviews must be >= 1 — with zero recent "
            "reviews there is no recent percentage to pass; omit both."
        )
    return cand_pairs


# ── Resolution + assembly ────────────────────────────────────────────────────


async def _resolve_game_id(
    name: str | None, appid: int | None, game_id: int | None
) -> int | None:
    """Resolve identity exactly like get_game_detail (id > appid > name+fuzzy)."""
    if game_id is not None:
        async with get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE id = ?", (game_id,)
            )
        return row["id"] if row is not None else None
    if appid is not None:
        row = await get_game_by_appid(appid)
        return row["id"] if row is not None else None
    assert name is not None
    match = build_name_match(name, column=NORMALIZED_NAME_SQL, use_fts=fts_ready())
    async with get_db() as db:
        row = await db.execute_fetchone(
            f"""SELECT g.id, {match.rank_sql} AS match_rank
                FROM games g
                WHERE {match.where_sql}
                ORDER BY match_rank ASC, length(g.name) ASC, g.id ASC
                LIMIT 1""",
            (*match.rank_params, *match.where_params),
        )
    if row is not None:
        return row["id"]
    fuzzy_ids = await fuzzy_fallback_game_ids(name)
    return fuzzy_ids[0] if fuzzy_ids else None


def _compact_game_block(detail: dict[str, Any]) -> dict[str, Any]:
    """Ownership/context subset of get_game_detail's result — not a re-fetch."""
    return {
        "game_id": detail["game_id"],
        "name": detail["name"],
        "owned": detail["owned"],
        "wishlisted": detail["wishlisted"],
        "completion_status": detail["completion_status"],
        "play_state": detail["play_state"],
        "playtime_hours": detail["playtime_hours"],
        "last_played_date": detail["last_played_date"],
        "hltb_main": detail["hltb_main"],
        "hltb_extra": detail["hltb_extra"],
        "content_type": detail["content_type"],
        "parent_game_id": detail["parent_game_id"],
        "my_rating": detail.get("my_rating"),
        "owned_platforms": [
            {
                "platform": p["platform"],
                "playtime_hours": p["playtime_hours"],
                "acquired_at": p["acquired_at"],
                "price_paid": p["price_paid"],
                "price_currency": p["price_currency"],
                "purchase_source": p["purchase_source"],
                "bundle_name": p["bundle_name"],
            }
            for p in detail["platforms"]
            if p["owned"]
        ],
    }


async def _server_cached_craft(resolved_game_id: int) -> dict[str, Any] | None:
    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT spd.steam_review_score, spd.steam_review_desc,
                      spd.store_cached_at
               FROM game_platforms gp
               JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
               WHERE gp.game_id = ? AND gp.platform = 'steam'
               ORDER BY gp.id
               LIMIT 1""",
            (resolved_game_id,),
        )
    if row is None or (
        row["steam_review_score"] is None and row["steam_review_desc"] is None
    ):
        return None
    return {
        "source": "server_cache",
        "steam_review_score": row["steam_review_score"],
        "steam_review_desc": row["steam_review_desc"],
        "as_of": row["store_cached_at"],
        "limitations": (
            "The server caches only Steam's 1-9 review-score enum and its "
            "description — no review counts and no recent window — so the "
            "sample-adjusted craft score cannot be computed from it. For the "
            "full formula, web-search the all-time/recent positive % and "
            "counts (SteamDB or the store page) and pass them as "
            "steam_positive_pct/steam_total_reviews."
        ),
    }


async def get_assessment_context(
    name: str | None = None,
    appid: int | None = None,
    game_id: int | None = None,
    tags: list[str] | None = None,
    steam_positive_pct: float | None = None,
    steam_total_reviews: int | None = None,
    steam_recent_positive_pct: float | None = None,
    steam_recent_total_reviews: int | None = None,
    early_access: bool = False,
) -> dict[str, Any]:
    """Gather craft/fit/anchors/pace/ownership context for one candidate game.

    Pure DB read. See main.py's tool docstring for the wire contract; block
    presence rules:
    - craft: caller numbers when given (full formula), else the cached Steam
      review enum for a resolved game (source="server_cache"), else absent.
    - fit + anchors (+ anchor_count/anchors_truncated): whenever candidate
      tags exist — caller-supplied, else the resolved game's stored tags.
    - game + game_resolution: when identity was given (game only on resolve).
    - pace: always.
    """
    cand_pairs = _validate_inputs(
        name,
        appid,
        game_id,
        tags,
        steam_positive_pct,
        steam_total_reviews,
        steam_recent_positive_pct,
        steam_recent_total_reviews,
    )

    result: dict[str, Any] = {}
    tags_source: str | None = "caller" if cand_pairs is not None else None

    resolved_id: int | None = None
    detail: dict[str, Any] | None = None
    if name is not None or appid is not None or game_id is not None:
        resolved_id = await _resolve_game_id(name, appid, game_id)
        if resolved_id is None:
            result["game_resolution"] = "not_found"
        else:
            result["game_resolution"] = "resolved"
            # enrich=False: serves cached enrichment only — never provider HTTP.
            detail = await get_game_detail(game_id=resolved_id, enrich=False)
            result["game"] = _compact_game_block(detail)
            if cand_pairs is None and detail.get("tags"):
                cand_pairs = _dedupe_candidate_tags(detail["tags"])
                tags_source = "library" if cand_pairs else None

    # Craft: caller numbers outrank the server cache (which has no counts).
    if steam_positive_pct is not None and steam_total_reviews is not None:
        craft = compute_craft_score(
            steam_positive_pct,
            steam_total_reviews,
            steam_recent_positive_pct,
            steam_recent_total_reviews,
            early_access,
        )
        craft["source"] = "caller"
        result["craft"] = craft
    elif resolved_id is not None:
        cached = await _server_cached_craft(resolved_id)
        if cached is not None:
            result["craft"] = cached

    if cand_pairs:
        display_tags = [display for display, _ in cand_pairs]
        profile = await get_taste_profile()
        fit = compute_fit(display_tags, profile)
        fit["tags_source"] = tags_source
        if not profile["top_tags"] and not profile["bottom_tags"]:
            fit["suggested_call"] = None
            fit["note"] = (
                "No taste-profile data yet — run sync(targets=[\"ratings\"]) "
                "or rate_game first, then retry."
            )
        # Raw per-tag affinity rows, including tags outside the profile's
        # top/bottom display lists (the fit call above deliberately matches
        # only those lists, like the script it ports).
        async with get_db() as db:
            affinity_rows = await db.execute_fetchall(
                "SELECT tag, affinity_score, avg_score, game_count FROM tag_affinity"
            )
        by_key: dict[str, Any] = {}
        for row in affinity_rows:
            key = _fit_key(row["tag"])
            previous = by_key.get(key)
            if previous is None or row["game_count"] > previous["game_count"]:
                by_key[key] = row
        fit["tag_affinities"] = [
            (
                {
                    "tag": display,
                    "affinity_score": round(match["affinity_score"], 3),
                    "avg_score": round(match["avg_score"], 2),
                    "game_count": match["game_count"],
                }
                if (match := by_key.get(key)) is not None
                else {
                    "tag": display,
                    "affinity_score": None,
                    "avg_score": None,
                    "game_count": 0,
                }
            )
            for display, key in cand_pairs
        ]
        result["fit"] = fit

        # cand_pairs already carry the (display, fit_key) shape anchors match on.
        anchors, anchor_count = await _load_anchors(
            cand_pairs[:_CORE_TAG_COUNT], resolved_id
        )
        result["anchors"] = anchors
        result["anchor_count"] = anchor_count
        result["anchors_truncated"] = anchor_count > len(anchors)

    history = await get_play_history(days=30, limit=1)
    result["pace"] = {
        "window": history["window"],
        "total_minutes": history["total_minutes"],
        "total_hours": history["total_hours"],
        "by_platform": history["by_platform"],
        "most_played": history["games"][0] if history["games"] else None,
        "switch2_unmatched_minutes": history["switch2_unmatched_minutes"],
    }

    return result
