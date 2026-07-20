"""Meta KV store, game lookups, and platform assembly for read paths."""

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

import aiosqlite

from . import (
    GOG_PRODUCT_ID,
    NINTENDO_TITLE_ID_TYPE,
    STEAM_APP_ID,
    STEAM_PLATFORM,
    get_db,
    normalize_identifier_value,
)


async def get_meta(key: str) -> str | None:
    async with get_db() as db:
        row = await db.execute_fetchone("SELECT value FROM meta WHERE key = ?", (key,))
    return row["value"] if row else None


async def get_meta_prefix(prefix: str) -> dict[str, str]:
    """Return all meta rows whose key starts with prefix as {key: value}."""
    async with get_db() as db:
        async with db.execute(
            "SELECT key, value FROM meta WHERE key LIKE ?", (f"{prefix}%",)
        ) as cursor:
            rows = await cursor.fetchall()
    return {row["key"]: row["value"] for row in rows if row["value"] is not None}


# Sentinel device_id for user-entered pre-tracking playtime (see
# set_switch2_playtime_baseline). Real Parental Controls syncs upsert only
# their own real device ids, so a baseline row is never overwritten by a
# sync; it participates in the SUM totals but is excluded from last_played
# (its period_key is a sentinel date, not a day anyone played).
NINTENDO_BASELINE_DEVICE_ID = "manual-baseline"
# Dated before any possible real daily summary, so window queries
# (get_play_history) never pick the baseline up as in-window playtime.
NINTENDO_BASELINE_PERIOD_KEY = "1970-01-01"


async def get_nintendo_play_totals(period_type: str = "day") -> dict[str, dict]:
    """Aggregate Parental Controls playtime per application_id across all devices.

    Returns ``{application_id: {"minutes", "minutes_2weeks", "last_played",
    "app_name"}}``. ``minutes`` is the running total since Parental Controls
    tracking began (plus any manual pre-tracking baseline row);
    ``minutes_2weeks`` sums the trailing 14-day window; and ``last_played`` is
    the most recent day (ISO ``YYYY-MM-DD``) with recorded playtime — derived
    the same way Steam exposes its own 2-week / last-played signals, so the
    switch2 platform can fill those columns too. ``period_type='day'`` is the
    source of truth (finalized daily summaries).
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT application_id,
                      SUM(playtime_minutes) AS minutes,
                      SUM(CASE WHEN period_key >= date('now', '-13 days')
                               THEN playtime_minutes ELSE 0 END) AS minutes_2weeks,
                      MAX(CASE WHEN playtime_minutes > 0 AND device_id != ?
                               THEN period_key END) AS last_played,
                      MAX(app_name) AS app_name
               FROM nintendo_play_summary
               WHERE period_type = ?
               GROUP BY application_id""",
            (NINTENDO_BASELINE_DEVICE_ID, period_type),
        )
    return {
        row["application_id"]: {
            "minutes": int(row["minutes"] or 0),
            "minutes_2weeks": int(row["minutes_2weeks"] or 0),
            "last_played": row["last_played"],
            "app_name": row["app_name"],
        }
        for row in rows
    }


async def get_nintendo_synced_minutes(application_id: str) -> int:
    """Device-reported daily minutes for one application_id, baseline excluded.

    application_id is normalized (uppercase) the same way it's stored at
    ingest (see normalize_identifier_value), so this is a plain equality
    match — nintendo_play_summary.application_id is never written any other
    way (upsert_nintendo_play_summary is the only writer).
    """
    application_id = normalize_identifier_value(NINTENDO_TITLE_ID_TYPE, application_id)
    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT COALESCE(SUM(playtime_minutes), 0) AS minutes
               FROM nintendo_play_summary
               WHERE application_id = ? AND period_type = 'day'
                 AND device_id != ?""",
            (application_id, NINTENDO_BASELINE_DEVICE_ID),
        )
    return int(row["minutes"]) if row else 0


async def get_nintendo_baseline_minutes(application_id: str) -> int | None:
    """The manual pre-tracking baseline minutes for one application_id, if any.

    Normalized like get_nintendo_synced_minutes — plain equality, no UPPER()
    duct tape, since every row's application_id is already uppercase at write
    time.
    """
    application_id = normalize_identifier_value(NINTENDO_TITLE_ID_TYPE, application_id)
    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT SUM(playtime_minutes) AS minutes FROM nintendo_play_summary
               WHERE application_id = ? AND period_type = 'day'
                 AND device_id = ? AND period_key = ?""",
            (application_id, NINTENDO_BASELINE_DEVICE_ID, NINTENDO_BASELINE_PERIOD_KEY),
        )
    return int(row["minutes"]) if row and row["minutes"] is not None else None


