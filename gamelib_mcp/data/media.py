"""On-demand trailer / screenshot / similar-games media, meta-KV cached.

Nothing else in the codebase fetches media: enrichment stores text and scores,
and the 7-day store cache predates the media filter groups entirely. Evaluation
candidates are also routinely unowned rows that enrichment never touches, so
this is fetch-on-demand keyed by store identity (appid or IGDB id) with a
meta-KV cache, rather than new columns on ``games``.

Steam wins whenever an appid is known and the source is never mixed: a card
showing Steam screenshots under an IGDB trailer describes two different
builds of a game. The IGDB path also carries ``similar_games`` and the
developer PEDIGREE (who made this, and what they shipped before it), neither of
which the Steam payload has an equivalent for.

Everything here is best-effort. ``get_game_media`` never raises for a fetch
failure — the verdict it decorates matters, the trailer does not — and an
expired cache whose refresh fails is served stale rather than dropped.
"""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .db import get_meta, set_meta
from .igdb import (
    IGDBRequestFailure,
    _get_token,
    _igdb_headers,
    _post_igdb_games,
    fetch_games_by_exact_name,
    igdb_credentials_configured,
)
from .steam_store import fetch_store_appdetails

logger = logging.getLogger(__name__)

# Caps (bounded-response pattern): a card shows a strip, not a gallery.
SCREENSHOT_CAP = 6
SIMILAR_CAP = 8
PREVIOUS_GAMES_CAP = 6
SUMMARY_MAX_CHARS = 500

# The developer's own back catalogue, fetched with ONE bounded query on the
# games endpoint. Deliberately not IGDB's `company.developed` expansion: that
# is unbounded, and an EA or a Ubisoft would drag a five-hundred-entry array
# through every card.
COMPANY_CATALOG_LIMIT = 30
# Above this, the studio is a factory rather than an authorship signal: six
# arbitrary posters out of a 500-game catalogue say nothing about the game in
# front of you, so the strip degrades to its header line. Deliberately below
# COMPANY_CATALOG_LIMIT, so "we fetched a full page" is not the same test.
BIG_CATALOG_THRESHOLD = 25

MEDIA_CACHE_TTL = timedelta(days=7)
COMPANY_CACHE_TTL = timedelta(days=30)
NAME_CACHE_TTL = timedelta(days=30)
# A miss is cached too, on a much shorter clock: a title IGDB genuinely has no
# record of must not be re-queried on every call, but "no record" is also what
# a bad name spelling looks like, and that gets corrected within a day.
MISS_CACHE_TTL = timedelta(hours=24)

_STEAM_MEDIA_FILTERS = "screenshots,movies,short_description"

# appdetails serves only DASH/HLS manifests today, which a bare <video> cannot
# play. The legacy constructible mp4 renditions still exist (verified for 2025
# uploads) but are undocumented surface Valve could drop, so the constructed URL
# is HEAD-validated before it reaches a card.
_STEAM_MOVIE_URL = (
    "https://cdn.cloudflare.steamstatic.com/steam/apps/{movie_id}/movie480.mp4"
)
_STEAM_MOVIE_HQ_URL = (
    "https://cdn.cloudflare.steamstatic.com/steam/apps/{movie_id}/movie_max.mp4"
)
_TRAILER_HEAD_TIMEOUT_SECONDS = 5.0

_IGDB_SCREENSHOT_THUMB_URL = (
    "https://images.igdb.com/igdb/image/upload/t_screenshot_med/{image_id}.jpg"
)
_IGDB_SCREENSHOT_FULL_URL = (
    "https://images.igdb.com/igdb/image/upload/t_screenshot_big/{image_id}.jpg"
)
_YOUTUBE_POSTER_URL = "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

_IGDB_MEDIA_FIELDS = (
    "fields name, summary, first_release_date, hypes, "
    "screenshots.image_id, videos.video_id, videos.name, "
    "similar_games.id, similar_games.name, similar_games.cover.image_id, "
    "similar_games.first_release_date, "
    "involved_companies.company.id, involved_companies.company.name, "
    "involved_companies.company.start_date, involved_companies.company.country, "
    "involved_companies.developer, involved_companies.publisher, "
    "involved_companies.porting, involved_companies.supporting;"
)

