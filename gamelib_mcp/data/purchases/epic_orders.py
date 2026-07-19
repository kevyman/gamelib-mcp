"""Epic Games Store purchase-history importer.

Reads the order history behind https://www.epicgames.com/account/transactions
via the paginated ``ajaxGetOrderHistory`` JSON endpoint. This is the account
WEBSITE, not the launcher API: Legendary's launcher session (``data/epic.py``)
cannot see order history or prices, so auth here is browser cookies exported
from a signed-in epicgames.com tab — stored in the same ``{name: value}`` JSON
file shape as the Humble/Nintendo cookie files and set with
``create_session_ingest_link(provider="epic")``.

Record building:
- The payload is community-documented, not official, so every field is parsed
  defensively. Each order carries ``createdAtMillis`` (unix milliseconds),
  ``orderStatus``/``orderType``, and an ``items`` list of
  ``{description, amount, offerId}``.
- ``amount`` is a locale-FORMATTED money string ("$19.99", "R$ 29,99"), not a
  number — ``locale=en-US`` is requested for predictable formatting, and the
  parser handles both decimal-point and decimal-comma shapes plus a symbol →
  ISO-code map. A bare number is taken at face value (decimal units).
- Orders whose ``orderStatus`` is present but not COMPLETED are skipped with
  the status in the reason (visible drift, not silent drops); ``orderType``
  REFUND is skipped likewise — importing a refund would double-count spend.
- A zero amount is an Epic giveaway claim (the weekly free game) →
  purchase_source "free" (the vocabulary's designated bucket for exactly
  this); paid items get "epic".
- When NO item carries its own amount but an order total is derivable, the
  total splits evenly across items (last share absorbs rounding —
  humble.py/gog_orders.py convention).
- Epic exposes no per-item content typing here, so an addon-ish NAME
  (match_addon_name / title override) becomes the content_type hint — DLC
  purchases match exact-name-only and mint nested, never as phantom base
  games (humble.py convention).
- store_identifier is the item's ``offerId`` when present. It is NOT the
  library's epic_artifact_id (offers and launcher assets are different id
  spaces), so there is deliberately no IDENTIFIER_TYPES entry — matching
  falls back to title, and the offerId is only carried for auditability.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone

import httpx

from . import PurchaseRecord, normalize_purchase_date
from gamelib_mcp.data.content import classify_title_override, match_addon_name
from gamelib_mcp.data.db import default_data_dir

logger = logging.getLogger(__name__)

PLATFORM = "epic"
PURCHASE_SOURCE = "epic"

_ORDER_HISTORY_URL = "https://www.epicgames.com/account/v2/payment/ajaxGetOrderHistory"
# ~10 orders per page; weekly-freebie accounts accumulate long histories, so
# the cap is generous — hitting it is reported in skipped, never silent.
_MAX_PAGES = 200
# Politeness delay between sequential page requests (humble.py convention).
_REQUEST_DELAY_SECONDS = 0.2

# Longest symbols first so "R$" never half-matches "$".
_CURRENCY_SYMBOLS = (
    ("R$", "BRL"),
    ("zł", "PLN"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("¥", "JPY"),
    ("$", "USD"),
)

_NUMBER_RE = re.compile(r"\d[\d.,\s ]*")
_ISO_CODE_RE = re.compile(r"\b([A-Z]{3})\b")

_AUTH_ERROR = (
    "Epic Games order-history request was not authenticated (epicgames.com "
    "session cookies missing or expired) — run "
    "create_session_ingest_link(provider=\"epic\") and open the link to paste "
    "fresh cookies from www.epicgames.com."
)


def _load_epic_cookies() -> dict[str, str] | None:
    """Load Epic session cookies from EPIC_COOKIES_FILE (same shape as the
    Humble/Nintendo cookie files: {name: value} or a Cookie Editor array)."""
    fallback_path = str(default_data_dir() / "epic_cookies.json")
    configured_path = os.getenv("EPIC_COOKIES_FILE") or fallback_path
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
            logger.warning("Failed to load Epic cookies from %s: %s", path, exc)
            return None

    if raw is None:
        return None

    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw if isinstance(c, dict) and "name" in c and "value" in c}
    if isinstance(raw, dict):
        return raw
    return None


def _parse_money(value: object) -> tuple[float | None, str | None]:
    """(amount, currency) from a formatted money string or bare number.

    Handles decimal-point and decimal-comma locales ("$19.99", "R$ 29,99",
    "1.234,56 zł", "1,234.56"): with both separators present the rightmost is
    the decimal one; with a single separator it is decimal only when followed
    by 1–2 digits (else thousands). Currency comes from the symbol map or an
    uppercase ISO code in the string; None when absent.
    """
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return (float(value), None) if value >= 0 else (None, None)
    if not isinstance(value, str):
        return None, None

    currency = None
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in value:
            currency = code
            break
    if currency is None:
        iso = _ISO_CODE_RE.search(value)
        if iso:
            currency = iso.group(1)

    match = _NUMBER_RE.search(value)
    if not match:
        return None, currency
    digits = re.sub(r"[\s ]", "", match.group(0)).rstrip(".,")
    decimal_sep: str | None
    if "." in digits and "," in digits:
        decimal_sep = "." if digits.rfind(".") > digits.rfind(",") else ","
    elif "." in digits or "," in digits:
        sep = "." if "." in digits else ","
        decimal_sep = sep if len(digits.rsplit(sep, 1)[1]) in (1, 2) else None
    else:
        decimal_sep = None

    if decimal_sep is None:
        return float(re.sub(r"[.,]", "", digits)), currency
    head, _, tail = digits.rpartition(decimal_sep)
    head = re.sub(r"[.,]", "", head)
    return float(f"{head or 0}.{tail}"), currency


def _order_date(order: dict) -> str | None:
    """YYYY-MM-DD from ``createdAtMillis`` (int or numeric string), tolerating
    an ISO-ish ``createdAt``/``created`` string as a fallback."""
    value = order.get("createdAtMillis")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    return normalize_purchase_date(order.get("createdAt") or order.get("created"))


def _split_amount(amount: float, count: int) -> list[float]:
    """Even split rounded to cents; the last share absorbs the remainder."""
    if count <= 0:
        return []
    share = round(amount / count, 2)
    shares = [share] * count
    shares[-1] = round(amount - share * (count - 1), 2)
    return shares


def _order_total(order: dict) -> tuple[float | None, str | None]:
    """Order-level total for the split-evenly fallback."""
    for key in ("total", "presentmentTotal", "totalAmount"):
        amount, currency = _parse_money(order.get(key))
        if amount is not None:
            return amount, currency
    return None, None


def _order_currency(order: dict, *candidates: str | None) -> str:
    """First explicit currency wins, then the order's own key, then USD."""
    for candidate in candidates:
        if candidate:
            return candidate
    for key in ("currency", "presentmentCurrency"):
        value = order.get(key)
        if isinstance(value, str) and len(value) == 3 and value.isalpha():
            return value.upper()
    return "USD"