async def set_meta(key: str, value: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def set_meta_many(values: dict[str, str | None]) -> None:
    if not values:
        return

    async with get_db() as db:
        for key, value in values.items():
            await db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()


async def get_game_substance(db, game_id: int) -> dict:
    """How much observable reality backs a games row.

    Returns ``{"has_identifier": bool, "playtime_minutes": int,
    "owned_platforms": int}`` aggregated over the row's game_platforms. Used by
    the nesting guard: a row holding a store identifier AND recorded playtime
    is a real, played library item, while a row with none of that may be a
    phantom (a name-collision mint or an importer artifact) — demoting the
    former under the latter hides a real game behind an empty shell.
    """
    row = await db.execute_fetchone(
        """SELECT
               EXISTS(SELECT 1 FROM game_platforms gp
                      JOIN game_platform_identifiers gpi
                        ON gpi.game_platform_id = gp.id
                      WHERE gp.game_id = ?) AS has_identifier,
               COALESCE((SELECT SUM(COALESCE(gp.playtime_minutes, 0))
                         FROM game_platforms gp
                         WHERE gp.game_id = ? AND gp.owned = 1), 0) AS playtime_minutes,
               (SELECT COUNT(*) FROM game_platforms gp
                WHERE gp.game_id = ? AND gp.owned = 1) AS owned_platforms""",
        (game_id, game_id, game_id),
    )
    return {
        "has_identifier": bool(row["has_identifier"]),
        "playtime_minutes": row["playtime_minutes"] or 0,
        "owned_platforms": row["owned_platforms"] or 0,
    }


async def nesting_substance_conflict(
    db, child_game_id: int, parent_game_id: int
) -> bool:
    """True when nesting ``child`` under ``parent`` would hide a real game.

    The conflict shape (observed in prod: Titanfall 2 — appid + 20h — nested
    under an empty same-name duplicate row): the CHILD carries a store
    identifier and recorded playtime, while the proposed PARENT carries
    neither. Nesting demotes the child out of every primary rollup, leaving
    the library showing only the hollow parent. Classification writers skip
    the demotion when this returns True; update_game raises so a human sees
    why. Deliberately conservative (identifier AND playtime on the child,
    neither on the parent) so legitimate shapes — a played edition under a
    played base game, a soundtrack under a wishlist-only parent — stay
    nestable.
    """
    child = await get_game_substance(db, child_game_id)
    if not (child["has_identifier"] and child["playtime_minutes"] > 0):
        return False
    parent = await get_game_substance(db, parent_game_id)
    return not parent["has_identifier"] and parent["playtime_minutes"] == 0


async def get_game_by_identifier(identifier_type: str, identifier_value: str) -> aiosqlite.Row | None:
    # Normalized the same way upsert_game_platform_identifier writes it (a
    # no-op for every identifier_type except nintendo_title_id), so callers
    # never need to pre-normalize a lookup value themselves.
    identifier_value = normalize_identifier_value(identifier_type, identifier_value)
    async with get_db() as db:
        return await db.execute_fetchone(
            """SELECT g.*
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id
               JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
               WHERE gpi.identifier_type = ? AND gpi.identifier_value = ?
               LIMIT 1""",
            (identifier_type, identifier_value),
        )


async def get_game_by_appid(appid: int) -> aiosqlite.Row | None:
    return await get_game_by_identifier(STEAM_APP_ID, str(appid))


async def get_game_by_igdb_id(igdb_id: int) -> aiosqlite.Row | None:
    async with get_db() as db:
        return await db.execute_fetchone(
            "SELECT * FROM games WHERE igdb_id = ?", (igdb_id,)
        )


async def has_nested_children(db, game_id: int) -> bool:
    """True when some other row nests under ``game_id`` (it is a parent).

    A parent must stay a primary library item (ADR 0002): nesting one would hide
    it from the is_primary rollups AND strand its children under a row that is
    itself unreachable — the shape that made both Fallout: New Vegas rows
    invisible. Every classification writer (apply_content_classification,
    igdb.py::_apply_igdb_metadata, update_game) checks this before demoting a row
    to a nested content_type. Takes the caller's open connection: all three run
    the check inside the same read→guard→write block they write from.
    """
    row = await db.execute_fetchone(
        "SELECT 1 FROM games WHERE parent_game_id = ? LIMIT 1", (game_id,)
    )
    return row is not None


async def get_game_by_name_exact(name: str) -> aiosqlite.Row | None:
    async with get_db() as db:
        return await db.execute_fetchone(
            "SELECT * FROM games WHERE lower(name) = lower(?) ORDER BY id LIMIT 1",
            (name,),
        )


async def get_platform_game_by_normalized_name(
    name: str, platform: str
) -> aiosqlite.Row | None:
    """The oldest games row with this normalized name already owning ``platform``.

    The stable-identifier equivalent for identifier-less stores (GOG): a store
    whose catalog is keyed only by title must treat "same normalized name on
    the same platform" as a re-sync of the same item. Without this pre-match,
    re-resolving the title through IGDB can land on a *different* IGDB
    candidate whose conflicting release year makes the fuzzy fallback refuse
    the existing row and fork a duplicate (observed in prod: 4 GOG pairs like
    "Agony" id 2037/3061).
    """
    from ..title_normalization import normalize_search_text

    normalized = normalize_search_text(name)
    if not normalized:
        return None
    async with get_db() as db:
        return await db.execute_fetchone(
            """SELECT g.*
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = ?
               WHERE COALESCE(g.name_normalized, '') = ?
               ORDER BY g.id
               LIMIT 1""",
            (platform, normalized),
        )


async def get_steam_appid_for_game(game_id: int) -> int | None:
    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT gpi.identifier_value
               FROM game_platform_identifiers gpi
               JOIN game_platforms gp ON gp.id = gpi.game_platform_id
               WHERE gp.game_id = ? AND gpi.identifier_type = ?
               ORDER BY gpi.is_primary DESC, gpi.id ASC
               LIMIT 1""",
            (game_id, STEAM_APP_ID),
        )
    if row is None:
        return None
    try:
        return int(row["identifier_value"])
    except (TypeError, ValueError):
        return None


async def get_steam_platform_row_by_appid(appid: int) -> aiosqlite.Row | None:
    async with get_db() as db:
        return await db.execute_fetchone(
            """SELECT gp.id AS game_platform_id,
                      gp.game_id,
                      gp.platform,
                      gp.owned,
                      gp.playtime_minutes,
                      gp.playtime_2weeks_minutes,
                      gp.last_played,
                      gp.last_synced,
                      g.name,
                      g.genres,
                      g.tags,
                      g.short_description,
                      g.release_date,
                      g.hltb_main,
                      g.hltb_extra,
                      g.hltb_complete,
                      g.hltb_cached_at,
                      g.is_farmed,
                      spd.steam_review_score,
                      spd.steam_review_desc,
                      spd.protondb_tier,
                      spd.store_cached_at,
                      spd.protondb_cached_at,
                      spd.steamspy_cached_at,
                      spd.rtime_last_played,
                      spd.library_updated_at,
                      gpe.metacritic_score,
                      gpe.metacritic_url,
                      gpe.opencritic_score,
                      gpe.opencritic_tier,
                      gpe.opencritic_percent_rec,
                      gpe.opencritic_url,
                      gpe.opencritic_num_reviews,
                      gpe.platform_release_date
               FROM game_platform_identifiers gpi
               JOIN game_platforms gp ON gp.id = gpi.game_platform_id
               JOIN games g ON g.id = gp.game_id
               LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
               LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
               WHERE gpi.identifier_type = ? AND gpi.identifier_value = ?
               LIMIT 1""",
            (STEAM_APP_ID, str(appid)),
        )


def _coerce_identifier_value(identifier_type: str, identifier_value: str) -> str | int:
    if identifier_type in {STEAM_APP_ID, GOG_PRODUCT_ID}:
        try:
            return int(identifier_value)
        except ValueError:
            return identifier_value
    return identifier_value


def _platform_dict(row: aiosqlite.Row) -> dict:
    playtime_minutes = row["playtime_minutes"]
    playtime_2weeks_minutes = row["playtime_2weeks_minutes"]
    last_played_date = row["last_played"]
    platform = {
        "game_platform_id": row["game_platform_id"],
        "platform": row["platform"],
        "owned": bool(row["owned"]),
        # Ownership sourced from the store account's license list for an app
        # the public owned-games API no longer returns (typically retired/
        # delisted) — see audit_steam_licenses. Tolerates rows selected before
        # the v32 column existed (tests build these dicts by hand).
        "delisted": bool(row["delisted"]) if "delisted" in row.keys() else False,
        "playtime_minutes": playtime_minutes,
        "playtime_hours": round((playtime_minutes or 0) / 60, 1),
        "playtime_2weeks_minutes": playtime_2weeks_minutes,
        "playtime_2weeks_hours": round((playtime_2weeks_minutes or 0) / 60, 1),
        "last_played_date": last_played_date,
        "last_synced": row["last_synced"],
        "acquired_at": row["acquired_at"],
        "price_paid": row["price_paid"],
        "price_currency": row["price_currency"],
        "purchase_source": row["purchase_source"],
        "bundle_name": row["bundle_name"],
        "identifiers": {},
        "provider_data": {},
        "platform_release_date": row["platform_release_date"],
        "metacritic_score": row["metacritic_score"],
        "metacritic_url": row["metacritic_url"],
        "opencritic_score": row["opencritic_score"],
        "opencritic_tier": row["opencritic_tier"],
        "opencritic_percent_rec": row["opencritic_percent_rec"],
        "opencritic_url": row["opencritic_url"],
        "opencritic_num_reviews": row["opencritic_num_reviews"],
    }

    if row["platform"] == STEAM_PLATFORM:
        last_played = row["rtime_last_played"]
        steam_last_played_date = (
            datetime.fromtimestamp(last_played, tz=timezone.utc).date().isoformat()
            if last_played
            else None
        )
        # Steam's last-played lives in steam_platform_data (rtime_last_played), not
        # the generic game_platforms.last_played column. Surface it at the top level
        # so last_played_date is uniform across platforms.
        if platform["last_played_date"] is None:
            platform["last_played_date"] = steam_last_played_date
        platform["provider_data"] = {
            "steam_review_score": row["steam_review_score"],
            "steam_review_desc": row["steam_review_desc"],
            "protondb_tier": row["protondb_tier"],
            "last_played_date": steam_last_played_date,
            "library_updated_at": row["library_updated_at"],
        }

    return platform


async def load_platforms_for_games(game_ids: Iterable[int]) -> dict[int, list[dict]]:
    """Load platform rows, identifiers, and provider-specific data for many games."""
    ids = list(dict.fromkeys(game_ids))
    if not ids:
        return {}

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT gp.id AS game_platform_id,
                       gp.game_id,
                       gp.platform,
                       gp.owned,
                       gp.delisted,
                       gp.playtime_minutes,
                       gp.playtime_2weeks_minutes,
                       gp.last_played,
                       gp.last_synced,
                       gp.acquired_at,
                       gp.price_paid,
                       gp.price_currency,
                       gp.purchase_source,
                       gp.bundle_name,
                       gpi.identifier_type,
                       gpi.identifier_value,
                       gpi.is_primary,
                       spd.steam_review_score,
                       spd.steam_review_desc,
                       spd.protondb_tier,
                       spd.rtime_last_played,
                       spd.library_updated_at,
                       gpe.platform_release_date,
                       gpe.metacritic_score,
                       gpe.metacritic_url,
                       gpe.opencritic_score,
                       gpe.opencritic_tier,
                       gpe.opencritic_percent_rec,
                       gpe.opencritic_url,
                       gpe.opencritic_num_reviews
                FROM game_platforms gp
                LEFT JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
                LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
                LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
                WHERE gp.game_id IN ({placeholders})
                ORDER BY gp.game_id, gp.platform, gp.id, gpi.is_primary DESC, gpi.identifier_type""",
            ids,
        )

    by_game: dict[int, list[dict]] = defaultdict(list)
    by_platform_id: dict[int, dict] = {}
    for row in rows:
        game_id = row["game_id"]
        platform_id = row["game_platform_id"]
        platform = by_platform_id.get(platform_id)
        if platform is None:
            platform = _platform_dict(row)
            by_platform_id[platform_id] = platform
            by_game[game_id].append(platform)

        identifier_type = row["identifier_type"]
        identifier_value = row["identifier_value"]
        if identifier_type and identifier_value:
            platform["identifiers"][identifier_type] = _coerce_identifier_value(
                identifier_type,
                identifier_value,
            )

    for platforms in by_game.values():
        platforms.sort(key=lambda item: item["platform"])

    return dict(by_game)


