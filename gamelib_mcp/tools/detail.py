"""get_game_detail: full info for one game, with platform-aware output."""

from fastmcp.exceptions import ToolError

from ..data.db import (
    fts_ready,
    get_db,
    get_game_by_appid,
    get_steam_appid_for_game,
    load_platforms_for_games,
    load_related_content_for_games,
    load_series_for_games,
)
from ..data.hltb import get_hltb
from ..data.protondb import get_protondb
from ..data.steam_store import enrich_game
from ..utils import _parse_json
from .common import cover_url
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

    Can resolve to a wishlist-only title (wishlisted but not owned anywhere) —
    check owned/wishlisted, not is_primary_library_item, which is a
    content-type flag (real game vs DLC/soundtrack/edition) and says nothing
    about ownership. A wishlist-only game reports platforms=[].
    """
    async with get_db() as db:
        if game_id is not None:
            row = await db.execute_fetchone("SELECT * FROM games WHERE id = ?", (game_id,))
        elif appid is not None:
            row = await get_game_by_appid(appid)
        elif name is not None:
            match = build_name_match(name, column=NORMALIZED_NAME_SQL, use_fts=fts_ready())
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
        wishlist_row = await db.execute_fetchone(
            "SELECT 1 FROM game_wishlist WHERE game_id = ? LIMIT 1", (game_id,)
        )

    platforms = (await load_platforms_for_games([game_id])).get(game_id, [])
    series = (await load_series_for_games([game_id])).get(game_id, [])
    related_content = (await load_related_content_for_games([game_id])).get(
        game_id,
        {"dlc": [], "expansions": [], "editions": [], "bundles": [], "other": []},
    )
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

    known_playtimes = [
        p["playtime_minutes"] for p in platforms if p["playtime_minutes"] is not None
    ]
    total_playtime_minutes = sum(known_playtimes) if known_playtimes else None
    total_playtime_2weeks_minutes = sum(p["playtime_2weeks_minutes"] or 0 for p in platforms)

    if row["completion_status"] == "completed":
        play_state = "played"
    elif bool(row["is_farmed"]):
        play_state = "unplayed"
    elif total_playtime_minutes is None:
        play_state = "unknown"
    elif total_playtime_minutes == 0:
        play_state = "unplayed"
    else:
        play_state = "played"

    result = {
        "game_id": row["id"],
        "appid": steam_appid,
        "steam_appid": steam_appid,
        "name": row["name"],
        "cover_url": cover_url(row["cover_image_id"], steam_appid),
        "release_date": row["release_date"],
        "series": series,
        "platforms": platforms,
        "playtime_hours": (
            None
            if play_state == "unknown"
            else round((total_playtime_minutes or 0) / 60, 1)
        ),
        "playtime_2weeks_hours": (
            round(total_playtime_2weeks_minutes / 60, 1)
            if total_playtime_2weeks_minutes
            else 0
        ),
        "last_played_date": max(
            (p["last_played_date"] for p in platforms if p.get("last_played_date")),
            default=None,
        ),
        "is_farmed": bool(row["is_farmed"]),
        "completion_status": row["completion_status"],
        "play_state": play_state,
        "content_type": row["content_type"],
        "parent_game_id": row["parent_game_id"],
        "is_primary_library_item": bool(row["is_primary_library_item"]),
        "owned": any(p["owned"] for p in platforms),
        "wishlisted": wishlist_row is not None,
        "related_content": related_content,
        "genres": _parse_json(row["genres"]),
        "tags": _parse_json(row["tags"]),
        "features": _parse_json(row["features"]),
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
        "manual_overrides": _parse_json(row["manual_overrides"]) or [],
    }

    if rating:
        result["my_rating"] = {
            "source": rating["source"],
            "raw_score": rating["raw_score"],
            "normalized_score": rating["normalized_score"],
            "review_text": rating["review_text"],
        }

    return result
