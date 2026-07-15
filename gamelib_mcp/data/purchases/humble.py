"""Humble Bundle purchase-history importer.

Reads the order list (``/api/v1/user/order`` → gamekeys) and each order's
detail (``/api/v1/order/<gamekey>?all_tpkds=true``). Auth is the
``_simpleauth_sess`` browser cookie, stored in the same ``{name: value}``
JSON file shape as the Nintendo cookie files — set it with
``create_session_ingest_link(provider="humble")``.

Record building:
- Game keys in ``tpkd_dict.all_tpks`` are preferred — they are the actual
  redeemable games. ``key_type`` maps to the library platform: "steam" →
  steam, "gog" → gog, anything else (generic keys, origin, …) → "other".
- Orders without tpks fall back to ``subproducts``; those have no platform
  signal, so they land on "other" (deliberately NOT guessed as steam).
- Orders with neither tpks nor subproducts (soundtrack-only, ebook rewards,
  …) are skipped with a reason; no deeper non-game detection is attempted.
- Multi-game orders split ``amount_spent`` evenly, rounded to 2 decimals with
  the last item absorbing the rounding remainder so the parts sum exactly to
  the order total; ``bundle_name`` is set only for category "bundle" orders
  with more than one game (per spec — subscription months still split, but
  keep purchase_source "subscription" as their grouping signal).
- category "subscriptioncontent"/"subscriptionplan" (Humble Choice) →
  purchase_source "subscription"; everything else → "humble". amount_spent 0
  (freebies) → price 0.0. A missing currency is assumed USD.
"""

import asyncio
import json
import logging
import os

import httpx

from . import PurchaseRecord, normalize_purchase_date
from gamelib_mcp.data.db import default_data_dir

logger = logging.getLogger(__name__)

PURCHASE_SOURCE = "humble"

_ORDER_LIST_URL = "https://www.humblebundle.com/api/v1/user/order"
_ORDER_DETAIL_URL = "https://www.humblebundle.com/api/v1/order/{gamekey}"
# Politeness delay between sequential order-detail requests.
_REQUEST_DELAY_SECONDS = 0.2

_KEY_TYPE_TO_PLATFORM = {"steam": "steam", "gog": "gog"}

_AUTH_ERROR = (
    "Humble Bundle API request was not authenticated (_simpleauth_sess cookie "
    "missing or expired) — run create_session_ingest_link(provider=\"humble\") and "
    "open the link to paste fresh cookies from humblebundle.com."
)


def _load_humble_cookies() -> dict[str, str] | None:
    """Load Humble session cookies from HUMBLE_COOKIES_FILE (same shape as
    the Nintendo cookie files: {name: value} or a Cookie Editor array)."""
    fallback_path = str(default_data_dir() / "humble_cookies.json")
    configured_path = os.getenv("HUMBLE_COOKIES_FILE") or fallback_path
    candidate_paths = [configured_path]
    if configured_path != fallback_path:
        candidate_paths.append(fallback_path)

    raw = None
    for path in candidate_paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            break
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("Failed to load Humble cookies from %s: %s", path, exc)
            return None

    if raw is None:
        return None

    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw if isinstance(c, dict) and "name" in c and "value" in c}
    if isinstance(raw, dict):
        return raw
    return None


def _split_amount(amount: float, count: int) -> list[float]:
    """Even split rounded to cents; the last share absorbs the remainder."""
    if count <= 0:
        return []
    share = round(amount / count, 2)
    shares = [share] * count
    shares[-1] = round(amount - share * (count - 1), 2)
    return shares


def _order_games(order: dict) -> list[tuple[str, str]]:
    """Extract [(title, platform)] from an order — tpks first, subproducts as
    the fallback."""
    games: list[tuple[str, str]] = []
    tpks = (order.get("tpkd_dict") or {}).get("all_tpks") or []
    for tpk in tpks:
        if not isinstance(tpk, dict):
            continue
        name = tpk.get("human_name")
        if not name or not isinstance(name, str):
            continue
        key_type = str(tpk.get("key_type") or "").lower()
        games.append((name.strip(), _KEY_TYPE_TO_PLATFORM.get(key_type, "other")))
    if games:
        return games

    for sub in order.get("subproducts") or []:
        if not isinstance(sub, dict):
            continue
        name = sub.get("human_name")
        if not name or not isinstance(name, str):
            continue
        # No platform signal on a subproduct — "other" beats a wrong guess.
        games.append((name.strip(), "other"))
    return games


