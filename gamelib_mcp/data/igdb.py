"""IGDB (Twitch) API client — game identity resolution with tags, genres, release dates."""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self, TypeVar
from weakref import WeakKeyDictionary

import httpx

from . import provider_health
from .content import (
    CONTENT_DLC,
    CONTENT_EDITION,
    CONTENT_EXPANSION,
    NESTED_CONTENT_TYPES,
    classify_igdb_game,
    classify_title_override,
    content_type_from_igdb_category,
    derive_is_primary,
)
from .db import (
    _claim_cutoff_iso,
    claim_game_ids_for_igdb,
    get_meta,
    load_games_for_igdb_backfill,
    load_platforms_for_games,
    release_game_claim,
    set_meta,
    upsert_game_platform_enrichment,
)
from .tag_synonyms import canonical_tag
from .tags import is_feature_flag
from .title_normalization import (
    ampersand_alternate,
    is_non_game_title,
    match_key,
    normalize_catalog_title,
    normalize_edition_comparison_title,
    normalize_search_text,
    normalize_series_gap_title,
    normalize_strict_edition_title,
)

logger = logging.getLogger(__name__)

_ChunkItem = TypeVar("_ChunkItem")

# Cap on the merged SteamSpy(≤20) + IGDB(≤30) tag cloud, to bound bloat in
# get_game_detail and tag_affinity. Existing (SteamSpy, vote-ranked) tags are kept
# preferentially; IGDB fills the remaining slots.
MERGED_TAG_CAP = 30

# Which generation of the name resolver wrote a row's igdb_cached_at stamp.
#
# CONTRACT: bump this whenever a MATCHING rule changes — the match key, the
# name gate, the tiebreak, the query ladder, anything that could turn a refusal
# into a link. Every row an older generation stamped with NO link is then
# automatically re-queued by claim_game_ids_for_igdb; LINKED rows are never
# touched by a bump (their version is not even read). Nothing else needs to
# happen: no migration, no SQL guess at which titles moved.
#
# Generations:
#   1 — the pre-``match_key`` resolver (six ad-hoc normalizations).
#   2 — one ``match_key`` + the year-aware tiebreak in _select_best_match.
#   3 — IGDB ``alternative_names`` read by the gate (abbreviations and
#       regional titles: "GTA V" -> "Grand Theft Auto V"); GOG product ids
#       resolved through external_games alongside Steam appids; "versus"
#       folded to "vs" in match_key; and the gate's edition-stripped tier
#       widened from ``normalize_series_gap_title`` to
#       ``normalize_edition_comparison_title`` ("Watch Dogs: Day One Edition"
#       reaches "Watch Dogs"), which the year rules make safe.
#   4 — the external_games store mapping moved off the retired `category`
#       filter onto `external_game_source` (the deprecated field had stopped
#       matching, so the authoritative store->game step silently returned
#       nothing), and the exact-name lookup became case-insensitive
#       (``name ~ "..."`` — library rows carry storefront casing, "DARK SOULS
#       III" vs IGDB's "Dark Souls III"). Both turn refusals into links, so
#       every generation-3 no-match re-queues.
IGDB_RESOLVER_VERSION = 4


def _merge_igdb_tags(existing: list[str], igdb_tags: list[str]) -> list[str]:
    """Union existing tags with IGDB tags, canonicalized, existing-first, capped.

    Existing tags (SteamSpy community tags, vote-ranked) keep their order and
    priority; IGDB themes/keywords append only when not already present. Feature
    flags are filtered out so they never reach tag_affinity.
    """
    seen: set[str] = set()
    result: list[str] = []
    for t in list(existing) + list(igdb_tags):
        if is_feature_flag(t):
            continue
        c = canonical_tag(t)
        if c and c not in seen:
            seen.add(c)
            result.append(c)
        if len(result) >= MERGED_TAG_CAP:
            break
    return result

_TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_IGDB_GAMES_URL = "https://api.igdb.com/v4/games"
_IGDB_EXTERNAL_GAMES_URL = "https://api.igdb.com/v4/external_games"

# IGDB external_games.external_game_source for storefront identifier lookups.
# IGDB deprecated external_games.`category` in favour of `external_game_source`
# (migration window Feb 18 -> Aug 31; the old field names are removed after it)
# — same numeric enum, so Steam is still 1 and GOG still 5. The old field had
# already stopped answering in prod: a backfill stamped 904 owned rows
# "checked, no match" while 448 of them carried a Steam appid IGDB certainly
# maps, and the drift audit found zero store-authoritative links across 46
# mismatches. Every external_games query in this module filters on
# `external_game_source`; the deprecated field name is referenced nowhere.
IGDB_EXTERNAL_SOURCE_STEAM = 1
IGDB_EXTERNAL_SOURCE_GOG = 5

# Long-standing public names for the same two values, kept because callers
# outside this module (and the tests) spell them this way. They are
# external_game_source values, not the retired `category` ones.
IGDB_EXTERNAL_CATEGORY_STEAM = IGDB_EXTERNAL_SOURCE_STEAM
IGDB_EXTERNAL_CATEGORY_GOG = IGDB_EXTERNAL_SOURCE_GOG


def igdb_credentials_configured() -> bool:
    """True only when both IGDB/Twitch credentials are present.

    ``_get_token()`` requires client id *and* secret and raises EnvironmentError
    otherwise, so any caller that gates on "is IGDB configured" must check both —
    a half-configured env (id set, secret missing) must read as unconfigured
    rather than crash.
    """
    return bool(os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET"))

# IGDB platform IDs
IGDB_PLATFORM_PC = 6
IGDB_PLATFORM_PS5 = 167
IGDB_PLATFORM_PS4 = 48
IGDB_PLATFORM_SWITCH = 130  # Switch
IGDB_PLATFORM_SWITCH2 = 508  # Nintendo Switch 2 (IGDB added it post-launch; verified 2026-07-03)
IGDB_PLATFORM_XBOX = 169  # Xbox Series X|S; the newest family id, same style as ps5's 167

# Our platform value → IGDB platform ID (primary id; single-id platforms only —
# for switch2, which spans two IGDB platforms, use PLATFORM_TO_IGDB_ANY).
PLATFORM_TO_IGDB: dict[str, int] = {
    "steam": IGDB_PLATFORM_PC,
    "epic": IGDB_PLATFORM_PC,
    "gog": IGDB_PLATFORM_PC,
    "ps5": IGDB_PLATFORM_PS5,
    "switch2": IGDB_PLATFORM_SWITCH,
    "xbox": IGDB_PLATFORM_XBOX,
}

# Our platform value → all IGDB platform ids that count as it, preference-
# ordered: the first id with a release date wins in
# upsert_backfill_platform_release_dates. switch2 covers both generations
# (native Switch 2 SKUs + the backward-compatible Switch library).
PLATFORM_TO_IGDB_ANY: dict[str, tuple[int, ...]] = {
    "steam": (IGDB_PLATFORM_PC,),
    "epic": (IGDB_PLATFORM_PC,),
    "gog": (IGDB_PLATFORM_PC,),
    "ps5": (IGDB_PLATFORM_PS5,),
    "switch2": (IGDB_PLATFORM_SWITCH2, IGDB_PLATFORM_SWITCH),
    "xbox": (IGDB_PLATFORM_XBOX,),
}

# Reverse map for availability checks (games.igdb_platforms → our platforms).
# PC deliberately maps to "steam": it's the only PC storefront with a price
# source, which is all this map is consumed for (tools/deals.py).
IGDB_TO_PLATFORM: dict[int, str] = {
    IGDB_PLATFORM_PC: "steam",
    IGDB_PLATFORM_PS5: "ps5",
    IGDB_PLATFORM_SWITCH: "switch2",
    IGDB_PLATFORM_SWITCH2: "switch2",
    IGDB_PLATFORM_XBOX: "xbox",
}

# IGDB category values
CATEGORY_MAIN_GAME = 0
CATEGORY_DLC = 1
CATEGORY_EXPANSION = 2
CATEGORY_BUNDLE = 3
CATEGORY_STANDALONE_EXPANSION = 4
CATEGORY_MOD = 5
CATEGORY_EPISODE = 6
CATEGORY_SEASON = 7
CATEGORY_REMAKE = 8
CATEGORY_REMASTER = 9
CATEGORY_EXPANDED_GAME = 10
CATEGORY_PORT = 11

# Cached token
_token: str | None = None
_token_expires_at: datetime = datetime.min.replace(tzinfo=UTC)

_IGDB_TARGET_REQUEST_INTERVAL = 1 / 3
_IGDB_MAX_REQUESTS_PER_SECOND = 4
_IGDB_MAX_IN_FLIGHT_REQUESTS = 4
_IGDB_MAX_RETRIES = 3
_IGDB_RETRY_BASE_DELAY_SECONDS = 0.5
_IGDB_RETRY_JITTER_SECONDS = 0.25
_IGDB_REQUEST_TIMEOUT_SECONDS = 15


class _IGDBRequestGate:
    """Shared gate that paces request starts and caps concurrent IGDB requests."""

    def __init__(
        self,
        *,
        target_interval: float,
        max_requests_per_second: int,
        max_in_flight: int,
    ) -> None:
        self._target_interval = target_interval
        self._max_requests_per_second = max_requests_per_second
        self._max_in_flight = max_in_flight
        self._loop_states: WeakKeyDictionary[asyncio.AbstractEventLoop, _IGDBRequestGateState] = WeakKeyDictionary()
        self._lease_stack: ContextVar[tuple[_IGDBRequestGateState, ...]] = ContextVar(
            "igdb_request_gate_lease_stack",
            default=(),
        )

    def _get_loop_state(self) -> "_IGDBRequestGateState":
        loop = asyncio.get_running_loop()
        state = self._loop_states.get(loop)
        if state is None:
            state = _IGDBRequestGateState(
                lock=asyncio.Lock(),
                semaphore=asyncio.Semaphore(self._max_in_flight),
            )
            self._loop_states[loop] = state
        return state

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self.release()
        return False

    async def acquire(self) -> None:
        state = self._get_loop_state()
        await state.semaphore.acquire()

        try:
            while True:
                wait_seconds = 0.0
                async with state.lock:
                    now = time.monotonic()
                    cutoff = now - 1.0
                    while state.request_started_at and state.request_started_at[0] <= cutoff:
                        state.request_started_at.popleft()

                    wait_seconds = max(0.0, state.next_slot_at - now)
                    if len(state.request_started_at) >= self._max_requests_per_second:
                        oldest = state.request_started_at[0]
                        wait_seconds = max(wait_seconds, (oldest + 1.0) - now)

                    if wait_seconds <= 0:
                        state.request_started_at.append(now)
                        state.next_slot_at = max(state.next_slot_at, now) + self._target_interval
                        lease_stack = self._lease_stack.get()
                        self._lease_stack.set((*lease_stack, state))
                        return

                await asyncio.sleep(wait_seconds)
        except BaseException:
            state.semaphore.release()
            raise

    async def backoff(self, delay_seconds: float) -> None:
        if delay_seconds <= 0:
            return

        state = self._get_loop_state()
        async with state.lock:
            state.next_slot_at = max(state.next_slot_at, time.monotonic() + delay_seconds)

    def release(self) -> None:
        lease_stack = self._lease_stack.get()
        if not lease_stack:
            raise RuntimeError("IGDB request gate released without matching acquire")

        state = lease_stack[-1]
        self._lease_stack.set(lease_stack[:-1])
        state.semaphore.release()


@dataclass
class _IGDBRequestGateState:
    lock: asyncio.Lock
    semaphore: asyncio.Semaphore
    request_started_at: deque[float] = field(default_factory=deque)
    next_slot_at: float = 0.0


_IGDB_REQUEST_GATE = _IGDBRequestGate(
    target_interval=_IGDB_TARGET_REQUEST_INTERVAL,
    max_requests_per_second=_IGDB_MAX_REQUESTS_PER_SECOND,
    max_in_flight=_IGDB_MAX_IN_FLIGHT_REQUESTS,
)

_IGDB_LINK_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]] = WeakKeyDictionary()
_FALLBACK_TITLE_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = WeakKeyDictionary()


class IGDBRequestFailure(RuntimeError):
    """Raised when IGDB request retries are exhausted or credentials fail operationally."""


def _get_igdb_link_lock(igdb_id: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    loop_locks = _IGDB_LINK_LOCKS.get(loop)
    if loop_locks is None:
        loop_locks = {}
        _IGDB_LINK_LOCKS[loop] = loop_locks

    lock = loop_locks.get(igdb_id)
    if lock is None:
        lock = asyncio.Lock()
        loop_locks[igdb_id] = lock
    return lock


def _get_fallback_title_lock(name: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    loop_locks = _FALLBACK_TITLE_LOCKS.get(loop)
    if loop_locks is None:
        loop_locks = {}
        _FALLBACK_TITLE_LOCKS[loop] = loop_locks

    normalized_name = name.casefold()
    lock = loop_locks.get(normalized_name)
    if lock is None:
        lock = asyncio.Lock()
        loop_locks[normalized_name] = lock
    return lock


# How many IGDB alternative names one record contributes to the gate and to
# the persisted aliases. IGDB holds dozens for some franchises (every regional
# and marketing spelling); a handful is what carries the abbreviation ("GTA V")
# and the regional title, and the rest is noise in both the gate and the alias
# table.
ALTERNATIVE_NAME_CAP = 12


@dataclass
class IGDBGame:
    igdb_id: int
    name: str
    category: int
    first_release_date: str | None  # ISO date string YYYY-MM-DD
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)   # themes + keywords
    platform_release_dates: dict[int, str] = field(default_factory=dict)  # igdb_platform_id → ISO date
    platforms: list[int] = field(default_factory=list)  # all IGDB platform ids the game is released on
    # Series groupings as (kind, igdb_id, name) tuples; kind is
    # "collection" (IGDB's term for a "Series") or "franchise".
    series: list[tuple[str, int, str]] = field(default_factory=list)
    game_type: int | None = None
    content_type: str = "base_game"
    parent_igdb_id: int | None = None
    parent_name: str | None = None
    version_parent_igdb_id: int | None = None
    version_parent_name: str | None = None
    version_title: str | None = None
    is_primary_library_item: bool = True
    alias_for_parent: bool = False
    cover_image_id: str | None = None  # images.igdb.com URL slug
    # IGDB's own alternative spellings for this record — abbreviations ("GTA
    # V"), regional titles, subtitle variants. Never includes the primary
    # ``name``. The resolver gate reads them (an abbreviation exists NOWHERE
    # else, so no normalization rule could ever bridge it) and
    # ``_apply_igdb_metadata`` persists them as library-side aliases.
    alternative_names: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # The alternative-name contract is enforced HERE and nowhere else:
        # stripped, case-insensitively deduplicated, never the primary name,
        # capped at ALTERNATIVE_NAME_CAP. Doing it in the dataclass means a
        # hand-built record (tests, a future caller that isn't the parser) is
        # bounded exactly like a parsed one, and no consumer has to re-apply
        # the cap — the gate and the alias writer both read the field raw.
        primary = (self.name or "").strip().casefold()
        seen = {primary} if primary else set()
        cleaned: list[str] = []
        for alt in self.alternative_names:
            value = (alt or "").strip()
            key = value.casefold()
            if not value or key in seen:
                continue
            seen.add(key)
            cleaned.append(value)
            if len(cleaned) >= ALTERNATIVE_NAME_CAP:
                break
        self.alternative_names = cleaned


async def _get_token() -> str:
    """Return a valid Twitch OAuth2 access token, refreshing if needed."""
    global _token, _token_expires_at

    now = datetime.now(UTC)
    if _token and now < _token_expires_at - timedelta(minutes=10):
        return _token

    client_id = os.environ.get("TWITCH_CLIENT_ID")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise OSError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be set for IGDB enrichment")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _TWITCH_TOKEN_URL,
                params={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        # Never let the raw error escape: httpx puts the full request URL in
        # its message, and this one carries the client secret in its query
        # string. Every log line and health counter downstream repr()s the
        # exception it was handed.
        raise IGDBRequestFailure(
            f"Twitch token request failed: HTTP {exc.response.status_code}"
        ) from None
    except httpx.HTTPError as exc:
        raise IGDBRequestFailure(
            f"Twitch token request failed: {type(exc).__name__}"
        ) from None

    _token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    _token_expires_at = now + timedelta(seconds=expires_in)
    return _token


def _unix_to_iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=UTC).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _parse_retry_after(retry_after: str | None) -> float | None:
    if not retry_after:
        return None

    try:
        return max(0.0, float(retry_after))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None

    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)

    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _retry_delay_seconds(attempt: int, response: httpx.Response | None = None) -> float:
    retry_after = _parse_retry_after(response.headers.get("Retry-After") if response else None)
    if retry_after is not None:
        return retry_after

    backoff = _IGDB_RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
    return backoff + random.uniform(0.0, _IGDB_RETRY_JITTER_SECONDS)


