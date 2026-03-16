"""search_games and get_library_stats tools."""

import json

import aiosqlite

from ..data.db import get_db

SORT_COLUMNS = {
    "playtime": "playtime_forever",
    "name": "name",
    "metacritic": "metacritic_score",
    "hltb": "hltb_main",
}


async def search_games(query: str, limit: int = 20) -> list[dict]:
    """Find games in library by name substring match."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT appid, name, playtime_forever, playtime_2weeks,
                      hltb_main, metacritic_score,
                      protondb_tier, steam_review_desc, is_farmed
               FROM games
               WHERE lower(name) LIKE lower(?)
               ORDER BY playtime_forever DESC
               LIMIT ?""",
            (f"%{query}%", limit),
        )
    return [_format_game(r) for r in rows]


async def search_games_batch(queries: list[str], limit_per_query: int = 5) -> dict[str, list[dict]]:
    """Look up multiple game names in one call. Returns dict keyed by query."""
    async with get_db() as db:
        results = {}
        for query in queries:
            rows = await db.execute_fetchall(
                """SELECT appid, name, playtime_forever, playtime_2weeks,
                          hltb_main, metacritic_score, protondb_tier,
                          steam_review_desc, is_farmed
                   FROM games
                   WHERE lower(name) LIKE lower(?)
                   ORDER BY playtime_forever DESC
                   LIMIT ?""",
                (f"%{query}%", limit_per_query),
            )
            results[query] = [_format_game(r) for r in rows]
    return results


async def get_library_stats(
    filter: str = "all",
    max_hltb_hours: float | None = None,
    min_metacritic: int | None = None,
    protondb_tier: str | None = None,
    sort_by: str = "playtime",
    limit: int = 50,
) -> dict:
    """
    Return filtered/sorted game list + aggregate stats.

    filter: all | unplayed | played | recent
    sort_by: playtime | name | metacritic | hltb
    """
    conditions = []
    params: list = []

    if filter == "unplayed":
        conditions.append("(playtime_forever = 0 OR is_farmed = 1)")
    elif filter == "played":
        conditions.append("(playtime_forever > 0 AND is_farmed = 0)")
    elif filter == "recent":
        conditions.append("playtime_2weeks > 0")
    elif filter == "farmed":
        conditions.append("is_farmed = 1")

    if max_hltb_hours is not None:
        conditions.append("hltb_main <= ?")
        params.append(max_hltb_hours)

    if min_metacritic is not None:
        conditions.append("metacritic_score >= ?")
        params.append(min_metacritic)

    if protondb_tier is not None:
        # Match tier or better using ordered list
        from ..data.protondb import TIER_ORDER
        min_rank = TIER_ORDER.index(protondb_tier.lower()) if protondb_tier.lower() in TIER_ORDER else 999
        allowed = [t for i, t in enumerate(TIER_ORDER) if i <= min_rank]
        placeholders = ",".join("?" * len(allowed))
        conditions.append(f"lower(protondb_tier) IN ({placeholders})")
        params.extend(allowed)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sort_col = SORT_COLUMNS.get(sort_by, "playtime_forever")
    sort_dir = "ASC" if sort_by == "name" else "DESC"

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT appid, name, playtime_forever, playtime_2weeks,
                       hltb_main, metacritic_score,
                       protondb_tier, steam_review_desc, is_farmed
                FROM games
                {where}
                ORDER BY {sort_col} {sort_dir} NULLS LAST
                LIMIT ?""",
            (*params, limit),
        )

        total = await db.execute_fetchone("SELECT COUNT(*) as c FROM games")
        played = await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM games WHERE playtime_forever > 0 AND is_farmed = 0"
        )
        unplayed = await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM games WHERE playtime_forever = 0 OR is_farmed = 1"
        )
        farmed = await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM games WHERE is_farmed = 1"
        )
        total_minutes = await db.execute_fetchone(
            "SELECT SUM(playtime_forever) as s FROM games"
        )

    stats = {
        "total_games": total["c"],
        "played": played["c"],
        "unplayed": unplayed["c"],
        "farmed_games": farmed["c"],
        "total_playtime_hours": round((total_minutes["s"] or 0) / 60, 1),
        "filter": filter,
        "sort_by": sort_by,
        "results": [_format_game(r) for r in rows],
    }
    return stats


def _format_game(row: aiosqlite.Row) -> dict:
    return {
        "appid": row["appid"],
        "name": row["name"],
        "playtime_hours": round(row["playtime_forever"] / 60, 1) if row["playtime_forever"] else 0,
        "playtime_2weeks_hours": round(row["playtime_2weeks"] / 60, 1) if row["playtime_2weeks"] else 0,
        "hltb_main": row["hltb_main"],
        "metacritic_score": row["metacritic_score"],
        "protondb_tier": row["protondb_tier"],
        "steam_review_desc": row["steam_review_desc"],
        "is_farmed": bool(row["is_farmed"]),
    }
