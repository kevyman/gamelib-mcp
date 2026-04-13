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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from weakref import WeakKeyDictionary

import httpx

from .db import get_db, get_steam_platform_row_by_appid, upsert_game_platform_enrichment, upsert_steam_platform_data

logger = logging.getLogger(__name__)

STORE_CACHE_DAYS = 7
STORE_API = "https://store.steampowered.com/api/appdetails"
REVIEWS_API = "https://store.steampowered.com/appreviews/{appid}"
_STEAM_TARGET_REQUEST_INTERVAL = 1.0
_STEAM_MAX_REQUESTS_PER_SECOND = 1
_STEAM_MAX_IN_FLIGHT_REQUESTS = 1
_STEAM_MAX_RETRIES = 3
_STEAM_RETRY_BASE_DELAY_SECONDS = 1.0
_STEAM_RETRY_JITTER_SECONDS = 0.5


class _SteamRequestGate:
    """Shared gate that paces request starts and caps concurrent Steam requests."""

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
        self._loop_states: WeakKeyDictionary[asyncio.AbstractEventLoop, _SteamRequestGateState] = WeakKeyDictionary()
        self._lease_stack: ContextVar[tuple["_SteamRequestGateState", ...]] = ContextVar(
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

    async def __aenter__(self) -> "_SteamRequestGate":
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
    next_slot_at: float = 0.0


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
        retry_at = retry_at.replace(tzinfo=timezone.utc)

    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


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
            if attempt >= _STEAM_MAX_RETRIES or not _should_retry(exc):
                raise

            response = exc.response if isinstance(exc, httpx.HTTPStatusError) else None
            delay_seconds = _retry_delay_seconds(attempt, response)
            logger.warning("Steam request rate-limited or failed for %s; retrying in %.2fs", url, delay_seconds)
            await _sleep_before_retry(delay_seconds)

    if last_error is not None:
        raise last_error

    return {}


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
    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        if store_data is not None:
            steam_tags = _extract_tags(store_data)
            genres = json.dumps([g["description"] for g in store_data.get("genres", [])])
            short_desc = store_data.get("short_description", "")
            raw_date = (store_data.get("release_date") or {}).get("date", "")
            release_date = _parse_steam_date(raw_date)

            await db.execute(
                """UPDATE games SET
                    genres = ?,
                    tags = ?,
                    short_description = ?,
                    release_date = COALESCE(release_date, ?)
                WHERE id = ?""",
                (genres, steam_tags, short_desc, release_date, row["game_id"]),
            )
        await db.commit()

    steam_fields = {"store_cached_at": now}
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

    refreshed = await get_steam_platform_row_by_appid(appid)
    return dict(refreshed) if refreshed else None


async def _fetch_all(appid: int, client: httpx.AsyncClient | None = None) -> tuple[dict | None, dict]:
    """Fetch appdetails and appreviews concurrently. Returns (store_data, review_summary)."""
    async def fetch_store(active_client: httpx.AsyncClient):
        try:
            payload = await _steam_get_json_with_retry(
                active_client,
                STORE_API,
                params={"appids": appid, "filters": "basic,genres,categories,short_description,metacritic,release_date"},
                timeout=15,
            )
            app_data = payload.get(str(appid), {})
            if not app_data.get("success"):
                return None
            return app_data.get("data", {})
        except Exception as exc:
            logger.warning("Steam store details fetch failed for %s: %s", appid, exc)
            return None

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

    async with httpx.AsyncClient() as owned_client:
        store_data, review_summary = await asyncio.gather(
            fetch_store(owned_client),
            fetch_reviews(owned_client),
        )
        return store_data, review_summary


def _extract_tags(data: dict) -> str:
    """Build tag list from genres + categories, deduplicated, max 20."""
    tags = []
    for g in data.get("genres", []):
        tags.append(g["description"])
    for c in data.get("categories", []):
        tags.append(c["description"])
    seen = set()
    unique = []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique.append(t)
    return json.dumps(unique[:20])


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
        return (datetime.now(timezone.utc) - dt).total_seconds() < days * 86400
    except ValueError:
        return False
