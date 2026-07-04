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
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from weakref import WeakKeyDictionary

import httpx

from .db import (
    _claim_cutoff_iso,
    claim_game_ids_for_igdb,
    load_games_for_igdb_backfill,
    load_platforms_for_games,
    release_game_claim,
    upsert_game_platform_enrichment,
)
from .content import classify_igdb_game, classify_title_override
from .tag_synonyms import canonical_tag
from .tags import is_feature_flag
from .title_normalization import normalize_catalog_title, normalize_search_text

logger = logging.getLogger(__name__)

# Cap on the merged SteamSpy(≤20) + IGDB(≤30) tag cloud, to bound bloat in
# get_game_detail and tag_affinity. Existing (SteamSpy, vote-ranked) tags are kept
# preferentially; IGDB fills the remaining slots.
MERGED_TAG_CAP = 30


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

# IGDB external_games.category for storefront identifier lookups.
IGDB_EXTERNAL_CATEGORY_STEAM = 1


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
_token_expires_at: datetime = datetime.min.replace(tzinfo=timezone.utc)

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
        self._lease_stack: ContextVar[tuple["_IGDBRequestGateState", ...]] = ContextVar(
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

    async def __aenter__(self) -> "_IGDBRequestGate":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
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


async def _get_token() -> str:
    """Return a valid Twitch OAuth2 access token, refreshing if needed."""
    global _token, _token_expires_at

    now = datetime.now(timezone.utc)
    if _token and now < _token_expires_at - timedelta(minutes=10):
        return _token

    client_id = os.environ.get("TWITCH_CLIENT_ID")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise EnvironmentError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be set for IGDB enrichment")

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

    _token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    _token_expires_at = now + timedelta(seconds=expires_in)
    return _token


def _unix_to_iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
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
        retry_at = retry_at.replace(tzinfo=timezone.utc)

    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


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
            async with _IGDB_REQUEST_GATE:
                async with httpx.AsyncClient(timeout=_IGDB_REQUEST_TIMEOUT_SECONDS) as client:
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
        "fields id, name, category, game_type, first_release_date, "
        "genres.name, themes.name, keywords.name, "
        "collections.id, collections.name, franchises.id, franchises.name, "
        "parent_game.id, parent_game.name, "
        "version_parent.id, version_parent.name, version_title, "
        "platforms, release_dates.platform, release_dates.date;",
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
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not client_id:
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


_FETCH_BY_ID_FIELDS = (
    "fields id, name, category, game_type, first_release_date, "
    "genres.name, themes.name, keywords.name, "
    "collections.id, collections.name, franchises.id, franchises.name, "
    "parent_game.id, parent_game.name, "
    "version_parent.id, version_parent.name, version_title, "
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
    ``suppress_errors`` is True.
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not client_id:
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
    a trailing edition segment or a leading article). Their results are gated
    against the *variant* rather than the original name — otherwise a numbered
    edition like "Sea of Thieves: 2026 Edition" could never match the base
    game, because the original's "2026" reads as a sequel number in
    ``titles_conflict_on_identity``. Token-dropping variants change what the
    query means, so they stay gated against the original.
    """
    variants: list[tuple[str, bool]] = []
    seen = {name.casefold()}

    def _add(candidate: str | None, *, identity_preserving: bool) -> None:
        candidate = (candidate or "").strip()
        key = candidate.casefold()
        if candidate and key not in seen:
            seen.add(key)
            variants.append((candidate, identity_preserving))

    _add(normalize_catalog_title(name), identity_preserving=True)
    _add(_LADDER_TRAILING_EDITION_PATTERN.sub("", name).strip(), identity_preserving=True)
    _add(_LADDER_LEADING_THE_PATTERN.sub("", name).strip(), identity_preserving=True)

    tokens = [t for t in re.findall(r"\S+", name) if t.strip(",:;").casefold() not in _LADDER_STOPWORDS]
    if tokens:
        _add(" ".join(tokens), identity_preserving=False)
        if len(tokens) > 2:
            _add(" ".join(tokens[-2:]), identity_preserving=False)

    return variants


def _select_best_match(
    name: str,
    results: list[IGDBGame],
    *,
    allow_inconclusive_fallback: bool,
) -> IGDBGame | None:
    """Pick the best candidate from `results` for query `name`, or None.

    Never collapses onto a different entry in the same series: "Xenoblade
    Chronicles" must not resolve to "Xenoblade Chronicles 2". Candidates whose
    sequel/version identity conflicts with the query are dropped before
    ranking.

    `allow_inconclusive_fallback` controls what happens when the fuzzy match
    is inconclusive (score below cutoff for every candidate): the original,
    IGDB-relevance-ranked search can fall back to its top identity-compatible
    hit, but a narrower fallback-ladder variant query (Fix 4) must not — an
    unrelated result from an overly-broad query (e.g. "Seance" alone matching
    unrelated "Silly Seance"-type titles) must not be accepted just because it
    was the only one returned.
    """
    from .db import extract_best_fuzzy_key, titles_conflict_on_identity

    choices = {
        i: g.name
        for i, g in enumerate(results)
        if not titles_conflict_on_identity(name, g.name)
    }
    if not choices:
        # Every candidate disagrees on the sequel number — a confident wrong match
        # is worse than none. Let the caller fall back to the normalized name.
        return None

    # Prefer an exact title match (under the same normalization used for
    # library identity) over IGDB's relevance ranking. This is what rescues
    # e.g. "Persona 3 Reload": the base game's title is an exact match while
    # every DLC/cosmetic pack's title has a longer suffix, so it wins even
    # though IGDB's own relevance model ranked it below all of them.
    normalized_query = normalize_search_text(name)
    exact_matches = [
        i for i in choices if normalize_search_text(results[i].name) == normalized_query
    ]
    if exact_matches:
        primary_exact = [i for i in exact_matches if results[i].is_primary_library_item]
        exact_idx = primary_exact[0] if primary_exact else exact_matches[0]
        return results[exact_idx]

    best_idx: int | None = extract_best_fuzzy_key(name, choices, cutoff=70)
    if best_idx is None:
        if not allow_inconclusive_fallback:
            return None
        # Fuzzy was inconclusive; take IGDB's top *identity-compatible* relevance
        # hit rather than forcing position 0 (which may be a conflicting entry).
        best_idx = next(iter(choices))

    return results[best_idx]


async def resolve_game(
    name: str,
    igdb_platform_id: int | tuple[int, ...] | None,
    *,
    suppress_errors: bool = True,
) -> IGDBGame | None:
    """
    Find the best IGDB match for a game name + platform. Returns None if not found
    or IGDB credentials are not configured.
    """
    if not os.environ.get("TWITCH_CLIENT_ID"):
        return None

    results = await search_game(name, igdb_platform_id, suppress_errors=suppress_errors)
    if not results:
        # Try without platform filter as fallback
        if igdb_platform_id is not None:
            results = await search_game(name, igdb_platform_id=None, suppress_errors=suppress_errors)

    if results:
        return _select_best_match(name, results, allow_inconclusive_fallback=True)

    # Zero results even without a platform filter: work through a ladder of
    # alternate query strings (Fix 4). Stop at the first variant whose results
    # produce an accepted match; a variant that returns only unrelated titles
    # must not be accepted just because it's non-empty.
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

        # Identity-preserving variants gate against the variant itself: the
        # transformation already vouches for series identity, and the original
        # may carry an edition number ("… 2026 Edition") that would wrongly
        # read as a sequel marker against the base game's title.
        gate_name = variant if identity_preserving else name
        match = _select_best_match(gate_name, variant_results, allow_inconclusive_fallback=False)
        if match is not None:
            return match

    return None


async def resolve_and_link_game(
    name: str,
    igdb_platform_id: int | tuple[int, ...] | None,
    candidates: dict[int, str],
    *,
    platform: str | None = None,
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
    """
    from .db import find_game_by_name_fuzzy, get_game_by_igdb_id, get_db

    igdb_game = await resolve_game(name, igdb_platform_id)
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
    from .db import get_db, get_game_by_igdb_id, get_manual_overrides, upsert_game

    now = datetime.now(timezone.utc).isoformat()
    parent_game_id: int | None = None
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
            parent_game_id = await upsert_game(appid=None, name=igdb_game.parent_name)

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
            """SELECT tags,
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
        updates: dict = {"igdb_id": igdb_game.igdb_id, "igdb_cached_at": now}
        if igdb_game.platforms:
            # NULL means "not fetched yet"; an empty fetch keeps NULL so the
            # deals tool can distinguish unknown from confirmed-single-platform.
            updates["igdb_platforms"] = json.dumps(igdb_game.platforms)
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
        # IGDB search can return a bare main-game hit on a later pass, and
        # silently flipping a nested DLC back to primary would resurface it as
        # its own library item. Only apply when the fetch carries a real signal
        # or the stored row is still at the default. Mirrors the enrich
        # re-fetch guard.
        new_is_default = (
            igdb_game.content_type == "base_game"
            and igdb_game.is_primary_library_item
            and parent_game_id is None
        )
        stored_is_default = (
            row["content_type"] == "base_game"
            and bool(row["is_primary_library_item"])
            and row["parent_game_id"] is None
        )
        if not new_is_default or stored_is_default:
            if "content_type" not in overrides:
                updates["content_type"] = igdb_game.content_type
            if parent_game_id is not None and "parent_game_id" not in overrides:
                updates["parent_game_id"] = parent_game_id
            # Without a real (distinct) parent a nested item has nowhere to be
            # reached from, so a self-referential parent forces it to stay primary.
            is_primary = igdb_game.is_primary_library_item or self_referential_parent
            if "is_primary_library_item" not in overrides:
                updates["is_primary_library_item"] = int(is_primary)

        cols_sql = ", ".join(f"{col} = ?" for col in updates)
        await db.execute(
            f"UPDATE games SET {cols_sql} WHERE id = ?",
            (*updates.values(), game_id),
        )
        await db.commit()

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
    from .db import get_db

    checked_at = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            "UPDATE games SET igdb_cached_at = ? WHERE id = ?",
            (checked_at, game_id),
        )
        await db.commit()


async def backfill_missing_games(limit: int = 10) -> int:
    stale_before = _claim_cutoff_iso()
    game_ids = await claim_game_ids_for_igdb(limit=limit, stale_before=stale_before)
    if not game_ids:
        return 0

    rows = await load_games_for_igdb_backfill(game_ids)
    rows_by_id = {row["id"]: row for row in rows}
    processed = 0

    for game_id in game_ids:
        row = rows_by_id.get(game_id)
        try:
            if row is None:
                continue

            igdb_game: IGDBGame | None = None
            existing_igdb_id = row["igdb_id"]
            if existing_igdb_id:
                # Row already has a matched igdb_id (e.g. from an earlier pass) —
                # fetch it directly instead of re-resolving by name, which can
                # drift onto a different, wrong candidate. Only trust the
                # by-id result when it actually carries platform data; an empty
                # fetch falls through to the normal name-based resolution below.
                fetched = await fetch_game_by_id(existing_igdb_id, suppress_errors=False)
                if fetched is not None and fetched.platforms:
                    igdb_game = fetched

            if igdb_game is None:
                platform_hint = await choose_igdb_platform_hint(game_id)
                igdb_game = await resolve_game(
                    normalize_catalog_title(row["name"]),
                    platform_hint,
                    suppress_errors=False,
                )
            if igdb_game is not None:
                try:
                    await _apply_igdb_metadata(game_id, igdb_game)
                    await upsert_backfill_platform_release_dates(game_id, igdb_game)
                except sqlite3.IntegrityError:
                    logger.warning(
                        "IGDB backfill skipped duplicate igdb_id for game_id=%s name=%r igdb_id=%s",
                        game_id,
                        row["name"],
                        igdb_game.igdb_id,
                    )
                    await mark_igdb_checked(game_id)
            else:
                await mark_igdb_checked(game_id)
            processed += 1
        except IGDBRequestFailure as exc:
            logger.warning(
                "IGDB backfill leaving game retryable after operational failure: game_id=%s name=%r error=%s",
                game_id,
                row["name"] if row is not None else None,
                exc,
            )
        finally:
            await release_game_claim(game_id, "igdb_claimed_at")

    return processed


def _igdb_headers(client_id: str, token: str) -> dict[str, str]:
    return {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
        "Content-Type": "text/plain",
    }


def _chunked(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def resolve_steam_appids_to_igdb(appids: list[str]) -> dict[str, int]:
    """Map Steam appids to the IGDB game id IGDB associates with each.

    Uses IGDB's external_games endpoint (the authoritative store→game mapping) so a
    caller can tell whether a Steam platform row really belongs to the game its
    library row claims to be. Returns {appid: igdb_game_id} for appids IGDB knows;
    unknown appids are simply omitted. Returns {} if IGDB is unconfigured.
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not client_id or not igdb_credentials_configured() or not appids:
        return {}

    unique = [str(a) for a in dict.fromkeys(appids)]
    token = await _get_token()
    headers = _igdb_headers(client_id, token)

    result: dict[str, int] = {}
    for chunk in _chunked(unique, 100):
        uid_list = ", ".join(f'"{_escape_igdb_search_term(a)}"' for a in chunk)
        query = (
            f"fields game, uid; "
            f"where category = {IGDB_EXTERNAL_CATEGORY_STEAM} & uid = ({uid_list}); "
            f"limit 500;"
        )
        rows = await _post_igdb_games(query, headers, url=_IGDB_EXTERNAL_GAMES_URL)
        for row in rows:
            uid = row.get("uid")
            game = row.get("game")
            if uid is not None and game is not None:
                result[str(uid)] = game
    return result


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
    """Map edition/version IGDB ids onto a series member's id via version_parent.

    A series' member list (fetch_series_members) contains only the canonical
    entries IGDB's collections/franchises fields point at; an owned
    edition-specific entry (e.g. "The Witcher: Enhanced Edition", igdb id
    283715) is a *different* IGDB game whose ``version_parent`` is the
    canonical member (id 80) — and typically carries no collection/franchise
    of its own, so IGDB backfill never links it into game_series_membership.

    Queries every game whose version_parent is one of ``member_igdb_ids`` and
    returns {edition_igdb_id: canonical_member_igdb_id}, so a caller's
    have-set of owned/wishlisted igdb_ids can also match on the edition.

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
            query = (
                "fields id, version_parent, name; "
                f"where version_parent = ({id_list}); "
                "limit 500;"
            )
            rows = await _post_igdb_games(query, headers)
            for row in rows:
                edition_id = row.get("id")
                parent_id = row.get("version_parent")
                if edition_id is not None and parent_id is not None:
                    aliases[edition_id] = parent_id
        return aliases
    except IGDBRequestFailure:
        raise
    except Exception as exc:
        raise IGDBRequestFailure(
            f"IGDB version-parent alias fetch failed for {len(ids)} member ids"
        ) from exc


async def fetch_igdb_game_names(igdb_ids: list[int]) -> dict[int, str]:
    """Return {igdb_game_id: name} for the given IGDB game ids (for display)."""
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    ids = [i for i in dict.fromkeys(igdb_ids) if i is not None]
    if not client_id or not igdb_credentials_configured() or not ids:
        return {}

    token = await _get_token()
    headers = _igdb_headers(client_id, token)

    names: dict[int, str] = {}
    for chunk in _chunked(ids, 100):
        id_list = ", ".join(str(i) for i in chunk)
        query = f"fields id, name; where id = ({id_list}); limit 500;"
        rows = await _post_igdb_games(query, headers)
        for row in rows:
            if row.get("id") is not None and row.get("name"):
                names[row["id"]] = row["name"]
    return names