# One bounded page of the developer's own games, newest first. The `where` runs
# on the GAMES endpoint so the limit actually bounds the response — the same
# filter expressed as a company expansion would not.
_IGDB_COMPANY_CATALOG_QUERY = (
    "fields id, name, cover.image_id, first_release_date, aggregated_rating; "
    "where involved_companies.company = {company_id} & "
    "involved_companies.developer = true; "
    "sort first_release_date desc; limit {limit};"
)


class _MediaFetchError(RuntimeError):
    """A provider request failed — distinct from the provider having nothing.

    The difference decides caching: a genuine miss is remembered (24h) so a
    permanently unresolvable title stops being re-queried, while a failure
    falls back to whatever the cache still holds and is never written down.
    """


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# Returned by ``_cached`` when a refresh FAILED and no stale copy exists — as
# opposed to None, which means the source genuinely has nothing. Swallowing the
# difference made a provider outage indistinguishable from a media-less game:
# ``get_game_media`` folds this into its ``errors`` list instead, so the
# package can report "unavailable" rather than rendering a silently bare card.
# Never cached, never returned to callers.
_FETCH_FAILED: Any = object()


# Payload version, carried in the cache KEY rather than inside the entry: a
# stored payload has no schema, so the only way a widened shape (pedigree,
# 2026-08) refetches instead of rendering half a card for seven days is to ask
# a question the old entries are not the answer to. Bump this whenever the
# cached payload grows a member a renderer depends on.
MEDIA_CACHE_VERSION = "v2"


def _cache_key(kind: str, identity: str | int) -> str:
    return f"game_media:{MEDIA_CACHE_VERSION}:{kind}:{identity}"


def _company_cache_key(company_id: int) -> str:
    """Per-company, not per-game: one studio's catalogue serves every card of its games."""
    return f"game_media_company:{company_id}"


def _name_cache_key(name: str) -> str:
    return f"game_media_name:{name.strip().lower()}"


