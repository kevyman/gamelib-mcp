"""get_game_detail: full info for one game, with platform-aware output."""

import asyncio
import json
import logging

from fastmcp.exceptions import ToolError

from ..data.content import NESTED_CONTENT_TYPES
from ..data.db import (
    fts_ready,
    get_db,
    get_game_by_appid,
    get_meta,
    get_steam_appid_for_game,
    load_platforms_for_games,
    load_recent_assessments,
    load_related_content_for_games,
    load_series_for_games,
)
from ..data.hltb import get_hltb
from ..data.igdb import get_igdb_children_cached
from ..data.protondb import get_protondb
from ..data.steam_store import enrich_game
from ..utils import _parse_json
from .batch import (
    DETAIL_BATCH_ITEM_CAP,
    apply_batch_item,
    check_batch_items,
    count_status,
)
from .common import cover_url
from .game_media import game_media_context
from .search import (
    NORMALIZED_NAME_SQL,
    build_name_match,
    fuzzy_fallback_game_ids,
)

logger = logging.getLogger(__name__)

# Recorded assessments are capped here rather than in tools/assessment.py:
# that module imports this one, so the constant lives on the importing
# side of the edge.
DETAIL_ASSESSMENT_CAP = 5

# The media lookup is the only provider call in this function that isn't a
# cached enrichment fetch, and it is decoration: the same budget the evaluation
# package gives it, after which the detail answer ships without it.
DETAIL_MEDIA_TIMEOUT_SECONDS = 8


