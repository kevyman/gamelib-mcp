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

import asyncio
import json
import logging
import math
import re
from datetime import UTC, datetime
from functools import partial
from typing import Any

from fastmcp.exceptions import ToolError

from ..data.db import (
    exact_name_steam_conflict,
    fts_ready,
    get_assessed_game_id_by_appid,
    get_db,
    get_game_by_appid,
    load_recent_assessments,
    titles_conflict_on_identity,
    upsert_game,
)
from ..data.media import get_game_media
from ..data.tag_synonyms import canonical_tag
from ..data.title_normalization import normalize_search_text
from ..utils import _parse_json
from .batch import apply_batch_item, check_batch_items, count_status
from .common import (
    OWNED_SQL,
    PLAY_STATE_SQL,
    PLAYTIME_SUM_SQL,
    STEAM_APPID_SQL,
    clamp_limit,
    cover_url,
)
from .detail import get_game_detail
from .game_media import media_context
from .history import get_play_history
from .ratings import get_taste_profile
from .search import NORMALIZED_NAME_SQL, build_name_match, fuzzy_fallback_game_ids

logger = logging.getLogger(__name__)

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
# profile scale, and this module long carried 1.0 as its equivalent. There is
# no such constant any more: affinity_score is a shrunk posterior deviation
# whose spread depends on the prior weight k estimated per recompute
# (data/db/affinity.py), so a hardcoded threshold silently falls out of
# calibration whenever the library grows. "Strong" now comes from the profile's
# own recorded scale (`shrinkage.strong_affinity` — the affinity of his 10th
# best-supported tag); a profile too thin to rank ten has NO strong tags, and
# the fit call degrades to coverage rather than inventing a bar.
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

    Same thresholds the retired fit_check.py applied, with "strong" read from
    the profile's recorded affinity scale (see the note above).
    """
    strong_cut = (profile.get("shrinkage") or {}).get("strong_affinity")
    if not isinstance(strong_cut, (int, float)) or isinstance(strong_cut, bool):
        strong_cut = None
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
    strong_top = (
        []
        if strong_cut is None
        else [m for m in matched_top if (m["affinity"] or 0) >= strong_cut]
    )
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


# Ordinal tokens a near-miss can differ by — the shapes a sequel number takes
# in a title. Roman numerals stop at xx (Final Fantasy's range); "i" is
# deliberately included even though it also spells a word, because a false
# reject only degrades to not_found, which the skill already treats as a
# normal unowned candidate, while a false ACCEPT misfiles a verdict.
_ROMAN_ORDINALS = frozenset(
    (
        "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
        "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx",
    )
)


def _is_ordinal_token(token: str) -> bool:
    """An arabic number, a roman numeral i..xx, or a single character."""
    if token.isdigit():
        return len(token) <= 4
    if token in _ROMAN_ORDINALS:
        return True
    # "Silent Hill f", "Devil May Cry 5" -> a lone character is a series marker.
    return len(token) == 1 and token.isalnum()


def _ordinal_near_miss(query: str, matched_name: str) -> bool:
    """True when the two titles differ by exactly ONE TRAILING ordinal token.

    The sequel-shaped near miss issue #150 was filed for: "Alan Wake 2"
    token-AND-misses the library's "Alan Wake" (the "2" appears nowhere), then
    the fuzzy fallback scores ~90 and hands back the predecessor. Symmetric —
    the query may be either the longer or the shorter title.

    Definition, and its limit: one list must equal the other PLUS one trailing
    ordinal token. Two different ordinals in the same position ("final fantasy
    vii" vs "final fantasy viii") are NOT a near miss by this rule and the
    function returns False — the tiers miss that pair, which is exactly what
    EXPOSES it to the fuzzy fallback (~97 on token-sort), so
    ``_sequel_near_miss`` unions this test with
    ``titles_conflict_on_identity``, whose number-identity comparison catches
    it. What this function alone contributes to the union is the lone
    trailing character with no digit ("Silent Hill f"), which carries no
    number identity for the other test to compare.
    """
    query_tokens = normalize_search_text(query).split()
    name_tokens = normalize_search_text(matched_name).split()
    if not query_tokens or not name_tokens:
        return False
    longer, shorter = (
        (query_tokens, name_tokens)
        if len(query_tokens) > len(name_tokens)
        else (name_tokens, query_tokens)
    )
    if len(longer) != len(shorter) + 1 or longer[:-1] != shorter:
        return False
    return _is_ordinal_token(longer[-1])


def _sequel_near_miss(query: str, matched_name: str) -> bool:
    """A non-exact match that reads as a DIFFERENT entry in the same series.

    Union of two complementary tests. ``titles_conflict_on_identity``
    (data/db/fuzzy.py — the same guard the sync-side fuzzy resolvers use)
    compares number-identity token sets with roman numerals normalized and
    edition decorations stripped, so it rejects both the added-ordinal shape
    ("Alan Wake 2" vs "Alan Wake") and the equal-length differing-ordinal
    shape ("Final Fantasy VIII" vs "Final Fantasy VII" — tiers miss it, fuzzy
    scores ~97). ``_ordinal_near_miss`` adds the one shape identity tokens
    cannot see: a lone trailing character with no digit ("Silent Hill f").
    """
    return _ordinal_near_miss(query, matched_name) or titles_conflict_on_identity(
        query, matched_name
    )


async def _resolve_by_id_or_appid(
    appid: int | None, game_id: int | None
) -> tuple[int | None, str | None]:
    """The identity resolution BOTH assessment tools share: id, then appid.

    Returns (game_id, mode) with mode one of "by_id" / "by_appid" /
    "by_assessed_appid", or (None, None) when the given identity missed. Name
    resolution is deliberately not here: the read path matches names loosely
    (``_resolve_name_for_context``) while the write path is exact-or-mint.
    """
    if game_id is not None:
        async with get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE id = ?", (game_id,)
            )
        return (row["id"], "by_id") if row is not None else (None, None)
    if appid is not None:
        row = await get_game_by_appid(appid)
        if row is not None:
            return row["id"], "by_appid"
        # Identifier rows hang off game_platforms, so a candidate that
        # record_assessment minted (unowned, unwishlisted) is invisible to
        # get_game_by_appid — its appid lives on the assessment row itself,
        # like game_wishlist.store_identifier. Without this fallback a repeat
        # ask by appid reported not_found and re-asked for a name it had
        # already been given.
        assessed_id = await get_assessed_game_id_by_appid(appid)
        if assessed_id is not None:
            return assessed_id, "by_assessed_appid"
    return None, None


async def _resolve_name_for_context(name: str) -> tuple[int | None, str, str | None]:
    """Tiered + fuzzy name resolution — READ PATH ONLY (get_assessment_context).

    Returns (game_id, mode, rejected_near_miss). mode is "exact" (rank 0),
    "partial" (rank 1-3), "fuzzy" (the rapidfuzz fallback), or "none" when
    nothing matched — including when the ordinal guard REJECTED a non-exact
    match, in which case the rejected row's name comes back so the caller can
    surface it ("did you mean…"; pass game_id if it was the intended game).

    record_assessment deliberately does NOT call this: a loose match on a
    write path silently files a verdict onto the wrong game (issue #150),
    which is invisible, whereas a minted phantom row is visible and
    repairable. Reads keep the loose match because a wrong-but-close context
    block is recoverable by the caller reading game.name.
    """
    match = build_name_match(name, column=NORMALIZED_NAME_SQL, use_fts=fts_ready())
    async with get_db() as db:
        row = await db.execute_fetchone(
            f"""SELECT g.id, g.name, {match.rank_sql} AS match_rank
                FROM games g
                WHERE {match.where_sql}
                ORDER BY match_rank ASC, length(g.name) ASC, g.id ASC
                LIMIT 1""",
            (*match.rank_params, *match.where_params),
        )
    if row is not None:
        if row["match_rank"] == 0:
            return row["id"], "exact", None
        if _sequel_near_miss(name, row["name"]):
            return None, "none", row["name"]
        return row["id"], "partial", None

    fuzzy_ids = await fuzzy_fallback_game_ids(name)
    if not fuzzy_ids:
        return None, "none", None
    async with get_db() as db:
        fuzzy_row = await db.execute_fetchone(
            "SELECT id, name FROM games WHERE id = ?", (fuzzy_ids[0],)
        )
    if fuzzy_row is None:
        return None, "none", None
    if _sequel_near_miss(name, fuzzy_row["name"]):
        return None, "none", fuzzy_row["name"]
    return fuzzy_row["id"], "fuzzy", None


_NAME_MODES = ("exact", "partial", "fuzzy", "minted")


def _resolution_query(
    mode: str | None, name: str | None, appid: int | None, game_id: int | None
) -> str | None:
    """The identity string a ``resolution`` block echoes back.

    Whichever input actually did the resolving — not simply the highest-
    precedence one given, since an appid that missed can still fall through to
    the name (record_assessment's mint path). Unresolved identity falls back to
    precedence order.
    """
    if mode == "by_id":
        return str(game_id)
    if mode in ("by_appid", "by_assessed_appid"):
        return str(appid)
    if mode in _NAME_MODES:
        return name
    if game_id is not None:
        return str(game_id)
    if appid is not None:
        return str(appid)
    return name


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


# The pace window both assessment tools read: get_assessment_context's `pace`
# block and the package's recent_weekly_minutes are the same observation, so
# they come from one place rather than two 30-day queries that could drift.
PACE_WINDOW_DAYS = 30


async def _recent_pace() -> dict[str, Any]:
    """Last-30-day play summary — the `pace` block, shared with the package."""
    history = await get_play_history(days=PACE_WINDOW_DAYS, limit=1)
    return {
        "window": history["window"],
        "total_minutes": history["total_minutes"],
        "total_hours": history["total_hours"],
        "by_platform": history["by_platform"],
        "most_played": history["games"][0] if history["games"] else None,
        "switch2_unmatched_minutes": history["switch2_unmatched_minutes"],
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
    - game + game_resolution + resolution: when identity was given (game and
      resolution.matched_name only on resolve; resolution.rejected_near_miss
      only when the ordinal guard turned a sequel-shaped match into
      not_found).
    - past_assessments (+ count/truncated): when identity resolved AND this
      game was assessed before — the repeat-ask signal, capped at
      PAST_ASSESSMENT_CAP.
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
        near_miss: str | None = None
        resolved_id, mode = await _resolve_by_id_or_appid(appid, game_id)
        if resolved_id is None and game_id is None and appid is None:
            assert name is not None
            resolved_id, mode, near_miss = await _resolve_name_for_context(name)
        resolution: dict[str, Any] = {
            "mode": mode or "none",
            "query": _resolution_query(mode, name, appid, game_id),
        }
        if near_miss is not None:
            # The guard rejected a sequel-shaped match: name it, so a caller
            # who DID mean that row can re-ask with game_id instead of taking
            # not_found at face value.
            resolution["rejected_near_miss"] = near_miss
        result["resolution"] = resolution
        if resolved_id is None:
            result["game_resolution"] = "not_found"
        else:
            result["game_resolution"] = "resolved"
            # enrich=False: serves cached enrichment only — never provider HTTP.
            detail = await get_game_detail(game_id=resolved_id, enrich=False)
            result["game"] = _compact_game_block(detail)
            resolution["matched_name"] = detail["name"]
            # Repeat-ask detection, for free, in the skill's Step 0: if this
            # game was assessed before, the prior verdict/date/price is what a
            # new assessment should be argued against — not re-derived blind.
            past, past_total = await load_recent_assessments(
                resolved_id, PAST_ASSESSMENT_CAP
            )
            if past:
                result["past_assessments"] = past
                result["past_assessment_count"] = past_total
                result["past_assessments_truncated"] = past_total > len(past)
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

    result["pace"] = await _recent_pace()

    return result


# ── Recording verdicts (ADR 0006 decision 5) ─────────────────────────────────
#
# HARD CONSTRAINT: nothing below may ever feed tag_affinity or discover_games.
# A recorded verdict is model output; mining it back into ranking would be a
# self-reinforcement loop (the ADR's "Rejected" list). record_assessment
# therefore never calls recompute_tag_affinity — not even in bulk mode, where
# every other write tool defers exactly one recompute to the end of the loop —
# and no scoring query joins game_assessments. That is also why this is a tool
# of its own rather than a mode of rate_game: ratings feed affinity by design,
# so keeping the two write paths separate makes the constraint structural
# instead of a comment. It cannot fold into get_assessment_context either — a
# merged tool inherits the strictest annotation it absorbs (ADR 0004), which
# would advertise the read as a mutation.

ASSESSMENT_VERDICTS = (
    "buy_now",
    "wishlist_for_sale",
    "try_demo",
    "skip",
    "play_what_you_own",
)
ASSESSMENT_TRAJECTORIES = ("improving", "stable", "regressing")
# fit_call reuses compute_fit's own vocabulary (_CALL_LADDER) so a recorded
# call is the same string the context tool suggested.
ASSESSMENT_FIT_CALLS = tuple(_CALL_LADDER)

# Bounded-response caps (root CLAUDE.md): the read blocks below never grow
# with how often a game was re-assessed.
PAST_ASSESSMENT_CAP = 5
ASSESSMENTS_REPORT_DEFAULT_LIMIT = 25
ASSESSMENTS_REPORT_MAX_LIMIT = 200
CALIBRATION_LIST_DEFAULT_LIMIT = 25

# Write-time caps. Lists are REJECTED over their cap (a 30-anchor payload is a
# caller bug worth naming); free text is truncated (a slightly long one-liner
# should not fail the whole record).
ANCHORS_CITED_CAP = 8
FLAGS_CAP = 8
SUMMARY_MAX_CHARS = 300
CONTEXT_MAX_CHARS = 200
FLAG_MAX_CHARS = 120

# Presentation caps (the model-authored half of a verdict). Same stance as
# above: prose is truncated, lists are rejected over their cap.
ELEVATOR_PITCH_MAX_CHARS = 420
# One line of craft context the score chips cannot carry (the critic spread,
# the recurring knock, the review-bomb caveat). Prose, so truncated.
CRAFT_NOTE_MAX_CHARS = 200
FOR_YOU_IF_CAP = 4
FOR_YOU_IF_MAX_CHARS = 200
COMPARISONS_CAP = 6
COMPARISON_NAME_MAX_CHARS = 120
COMPARISON_NOTE_MAX_CHARS = 200
COMPARISON_RELATIONS = (
    "better_version",
    "similar",
    "ancestor",
    "descendant",
    "cheaper_substitute",
)

# why_care: the editorial counterpart to the server-fetched pedigree block —
# the one or two sourceable claims that make a game worth a look at all (who
# made it, what the studio is, what it is following, the moment it belongs
# to). A closed KIND vocabulary for the same reason comparisons has one: each
# kind renders as its own chip, so an unknown kind would silently render as
# nothing. Three lines maximum — this is an eyebrow, not a paragraph.
WHY_CARE_CAP = 3
WHY_CARE_TEXT_MAX_CHARS = 160
WHY_CARE_KINDS = ("people", "studio", "anticipation", "moment")

# "Played" for calibration: two hours is the same bar the taste-profile
# playtime pseudo-rating uses for "he actually engaged with this".
CALIBRATION_PLAYED_MINUTES = 120

# Declared-only provenance caps. These are identifiers, not prose, so an
# over-cap value is REJECTED like an over-cap list rather than truncated: a
# silently shortened model id would group calibration under a name nothing ever
# declared.
SKILL_MAX_CHARS = 64
MODEL_MAX_CHARS = 64
SKILL_VERSION_MAX_CHARS = 32

_ASSESSMENT_COMPONENT_COLUMNS = (
    "summary",
    "craft_adjusted",
    "craft_positive_pct",
    "review_count",
    "recent_trajectory",
    "opencritic_score",
    "fit_call",
    "anchors_cited",
    "flags",
    "price_seen",
    "price_currency",
    "price_platform",
    "target_price",
    "instead_game_id",
    "steam_appid",
    "context",
)

# Methodology provenance (issue #153) — kept apart from the verdict components
# above because it describes HOW the verdict was produced, not what it says.
# Declared-only: the server never stamps its own skill version or a model name,
# so NULL genuinely means "the recording client didn't say".
_ASSESSMENT_PROVENANCE_COLUMNS = ("skill", "skill_version", "model")

# The model-authored presentation PARAMETERS. They are wire fields, not
# columns: they all land in the single `presentation` JSON column, so the two
# lists have to stay separate (item keys and the void-exclusive check read the
# parameter names, the write path reads the column). The column is free-form
# JSON by design, so a new member here needs no migration.
_PRESENTATION_PARAMS = (
    "elevator_pitch",
    "for_you_if",
    "not_for_you_if",
    "comparisons",
    "why_care",
    "craft_note",
)

# Everything a recording write touches, in wire order.
_ASSESSMENT_WRITE_COLUMNS = (
    *_ASSESSMENT_COMPONENT_COLUMNS,
    *_ASSESSMENT_PROVENANCE_COLUMNS,
    "presentation",
)

RECORD_ASSESSMENT_ITEM_KEYS = frozenset(
    {
        "name",
        "appid",
        "game_id",
        "verdict",
        "assessed_at",
        *_ASSESSMENT_COMPONENT_COLUMNS,
        *_ASSESSMENT_PROVENANCE_COLUMNS,
        *_PRESENTATION_PARAMS,
    }
)


def _truncate(value: str, limit: int) -> str:
    text = value.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _normalize_assessed_at(value: str | None) -> str:
    """Caller override → UTC ISO 8601; default now. Date-only means midnight UTC."""
    if value is None:
        return datetime.now(UTC).isoformat()
    try:
        # 3.11's fromisoformat takes the trailing "Z" itself.
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ToolError(
            f"assessed_at must be ISO 8601 (e.g. '2026-08-01' or "
            f"'2026-08-01T14:30:00Z'); got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _normalize_anchors(anchors: list | None) -> str | None:
    if anchors is None:
        return None
    if not isinstance(anchors, list):
        raise ToolError("anchors_cited must be a list of names or {name, game_id} objects")
    if len(anchors) > ANCHORS_CITED_CAP:
        raise ToolError(
            f"anchors_cited accepts at most {ANCHORS_CITED_CAP} entries "
            f"(got {len(anchors)}) — cite the ones the verdict actually rests on"
        )
    normalized: list[dict[str, Any]] = []
    for anchor in anchors:
        if isinstance(anchor, str):
            if anchor.strip():
                normalized.append({"name": _truncate(anchor, FLAG_MAX_CHARS)})
            continue
        if not isinstance(anchor, dict):
            raise ToolError(
                "each anchors_cited entry must be a game name or a "
                "{name, game_id} object"
            )
        unknown = set(anchor) - {"name", "game_id"}
        if unknown:
            raise ToolError(
                f"anchors_cited entries accept only 'name' and 'game_id' "
                f"(got {sorted(unknown)})"
            )
        name = anchor.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolError("each anchors_cited entry needs a non-empty 'name'")
        entry: dict[str, Any] = {"name": _truncate(name, FLAG_MAX_CHARS)}
        anchor_id = anchor.get("game_id")
        if anchor_id is not None:
            if isinstance(anchor_id, bool) or not isinstance(anchor_id, int):
                raise ToolError("anchors_cited game_id must be an integer")
            entry["game_id"] = anchor_id
        normalized.append(entry)
    return json.dumps(normalized) if normalized else None


def _normalize_flags(flags: list | None) -> str | None:
    if flags is None:
        return None
    if not isinstance(flags, list):
        raise ToolError("flags must be a list of short strings")
    if len(flags) > FLAGS_CAP:
        raise ToolError(f"flags accepts at most {FLAGS_CAP} entries (got {len(flags)})")
    for flag in flags:
        if not isinstance(flag, str) or not flag.strip():
            raise ToolError("each flag must be a non-empty string")
    cleaned = [_truncate(flag, FLAG_MAX_CHARS) for flag in flags]
    return json.dumps(cleaned) if cleaned else None


def _normalize_bullets(label: str, bullets: list | None) -> list[str] | None:
    """A short list of grounded one-liners — same shape rules as flags."""
    if bullets is None:
        return None
    if not isinstance(bullets, list):
        raise ToolError(f"{label} must be a list of short strings")
    if len(bullets) > FOR_YOU_IF_CAP:
        raise ToolError(
            f"{label} accepts at most {FOR_YOU_IF_CAP} entries (got {len(bullets)}) "
            "— keep the ones actually grounded in his library"
        )
    for bullet in bullets:
        if not isinstance(bullet, str) or not bullet.strip():
            raise ToolError(f"each {label} entry must be a non-empty string")
    cleaned = [_truncate(bullet, FOR_YOU_IF_MAX_CHARS) for bullet in bullets]
    return cleaned or None


def _normalize_comparisons(comparisons: list | None) -> list[dict[str, Any]] | None:
    """The lineage strip: {name, relation, note?, game_id?} entries.

    Relations are a closed vocabulary because the card renders each one
    differently; an unknown relation would silently render as nothing.
    """
    if comparisons is None:
        return None
    if not isinstance(comparisons, list):
        raise ToolError(
            "comparisons must be a list of {name, relation, note, game_id} objects"
        )
    if len(comparisons) > COMPARISONS_CAP:
        raise ToolError(
            f"comparisons accepts at most {COMPARISONS_CAP} entries "
            f"(got {len(comparisons)}) — cite the lineage that matters"
        )
    normalized: list[dict[str, Any]] = []
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            raise ToolError(
                "each comparisons entry must be a {name, relation, note, game_id} object"
            )
        unknown = set(comparison) - {"name", "relation", "note", "game_id"}
        if unknown:
            raise ToolError(
                f"comparisons entries accept only 'name', 'relation', 'note' and "
                f"'game_id' (got {sorted(unknown)})"
            )
        name = comparison.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolError("each comparisons entry needs a non-empty 'name'")
        relation = comparison.get("relation")
        if relation not in COMPARISON_RELATIONS:
            raise ToolError(
                f"Unknown comparisons relation {relation!r}. "
                f"Valid: {list(COMPARISON_RELATIONS)}"
            )
        entry: dict[str, Any] = {
            "name": _truncate(name, COMPARISON_NAME_MAX_CHARS),
            "relation": relation,
        }
        note = comparison.get("note")
        if note is not None:
            if not isinstance(note, str):
                raise ToolError("comparisons note must be a string")
            if note.strip():
                entry["note"] = _truncate(note, COMPARISON_NOTE_MAX_CHARS)
        comparison_id = comparison.get("game_id")
        if comparison_id is not None:
            if isinstance(comparison_id, bool) or not isinstance(comparison_id, int):
                raise ToolError("comparisons game_id must be an integer")
            entry["game_id"] = comparison_id
        normalized.append(entry)
    return normalized or None


def _normalize_why_care(why_care: list | None) -> list[dict[str, Any]] | None:
    """The "why care at all" eyebrow: ``{kind, text}`` entries, kinds enumerated.

    Deliberately the editorial half of the pedigree pair: the server fetches
    the studio and its back catalogue (data/media.py), and the model writes the
    part no API holds — the credits behind it, the moment it belongs to. Text
    is truncated like every other one-liner here; a wrong KIND is rejected,
    because the card renders one chip per kind and an unknown one would vanish.
    """
    if why_care is None:
        return None
    if not isinstance(why_care, list):
        raise ToolError("why_care must be a list of {kind, text} objects")
    if len(why_care) > WHY_CARE_CAP:
        raise ToolError(
            f"why_care accepts at most {WHY_CARE_CAP} entries "
            f"(got {len(why_care)}) — keep the ones a reader would check"
        )
    normalized: list[dict[str, Any]] = []
    for entry in why_care:
        if not isinstance(entry, dict):
            raise ToolError("each why_care entry must be a {kind, text} object")
        unknown = set(entry) - {"kind", "text"}
        if unknown:
            raise ToolError(
                f"why_care entries accept only 'kind' and 'text' (got {sorted(unknown)})"
            )
        kind = entry.get("kind")
        if kind not in WHY_CARE_KINDS:
            raise ToolError(
                f"Unknown why_care kind {kind!r}. Valid: {list(WHY_CARE_KINDS)}"
            )
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("each why_care entry needs a non-empty 'text'")
        normalized.append(
            {"kind": kind, "text": _truncate(text, WHY_CARE_TEXT_MAX_CHARS)}
        )
    return normalized or None


def _build_presentation(
    elevator_pitch: str | None,
    for_you_if: list | None,
    not_for_you_if: list | None,
    comparisons: list | None,
    why_care: list | None,
    craft_note: str | None,
) -> str | None:
    """The presentation params as ONE JSON column value (NULL when empty).

    Only non-null members are stored, so the column says what was authored
    rather than padding absent halves with nulls.
    """
    presentation: dict[str, Any] = {}
    if elevator_pitch is not None and not isinstance(elevator_pitch, str):
        raise ToolError("elevator_pitch must be a string")
    pitch = _truncate(elevator_pitch, ELEVATOR_PITCH_MAX_CHARS) if elevator_pitch else None
    if pitch:
        presentation["elevator_pitch"] = pitch
    if craft_note is not None and not isinstance(craft_note, str):
        raise ToolError("craft_note must be a string")
    note = _truncate(craft_note, CRAFT_NOTE_MAX_CHARS) if craft_note else None
    if note:
        presentation["craft_note"] = note
    for label, bullets in (
        ("for_you_if", for_you_if),
        ("not_for_you_if", not_for_you_if),
    ):
        cleaned = _normalize_bullets(label, bullets)
        if cleaned:
            presentation[label] = cleaned
    resolved_comparisons = _normalize_comparisons(comparisons)
    if resolved_comparisons:
        presentation["comparisons"] = resolved_comparisons
    resolved_why_care = _normalize_why_care(why_care)
    if resolved_why_care:
        presentation["why_care"] = resolved_why_care
    return json.dumps(presentation) if presentation else None


def _normalize_declared(
    label: str, value: str | None, cap: int, *, lowercase: bool
) -> str | None:
    """Light normalization for a declared provenance value — never a vocabulary.

    Strip, empty → None (an empty claim is no claim, which is exactly what NULL
    means here), cap-check, and lowercase the free-form identifiers so
    "Claude-Opus-5" and "claude-opus-5" don't split a calibration group. Skill
    VERSIONS keep their case — a version string is not a name. Deliberately no
    pattern check: a new skill or a new model must never need a code change to
    be recordable.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{label} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > cap:
        raise ToolError(
            f"{label} accepts at most {cap} characters (got {len(text)}) — it "
            "records what recorded the verdict, not a description"
        )
    return text.lower() if lowercase else text


def _check_range(label: str, value: float | None, low: float, high: float) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"{label} must be a number")
    if not low <= value <= high:
        raise ToolError(f"{label} must be between {low} and {high} (got {value})")


async def _validate_assessment_inputs(
    *,
    name: str | None,
    appid: int | None,
    game_id: int | None,
    verdict: str | None,
    assessed_at: str | None,
    summary: str | None,
    craft_adjusted: float | None,
    craft_positive_pct: float | None,
    review_count: int | None,
    recent_trajectory: str | None,
    opencritic_score: float | None,
    fit_call: str | None,
    anchors_cited: list | None,
    flags: list | None,
    price_seen: float | None,
    price_currency: str | None,
    price_platform: str | None,
    target_price: float | None,
    instead_game_id: int | None,
    steam_appid: int | None,
    context: str | None,
    skill: str | None,
    skill_version: str | None,
    model: str | None,
    elevator_pitch: str | None,
    for_you_if: list | None,
    not_for_you_if: list | None,
    comparisons: list | None,
    why_care: list | None,
    craft_note: str | None,
) -> dict[str, Any]:
    """Validate EVERYTHING before any write (ADR 0004's multi-mode rule).

    Returns the normalized column values. A half-validated write would leave a
    verdict recorded with the component that failed silently missing.
    """
    if name is None and appid is None and game_id is None:
        raise ToolError(
            "Provide a game identity (name, game_id, or appid) — an assessment "
            "is always about one game."
        )
    if verdict is None:
        raise ToolError(f"verdict is required; one of {list(ASSESSMENT_VERDICTS)}")
    if verdict not in ASSESSMENT_VERDICTS:
        raise ToolError(
            f"Unknown verdict {verdict!r}. Valid: {list(ASSESSMENT_VERDICTS)}"
        )
    if recent_trajectory is not None and recent_trajectory not in ASSESSMENT_TRAJECTORIES:
        raise ToolError(
            f"Unknown recent_trajectory {recent_trajectory!r}. "
            f"Valid: {list(ASSESSMENT_TRAJECTORIES)}"
        )
    if fit_call is not None and fit_call not in ASSESSMENT_FIT_CALLS:
        raise ToolError(
            f"Unknown fit_call {fit_call!r}. Valid: {list(ASSESSMENT_FIT_CALLS)} "
            "(the same strings get_assessment_context's fit.suggested_call uses)"
        )

    # craft_adjusted is the 0-1 sample-adjusted score and craft_positive_pct the
    # raw 0-100 percentage — deliberately NOT interchangeable, so 88 in the
    # first is a caller mistake worth catching rather than silently rescaling.
    _check_range("craft_adjusted", craft_adjusted, 0.0, 1.0)
    _check_range("craft_positive_pct", craft_positive_pct, 0.0, 100.0)
    _check_range("opencritic_score", opencritic_score, 0.0, 100.0)
    if review_count is not None and (isinstance(review_count, bool) or review_count < 0):
        raise ToolError("review_count must be >= 0")
    for label, price in (("price_seen", price_seen), ("target_price", target_price)):
        if price is not None:
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                raise ToolError(f"{label} must be a number")
            if price < 0:
                raise ToolError(f"{label} must be >= 0")
    if steam_appid is not None and (isinstance(steam_appid, bool) or steam_appid <= 0):
        raise ToolError("steam_appid must be a positive integer")
    if appid is not None and (isinstance(appid, bool) or appid <= 0):
        raise ToolError("appid must be a positive integer")

    if instead_game_id is not None:
        async with get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE id = ?", (instead_game_id,)
            )
        if row is None:
            raise ToolError(
                f"instead_game_id {instead_game_id} does not exist — pass the "
                "game_id of the library game you're pointing at instead"
            )

    return {
        "assessed_at": _normalize_assessed_at(assessed_at),
        "verdict": verdict,
        "summary": _truncate(summary, SUMMARY_MAX_CHARS) if summary else None,
        "craft_adjusted": craft_adjusted,
        "craft_positive_pct": craft_positive_pct,
        "review_count": review_count,
        "recent_trajectory": recent_trajectory,
        "opencritic_score": opencritic_score,
        "fit_call": fit_call,
        "anchors_cited": _normalize_anchors(anchors_cited),
        "flags": _normalize_flags(flags),
        "price_seen": price_seen,
        "price_currency": price_currency.strip().upper()[:8] if price_currency else None,
        "price_platform": price_platform.strip().lower() if price_platform else None,
        "target_price": target_price,
        "instead_game_id": instead_game_id,
        "steam_appid": steam_appid if steam_appid is not None else appid,
        "context": _truncate(context, CONTEXT_MAX_CHARS) if context else None,
        "skill": _normalize_declared("skill", skill, SKILL_MAX_CHARS, lowercase=True),
        "skill_version": _normalize_declared(
            "skill_version", skill_version, SKILL_VERSION_MAX_CHARS, lowercase=False
        ),
        "model": _normalize_declared("model", model, MODEL_MAX_CHARS, lowercase=True),
        "presentation": _build_presentation(
            elevator_pitch, for_you_if, not_for_you_if, comparisons, why_care, craft_note
        ),
    }


async def _existing_exact_game_id(
    name: str, appid: int | None, *, match_by_name: bool
) -> int | None:
    """``upsert_game``'s own two lookups, run ahead of it.

    upsert_game adopts-or-mints in one statement and never says which it did,
    so ``created`` would otherwise be a guess. Mirroring its identifier-then-
    exact-name lookups here (same order, same ``match_existing_by_name``
    branch) keeps the reported mode honest without a second write path.
    """
    if appid is not None:
        row = await get_game_by_appid(appid)
        if row is not None:
            return row["id"]
    if not match_by_name:
        return None
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT id FROM games WHERE lower(name) = lower(?) ORDER BY id LIMIT 1",
            (name,),
        )
    return row["id"] if row is not None else None


