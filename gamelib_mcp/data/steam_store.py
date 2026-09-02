"""Lazy Steam Store API enrichment — genres, tags, review score, metacritic."""

import asyncio
import json
import logging
import random
import re
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Self
from weakref import WeakKeyDictionary

import httpx

from .content import classify_steam_app_type
from .db import (
    apply_content_classification,
    get_db,
    get_manual_overrides,
    get_steam_platform_row_by_appid,
    set_meta,
    upsert_game_platform_enrichment,
    upsert_steam_platform_data,
)
from .tag_synonyms import canonical_tag
from .tags import split_features

logger = logging.getLogger(__name__)

STORE_CACHE_DAYS = 7
STORE_API = "https://store.steampowered.com/api/appdetails"
REVIEWS_API = "https://store.steampowered.com/appreviews/{appid}"
# Steam meters these endpoints against a ~5-minute quota (roughly 200 requests
# per window), not a per-second rate. Pacing alone cannot respect a windowed
# quota: a steady 1 req/s is 300 per window, so every window used to run ~200
# requests through and then take 429s for the remainder — a 429 burst every
# five minutes, on the clock. The budget window below is what actually keeps
# us inside the quota; the per-second interval only smooths bursts within it.
_STEAM_TARGET_REQUEST_INTERVAL = 1.6
_STEAM_MAX_REQUESTS_PER_SECOND = 1
_STEAM_MAX_IN_FLIGHT_REQUESTS = 1
_STEAM_BUDGET_WINDOW_SECONDS = 300.0
_STEAM_MAX_REQUESTS_PER_WINDOW = 190
_STEAM_MAX_RETRIES = 3
_STEAM_RETRY_BASE_DELAY_SECONDS = 1.0
_STEAM_RETRY_JITTER_SECONDS = 0.5
# A 429 means the quota is already gone, so backing off only the request that
# happened to catch it just lets the next queued request take the next 429 (and
# each rejection then burns retries, adding load exactly when Steam is asking
# for less). Park the whole gate instead, for at least this long.
_STEAM_RATE_LIMIT_COOLDOWN_SECONDS = 10.0


class _SteamRequestGate:
    """Shared gate that paces request starts and caps concurrent Steam requests."""

    def __init__(
        self,
        *,
        target_interval: float,
        max_requests_per_second: int,
        max_in_flight: int,
        budget_window_seconds: float = _STEAM_BUDGET_WINDOW_SECONDS,
        max_requests_per_window: int = _STEAM_MAX_REQUESTS_PER_WINDOW,
    ) -> None:
        self._target_interval = target_interval
        self._max_requests_per_second = max_requests_per_second
        self._max_in_flight = max_in_flight
        self._budget_window_seconds = budget_window_seconds
        self._max_requests_per_window = max_requests_per_window
        self._loop_states: WeakKeyDictionary[asyncio.AbstractEventLoop, _SteamRequestGateState] = WeakKeyDictionary()
        self._lease_stack: ContextVar[tuple[_SteamRequestGateState, ...]] = ContextVar(
            "steam_request_gate_lease_stack",
            default=(),
        )

    def _get_loop_state(self) -> "_SteamRequestGateState":
        loop = asyncio.get_running_loop()
        state = self._loop_states.get(loop)
        if state is None:
            state = _SteamRequestGateState(
                lock=asyncio.Lock(),
                semaphore=asyncio.Semaphore(self._max_in_flight),
            )
            self._loop_states[loop] = state
        return state

    async def __aenter__(self) -> Self:
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

                    window_cutoff = now - self._budget_window_seconds
                    while state.window_started_at and state.window_started_at[0] <= window_cutoff:
                        state.window_started_at.popleft()

                    wait_seconds = max(0.0, state.next_slot_at - now)
                    # A 429 anywhere parks every caller, not just the unlucky one.
                    wait_seconds = max(wait_seconds, state.cooldown_until - now)
                    if len(state.request_started_at) >= self._max_requests_per_second:
                        oldest = state.request_started_at[0]
                        wait_seconds = max(wait_seconds, (oldest + 1.0) - now)

                    # Quota is spent: wait for the oldest request to age out of
                    # the window rather than spending the next slot on a 429.
                    if len(state.window_started_at) >= self._max_requests_per_window:
                        oldest_in_window = state.window_started_at[0]
                        wait_seconds = max(
                            wait_seconds,
                            (oldest_in_window + self._budget_window_seconds) - now,
                        )

                    if wait_seconds <= 0:
                        state.request_started_at.append(now)
                        state.window_started_at.append(now)
                        state.next_slot_at = max(state.next_slot_at, now) + self._target_interval
                        lease_stack = self._lease_stack.get()
                        self._lease_stack.set((*lease_stack, state))
                        return

                await asyncio.sleep(wait_seconds)
        except BaseException:
            state.semaphore.release()
            raise

    def penalize(self, delay_seconds: float) -> None:
        """Park every caller on this gate after a rate-limit rejection."""
        state = self._get_loop_state()
        cooldown = max(delay_seconds, _STEAM_RATE_LIMIT_COOLDOWN_SECONDS)
        state.cooldown_until = max(state.cooldown_until, time.monotonic() + cooldown)

    def release(self) -> None:
        lease_stack = self._lease_stack.get()
        if not lease_stack:
            raise RuntimeError("Steam request gate released without matching acquire")

        state = lease_stack[-1]
        self._lease_stack.set(lease_stack[:-1])
        state.semaphore.release()


