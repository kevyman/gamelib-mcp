"""get_game_detail — full info for one game, triggers lazy fetches."""

from datetime import datetime, timezone

from ..data.db import get_db
from ..data.steam_store import enrich_game
from ..data.hltb import get_hltb
from ..data.opencritic import get_metacritic
from ..data.protondb import get_protondb
from ..utils import _parse_json


async def get_game_detail(name: str | None = None, appid: int | None = None) -> dict:
    """
    Return full detail for a game, triggering lazy enrichment.
    Accepts either name (substring) or appid.
    """
    async with get_db() as db:
        if appid is not None:
            row = await db.execute_fetchone("SELECT * FROM games WHERE appid = ?", (appid,))
        elif name is not None:
            row = await db.execute_fetchone(
                "SELECT * FROM games WHERE lower(name) LIKE lower(?) LIMIT 1",
                (f"%{name}%",),
            )
        else:
            return {"error": "Provide name or appid"}

    if row is None:
        return {"error": "Game not found in library"}

    game_appid = row["appid"]
    game_name = row["name"]

    # Trigger lazy fetches (each checks its own cache)
    await enrich_game(game_appid)
    hltb = await get_hltb(game_appid, game_name)
    await get_metacritic(game_appid)
    proton = await get_protondb(game_appid)

    # Re-fetch from DB after enrichment
    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT g.*, COALESCE(gp.playtime_minutes, 0) as playtime_forever,
                      COALESCE(gp.playtime_2weeks_minutes, 0) as playtime_2weeks
               FROM games g
               LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               WHERE g.appid = ?""",
            (game_appid,),
        )
        rating = await db.execute_fetchone(
            """SELECT source, raw_score, normalized_score, review_text
               FROM ratings WHERE game_id = (SELECT id FROM games WHERE appid = ?)
               ORDER BY source""",
            (game_appid,),
        )

    rtime = row["rtime_last_played"]
    last_played_date = (
        datetime.fromtimestamp(rtime, tz=timezone.utc).date().isoformat()
        if rtime
        else None
    )

    result = {
        "appid": row["appid"],
        "name": row["name"],
        "playtime_hours": round(row["playtime_forever"] / 60, 1) if row["playtime_forever"] else 0,
        "playtime_2weeks_hours": round(row["playtime_2weeks"] / 60, 1) if row["playtime_2weeks"] else 0,
        "last_played_date": last_played_date,
        "is_farmed": bool(row["is_farmed"]),
        "genres": _parse_json(row["genres"]),
        "tags": _parse_json(row["tags"]),
        "short_description": row["short_description"],
        "steam_review_score": row["steam_review_score"],
        "steam_review_desc": row["steam_review_desc"],
        "hltb_main": row["hltb_main"],
        "hltb_extra": row["hltb_extra"],
        "hltb_complete": row["hltb_complete"],
        "metacritic_score": row["metacritic_score"],
        "protondb_tier": row["protondb_tier"],
    }

    if rating:
        result["my_rating"] = {
            "source": rating["source"],
            "raw_score": rating["raw_score"],
            "normalized_score": rating["normalized_score"],
            "review_text": rating["review_text"],
        }

    return result


