"""discover_games: unified vibe / taste / value discovery tool."""

import json
from typing import Literal

from fastmcp.exceptions import ToolError

from ..data.db import get_db, get_meta, load_platforms_for_games
from ..data.protondb import TIER_ORDER
from ..data.tag_synonyms import canonical_tag
from ..utils import _parse_json
from .common import STEAM_APPID_SQL as _STEAM_APPID_SQL, clamp_limit as _clamp_limit

ResponseFormat = Literal["concise", "detailed"]

# Vibe -> tag mappings. A game matches a vibe when it carries ANY tag in the
# group; multiple vibes combine with AND.
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
}

VALID_SORTS = {"match", "critic", "value"}

# NOTE: discover-specific CTE — no 2-week playtime column; tags drive vibe/affinity
# matching. Distinct from the library and stats variants on purpose.
_GAME_ROLLUP_CTE = f"""
WITH game_rollup AS (
    SELECT g.id AS game_id,
           g.name,
           {_STEAM_APPID_SQL} AS steam_appid,
           g.tags,
           g.hltb_main,
           g.is_farmed,
           g.is_primary_library_item,
           COALESCE(SUM(COALESCE(gp.playtime_minutes, 0)), 0) AS total_playtime_minutes,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.protondb_tier END) AS protondb_tier,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.steam_review_desc END) AS steam_review_desc,
           MAX(gpe.metacritic_score) AS metacritic_score,
           MAX(gpe.opencritic_score) AS opencritic_score
    FROM games g
    LEFT JOIN game_platforms gp ON gp.game_id = g.id
    LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
    LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
    WHERE g.is_primary_library_item = 1
    GROUP BY g.id
)
"""

# Correlated per-game taste score: average affinity over the game's tags.
_MATCH_SCORE_SQL = """
(
    SELECT AVG(ta.affinity_score)
    FROM json_each(COALESCE(game_rollup.tags, '[]')) je
    JOIN tag_affinity ta ON ta.tag = lower(je.value)
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
        # Match against the canonical vocabulary stored in games.tags.
        tags = [canonical_tag(t) for t in tags]
        placeholders = ",".join("?" * len(tags))
        inner_conditions.append(
            f"""EXISTS (
                SELECT 1 FROM json_each(COALESCE(tags, '[]'))
                WHERE lower(value) IN ({placeholders})
            )"""
        )
        params.extend(tags)

    if unplayed_only:
        inner_conditions.append("(total_playtime_minutes = 0 OR is_farmed = 1)")

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
            _GAME_ROLLUP_CTE
            + f"SELECT COUNT(*) AS c FROM ({scored_select}) WHERE {outer_where}",
            (*params, *outer_params),
        )
        rows = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
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
            "No taste profile yet — run sync_ratings (or rate games with "
            "rate_game) to compute tag affinity before match ranking works."
        )
    return envelope


async def _load_matched_tags(game_ids: list[int]) -> dict[int, list[dict]]:
    """Top-3 affinity tags per game — the 'why' behind each result."""
    if not game_ids:
        return {}
    placeholders = ",".join("?" * len(game_ids))
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT g.id AS game_id, lower(je.value) AS tag, ta.affinity_score
                FROM games g
                JOIN json_each(COALESCE(g.tags, '[]')) je
                JOIN tag_affinity ta ON ta.tag = lower(je.value)
                WHERE g.id IN ({placeholders})
                ORDER BY g.id, ta.affinity_score DESC""",
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
) -> list[dict]:
    platforms_by_game = await load_platforms_for_games(row["game_id"] for row in rows)
    formatted = []
    for row in rows:
        owned_platforms = [p["platform"] for p in platforms_by_game.get(row["game_id"], []) if p["owned"]]
        game = {
            "game_id": row["game_id"],
            "appid": row["steam_appid"],
            "name": row["name"],
            "playtime_hours": round((row["total_playtime_minutes"] or 0) / 60, 1),
            "hltb_main": row["hltb_main"],
            "metacritic_score": row["metacritic_score"],
            "opencritic_score": row["opencritic_score"],
            "steam_review_desc": row["steam_review_desc"],
            "protondb_tier": row["protondb_tier"],
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
    }