def parse_order(order: dict) -> tuple[list[PurchaseRecord], list[dict]]:
    """Convert one order payload into (records, skipped)."""
    order_label = str(order.get("orderId") or "(unknown order)")

    status = str(order.get("orderStatus") or "").upper()
    if status and status != "COMPLETED":
        return [], [
            {"description": order_label, "reason": f"order status {status} (not COMPLETED)"}
        ]
    order_type = str(order.get("orderType") or "").upper()
    if order_type == "REFUND":
        return [], [
            {"description": order_label, "reason": "refund order — money returned, not spend"}
        ]

    items = order.get("items")
    if not isinstance(items, list) or not items:
        return [], [{"description": order_label, "reason": "order has no items"}]

    acquired_at = _order_date(order)

    entries: list[tuple[str, str | None, float | None, str | None]] = []
    skipped: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            skipped.append({"description": repr(item), "reason": "not an item object"})
            continue
        title = item.get("description")
        if not title or not isinstance(title, str):
            skipped.append({"description": order_label, "reason": "item missing description"})
            continue
        offer_id = item.get("offerId")
        amount, currency = _parse_money(item.get("amount"))
        entries.append(
            (" ".join(title.split()), str(offer_id) if offer_id else None, amount, currency)
        )

    if not entries:
        return [], skipped

    # Split-evenly fallback: only when NO item carried its own amount but an
    # order total is derivable (mirrors humble.py/gog_orders.py convention).
    if all(amount is None for _, _, amount, _ in entries):
        total, total_currency = _order_total(order)
        if total is not None:
            shares = _split_amount(total, len(entries))
            entries = [
                (title, offer_id, share, currency or total_currency)
                for (title, offer_id, _, currency), share in zip(
                    entries, shares, strict=True
                )
            ]

    records = []
    for title, offer_id, amount, currency in entries:
        # Epic exposes no per-item content typing here — an addon-ish NAME is
        # the only nested signal (humble.py convention), so DLC/season-pass
        # purchases match exact-name-only and mint nested instead of as
        # phantom base games.
        addon = match_addon_name(title)
        if addon is None:
            override = classify_title_override(title)
            if override is not None and not override.is_primary_library_item:
                addon = (override.content_type, "title override")
        records.append(
            PurchaseRecord(
                title=title,
                platform=PLATFORM,
                # Zero spend on Epic is the weekly-giveaway claim — a
                # no-strings "free" acquisition, not a store purchase.
                purchase_source=(
                    "free" if amount is not None and amount == 0.0 else PURCHASE_SOURCE
                ),
                acquired_at=acquired_at,
                price_paid=amount,
                price_currency=(
                    _order_currency(order, currency) if amount is not None else None
                ),
                store_identifier=offer_id,
                content_type=addon[0] if addon is not None else None,
            )
        )
    return records, skipped