async def _sleep_before_retry(delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)


def _should_retry(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or 500 <= exc.response.status_code < 600

    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


async def _post_igdb_games(
    query: str, headers: dict[str, str], url: str = _IGDB_GAMES_URL
) -> list[dict]:
    last_error: Exception | None = None

    for attempt in range(_IGDB_MAX_RETRIES + 1):
        try:
            async with (
                _IGDB_REQUEST_GATE,
                httpx.AsyncClient(timeout=_IGDB_REQUEST_TIMEOUT_SECONDS) as client,
            ):
                resp = await client.post(
                    url,
                    content=query,
                    headers=headers,
                )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_error = exc
            if attempt >= _IGDB_MAX_RETRIES or not _should_retry(exc):
                status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                logger.warning(
                    "IGDB search exhausted retries after %s attempts%s: %s",
                    attempt + 1,
                    f" status={status_code}" if status_code is not None else "",
                    exc,
                )
                raise

            response = exc.response if isinstance(exc, httpx.HTTPStatusError) else None
            delay_seconds = _retry_delay_seconds(attempt, response)
            await _IGDB_REQUEST_GATE.backoff(delay_seconds)
            await _sleep_before_retry(delay_seconds)

    if last_error is not None:
        raise last_error
    return []


def _escape_igdb_search_term(term: str) -> str:
    return term.replace("\\", "\\\\").replace('"', '\\"')


def _build_search_game_query(
    name: str, igdb_platform_id: int | tuple[int, ...] | None = None
) -> str:
    escaped_name = _escape_igdb_search_term(name)
    filters = []
    if igdb_platform_id is not None:
        ids = igdb_platform_id if isinstance(igdb_platform_id, tuple) else (igdb_platform_id,)
        if len(ids) == 1:
            filters.append(f"platforms = {ids[0]}")
        else:
            # Apicalypse: (a,b) = "contains at least one of".
            filters.append(f"platforms = ({','.join(str(i) for i in ids)})")
    clauses = [
        (
            "fields id, name, alternative_names.name, category, game_type, "
            "first_release_date, "
            "genres.name, themes.name, keywords.name, "
            "collections.id, collections.name, franchises.id, franchises.name, "
            "parent_game.id, parent_game.name, "
            "version_parent.id, version_parent.name, version_title, cover.image_id, "
            "platforms, release_dates.platform, release_dates.date;"
        ),
        f'search "{escaped_name}";',
    ]
    if filters:
        clauses.append(f"where {' & '.join(filters)};")
    # IGDB's own relevance ranking can bury the base game behind a pile of
    # same-franchise DLC/cosmetic packs (e.g. "Persona 3 Reload" appeared at
    # position 11 of 20 candidates, behind 10 "Persona Set"/"BGM Set" packs).
    # A limit of 5 truncated before the real match ever appeared.
    clauses.append("limit 20;")
    return " ".join(clauses)


def _parse_alternative_names(item: dict) -> list[str]:
    """The raw ``alternative_names`` strings off one IGDB item.

    Extraction only — stripping, deduplication, dropping the primary name and
    the cap all happen once, in ``IGDBGame.__post_init__``.
    """
    return [
        entry["name"]
        for entry in item.get("alternative_names") or []
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]


def _parse_igdb_item(item: dict) -> IGDBGame:
    """Convert a raw IGDB `games` endpoint item into an ``IGDBGame``.

    Shared by ``search_game`` and ``fetch_game_by_id`` so the field-parsing and
    content-classification logic (including the category/game_type fallback)
    isn't duplicated between the search and by-id fetch paths.
    """
    category = item.get("category")
    game_type = item.get("game_type")
    # IGDB has effectively migrated `category` -> `game_type` for some titles
    # (same numeric enum values); category comes back None while game_type is
    # populated. Fall back so downstream consumers of IGDBGame.category see a
    # coherent value instead of a mislabeled base-game default.
    effective_category = category if category is not None else game_type

    genres = [g["name"] for g in item.get("genres") or []]
    alternative_names = _parse_alternative_names(item)
    themes = [t["name"] for t in item.get("themes") or []]
    keywords = [k["name"] for k in item.get("keywords") or []]
    tags = list(dict.fromkeys(themes + keywords))[:30]  # deduplicate, cap at 30

    platform_dates: dict[int, str] = {}
    for rd in item.get("release_dates") or []:
        pid = rd.get("platform")
        date_ts = rd.get("date")
        if pid and date_ts:
            iso = _unix_to_iso(date_ts)
            if iso:
                platform_dates[pid] = iso

    platform_ids = sorted(
        {int(p) for p in item.get("platforms") or [] if isinstance(p, int)}
        | set(platform_dates)
    )

    series: list[tuple[str, int, str]] = []
    for kind, key in (("collection", "collections"), ("franchise", "franchises")):
        for entry in item.get(key) or []:
            sid = entry.get("id")
            sname = entry.get("name")
            if sid and sname:
                series.append((kind, sid, sname))

    raw_parent_game = item.get("parent_game")
    parent_game = raw_parent_game if isinstance(raw_parent_game, dict) else {}
    raw_version_parent = item.get("version_parent")
    version_parent = raw_version_parent if isinstance(raw_version_parent, dict) else {}
    classification = classify_igdb_game(
        title=item["name"],
        category=category,
        game_type=game_type,
        parent_name=parent_game.get("name"),
        parent_igdb_id=parent_game.get("id"),
        version_parent_name=version_parent.get("name"),
        version_parent_igdb_id=version_parent.get("id"),
    )

    return IGDBGame(
        igdb_id=item["id"],
        name=item["name"],
        category=effective_category if effective_category is not None else CATEGORY_MAIN_GAME,
        first_release_date=_unix_to_iso(item.get("first_release_date")),
        genres=genres,
        tags=tags,
        platform_release_dates=platform_dates,
        platforms=platform_ids,
        series=series,
        game_type=game_type,
        content_type=classification.content_type,
        parent_igdb_id=classification.parent_igdb_id,
        parent_name=classification.parent_name,
        version_parent_igdb_id=version_parent.get("id"),
        version_parent_name=version_parent.get("name"),
        version_title=item.get("version_title"),
        is_primary_library_item=classification.is_primary_library_item,
        alias_for_parent=classification.alias_for_parent,
        cover_image_id=(
            (item.get("cover") or {}).get("image_id")
            if isinstance(item.get("cover"), dict)
            else None
        ),
        alternative_names=alternative_names,
    )


async def search_game(
    name: str,
    igdb_platform_id: int | tuple[int, ...] | None = None,
    *,
    suppress_errors: bool = True,
) -> list[IGDBGame]:
    """
    Search IGDB for a game by name, optionally filtered to a platform.
    Returns up to `limit` matches (see ``_build_search_game_query``) ranked by
    IGDB's own relevance model.

    Missing credentials are an *operational* condition, not "not found": with
    ``suppress_errors=False`` they raise ``IGDBRequestFailure`` so callers that
    persist no-match markers (the backfill) can never cache a creds outage as a
    permanent no-match. (Prod 2026-07-05: a deploy briefly ran without
    TWITCH_CLIENT_ID and 800+ owned games were marked "checked, no match".)
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not client_id:
        if not suppress_errors:
            raise IGDBRequestFailure(
                f"IGDB credentials not configured; cannot search for {name!r}"
            )
        return []

    query = _build_search_game_query(name, igdb_platform_id)

    try:
        token = await _get_token()
        results = await _post_igdb_games(
            query,
            headers={
                "Client-ID": client_id,
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/plain",
            },
        )
    except Exception as exc:
        if not suppress_errors:
            raise IGDBRequestFailure(f"IGDB search failed for {name!r}") from exc
        logger.warning("IGDB search failed for %r: %s", name, exc)
        return []

    return [_parse_igdb_item(item) for item in results]


def _build_exact_name_query(
    name: str, igdb_platform_id: int | tuple[int, ...] | None = None
) -> str:
    """A case-insensitive equality lookup on games.name — no search index.

    Apicalypse's ``=`` on a string is CASE-SENSITIVE, and library rows carry
    storefront casing that IGDB does not ("DARK SOULS III" on Steam vs IGDB's
    "Dark Souls III", "Ori and the Blind Forest" vs "Ori and the Blind
    Forest"), so the equality rung refused titles IGDB demonstrably holds.
    ``~`` is the case-insensitive comparison; with no ``*`` wildcards it is
    still EQUALITY, not a contains match, so the rung keeps its "cannot match a
    different title by construction" property.
    """
    escaped_name = _escape_igdb_search_term(name)
    filters = [f'name ~ "{escaped_name}"']
    if igdb_platform_id is not None:
        ids = igdb_platform_id if isinstance(igdb_platform_id, tuple) else (igdb_platform_id,)
        if len(ids) == 1:
            filters.append(f"platforms = {ids[0]}")
        else:
            filters.append(f"platforms = ({','.join(str(i) for i in ids)})")
    return " ".join(
        [
            _FETCH_BY_ID_FIELDS,
            f"where {' & '.join(filters)};",
            # Ambiguity is resolved by refusing, not by ranking, so a handful
            # of rows is all we need to detect it.
            "limit 10;",
        ]
    )


async def fetch_games_by_exact_name(
    name: str,
    igdb_platform_id: int | tuple[int, ...] | None = None,
    *,
    suppress_errors: bool = True,
) -> list[IGDBGame]:
    """Games whose IGDB name EQUALS ``name`` (case-insensitive on IGDB's side).

    IGDB's ``search`` endpoint is a relevance index and it does fail outright
    on titles it demonstrably holds: prod searches for "The Forest", "The Gunk"
    and "The Invincible" all returned zero while IGDB stored those exact names
    (7504, 136000, 138906). This is the deterministic alternative — an equality
    filter on the games endpoint — used only after search has come up empty.
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not client_id:
        if not suppress_errors:
            raise IGDBRequestFailure(
                f"IGDB credentials not configured; cannot look up {name!r}"
            )
        return []

    try:
        token = await _get_token()
        results = await _post_igdb_games(
            _build_exact_name_query(name, igdb_platform_id),
            headers=_igdb_headers(client_id, token),
        )
    except Exception as exc:
        if not suppress_errors:
            raise IGDBRequestFailure(f"IGDB exact-name lookup failed for {name!r}") from exc
        logger.warning("IGDB exact-name lookup failed for %r: %s", name, exc)
        return []

    return [_parse_igdb_item(item) for item in results]


_FETCH_BY_ID_FIELDS = (
    "fields id, name, alternative_names.name, category, game_type, "
    "first_release_date, "
    "genres.name, themes.name, keywords.name, "
    "collections.id, collections.name, franchises.id, franchises.name, "
    "parent_game.id, parent_game.name, "
    "version_parent.id, version_parent.name, version_title, cover.image_id, "
    "platforms, release_dates.platform, release_dates.date;"
)


async def fetch_game_by_id(
    igdb_id: int, *, suppress_errors: bool = True
) -> IGDBGame | None:
    """Fetch a single IGDB game by its id (no fuzzy search involved).

    Used by the backfill path when a row already has a matched `igdb_id`, so a
    known-correct link is never re-resolved through name search (which can
    drift onto the wrong candidate). Returns None if IGDB is unconfigured, the
    id doesn't resolve, or the request ultimately fails while
    ``suppress_errors`` is True. With ``suppress_errors=False`` missing
    credentials raise ``IGDBRequestFailure`` (see ``search_game``).
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not client_id:
        if not suppress_errors:
            raise IGDBRequestFailure(
                f"IGDB credentials not configured; cannot fetch igdb_id {igdb_id!r}"
            )
        return None

    query = f"{_FETCH_BY_ID_FIELDS} where id = {igdb_id}; limit 1;"

    try:
        token = await _get_token()
        results = await _post_igdb_games(
            query,
            headers={
                "Client-ID": client_id,
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/plain",
            },
        )
    except Exception as exc:
        if not suppress_errors:
            raise IGDBRequestFailure(f"IGDB fetch-by-id failed for {igdb_id!r}") from exc
        logger.warning("IGDB fetch-by-id failed for %r: %s", igdb_id, exc)
        return None

    if not results:
        return None
    return _parse_igdb_item(results[0])


# Zero-result fallback ladder (Fix 4): query-variant patterns local to
# resolve_game. Deliberately NOT folded into title_normalization.py's shared
# _TRAILING_VARIANT_PATTERNS — those also feed library identity matching, and
# a generic edition-strip rule there could collapse distinct games. Here they
# only ever widen an IGDB *search* that already returned zero results, and any
# hit they turn up still has to clear the same identity/fuzzy gate as a normal
# search.
_LADDER_TRAILING_EDITION_PATTERN = re.compile(r"[:\-]?\s*\S+\s+Edition\s*$", re.IGNORECASE)
_LADDER_LEADING_THE_PATTERN = re.compile(r"^The\s+", re.IGNORECASE)
_LADDER_STOPWORDS = {"the", "of", "a", "an", "and", "for"}


def _generate_resolve_query_variants(name: str) -> list[tuple[str, bool]]:
    """Ordered, deduplicated (query, identity_preserving) variants to retry.

    Only consulted when the original name (with and without a platform
    filter) returned zero IGDB results.

    ``identity_preserving`` marks variants produced by transformations that
    keep the title's series identity intact (catalog normalization, stripping
    a trailing edition segment). Their results are gated against the *variant*
    rather than the original name — otherwise a numbered edition like "Sea of
    Thieves: 2026 Edition" could never match the base game, because the
    original's "2026" reads as a sequel number in
    ``titles_conflict_on_identity``. Variants that change what the query means
    stay gated against the original.

    Dropping a leading article is one of those meaning-changing variants, NOT
    an identity-preserving one: "The Forest", "The Surge", "The Hex" and "The
    Gunk" all have unrelated IGDB entries named "Forest"/"Surge"/"Hex"/"Gunk",
    and gating against the stripped variant bound eight prod rows to the wrong
    game. It stays in the ladder purely as a query widener — a search for
    "Forest" can still surface "The Forest" — but its hits must clear the gate
    against the ORIGINAL title, article included.

    The ampersand swap leads the ladder and IS identity-preserving: the two
    spellings name one game (Steam "Rabbit and Steel" / IGDB "Rabbit & Steel"),
    and gating against the VARIANT is what lets IGDB's spelling through — the
    gate compares edition-stripped titles, and while it now folds "&" into
    "and" on both sides, gating the rung against its own query keeps the rung
    meaningful for a search that only answers to the alternate spelling.
    """
    variants: list[tuple[str, bool]] = []
    seen = {name.casefold()}

    def _add(candidate: str | None, *, identity_preserving: bool) -> None:
        candidate = (candidate or "").strip()
        key = candidate.casefold()
        if candidate and key not in seen:
            seen.add(key)
            variants.append((candidate, identity_preserving))

    _add(ampersand_alternate(name), identity_preserving=True)
    _add(normalize_catalog_title(name), identity_preserving=True)
    _add(_LADDER_TRAILING_EDITION_PATTERN.sub("", name).strip(), identity_preserving=True)
    _add(_LADDER_LEADING_THE_PATTERN.sub("", name).strip(), identity_preserving=False)

    tokens = [t for t in re.findall(r"\S+", name) if t.strip(",:;").casefold() not in _LADDER_STOPWORDS]
    if tokens:
        _add(" ".join(tokens), identity_preserving=False)
        if len(tokens) > 2:
            _add(" ".join(tokens[-2:]), identity_preserving=False)

    return variants


def _igdb_name_agrees(library_name: str, igdb_name: str) -> bool:
    """Whether an IGDB record's name vouches for a library row's identity.

    The same two-step the drift audit uses: edition-stripped equality, then
    the wider edition-comparison normalization, so "Nioh 2 - The Complete
    Edition" agrees with "Nioh 2" while "FTL: Faster Than Light" does not
    agree with "Faster than light?".
    """
    if normalize_series_gap_title(library_name) == normalize_series_gap_title(igdb_name):
        return True
    return normalize_edition_comparison_title(
        library_name
    ) == normalize_edition_comparison_title(igdb_name)


def _candidate_names(game: IGDBGame) -> list[str]:
    """Every spelling IGDB holds for a candidate, primary name first.

    An abbreviation ("GTA V" for "Grand Theft Auto V") or a regional title is
    written down in no normalization rule and in no storefront field — IGDB's
    ``alternative_names`` is the only place it exists, so the gate has to read
    them or those rows can never link.
    """
    return [game.name, *game.alternative_names]


def _candidate_year(game: IGDBGame) -> int | None:
    """Release year of an IGDB candidate, or None when IGDB has no date."""
    head = (game.first_release_date or "").strip()[:4]
    return int(head) if head.isdigit() else None


def _year_debug(results: list[IGDBGame], indexes: list[int]) -> list[tuple[int, int | None]]:
    return [(results[i].igdb_id, _candidate_year(results[i])) for i in indexes]


def _select_best_match(
    name: str,
    results: list[IGDBGame],
    *,
    allow_inconclusive_fallback: bool,
    reference_year: int | None = None,
    generic_edition_query: bool = False,
) -> IGDBGame | None:
    """Pick the best candidate from `results` for query `name`, or None.

    Never collapses onto a different entry in the same series: "Xenoblade
    Chronicles" must not resolve to "Xenoblade Chronicles 2". Candidates whose
    sequel/version identity conflicts with the query are dropped before
    ranking.

    A candidate is compared under EVERY name IGDB holds for it — its primary
    title and its ``alternative_names`` — because an abbreviation ("GTA V" for
    "Grand Theft Auto V") or a regional title exists nowhere else and no
    normalization rule could invent it. The names are filtered one by one:
    those that conflict with the query on identity are discarded, a candidate
    is dropped only when every one of its spellings conflicts, and the gate
    (and tier 1) then ask whether ANY surviving spelling matches. So an
    alternative name can never smuggle in a sequel the primary name would have
    refused.

    `allow_inconclusive_fallback` controls what happens when the fuzzy match
    is inconclusive (score below cutoff for every candidate): the original,
    IGDB-relevance-ranked search can fall back to its top identity-compatible
    hit, but a narrower fallback-ladder variant query (Fix 4) must not — an
    unrelated result from an overly-broad query (e.g. "Seance" alone matching
    unrelated "Silly Seance"-type titles) must not be accepted just because it
    was the only one returned.

    Whatever the selection path, the final candidate must clear the strict
    name gate: one of its edition-stripped normalized names has to EQUAL the
    query's. The strip is ``normalize_edition_comparison_title`` — the broad
    one, which peels a qualifier-anchored tail and the generic "<up to 3
    words> Edition" tail, so "Watch Dogs: Day One Edition" reaches "Watch
    Dogs". It is safe here only because the year rules below guard every
    edition fold (a tier-2 match is a lone one-sided window or a symmetric ±2),
    and because it never strips a subtitle that is not an edition phrase:
    "Halo: The Master Chief Collection" is still not "Halo" and "Persona 5
    Royal" is still not "Persona 5".

    Fuzzy scores and relevance fallbacks only ever rank candidates;
    they can no longer accept one whose name actually differs. This is what
    stops the observed prod disasters — "Borderlands GOTY" enriched as "The
    Tower on the Borderland", "PAYDAY 2" as "Payday 2 VR", "Tales from the
    Borderlands" as "New Tales from the Borderlands" — while edition
    variants ("The Witcher: Enhanced Edition" -> "The Witcher") still pass
    because both sides strip to the same title.

    The gate walks the WHOLE identity-compatible candidate list (selected
    candidate first, primaries preferred among the rest), not just the single
    ranked pick: fuzzy/relevance ranking can put a decorated sibling first
    ("Counter-Strike" -> "Counter-Strike Nexon", "DEFCON" -> "DEFCON VR")
    while a gate-passing candidate sits further down the results. Accepting a
    non-first candidate is safe precisely because the gate demands
    edition-stripped-normalized equality with the query — it cannot land on a
    different game. Only when NO candidate passes does the query store no
    match at all (row stays unenriched; logged at info).

    ``reference_year`` is the library row's own release year (see
    ``_resolve_game_with_status``), and it arbitrates the case the name gate
    cannot: two IGDB records that genuinely share a title. The gate-passing
    candidates split into five bands, ranked by how much the equality is
    worth, and the FIRST non-empty band is considered alone:

      1a  ``match_key`` equality on the candidate's own PRIMARY name — the
          exact title, no stripping.
      1b  the same, reached through one of its alternative names.
      2a  equality under ``normalize_strict_edition_title`` on the primary
          name — a KNOWN edition phrase was stripped ("Game of the Year
          Edition", "Day One Edition").
      2b  the same, through an alternative name.
      3   equality only under the full ``normalize_edition_comparison_title``,
          i.e. the generic "<up to 3 words> Edition" tail did it.

    Tier 1 before tier 2 keeps a remake marketed as an edition from
    outranking the real thing; primary before alternative keeps a coinciding
    working title ("Titan" for "Overwatch") from outranking a record that owns
    the name. Tier 3 is last because it is a GUESS: the generic tail eats
    arbitrary words, so "Minecraft: Education Edition" collapses onto
    "Minecraft" exactly like a real SKU would, and it is a different product.

    Within the chosen tier, with a known reference year: candidates within ±1
    year win in ranked order (platform and region releases drift by months).
    With none inside that window, a LONE tier-1 candidate is still accepted —
    a re-release carries a later store date and the exact title vouches for it
    — while SEVERAL tier-1 candidates refuse, because that is the same-name
    case with no evidence to pick by. A LONE tier-2 candidate gets a ONE-SIDED
    window: an edition cannot predate its own game, so an older or same-age
    candidate is the original and is accepted however far back it sits, and
    only a candidate more than two years NEWER than the row is refused — which
    is what keeps "Mafia" (2002) off "Mafia: Definitive Edition" (2020) while
    letting "Deus Ex: Game of the Year Edition" (store date 2013) reach "Deus
    Ex" (2000). SEVERAL tier-2 candidates keep the symmetric ±2 window.

    With no reference year, two or more distinct candidates whose known years
    disagree by more than a year are refused rather than ranked: that is the
    "Dead Space 2008 vs 2023" shape, and IGDB's own ordering is not evidence.

    TIER 3 does not get any of that. Nothing vouches for its edition reading,
    so the year has to: without a reference year it refuses outright, and with
    one it accepts only inside a SYMMETRIC ±1 window (ties refused like tier
    1). The one-sided "an edition cannot predate its own game" rule is
    deliberately not extended to it — that rule assumes the suffix really is
    an edition, which is the very thing in question. ``generic_edition_query``
    says the QUERY is already a ladder rung that peeled such a tail, so
    whatever it matches is tier-3 strength however exactly it matches.
    """
    from .db import extract_best_fuzzy_key, titles_conflict_on_identity

    # A candidate speaks with every name IGDB holds for it: the primary title
    # plus its alternative names. Names that CONFLICT with the query on
    # sequel/version identity are dropped one by one rather than sinking the
    # whole candidate — an "Alan Wake II" alternative name sitting beside a
    # primary the query agrees with must not disqualify the record — and a
    # candidate is dropped entirely only when EVERY spelling conflicts.
    compatible_names = {
        i: [n for n in _candidate_names(g) if not titles_conflict_on_identity(name, n)]
        for i, g in enumerate(results)
    }
    choices = {i: g.name for i, g in enumerate(results) if compatible_names[i]}
    if not choices:
        # Every candidate disagrees on the sequel number — a confident wrong match
        # is worse than none. Let the caller fall back to the normalized name.
        return None

    # Prefer an exact title match (under the same normalization used for
    # library identity) over IGDB's relevance ranking. This is what rescues
    # e.g. "Persona 3 Reload": the base game's title is an exact match while
    # every DLC/cosmetic pack's title has a longer suffix, so it wins even
    # though IGDB's own relevance model ranked it below all of them. An
    # alternative name counts as an exact title too — "GTA V" is the whole
    # reason the query reached the "Grand Theft Auto V" record.
    normalized_query = match_key(name)
    exact_matches = [
        i
        for i in choices
        if any(match_key(n) == normalized_query for n in compatible_names[i])
    ]
    selected_idx: int | None
    if exact_matches:
        primary_exact = [i for i in exact_matches if results[i].is_primary_library_item]
        selected_idx = primary_exact[0] if primary_exact else exact_matches[0]
    else:
        selected_idx = extract_best_fuzzy_key(name, choices, cutoff=70)
        if selected_idx is None and allow_inconclusive_fallback:
            # Fuzzy was inconclusive; take IGDB's top *identity-compatible*
            # relevance hit rather than forcing position 0 (which may be a
            # conflicting entry). Without the fallback the gate walk below
            # still runs — a candidate that passes strict name equality is
            # acceptable even from a narrow ladder query.
            selected_idx = next(iter(choices))

    # Gate walk: the ranked pick goes first (preserving all prior selection
    # behavior when it passes); the remaining identity-compatible candidates
    # follow in relevance order with primary library items preferred.
    rest = sorted(
        (i for i in choices if i != selected_idx),
        key=lambda i: (not results[i].is_primary_library_item, i),
    )
    ordered = ([selected_idx] if selected_idx is not None else []) + rest
    gate_target = normalize_edition_comparison_title(name)
    passing: list[int] = []
    matched_spelling: dict[int, str] = {}
    for idx in ordered:
        for candidate_name in compatible_names[idx]:
            if normalize_edition_comparison_title(candidate_name) == gate_target:
                passing.append(idx)
                matched_spelling[idx] = candidate_name
                break

    def _accept(idx: int) -> IGDBGame:
        """Return the chosen candidate, naming the spelling that carried it."""
        game = results[idx]
        spelling = matched_spelling.get(idx)
        if spelling is not None and spelling != game.name:
            logger.info(
                "IGDB name-match gate accepted %r via IGDB alternative name %r "
                "(igdb_id=%s primary name=%r)",
                name,
                spelling,
                game.igdb_id,
                game.name,
            )
        return game

    if not passing:
        if selected_idx is not None:
            logger.info(
                "IGDB name-match gate rejected %r -> %r (igdb_id=%s): "
                "edition-stripped titles differ on every candidate; leaving unmatched",
                name,
                results[selected_idx].name,
                results[selected_idx].igdb_id,
            )
        return None

    # HOW the equality was reached decides how much it is worth. Two axes:
    #
    #   * WHICH NAME matched — a record's own primary title is stronger
    #     evidence than a spelling it merely also answers to. Alternative
    #     names carry working titles, acronyms and regional names that
    #     legitimately coincide with another record's primary ("Titan" is
    #     Blizzard's working title for "Overwatch" AND a 2019 game of its
    #     own), so ranking them equally dragged a clean exact-title match into
    #     a same-name ambiguity refusal.
    #   * WHICH STRIP was needed — no strip at all (the exact title), a KNOWN
    #     edition phrase (`normalize_strict_edition_title`), or the generic
    #     "<up to 3 words> Edition" tail, which eats arbitrary words and is a
    #     guess: "Minecraft: Education Edition" collapses onto "Minecraft"
    #     exactly like a real SKU would, and the lone-tier-2 rule would then
    #     accept the older record and link a different product.
    #
    # Bands, first non-empty considered ALONE: 1a exact/primary, 1b
    # exact/alternative, 2a strict-edition/primary, 2b strict-edition/
    # alternative, 3 generic tail (either name). Tier 3 is year-gated below.
    strict_target = normalize_strict_edition_title(name)
    strict_spelling: dict[int, str] = {}
    for idx in passing:
        for candidate_name in compatible_names[idx]:
            if normalize_strict_edition_title(candidate_name) == strict_target:
                strict_spelling[idx] = candidate_name
                break

    tier_1a = [
        idx
        for idx in passing
        if results[idx].name in compatible_names[idx]
        and match_key(results[idx].name) == normalized_query
    ]
    tier_1b = [
        idx
        for idx in passing
        if idx not in tier_1a
        and any(match_key(n) == normalized_query for n in compatible_names[idx])
    ]
    rest = [idx for idx in passing if idx not in tier_1a and idx not in tier_1b]
    tier_2a = [idx for idx in rest if strict_spelling.get(idx) == results[idx].name]
    tier_2b = [idx for idx in rest if idx not in tier_2a and idx in strict_spelling]
    tier_3 = [idx for idx in rest if idx not in strict_spelling]
    if generic_edition_query:
        # ``name`` is not the library row's title: it is a ladder rung that
        # already peeled a tail the STRICT strip does not recognize (see
        # _resolve_game_with_status). The rung vouches for identity — that is
        # why it gates against itself — but not for the edition reading, so
        # every match it produces is tier-3 strength however exactly it
        # matches the rung's own string.
        tier, tier_kind = passing, "generic"
    else:
        tier, tier_kind = next(
            (band, kind)
            for band, kind in (
                (tier_1a, "exact"),
                (tier_1b, "exact"),
                (tier_2a, "edition"),
                (tier_2b, "edition"),
                (tier_3, "generic"),
            )
            if band
        )
    is_tier_one = tier_kind == "exact"
    distinct_ids = {results[idx].igdb_id for idx in tier}

    def _closest_within_a_year(band: list[int], year_reference: int) -> tuple[int | None, list[int]]:
        """The single closest candidate within ±1 year, and the whole window.

        The index is None when the window is empty OR when two DISTINCT
        records tie at the best distance — list position is not evidence, and
        picking by it would flip the link on a re-fetch. The window itself is
        returned for the caller's log line.
        """
        distances = {
            idx: abs(year - year_reference)
            for idx in band
            if (year := _candidate_year(results[idx])) is not None
        }
        window = sorted(
            (idx for idx, distance in distances.items() if distance <= 1),
            key=lambda idx: (distances[idx], idx),
        )
        if not window:
            return None, window
        best = window[0]
        tied_ids = {
            results[idx].igdb_id for idx in window if distances[idx] == distances[best]
        }
        return (None if len(tied_ids) > 1 else best), window

    if tier_kind == "generic":
        # Only the generic tail folded these together, so the "edition"
        # reading is unevidenced: it needs the year to vouch for it, and only
        # a SYMMETRIC ±1 window — tier 2's one-sided "an edition cannot
        # predate its own game" rule assumes the suffix really is an edition,
        # which is exactly what is in question here.
        if reference_year is None:
            logger.info(
                "IGDB gate refused %r: generic edition tail, no year evidence "
                "(candidates=%s)",
                name,
                _year_debug(results, tier),
            )
            return None
        best_idx, window = _closest_within_a_year(tier, reference_year)
        if not window:
            logger.info(
                "IGDB gate refused %r (reference_year=%s): generic edition tail, "
                "year conflict (candidates=%s)",
                name,
                reference_year,
                _year_debug(results, tier),
            )
            return None
        if best_idx is None:
            logger.info(
                "IGDB year tiebreak refused %r (reference_year=%s): several "
                "generic-edition candidates equally close in year (candidates=%s)",
                name,
                reference_year,
                _year_debug(results, window),
            )
            return None
        return _accept(best_idx)

    if reference_year is None:
        known = {
            results[idx].igdb_id: year
            for idx in tier
            if (year := _candidate_year(results[idx])) is not None
        }
        if len(known) > 1 and max(known.values()) - min(known.values()) > 1:
            logger.info(
                "IGDB year tiebreak refused %r: same-name candidates, no reference "
                "year (tier=%s candidates=%s)",
                name,
                1 if is_tier_one else 2,  # tier 3 returned above
                _year_debug(results, tier),
            )
            return None
        return _accept(tier[0])

    # Inside the window, DISTANCE decides, never provider order: with a 2023
    # row and candidates from 2022 and 2023, the 2023 record wins whichever
    # IGDB listed first. Two distinct records at the same best distance are
    # equally plausible, and picking between them by list position would
    # flip the link on a re-fetch — refuse instead (AGENTS.md: reject
    # release-year conflicts; never let ordering stand in for evidence).
    best_idx, within_one = _closest_within_a_year(tier, reference_year)
    if within_one:
        if best_idx is None:
            logger.info(
                "IGDB year tiebreak refused %r (reference_year=%s): several candidates "
                "equally close in year (candidates=%s)",
                name,
                reference_year,
                _year_debug(results, within_one),
            )
            return None
        return _accept(best_idx)

    if is_tier_one:
        if len(distinct_ids) == 1:
            # A lone exact-title candidate: a re-release's store date can sit
            # years off the library row's, and the title itself vouches. An
            # unknown IGDB year lands here too, and must never block.
            return _accept(tier[0])
        logger.info(
            "IGDB year tiebreak refused %r (reference_year=%s): several exact-title "
            "candidates, none within a year (candidates=%s)",
            name,
            reference_year,
            _year_debug(results, tier),
        )
        return None

    if len(distinct_ids) == 1:
        # One edition-stripped match, and the window is ONE-SIDED: an edition
        # cannot predate the game it is an edition of, so a candidate that is
        # older than the row (or the same age) IS the original — the row's year
        # is just the store's re-listing date ("Deus Ex: Game of the Year
        # Edition" bought in 2013, IGDB's "Deus Ex" from 2000). Only a
        # candidate NEWER than the row by more than two years is a later
        # product that would absorb the original ("Mafia" 2002 ->
        # "Mafia: Definitive Edition" 2020). An unknown IGDB year accepts, like
        # a lone tier-1 candidate.
        lone_year = _candidate_year(results[tier[0]])
        if lone_year is None or lone_year - reference_year <= 2:
            return _accept(tier[0])
        logger.info(
            "IGDB year tiebreak refused %r (reference_year=%s): the only "
            "edition-stripped match is more than two years newer (candidates=%s)",
            name,
            reference_year,
            _year_debug(results, tier),
        )
        return None

    within_two = [
        idx for idx in tier
        if (year := _candidate_year(results[idx])) is not None
        and abs(year - reference_year) <= 2
    ]
    if within_two:
        return _accept(within_two[0])
    logger.info(
        "IGDB year tiebreak refused %r (reference_year=%s): several "
        "edition-stripped matches, none within two years (candidates=%s)",
        name,
        reference_year,
        _year_debug(results, tier),
    )
    return None


@dataclass(frozen=True)
class _ResolveOutcome:
    """Result of a status-carrying name resolution (see _resolve_game_with_status).

    ``saw_candidates`` distinguishes "IGDB returned candidates but every one
    was rejected by the identity/name gate" (a genuine refuse-to-guess
    no-match from a demonstrably alive API) from "every query returned zero
    candidates" (which, in bulk, is outage-shaped). The backfill's circuit
    breaker must only count the latter.
    """

    game: IGDBGame | None
    saw_candidates: bool


_TRAILING_YEAR_RE = re.compile(r"\(\s*(\d{4})\s*\)\s*$")


def _trailing_year(name: str) -> int | None:
    """The year a title disambiguates itself with ("Prey (2017)"), or None.

    Must be read off the RAW name: every catalog/edition normalization drops
    the marker, so a caller that strips before asking gets nothing.
    """
    match = _TRAILING_YEAR_RE.search(name.strip())
    return int(match.group(1)) if match else None


def _reference_year_for(name: str, reference_release_date: str | None) -> int | None:
    """The library row's release year, for the same-name tiebreak.

    The stored release date first; failing that, a trailing "(YYYY)" the title
    itself carries.
    """
    head = (reference_release_date or "").strip()[:4]
    if head.isdigit():
        return int(head)
    return _trailing_year(name)


async def _resolve_game_with_status(
    name: str,
    igdb_platform_id: int | tuple[int, ...] | None,
    *,
    suppress_errors: bool = True,
    reference_release_date: str | None = None,
) -> _ResolveOutcome:
    """resolve_game's implementation, reporting whether any query returned candidates.

    Internal: only the backfill needs the status. Everything else should keep
    calling the public ``resolve_game``.

    ``reference_release_date`` is the library row's own release date. It is the
    only evidence that separates two IGDB records sharing a title (Dead Space
    2008 vs the 2023 remake) and the only thing that stops a remake marketed as
    an edition from absorbing the original ("Mafia" 2002 vs "Mafia: Definitive
    Edition" 2020) — see ``_select_best_match``. Absent, a title's own trailing
    "(YYYY)" is used instead.
    """
    if not os.environ.get("TWITCH_CLIENT_ID"):
        if not suppress_errors:
            raise IGDBRequestFailure(
                f"IGDB credentials not configured; cannot resolve {name!r}"
            )
        return _ResolveOutcome(game=None, saw_candidates=False)

    reference_year = _reference_year_for(name, reference_release_date)

    saw_candidates = False
    results = await search_game(name, igdb_platform_id, suppress_errors=suppress_errors)
    # Try without platform filter as fallback
    if not results and igdb_platform_id is not None:
        results = await search_game(name, igdb_platform_id=None, suppress_errors=suppress_errors)

    if results:
        saw_candidates = True
        match = _select_best_match(
            name,
            results,
            allow_inconclusive_fallback=True,
            reference_year=reference_year,
        )
        if match is not None:
            return _ResolveOutcome(game=match, saw_candidates=True)
        # Non-empty results, but every candidate was rejected (identity
        # conflict or the strict name gate). Fall through to the ladder: an
        # edition-carrying query like "Sea of Thieves: 2026 Edition" can
        # return the base game yet fail the gate against the ORIGINAL title
        # (the "2026" token survives normalization) — an identity-preserving
        # rung re-queries with the edition stripped and gates against the
        # rung's own query string, which passes.

    # Search is a relevance index and it does miss titles IGDB actually holds
    # ("The Forest" → zero results, while IGDB stores that exact name as
    # 7504). Before widening the QUERY — which is what put eight prod rows on
    # article-stripped strangers — try narrowing it to an exact-name equality
    # lookup, which cannot match a different title by construction.
    #
    # Ambiguity is refused, never ranked: two games can share an exact name
    # ("The Bridge" 2013 and 2024, both real), and guessing between them is
    # the same mistake as accepting "Forest" for "The Forest". The platform
    # filter runs first because it usually resolves the ambiguity on its own
    # (a stub duplicate carries no platforms).
    async def _exact_name_pass(query: str) -> tuple[IGDBGame | None, bool]:
        """One equality lookup (platform-filtered, then not): (match, saw_any)."""
        saw_any = False
        for platform_filter in (igdb_platform_id, None):
            exact = await fetch_games_by_exact_name(
                query, platform_filter, suppress_errors=suppress_errors
            )
            if not exact:
                continue
            saw_any = True
            distinct = {game.igdb_id: game for game in exact}
            if len(distinct) > 1 and reference_year is None:
                # Nothing to arbitrate with: two real games share the name and
                # IGDB's ordering is not evidence. With a reference year the
                # ambiguity is handed to _select_best_match, which resolves it
                # when the years do and refuses when they don't.
                logger.info(
                    "IGDB exact-name lookup for %r is ambiguous (%s) — refusing to guess",
                    query,
                    sorted(distinct),
                )
                break
            match = _select_best_match(
                query,
                list(distinct.values()),
                allow_inconclusive_fallback=False,
                reference_year=reference_year,
            )
            if match is not None:
                return match, True
            # A single exact-name hit that still fails the identity/name gate is
            # a genuine no — fall through rather than re-asking for the same
            # title without the platform filter.
            break
        return None, saw_any

    exact_match, exact_saw = await _exact_name_pass(name)
    saw_candidates = saw_candidates or exact_saw
    if exact_match is not None:
        return _ResolveOutcome(game=exact_match, saw_candidates=True)

    # The stores disagree with IGDB about the ampersand ("Rabbit and Steel" on
    # Steam, "Rabbit & Steel" on IGDB), and an equality filter cannot bridge a
    # spelling. One extra lookup for the other spelling, under the same
    # refusal rules — only when the stored spelling matched NOTHING, so a real
    # hit (or an ambiguity) on the stored name is never second-guessed.
    if not exact_saw:
        alternate = ampersand_alternate(name)
        if alternate is not None:
            alt_match, alt_saw = await _exact_name_pass(alternate)
            saw_candidates = saw_candidates or alt_saw
            if alt_match is not None:
                return _ResolveOutcome(game=alt_match, saw_candidates=True)

    # Zero results even without a platform filter — or nothing accepted from
    # the original query: work through a ladder of alternate query strings
    # (Fix 4). Stop at the first variant whose results produce an accepted
    # match; a variant that returns only unrelated titles must not be
    # accepted just because it's non-empty (each rung's _select_best_match
    # still applies the identity check and the strict name gate).
    tried = {name.casefold()}
    for variant, identity_preserving in _generate_resolve_query_variants(name):
        if variant.casefold() in tried:
            continue
        tried.add(variant.casefold())

        variant_results = await search_game(variant, igdb_platform_id, suppress_errors=suppress_errors)
        if not variant_results and igdb_platform_id is not None:
            variant_results = await search_game(variant, igdb_platform_id=None, suppress_errors=suppress_errors)
        if not variant_results:
            continue
        saw_candidates = True

        # Identity-preserving variants gate against the variant itself: the
        # transformation already vouches for series identity, and the original
        # may carry an edition number ("… 2026 Edition") that would wrongly
        # read as a sequel marker against the base game's title.
        #
        # What such a rung does NOT vouch for is the edition reading itself
        # when the tail it peeled is one no edition-word list recognizes
        # ("Minecraft: Education Edition" -> "Minecraft"). Gating against the
        # rung would otherwise turn that guess into a tier-1 exact title and
        # walk straight past the year evidence _select_best_match demands of a
        # generic edition tail, linking a different product outright.
        gate_name = variant if identity_preserving else name
        generic_edition_rung = identity_preserving and normalize_strict_edition_title(
            name
        ) != normalize_strict_edition_title(variant)
        match = _select_best_match(
            gate_name,
            variant_results,
            allow_inconclusive_fallback=False,
            reference_year=reference_year,
            generic_edition_query=generic_edition_rung,
        )
        if match is not None:
            return _ResolveOutcome(game=match, saw_candidates=True)

    return _ResolveOutcome(game=None, saw_candidates=saw_candidates)


def igdb_game_from_record(record: dict) -> IGDBGame:
    """An ``IGDBGame`` from one ``fetch_igdb_game_records`` entry — no network.

    Only the fields that decide a name match are carried across (id, name,
    alternative names, release year, category/game_type); everything else keeps
    its dataclass default, because the only consumer is
    ``resolver_would_accept``, which reads exactly those. ``record`` may carry
    its own id under ``id`` or ``igdb_id``; a record taken straight out of the
    ``{igdb_id: record}`` mapping has neither, and 0 is harmless here (the id
    is only logged and deduplicated on).
    """
    category = record.get("category")
    game_type = record.get("game_type")
    raw_id = record.get("id") if record.get("id") is not None else record.get("igdb_id")
    alternative_names = [
        str(name) for name in (record.get("alternative_names") or []) if name
    ]
    return IGDBGame(
        igdb_id=int(raw_id or 0),
        name=str(record.get("name") or ""),
        category=int(category if category is not None else (game_type or 0)),
        first_release_date=record.get("first_release_date"),
        game_type=game_type,
        alternative_names=alternative_names,
    )


def resolver_would_accept(
    library_name: str,
    game: IGDBGame,
    *,
    reference_release_date: str | None = None,
) -> bool:
    """Offered exactly this record, would the name resolver link it to this row?

    Pure and offline: it replays the SELECTION half of
    ``_resolve_game_with_status`` — the gate, the tiers, the year rules, then
    every rung of the query ladder with that rung's own gate name and
    ``generic_edition_query`` flag — against a one-candidate result list. It
    deliberately does NOT replay the QUERIES: whether IGDB's search index or
    the exact-name filter would have surfaced this record is a different
    question (and a network one).

    So a True answer means "the resolver's rules accept this pairing", which is
    what an audit needs to tell a resolver refusal apart from a retrieval miss;
    a False answer means no rung of the ladder would have taken it, however it
    was found.
    """
    reference_year = _reference_year_for(library_name, reference_release_date)
    if (
        _select_best_match(
            library_name,
            [game],
            allow_inconclusive_fallback=True,
            reference_year=reference_year,
        )
        is not None
    ):
        return True

    tried = {library_name.casefold()}
    for variant, identity_preserving in _generate_resolve_query_variants(library_name):
        if variant.casefold() in tried:
            continue
        tried.add(variant.casefold())
        gate_name = variant if identity_preserving else library_name
        generic_edition_query = identity_preserving and normalize_strict_edition_title(
            library_name
        ) != normalize_strict_edition_title(variant)
        if (
            _select_best_match(
                gate_name,
                [game],
                allow_inconclusive_fallback=False,
                reference_year=reference_year,
                generic_edition_query=generic_edition_query,
            )
            is not None
        ):
            return True

    # The exact-name rung's ampersand retry ("Rabbit and Steel" ->
    # "Rabbit & Steel"), gated against the alternate spelling like the
    # identity-preserving variant it is.
    alternate = ampersand_alternate(library_name)
    return alternate is not None and (
        _select_best_match(
            alternate,
            [game],
            allow_inconclusive_fallback=False,
            reference_year=reference_year,
        )
        is not None
    )


async def resolve_game(
    name: str,
    igdb_platform_id: int | tuple[int, ...] | None,
    *,
    suppress_errors: bool = True,
    reference_release_date: str | None = None,
) -> IGDBGame | None:
    """
    Find the best IGDB match for a game name + platform. Returns None if not
    found. Unconfigured credentials return None only while ``suppress_errors``
    is True; with ``suppress_errors=False`` they raise ``IGDBRequestFailure``
    so a caller that records "checked, no match" can never mistake an
    operational outage for a genuine miss.

    ``reference_release_date`` is the library row's release date, used to
    separate two IGDB records that share a title (see ``_select_best_match``).
    """
    outcome = await _resolve_game_with_status(
        name,
        igdb_platform_id,
        suppress_errors=suppress_errors,
        reference_release_date=reference_release_date,
    )
    return outcome.game


async def resolve_and_link_game(
    name: str,
    igdb_platform_id: int | tuple[int, ...] | None,
    candidates: dict[int, str],
    *,
    platform: str | None = None,
    reference_release_date: str | None = None,
) -> tuple[int, "IGDBGame | None"]:
    """
    Resolve a game to its canonical games row via IGDB, creating a new row if needed.
    Also writes tags, genres, release_date, and igdb_id from IGDB if the game row
    doesn't already have them.

    Returns (game_id, igdb_game) so callers can write platform_release_date
    to game_platform_enrichment after upsert_game_platform gives them a platform_id.
    igdb_game is None when IGDB is unconfigured or returns no result.

    Falls back to fuzzy name matching if IGDB is unconfigured or returns no result.

    ``platform`` is the caller's internal platform string (e.g. "steam", "gog"). When
    given, the title→existing-row fuzzy fallback refuses to attach onto a row that
    already owns that platform, so two distinct same-platform store entries with the
    same name stay separate instead of collapsing.

    ``reference_release_date`` is passed straight through to ``resolve_game``
    as the same-name year tiebreak.
    """
    from .db import find_game_by_name_fuzzy, get_db, get_game_by_igdb_id

    igdb_game = await resolve_game(
        name, igdb_platform_id, reference_release_date=reference_release_date
    )
    if igdb_game is not None:
        async with _get_igdb_link_lock(igdb_game.igdb_id):
            if igdb_game.alias_for_parent and (igdb_game.parent_igdb_id or igdb_game.parent_name):
                parent = None
                if igdb_game.parent_igdb_id is not None:
                    parent = await get_game_by_igdb_id(igdb_game.parent_igdb_id)
                if parent is None and igdb_game.parent_name:
                    parent = await find_game_by_name_fuzzy(igdb_game.parent_name, candidates=candidates)
                if parent is not None:
                    game_id = parent["id"]
                else:
                    from .db import upsert_game

                    game_id = await upsert_game(
                        appid=None,
                        name=igdb_game.parent_name or igdb_game.name,
                    )
                    candidates[game_id] = igdb_game.parent_name or igdb_game.name

                from .db import upsert_game_alias

                await upsert_game_alias(
                    game_id,
                    name,
                    alias_type=igdb_game.content_type,
                    source="igdb",
                    source_key=str(igdb_game.igdb_id),
                )
                return game_id, igdb_game

            existing = await get_game_by_igdb_id(igdb_game.igdb_id)
            if existing is not None:
                game_id = existing["id"]
            else:
                # On upgraded databases we may already have the title row without igdb_id.
                existing = await find_game_by_name_fuzzy(
                    name,
                    candidates=candidates,
                    exclude_platform=platform,
                    reference_release_date=igdb_game.first_release_date,
                )
                if existing is None and igdb_game.name.casefold() != name.casefold():
                    existing = await find_game_by_name_fuzzy(
                        igdb_game.name,
                        candidates=candidates,
                        exclude_platform=platform,
                        reference_release_date=igdb_game.first_release_date,
                    )

                if existing is not None:
                    game_id = existing["id"]
                else:
                    async with get_db() as db:
                        cursor = await db.execute(
                            "INSERT INTO games (name, name_normalized) VALUES (?, ?)",
                            (name, normalize_search_text(name)),
                        )
                        game_id = cursor.lastrowid
                        await db.commit()

            await _apply_igdb_metadata(game_id, igdb_game)
        return game_id, igdb_game

    # No IGDB result — fall back to fuzzy matching
    async with _get_fallback_title_lock(name):
        override = classify_title_override(name)
        if override is not None and override.alias_for_parent and override.parent_name:
            parent = await find_game_by_name_fuzzy(override.parent_name, candidates=candidates)
            if parent is not None:
                game_id = parent["id"]
            else:
                from .db import upsert_game

                game_id = await upsert_game(appid=None, name=override.parent_name)
                candidates[game_id] = override.parent_name

            from .db import upsert_game_alias

            await upsert_game_alias(
                game_id,
                name,
                alias_type=override.content_type,
                source="local_override",
                source_key=None,
            )
            return game_id, None
        if override is not None and not override.is_primary_library_item:
            parent_game_id = None
            if override.parent_name:
                parent = await find_game_by_name_fuzzy(override.parent_name, candidates=candidates)
                if parent is not None:
                    parent_game_id = parent["id"]
                else:
                    from .db import upsert_game

                    parent_game_id = await upsert_game(appid=None, name=override.parent_name)
                    candidates[parent_game_id] = override.parent_name

            from .db import upsert_game

            game_id = await upsert_game(
                appid=None,
                name=name,
                content_type=override.content_type,
                parent_game_id=parent_game_id,
                is_primary_library_item=int(override.is_primary_library_item),
            )
            candidates[game_id] = name
            return game_id, None

        existing = await find_game_by_name_fuzzy(
            name, candidates=candidates, exclude_platform=platform
        )
        if existing:
            return existing["id"], None

        from .db import upsert_game
        return await upsert_game(appid=None, name=name, match_existing_by_name=False), None


async def _apply_igdb_metadata(game_id: int, igdb_game: IGDBGame) -> None:
    """Write IGDB fields to games row, skipping columns that are already populated."""
    from .db import (
        get_db,
        get_game_by_igdb_id,
        get_manual_overrides,
        has_nested_children,
        upsert_game,
    )

    now = datetime.now(UTC).isoformat()

    # Parent guard (mirrors upserts.py::apply_content_classification): never nest a
    # row other rows already hang off. IGDB happily hands back an edition/version
    # verdict for a base game's own title, and applying it would hide the parent from
    # the is_primary rollups AND strand its children. Resolved up front, before parent
    # resolution: a blocked verdict must not mint a parent row that nothing will point
    # at. The metadata writes below still land; only the classification is dropped.
    async with get_db() as db:
        nesting_blocked = igdb_game.content_type in NESTED_CONTENT_TYPES and (
            await has_nested_children(db, game_id)
        )

    # Parent resolution here is mint-free: an existing row is looked up by igdb
    # id, then by exact name. Minting a missing parent is deferred until the
    # guards below have decided the classification will actually be written —
    # minting up front left an orphan phantom row behind (no platforms, no
    # children pointing at it) every time the default-clobber or substance
    # guard then dropped the classification write. Gated on a NESTED verdict:
    # a primary library item must not keep a parent (the update_game
    # invariant), so a primary verdict that still carries an IGDB parent
    # (remake/remaster/standalone-expansion records list their original) must
    # neither resolve nor mint one — resolving it wrote a parent link onto a
    # primary row and minted an unowned phantom ("Sid Meier's Colonization"
    # 1994 minted above the primary Civ IV: Colonization).
    parent_game_id: int | None = None
    mint_parent_name: str | None = None
    if not nesting_blocked and igdb_game.content_type in NESTED_CONTENT_TYPES:
        if igdb_game.parent_igdb_id is not None:
            parent = await get_game_by_igdb_id(igdb_game.parent_igdb_id)
            if parent is not None:
                parent_game_id = parent["id"]
        if parent_game_id is None and igdb_game.parent_name:
            async with get_db() as db:
                parent = await db.execute_fetchone(
                    "SELECT id FROM games WHERE lower(name) = lower(?) ORDER BY id LIMIT 1",
                    (igdb_game.parent_name,),
                )
            if parent is not None:
                parent_game_id = parent["id"]
            else:
                mint_parent_name = igdb_game.parent_name

    # A parent that resolves back to this same row is not a real parent (IGDB
    # occasionally lists an edition/version whose parent is the row itself). Writing
    # parent_game_id = game_id would orphan the row: it is excluded from search/list
    # (is_primary filter) yet unreachable as any other row's edition. Drop the
    # self-parent and keep the row a primary library item.
    self_referential_parent = parent_game_id == game_id
    if self_referential_parent:
        parent_game_id = None

    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT name,
                      tags,
                      genres,
                      release_date,
                      content_type,
                      parent_game_id,
                      is_primary_library_item
               FROM games
               WHERE id = ?""",
            (game_id,),
        )
        if row is None:
            return

        overrides = await get_manual_overrides(db, game_id)
        updates: dict = {
            "igdb_cached_at": now,
            "igdb_resolver_version": IGDB_RESOLVER_VERSION,
        }
        # igdb_id can be pinned by hand (update_game) when auto-matching picked
        # the wrong title; honor that override so a later fetch doesn't relink it.
        if "igdb_id" not in overrides:
            updates["igdb_id"] = igdb_game.igdb_id
        if igdb_game.platforms and "igdb_platforms" not in overrides:
            # NULL means "not fetched yet"; an empty fetch keeps NULL so the
            # deals tool can distinguish unknown from confirmed-single-platform.
            updates["igdb_platforms"] = json.dumps(igdb_game.platforms)
        if igdb_game.cover_image_id and "cover_image_id" not in overrides:
            updates["cover_image_id"] = igdb_game.cover_image_id
        if row["release_date"] is None and igdb_game.first_release_date and "release_date" not in overrides:
            updates["release_date"] = igdb_game.first_release_date
        if row["genres"] is None and igdb_game.genres and "genres" not in overrides:
            updates["genres"] = json.dumps(igdb_game.genres)
        if igdb_game.tags and "tags" not in overrides:
            existing = json.loads(row["tags"]) if row["tags"] else []
            merged = _merge_igdb_tags(existing, igdb_game.tags)
            if merged != existing:
                updates["tags"] = json.dumps(merged)
        # Content classification: never let a default ("base_game"/primary,
        # no parent) re-fetch clobber a prior non-default classification.
        # NOTE: the same guard semantics live in
        # db/upserts.py::apply_content_classification (the reusable writer for
        # the Steam/purchase classifiers) — keep the two in sync. This IGDB path
        # keeps extra IGDB-only behavior (alias handling, parent minting, series)
        # and is deliberately not routed through that helper.
        # IGDB search can return a bare main-game hit on a later pass, and
        # silently flipping a nested DLC back to primary would resurface it as
        # its own library item. Only apply when the fetch carries a real signal
        # or the stored row is still at the default. Mirrors the enrich
        # re-fetch guard.
        new_is_default = (
            igdb_game.content_type == "base_game"
            and igdb_game.is_primary_library_item
            and parent_game_id is None
            and mint_parent_name is None
        )
        stored_is_default = (
            row["content_type"] == "base_game"
            and bool(row["is_primary_library_item"])
            and row["parent_game_id"] is None
        )
        # Substance guard — mirrors db/upserts.py::apply_content_classification
        # (keep the two in sync): a row carrying a store identifier and real
        # playtime is never demoted under a parent that has neither. A wrong
        # IGDB version_parent match would otherwise hide the real, played game
        # behind an empty shell row. Only the classification is dropped; the
        # metadata writes above still land.
        apply_classification = not new_is_default or stored_is_default
        if apply_classification and igdb_game.content_type in NESTED_CONTENT_TYPES:
            conflict = False
            if parent_game_id is not None:
                from .db import nesting_substance_conflict

                conflict = await nesting_substance_conflict(db, game_id, parent_game_id)
            elif mint_parent_name:
                # The parent doesn't exist yet: freshly minted it would carry
                # no identifier and no playtime, so apply the substance rule
                # against that empty-by-construction shape directly.
                from .db import get_game_substance

                substance = await get_game_substance(db, game_id)
                conflict = bool(
                    substance["has_identifier"] and substance["playtime_minutes"] > 0
                )
            # Edition-ownership guard — an edition of a game the user OWNS is
            # the ownership record itself; nesting it under a parent nobody
            # owns (or one about to be minted empty) hides an owned game from
            # every rollup and leaves the parent as a false orphan. Keyed on
            # ownership, not playtime — owned-but-unplayed editions were
            # exactly the rows the substance guard above let through.
            if not conflict and igdb_game.content_type == CONTENT_EDITION:
                from .db import edition_hides_owned_game

                conflict = await edition_hides_owned_game(
                    db, game_id, parent_game_id
                )
            if conflict:
                logger.info(
                    "IGDB classification for game %s skipped: nesting an "
                    "owned/played row under unowned parent %s",
                    game_id,
                    parent_game_id if parent_game_id is not None else mint_parent_name,
                )
                apply_classification = False

        if apply_classification:
            content_type = igdb_game.content_type
            # Without a real (distinct) parent a nested item has nowhere to be
            # reached from, so a self-referential parent forces the row to stay
            # a primary BASE GAME — content_type included, keeping is_primary
            # derived from content_type (a 'dlc' + primary row would be
            # invisible to both the games and addons views).
            if self_referential_parent and content_type in NESTED_CONTENT_TYPES:
                content_type = "base_game"
            if nesting_blocked:
                logger.debug(
                    "IGDB classification would nest game %s, which is a parent of "
                    "nested content; kept its stored classification",
                    game_id,
                )
            else:
                # The content_type that will ACTUALLY be stored — when the
                # column is pinned by a manual override, the stored value wins
                # and every derived decision below (minting, parent link,
                # is_primary) must follow it, not the incoming verdict.
                effective_content_type = (
                    row["content_type"] if "content_type" in overrides else content_type
                )
                if (
                    parent_game_id is None
                    and mint_parent_name
                    and "parent_game_id" not in overrides
                    and effective_content_type in NESTED_CONTENT_TYPES
                ):
                    # Every guard passed and the row will genuinely end up
                    # nested, so the missing parent row is needed now (a
                    # pinned-primary content_type must not leave an orphan
                    # phantom parent behind). upsert_game can still name-match
                    # back onto this very row (IGDB sometimes lists a parent
                    # carrying the row's own title) — treat that like the
                    # self-referential case above.
                    minted = await upsert_game(appid=None, name=mint_parent_name)
                    if minted == game_id:
                        if content_type in NESTED_CONTENT_TYPES:
                            content_type = "base_game"
                            effective_content_type = (
                                row["content_type"]
                                if "content_type" in overrides
                                else content_type
                            )
                    else:
                        parent_game_id = minted
                if "content_type" not in overrides:
                    updates["content_type"] = content_type
                if "parent_game_id" not in overrides:
                    if (
                        parent_game_id is not None
                        and effective_content_type in NESTED_CONTENT_TYPES
                    ):
                        updates["parent_game_id"] = parent_game_id
                    elif (
                        derive_is_primary(effective_content_type)
                        and row["parent_game_id"] is not None
                    ):
                        # A primary library item must not keep a parent (the
                        # update_game promotion invariant): clear the leftover
                        # link a wrong earlier nested classification wrote, so
                        # a primary verdict fully heals the row instead of
                        # leaving it chained to an unrelated game.
                        updates["parent_game_id"] = None
                if "is_primary_library_item" not in overrides:
                    updates["is_primary_library_item"] = int(
                        derive_is_primary(effective_content_type)
                    )

        cols_sql = ", ".join(f"{col} = ?" for col in updates)
        await db.execute(
            f"UPDATE games SET {cols_sql} WHERE id = ?",
            (*updates.values(), game_id),
        )
        await db.commit()

    # Seed the IGDB display name as an alias whenever it differs from the
    # stored row name — the provider full title ("Orwell: Keeping an Eye On
    # You" for a row named "Orwell") is what storefront purchase records and
    # ownership screens arrive with, and without the alias no name tier
    # bridges the two (the extra tokens sink token-AND matching, and minting
    # a duplicate row was the observed failure).
    if (
        igdb_game.name
        and "igdb_id" not in overrides
        and normalize_search_text(igdb_game.name) != normalize_search_text(row["name"])
    ):
        from .db import upsert_game_alias

        await upsert_game_alias(
            game_id,
            igdb_game.name,
            alias_type="provider_name",
            source="igdb",
            source_key=str(igdb_game.igdb_id),
        )

    # IGDB's alternative names are the abbreviations and regional titles the
    # user (and a storefront purchase record) actually type — "GTA V" for
    # "Grand Theft Auto V". Persisted as aliases they make LIBRARY-side search
    # and name matching find the row; they are deliberately NOT fed back into
    # the resolver, which reads them from the IGDB record directly. Gated on
    # the same pinned-igdb_id override as the provider name above: a link the
    # user pinned by hand must not accumulate aliases from whatever record a
    # name resolution happened to return.
    #
    # One batched call, because this runs inside the enrichment loop and a
    # record can carry a dozen names. It also PRUNES this game's other
    # igdb-sourced aliases: when the external mapping (Steam or GOG) re-points
    # a row at a different record, the previous record's spellings are simply
    # wrong and must not linger. The prune runs even when the new record has
    # no alternative names at all, which is why the call is unconditional.
    if "igdb_id" not in overrides:
        from .db import upsert_game_aliases

        stored_name_key = normalize_search_text(row["name"])
        await upsert_game_aliases(
            game_id,
            [
                alt
                for alt in igdb_game.alternative_names
                if normalize_search_text(alt) != stored_name_key
            ],
            alias_type="alternative_name",
            source="igdb",
            source_key=str(igdb_game.igdb_id),
            prune_stale_source_keys=True,
        )

    if igdb_game.series:
        from .db import upsert_game_series_links

        await upsert_game_series_links(game_id, igdb_game.series)


async def choose_igdb_platform_hint(game_id: int) -> tuple[int, ...] | None:
    platforms_by_game = await load_platforms_for_games([game_id])
    platforms = platforms_by_game.get(game_id, [])
    if not platforms:
        return None

    for platform in platforms:
        if platform["platform"] == "steam":
            return PLATFORM_TO_IGDB_ANY["steam"]

    for platform in platforms:
        if platform.get("owned") and platform["platform"] in PLATFORM_TO_IGDB_ANY:
            return PLATFORM_TO_IGDB_ANY[platform["platform"]]

    return None


async def upsert_backfill_platform_release_dates(game_id: int, igdb_game: IGDBGame) -> None:
    if not igdb_game.platform_release_dates:
        return

    platforms_by_game = await load_platforms_for_games([game_id])
    for platform in platforms_by_game.get(game_id, []):
        candidate_ids = PLATFORM_TO_IGDB_ANY.get(platform["platform"], ())
        release_date = next(
            (
                igdb_game.platform_release_dates[pid]
                for pid in candidate_ids
                if pid in igdb_game.platform_release_dates
            ),
            None,
        )
        game_platform_id = platform["game_platform_id"]
        if release_date is None or game_platform_id is None:
            continue
        await upsert_game_platform_enrichment(
            game_platform_id,
            platform_release_date=release_date,
        )


async def mark_igdb_checked(game_id: int) -> None:
    """Stamp a row "checked", recording WHICH resolver generation checked it.

    The version is what makes a no-match stamp temporary: a later generation
    re-claims the row without a migration (see IGDB_RESOLVER_VERSION).
    """
    from .db import get_db

    checked_at = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.execute(
            "UPDATE games SET igdb_cached_at = ?, igdb_resolver_version = ? WHERE id = ?",
            (checked_at, IGDB_RESOLVER_VERSION, game_id),
        )
        await db.commit()


# Systemic-failure circuit breaker for the backfill: this many *consecutive*
# zero-candidate name searches suggests the search API is effectively down
# (prod 2026-07-05: an IGDB outage window yielded ~0% matches and every miss
# was cached as a permanent no-match). Only zero-candidate searches count —
# a search whose candidates were all rejected by the identity/name gate is
# proof the API is alive, and counting it caused a prod livelock (2026-07-07:
# a deterministic cluster of gate-rejected titles at the head of the claim
# order tripped the breaker forever, freezing the heal at ~19/1028 rows).
# The counter is process-wide and survives across passes so an outage cannot
# slip through on pass boundaries; it resets on any search that returns
# candidates (resolved or gate-rejected) and on a live canary probe.
_BACKFILL_MISS_CIRCUIT_BREAKER = 10
_consecutive_backfill_misses = 0

# Canary title for the breaker's trip check: guaranteed to exist on IGDB, so a
# zero-candidate or failing search for it confirms a real outage while a
# candidate-returning search proves the miss streak was a genuine run of
# IGDB-absent titles (possible under the deterministic claim order) rather
# than an outage.
_CANARY_TITLE = "The Witcher 3: Wild Hunt"

# The same idea one endpoint over. A store mapping that answers HTTP 200 with
# an empty body is indistinguishable from "IGDB knows none of these uids", and
# that is exactly how the retired `external_games.category` filter failed: 904
# owned rows were stamped "checked, no match" while the authoritative step
# quietly mapped nothing. So every batch carries a uid IGDB certainly maps
# (Steam appid 292030 — The Witcher 3: Wild Hunt) and an empty answer that
# ALSO loses the canary is read as an outage, not as an answer. Keyed by the
# library column the batch is built from; a column with no canary is exempt
# (GOG product ids have no equally certain reference uid here).
_EXTERNAL_MAPPING_CANARY_UIDS = {"steam_appid": "292030"}


async def _igdb_canary_alive() -> bool:
    """One search for a certainly-existing title: is IGDB search actually up?

    True when candidates come back (API alive — a tripping miss streak is a
    genuine run of no-matches). False when the search fails operationally OR
    returns zero candidates for a blockbuster (both outage-shaped: the abort
    stands and rows stay retryable).
    """
    try:
        results = await search_game(_CANARY_TITLE, suppress_errors=False)
    except IGDBRequestFailure as exc:
        provider_health.record_failure("igdb", exc)
        logger.warning("IGDB canary search failed: %s", exc)
        return False
    if not results:
        # HTTP 200 with zero candidates for a blockbuster is the other outage
        # shape this probe exists to confirm; the backfill aborts on it with
        # zero rows resolved, so without this record the pass would publish
        # processed=0, failed=0 and the outage would vanish from /admin/health.
        provider_health.record_failure(
            "igdb", f"IGDB canary search returned no candidates for {_CANARY_TITLE!r}"
        )
        logger.warning("IGDB canary search returned no candidates for %r", _CANARY_TITLE)
        return False
    return True


async def _describe_igdb_id_holder(igdb_id: int) -> str:
    """"id=N name='X'" for the row already linked to ``igdb_id``, for a log line.

    A duplicate-igdb_id IntegrityError is only diagnosable if the log says WHICH
    row is holding the link — otherwise the operator has the losing row and a
    bare id, and has to go query prod to find out whether it is a real
    duplicate, an over-merge, or a wrong link that should be reset. Never
    raises: this runs inside an exception handler, and a failure to describe
    must not replace the warning with a crash.
    """
    try:
        from .db import get_game_by_igdb_id

        holder = await get_game_by_igdb_id(igdb_id)
    except Exception as exc:  # pragma: no cover - defensive
        return f"unknown (lookup failed: {exc})"
    if holder is None:
        return "no row (constraint came from elsewhere)"
    return f"id={holder['id']} name={holder['name']!r}"


def _decode_manual_overrides(raw: str | None) -> set[str]:
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    return set(data) if isinstance(data, list) else set()


def _row_value(row: sqlite3.Row | Mapping[str, Any], key: str) -> Any:
    """Tolerant row access: sqlite3.Row raises IndexError, dicts KeyError."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


async def _link_via_external_mapping(
    *,
    game_id: int,
    row_name: str,
    existing_igdb_id: int | None,
    external_igdb_id: int,
    identifier_label: str,
    identifier_value: Any,
) -> IGDBGame | None:
    """Fetch the record IGDB's external_games maps a store id to, guard included.

    Shared by the Steam-appid and GOG-product-id batches: the mapping is
    authoritative for both, and so is the reason it cannot be trusted blindly.
    Returns the record to apply, or None when the mapped id does not resolve
    (the caller then falls through to the stored link / name resolution).
    """
    if existing_igdb_id and existing_igdb_id != external_igdb_id:
        logger.info(
            "IGDB backfill re-linking game_id=%s name=%r: stored igdb_id=%s "
            "but external_games maps %s=%s to igdb_id=%s",
            game_id,
            row_name,
            existing_igdb_id,
            identifier_label,
            identifier_value,
            external_igdb_id,
        )
    fetched = await fetch_game_by_id(external_igdb_id, suppress_errors=False)
    if fetched is None:
        return None
    if (
        existing_igdb_id
        and existing_igdb_id != external_igdb_id
        and not _igdb_name_agrees(row_name, fetched.name)
    ):
        # The mapping is authoritative but not infallible: prod Steam appid
        # 212680 maps to 178437 ("Faster than light?"), a junk duplicate, and
        # this branch replaced the row's correct link to 3075 ("FTL: Faster
        # Than Light") with it. Only override a stored link the name VOUCHES
        # for when the mapping's own record vouches too; the extra fetch costs
        # one call in the rare case where the two disagree AND the new name
        # doesn't match.
        stored = await fetch_game_by_id(existing_igdb_id, suppress_errors=False)
        if stored is not None and _igdb_name_agrees(row_name, stored.name):
            logger.info(
                "IGDB backfill keeping stored igdb_id=%s for game_id=%s "
                "name=%r: external_games maps %s=%s to %s (%r), whose name "
                "does not match while the stored link's does",
                existing_igdb_id,
                game_id,
                row_name,
                identifier_label,
                identifier_value,
                external_igdb_id,
                fetched.name,
            )
            return stored
    return fetched


async def backfill_missing_games(
    limit: int = 10, *, game_ids: Iterable[int] | None = None
) -> int:
    """Resolve claimed games to IGDB and persist their metadata.

    ``game_ids`` narrows the claim to those rows (nothing else changes): the
    scoped pass ``get_game_detail`` runs for the one row it was asked about,
    rather than leaving an unlinked row waiting for the background drain.

    Resolution order per row:
      1. ``external_games`` (the authoritative store id -> IGDB game mapping):
         the Steam appid first, then — only for rows the Steam batch did not
         map — the GOG product id, each batched once per pass. This also
         *self-corrects* a stored igdb_id that disagrees with what the store id
         actually is (wrong-edition links like Layers of Fear 2016 pointing at
         the 2023 remake).
      2. Fetch by the stored igdb_id (never re-resolve a known link by name).
      3. Name search (``resolve_game``).

    Operational hygiene: any operational failure (missing credentials, request
    failure) leaves the row retryable — never marked "checked". A store-mapping
    batch that maps NOTHING and also loses its canary uid
    (``_EXTERNAL_MAPPING_CANARY_UIDS``) counts as exactly such a failure: an
    empty mapping is otherwise indistinguishable from "IGDB knows none of these
    ids", which is how a retired filter field silently stamped 904 owned rows
    "no match". A search that
    returned candidates which were ALL gate/identity-rejected is committed as
    checked immediately (the API answered; refusing to guess is a genuine
    no-match) and resets the breaker. Zero-candidate no-matches are buffered
    and only committed once the pass demonstrates the search API works (a
    search that returned candidates), or at pass end while the breaker has
    not tripped. When the consecutive zero-candidate counter reaches the
    threshold, a canary search for a certainly-existing title arbitrates:
    canary alive -> the streak is a genuine run of IGDB-absent titles, commit
    it and continue; canary dead -> abort the pass leaving every pending and
    unprocessed row retryable (the counter deliberately survives the abort so
    the next pass re-trips — and re-probes — after a single miss).

    Returns the number of rows that reached a terminal state (matched or
    confidently marked no-match) so the enrichment loop goes quiescent instead
    of spinning while the breaker is tripping.
    """
    global _consecutive_backfill_misses

    stale_before = _claim_cutoff_iso()
    claimed_ids = await claim_game_ids_for_igdb(
        limit=limit,
        stale_before=stale_before,
        game_ids=game_ids,
        resolver_version=IGDB_RESOLVER_VERSION,
    )
    if not claimed_ids:
        return 0

    rows = await load_games_for_igdb_backfill(claimed_ids)
    rows_by_id = {row["id"]: row for row in rows}

    # One authoritative external_games batch per store per pass, in
    # precedence order: a row the Steam batch mapped is never asked about on
    # GOG, because a Steam mapping already answers the question. An
    # operational failure of ANY batch aborts the whole pass (all rows stay
    # retryable) rather than degrading to name search, which could
    # mass-produce wrong or missing links during an outage. The fetchers are
    # read from the module at call time (not bound in a module-level table) so
    # each store's entry point stays independently patchable.
    external_sources: tuple[
        tuple[str, str, Callable[[list[str]], Awaitable[dict[str, int]]]], ...
    ] = (
        ("steam_appid", "external_games", resolve_steam_appids_to_igdb),
        (
            "gog_product_id",
            "external_games GOG",
            lambda uids: resolve_external_ids_to_igdb(IGDB_EXTERNAL_SOURCE_GOG, uids),
        ),
    )
    external_by_source: dict[str, dict[str, int]] = {}
    externally_mapped_ids: set[int] = set()
    for column, log_label, fetch_mapping in external_sources:
        pending_rows = [
            row
            for row in rows
            if row["id"] not in externally_mapped_ids and _row_value(row, column)
        ]
        uids = [str(_row_value(row, column)) for row in pending_rows]
        if not uids or not igdb_credentials_configured():
            continue
        # Ride a certainly-mapped uid along so an empty answer can be told
        # apart from a broken mapping (see _EXTERNAL_MAPPING_CANARY_UIDS). It
        # is appended only when the batch does not already ask about it, and
        # stripped before anything reads the mapping — the canary must never
        # link a row.
        canary_uid = _EXTERNAL_MAPPING_CANARY_UIDS.get(column)
        appended_canary = canary_uid is not None and canary_uid not in uids
        request_uids = [*uids, canary_uid] if appended_canary and canary_uid else uids
        try:
            mapping = await fetch_mapping(request_uids)
        except Exception as exc:
            # Logged AND counted: the pass returns "rows resolved", so an
            # aborted pass is indistinguishable from an empty queue in that
            # number — provider_health is where a dead external_games endpoint
            # becomes visible (same contract as the per-row failure handler
            # below).
            provider_health.record_failure("igdb", exc)
            logger.warning(
                "IGDB %s lookup failed; leaving backfill pass retryable: %s",
                log_label,
                exc,
            )
            for game_id in claimed_ids:
                await release_game_claim(game_id, "igdb_claimed_at")
            return 0
        canary_seen = canary_uid is None or canary_uid in mapping
        if appended_canary:
            mapping = {uid: game for uid, game in mapping.items() if uid != canary_uid}
        if not canary_seen and not any(uid in mapping for uid in uids):
            # A silent wrong-field / dead-endpoint answer: HTTP 200, nothing
            # mapped, not even the title IGDB certainly holds. Treated exactly
            # like a raised fetch failure — count it, log it, leave every claim
            # retryable — because the alternative is stamping this whole batch
            # "checked, no match" off an answer that never actually looked.
            message = (
                f"IGDB {log_label} mapping returned nothing for {len(uids)} uids "
                f"AND lost its canary uid {canary_uid!r} — treating as an outage"
            )
            provider_health.record_failure("igdb", message)
            logger.warning(
                "%s; leaving backfill pass retryable",
                message,
            )
            for game_id in claimed_ids:
                await release_game_claim(game_id, "igdb_claimed_at")
            return 0
        if not canary_seen:
            # Real uids mapped, so the endpoint answers — the canary itself may
            # simply have moved (IGDB re-points store rows). Not an outage.
            logger.info(
                "IGDB %s mapping is missing canary uid %r but mapped real uids; continuing",
                log_label,
                canary_uid,
            )
        external_by_source[column] = mapping
        externally_mapped_ids.update(
            row["id"]
            for row in pending_rows
            if str(_row_value(row, column)) in mapping
        )

    processed = 0
    pending_no_match: list[int] = []
    breaker_tripped = False
    next_index = 0

    for next_index, game_id in enumerate(claimed_ids, start=1):
        row = rows_by_id.get(game_id)
        try:
            if row is None:
                continue

            igdb_game: IGDBGame | None = None
            resolved_via_search = False
            existing_igdb_id = row["igdb_id"]
            overrides = _decode_manual_overrides(_row_value(row, "manual_overrides"))

            # Authoritative store mapping first, in the same precedence the
            # batches above used — Steam, then GOG for a row Steam did not
            # map. When it disagrees with the stored igdb_id, re-resolve
            # instead of trusting the stored link (name-based resolution
            # attached wrong editions in the past). Column-level
            # manual_overrides are still honored inside _apply_igdb_metadata;
            # an explicit igdb_id override pins the stored link entirely.
            external_igdb_id: int | None = None
            identifier_label = ""
            identifier_value: Any = None
            for column, _label, _fetcher in external_sources:
                uid = _row_value(row, column)
                mapped = (
                    external_by_source.get(column, {}).get(str(uid)) if uid else None
                )
                if mapped is not None:
                    external_igdb_id = mapped
                    identifier_label = column
                    identifier_value = uid
                    break
            if external_igdb_id is not None and "igdb_id" not in overrides:
                igdb_game = await _link_via_external_mapping(
                    game_id=game_id,
                    row_name=row["name"],
                    existing_igdb_id=existing_igdb_id,
                    external_igdb_id=external_igdb_id,
                    identifier_label=identifier_label,
                    identifier_value=identifier_value,
                )

            if igdb_game is None and existing_igdb_id:
                # Row already has a matched igdb_id (e.g. from an earlier pass) —
                # fetch it directly instead of re-resolving by name, which can
                # drift onto a different, wrong candidate. Only trust the
                # by-id result when it actually carries platform data; an empty
                # fetch falls through to the normal name-based resolution below.
                fetched = await fetch_game_by_id(existing_igdb_id, suppress_errors=False)
                if fetched is not None and fetched.platforms:
                    igdb_game = fetched

            search_saw_candidates = False
            if igdb_game is None:
                platform_hint = await choose_igdb_platform_hint(game_id)
                resolved_via_search = True
                # The year has to be read off the RAW name here: the query is
                # normalize_catalog_title's output, which drops a trailing
                # "(YYYY)" — so a row named "Prey (2017)" with no stored
                # release_date would hand the resolver no reference at all and
                # its two same-named candidates would refuse each other.
                reference_release_date = _row_value(row, "release_date")
                if not reference_release_date:
                    titled_year = _trailing_year(row["name"])
                    if titled_year is not None:
                        reference_release_date = f"{titled_year:04d}-01-01"
                outcome = await _resolve_game_with_status(
                    normalize_catalog_title(row["name"]),
                    platform_hint,
                    suppress_errors=False,
                    reference_release_date=reference_release_date,
                )
                igdb_game = outcome.game
                search_saw_candidates = outcome.saw_candidates
            if igdb_game is not None:
                try:
                    await _apply_igdb_metadata(game_id, igdb_game)
                    await upsert_backfill_platform_release_dates(game_id, igdb_game)
                except sqlite3.IntegrityError:
                    logger.warning(
                        "IGDB backfill skipped duplicate igdb_id for game_id=%s name=%r "
                        "igdb_id=%s (already held by %s)",
                        game_id,
                        row["name"],
                        igdb_game.igdb_id,
                        await _describe_igdb_id_holder(igdb_game.igdb_id),
                    )
                    await mark_igdb_checked(game_id)
                provider_health.record_success("igdb")
                processed += 1
                if resolved_via_search:
                    # Search demonstrably works, so buffered no-matches are
                    # genuine — commit them. By-id/external successes prove
                    # nothing about the search index and do not flush.
                    _consecutive_backfill_misses = 0
                    for miss_id in pending_no_match:
                        await mark_igdb_checked(miss_id)
                    processed += len(pending_no_match)
                    pending_no_match.clear()
            elif search_saw_candidates:
                # Candidates came back but every one was rejected by the
                # identity/name gate — a refuse-to-guess no-match from a
                # demonstrably alive API. Commit it directly, and treat it as
                # proof of life: reset the breaker and flush buffered
                # zero-candidate misses as genuine. Counting these as outage
                # evidence livelocked prod (a deterministic cluster of
                # gate-rejected titles at the head of the claim order tripped
                # the breaker on every pass).
                _consecutive_backfill_misses = 0
                await mark_igdb_checked(game_id)
                processed += 1
                for miss_id in pending_no_match:
                    await mark_igdb_checked(miss_id)
                processed += len(pending_no_match)
                pending_no_match.clear()
            else:
                pending_no_match.append(game_id)
                _consecutive_backfill_misses += 1
                if _consecutive_backfill_misses >= _BACKFILL_MISS_CIRCUIT_BREAKER:
                    if await _igdb_canary_alive():
                        # The API answers for a blockbuster, so this streak is
                        # a genuine run of IGDB-absent titles (the
                        # deterministic claim order can serve such a cluster
                        # forever) — commit them and keep going instead of
                        # aborting into a livelock.
                        logger.warning(
                            "IGDB backfill hit %d consecutive zero-candidate "
                            "misses but canary search %r returned candidates — "
                            "API is alive; committing %d pending no-match(es) "
                            "and continuing the pass",
                            _consecutive_backfill_misses,
                            _CANARY_TITLE,
                            len(pending_no_match),
                        )
                        _consecutive_backfill_misses = 0
                        for miss_id in pending_no_match:
                            await mark_igdb_checked(miss_id)
                        processed += len(pending_no_match)
                        pending_no_match.clear()
                    else:
                        breaker_tripped = True
                        logger.error(
                            "IGDB backfill circuit breaker tripped: %d consecutive "
                            "zero-candidate name searches and the canary search %r "
                            "confirmed the outage — aborting the pass and leaving "
                            "%d pending and %d unprocessed row(s) retryable",
                            _consecutive_backfill_misses,
                            _CANARY_TITLE,
                            len(pending_no_match),
                            len(claimed_ids) - next_index,
                        )
        except IGDBRequestFailure as exc:
            # backfill_missing_games returns "rows resolved", so an operational
            # failure it swallows here is invisible in that number — record it
            # so a dead IGDB stops reading as a pass with nothing to do.
            provider_health.record_failure("igdb", exc)
            logger.warning(
                "IGDB backfill leaving game retryable after operational failure: game_id=%s name=%r error=%s",
                game_id,
                row["name"] if row is not None else None,
                exc,
            )
        finally:
            await release_game_claim(game_id, "igdb_claimed_at")
        if breaker_tripped:
            break

    if breaker_tripped:
        # Rows never reached after the trip still hold fresh claims; release
        # them so the next healthy pass can pick them up immediately.
        for game_id in claimed_ids[next_index:]:
            await release_game_claim(game_id, "igdb_claimed_at")
    else:
        for miss_id in pending_no_match:
            await mark_igdb_checked(miss_id)
        processed += len(pending_no_match)

    return processed


def _igdb_headers(client_id: str, token: str) -> dict[str, str]:
    return {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
        "Content-Type": "text/plain",
    }


def _chunked(items: list[_ChunkItem], size: int) -> Iterator[list[_ChunkItem]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def resolve_external_ids_to_igdb(source: int, uids: list[str]) -> dict[str, int]:
    """Map one storefront's product ids to the IGDB game id IGDB associates with each.

    Uses IGDB's external_games endpoint (the authoritative store→game mapping) so a
    caller can tell whether a platform row really belongs to the game its library
    row claims to be. Returns {uid: igdb_game_id} for uids IGDB knows; unknown uids
    are simply omitted. Returns {} if IGDB is unconfigured.

    ``source`` is an ``IGDB_EXTERNAL_SOURCE_*`` value (Steam 1, GOG 5) and is
    filtered on ``external_game_source``, the field that replaced the
    deprecated ``category`` — filtering on the old name matched nothing and
    took the authoritative step out of the resolver without any error to see.
    Those two stores are the ones whose uid format is verified against prod
    data; other stores are deliberately absent: an Epic/PSN/Xbox uid format we
    have not confirmed would silently map nothing, or worse, map the wrong
    thing.
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not client_id or not igdb_credentials_configured() or not uids:
        return {}

    unique = [str(a) for a in dict.fromkeys(uids)]
    token = await _get_token()
    headers = _igdb_headers(client_id, token)

    result: dict[str, int] = {}
    for chunk in _chunked(unique, 100):
        uid_list = ", ".join(f'"{_escape_igdb_search_term(a)}"' for a in chunk)
        query = (
            f"fields game, uid; "
            f"where external_game_source = {source} & uid = ({uid_list}); "
            f"limit 500;"
        )
        rows = await _post_igdb_games(query, headers, url=_IGDB_EXTERNAL_GAMES_URL)
        for row in rows:
            uid = row.get("uid")
            game = row.get("game")
            if uid is not None and game is not None:
                result[str(uid)] = game
    return result


async def resolve_steam_appids_to_igdb(appids: list[str]) -> dict[str, int]:
    """Map Steam appids to IGDB game ids (``resolve_external_ids_to_igdb``, source 1).

    Kept as its own name because it is what every caller outside this module
    asks for — the drift audit, the split/merge checks, the wishlist identity
    probe — and because "Steam appid" is a stronger contract than "some uid".
    """
    return await resolve_external_ids_to_igdb(IGDB_EXTERNAL_SOURCE_STEAM, appids)


@dataclass(frozen=True)
class SeriesMember:
    igdb_id: int
    name: str
    first_release_date: str | None  # ISO YYYY-MM-DD via _unix_to_iso
    game_type: int
    platforms: list[int]  # raw IGDB platform ids


# DLC/bundles/ports are noise for "gap" purposes; keep only main-line entries.
SERIES_MEMBER_GAME_TYPES = frozenset({0, 4, 8, 9})

_SERIES_FIELD_FOR_KIND = {"collection": "collections", "franchise": "franchises"}

_SERIES_MEMBERS_PAGE_SIZE = 500


async def fetch_series_members(kind: str, series_igdb_id: int) -> list["SeriesMember"]:
    """All main-game members of an IGDB collection or franchise.

    kind is "collection" or "franchise" (matching game_series.kind). Paginates
    (IGDB caps at 500/page; few series exceed one page). Raises
    IGDBRequestFailure on API failure — callers decide whether a stale cache is
    acceptable.
    """
    field = _SERIES_FIELD_FOR_KIND.get(kind)
    if field is None:
        raise ValueError(f"kind must be one of {sorted(_SERIES_FIELD_FOR_KIND)}")

    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not client_id:
        raise IGDBRequestFailure(
            "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be set for IGDB enrichment"
        )

    try:
        token = await _get_token()
        headers = _igdb_headers(client_id, token)

        members: list[SeriesMember] = []
        offset = 0
        while True:
            query = (
                "fields id, name, first_release_date, game_type, platforms.id; "
                f"where {field} = ({series_igdb_id}); "
                f"limit {_SERIES_MEMBERS_PAGE_SIZE}; offset {offset};"
            )
            rows = await _post_igdb_games(query, headers)
            for row in rows:
                game_type = row.get("game_type")
                if game_type is None:
                    game_type = 0
                if game_type not in SERIES_MEMBER_GAME_TYPES:
                    continue
                # IGDB files demos/trial builds into series under a main-game
                # game_type ("Infinity Wealth Special Trial Version"), so the
                # enum filter alone lets them through as phantom gaps.
                if is_non_game_title(row.get("name", "")):
                    continue
                members.append(
                    SeriesMember(
                        igdb_id=row["id"],
                        name=row.get("name", ""),
                        first_release_date=_unix_to_iso(row.get("first_release_date")),
                        game_type=game_type,
                        platforms=[
                            p["id"] for p in row.get("platforms") or [] if isinstance(p, dict)
                        ],
                    )
                )
            if len(rows) < _SERIES_MEMBERS_PAGE_SIZE:
                return members
            offset += _SERIES_MEMBERS_PAGE_SIZE
    except IGDBRequestFailure:
        raise
    except Exception as exc:
        raise IGDBRequestFailure(
            f"IGDB series-member fetch failed for {kind} {series_igdb_id}"
        ) from exc


async def fetch_version_parent_aliases(member_igdb_ids: list[int]) -> dict[int, int]:
    """Map edition/re-release IGDB ids onto a series member's id.

    A series' member list (fetch_series_members) contains only the canonical
    entries IGDB's collections/franchises fields point at; an owned
    edition-specific entry (e.g. "The Witcher: Enhanced Edition", igdb id
    283715) is a *different* IGDB game whose ``version_parent`` is the
    canonical member (id 80) — and typically carries no collection/franchise
    of its own, so IGDB backfill never links it into game_series_membership.
    The same happens through ``parent_game``: re-releases/remasters/bundled
    GOTY entries (e.g. the 2021 "Tales from the Borderlands" re-release, igdb
    214139, whose parent_game is the 2014 original 6707) hang off the member
    as *children* rather than versions.

    Queries every game whose version_parent OR parent_game is one of
    ``member_igdb_ids`` and returns {child_igdb_id: canonical_member_igdb_id}.
    version_parent children always alias. parent_game children alias only
    when content_type_from_igdb_category does not classify them as DLC or
    expansion content (categories 1, 2, and 13 — pack-style add-ons) —
    owning a DLC or cosmetic pack must not read as owning the base game, but
    a re-release/remaster/bundle/standalone GOTY entry does. When a child
    carries both links, version_parent wins.

    Raises IGDBRequestFailure on API failure; returns {} for an empty input or
    when IGDB is unconfigured (mirrors fetch_igdb_game_names).
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    ids = [i for i in dict.fromkeys(member_igdb_ids) if i is not None]
    if not client_id or not igdb_credentials_configured() or not ids:
        return {}

    try:
        token = await _get_token()
        headers = _igdb_headers(client_id, token)

        aliases: dict[int, int] = {}
        for chunk in _chunked(ids, 100):
            id_list = ", ".join(str(i) for i in chunk)
            # Paginate like fetch_series_members: IGDB caps a page at 500,
            # and a prolific series' members can have >500 edition children
            # between them — a single page would silently drop aliases.
            offset = 0
            while True:
                query = (
                    "fields id, version_parent, parent_game, category, game_type, name; "
                    f"where version_parent = ({id_list}) | parent_game = ({id_list}); "
                    f"limit {_SERIES_MEMBERS_PAGE_SIZE}; offset {offset};"
                )
                rows = await _post_igdb_games(query, headers)
                for row in rows:
                    child_id = row.get("id")
                    if child_id is None:
                        continue
                    version_parent = row.get("version_parent")
                    if version_parent is not None:
                        aliases[child_id] = version_parent
                        continue
                    parent_game = row.get("parent_game")
                    if parent_game is None:
                        continue
                    # category/game_type fallback mirrors _parse_igdb_item:
                    # IGDB has migrated category -> game_type for some titles.
                    category = row.get("category")
                    effective = category if category is not None else row.get("game_type")
                    # Alias eligibility rides on the shared content classifier
                    # rather than a hand-rolled category set, so the two can't
                    # drift: content_type_from_igdb_category maps DLC (1),
                    # expansion (2), AND pack-style add-ons (13 — cosmetic/BGM/
                    # persona-set packs) to DLC/expansion content, none of
                    # which imply owning the base game. Everything the
                    # classifier deems standalone-ish still aliases: bundle
                    # (3), standalone expansion (4), remake (8), remaster (9),
                    # expanded game (10), port (11), and unclassified
                    # (base_game default).
                    content_type = content_type_from_igdb_category(effective)
                    if content_type in (CONTENT_DLC, CONTENT_EXPANSION):
                        continue
                    aliases[child_id] = parent_game
                if len(rows) < _SERIES_MEMBERS_PAGE_SIZE:
                    break
                offset += _SERIES_MEMBERS_PAGE_SIZE
        return aliases
    except IGDBRequestFailure:
        raise
    except Exception as exc:
        raise IGDBRequestFailure(
            f"IGDB version-parent alias fetch failed for {len(ids)} member ids"
        ) from exc


async def fetch_member_steam_appids(member_igdb_ids: list[int]) -> dict[int, list[str]]:
    """Map series-member IGDB ids onto their Steam appids via external_games.

    IGDB frequently splits a remaster from its original into separate records,
    and ownership can sit on either side under a name that matches neither —
    the library rows "Yakuza 3"/"Yakuza 4" carry the appids of the *remastered*
    Steam releases, so name/id matching reported the "Yakuza 3 Remastered"
    member as a gap the user already owns. A member's own Steam appid is the
    exact bridge: when an owned identifier matches it, the member is owned,
    whatever either side is called.

    Returns {member_igdb_id: [steam appid strings]} — only members with at
    least one Steam listing appear. Filters on ``external_game_source`` (the
    field that replaced the deprecated ``category``, same numeric values).
    Raises IGDBRequestFailure on API failure; returns {} for an empty input or
    when IGDB is unconfigured.
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    ids = [i for i in dict.fromkeys(member_igdb_ids) if i is not None]
    if not client_id or not igdb_credentials_configured() or not ids:
        return {}

    try:
        token = await _get_token()
        headers = _igdb_headers(client_id, token)

        appids: dict[int, list[str]] = {}
        for chunk in _chunked(ids, 100):
            id_list = ", ".join(str(i) for i in chunk)
            query = (
                f"fields game, uid; "
                f"where external_game_source = {IGDB_EXTERNAL_SOURCE_STEAM} "
                f"& game = ({id_list}); "
                f"limit 500;"
            )
            rows = await _post_igdb_games(query, headers, url=_IGDB_EXTERNAL_GAMES_URL)
            for row in rows:
                game = row.get("game")
                uid = row.get("uid")
                if game is None or uid is None:
                    continue
                appids.setdefault(game, []).append(str(uid))
        return appids
    except IGDBRequestFailure:
        raise
    except Exception as exc:
        raise IGDBRequestFailure(
            f"IGDB member Steam-appid fetch failed for {len(ids)} member ids"
        ) from exc


async def fetch_igdb_game_names(igdb_ids: list[int]) -> dict[int, str]:
    """Return {igdb_game_id: name} for the given IGDB game ids (for display)."""
    return {
        igdb_id: record["name"]
        for igdb_id, record in (await fetch_igdb_game_records(igdb_ids)).items()
    }


async def fetch_igdb_game_records(igdb_ids: list[int]) -> dict[int, dict]:
    """Return {igdb_game_id: record} with name + classification-relevant fields.

    The superset behind ``fetch_igdb_game_names``, used by
    revalidate_igdb_matches: ``category``/``game_type`` and the
    parent/version_parent fields let the caller decide whether a stored
    content classification is attributable to this (mismatched) IGDB link and
    should be reset along with it. Each record carries ``name``, ``category``,
    ``game_type``, ``parent_igdb_id``, ``parent_name``,
    ``version_parent_igdb_id``, ``version_parent_name``,
    ``first_release_date`` (ISO ``YYYY-MM-DD``) and ``alternative_names``
    (absent IGDB fields are None / an empty list).

    The last two exist so a caller holding only a record can ask
    ``resolver_would_accept`` whether the name resolver would have linked it:
    the gate reads alternative names and the tiebreak reads the year, so a
    record without them answers a different question than the resolver does.
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    ids = [i for i in dict.fromkeys(igdb_ids) if i is not None]
    if not client_id or not igdb_credentials_configured() or not ids:
        return {}

    token = await _get_token()
    headers = _igdb_headers(client_id, token)

    records: dict[int, dict] = {}
    for chunk in _chunked(ids, 100):
        id_list = ", ".join(str(i) for i in chunk)
        query = (
            "fields id, name, alternative_names.name, first_release_date, "
            "category, game_type, parent_game.id, "
            "parent_game.name, version_parent.id, version_parent.name; "
            f"where id = ({id_list}); limit 500;"
        )
        rows = await _post_igdb_games(query, headers)
        for row in rows:
            if row.get("id") is None or not row.get("name"):
                continue
            raw_parent = row.get("parent_game")
            parent = raw_parent if isinstance(raw_parent, dict) else {}
            raw_version_parent = row.get("version_parent")
            version_parent = (
                raw_version_parent if isinstance(raw_version_parent, dict) else {}
            )
            records[row["id"]] = {
                "name": row["name"],
                "alternative_names": _parse_alternative_names(row),
                "first_release_date": _unix_to_iso(row.get("first_release_date")),
                "category": row.get("category"),
                "game_type": row.get("game_type"),
                "parent_igdb_id": parent.get("id"),
                "parent_name": parent.get("name"),
                "version_parent_igdb_id": version_parent.get("id"),
                "version_parent_name": version_parent.get("name"),
            }
    return records


# On-demand DLC/expansion children catalog, used as a fallback dlc_ownership
# source (tools/detail.py) for primary games that have no Steam DLC catalog
# (e.g. Switch-only titles). Deliberately NOT folded into _FETCH_BY_ID_FIELDS /
# _build_search_game_query: those feed every enrichment/backfill pass, and
# dlcs/expansions are only ever needed lazily, at detail-view time.
_IGDB_CHILDREN_FIELDS = "fields dlcs.id, dlcs.name, expansions.id, expansions.name;"

_IGDB_CHILDREN_CACHE_TTL_DAYS = 7
# Failure markers expire fast: long enough to ride out an IGDB outage without
# hammering the retry ladder from detail views, short enough to self-heal.
_IGDB_CHILDREN_FAILURE_TTL_HOURS = 6


async def fetch_igdb_children(igdb_id: int) -> list[dict] | None:
    """Fetch an IGDB game's DLC + expansion children (one request).

    Returns a combined list of ``{"igdb_id", "name", "kind"}`` entries
    (``kind`` is ``"dlc"`` or ``"expansion"``), empty when the game genuinely
    has neither. Returns ``None`` on any operational failure (missing
    credentials, request error) — a sentinel distinct from a confirmed-empty
    catalog, so ``get_igdb_children_cached`` can serve a stale cache instead
    of mistaking an IGDB outage for "this game has no DLC".
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not client_id or not igdb_credentials_configured():
        return None

    query = f"{_IGDB_CHILDREN_FIELDS} where id = {igdb_id}; limit 1;"

    try:
        token = await _get_token()
        headers = _igdb_headers(client_id, token)
        results = await _post_igdb_games(query, headers)
    except Exception as exc:
        logger.warning("IGDB children fetch failed for igdb_id=%s: %s", igdb_id, exc)
        return None

    if not results:
        return []

    item = results[0]
    children: list[dict] = []
    for dlc in item.get("dlcs") or []:
        if dlc.get("id") is not None and dlc.get("name"):
            children.append({"igdb_id": dlc["id"], "name": dlc["name"], "kind": "dlc"})
    for expansion in item.get("expansions") or []:
        if expansion.get("id") is not None and expansion.get("name"):
            children.append(
                {"igdb_id": expansion["id"], "name": expansion["name"], "kind": "expansion"}
            )
    return children


def _igdb_children_cache_key(igdb_id: int) -> str:
    return f"igdb_children:{igdb_id}"


def _parse_igdb_children_cache(
    raw: str | None,
) -> tuple[datetime, list[dict], bool] | None:
    """Parse a cached meta value, treating any malformed entry as absent.

    Returns (fetched_at, children, failed) — ``failed`` marks a short-lived
    negative entry written after a fetch failure with no prior cache.
    """
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        children = data["children"]
        failed = bool(data.get("failed", False))
        if not isinstance(children, list):
            return None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return fetched_at, children, failed


async def get_igdb_children_cached(
    igdb_id: int, *, allow_fetch: bool = True
) -> list[dict] | None:
    """``fetch_igdb_children`` with a meta-KV cache (7-day TTL, stale-on-failure).

    allow_fetch=False is cache-only (batch detail views): a fresh hit is
    served normally, an expired non-failure entry is served stale (the same
    stale-over-nothing philosophy as fetch failure), and a miss returns None
    without any network call or marker write.

    Mirrors ``data/series_gaps.py``'s series-member caching. A cache hit
    within TTL returns without a network call. On fetch failure (
    ``fetch_igdb_children`` returning ``None``) with a stale cache present,
    serves the stale copy (logged); with no cache at all, writes a short-lived
    NEGATIVE entry (failed=True, ~6h TTL) and returns ``None`` — this runs
    inline in get_game_detail, and without the marker an IGDB outage would
    re-run the full retry ladder on every detail view of every affected game.
    A later successful fetch overwrites the marker with a normal 7-day entry.
    A game with NO children caches an empty list — itself a valid,
    cache-worthy answer that avoids refetching on every detail view.
    """
    key = _igdb_children_cache_key(igdb_id)
    cached = _parse_igdb_children_cache(await get_meta(key))

    if cached is not None:
        fetched_at, children, failed = cached
        age = datetime.now(UTC) - fetched_at
        ttl = timedelta(
            hours=_IGDB_CHILDREN_FAILURE_TTL_HOURS
        ) if failed else timedelta(days=_IGDB_CHILDREN_CACHE_TTL_DAYS)
        if age < ttl:
            # A live failure marker suppresses refetching; the caller sees "no
            # catalog" (dlc_ownership omitted), never an error.
            return None if failed else children

    if not allow_fetch:
        if cached is not None and not cached[2]:
            return cached[1]
        return None

    fetched = await fetch_igdb_children(igdb_id)
    if fetched is None:
        if cached is not None and not cached[2]:
            logger.warning(
                "IGDB children fetch failed for igdb_id=%s; serving stale cache",
                igdb_id,
            )
            return cached[1]
        now = datetime.now(UTC).isoformat()
        await set_meta(
            key, json.dumps({"fetched_at": now, "children": [], "failed": True})
        )
        return None

    now = datetime.now(UTC).isoformat()
    await set_meta(key, json.dumps({"fetched_at": now, "children": fetched}))
    return fetched