def records_from_order(order: dict) -> tuple[list[PurchaseRecord], list[dict]]:
    """Convert one order-detail payload into (records, skipped)."""
    product = order.get("product") or {}
    order_name = product.get("human_name") or order.get("gamekey") or "(unknown order)"
    category = str(product.get("category") or "").lower()

    games = _order_games(order)
    if not games:
        return [], [{"description": str(order_name), "reason": "no game keys or subproducts"}]

    try:
        amount_spent = float(order.get("amount_spent") or 0.0)
    except (TypeError, ValueError):
        amount_spent = 0.0
    currency = str(order.get("currency") or "USD")
    acquired_at = normalize_purchase_date(order.get("created"))
    source = "subscription" if category.startswith("subscription") else PURCHASE_SOURCE
    bundle_name = (
        str(order_name) if category == "bundle" and len(games) > 1 else None
    )
    shares = _split_amount(amount_spent, len(games))

    records = [
        PurchaseRecord(
            title=title,
            platform=platform,
            purchase_source=source,
            acquired_at=acquired_at,
            price_paid=share,
            price_currency=currency,
            bundle_name=bundle_name,
        )
        for (title, platform), share in zip(games, shares, strict=True)
    ]
    return records, []


def _check_auth(response: httpx.Response) -> None:
    if response.status_code in (401, 403):
        raise RuntimeError(_AUTH_ERROR)
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type or response.text.lstrip()[:1] == "<":
        # Unauthenticated API calls bounce to the HTML login page.
        raise RuntimeError(_AUTH_ERROR)


async def fetch_humble_purchases(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[list[PurchaseRecord], list[dict]]:
    """Fetch every Humble order as purchase records.

    Raises RuntimeError on missing/stale auth; the orchestrator catches per
    source. ``transport`` exists for tests (httpx.MockTransport).
    """
    cookies = _load_humble_cookies()
    if not cookies:
        raise RuntimeError(
            "No Humble Bundle session cookies found (HUMBLE_COOKIES_FILE not "
            "set or missing) — run create_session_ingest_link(provider=\"humble\") first."
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        ),
        "Accept": "application/json",
    }

    records: list[PurchaseRecord] = []
    skipped: list[dict] = []
    async with httpx.AsyncClient(
        cookies=cookies, follow_redirects=True, timeout=30, transport=transport
    ) as client:
        list_resp = await client.get(_ORDER_LIST_URL, headers=headers)
        _check_auth(list_resp)
        list_resp.raise_for_status()
        order_list = list_resp.json()
        if not isinstance(order_list, list):
            raise RuntimeError(
                f"Unexpected Humble order-list payload: {type(order_list).__name__}"
            )
        gamekeys = [
            entry["gamekey"]
            for entry in order_list
            if isinstance(entry, dict) and entry.get("gamekey")
        ]

        for index, gamekey in enumerate(gamekeys):
            if index:
                await asyncio.sleep(_REQUEST_DELAY_SECONDS)
            detail_resp = await client.get(
                _ORDER_DETAIL_URL.format(gamekey=gamekey),
                params={"all_tpkds": "true"},
                headers=headers,
            )
            _check_auth(detail_resp)
            detail_resp.raise_for_status()
            order = detail_resp.json()
            if not isinstance(order, dict):
                raise RuntimeError(
                    f"Unexpected Humble order payload for {gamekey}: "
                    f"{type(order).__name__}"
                )
            order_records, order_skipped = records_from_order(order)
            records.extend(order_records)
            skipped.extend(order_skipped)

    logger.info(
        "Humble: fetched %d orders → %d purchases, %d skipped",
        len(gamekeys), len(records), len(skipped),
    )
    return records, skipped