async def _adopt_or_mint_assessed_game(
    name: str, appid: int | None
) -> tuple[int, str]:
    """EXACT-name adopt, else mint — the write path's whole name resolution.

    Same path sync_wishlist mints an unowned row through — ``upsert_game``
    with the anti-collapse guards — because an assessed candidate is exactly
    as ownership-free as a wishlist item: no game_platforms row, so no
    identifier row either, which is why the Steam appid lands on the
    assessment itself. The guard matters for the same reason it does there: an
    exact-name row that already owns Steam under a DIFFERENT appid is a
    different game (Dead Space 2008 vs 2023), and attaching onto it would
    collapse two titles.

    Deliberately NO tiered/fuzzy matching, mirroring add_game_to_platform
    (issue #150): a fuzzy write attached "Alan Wake 2" onto the library's
    "Alan Wake" and reported created=false, which is invisible. A typo now
    mints a visible phantom row instead — repairable with merge_games — which
    is the trade the whole write surface already makes.

    Returns (game_id, "exact" | "minted").
    """
    conflict = appid is not None and await exact_name_steam_conflict(name, appid)
    existing = await _existing_exact_game_id(name, appid, match_by_name=not conflict)
    if conflict:
        game_id = await upsert_game(appid, name, match_existing_by_name=False)
    else:
        game_id = await upsert_game(appid, name)
    return game_id, "exact" if existing is not None else "minted"


