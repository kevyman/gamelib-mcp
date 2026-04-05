"""find_games_by_vibe and get_recommendations tools."""

from ..data.db import STEAM_APP_ID, get_db, load_platforms_for_games
from ..data.protondb import TIER_ORDER
from ..utils import _parse_json

# Vibe -> tag mappings (multi-tag = AND logic by default; tuple of lists = OR groups)
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

_GAME_ROLLUP_CTE = f"""
WITH game_rollup AS (
    SELECT g.id AS game_id,
           g.name,
           {_STEAM_APPID_SQL} AS steam_appid,
           g.tags,
           g.hltb_main,
           g.metacritic_score,
           g.is_farmed,
           COALESCE(SUM(COALESCE(gp.playtime_minutes, 0)), 0) AS total_playtime_minutes,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.protondb_tier END) AS protondb_tier,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.steam_review_desc END) AS steam_review_desc
    FROM games g
    LEFT JOIN game_platforms gp ON gp.game_id = g.id
    LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
    GROUP BY g.id
)
"""


async def find_games_by_vibe(
    vibe: str,
    max_hltb_hours: float | None = None,
    unplayed_only: bool = True,
    protondb_min_tier: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Find games matching a vibe (tag-based). Uses json_each() for proper JSON tag matching.
    vibe: one of the keys in VIBE_TAGS, or a raw tag string.
    """
    tags = VIBE_TAGS.get(vibe.lower(), [vibe.lower()])
    placeholders = ",".join("?" * len(tags))

    conditions = [
        f"""EXISTS (
            SELECT 1 FROM json_each(tags)
            WHERE lower(value) IN ({placeholders})
        )"""
    ]
    params: list = list(tags)

    if unplayed_only:
        conditions.append("(total_playtime_minutes = 0 OR is_farmed = 1)")

    if max_hltb_hours is not None:
        conditions.append("hltb_main <= ?")
        params.append(max_hltb_hours)

    if protondb_min_tier is not None:
        tier_lower = protondb_min_tier.lower()
        if tier_lower in TIER_ORDER:
            min_rank = TIER_ORDER.index(tier_lower)
            allowed = [tier for index, tier in enumerate(TIER_ORDER) if index <= min_rank]
            tier_ph = ",".join("?" * len(allowed))
            conditions.append(f"lower(COALESCE(protondb_tier, '')) IN ({tier_ph})")
            params.extend(allowed)

    where = " AND ".join(conditions)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT *
            FROM game_rollup
            WHERE {where}
            ORDER BY metacritic_score DESC NULLS LAST, name ASC
            LIMIT ?
            """,
            (*params, limit),
        )

    return await _format_rows(rows, include_match_score=False)


async def get_recommendations(
    max_hltb_hours: float | None = None,
    unplayed_only: bool = True,
    limit: int = 20,
) -> list[dict]:
    """
    Rank unplayed games by tag affinity score (from sync_ratings).
    Returns games sorted by how well they match your taste profile.
    Each result includes suggested_platform based on your hardware_preference setting.
    """
    import json as _json
    from ..data.db import get_meta

    hw_pref_raw = await get_meta("hardware_preference")
    hw_pref: list[str] = _json.loads(hw_pref_raw) if hw_pref_raw else []

    conditions = ["tags IS NOT NULL"]
    params: list = []

    if unplayed_only:
        conditions.append("(total_playtime_minutes = 0 OR is_farmed = 1)")

    if max_hltb_hours is not None:
        conditions.append("hltb_main <= ?")
        params.append(max_hltb_hours)

    where = " AND ".join(conditions)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT game_rollup.*,
                   AVG(ta.affinity_score) AS match_score
            FROM game_rollup
            JOIN json_each(game_rollup.tags) je ON 1 = 1
            JOIN tag_affinity ta ON ta.tag = lower(je.value)
            WHERE {where}
            GROUP BY game_rollup.game_id
            ORDER BY match_score DESC, name ASC
            LIMIT ?
            """,
            (*params, limit),
        )

    return await _format_rows(rows, include_match_score=True, hw_pref=hw_pref)


async def _format_rows(
    rows, include_match_score: bool, hw_pref: list[str] | None = None
) -> list[dict]:
    platforms_by_game = await load_platforms_for_games(row["game_id"] for row in rows)
    formatted = []
    for row in rows:
        owned_platforms = [p["platform"] for p in platforms_by_game.get(row["game_id"], []) if p["owned"]]
        game = {
            "game_id": row["game_id"],
            "appid": row["steam_appid"],
            "name": row["name"],
            "platforms": platforms_by_game.get(row["game_id"], []),
            "playtime_hours": round((row["total_playtime_minutes"] or 0) / 60, 1),
            "hltb_main": row["hltb_main"],
            "metacritic_score": row["metacritic_score"],
            "steam_review_desc": row["steam_review_desc"],
            "protondb_tier": row["protondb_tier"],
            "tags": _parse_json(row["tags"]),
        }
        if include_match_score:
            game["match_score"] = round(row["match_score"], 3) if row["match_score"] else 0
            pref = hw_pref or []
            game["suggested_platform"] = next(
                (hw for hw in pref if hw in owned_platforms),
                owned_platforms[0] if owned_platforms else None,
            )
        formatted.append(game)
    return formatted
