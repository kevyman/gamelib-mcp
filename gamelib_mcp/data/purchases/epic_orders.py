"""Epic Games Store purchase-history importer.

Reads the order history behind https://www.epicgames.com/account/transactions
via the paginated ``ajaxGetOrderHistory`` JSON endpoint. This is the account
WEBSITE, not the launcher API: Legendary's launcher session (``data/epic.py``)
cannot see order history or prices, so auth here is browser cookies exported
from a signed-in epicgames.com tab — stored in the same ``{name: value}`` JSON
file shape as the Humble/Nintendo cookie files and set with
``create_session_ingest_link(provider="epic")``.

Transport: www.epicgames.com sits behind Cloudflare bot management, which
challenges on a combined IP-reputation + TLS-fingerprint score. From a
datacenter IP (production is a Hetzner VPS) every plain-httpx request — and
even curl_cffi's Chrome profiles — draws ``cf-mitigated: challenge``; the
Firefox/Safari impersonation profiles pass (verified against prod, 2026-07-20).
So the real network path uses curl_cffi impersonating Firefox, with a warm-up
GET of the transactions page (Epic's ajax endpoint answers 401 ``needLogin``
for a valid session until the web session has been touched). The
``transport=httpx.MockTransport`` test seam keeps exercising the shared
pagination/auth logic over plain httpx. If Cloudflare starts challenging the
impersonated profile too, the remaining fix is residential egress —
``_CHALLENGE_ERROR`` says so instead of blaming cookies.

Record building:
- The payload is community-documented, not official, so every field is parsed
  defensively. Each order carries ``createdAtMillis`` (unix milliseconds),
  ``orderType`` (and sometimes ``orderStatus``), and an ``items`` list of
  ``{description, amount, currency, offerId, quantity}``.
- Money comes in two shapes. The LIVE v2 payload (observed 2026-07-20) uses
  integer ISO-4217 minor units with a sibling ISO code — item ``amount: 719``
  + ``currency: "EUR"`` is €7.19, and order-level ``total``/``subtotal``/
  ``tax`` are ``{amount, currency}`` objects. The community-documented legacy
  shape is a locale-FORMATTED string ("$19.99", "R$ 29,99") — ``locale=en-US``
  is requested for predictable formatting, and the parser handles decimal-point
  and decimal-comma shapes plus a symbol → ISO-code map. A bare number WITHOUT
  a sibling ISO code is taken at face value (decimal units).
- Item ``amount`` is the LIST price, not the price paid: giveaway claims carry
  the full list price with a 100% ``promotions`` discount and a zero ``total``,
  and coupon orders likewise land below list. Importing list prices would mint
  phantom spend (a claimed-free order showing as €56.76), so whenever every
  item priced itself but the order ``total`` disagrees with their sum, the
  total is re-allocated across items proportionally to list price
  (cent-preserving; even split when all list prices are zero). Known limit:
  items skipped as consumables don't join the allocation, slightly overstating
  the games' share in mixed orders.
- Currency: the order's explicit ISO field ("currency"/"presentmentCurrency",
  or the ``total``/``subtotal`` object's code) outranks any symbol inferred
  from the amount string — "$" is ambiguous (USD/CAD/AUD/NZD all format with
  it); multi-character dollar symbols (CA$, A$, NZ$, …) are mapped as
  fallbacks when no ISO field exists.
- Orders whose ``orderStatus`` is present but not COMPLETED are skipped with
  the status in the reason (visible drift, not silent drops); ``orderType``
  REFUND is skipped likewise — importing a refund would double-count spend.
- In-game currency packs (V-Bucks, "Credits x1100", "2,800 Apex Coins" …)
  are detected by NAME — the payload has no item typing — and routed to
  skipped: fed to the matcher, a paid pack would mint a phantom owned game
  under create_missing.
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
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
from curl_cffi.requests import AsyncSession, BrowserTypeLiteral

from . import PurchaseRecord, normalize_purchase_date
from gamelib_mcp.data.content import classify_title_override, match_addon_name
from gamelib_mcp.data.db import default_data_dir

logger = logging.getLogger(__name__)

PLATFORM = "epic"
PURCHASE_SOURCE = "epic"

_ORDER_HISTORY_URL = "https://www.epicgames.com/account/v2/payment/ajaxGetOrderHistory"
_TRANSACTIONS_URL = "https://www.epicgames.com/account/transactions"
# ~10 orders per page; weekly-freebie accounts accumulate long histories, so
# the cap is generous — hitting it is reported in skipped, never silent.
_MAX_PAGES = 200
# Politeness delay between sequential page requests (humble.py convention).
_REQUEST_DELAY_SECONDS = 0.2

# curl_cffi impersonation profile for the real network path. Deliberately
# Firefox: Cloudflare detects the Chrome profiles (chrome/chrome124/chrome136/
# chrome131_android all drew challenges from the production IP on 2026-07-20)
# but passes firefox135 and safari184. Revisit on curl_cffi upgrades.
_IMPERSONATE_PROFILE: BrowserTypeLiteral = "firefox135"

# XHR-shaped headers: without Accept: application/json the endpoint serves the
# transactions page shell instead of JSON. curl_cffi's impersonation supplies
# the browser User-Agent; the httpx test path adds its own.
_AJAX_HEADERS = {
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": _TRANSACTIONS_URL,
}

# Longest symbols first so "CA$"/"R$" never half-match "A$"/"$". Bare "$" is
# ambiguous (USD/CAD/AUD/NZD all format with it) — the order's explicit ISO
# currency field outranks every symbol guess in _order_currency.
_CURRENCY_SYMBOLS = (
    ("CA$", "CAD"),
    ("NZ$", "NZD"),
    ("HK$", "HKD"),
    ("MX$", "MXN"),
    ("A$", "AUD"),
    ("R$", "BRL"),
    ("zł", "PLN"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("¥", "JPY"),
    ("$", "USD"),
)

# In-game currency packs sold through Epic checkout ("1,000 V-Bucks",
# "Rocket League® - Credits x1100", "2,800 Apex Coins", "EA SPORTS FC 24 -
# 1050 FC Points"). The order payload carries no item typing, so the NAME is
# the only signal (steam_history.py's wallet-credit precedent): a count
# attached to a currency noun — either order — or the unambiguous
# V-Bucks/Show-Bucks brands. A bare noun with no number never trips it, so
# games legitimately titled "…Coins"/"…Points" don't get filtered.
_CURRENCY_NOUN = r"(?:v-?bucks|show-?bucks|credits?|coins?|points|gems|gold\s+bars)"
# Up to two words may sit between count and noun ("2,800 Apex Coins",
# "1050 FC Points", "1000 Rocket League Credits").
_CONSUMABLE_NAME_RE = re.compile(
    rf"\bv-?bucks\b|\bshow-?bucks\b"
    rf"|\b\d[\d,.]*\+?\s*(?:[\w'&.®™-]+\s+){{0,2}}{_CURRENCY_NOUN}\b"
    rf"|\b{_CURRENCY_NOUN}\s*x\s*\d",
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(r"\d[\d.,\s ]*")
_ISO_CODE_RE = re.compile(r"\b([A-Z]{3})\b")

# ISO 4217 currencies with no minor unit: an integer amount in these IS the
# face value (¥1980 stays 1980), everything else divides by 100 (719 → 7.19).
_ZERO_DECIMAL_CURRENCIES = frozenset(
    {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW",
     "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
)

_AUTH_ERROR = (
    "Epic Games order-history request was not authenticated (epicgames.com "
    "session cookies missing or expired) — run "
    "create_session_ingest_link(provider=\"epic\") and open the link to paste "
    "fresh cookies from www.epicgames.com."
)

# Cloudflare fronts the Epic account portal and answers bot-scored requests with
# a JS challenge — a 403 that never reaches Epic's auth layer, so it says
# nothing about cookie freshness. Re-pasting cookies cannot clear it. The
# importer already impersonates a browser TLS fingerprint, so seeing this means
# Cloudflare's scoring has tightened past the current profile (or the server's
# IP reputation degraded) — the durable fix is a different impersonation
# profile or residential egress, not new cookies.
_CHALLENGE_ERROR = (
    "Epic Games order-history request was blocked by Cloudflare bot protection "
    "(cf-mitigated: challenge), not by authentication — the session cookies "
    "were never checked, so re-pasting them will not help. The importer already "
    f"impersonates a browser ({_IMPERSONATE_PROFILE}); Cloudflare has evidently "
    "started challenging that profile from this server's IP. Fixes: try a newer "
    "curl_cffi impersonation profile, or route this request out through a "
    "residential IP (e.g. a WireGuard/Tailscale exit node at home)."
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


def _iso_currency(value: object) -> str | None:
    return (
        value.upper()
        if isinstance(value, str) and len(value) == 3 and value.isalpha()
        else None
    )


def _parse_money(value: object) -> tuple[float | None, str | None]:
    """(amount, currency) from any of the payload's money shapes.

    A ``{amount, currency}`` object (the live v2 payload: order ``total``/
    ``subtotal``, or reassembled from an item's sibling fields) holds integer
    ISO-4217 minor units — 719 EUR-cents → 7.19 — except zero-decimal
    currencies, whose integers are face value. A non-integral float in that
    shape is already decimal units.

    A string is the legacy locale-formatted shape, handling decimal-point and
    decimal-comma locales ("$19.99", "R$ 29,99", "1.234,56 zł", "1,234.56"):
    with both separators present the rightmost is the decimal one; with a
    single separator it is decimal only when followed by 1–2 digits (else
    thousands). Currency comes from the symbol map or an uppercase ISO code in
    the string; None when absent.

    A bare number without a currency hint is taken at face value.
    """
    if isinstance(value, dict):
        raw = value.get("amount")
        currency = _iso_currency(value.get("currency"))
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            return None, currency
        if isinstance(raw, int):
            divisor = 1 if currency in _ZERO_DECIMAL_CURRENCIES else 100
            return round(raw / divisor, 2), currency
        return float(raw), currency
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
    """The order's explicit ISO currency wins — symbol-inferred candidates
    are ambiguous ("$" formats USD, CAD, AUD, …) — then parsed candidates,
    then USD."""
    for key in ("currency", "presentmentCurrency"):
        currency = _iso_currency(order.get(key))
        if currency:
            return currency
    for key in ("total", "subtotal"):
        value = order.get(key)
        if isinstance(value, dict):
            currency = _iso_currency(value.get("currency"))
            if currency:
                return currency
    for candidate in candidates:
        if candidate:
            return candidate
    return "USD"


def _allocate_total(total: float, list_prices: list[float]) -> list[float]:
    """Cent-preserving split of the paid total proportional to list prices
    (even split when all list prices are zero); last share absorbs rounding."""
    if not list_prices:
        return []
    weight_sum = sum(list_prices)
    if weight_sum <= 0:
        return _split_amount(total, len(list_prices))
    shares = [round(total * w / weight_sum, 2) for w in list_prices[:-1]]
    shares.append(round(total - sum(shares), 2))
    return shares


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
        if _CONSUMABLE_NAME_RE.search(title):
            # A paid currency pack fed to the matcher would mint a phantom
            # owned game under create_missing — route it to skipped instead.
            skipped.append(
                {
                    "description": title.strip(),
                    "reason": "in-game currency/consumable, not a game",
                }
            )
            continue
        offer_id = item.get("offerId")
        amount_value = item.get("amount")
        item_currency = _iso_currency(item.get("currency"))
        if (
            item_currency
            and isinstance(amount_value, (int, float))
            and not isinstance(amount_value, bool)
        ):
            # Live v2 shape: bare integer minor units with the ISO code in a
            # sibling field — reassemble so _parse_money sees them together
            # (719 + "EUR" → 7.19, not 719.0).
            amount, currency = _parse_money(
                {"amount": amount_value, "currency": item_currency}
            )
        else:
            amount, currency = _parse_money(amount_value)
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
    elif all(amount is not None for _, _, amount, _ in entries):
        # Item amounts are LIST prices; the order total is what was paid.
        # Giveaway claims list every item at full price with a 100%
        # ``promotions`` discount and total 0 — importing list prices would
        # record spend that never happened — and coupon orders land between.
        # When the total disagrees with the list sum, re-allocate it across
        # items proportionally to list price.
        total, total_currency = _order_total(order)
        listed = sum(amount for _, _, amount, _ in entries if amount is not None)
        if total is not None and abs(listed - total) > 0.005:
            shares = _allocate_total(total, [amount or 0.0 for _, _, amount, _ in entries])
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


class _OrderHistoryResponse(Protocol):
    """Structural intersection of httpx.Response and curl_cffi's Response —
    the shared pagination/auth logic runs over either client."""

    @property
    def status_code(self) -> int: ...
    @property
    def text(self) -> str: ...
    @property
    def headers(self) -> Mapping[str, str]: ...
    def json(self) -> Any: ...
    def raise_for_status(self) -> object: ...


def _check_auth(response: _OrderHistoryResponse) -> None:
    if response.status_code in (401, 403):
        # Cloudflare marks its own mitigations; fall back to sniffing the
        # challenge-platform script it injects, since the header is not
        # guaranteed on every challenge variant.
        mitigated = response.headers.get("cf-mitigated", "").lower()
        if mitigated == "challenge" or "challenge-platform" in response.text:
            raise RuntimeError(_CHALLENGE_ERROR)
        raise RuntimeError(_AUTH_ERROR)
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type or response.text.lstrip()[:1] == "<":
        # An unauthenticated ajax call bounces to the HTML login page.
        raise RuntimeError(_AUTH_ERROR)


async def _paginate_orders(
    get_page: Callable[[dict[str, str]], Awaitable[_OrderHistoryResponse]],
) -> tuple[list[dict], bool]:
    """Walk ajaxGetOrderHistory via nextPageToken; (orders, hit_page_cap)."""
    orders: list[dict] = []
    next_page_token = ""
    for page in range(_MAX_PAGES):
        if page:
            await asyncio.sleep(_REQUEST_DELAY_SECONDS)
        params = {"sortDir": "DESC", "sortBy": "DATE", "locale": "en-US"}
        if next_page_token:
            params["nextPageToken"] = next_page_token
        resp = await get_page(params)
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
            return orders, False
    return orders, True


async def _fetch_orders_impersonated(
    cookies: dict[str, str],
) -> tuple[list[dict], bool]:
    """Real network path: curl_cffi with a browser TLS fingerprint (see module
    docstring — plain httpx draws a Cloudflare challenge from datacenter IPs)."""
    async with AsyncSession(impersonate=_IMPERSONATE_PROFILE, timeout=30) as session:
        for name, value in cookies.items():
            session.cookies.set(name, value, domain=".epicgames.com")
        # Warm-up: walk the transactions page like a browser first. Epic's
        # ajax endpoint answers 401 needLogin for a valid session until the
        # web session has been touched (observed 2026-07-20); the page visit
        # revives it server-side. Failures here are non-fatal — the ajax
        # call's own _check_auth produces the actionable error.
        try:
            await session.get(_TRANSACTIONS_URL, allow_redirects=True)
        except Exception as exc:
            logger.debug("Epic transactions warm-up request failed: %s", exc)
        return await _paginate_orders(
            lambda params: session.get(
                _ORDER_HISTORY_URL, params=params, headers=_AJAX_HEADERS
            )
        )


async def _fetch_orders_httpx(
    cookies: dict[str, str], transport: httpx.AsyncBaseTransport
) -> tuple[list[dict], bool]:
    """Test path: plain httpx over the caller's MockTransport, exercising the
    same pagination/auth logic as the impersonated path."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        ),
        **_AJAX_HEADERS,
    }
    async with httpx.AsyncClient(
        cookies=cookies, follow_redirects=True, timeout=30, transport=transport
    ) as client:
        return await _paginate_orders(
            lambda params: client.get(
                _ORDER_HISTORY_URL, params=params, headers=headers
            )
        )


async def fetch_epic_purchases(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[list[PurchaseRecord], list[dict]]:
    """Fetch the full Epic order history as purchase records.

    Raises RuntimeError on missing/stale auth; the orchestrator catches per
    source. ``transport`` exists for tests (httpx.MockTransport); without it
    the fetch impersonates a browser via curl_cffi.
    """
    cookies = _load_epic_cookies()
    if not cookies:
        raise RuntimeError(
            "No Epic Games session cookies found (EPIC_COOKIES_FILE not set or "
            "missing) — run create_session_ingest_link(provider=\"epic\") first."
        )

    if transport is not None:
        orders, pagination_capped = await _fetch_orders_httpx(cookies, transport)
    else:
        orders, pagination_capped = await _fetch_orders_impersonated(cookies)

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