async def _live_ownership_state(game_id: int) -> tuple[bool, bool]:
    async with get_db() as db:
        owned_row = await db.execute_fetchone(
            "SELECT 1 FROM game_platforms WHERE game_id = ? AND owned = 1 LIMIT 1",
            (game_id,),
        )
        wishlisted_row = await db.execute_fetchone(
            "SELECT 1 FROM game_wishlist WHERE game_id = ? LIMIT 1", (game_id,)
        )
    return owned_row is not None, wishlisted_row is not None


async def void_assessment(assessment_id: int) -> dict[str, Any]:
    """Hard-delete one misfiled assessment row.

    A HARD delete on an otherwise append-only table, on purpose: a verdict
    filed onto the wrong game was never an observation of that game, and
    leaving it as tombstoned history would keep it in the calibration report
    and in the wrong game's past_assessments. This is the repair for a misfile
    noticed after the same-UTC-day replace window has passed (within the day,
    re-recording simply overwrites).
    """
    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT a.id, a.game_id, a.assessed_at, a.verdict, g.name
               FROM game_assessments a
               JOIN games g ON g.id = a.game_id
               WHERE a.id = ?""",
            (assessment_id,),
        )
        if row is None:
            raise ToolError(
                f"Assessment {assessment_id} not found — get the id from "
                "record_assessment's response, get_game_detail's assessments "
                "block, or get_stats(report=\"assessments\")"
            )
        game_id = row["game_id"]
        await db.execute("DELETE FROM game_assessments WHERE id = ?", (assessment_id,))
        # Bare = the third ownership-free shape (detect_orphan_games) is gone
        # too: nothing owns it, nothing wants it, nothing assessed it.
        remaining = await db.execute_fetchone(
            """SELECT
                 (SELECT COUNT(*) FROM game_platforms WHERE game_id = ?) AS platforms,
                 (SELECT COUNT(*) FROM game_wishlist WHERE game_id = ?) AS wishlist,
                 (SELECT COUNT(*) FROM game_assessments WHERE game_id = ?) AS assessments
            """,
            (game_id, game_id, game_id),
        )
        await db.commit()

    result: dict[str, Any] = {
        "voided": True,
        "assessment_id": assessment_id,
        "game_id": game_id,
        "name": row["name"],
        "verdict": row["verdict"],
        "assessed_at": row["assessed_at"],
    }
    if remaining is not None and not any(
        remaining[column] for column in ("platforms", "wishlist", "assessments")
    ):
        # Reported, never done — deleting a games row is a confirmed human
        # action, and the row may predate the assessment entirely.
        result["suggested_action"] = {
            "tool": "delete_game",
            "args": {"game_id": game_id, "confirm": False},
            "note": (
                "this game row now has no ownership, wishlist entry or "
                "assessment left — if the void stranded a row minted for the "
                "misfiled verdict, preview the delete before confirming"
            ),
        }
    return result


# ── The evaluation package (card payload) ────────────────────────────────────
#
# Everything below decorates a verdict that is ALREADY committed. It runs after
# the write, never before, and is bounded twice over: an inner budget on the one
# network step (media) so a hanging provider costs the trailer rather than the
# whole block, and an outer wait_for so nothing here can hold the tool open.
# Any failure degrades to `errors` — recording a verdict must never fail
# because a screenshot didn't load.

PACKAGE_TIMEOUT_SECONDS = 10
_PACKAGE_MEDIA_TIMEOUT_SECONDS = 8
PACKAGE_PAST_CAP = 5
# The similar-games cap lives with the block it caps (tools/game_media.py):
# the same row renders on the detail card.

# The stored Metascore. It hangs off the PLATFORM row (migration v11 moved it
# out of `games` into game_platform_enrichment), so it needs the same shape as
# STEAM_APPID_SQL rather than a bare column: MAX over the game's platform rows,
# like every other metacritic rollup in the codebase (tools/library.py). NULL
# for an unowned candidate with no platform row at all, which is honest — the
# library holds no score for a game it has never enriched.
_METACRITIC_SQL = """
(
    SELECT MAX(mgpe.metacritic_score)
    FROM game_platform_enrichment mgpe
    JOIN game_platforms mgp ON mgp.id = mgpe.game_platform_id
    WHERE mgp.game_id = g.id
)
"""

# Ownership/rating/playtime for a set of games, as the card renders them. Same
# rating priority as the anchors and calibration queries (full-weight sources
# first, then lowest id), same owned-only playtime rollup.
_PACKAGE_ANNOTATION_SQL = f"""
SELECT g.id AS game_id,
       g.name,
       g.igdb_id,
       g.release_date,
       g.completion_status,
       g.cover_image_id,
       g.hltb_main,
       g.hltb_extra,
       {_METACRITIC_SQL} AS metacritic_score,
       {STEAM_APPID_SQL} AS steam_appid,
       {OWNED_SQL} AS owned,
       (
           SELECT {PLAYTIME_SUM_SQL} FROM game_platforms gp
           WHERE gp.game_id = g.id AND gp.owned = 1
       ) AS playtime_minutes,
       (
           SELECT rt.normalized_score FROM ratings rt
           WHERE rt.game_id = g.id AND rt.normalized_score IS NOT NULL
           ORDER BY CASE rt.source WHEN 'manual' THEN 0 WHEN 'backloggd' THEN 1
                    ELSE 2 END, rt.id
           LIMIT 1
       ) AS my_rating