@dataclass
class _SteamRequestGateState:
    lock: asyncio.Lock
    semaphore: asyncio.Semaphore
    request_started_at: deque[float] = field(default_factory=deque)
    window_started_at: deque[float] = field(default_factory=deque)
    next_slot_at: float = 0.0
    cooldown_until: float = 0.0


_STEAM_REQUEST_GATE = _SteamRequestGate(
    target_interval=_STEAM_TARGET_REQUEST_INTERVAL,
    max_requests_per_second=_STEAM_MAX_REQUESTS_PER_SECOND,
    max_in_flight=_STEAM_MAX_IN_FLIGHT_REQUESTS,
)


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

    backoff = _STEAM_RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
    return backoff + random.uniform(0.0, _STEAM_RETRY_JITTER_SECONDS)


async def _sleep_before_retry(delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)


def _should_retry(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or 500 <= exc.response.status_code < 600

    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


async def _steam_get_json_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, int | str],
    timeout: int,
):
    last_error: Exception | None = None

    for attempt in range(_STEAM_MAX_RETRIES + 1):
        try:
            async with _STEAM_REQUEST_GATE:
                resp = await client.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_error = exc
            response = exc.response if isinstance(exc, httpx.HTTPStatusError) else None
            delay_seconds = _retry_delay_seconds(attempt, response)
            # Park the whole gate on any 429, including the terminal one — an
            # exhausted call must not hand the next queued request an immediate
            # slot straight into the same quota outage.
            if response is not None and response.status_code == 429:
                _STEAM_REQUEST_GATE.penalize(delay_seconds)

            if attempt >= _STEAM_MAX_RETRIES or not _should_retry(exc):
                raise

            logger.warning("Steam request rate-limited or failed for %s; retrying in %.2fs", url, delay_seconds)
            await _sleep_before_retry(delay_seconds)

    if last_error is not None:
        raise last_error

    return {}


async def fetch_app_name(appid: int, client: httpx.AsyncClient | None = None) -> str | None:
    """Fetch just an app's store title, for appids with no game_platforms row yet.

    Used by wishlist sync to name a Steam wishlist item that isn't owned (and so
    has no game_platforms row for enrich_game's claim-based path to attach to).
    Goes through the same shared rate gate as enrich_game.
    """
    async def fetch(active_client: httpx.AsyncClient) -> str | None:
        try:
            payload = await _steam_get_json_with_retry(
                active_client,
                STORE_API,
                params={"appids": appid, "filters": "basic"},
                timeout=15,
            )
            app_data = payload.get(str(appid), {})
            if not app_data.get("success"):
                return None
            return (app_data.get("data") or {}).get("name") or None
        except Exception as exc:
            logger.warning("Steam app name fetch failed for %s: %s", appid, exc)
            return None

    if client is not None:
        return await fetch(client)

    # Same 15s the request below passes explicitly; the client default only
    # matters if a future call forgets its own timeout.
    async with httpx.AsyncClient(timeout=15) as owned_client:
        return await fetch(owned_client)


