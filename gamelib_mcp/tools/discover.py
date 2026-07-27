"""discover_games: unified vibe / taste / value discovery tool."""

import json
from typing import Literal

from fastmcp.exceptions import ToolError

from ..data.db import get_db, get_meta, load_platforms_for_games
from ..data.protondb import TIER_ORDER
from ..data.tag_synonyms import canonical_tag
from ..utils import _parse_json
from .common import (
    OWNED_SQL as _OWNED_SQL,
    STEAM_APPID_SQL as _STEAM_APPID_SQL,
    PLAY_STATE_SQL as _PLAY_STATE_SQL,
    PLAYTIME_SUM_SQL as _PLAYTIME_SUM_SQL,
    WISHLISTED_SQL as _WISHLISTED_SQL,
    clamp_limit as _clamp_limit,
    cover_url as _cover_url,
)

ResponseFormat = Literal["concise", "detailed"]

# Vibe -> tag mappings. A game matches a vibe when it carries ANY tag in the
# group within its most-prominent tags (see VIBE_TAG_PROMINENCE_CUTOFF);
# multiple vibes combine with AND.
VIBE_TAGS: dict[str, list[str]] = {
    "roguelike": ["roguelike", "rogue-lite", "roguelite", "roguelike deckbuilder", "deckbuilder", "deck building"],
    "cozy": ["cozy", "relaxing", "casual", "wholesome"],
    "horror": ["horror", "survival horror", "psychological horror", "cosmic horror"],
    "metroidvania": ["metroidvania"],
    "souls": ["souls-like", "soulslike", "souls like"],
    "open world": ["open world", "open-world"],
    "crafting": ["crafting", "base building", "building", "survival crafting"],
    "puzzle": ["puzzle", "logic"],
    "platformer": ["platformer", "2d platformer", "3d platformer", "precision platformer", "puzzle platformer"],
    "rpg": ["rpg", "role-playing", "jrpg", "action rpg", "turn-based rpg", "dungeon crawler"],
    "strategy": ["strategy", "turn-based strategy", "real-time strategy", "rts", "grand strategy", "4x", "tower defense", "turn-based tactics"],
    "simulation": ["simulation", "life sim", "farming sim", "city builder", "management", "colony sim"],
    "stealth": ["stealth"],
    "narrative": ["story rich", "narrative", "visual novel", "interactive fiction", "choices matter", "multiple endings"],
    "co-op": ["co-op", "cooperative", "multiplayer"],
    "shooter": ["shooter", "fps", "third-person shooter", "tactical shooter", "bullet hell", "shoot 'em up"],
    "survival": ["survival"],
    "indie": ["indie"],
    "cyberpunk": ["cyberpunk", "sci-fi", "futuristic"],
    "fantasy": ["fantasy", "dark fantasy", "high fantasy"],
    "card game": ["card game", "card battler", "deckbuilder", "roguelike deckbuilder"],
    "fighting": ["fighting", "beat 'em up", "brawler"],
    # Deliberately no "driving"/"automobile sim": those tags are prominent on
    # open-world crime games and truck sims, which are not racing games.
    "racing": ["racing", "arcade racing", "combat racing", "rally", "offroad", "motocross"],
    "sports": ["sports", "football", "soccer", "basketball", "golf", "skateboarding", "skating"],
}

VALID_SORTS = {"match", "critic", "value"}

# games.tags is prominence-ordered (SteamSpy tags sorted by community votes,
# IGDB tags appended after) — a vibe only matches a tag inside this prefix, so
# GTA V's low-vote "racing" tag (position 10) no longer makes it a racing
# recommendation while every genuine racer's (position 0-4) still does.
VIBE_TAG_PROMINENCE_CUTOFF = 8

# NOTE: discover-specific CTE — no 2-week playtime column; tags drive vibe/affinity
# matching. Distinct from the library and stats variants on purpose.
_GAME_ROLLUP_CTE = f"""
WITH game_rollup AS (
    SELECT g.id AS game_id,
           g.name,
           {_STEAM_APPID_SQL} AS steam_appid,
           g.cover_image_id,
           g.tags,
           g.hltb_main,
           g.is_farmed,
           g.completion_status,
           g.is_primary_library_item,
           {_OWNED_SQL} AS owned,
           {_WISHLISTED_SQL} AS wishlisted,
           {_PLAYTIME_SUM_SQL} AS total_playtime_minutes,
           {_PLAY_STATE_SQL} AS play_state,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.protondb_tier END) AS protondb_tier,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.steam_review_desc END) AS steam_review_desc,
           MAX(gpe.metacritic_score) AS metacritic_score,
           MAX(gpe.opencritic_score) AS opencritic_score
    FROM games g
    -- owned = 1: an owned=0 stub's playtime/enrichment must not feed the
    -- aggregates — e.g. 600 stub minutes would mark an otherwise-unplayed
    -- game 'played' and hide it from recommendations.
    LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
    LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
    LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
    WHERE g.is_primary_library_item = 1
      -- discover_games only ever recommends what's actually owned: a
      -- wishlist-only games row (games + game_wishlist, zero game_platforms
      -- rows) must never surface as a recommendation.
      AND {_OWNED_SQL}
    GROUP BY g.id
)
"""