def _parse_cache(raw: str | None) -> tuple[datetime, Any] | None:
    """(fetched_at, payload) from a cached entry; None when absent/malformed."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        payload = data["payload"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return fetched_at, payload


async def _cached(key: str, fetch, *, ttl: timedelta, label: str) -> Any:
    """Serve ``key`` from the meta KV, refreshing through ``fetch`` when stale.

    A refresh that FAILS (``_MediaFetchError``) falls back to the cached copy
    however old it is and writes nothing; a refresh that legitimately finds
    nothing is cached as a miss on the shorter MISS_CACHE_TTL, except when a
    payload was already known — a source that suddenly returns nothing for a
    game it used to describe is far more likely to be broken than to have
    forgotten the game.
    """
    cached = _parse_cache(await get_meta(key))
    if cached is not None:
        fetched_at, payload = cached
        age = datetime.now(UTC) - fetched_at
        if age < (ttl if payload is not None else MISS_CACHE_TTL):
            return payload

    try:
        payload = await fetch()
    except _MediaFetchError:
        if cached is not None:
            logger.warning("Media fetch failed for %s; serving stale cache", label)
            return cached[1]
        logger.warning("Media fetch failed for %s and nothing is cached", label)
        return _FETCH_FAILED
    except Exception:
        logger.warning("Unexpected media fetch error for %s", label, exc_info=True)
        return cached[1] if cached is not None else _FETCH_FAILED

    if payload is None and cached is not None and cached[1] is not None:
        logger.warning("Media source returned nothing for %s; serving stale cache", label)
        return cached[1]

    if isinstance(payload, dict) and payload.get("partial"):
        # A payload assembled around a FAILED sub-fetch (e.g. the developer
        # catalog): serve it best-effort this call, but never store it — a
        # cached partial would freeze the gap for the whole TTL.
        logger.warning("Partial media payload for %s (%s) — not cached", label,
                       payload["partial"])
        return payload

    await set_meta(
        key,
        json.dumps({"fetched_at": datetime.now(UTC).isoformat(), "payload": payload}),
    )
    return payload


# ── Steam ────────────────────────────────────────────────────────────────────


async def _mp4_is_live(url: str) -> bool:
    """One HEAD request against the constructed mp4 — the legacy-surface gate.

    The gate exists to catch Valve DROPPING the legacy renditions (a
    definitive non-2xx), not to demand a healthy CDN this instant: a
    transport failure is no evidence the mp4 is gone, and dropping the
    trailer on one would bake a trailer-less payload into the 7-day cache.
    Inconclusive checks trust the URL — the widget's own <video> error
    fallback (poster + link-out) covers a genuinely dead one.
    """
    try:
        async with httpx.AsyncClient(timeout=_TRAILER_HEAD_TIMEOUT_SECONDS) as client:
            response = await client.head(url, follow_redirects=True)
    except Exception as exc:
        logger.warning(
            "Trailer HEAD check inconclusive for %s (%s) — trusting the URL", url, exc
        )
        return True
    if response.is_success:
        return True
    if response.status_code in (404, 410):
        return False
    # 429, 5xx, auth quirks: transient or ambiguous, not evidence the
    # rendition is gone — same stance as a transport failure.
    logger.warning(
        "Trailer HEAD check inconclusive for %s (HTTP %s) — trusting the URL",
        url,
        response.status_code,
    )
    return True


async def _steam_trailer(movies: list | None) -> dict | None:
    if not isinstance(movies, list) or not movies:
        return None
    highlighted = [m for m in movies if isinstance(m, dict) and m.get("highlight")]
    candidates = highlighted or [m for m in movies if isinstance(m, dict)]
    if not candidates:
        return None
    movie = candidates[0]
    movie_id = movie.get("id")
    if movie_id is None:
        return None

    url = _STEAM_MOVIE_URL.format(movie_id=movie_id)
    if not await _mp4_is_live(url):
        # Screenshots still come back; only the hero degrades.
        return None
    return {
        "kind": "mp4",
        "url": url,
        "hq_url": _STEAM_MOVIE_HQ_URL.format(movie_id=movie_id),
        "poster": movie.get("thumbnail"),
        "name": movie.get("name"),
    }


async def _fetch_steam_media(appid: int) -> dict | None:
    # raise_on_failure: a request failure must NOT come back as the same None
    # a "Steam has nothing for this appid" answer uses — _cached would write
    # it down as a 24-hour miss, and one transient outage would strip media
    # from every card of this game for the rest of the day.
    try:
        data = await fetch_store_appdetails(
            appid, filters=_STEAM_MEDIA_FILTERS, raise_on_failure=True
        )
    except Exception as exc:
        raise _MediaFetchError(f"steam appdetails failed for {appid}: {exc}") from exc
    if not data:
        return None

    raw_shots = [s for s in (data.get("screenshots") or []) if isinstance(s, dict)]
    screenshots = [
        {"thumb": shot.get("path_thumbnail"), "full": shot.get("path_full")}
        for shot in raw_shots
        if shot.get("path_thumbnail") or shot.get("path_full")
    ]
    trailer = await _steam_trailer(data.get("movies"))
    description = data.get("short_description") or None

    if not screenshots and trailer is None and description is None:
        return None

    return {
        "media": {
            "source": "steam",
            "trailer": trailer,
            "screenshots": screenshots[:SCREENSHOT_CAP],
            "screenshot_count": len(screenshots),
            "screenshots_truncated": len(screenshots) > SCREENSHOT_CAP,
            "short_description": description,
        },
        "similar_raw": None,
        "similar_count": None,
        "pedigree_raw": None,
        "igdb_id": None,
    }


# ── IGDB ─────────────────────────────────────────────────────────────────────


def _igdb_trailer(videos: list | None) -> dict | None:
    entries = [v for v in (videos or []) if isinstance(v, dict) and v.get("video_id")]
    if not entries:
        return None
    named = [v for v in entries if "trailer" in str(v.get("name") or "").lower()]
    video = (named or entries)[0]
    video_id = video["video_id"]
    return {
        "kind": "youtube",
        "video_id": video_id,
        "poster": _YOUTUBE_POSTER_URL.format(video_id=video_id),
        "name": video.get("name"),
    }


def _release_year(epoch: Any) -> int | None:
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC).year
    except (OSError, OverflowError, ValueError):
        return None


def _int_field(value: Any) -> int | None:
    """An IGDB integer field (epoch, hype count) — bools are not integers here."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _cover_image_id(entry: dict) -> str | None:
    cover = entry.get("cover")
    return cover.get("image_id") if isinstance(cover, dict) else None


