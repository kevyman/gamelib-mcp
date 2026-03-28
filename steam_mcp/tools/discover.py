"""find_games_by_vibe and get_recommendations tools."""

from ..data.db import get_db
from ..data.protondb import TIER_ORDER
from ..utils import _parse_json

# Vibe → tag mappings (multi-tag = AND logic by default; tuple of lists = OR groups)
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
            SELECT 1 FROM json_each(g.tags)
            WHERE lower(value) IN ({placeholders})
        )"""
    ]
    params: list = list(tags)

    if unplayed_only:
        conditions.append("(COALESCE(gp.playtime_minutes, 0) = 0 OR g.is_farmed = 1)")

    if max_hltb_hours is not None:
        conditions.append("g.hltb_main <= ?")
        params.append(max_hltb_hours)

    if protondb_min_tier is not None:
        tier_lower = protondb_min_tier.lower()
        if tier_lower in TIER_ORDER:
            min_rank = TIER_ORDER.index(tier_lower)
            allowed = [t for i, t in enumerate(TIER_ORDER) if i <= min_rank]
            tier_ph = ",".join("?" * len(allowed))
            conditions.append(f"lower(g.protondb_tier) IN ({tier_ph})")
            params.extend(allowed)

    where = " AND ".join(conditions)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT g.appid, g.name, COALESCE(gp.playtime_minutes, 0) as playtime_forever,
                       g.hltb_main, g.metacritic_score,
                       g.protondb_tier, g.steam_review_desc, g.tags
                FROM games g
                LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
                WHERE {where}
                ORDER BY g.metacritic_score DESC NULLS LAST
                LIMIT ?""",
            (*params, limit),
        )

    return [
        {
            "appid": r["appid"],
            "name": r["name"],
            "playtime_hours": round(r["playtime_forever"] / 60, 1) if r["playtime_forever"] else 0,
            "hltb_main": r["hltb_main"],
            "metacritic_score": r["metacritic_score"],
            "protondb_tier": r["protondb_tier"],
            "steam_review_desc": r["steam_review_desc"],
            "tags": _parse_json(r["tags"]),
        }
        for r in rows
    ]


async def get_recommendations(
    max_hltb_hours: float | None = None,
    unplayed_only: bool = True,
    limit: int = 20,
) -> list[dict]:
    """
    Rank unplayed games by tag affinity score (from sync_ratings).
    Returns games sorted by how well they match your taste profile.
    """
    conditions = []
    params: list = []

    if unplayed_only:
        conditions.append("(COALESCE(gp.playtime_minutes, 0) = 0 OR g.is_farmed = 1)")

    conditions.append("g.tags IS NOT NULL")

    if max_hltb_hours is not None:
        conditions.append("g.hltb_main <= ?")
        params.append(max_hltb_hours)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT g.appid, g.name, COALESCE(gp.playtime_minutes, 0) as playtime_forever,
                       AVG(ta.affinity_score) as match_score,
                       g.hltb_main, g.metacritic_score,
                       g.steam_review_desc, g.protondb_tier, g.tags
                FROM games g
                LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
                JOIN json_each(g.tags) je ON 1=1
                JOIN tag_affinity ta ON ta.tag = lower(je.value)
                {where}
                GROUP BY g.appid
                ORDER BY match_score DESC
                LIMIT ?""",
            (*params, limit),
        )

    return [
        {
            "appid": r["appid"],
            "name": r["name"],
            "match_score": round(r["match_score"], 3) if r["match_score"] else 0,
            "hltb_main": r["hltb_main"],
            "metacritic_score": r["metacritic_score"],
            "steam_review_desc": r["steam_review_desc"],
            "protondb_tier": r["protondb_tier"],
            "tags": _parse_json(r["tags"]),
        }
        for r in rows
    ]