# IDF weights: how rare each tag is across the owned library. A tag on every
# game ("action", "indie") carries little information about THIS game; a tag on
# three games carries a lot. Classic content-based TF-IDF weighting.
_SCORING_CTES = _GAME_ROLLUP_CTE + """,
lib_size AS (SELECT COUNT(*) AS n FROM game_rollup),
lib_tag_df AS (
    SELECT lower(je.value) AS tag, COUNT(DISTINCT gr.game_id) AS df
    FROM game_rollup gr, json_each(COALESCE(gr.tags, '[]')) je
    GROUP BY lower(je.value)
)
"""

# Shrinkage prior in the score denominator (in IDF units): a game with little
# tag evidence gets pulled toward neutral instead of inheriting its tags' full
# affinity. Sized against the IDF ceiling — a library-unique tag carries
# idf = ln(1 + N) ≈ 8 at N≈3000, so a prior of 3 let a game whose ONLY tag was
# one rare loved keyword ("dinosaurs") outrank every rich-profile match; 9
# caps that single-tag case at ~47% of the tag's affinity while barely moving
# games whose tag sets carry 30+ IDF units of evidence.
_MATCH_PRIOR = 9.0

# Affinity support damping (matches _SUPPORT_SHRINKAGE_K in tools/ratings.py):
# an affinity backed by 1-2 rated games is mostly noise — IGDB one-off
# keywords on a single 10/10 game ("cow", "go-kart") otherwise get BOTH the
# highest affinities (tiny samples hit the score ceiling) and the highest IDF
# (rare by construction), a compounding error that put single-keyword games at
# the top of every match ranking.
_SUPPORT_K = 2.0
_DAMPED_AFFINITY_SQL = f"ta.affinity_score * ta.game_count / (ta.game_count + {_SUPPORT_K})"

# Correlated per-game taste score: IDF-weighted mean support-damped affinity
# over ALL the game's tags (unrated tags contribute 0 = neutral), damped by
# _MATCH_PRIOR. NULL when no tag has an affinity row — the profile can't
# score the game.
_MATCH_SCORE_SQL = f"""
(
    SELECT CASE WHEN COUNT(ta.tag) = 0 THEN NULL
           ELSE SUM(COALESCE({_DAMPED_AFFINITY_SQL}, 0) * gl_ln(1.0 + ls.n * 1.0 / df.df))
                / (SUM(gl_ln(1.0 + ls.n * 1.0 / df.df)) + {_MATCH_PRIOR})
           END
    FROM json_each(COALESCE(game_rollup.tags, '[]')) je
    CROSS JOIN lib_size ls
    JOIN lib_tag_df df ON df.tag = lower(je.value)
    LEFT JOIN tag_affinity ta ON ta.tag = lower(je.value)
)
"""


