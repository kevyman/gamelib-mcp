"""IsThereAnyDeal price lookups for Steam/GOG/Epic wishlist items.

ITAD API v2 (https://docs.isthereanydeal.com), free key via
isthereanydeal.com/apps/my/. Two-step: Steam appid -> ITAD game UUID
(lookup endpoint), then a batch prices call. Only the *best current deal*
per game is kept, plus two facts about it that cannot be derived locally:
the all-time low ITAD has on record (`historyLow.all`) and when the deal
expires. ITAD itself remains the history-of-record — game_prices caches
"what does it cost right now, and is that as low as it has ever been",
never a price series.

Follows the provider conventions of this package: module-level env reads
with os.getenv fallbacks, explicit httpx timeouts, and failures raised to
the caller (the tool layer decides whether stale cache is acceptable).
"""

import logging
import os
from dataclasses import dataclass, replace
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ITAD_BASE = "https://api.isthereanydeal.com"
_ITAD_TIMEOUT = 20.0
_LOOKUP_BATCH = 100


@dataclass(frozen=True)
class PriceInfo:
    shop: str
    price: float
    regular_price: float | None
    cut_pct: int | None
    currency: str
    deal_url: str | None
    # The all-time low ITAD has ever recorded for the game (historyLow.all),
    # with its own currency: it is a property of the GAME, not of the winning
    # deal, so it is attached after _best_deal picks one. Its currency is
    # carried separately and never assumed to match the deal's — nothing in
    # this codebase converts currencies.
    history_low: float | None = None
    history_low_currency: str | None = None
    # When the chosen deal expires (ISO 8601, nullable in ITAD's payload for
    # an open-ended price). "This sale ends Thursday" is the one urgency
    # signal a model cannot derive from the library.
    deal_ends_at: str | None = None


def is_itad_configured() -> bool:
    return bool(os.getenv("ITAD_API_KEY"))


def _best_deal(deals: list[Any]) -> PriceInfo | None:
    """Pick the cheapest well-formed deal. Pure; tolerates malformed entries."""
    best: PriceInfo | None = None
    for deal in deals or []:
        try:
            shop = deal["shop"]["name"]
            amount = float(deal["price"]["amount"])
            currency = deal["price"]["currency"]
            regular = deal.get("regular") or {}
            info = PriceInfo(
                shop=str(shop),
                price=amount,
                regular_price=float(regular["amount"]) if "amount" in regular else None,
                cut_pct=int(deal["cut"]) if deal.get("cut") is not None else None,
                currency=str(currency),
                deal_url=deal.get("url"),
                deal_ends_at=_expiry(deal.get("expiry")),
            )
        except (TypeError, KeyError, ValueError):
            continue
        if best is None or info.price < best.price:
            best = info
    return best


def _expiry(value: Any) -> str | None:
    """The deal's own expiry timestamp, or None when open-ended/malformed.

    ITAD sends `expiry` as a nullable ISO 8601 string. Kept verbatim rather
    than parsed: it is passed through to the reader, and a value this module
    cannot interpret is better dropped than guessed at."""
    return value if isinstance(value, str) and value else None


def _history_low(entry: Any) -> tuple[float | None, str | None]:
    """(amount, currency) of a price entry's ALL-TIME low, tolerating absence.

    ITAD v2 nests three windows under `historyLow` — `all`, `y1`, `m3`. Only
    `all` is read: "cheaper than it has ever been" is the claim worth caching,
    and the shorter windows would need their own columns to stay honest.
    Malformed or missing values degrade to (None, None) — the same tolerance
    ``_best_deal`` applies to a deal, since one odd payload must not cost the
    whole batch its prices."""
    if not isinstance(entry, dict):
        return None, None
    window = entry.get("historyLow")
    if not isinstance(window, dict):
        return None, None
    low = window.get("all")
    if not isinstance(low, dict):
        return None, None
    try:
        amount = float(low["amount"])
    except (TypeError, KeyError, ValueError):
        return None, None
    currency = low.get("currency")
    return amount, str(currency) if isinstance(currency, str) and currency else None


async def fetch_steam_prices(appids: list[int]) -> dict[int, PriceInfo]:
    """Map Steam appids to their best current deal. Raises on HTTP failure."""
    api_key = os.getenv("ITAD_API_KEY", "")
    if not api_key or not appids:
        return {}
    country = os.getenv("ITAD_COUNTRY", "US")

    async with httpx.AsyncClient(
        timeout=_ITAD_TIMEOUT, headers={"User-Agent": "gamelib-mcp/1.0"}
    ) as client:
        # Step 1: appid -> ITAD UUID.
        uuid_by_appid: dict[int, str] = {}
        for i in range(0, len(appids), _LOOKUP_BATCH):
            chunk = appids[i : i + _LOOKUP_BATCH]
            resp = await client.post(
                f"{_ITAD_BASE}/lookup/id/shop/61/v1",
                params={"key": api_key},
                json=[f"app/{appid}" for appid in chunk],
            )
            resp.raise_for_status()
            payload = resp.json()
            for appid in chunk:
                uuid = payload.get(f"app/{appid}")
                if uuid:
                    uuid_by_appid[appid] = uuid

        if not uuid_by_appid:
            return {}

        # Step 2: batch prices for all found UUIDs.
        resp = await client.post(
            f"{_ITAD_BASE}/games/prices/v3",
            params={"key": api_key, "country": country},
            json=list(uuid_by_appid.values()),
        )
        resp.raise_for_status()
        prices_payload = resp.json()

    deals_by_uuid: dict[str, list[Any]] = {}
    history_by_uuid: dict[str, tuple[float | None, str | None]] = {}
    for entry in prices_payload if isinstance(prices_payload, list) else []:
        if isinstance(entry, dict) and entry.get("id"):
            deals_by_uuid[entry["id"]] = entry.get("deals") or []
            history_by_uuid[entry["id"]] = _history_low(entry)

    result: dict[int, PriceInfo] = {}
    for appid, uuid in uuid_by_appid.items():
        best = _best_deal(deals_by_uuid.get(uuid, []))
        if best is not None:
            low, low_currency = history_by_uuid.get(uuid, (None, None))
            result[appid] = replace(
                best, history_low=low, history_low_currency=low_currency
            )
    return result