async def enrich_game(appid: int, client: httpx.AsyncClient | None = None) -> dict | None:
    """
    Fetch Steam Store data for appid and cache in DB.
    Returns the full games row dict, or None on failure.
    """
    row = await get_steam_platform_row_by_appid(appid)
    if row is None:
        return None
    if _is_fresh(row["store_cached_at"], STORE_CACHE_DAYS):
        return dict(row)

    store_data, review_summary = await _fetch_all(appid, client=client)
    now = datetime.now(UTC).isoformat()

    async with get_db() as db:
        if store_data is not None:
            steam_tags, steam_features = _extract_tags(store_data)
            genres = json.dumps([g["description"] for g in store_data.get("genres", [])])
            short_desc = store_data.get("short_description", "")
            raw_date = (store_data.get("release_date") or {}).get("date", "")
            release_date = _parse_steam_date(raw_date)

            # Skip any column the user set via update_game so sync never clobbers
            # a manual edit.
            overrides = await get_manual_overrides(db, row["game_id"])
            candidates = [
                ("genres = ?", "genres", genres),
                # Seed-only: never clobber a richer SteamSpy/IGDB/user tag cloud on
                # the 7-day store re-run. SteamSpy owns tags once it has run.
                ("tags = COALESCE(tags, ?)", "tags", steam_tags),
                ("features = ?", "features", steam_features),
                ("short_description = ?", "short_description", short_desc),
                ("release_date = COALESCE(release_date, ?)", "release_date", release_date),
            ]
            assignments = [(sql, value) for sql, col, value in candidates if col not in overrides]
            if assignments:
                set_sql = ", ".join(sql for sql, _ in assignments)
                params = [value for _, value in assignments]
                await db.execute(
                    f"UPDATE games SET {set_sql} WHERE id = ?",
                    (*params, row["game_id"]),
                )
        await db.commit()

    # Always write store_cached_at — on failure this acts as a backoff marker so
    # the background worker doesn't immediately re-claim the row and hot-loop.
    # Game data fields are only written above when store_data is not None.
    steam_fields: dict = {"store_cached_at": now}
    if "review_score" in review_summary:
        steam_fields["steam_review_score"] = review_summary["review_score"]
    if "review_score_desc" in review_summary:
        steam_fields["steam_review_desc"] = review_summary["review_score_desc"]
    await upsert_steam_platform_data(row["game_platform_id"], **steam_fields)

    # Write metacritic to game_platform_enrichment (Steam Store provides this for free)
    if store_data is not None:
        metacritic = store_data.get("metacritic") or {}
        metacritic_score = metacritic.get("score")
        metacritic_url = metacritic.get("url")
        if metacritic_score is not None:
            enrichment_fields: dict = {
                "metacritic_score": metacritic_score,
                "metacritic_cached_at": now,
            }
            if metacritic_url:
                enrichment_fields["metacritic_url"] = metacritic_url
            await upsert_game_platform_enrichment(row["game_platform_id"], **enrichment_fields)

    # Content classification from Steam's own type/fullgame signal. Live-fetch
    # only: the 7-day store cache (steam_platform_data.store_cached_at) persists
    # no type/fullgame/dlc columns, and adding some would need a schema
    # migration, so classification is re-derived on each live fetch and heals on
    # the 7-day refresh cycle rather than on a cache hit.
    if store_data is not None:
        store_type, fullgame_name, fullgame_appid, dlc_appids = _parse_content_fields(store_data)
        classification = classify_steam_app_type(
            store_type,
            title=row["name"],
            fullgame_name=fullgame_name,
            fullgame_appid=fullgame_appid,
        )
        if classification is not None:
            # apply_content_classification carries the manual-override,
            # default-clobber, and self-parent guards, so a bare "game" type
            # never demotes a stored DLC classification.
            await apply_content_classification(row["game_id"], classification, source="steam_store")

        # A base game with DLC carries a dlc:[appids] catalog — cache it in the
        # meta KV for later DLC backfill. Never mints games rows from this list.
        if (store_type or "").strip().lower() == "game" and dlc_appids:
            await set_meta(
                f"steam_dlc_catalog:{appid}",
                json.dumps({"appids": dlc_appids, "fetched_at": now}),
            )

    refreshed = await get_steam_platform_row_by_appid(appid)
    return dict(refreshed) if refreshed else None