async def discover_games(
    vibes: list[str] | None = None,
    sort_by: str = "match",
    max_hltb_hours: float | None = None,
    min_score: int | None = None,
    unplayed_only: bool = True,
    protondb_min_tier: str | None = None,
    limit: int = 20,
    offset: int = 0,
    response_format: ResponseFormat = "concise",
) -> dict:
    """
    Discover games to play: by vibe tags, taste profile, critic score, or value.

    vibes: list of VIBE_TAGS keys or raw tag strings; multiple vibes AND
    together. Omit for pure taste-profile recommendations.
    sort_by: match (taste affinity) | critic (OpenCritic/Metacritic) |
    value (high critic score per HLTB hour — backlog hidden gems).
    min_score: floor on COALESCE(opencritic, metacritic); excludes unscored games.
    Games marked completed or abandoned (via update_game) are never recommended,
    and only actually-owned games are ever recommended — a wishlist-only title
    (wishlisted but not owned anywhere) never appears here.
    """
    limit = _clamp_limit(limit)
    if sort_by not in VALID_SORTS:
        raise ToolError(f"Unknown sort_by '{sort_by}'. Valid: {sorted(VALID_SORTS)}")

    inner_conditions: list[str] = []
    params: list = []

    unknown_vibes: list[str] = []
    for vibe in vibes or []:
        tags = VIBE_TAGS.get(vibe.lower())
        if tags is None:
            tags = [vibe.lower()]
            unknown_vibes.append(vibe)
        # Match against the canonical vocabulary stored in games.tags. The
        # `key <` prominence gate keeps a barely-there tag deep in the list
        # from qualifying the game for the vibe.
        tags = [canonical_tag(t) for t in tags]
        placeholders = ",".join("?" * len(tags))
        inner_conditions.append(
            f"""EXISTS (
                SELECT 1 FROM json_each(COALESCE(tags, '[]'))
                WHERE lower(value) IN ({placeholders})
                  AND key < {VIBE_TAG_PROMINENCE_CUTOFF}
            )"""
        )
        params.extend(tags)

    if unplayed_only:
        inner_conditions.append("play_state IN ('unplayed', 'unknown')")
    # A completed/abandoned game should never surface as a recommendation, even
    # when unplayed_only=False (e.g. sort_by=critic browsing everything).
    inner_conditions.append(
        "(completion_status IS NULL OR completion_status NOT IN ('completed', 'abandoned'))"
    )

    if max_hltb_hours is not None:
        inner_conditions.append("hltb_main <= ?")
        params.append(max_hltb_hours)

    if protondb_min_tier is not None:
        tier_lower = protondb_min_tier.lower()
        if tier_lower not in TIER_ORDER:
            raise ToolError(
                f"Unknown protondb_min_tier '{protondb_min_tier}'. Valid: {list(TIER_ORDER)}"
            )
        min_rank = TIER_ORDER.index(tier_lower)
        allowed = [tier for index, tier in enumerate(TIER_ORDER) if index <= min_rank]
        tier_ph = ",".join("?" * len(allowed))
        inner_conditions.append(f"lower(COALESCE(protondb_tier, '')) IN ({tier_ph})")
        params.extend(allowed)

    outer_conditions: list[str] = []
    outer_params: list = []
    if vibes is None and sort_by == "match":
        # Pure recommendations: only games the taste profile can score.
        outer_conditions.append("match_score IS NOT NULL")
    if min_score is not None:
        outer_conditions.append("critic_score >= ?")
        outer_params.append(min_score)
    if sort_by == "value":
        outer_conditions.append("critic_score IS NOT NULL")
        outer_conditions.append("hltb_main IS NOT NULL")

    inner_where = " AND ".join(inner_conditions) if inner_conditions else "1=1"
    outer_where = " AND ".join(outer_conditions) if outer_conditions else "1=1"
    order = {
        "match": "match_score DESC NULLS LAST, critic_score DESC NULLS LAST, name ASC",
        "critic": "critic_score DESC NULLS LAST, name ASC",
        "value": "critic_score DESC, hltb_main ASC, name ASC",
    }[sort_by]

    scored_select = f"""
        SELECT game_rollup.*,
               COALESCE(opencritic_score, metacritic_score) AS critic_score,
               {_MATCH_SCORE_SQL} AS match_score
        FROM game_rollup
        WHERE {inner_where}
    """

    async with get_db() as db:
        total = await db.execute_fetchone(
            _SCORING_CTES
            + f"SELECT COUNT(*) AS c FROM ({scored_select}) WHERE {outer_where}",
            (*params, *outer_params),
        )
        rows = await db.execute_fetchall(
            _SCORING_CTES
            + f"""
            SELECT * FROM ({scored_select})
            WHERE {outer_where}
            ORDER BY {order}
            LIMIT ?
            OFFSET ?
            """,
            (*params, *outer_params, limit, offset),
        )
        affinity_row = await db.execute_fetchone("SELECT COUNT(*) AS c FROM tag_affinity")
        # Library-wide best match score: the anchor that turns raw match scores
        # into an honest percentage — 100% = the strongest match in the whole
        # owned library, stable across vibe filters and pagination.
        max_match_row = await db.execute_fetchone(
            _SCORING_CTES + f"SELECT MAX({_MATCH_SCORE_SQL}) AS m FROM game_rollup"
        )
        max_match = max_match_row["m"] if max_match_row else None

    hw_pref_raw = await get_meta("hardware_preference")
    hw_pref: list[str] = json.loads(hw_pref_raw) if hw_pref_raw else []
    matched_tags = await _load_matched_tags([row["game_id"] for row in rows])

    envelope = _envelope(
        await _format_rows(
            rows,
            hw_pref=hw_pref,
            matched_tags=matched_tags,
            include_value_note=sort_by == "value",
            response_format=response_format,
            max_match=max_match,
        ),
        total["c"],
        limit,
        offset,
    )
    if total["c"] == 0 and unknown_vibes:
        unknown = "', '".join(unknown_vibes)
        envelope["note"] = (
            f"'{unknown}' is not a known vibe and matched no tags directly. "
            f"Known vibes: {sorted(VIBE_TAGS)}"
        )
    elif vibes is None and sort_by == "match" and affinity_row["c"] == 0:
        envelope["note"] = (
            "No taste profile yet — run sync(targets=[\"ratings\"]) (or rate games with "
            "rate_game) to compute tag affinity before match ranking works."
        )
    return envelope