async def load_series_for_games(game_ids: Iterable[int]) -> dict[int, list[dict]]:
    """Load series memberships (IGDB collections/franchises) for many games.

    Returns {game_id: [{"name", "kind", "igdb_id"}, ...]}.
    """
    ids = list(dict.fromkeys(game_ids))
    if not ids:
        return {}

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT m.game_id, s.name, s.kind, s.igdb_id
                FROM game_series_membership m
                JOIN game_series s ON s.id = m.series_id
                WHERE m.game_id IN ({placeholders})
                ORDER BY m.game_id, s.kind, s.name""",
            ids,
        )

    by_game: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        by_game[row["game_id"]].append(
            {"name": row["name"], "kind": row["kind"], "igdb_id": row["igdb_id"]}
        )
    return dict(by_game)


def _related_content_group(content_type: str) -> str:
    if content_type == "dlc":
        return "dlc"
    if content_type == "expansion":
        return "expansions"
    if content_type == "bundle":
        return "bundles"
    if content_type == "edition":
        return "editions"
    return "other"


async def load_related_content_for_games(game_ids: Iterable[int]) -> dict[int, dict[str, list[dict]]]:
    """Load child DLC/expansion/edition/bundle rows grouped by parent game id."""
    ids = list(dict.fromkeys(game_ids))
    empty: dict[str, list[dict]] = {"dlc": [], "expansions": [], "editions": [], "bundles": [], "other": []}
    if not ids:
        return {}

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT id AS game_id,
                       parent_game_id,
                       name,
                       content_type,
                       is_primary_library_item
                FROM games
                WHERE parent_game_id IN ({placeholders})
                ORDER BY content_type, name""",
            ids,
        )

    related_ids = [row["game_id"] for row in rows]
    platforms_by_game = await load_platforms_for_games(related_ids)
    grouped: dict[int, dict[str, list[dict]]] = {
        game_id: {key: list(value) for key, value in empty.items()} for game_id in ids
    }
    for row in rows:
        child_platforms = platforms_by_game.get(row["game_id"], [])

        # Ownership mirrors tools/common.py::OWNED_SQL's notion (any
        # game_platforms row with owned=1) — here expressed over the
        # already-loaded platform dicts rather than a fresh EXISTS subquery,
        # since load_platforms_for_games() already carries each row's `owned`.
        owned = any(p["owned"] for p in child_platforms)

        # A child can carry owned rows on multiple platforms (e.g. bought on
        # both Steam and Switch2); hoist a single deterministic scalar rather
        # than expose a list. Preference: among *owned* rows, the one with a
        # non-null price_paid; ties/absences broken by earliest acquired_at,
        # then lowest game_platform_id. If no owned row has a recorded price,
        # all three hoisted fields are null (no owned row is preferred over
        # another on acquired_at alone without a price to go with it).
        priced_owned = [p for p in child_platforms if p["owned"] and p["price_paid"] is not None]
        priced_owned.sort(
            key=lambda p: (
                p["acquired_at"] is None,
                p["acquired_at"] or "",
                p["game_platform_id"],
            )
        )
        best = priced_owned[0] if priced_owned else None

        entry = {
            "game_id": row["game_id"],
            "name": row["name"],
            "content_type": row["content_type"],
            "is_primary_library_item": bool(row["is_primary_library_item"]),
            "platforms": child_platforms,
            "owned": owned,
            "price_paid": best["price_paid"] if best else None,
            "price_currency": best["price_currency"] if best else None,
            "acquired_at": best["acquired_at"] if best else None,
        }
        grouped[row["parent_game_id"]][_related_content_group(row["content_type"])].append(entry)

    return grouped


