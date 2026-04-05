"""get_game_detail: full info for one game, with platform-aware output."""

from ..data.db import (
    get_db,
    get_game_by_appid,
    get_steam_appid_for_game,
    load_platforms_for_games,
)
from ..data.hltb import get_hltb
from ..data.protondb import get_protondb
from ..data.steam_store import enrich_game
from ..utils import _parse_json


async def get_game_detail(
    name: str | None = None,
    appid: int | None = None,
    game_id: int | None = None,
) -> dict:
    """
    Return full detail for a game, triggering lazy enrichment.
    Accepts game_id, Steam appid, or a partial name.
    """
    async with get_db() as db:
        if game_id is not None:
            row = await db.execute_fetchone("SELECT * FROM games WHERE id = ?", (game_id,))
        elif appid is not None:
            row = await get_game_by_appid(appid)
        elif name is not None:
            row = await db.execute_fetchone(
                "SELECT * FROM games WHERE lower(name) LIKE lower(?) LIMIT 1",
                (f"%{name}%",),
            )
        else:
            return {"error": "Provide game_id, name, or appid"}

    if row is None:
        return {"error": "Game not found in library"}

    game_id = row["id"]
    game_name = row["name"]
    steam_appid = await get_steam_appid_for_game(game_id)

    if steam_appid is not None:
        await enrich_game(steam_appid)
        await get_protondb(steam_appid)
    await get_hltb(game_id, game_name)

    async with get_db() as db:
        row = await db.execute_fetchone("SELECT * FROM games WHERE id = ?", (game_id,))
        rating = await db.execute_fetchone(
            """SELECT source, raw_score, normalized_score, review_text
               FROM ratings
               WHERE game_id = ?
               ORDER BY source
               LIMIT 1""",
            (game_id,),
        )

    platforms = (await load_platforms_for_games([game_id])).get(game_id, [])
    steam_platform = next((p for p in platforms if p["platform"] == "steam"), None)
    steam_data = steam_platform["provider_data"] if steam_platform else {}

    total_playtime_minutes = sum(p["playtime_minutes"] or 0 for p in platforms)
    total_playtime_2weeks_minutes = sum(p["playtime_2weeks_minutes"] or 0 for p in platforms)

    result = {
        "game_id": row["id"],
        "appid": steam_appid,
        "name": row["name"],
        "release_date": row["release_date"],
        "platforms": platforms,
        "playtime_hours": round(total_playtime_minutes / 60, 1) if total_playtime_minutes else 0,
        "playtime_2weeks_hours": (
            round(total_playtime_2weeks_minutes / 60, 1)
            if total_playtime_2weeks_minutes
            else 0
        ),
        "last_played_date": steam_data.get("last_played_date"),
        "is_farmed": bool(row["is_farmed"]),
        "genres": _parse_json(row["genres"]),
        "tags": _parse_json(row["tags"]),
        "short_description": row["short_description"],
        "steam_review_score": steam_data.get("steam_review_score"),
        "steam_review_desc": steam_data.get("steam_review_desc"),
        "hltb_main": row["hltb_main"],
        "hltb_extra": row["hltb_extra"],
        "hltb_complete": row["hltb_complete"],
        "protondb_tier": steam_data.get("protondb_tier"),
    }

    if rating:
        result["my_rating"] = {
            "source": rating["source"],
            "raw_score": rating["raw_score"],
            "normalized_score": rating["normalized_score"],
            "review_text": rating["review_text"],
        }

    return result