async def _load_matched_tags(game_ids: list[int]) -> dict[int, list[dict]]:
    """Top-3 contributing tags per game — the 'why' behind each result.

    Ordered by support-damped affinity x IDF (the same weighting the match
    score uses), not raw affinity — otherwise ubiquitous mildly-liked tags
    ("indie", storefront keywords) crowd out the rare tags that actually
    drove the ranking.
    """
    if not game_ids:
        return {}
    placeholders = ",".join("?" * len(game_ids))
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""WITH owned AS (
                    SELECT g2.id AS game_id, g2.tags
                    FROM games g2
                    WHERE g2.is_primary_library_item = 1
                      AND EXISTS (SELECT 1 FROM game_platforms gp
                                  WHERE gp.game_id = g2.id AND gp.owned = 1)
                ),
                lib_size AS (SELECT COUNT(*) AS n FROM owned),
                tag_df AS (
                    SELECT lower(je.value) AS tag, COUNT(DISTINCT owned.game_id) AS df
                    FROM owned, json_each(COALESCE(owned.tags, '[]')) je
                    GROUP BY lower(je.value)
                )
                SELECT g.id AS game_id, lower(je.value) AS tag, ta.affinity_score
                FROM games g
                JOIN json_each(COALESCE(g.tags, '[]')) je
                JOIN tag_affinity ta ON ta.tag = lower(je.value)
                JOIN tag_df df ON df.tag = lower(je.value)
                CROSS JOIN lib_size ls
                WHERE g.id IN ({placeholders})
                ORDER BY g.id,
                         {_DAMPED_AFFINITY_SQL} * gl_ln(1.0 + ls.n * 1.0 / df.df) DESC""",
            tuple(game_ids),
        )
    matched: dict[int, list[dict]] = {}
    for row in rows:
        entries = matched.setdefault(row["game_id"], [])
        if len(entries) < 3:
            entries.append(
                {"tag": row["tag"], "affinity_score": round(row["affinity_score"], 3)}
            )
    return matched


async def _format_rows(
    rows,
    hw_pref: list[str] | None = None,
    matched_tags: dict[int, list[dict]] | None = None,
    include_value_note: bool = False,
    response_format: ResponseFormat = "detailed",
    max_match: float | None = None,
) -> list[dict]:
    platforms_by_game = await load_platforms_for_games(row["game_id"] for row in rows)
    formatted = []
    for row in rows:
        owned_platforms = [p["platform"] for p in platforms_by_game.get(row["game_id"], []) if p["owned"]]
        game = {
            "game_id": row["game_id"],
            "appid": row["steam_appid"],
            "name": row["name"],
            "cover_url": _cover_url(row["cover_image_id"], row["steam_appid"]),
            "play_state": row["play_state"],
            "playtime_hours": (
                None
                if row["play_state"] == "unknown"
                else round((row["total_playtime_minutes"] or 0) / 60, 1)
            ),
            "hltb_main": row["hltb_main"],
            "metacritic_score": row["metacritic_score"],
            "opencritic_score": row["opencritic_score"],
            "steam_review_desc": row["steam_review_desc"],
            "protondb_tier": row["protondb_tier"],
            "owned": bool(row["owned"]),
            "wishlisted": bool(row["wishlisted"]),
        }
        if response_format == "detailed":
            game["platforms"] = platforms_by_game.get(row["game_id"], [])
            game["tags"] = _parse_json(row["tags"])
        pref = hw_pref or []
        game["suggested_platform"] = next(
            (hw for hw in pref if hw in owned_platforms),
            owned_platforms[0] if owned_platforms else None,
        )
        if row["match_score"] is not None:
            game["match_score"] = round(row["match_score"], 3)
            # Affinity is mean-centered, so scores (and the anchor) can be
            # negative; a percentage only makes sense against a positive best.
            if max_match and max_match > 0:
                game["match_percent"] = max(
                    0, min(100, round(100 * row["match_score"] / max_match))
                )
        game_matches = (matched_tags or {}).get(row["game_id"])
        if game_matches:
            game["matched_tags"] = game_matches
        if include_value_note:
            source = "OpenCritic" if row["opencritic_score"] is not None else "Metacritic"
            game["value_note"] = f"{row['critic_score']} on {source}, ~{round(row['hltb_main'])}h"
        formatted.append(game)
    return formatted


def _envelope(results: list[dict], total_matches: int, limit: int, offset: int) -> dict:
    return {
        "results": results,
        "total_matches": total_matches,
        "has_more": offset + len(results) < total_matches,
        # Results are always rank-ordered (match/critic/value); offset lets the
        # game-cards app number them globally, so page two starts at № 21.
        "offset": offset,
    }