async def load_wishlist_with_prices(platform: str | None) -> list[aiosqlite.Row]:
    """Wishlist rows LEFT JOINed to cached price rows across ALL platforms,
    plus a resolved Steam appid for ITAD lookups, IGDB platform-availability
    metadata, and current ownership.

    appid resolution: w.store_identifier first (captured at Steam-wishlist-
    sync time for unowned items with no game_platforms row), falling back to
    the owned-row identifier subquery (mirrors tools/common.py::STEAM_APPID_SQL)
    for the rare case where an item is wishlisted on one platform but owned
    on Steam under the same game_id — e.g. a bundle or gift.

    Join shape: LEFT JOIN game_prices on game_id ONLY — not also on platform
    or shop. This is deliberate: a wishlist row's own `platform` column now
    means "where this was wishlisted", not "which platform's prices are
    relevant" — a game wishlisted on Steam may also have a cached Switch2
    price worth surfacing (e.g. for a cheaper-elsewhere recommendation), so
    the join fans out across every cached price row for the game_id on any
    platform/shop. `price_platform` (the joined game_prices.platform) is what
    disambiguates which platform each fanned-out price row belongs to. This
    is a thin data-access layer; picking "cheapest", grouping per platform,
    or otherwise collapsing the fanned-out rows is business logic that
    belongs to the tool layer (tools/deals.py), not here.
    """
    where = "WHERE 1=1"
    params: list = []
    if platform is not None:
        where += " AND w.platform = ?"
        params.append(platform)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT w.game_id, g.name, w.platform, w.wishlisted_at, w.source,
                       w.store_identifier,
                       g.igdb_platforms, g.igdb_cached_at,
                       (
                           SELECT COALESCE(json_group_array(sgp2.platform), '[]')
                           FROM game_platforms sgp2
                           WHERE sgp2.game_id = w.game_id AND sgp2.owned = 1
                       ) AS owned_platforms,
                       COALESCE(
                           CAST(w.store_identifier AS INTEGER),
                           (
                               SELECT CAST(gpi.identifier_value AS INTEGER)
                               FROM game_platform_identifiers gpi
                               JOIN game_platforms sgp ON sgp.id = gpi.game_platform_id
                               WHERE sgp.game_id = w.game_id
                                 AND gpi.identifier_type = '{STEAM_APP_ID}'
                               ORDER BY gpi.is_primary DESC, gpi.id ASC
                               LIMIT 1
                           )
                       ) AS steam_appid,
                       gp.platform AS price_platform,
                       gp.shop, gp.price, gp.regular_price, gp.cut_pct,
                       gp.currency, gp.deal_url, gp.fetched_at
                FROM game_wishlist w
                JOIN games g ON g.id = w.game_id
                LEFT JOIN game_prices gp ON gp.game_id = w.game_id
                {where}
                ORDER BY w.wishlisted_at DESC""",
            params,
        )
    return rows