FROM games g
WHERE {{predicate}}
"""


def _release_year(release_date: str | None) -> int | None:
    """Year component of a stored release_date ('2008-10-13'), or None."""
    if not release_date:
        return None
    head = str(release_date).strip()[:4]
    return int(head) if head.isdigit() else None


def _hours(minutes: float | None) -> float | None:
    return round(minutes / 60, 1) if minutes is not None else None


async def _annotate_games(column: str, values: list) -> dict[Any, Any]:
    """{value: row} for the given games, looked up by ``column`` (id/igdb_id)."""
    keys = [value for value in dict.fromkeys(values) if value is not None]
    if not keys:
        return {}
    placeholders = ", ".join("?" * len(keys))
    async with get_db() as db:
        rows = await db.execute_fetchall(
            _PACKAGE_ANNOTATION_SQL.format(predicate=f"g.{column} IN ({placeholders})"),
            keys,
        )
    result_key = "game_id" if column == "id" else column
    return {row[result_key]: row for row in rows}


def _block_or_none(block: dict[str, Any]) -> dict[str, Any] | None:
    """A block is absent when every one of its members is."""
    return block if any(value is not None for value in block.values()) else None


async def _package_anchors(anchors_cited: str | None) -> list[dict[str, Any]]:
    """Cited anchors resolved against the library — by game_id only.

    An entry without a game_id passes through name-only. Resolving a bare name
    here would be the same loose write-path match issue #150 removed, one layer
    down: a wrong cover under a verdict's anchor chip is a claim about his
    library that nothing else would surface.
    """
    cited = json.loads(anchors_cited) if anchors_cited else []
    library = await _annotate_games("id", [a.get("game_id") for a in cited])
    anchors: list[dict[str, Any]] = []
    for anchor in cited:
        row = library.get(anchor.get("game_id"))
        anchors.append(
            {
                "game_id": anchor.get("game_id"),
                "name": row["name"] if row is not None else anchor.get("name"),
                "rating": row["my_rating"] if row is not None else None,
                "playtime_hours": (
                    _hours(row["playtime_minutes"]) if row is not None else None
                ),
                "completion_status": (
                    row["completion_status"] if row is not None else None
                ),
                "cover_url": (
                    cover_url(row["cover_image_id"], row["steam_appid"])
                    if row is not None
                    else None
                ),
            }
        )
    return anchors


async def _package_comparisons(presentation: dict[str, Any]) -> list[dict[str, Any]]:
    """The lineage strip, annotated with what he owns of it.

    A comparison names a game the model knows about; the library may or may not
    have it. Resolution is game_id, else an EXACT name match — never fuzzy, for
    the same reason the write path isn't.
    """
    comparisons = presentation.get("comparisons") or []
    resolved: list[tuple[dict[str, Any], int | None]] = []
    for comparison in comparisons:
        game_id = comparison.get("game_id")
        if game_id is None:
            game_id = await _existing_exact_game_id(
                comparison["name"], None, match_by_name=True
            )
        resolved.append((comparison, game_id))

    library = await _annotate_games("id", [gid for _, gid in resolved])
    items: list[dict[str, Any]] = []
    for comparison, game_id in resolved:
        row = library.get(game_id)
        items.append(
            {
                # The resolved row's name, like the anchors path: a mismatched
                # game_id then shows the row's REAL name beside its stats — a
                # visible mistake — instead of one game's name over another
                # game's ownership and rating. The declared name still stands
                # in the stored presentation JSON.
                "name": row["name"] if row is not None else comparison["name"],
                "relation": comparison["relation"],
                "note": comparison.get("note"),
                "game_id": game_id,
                "owned": bool(row["owned"]) if row is not None else None,
                "my_rating": row["my_rating"] if row is not None else None,
                "playtime_hours": (
                    _hours(row["playtime_minutes"]) if row is not None else None
                ),
            }
        )
    return items


async def _build_package(
    *,
    game_id: int,
    values: dict[str, Any],
    appid: int | None,
    previous: list,
    owned: bool,
    wishlisted: bool,
) -> dict[str, Any]:
    """Assemble the card payload for one just-recorded verdict."""
    errors: list[str] = []
    presentation = json.loads(values["presentation"]) if values["presentation"] else {}

    async with get_db() as db:
        row = await db.execute_fetchone(
            _PACKAGE_ANNOTATION_SQL.format(predicate="g.id = ?"), (game_id,)
        )
        platform_rows = await db.execute_fetchall(
            """SELECT platform, price_paid, price_currency, purchase_source,
                      bundle_name
               FROM game_platforms
               WHERE game_id = ? AND owned = 1
               ORDER BY acquired_at IS NULL, acquired_at, id""",
            (game_id,),
        )

    acquisition = next(
        (p for p in platform_rows if p["price_paid"] is not None), None
    )

    try:
        pace = await _recent_pace()
        # A weekly rate, because that is how the card reads a backlog ("≈9
        # weeks at your current pace"); the window itself stays 30 days so the
        # number is the same observation get_assessment_context reports.
        recent_weekly_minutes = round(pace["total_minutes"] * 7 / PACE_WINDOW_DAYS)
    except Exception:
        logger.warning("Package pace lookup failed for game %s", game_id, exc_info=True)
        errors.append("pace: unavailable")
        recent_weekly_minutes = None

    media_appid = appid or values["steam_appid"] or (row["steam_appid"] if row else None)
    media_payload: dict[str, Any] | None = None
    try:
        media_payload = await asyncio.wait_for(
            get_game_media(
                steam_appid=media_appid,
                igdb_id=row["igdb_id"] if row else None,
                name=row["name"] if row else None,
            ),
            timeout=_PACKAGE_MEDIA_TIMEOUT_SECONDS,
        )
        # get_game_media never raises for a provider failure — it reports one
        # here instead, so an outage doesn't masquerade as a media-less game.
        if media_payload:
            errors.extend(
                f"media: {source_error}"
                for source_error in media_payload.get("errors") or []
            )
    except Exception:
        logger.warning("Package media fetch failed for game %s", game_id, exc_info=True)
        errors.append("media: fetch failed")

    # The media block and its similar-games row are the neutral game
    # representation get_game_detail(media=True) also serves — shaped in
    # tools/game_media.py so both renderers get the same keys. The FETCH stays
    # here, under this module's own budget and errors bookkeeping: the package
    # decorates a committed verdict and must degrade, not raise.
    media_context_block = await media_context(media_payload)

    past_items = [
        {
            "assessed_at": past["assessed_at"],
            "verdict": past["verdict"],
            "summary": past["summary"],
            "price_seen": past["price_seen"],
            "price_currency": past["price_currency"],
        }
        for past in previous[:PACKAGE_PAST_CAP]
    ]

    return {
        "game": {
            "game_id": game_id,
            "name": row["name"] if row else None,
            "release_year": _release_year(row["release_date"]) if row else None,
            # An unowned candidate has neither an IGDB cover slug nor a
            # game_platforms identifier row, so the games row yields nothing and
            # the card fell back to its name-seeded gradient plate — beside real
            # screenshots, which looks like a bug. The appid the media lookup
            # already resolved carries the store capsule.
            "cover_url": (
                (cover_url(row["cover_image_id"], row["steam_appid"]) if row else None)
                or cover_url(None, media_appid)
            ),
        },
        "verdict": values["verdict"],
        "summary": values["summary"],
        "presentation": _block_or_none(
            {
                "elevator_pitch": presentation.get("elevator_pitch"),
                "craft_note": presentation.get("craft_note"),
                "for_you_if": presentation.get("for_you_if"),
                "not_for_you_if": presentation.get("not_for_you_if"),
                # Echoed with the rest of the authored half, so the block keeps
                # a stable shape: null here means "unauthored", not "absent".
                "why_care": presentation.get("why_care"),
            }
        ),
        "comparisons": await _package_comparisons(presentation),
        "craft": _block_or_none(
            {
                "adjusted": values["craft_adjusted"],
                "positive_pct": values["craft_positive_pct"],
                "review_count": values["review_count"],
                "trajectory": values["recent_trajectory"],
                "opencritic_score": values["opencritic_score"],
                # The one craft number the caller doesn't supply: the library's
                # own stored Metacritic score, so the card's score row isn't
                # two lonely chips when only critics have spoken.
                "metacritic_score": row["metacritic_score"] if row else None,
            }
        ),
        "fit_call": values["fit_call"],
        "flags": json.loads(values["flags"]) if values["flags"] else [],
        "anchors": await _package_anchors(values["anchors_cited"]),
        "ownership": {
            "owned": owned,
            "wishlisted": wishlisted,
            "platforms": sorted(p["platform"] for p in platform_rows),
            "completion_status": row["completion_status"] if row else None,
            "my_rating": row["my_rating"] if row else None,
            "playtime_hours": _hours(row["playtime_minutes"]) if row else None,
            "price_paid": acquisition["price_paid"] if acquisition else None,
            "price_currency": acquisition["price_currency"] if acquisition else None,
            "purchase_source": acquisition["purchase_source"] if acquisition else None,
            "bundle_name": acquisition["bundle_name"] if acquisition else None,
        },
        "time": _block_or_none(
            {
                "hltb_main_hours": row["hltb_main"] if row else None,
                "hltb_extra_hours": row["hltb_extra"] if row else None,
                "recent_weekly_minutes": recent_weekly_minutes,
            }
        ),
        "price": _block_or_none(
            {
                "seen": values["price_seen"],
                "currency": values["price_currency"],
                "platform": values["price_platform"],
                "target": values["target_price"],
            }
        ),
        "media": media_context_block["media"],
        "similar": media_context_block["similar"],
        "pedigree": media_context_block["pedigree"],
        "past": (
            {
                "items": past_items,
                "count": len(previous),
                "truncated": len(previous) > len(past_items),
            }
            if previous
            else None
        ),
        "errors": errors,
    }


async def _safe_package(**kwargs: Any) -> dict[str, Any]:
    """``_build_package`` that can never fail the recording it decorates."""
    try:
        return await asyncio.wait_for(
            _build_package(**kwargs), timeout=PACKAGE_TIMEOUT_SECONDS
        )
    except Exception:
        logger.warning(
            "Evaluation package assembly failed for game %s",
            kwargs.get("game_id"),
            exc_info=True,
        )
        values = kwargs.get("values") or {}
        # The verdict is already recorded; answer with the minimum a card needs
        # to render at all, and say what is missing.
        return {
            "game": {"game_id": kwargs.get("game_id")},
            "verdict": values.get("verdict"),
            "errors": ["package: assembly failed"],
        }


async def record_assessment(
    name: str | None = None,
    appid: int | None = None,
    game_id: int | None = None,
    verdict: str | None = None,
    assessed_at: str | None = None,
    summary: str | None = None,
    craft_adjusted: float | None = None,
    craft_positive_pct: float | None = None,
    review_count: int | None = None,
    recent_trajectory: str | None = None,
    opencritic_score: float | None = None,
    fit_call: str | None = None,
    anchors_cited: list | None = None,
    flags: list | None = None,
    price_seen: float | None = None,
    price_currency: str | None = None,
    price_platform: str | None = None,
    target_price: float | None = None,
    instead_game_id: int | None = None,
    steam_appid: int | None = None,
    context: str | None = None,
    skill: str | None = None,
    skill_version: str | None = None,
    model: str | None = None,
    elevator_pitch: str | None = None,
    for_you_if: list | None = None,
    not_for_you_if: list | None = None,
    comparisons: list | None = None,
    why_care: list | None = None,
    craft_note: str | None = None,
    *,
    with_package: bool = True,
) -> dict:
    """Record one game-quality verdict and its components.

    Identity resolves by game_id, then appid (identifier row, then the appid a
    past assessment carries), then name — and the name branch is EXACT-match-
    or-mint (``_adopt_or_mint_assessed_game``), like add_game_to_platform.
    Loose matching stays on the read path only; see that function for why.
    At most one assessment per game per UTC day: a same-day re-record REPLACES
    that day's row (replaced=true) instead of appending a second verdict.

    ``skill`` / ``skill_version`` / ``model`` record the METHODOLOGY behind the
    verdict, and are DECLARED-ONLY: whatever the recording client says about
    itself, normalized only lightly. Nothing here ever fills them in — an
    omitted value stays NULL, meaning "unknown", which is the honest answer for
    an ad-hoc assessment or a stale installed copy of the skill.

    ``elevator_pitch`` / ``craft_note`` / ``for_you_if`` / ``not_for_you_if`` /
    ``comparisons`` / ``why_care`` are the model-authored PRESENTATION; they
    persist together as one ``presentation`` JSON column and are declared
    content like the provenance columns — capped and truncated, never
    synthesized here. ``why_care`` is the editorial half of the pedigree pair:
    the server fetches the studio and its back catalogue, the model writes the
    credits and the moment no API holds.

    A misfiled verdict is repaired with ``void_assessment``, a tool of its
    own — hard-deleting a row is not idempotent, and this one is.

    A single-item recording additionally answers with ``package``: the card
    payload (media, comparisons and anchors resolved against the library, price
    and time context, prior verdicts). It is assembled AFTER the write, is
    bounded, and degrades into its own ``errors`` list — nothing in it can fail
    or delay the recording it describes.

    Never touches tag_affinity, discover_games, or the wishlist — a
    wishlist_for_sale verdict on an unwishlisted game answers with a
    suggested_action naming add_game_to_platform, and stops there.
    """
    values = await _validate_assessment_inputs(
        name=name,
        appid=appid,
        game_id=game_id,
        verdict=verdict,
        assessed_at=assessed_at,
        summary=summary,
        craft_adjusted=craft_adjusted,
        craft_positive_pct=craft_positive_pct,
        review_count=review_count,
        recent_trajectory=recent_trajectory,
        opencritic_score=opencritic_score,
        fit_call=fit_call,
        anchors_cited=anchors_cited,
        flags=flags,
        price_seen=price_seen,
        price_currency=price_currency,
        price_platform=price_platform,
        target_price=target_price,
        instead_game_id=instead_game_id,
        steam_appid=steam_appid,
        context=context,
        skill=skill,
        skill_version=skill_version,
        model=model,
        elevator_pitch=elevator_pitch,
        for_you_if=for_you_if,
        not_for_you_if=not_for_you_if,
        comparisons=comparisons,
        why_care=why_care,
        craft_note=craft_note,
    )

    resolved_id, mode = await _resolve_by_id_or_appid(appid, game_id)
    if resolved_id is None:
        if game_id is not None:
            raise ToolError(f"Game {game_id} not found")
        if name is None:
            raise ToolError(
                f"No library game matches appid {appid} — pass name= as well so "
                "the candidate can be recorded as a new row"
            )
        resolved_id, mode = await _adopt_or_mint_assessed_game(
            name, appid or steam_appid
        )
    created = mode == "minted"

    owned_now, wishlisted_now = await _live_ownership_state(resolved_id)
    day = values["assessed_at"][:10]

    async with get_db() as db:
        game_row = await db.execute_fetchone(
            "SELECT name FROM games WHERE id = ?", (resolved_id,)
        )
        # Prior verdicts EXCLUDING the day being written: a same-day re-record
        # is a correction of this assessment, not a repeat ask. The package's
        # `past` block reads these same rows — hence summary/price here rather
        # than a second query for the same history.
        previous = await db.execute_fetchall(
            """SELECT assessed_at, verdict, summary, price_seen, price_currency
               FROM game_assessments
               WHERE game_id = ? AND date(assessed_at) != ?
               ORDER BY assessed_at DESC, id DESC""",
            (resolved_id, day),
        )
        same_day = await db.execute_fetchone(
            """SELECT id FROM game_assessments
               WHERE game_id = ? AND date(assessed_at) = ?""",
            (resolved_id, day),
        )
        columns = ["game_id", "assessed_at", "verdict", *_ASSESSMENT_WRITE_COLUMNS,
                   "owned_at_assessment", "wishlisted_at_assessment"]
        params = [
            resolved_id,
            values["assessed_at"],
            values["verdict"],
            *(values[column] for column in _ASSESSMENT_WRITE_COLUMNS),
            int(owned_now),
            int(wishlisted_now),
        ]
        # Provenance is in the update set too: a same-day re-record under a
        # newer skill version (or a different model) describes the row that now
        # stands, so the day's row must carry the methodology that produced it.
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in (
                "assessed_at",
                "verdict",
                *_ASSESSMENT_WRITE_COLUMNS,
                "owned_at_assessment",
                "wishlisted_at_assessment",
            )
        )
        cursor = await db.execute(
            f"""INSERT INTO game_assessments ({", ".join(columns)})
                VALUES ({", ".join("?" * len(columns))})
                ON CONFLICT(game_id, date(assessed_at)) DO UPDATE SET {updates}""",
            params,
        )
        assessment_id = same_day["id"] if same_day is not None else cursor.lastrowid
        await db.commit()

    resolved_name = game_row["name"] if game_row else name
    result: dict[str, Any] = {
        "game_id": resolved_id,
        "name": resolved_name,
        "created": created,
        "replaced": same_day is not None,
        "assessment_id": assessment_id,
        "assessed_at": values["assessed_at"],
        "verdict": values["verdict"],
        # How identity resolved, so a caller can diff matched_name against the
        # candidate it meant (issue #150) rather than trusting created=false.
        "resolution": {
            "mode": mode,
            "query": _resolution_query(mode, name, appid, game_id),
            "matched_name": resolved_name,
        },
    }
    if previous:
        result["repeat_ask"] = {
            "previous_count": len(previous),
            "last_assessed_at": previous[0]["assessed_at"],
            "last_verdict": previous[0]["verdict"],
        }
    if values["verdict"] == "wishlist_for_sale" and not wishlisted_now:
        # Reported, never done: wishlist writes stay a confirmed human action
        # (ADR 0006 decision 5 — verdict-driven promotion is deliberately not
        # automatic).
        result["suggested_action"] = {
            "tool": "add_game_to_platform",
            "args": {
                "game_id": resolved_id,
                "platform": values["price_platform"] or "steam",
                "owned": False,
                "wishlist_source": "assessment",
            },
            "note": (
                "not wishlisted yet — offer this promotion; recording a verdict "
                "never writes the wishlist itself"
            ),
        }

    # Everything above is the recorded fact; the package is decoration, built
    # only after the write committed and never able to undo it. Keyword-only
    # and off in bulk mode (see record_assessments_batch): a card renders one
    # game, and 200 of them would be 200 media fetches nothing displays.
    if with_package:
        result["package"] = await _safe_package(
            game_id=resolved_id,
            values=values,
            appid=appid,
            previous=previous,
            owned=owned_now,
            wishlisted=wishlisted_now,
        )
    return result


async def record_assessments_batch(items: list[dict]) -> dict:
    """Record many assessments in one call.

    Standard bulk conventions (tools/batch.py): only an empty or over-cap
    items list raises; everything else is a per-item status preserving input
    order. Unlike every other bulk write tool there is NO deferred affinity
    recompute at the end — verdicts never feed affinity (ADR 0006). Bulk
    results also carry no evaluation package: it is a per-game card payload,
    and assembling one per item would fan out to a media fetch per item for a
    response nothing renders.
    """
    check_batch_items(items)
    results = [
        await apply_batch_item(
            item, RECORD_ASSESSMENT_ITEM_KEYS, partial(record_assessment, with_package=False)
        )
        for item in items
    ]
    return {
        "results": results,
        "total": len(items),
        "ok": count_status(results, "ok"),
        "errors": count_status(results, "error"),
    }


# ── Reports (reached as get_stats(report=...)) ───────────────────────────────


async def get_assessments_report(
    limit: int = ASSESSMENTS_REPORT_DEFAULT_LIMIT,
    offset: int = 0,
    verdict: str | None = None,
) -> dict:
    """Newest-first page of recorded assessments, optionally one verdict only."""
    if verdict is not None and verdict not in ASSESSMENT_VERDICTS:
        raise ToolError(
            f"Unknown verdict {verdict!r}. Valid: {list(ASSESSMENT_VERDICTS)}"
        )
    limit = max(1, clamp_limit(int(limit), ASSESSMENTS_REPORT_MAX_LIMIT))
    offset = max(0, int(offset))

    where = "" if verdict is None else "WHERE a.verdict = ?"
    params: list = [] if verdict is None else [verdict]

    async with get_db() as db:
        total_row = await db.execute_fetchone(
            f"SELECT COUNT(*) AS c FROM game_assessments a {where}", params
        )
        rows = await db.execute_fetchall(
            f"""SELECT a.id AS assessment_id, a.game_id, g.name,
                       a.assessed_at, a.verdict, a.summary,
                       a.price_seen, a.price_currency, a.target_price,
                       a.skill, a.skill_version, a.model,
                       EXISTS (
                           SELECT 1 FROM game_platforms gp
                           WHERE gp.game_id = a.game_id AND gp.owned = 1
                       ) AS owned,
                       EXISTS (
                           SELECT 1 FROM game_wishlist w WHERE w.game_id = a.game_id
                       ) AS wishlisted
                FROM game_assessments a
                JOIN games g ON g.id = a.game_id
                {where}
                ORDER BY a.assessed_at DESC, a.id DESC
                LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        )

    total = total_row["c"] if total_row else 0
    return {
        "assessments": [
            {
                # assessment_id is what void_assessment(assessment_id=…)
                # takes — this report and the per-game summary blocks are the
                # advertised ways to recover it for a historical misfile.
                "assessment_id": row["assessment_id"],
                "game_id": row["game_id"],
                "name": row["name"],
                "assessed_at": row["assessed_at"],
                "verdict": row["verdict"],
                "summary": row["summary"],
                "price_seen": row["price_seen"],
                "price_currency": row["price_currency"],
                "target_price": row["target_price"],
                # Declared methodology; NULL means the recorder didn't say.
                "skill": row["skill"],
                "skill_version": row["skill_version"],
                "model": row["model"],
                "owned": bool(row["owned"]),
                "wishlisted": bool(row["wishlisted"]),
            }
            for row in rows
        ],
        "total_matches": total,
        "has_more": offset + len(rows) < total,
        "verdict": verdict,
    }