STORE_ENRICHMENT_FILTERS = (
    "basic,genres,categories,short_description,metacritic,release_date,dlc"
)


async def fetch_store_appdetails(
    appid: int,
    client: httpx.AsyncClient | None = None,
    *,
    filters: str = STORE_ENRICHMENT_FILTERS,
    raise_on_failure: bool = False,
) -> dict | None:
    """Fetch ONE app's appdetails ``data`` payload; None on failure/no data.

    The store half of ``_fetch_all``, exposed on its own for callers that have
    no use for the review summary (e.g. detect_misclassified_dlc's type probe)
    — every request goes through the shared quota-budgeted gate, so a
    discarded reviews call would halve a probe's effective budget.

    ``filters`` selects the appdetails field groups. The default is the
    enrichment set every long-standing caller wants; data/media.py asks for the
    media groups instead, which enrichment has no use for (and which the 7-day
    store cache predates, so media is fetched on demand rather than read off a
    stored row).

    ``raise_on_failure`` re-raises a request failure instead of folding it into
    the None that also means "Steam has no data for this appid". The default
    keeps the long-standing swallow for enrichment callers; data/media.py needs
    the distinction because it CACHES a legitimate miss — a transient outage
    written down as a 24-hour miss would strip media from every card of that
    game for the rest of the day.
    """
    async def fetch(active_client: httpx.AsyncClient) -> dict | None:
        try:
            payload = await _steam_get_json_with_retry(
                active_client,
                STORE_API,
                params={"appids": appid, "filters": filters},
                timeout=15,
            )
        except Exception as exc:
            if raise_on_failure:
                raise
            logger.warning("Steam store details fetch failed for %s: %s", appid, exc)
            return None
        if not isinstance(payload, dict):
            # A malformed answer is a FAILURE, not "Steam has no data": on the
            # failure-aware path it must raise (the media cache would otherwise
            # remember it as a 24h miss); enrichment callers keep the swallow.
            if raise_on_failure:
                raise ValueError(
                    f"unexpected appdetails payload shape for {appid}: "
                    f"{type(payload).__name__}"
                )
            logger.warning(
                "Unexpected appdetails payload shape for %s: %s",
                appid,
                type(payload).__name__,
            )
            return None
        app_data = payload.get(str(appid), {})
        if not isinstance(app_data, dict):
            # Same stance as the top-level guard: a malformed member is a
            # FAILURE on the failure-aware path, a logged None otherwise —
            # never an AttributeError escaping the best-effort contract.
            if raise_on_failure:
                raise ValueError(
                    f"unexpected appdetails app entry for {appid}: "
                    f"{type(app_data).__name__}"
                )
            logger.warning(
                "Unexpected appdetails app entry for %s: %s",
                appid,
                type(app_data).__name__,
            )
            return None
        if not app_data.get("success"):
            return None
        return app_data.get("data", {})

    if client is not None:
        return await fetch(client)

    # Same 15s the request above passes explicitly; the client default only
    # matters if a future call forgets its own timeout.
    async with httpx.AsyncClient(timeout=15) as owned_client:
        return await fetch(owned_client)


