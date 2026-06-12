"""get_game_detail: full info for one game, with platform-aware output."""

from fastmcp.exceptions import ToolError

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
from .search import (
    NORMALIZED_NAME_SQL,
    build_name_match,
    fuzzy_fallback_game_ids,
)


async def get_game_detail(
    name: str | None = None,
    appid: int | None = None,
    game_id: int | None = None,
) -> dict:
    """
    Return full detail for a game, triggering lazy enrichment.
    Accepts game_id, a Steam appid when available, or a partial name.
    """
    async with get_db() as db:
        if game_id is not None:
            row = await db.execute_fetchone("SELECT * FROM games WHERE id = ?", (game_id,))
        elif appid is not None:
            row = await get_game_by_appid(appid)
        elif name is not None:
            match = build_name_match(name, column=NORMALIZED_NAME_SQL)
            row = await db.execute_fetchone(
                f"""SELECT g.*, {match.rank_sql} AS match_rank
                    FROM games g
                    WHERE {match.where_sql}
                    ORDER BY match_rank ASC, length(g.name) ASC, g.id ASC
                    LIMIT 1""",
                (*match.rank_params, *match.where_params),
            )
        else:
            raise ToolError("Provide game_id, name, or appid")

    if row is None and name is not None:
        fuzzy_ids = await fuzzy_fallback_game_ids(name)
        if fuzzy_ids:
            async with get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT * FROM games WHERE id = ?", (fuzzy_ids[0],)
                )

    if row is None:
        raise ToolError("Game not found in library")

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

    # Best-of-platforms critic scores, hoisted so clients don't have to dig
    # through the platforms array (mirrors the MAX() rollup in list tools).
    best_metacritic = max(
        (p for p in platforms if p.get("metacritic_score") is not None),
        key=lambda p: p["metacritic_score"],
        default=None,
    )
    best_opencritic = max(
        (p for p in platforms if p.get("opencritic_score") is not None),
        key=lambda p: p["opencritic_score"],
        default=None,
    )

    total_playtime_minutes = sum(p["playtime_minutes"] or 0 for p in platforms)
    total_playtime_2weeks_minutes = sum(p["playtime_2weeks_minutes"] or 0 for p in platforms)

    result = {
        "game_id": row["id"],
        "appid": steam_appid,
        "steam_appid": steam_appid,
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
        "metacritic_score": best_metacritic["metacritic_score"] if best_metacritic else None,
        "metacritic_url": best_metacritic["metacritic_url"] if best_metacritic else None,
        "opencritic_score": best_opencritic["opencritic_score"] if best_opencritic else None,
        "opencritic_tier": best_opencritic["opencritic_tier"] if best_opencritic else None,
        "opencritic_percent_rec": (
            best_opencritic["opencritic_percent_rec"] if best_opencritic else None
        ),
        "opencritic_url": best_opencritic["opencritic_url"] if best_opencritic else None,
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