def _check_auth(response: httpx.Response) -> None:
    if response.status_code in (401, 403):
        raise RuntimeError(_AUTH_ERROR)
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type or response.text.lstrip()[:1] == "<":
        # An unauthenticated ajax call bounces to the HTML login page.
        raise RuntimeError(_AUTH_ERROR)


async def fetch_epic_purchases(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[list[PurchaseRecord], list[dict]]:
    """Fetch the full Epic order history as purchase records.

    Raises RuntimeError on missing/stale auth; the orchestrator catches per
    source. ``transport`` exists for tests (httpx.MockTransport).
    """
    cookies = _load_epic_cookies()
    if not cookies:
        raise RuntimeError(
            "No Epic Games session cookies found (EPIC_COOKIES_FILE not set or "
            "missing) — run create_session_ingest_link(provider=\"epic\") first."
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        ),
        "Accept": "application/json",
    }

    orders: list[dict] = []
    pagination_capped = False
    async with httpx.AsyncClient(
        cookies=cookies, follow_redirects=True, timeout=30, transport=transport
    ) as client:
        next_page_token = ""
        for page in range(_MAX_PAGES):
            if page:
                await asyncio.sleep(_REQUEST_DELAY_SECONDS)
            params = {"sortDir": "DESC", "sortBy": "DATE", "locale": "en-US"}
            if next_page_token:
                params["nextPageToken"] = next_page_token
            resp = await client.get(_ORDER_HISTORY_URL, params=params, headers=headers)
            _check_auth(resp)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError(
                    f"Unexpected Epic order-history payload: {type(data).__name__}"
                )
            page_orders = data.get("orders")
            if isinstance(page_orders, list):
                orders.extend(o for o in page_orders if isinstance(o, dict))
            token = data.get("nextPageToken")
            next_page_token = token if isinstance(token, str) else ""
            if not next_page_token:
                break
        else:
            pagination_capped = True

    records: list[PurchaseRecord] = []
    skipped: list[dict] = []
    for order in orders:
        order_records, order_skipped = parse_order(order)
        records.extend(order_records)
        skipped.extend(order_skipped)
    if pagination_capped:
        skipped.append(
            {
                "description": "(pagination)",
                "reason": (
                    f"order history longer than {_MAX_PAGES} pages — older "
                    "orders beyond the cap were not fetched"
                ),
            }
        )

    logger.info(
        "Epic: fetched %d orders → %d purchases, %d skipped",
        len(orders), len(records), len(skipped),
    )
    return records, skipped