async def _fetch_all(appid: int, client: httpx.AsyncClient | None = None) -> tuple[dict | None, dict]:
    """Fetch appdetails and appreviews concurrently. Returns (store_data, review_summary)."""
    async def fetch_store(active_client: httpx.AsyncClient):
        return await fetch_store_appdetails(appid, client=active_client)

    async def fetch_reviews(active_client: httpx.AsyncClient):
        try:
            payload = await _steam_get_json_with_retry(
                active_client,
                REVIEWS_API.format(appid=appid),
                params={"json": 1, "language": "all", "purchase_type": "all"},
                timeout=10,
            )
            return payload.get("query_summary", {})
        except Exception as exc:
            logger.warning("Steam review summary fetch failed for %s: %s", appid, exc)
            return {}

    if client is not None:
        store_data, review_summary = await asyncio.gather(fetch_store(client), fetch_reviews(client))
        return store_data, review_summary

    # The looser of the two per-request timeouts these calls pass (appdetails
    # 15s, appreviews 10s), so the client default never binds tighter than a
    # request that states its own.
    async with httpx.AsyncClient(timeout=15) as owned_client:
        store_data, review_summary = await asyncio.gather(
            fetch_store(owned_client),
            fetch_reviews(owned_client),
        )
        return store_data, review_summary


def _coerce_appid(value) -> int | None:
    """Best-effort int coercion for a Steam appid (JSON gives it as int or str)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _parse_content_fields(data: dict) -> tuple[str | None, str | None, int | None, list[int]]:
    """Extract Steam's (type, fullgame name, fullgame appid, dlc appids), tolerantly.

    appdetails' ``basic`` group carries ``type`` (game/dlc/demo/music/...) and,
    on a DLC record, ``fullgame: {appid, name}`` pointing at its base game. A
    base game with DLC carries a ``dlc: [appids]`` array (requested via the
    ``dlc`` filter). Any field that is missing or the wrong shape yields
    None/[] — never raises — so a partial payload still enriches everything else.
    """
    raw_type = data.get("type")
    store_type = raw_type.strip() if isinstance(raw_type, str) else None

    fullgame_name: str | None = None
    fullgame_appid: int | None = None
    fullgame = data.get("fullgame")
    if isinstance(fullgame, dict):
        name = fullgame.get("name")
        if isinstance(name, str) and name.strip():
            fullgame_name = name
        fullgame_appid = _coerce_appid(fullgame.get("appid"))

    dlc_appids: list[int] = []
    raw_dlc = data.get("dlc")
    if isinstance(raw_dlc, list):
        for item in raw_dlc:
            appid = _coerce_appid(item)
            if appid is not None:
                dlc_appids.append(appid)

    return store_type, fullgame_name, fullgame_appid, dlc_appids


def _extract_tags(data: dict) -> tuple[str, str]:
    """Build (tags, features) JSON from genres + categories, deduplicated.

    Storefront/platform feature categories (Steam Trading Cards, controller
    support, ...) land in features so they never pollute tag affinity;
    gameplay-mode categories and genres stay tags. Tags capped at 20.
    """
    raw = []
    for g in data.get("genres", []):
        raw.append(g["description"])
    for c in data.get("categories", []):
        raw.append(c["description"])
    seen = set()
    unique = []
    for t in raw:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique.append(t)
    # Split features on the original surface form (the feature-flag set uses
    # specific punctuation, e.g. "cross-platform multiplayer"), then canonicalize
    # the real tags so they share one vocabulary with SteamSpy/IGDB.
    tags, features = split_features(unique)
    canon_seen: set[str] = set()
    canon_tags: list[str] = []
    for t in tags:
        c = canonical_tag(t)
        if c not in canon_seen:
            canon_seen.add(c)
            canon_tags.append(c)
    return json.dumps(canon_tags[:20]), json.dumps(features)


def _parse_steam_date(raw: str) -> str | None:
    """Parse Steam's release date string (e.g. '8 Nov, 2022') to ISO format, best-effort."""
    if not raw:
        return None
    # Try "D Mon, YYYY" or "D Mon YYYY"
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)[,\s]+(\d{4})", raw)
    if m:
        months = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        month = months.get(m.group(2).lower()[:3])
        if month:
            return f"{m.group(3)}-{month}-{int(m.group(1)):02d}"
    # Try bare year
    m = re.match(r"^(\d{4})$", raw.strip())
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _is_fresh(cached_at: str | None, days: int) -> bool:
    if not cached_at or cached_at == "FAILED":
        return False
    try:
        dt = datetime.fromisoformat(cached_at)
        return (datetime.now(UTC) - dt).total_seconds() < days * 86400
    except ValueError:
        return False