# One representative row per (game, verdict) — the most recent such assessment
# — so re-recording the same verdict for a game never double-counts it in the
# calibration rates below. The price_paid/paid_currency subqueries share one
# ORDER BY (earliest recorded acquisition carrying a price) so they always
# describe the SAME platform row; that order deliberately references nothing
# from the outer row, because SQLite does not resolve correlated names inside
# a subquery's ORDER BY.
#
# Playtime is deliberately PLAYTIME_SUM_SQL over game_platforms — NOT
# v_game_playtime. The PCTL sync recomputes the switch2 gp total from the
# Nintendo daily summaries on every run (see the view's own comment), so the
# only divergence window is sync lag, and every sibling rollup (the anchors
# CTE above, backlog, discovery, library stats) reads the same column through
# the same constant. Calibration's played_count must agree with what those
# reports say, not answer from a different source during the same lag.
_CALIBRATION_SQL = f"""
WITH ranked AS (
    SELECT a.*,
           ROW_NUMBER() OVER (
               PARTITION BY a.game_id, a.verdict
               ORDER BY a.assessed_at DESC, a.id DESC
           ) AS rn
    FROM game_assessments a
)
SELECT r.game_id, g.name, r.verdict, r.assessed_at, r.owned_at_assessment,
       r.price_seen, r.price_currency, r.target_price, r.instead_game_id,
       r.skill, r.skill_version, r.model,
       EXISTS (
           SELECT 1 FROM game_platforms gp
           WHERE gp.game_id = r.game_id AND gp.owned = 1
       ) AS owned_now,
       EXISTS (
           SELECT 1 FROM game_wishlist w WHERE w.game_id = r.game_id
       ) AS wishlisted_now,
       (
           SELECT {PLAYTIME_SUM_SQL} FROM game_platforms gp
           WHERE gp.game_id = r.game_id AND gp.owned = 1
       ) AS playtime_minutes,
       (
           SELECT rt.normalized_score FROM ratings rt
           WHERE rt.game_id = r.game_id AND rt.normalized_score IS NOT NULL
           ORDER BY CASE rt.source WHEN 'manual' THEN 0 WHEN 'backloggd' THEN 1
                    ELSE 2 END, rt.id
           LIMIT 1
       ) AS rating,
       (
           SELECT gp.price_paid FROM game_platforms gp
           WHERE gp.game_id = r.game_id AND gp.owned = 1 AND gp.price_paid IS NOT NULL
           ORDER BY gp.acquired_at IS NULL, gp.acquired_at, gp.id
           LIMIT 1
       ) AS price_paid,
       (
           SELECT gp.price_currency FROM game_platforms gp
           WHERE gp.game_id = r.game_id AND gp.owned = 1 AND gp.price_paid IS NOT NULL
           ORDER BY gp.acquired_at IS NULL, gp.acquired_at, gp.id
           LIMIT 1
       ) AS paid_currency,
       (
           SELECT MAX(gp.last_played) FROM game_platforms gp
           WHERE gp.game_id = r.instead_game_id
       ) AS instead_last_played,
       (SELECT g2.name FROM games g2 WHERE g2.id = r.instead_game_id) AS instead_name
FROM ranked r
JOIN games g ON g.id = r.game_id
WHERE r.rn = 1
ORDER BY r.assessed_at DESC, r.game_id
"""