async def get_game_detail(
    name: str | None = None,
    appid: int | None = None,
    game_id: int | None = None,
    *,
    enrich: bool = True,
    media: bool = False,
) -> dict:
    """
    Return full detail for a game, triggering lazy enrichment.
    Accepts game_id, a Steam appid when available, or a partial name.

    enrich=False (internal, batch-only) skips every lazy provider fetch (Steam
    store/reviews, ProtonDB, HLTB, and the IGDB children cache-miss fetch) and
    serves whatever is already cached — including a warm IGDB children catalog
    — so enrichment fields may be null/absent that a single-item call would
    have filled.

    media=True adds the neutral game representation (tools/game_media.py) as
    optional `media` and `similar` keys — trailer, screenshots, and IGDB's
    similar games annotated with what the library owns. Off by default: it is
    card decoration, costs a provider round trip on a cache miss, and nothing
    in the response depends on it. Single mode only (the bulk path never asks
    for it), and both keys are simply ABSENT when nothing resolved or the
    lookup failed — never null placeholders, and never a failed call.

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

    if enrich:
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

    # Nested rows (DLC/expansion/edition/bundle) get a `parent` back-pointer so
    # a client landing on a nested row can jump to its base game. Mirrors the
    # is_primary_library_item/content_type nesting test used elsewhere
    # (data/content.py::NESTED_CONTENT_TYPES) rather than trusting either
    # signal alone. Omitted entirely (not null) for primary rows and for
    # parentless nested rows — the widget/response convention here is to
    # leave optional keys out rather than send null placeholders.
    is_nested = (not bool(row["is_primary_library_item"])) or (
        row["content_type"] in NESTED_CONTENT_TYPES
    )
    parent_info = None
    if is_nested and row["parent_game_id"] is not None:
        async with get_db() as db:
            parent_row = await db.execute_fetchone(
                "SELECT id, name FROM games WHERE id = ?", (row["parent_game_id"],)
            )
        if parent_row is not None:
            parent_info = {"game_id": parent_row["id"], "name": parent_row["name"]}

    # dlc_ownership: for a primary/base Steam game, compare the Steam DLC
    # catalog cached at enrichment time (steam_dlc_catalog:{appid} meta key —
    # never live-fetched here) against how many of this game's nested
    # children the library actually owns. `known` is Steam's catalog size;
    # `owned` counts owned children across ALL platforms (a Switch expansion
    # of a Steam base game still counts as owned) — so `owned` can exceed a
    # naive appid-by-appid match against `known`. That asymmetry is
    # intentional: ownership is ownership, and this stays a simple, honest
    # signal rather than a precise appid reconciliation. Key is omitted
    # entirely when there's no cached catalog (never enriched, or not a
    # Steam base game with DLC).
    dlc_ownership = None
    if bool(row["is_primary_library_item"]) and steam_appid is not None:
        raw_catalog = await get_meta(f"steam_dlc_catalog:{steam_appid}")
        if raw_catalog:
            try:
                catalog = json.loads(raw_catalog)
                catalog_appids = catalog["appids"]
            except (ValueError, TypeError, KeyError):
                catalog_appids = None
            if catalog_appids is not None:
                owned_children = sum(
                    1
                    for group in related_content.values()
                    for child in group
                    if child.get("owned")
                )
                dlc_ownership = {
                    "owned": owned_children,
                    "known": len(catalog_appids),
                    "source": "steam",
                }

    # Fallback catalog source for primary games without a Steam DLC catalog
    # (e.g. Switch-only titles): IGDB's dlcs/expansions child arrays, lazily
    # fetched at most once per 7 days per game via a meta-KV cache
    # (get_igdb_children_cached, data/igdb.py). A fetch failure there returns
    # None/serves stale and never raises, so a slow or down IGDB can only
    # ever leave this key absent, never break the response.
    if (
        dlc_ownership is None
        and bool(row["is_primary_library_item"])
        and row["igdb_id"] is not None
    ):
        # allow_fetch=enrich: batch detail still serves an already-cached IGDB
        # children catalog, it just never live-fetches on a cache miss.
        children = await get_igdb_children_cached(row["igdb_id"], allow_fetch=enrich)
        if children:
            owned_children = sum(
                1
                for group in related_content.values()
                for child in group
                if child.get("owned")
            )
            dlc_ownership = {
                "owned": owned_children,
                "known": len(children),
                "source": "igdb",
            }

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

    if parent_info is not None:
        result["parent"] = parent_info

    if dlc_ownership is not None:
        result["dlc_ownership"] = dlc_ownership

    # Past verdicts recorded by record_assessment (ADR 0006 decision 5) —
    # read-only context, never an input to any scoring path. Single mode only:
    # `enrich` is this module's established single/bulk discriminator, and bulk
    # detail deliberately skips verbose per-game blocks. Bounded like every
    # other growing list — newest 5, with the true total and a truncation flag.
    if enrich:
        assessments, assessment_count = await load_recent_assessments(
            row["id"], DETAIL_ASSESSMENT_CAP
        )
        if assessments:
            result["assessments"] = assessments
            result["assessment_count"] = assessment_count
            result["assessments_truncated"] = assessment_count > len(assessments)

    # Trailer / screenshots / similar-games-you-own, for a client rendering a
    # card. Bounded and best-effort in both directions: the lookup can't hold
    # the response open past its budget, and a provider failure costs the two
    # keys, not the call.
    if media:
        try:
            context = await asyncio.wait_for(
                game_media_context(
                    steam_appid=steam_appid,
                    igdb_id=row["igdb_id"],
                    name=row["name"],
                ),
                timeout=DETAIL_MEDIA_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning(
                "Media lookup failed for game %s; serving detail without it",
                game_id,
                exc_info=True,
            )
        else:
            for key in ("media", "similar"):
                if context[key] is not None:
                    result[key] = context[key]

    return result


_DETAIL_BATCH_ITEM_KEYS = frozenset({"name", "appid", "game_id"})


async def get_game_details_batch(items: list[dict]) -> dict:
    """
    Full detail for many games in one call (max 50 items), enrichment skipped.

    Each item takes exactly get_game_detail's resolution parameters ({name,
    appid, or game_id}). Unlike the single-item tool, NO lazy provider fetches
    run (like the other bulk tools) — only already-cached enrichment is served,
    so enrichment fields a single-item call would fill may be null/absent; the
    response says so via enrichment="skipped". An unresolvable item is that
    item's status="error"; it never fails the batch.
    """
    check_batch_items(items, cap=DETAIL_BATCH_ITEM_CAP)

    async def _one(**kwargs):
        return await get_game_detail(**kwargs, enrich=False)

    results = [
        await apply_batch_item(item, _DETAIL_BATCH_ITEM_KEYS, _one) for item in items
    ]
    return {
        "results": results,
        "total": len(items),
        "ok": count_status(results, "ok"),
        "errors": count_status(results, "error"),
        "enrichment": "skipped",
    }