def _critic_score(value: Any) -> int | None:
    """IGDB's 0-100 aggregated_rating, rounded — a badge, not a statistic."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value)


def _igdb_similar(similar_games: list | None) -> tuple[list[dict], int]:
    entries = [
        {
            "igdb_id": game.get("id"),
            "name": game.get("name"),
            "release_year": _release_year(game.get("first_release_date")),
            "cover_image_id": _cover_image_id(game),
        }
        for game in (similar_games or [])
        if isinstance(game, dict) and game.get("id") is not None
    ]
    return entries[:SIMILAR_CAP], len(entries)


# ── Pedigree (who made this, and what else they made) ────────────────────────


def _company_roles(involved: list | None) -> tuple[dict | None, list[str], str | None]:
    """``(primary developer company, every developer name, publisher name)``.

    A row flagged ``porting`` or ``supporting`` describes a contractor on this
    one title, not the studio whose body of work the card is about — those are
    excluded from both the primary pick and the co-dev header, so a Bluepoint
    port never rewrites a game's authorship. The publisher is carried as a NAME
    only and never gets a catalogue: a publisher's back list is a distribution
    record, not a body of work, and rendering six of its posters would say
    nothing about the game in front of you.
    """
    rows = [row for row in (involved or []) if isinstance(row, dict)]
    developers = [
        row
        for row in rows
        if row.get("developer") and not row.get("porting") and not row.get("supporting")
        and isinstance(row.get("company"), dict)
    ]
    primary = developers[0]["company"] if developers else None
    names: list[str] = []
    for row in developers:
        name = row["company"].get("name")
        if isinstance(name, str) and name.strip() and name not in names:
            names.append(name)

    publisher_name = None
    for row in rows:
        company = row.get("company")
        if row.get("publisher") and isinstance(company, dict) and company.get("name"):
            publisher_name = company["name"]
            break
    return primary, names, publisher_name


async def _fetch_company_catalog(company_id: int) -> list[dict] | None:
    """One bounded page of games this company DEVELOPED, newest first."""
    results = await _post_igdb_media_query(
        _IGDB_COMPANY_CATALOG_QUERY.format(
            company_id=company_id, limit=COMPANY_CATALOG_LIMIT
        ),
        f"company {company_id}",
    )
    entries = [
        {
            "igdb_id": game["id"],
            "name": game.get("name"),
            "first_release_date": _int_field(game.get("first_release_date")),
            "cover_image_id": _cover_image_id(game),
            "critic_score": _critic_score(game.get("aggregated_rating")),
        }
        for game in results
        if isinstance(game, dict) and game.get("id") is not None
    ]
    return entries or None


async def _company_catalog(company_id: int) -> tuple[list[dict], bool]:
    """(catalog, fetch_failed) — the flag keeps a failed catalog fetch from
    masquerading as an empty catalog, which the enclosing game payload would
    otherwise cache for seven days."""
    payload = await _cached(
        _company_cache_key(company_id),
        lambda: _fetch_company_catalog(company_id),
        ttl=COMPANY_CACHE_TTL,
        label=f"company {company_id}",
    )
    if payload is _FETCH_FAILED:
        return [], True
    return (payload, False) if isinstance(payload, list) else ([], False)


async def _igdb_pedigree(item: dict, igdb_id: int) -> tuple[dict | None, bool]:
    """(raw pedigree block, catalog_fetch_failed).

    Returns (None, False) when IGDB names no qualifying developer — a pedigree
    with no studio is not a weaker pedigree, it is no claim at all. The flag
    reports a FAILED catalog fetch (rendered header-only, like the big-studio
    damper) so the caller can keep the incomplete payload out of the cache.
    Annotation against the library happens one layer up (tools/game_media.py);
    nothing here knows what is owned.
    """
    developer, developer_names, publisher_name = _company_roles(
        item.get("involved_companies")
    )
    if developer is None:
        return None, False

    company_id = _int_field(developer.get("id"))
    catalog, catalog_failed = (
        await _company_catalog(company_id) if company_id is not None else ([], False)
    )
    catalog_size = len(catalog)
    big_catalog = catalog_size > BIG_CATALOG_THRESHOLD

    previous: list[dict[str, Any]] = []
    previous_count = 0
    if not big_catalog:
        # "Before this game", not "everything they made": a studio's later
        # releases say nothing about the track record that produced this one.
        # A candidate with no release date of its own (an announced game) falls
        # back to now, which is the same question asked loosely.
        cutoff = _int_field(item.get("first_release_date")) or int(
            datetime.now(UTC).timestamp()
        )
        qualifying = sorted(
            (
                entry
                for entry in catalog
                if entry["igdb_id"] != igdb_id
                and entry["first_release_date"] is not None
                and entry["first_release_date"] < cutoff
            ),
            key=lambda entry: entry["first_release_date"],
            reverse=True,
        )
        # Counted within the fetched page, so it is a floor rather than the
        # studio's true output — bounded by design, and catalog_truncated says
        # when the page was full.
        previous_count = len(qualifying)
        previous = [
            {
                "igdb_id": entry["igdb_id"],
                "name": entry["name"],
                "release_year": _release_year(entry["first_release_date"]),
                "cover_image_id": entry["cover_image_id"],
                "critic_score": entry["critic_score"],
            }
            for entry in qualifying[:PREVIOUS_GAMES_CAP]
        ]

    return {
        "developer": {
            "name": developer.get("name"),
            "igdb_company_id": company_id,
            "founded_year": _release_year(_int_field(developer.get("start_date"))),
            "country": developer.get("country"),
        },
        "developer_names": developer_names,
        "publisher_name": publisher_name,
        "previous_games": previous,
        "previous_count": previous_count,
        "previous_truncated": previous_count > len(previous),
        "catalog_size": catalog_size,
        "catalog_truncated": catalog_size >= COMPANY_CATALOG_LIMIT,
        "big_catalog": big_catalog,
        # Carried, never rendered: an anticipation counter is a popularity
        # signal, and this card does not argue from popularity.
        "hypes": _int_field(item.get("hypes")),
    }, catalog_failed


async def _post_igdb_media_query(query: str, label: str) -> list[dict]:
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not igdb_credentials_configured() or not client_id:
        # Not a failure: an unconfigured IGDB has nothing to say and never will
        # this run, so the miss is cacheable like any other.
        return []
    try:
        token = await _get_token()
        return await _post_igdb_games(query, headers=_igdb_headers(client_id, token))
    except Exception as exc:
        raise _MediaFetchError(f"IGDB media query failed for {label}") from exc


async def _fetch_igdb_media(igdb_id: int) -> dict | None:
    results = await _post_igdb_media_query(
        f"{_IGDB_MEDIA_FIELDS} where id = {igdb_id}; limit 1;", f"igdb {igdb_id}"
    )
    if not results:
        return None
    item = results[0]

    screenshots = [
        {
            "thumb": _IGDB_SCREENSHOT_THUMB_URL.format(image_id=shot["image_id"]),
            "full": _IGDB_SCREENSHOT_FULL_URL.format(image_id=shot["image_id"]),
        }
        for shot in (item.get("screenshots") or [])
        if isinstance(shot, dict) and shot.get("image_id")
    ]
    trailer = _igdb_trailer(item.get("videos"))
    summary = item.get("summary")
    description = _truncate(summary, SUMMARY_MAX_CHARS) if summary else None
    similar, similar_count = _igdb_similar(item.get("similar_games"))
    pedigree, catalog_failed = await _igdb_pedigree(item, igdb_id)

    if (
        not screenshots
        and trailer is None
        and description is None
        and not similar
        and pedigree is None
    ):
        return None

    payload: dict[str, Any] = {
        "media": {
            "source": "igdb",
            "trailer": trailer,
            "screenshots": screenshots[:SCREENSHOT_CAP],
            "screenshot_count": len(screenshots),
            "screenshots_truncated": len(screenshots) > SCREENSHOT_CAP,
            "short_description": description,
        },
        "similar_raw": similar or None,
        "similar_count": similar_count or None,
        "pedigree_raw": pedigree,
        "igdb_id": igdb_id,
    }
    if catalog_failed:
        # Serve this call best-effort (header-only strip) but keep the payload
        # OUT of the 7-day cache — otherwise one transient catalog failure
        # freezes an empty studio strip for a week. ``_cached`` honors the
        # marker; ``get_game_media`` strips it and reports the error.
        payload["partial"] = "igdb: company catalog fetch failed"
    return payload


async def _resolve_igdb_id_by_name(
    name: str, errors: list[str] | None = None
) -> int | None:
    """EXACT-name IGDB lookup, refusing ambiguity — display use only.

    This is igdb.py's own equality lookup (the one search_game falls back to)
    with the same refusal: two real games sharing a name cannot be told apart
    here, and guessing would decorate a verdict with another game's trailer.
    The result is NEVER written back to ``games.igdb_id`` — a display-time
    guess must not become a stored link.
    """

    async def fetch() -> dict | None:
        if not igdb_credentials_configured():
            return None
        try:
            matches = await fetch_games_by_exact_name(name, suppress_errors=False)
        except IGDBRequestFailure as exc:
            raise _MediaFetchError(f"IGDB exact-name lookup failed for {name!r}") from exc
        distinct = {game.igdb_id for game in matches}
        if len(distinct) != 1:
            if len(distinct) > 1:
                logger.info(
                    "IGDB media name lookup for %r is ambiguous (%s) — refusing to guess",
                    name,
                    sorted(distinct),
                )
            return None
        return {"igdb_id": distinct.pop()}

    payload = await _cached(
        _name_cache_key(name), fetch, ttl=NAME_CACHE_TTL, label=f"name {name!r}"
    )
    if payload is _FETCH_FAILED:
        if errors is not None:
            errors.append("igdb: name resolution failed")
        return None
    if isinstance(payload, dict):
        igdb_id = payload.get("igdb_id")
        if isinstance(igdb_id, int):
            return igdb_id
    return None


# ── Public entry point ───────────────────────────────────────────────────────


async def get_game_media(
    *,
    steam_appid: int | None = None,
    igdb_id: int | None = None,
    name: str | None = None,
) -> dict | None:
    """Media for one game: ``{"media", "similar_raw", "similar_count",
    "pedigree_raw", "igdb_id", "errors"}``.

    Identity order is Steam appid, then IGDB id, then an exact-name IGDB
    resolution. The MEDIA block is whole-source, never mixed — but similar
    games and pedigree exist only on IGDB, so a Steam-sourced result still
    borrows both from the game's IGDB record when one is reachable; otherwise
    the most common candidates (Steam appids) would never get a similar row or
    a studio strip at all.

    Never raises for a provider failure, but does not hide one either:
    ``errors`` names each source whose fetch FAILED (as opposed to genuinely
    holding nothing), so the package's failure reporting can tell an outage
    from a media-less game. Returns None only when nothing resolves, nothing
    failed, and no source holds media.
    """
    errors: list[str] = []

    async def steam_side() -> dict | None:
        if steam_appid is None:
            return None
        payload = await _cached(
            _cache_key("steam", steam_appid),
            lambda: _fetch_steam_media(steam_appid),
            ttl=MEDIA_CACHE_TTL,
            label=f"steam appid {steam_appid}",
        )
        if payload is _FETCH_FAILED:
            errors.append("steam: fetch failed")
            return None
        return payload

    async def igdb_side() -> tuple[dict | None, int | None]:
        resolved = igdb_id
        if resolved is None and name:
            resolved = await _resolve_igdb_id_by_name(name, errors)
        if resolved is None:
            return None, None
        query_id = resolved
        payload = await _cached(
            _cache_key("igdb", query_id),
            lambda: _fetch_igdb_media(query_id),
            ttl=MEDIA_CACHE_TTL,
            label=f"igdb {query_id}",
        )
        if payload is _FETCH_FAILED:
            errors.append("igdb: fetch failed")
            return None, resolved
        if isinstance(payload, dict):
            partial = payload.pop("partial", None)
            if partial:
                errors.append(str(partial))
        return payload, resolved

    # Concurrent on purpose: both sides sit inside the callers' 8s budget, and
    # awaiting IGDB first (one request allows 15s plus retries) could burn the
    # whole budget before the PREFERRED Steam source was even attempted.
    steam_payload, (igdb_payload, resolved_igdb_id) = await asyncio.gather(
        steam_side(), igdb_side()
    )

    if steam_payload is not None:
        merged = (
            {
                **steam_payload,
                "similar_raw": igdb_payload.get("similar_raw"),
                "similar_count": igdb_payload.get("similar_count"),
                "pedigree_raw": igdb_payload.get("pedigree_raw"),
                "igdb_id": igdb_payload.get("igdb_id"),
            }
            if igdb_payload is not None
            else steam_payload
        )
        return {**merged, "errors": errors}
    if igdb_payload is not None:
        return {**igdb_payload, "errors": errors}
    if errors:
        # Everything reachable failed (or the one source did): an empty-handed
        # answer that still SAYS so, rather than the None a media-less game
        # legitimately earns. Assembled fresh, never cached.
        return {
            "media": None,
            "similar_raw": None,
            "similar_count": None,
            "pedigree_raw": None,
            "igdb_id": resolved_igdb_id if resolved_igdb_id is not None else igdb_id,
            "errors": errors,
        }
    return None