def _capped(entries: list[dict], limit: int) -> dict:
    """Bounded list block: the page, the true total, and the truncation flag."""
    return {
        "items": entries[:limit],
        "count": len(entries),
        "truncated": len(entries) > limit,
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _group_by_provenance(rows: list, keys: tuple[str, ...]) -> list[dict]:
    """Calibration rates grouped by declared methodology (issue #153).

    Runs over the SAME deduped set the rest of the report uses (one row per
    (game, verdict), its most recent). A NULL — or partially NULL — key is a
    real bucket and is reported with explicit nulls, never dropped: verdicts
    recorded before the columns existed, or by a client that declared nothing,
    are still history, and silently omitting them would make the versioned rows
    look like the whole record.

    Acquisition/playtime/rating rates read only rows he did NOT already own,
    the same restriction by_verdict applies for the same reason.
    """
    grouped: dict[tuple, list] = {}
    for row in rows:
        grouped.setdefault(tuple(row[key] for key in keys), []).append(row)

    entries: list[dict] = []
    for key, group in grouped.items():
        unowned = [row for row in group if not row["owned_at_assessment"]]
        acquired = [row for row in unowned if row["owned_now"]]
        played = [
            row
            for row in acquired
            if (row["playtime_minutes"] or 0) >= CALIBRATION_PLAYED_MINUTES
        ]
        ratings = [row["rating"] for row in acquired if row["rating"] is not None]
        dates = [row["assessed_at"] for row in group]
        entries.append(
            {
                **dict(zip(keys, key, strict=True)),
                "assessments": len(group),
                "distinct_games": len({row["game_id"] for row in group}),
                "first_assessed_at": min(dates),
                "last_assessed_at": max(dates),
                # Denominator + funnel rates, so groups with different owned/
                # unowned mixes stay comparable: a bare acquired_count of 0
                # can't distinguish "every recommendation ignored" from "no
                # unowned candidates at all". Same pct convention as
                # by_verdict (1 decimal, None when the denominator is empty);
                # played/rated read against ACQUIRED — the funnel step they
                # actually measure (recommend → acquire → play → rate).
                "unowned_at_assessment": len(unowned),
                "acquired_count": len(acquired),
                "acquired_pct": (
                    round(100 * len(acquired) / len(unowned), 1) if unowned else None
                ),
                "played_count": len(played),
                "played_pct": (
                    round(100 * len(played) / len(acquired), 1) if acquired else None
                ),
                "rated_count": len(ratings),
                "rated_pct": (
                    round(100 * len(ratings) / len(acquired), 1) if acquired else None
                ),
                "avg_rating": _mean(ratings),
            }
        )

    # Newest last-assessed first (the methodology in use now on top), with a
    # deterministic name tiebreak underneath.
    entries.sort(key=lambda entry: tuple(str(entry[key] or "") for key in keys))
    entries.sort(key=lambda entry: entry["last_assessed_at"], reverse=True)
    return entries


def _per_currency(rows: list[tuple[str | None, float]]) -> list[dict]:
    """Group amounts by currency — never summed or averaged across them."""
    grouped: dict[str | None, list[float]] = {}
    for currency, amount in rows:
        grouped.setdefault(currency, []).append(amount)
    return [
        {"currency": currency, "average": _mean(amounts), "count": len(amounts)}
        for currency, amounts in sorted(grouped.items(), key=lambda kv: (kv[0] or ""))
    ]


async def get_calibration_report(limit: int = CALIBRATION_LIST_DEFAULT_LIMIT) -> dict:
    """How recorded verdicts held up: acquisition, playtime, and ratings since.

    Grouped by verdict, and — over the same deduped rows — by the declared
    methodology behind each call (by_methodology / by_model, issue #153).

    Read-only calibration — nothing here writes, and nothing here feeds
    affinity or discovery scoring (ADR 0006's hard constraint).
    """
    limit = max(1, clamp_limit(int(limit), ASSESSMENTS_REPORT_MAX_LIMIT))

    async with get_db() as db:
        overall_row = await db.execute_fetchone(
            """SELECT COUNT(*) AS total,
                      COUNT(DISTINCT game_id) AS games,
                      MIN(assessed_at) AS first_assessed_at,
                      MAX(assessed_at) AS last_assessed_at
               FROM game_assessments"""
        )
        histogram_rows = await db.execute_fetchall(
            "SELECT verdict, COUNT(*) AS c FROM game_assessments GROUP BY verdict"
        )
        rows = await db.execute_fetchall(_CALIBRATION_SQL)

    histogram: dict[str, int] = {row["verdict"]: row["c"] for row in histogram_rows}
    overall = {
        "total_assessments": overall_row["total"] if overall_row else 0,
        "distinct_games": overall_row["games"] if overall_row else 0,
        "first_assessed_at": overall_row["first_assessed_at"] if overall_row else None,
        "last_assessed_at": overall_row["last_assessed_at"] if overall_row else None,
        "by_verdict": histogram,
    }

    by_verdict: list[dict] = []
    for verdict in ASSESSMENT_VERDICTS:
        verdict_rows = [row for row in rows if row["verdict"] == verdict]
        if not verdict_rows:
            continue
        # Acquisition metrics read only rows he did NOT already own: "did the
        # verdict predict a purchase" is meaningless for a game already on the
        # shelf.
        unowned = [row for row in verdict_rows if not row["owned_at_assessment"]]
        acquired = [row for row in unowned if row["owned_now"]]
        played = [
            row
            for row in acquired
            if (row["playtime_minutes"] or 0) >= CALIBRATION_PLAYED_MINUTES
        ]
        ratings = [row["rating"] for row in acquired if row["rating"] is not None]
        by_verdict.append(
            {
                "verdict": verdict,
                "games": len(verdict_rows),
                "assessments": histogram.get(verdict, 0),
                "unowned_at_assessment": len(unowned),
                "acquired_count": len(acquired),
                "acquired_pct": (
                    round(100 * len(acquired) / len(unowned), 1) if unowned else None
                ),
                "played_count": len(played),
                "rated_count": len(ratings),
                "avg_rating": _mean(ratings),
            }
        )

    wfs = [row for row in rows if row["verdict"] == "wishlist_for_sale"]
    wfs_acquired = [row for row in wfs if row["owned_now"] and row["price_paid"] is not None]
    within_target: dict[str | None, int] = {}
    for row in wfs_acquired:
        # Cross-currency comparison is meaningless (repo rule): a target in EUR
        # says nothing about a price paid in USD, so an unmatched pair simply
        # isn't counted.
        if (
            row["target_price"] is not None
            and (row["price_currency"] is None or row["price_currency"] == row["paid_currency"])
            and row["price_paid"] <= row["target_price"]
        ):
            within_target[row["paid_currency"]] = within_target.get(row["paid_currency"], 0) + 1
    paid_by_currency = _per_currency(
        [(row["paid_currency"], row["price_paid"]) for row in wfs_acquired]
    )
    for entry in paid_by_currency:
        entry["within_target_count"] = within_target.get(entry["currency"], 0)

    wishlist_for_sale = {
        "count": len(wfs),
        "price_seen": _per_currency(
            [
                (row["price_currency"], row["price_seen"])
                for row in wfs
                if row["price_seen"] is not None
            ]
        ),
        "target_price": _per_currency(
            [
                (row["price_currency"], row["target_price"])
                for row in wfs
                if row["target_price"] is not None
            ]
        ),
        "acquired": paid_by_currency,
    }

    def _entry(row) -> dict:
        return {
            "game_id": row["game_id"],
            "name": row["name"],
            "assessed_at": row["assessed_at"],
            "verdict": row["verdict"],
            "playtime_minutes": row["playtime_minutes"],
            "target_price": row["target_price"],
        }

    mismatches = {
        "skip_but_acquired": _capped(
            [
                _entry(row)
                for row in rows
                if row["verdict"] == "skip"
                and not row["owned_at_assessment"]
                and row["owned_now"]
            ],
            limit,
        ),
        "buy_now_still_unplayed": _capped(
            [
                _entry(row)
                for row in rows
                if row["verdict"] == "buy_now"
                and row["owned_now"]
                and (row["playtime_minutes"] or 0) < CALIBRATION_PLAYED_MINUTES
            ],
            limit,
        ),
        "wishlist_still_waiting": _capped(
            [
                _entry(row)
                for row in rows
                if row["verdict"] == "wishlist_for_sale" and not row["owned_now"]
            ],
            limit,
        ),
    }

    # "Play what you own instead: X" — did he actually play X afterwards?
    # A NULL last_played is UNKNOWN (data/last_played.py's contract), never
    # "not played", so it counts toward neither side.
    instead_rows = [row for row in rows if row["instead_game_id"] is not None]
    followed_up = 0
    not_yet = 0
    unknown = 0
    for row in instead_rows:
        last_played = row["instead_last_played"]
        if last_played is None:
            unknown += 1
        elif last_played > row["assessed_at"][:10]:
            followed_up += 1
        else:
            not_yet += 1

    return {
        "overall": overall,
        "by_verdict": by_verdict,
        # Declared methodology (issue #153): which skill version / which model
        # produced the calls, and how those calls held up. Both blocks include
        # the unknown bucket with explicit nulls.
        "by_methodology": _capped(
            _group_by_provenance(rows, ("skill", "skill_version")), limit
        ),
        "by_model": _capped(_group_by_provenance(rows, ("model",)), limit),
        "wishlist_for_sale": wishlist_for_sale,
        "mismatches": mismatches,
        "play_what_you_own_follow_through": {
            "total": len(instead_rows),
            "followed_up_count": followed_up,
            "not_yet_count": not_yet,
            "unknown_count": unknown,
        },
    }
