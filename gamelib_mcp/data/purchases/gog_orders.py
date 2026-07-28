"""GOG purchase-history importer.

Reads the order history behind https://www.gog.com/account/settings/orders via
the paginated ``embed.gog.com`` JSON endpoint. Auth reuses the lgogdownloader
session that ``data/gog.py`` already depends on (same config dir): a
``galaxy_tokens.json`` access token is preferred (sent as a Bearer header),
falling back to a Netscape-format ``cookies.txt`` jar for gog.com domains.
Neither present → the same "run lgogdownloader --login" advice as gog.py.

Record building:
- The order payload is community-documented, not official, so every field is
  parsed defensively. Each order carries a date (unix timestamp, int or
  string), a ``publicId``, and a ``products`` list.
- Per-product paid amounts are preferred, trying several community-observed
  price shapes (``price.amount``, ``cashValue``, ``amount``); when only an
  order total is derivable it is split evenly across the order's products
  (last share absorbs rounding, humble.py convention).
- Currency comes from an explicit currency/code key when present, else a
  symbol map ($ → USD, € → EUR, £ → GBP, zł → PLN), else USD.
- GOG orders are carts, not bundles — bundle_name stays None by design.
- Free/giveaway products (amount 0) keep purchase_source "gog" with price 0.0.
- store_identifier is the product id when present.
"""

import json
import logging
import re
from datetime import UTC, datetime

import httpx

from . import PurchaseRecord, normalize_purchase_date

logger = logging.getLogger(__name__)

PLATFORM = "gog"
PURCHASE_SOURCE = "gog"

_ORDERS_URL = "https://embed.gog.com/account/settings/orders/data"
# Hard cap on pagination — 50 pages of orders is beyond any plausible history.
_MAX_PAGES = 50

_TOKENS_FILENAME = "galaxy_tokens.json"
_COOKIE_JAR_FILENAME = "cookies.txt"

# Longest symbols first so "zł" never half-matches.
_CURRENCY_SYMBOLS = (("zł", "PLN"), ("€", "EUR"), ("£", "GBP"), ("$", "USD"))

_LOGIN_ADVICE = "run lgogdownloader --login"

_AUTH_ERROR = (
    "GOG order-history request was not authenticated (lgogdownloader session "
    f"expired) — {_LOGIN_ADVICE} again to refresh it."
)

_MONEY_RE = re.compile(r"(\d+(?:[.,]\d{1,2})?)")


def _missing_session_error() -> RuntimeError:
    # Deliberately matches data/gog.py's unconfigured phrasing.
    from gamelib_mcp.data.gog import _config_dir

    return RuntimeError(
        f"lgogdownloader session files missing in {_config_dir()}; "
        f"{_LOGIN_ADVICE}"
    )


def _load_access_token() -> str | None:
    """Pull an access_token out of lgogdownloader's galaxy_tokens.json.

    The file is either a flat token object or keyed by OAuth client id with
    token objects as values — both shapes are accepted.
    """
    from gamelib_mcp.data.gog import _config_dir

    path = _config_dir() / _TOKENS_FILENAME
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Failed to load GOG tokens from %s: %s", path, exc)
        return None

    if not isinstance(raw, dict):
        return None
    token = raw.get("access_token")
    if isinstance(token, str) and token:
        return token
    for value in raw.values():
        if isinstance(value, dict):
            token = value.get("access_token")
            if isinstance(token, str) and token:
                return token
    return None


def _load_cookie_jar() -> dict[str, str] | None:
    """Parse gog.com name/value pairs from a curl/Netscape cookies.txt jar."""
    from gamelib_mcp.data.gog import _config_dir

    path = _config_dir() / _COOKIE_JAR_FILENAME
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Failed to load GOG cookie jar from %s: %s", path, exc)
        return None

    cookies: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        # #HttpOnly_ lines are real cookies; every other #-line is a comment.
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        domain, name, value = fields[0], fields[5], fields[6]
        if "gog.com" in domain.lower() and name:
            cookies[name] = value
    return cookies or None


def _parse_money(value: object) -> float | None:
    """Best-effort float from an int/float/str money value; None on a miss."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    match = _MONEY_RE.search(value.replace("\xa0", " "))
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def _currency_from_symbol(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in text:
            return code
    return None


def _money_shape(container: dict) -> tuple[float | None, str | None]:
    """(amount, currency) from one price-ish dict — {amount, baseAmount,
    symbol, currency/code, ...} in any community-observed combination."""
    amount = None
    for key in ("amount", "baseAmount", "full", "total"):
        amount = _parse_money(container.get(key))
        if amount is not None:
            break
    currency = None
    for key in ("currency", "code"):
        value = container.get(key)
        if isinstance(value, str) and len(value) == 3 and value.isalpha():
            currency = value.upper()
            break
    if currency is None:
        currency = _currency_from_symbol(container.get("symbol"))
    return amount, currency


def _product_amount(product: dict) -> tuple[float | None, str | None]:
    """Per-product paid amount, trying several community-observed shapes."""
    for key in ("price", "cashValue", "amount"):
        value = product.get(key)
        if isinstance(value, dict):
            amount, currency = _money_shape(value)
            if amount is not None:
                return amount, currency
        else:
            amount = _parse_money(value)
            if amount is not None:
                return amount, _currency_from_symbol(value)
    return None, None


def _order_total(order: dict) -> tuple[float | None, str | None]:
    """Order-level total for the split-evenly fallback."""
    for key in ("total", "totalAmount", "cashValue", "amount"):
        value = order.get(key)
        if isinstance(value, dict):
            amount, currency = _money_shape(value)
            if amount is not None:
                return amount, currency
        else:
            amount = _parse_money(value)
            if amount is not None:
                return amount, _currency_from_symbol(value)
    return None, None


def _order_currency(order: dict, *candidates: str | None) -> str:
    """First explicit currency wins, then the order's own key, then USD."""
    for candidate in candidates:
        if candidate:
            return candidate
    value = order.get("currency")
    if isinstance(value, str) and len(value) == 3 and value.isalpha():
        return value.upper()
    symbol_currency = _currency_from_symbol(order.get("currency"))
    return symbol_currency or "USD"


def _order_date(order: dict) -> str | None:
    """YYYY-MM-DD from the order's unix timestamp (int or string), tolerating
    an ISO-ish string as a fallback."""
    value = order.get("date")
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    return normalize_purchase_date(value)


def _split_amount(amount: float, count: int) -> list[float]:
    """Even split rounded to cents; the last share absorbs the remainder."""
    if count <= 0:
        return []
    share = round(amount / count, 2)
    shares = [share] * count
    shares[-1] = round(amount - share * (count - 1), 2)
    return shares


def parse_order(order: dict) -> tuple[list[PurchaseRecord], list[dict]]:
    """Convert one order payload into (records, skipped)."""
    order_label = str(order.get("publicId") or "(unknown order)")
    products = order.get("products")
    if not isinstance(products, list) or not products:
        return [], [{"description": order_label, "reason": "order has no products"}]

    acquired_at = _order_date(order)

    entries: list[tuple[str, str | None, float | None, str | None]] = []
    skipped: list[dict] = []
    for product in products:
        if not isinstance(product, dict):
            skipped.append({"description": repr(product), "reason": "not a product object"})
            continue
        title = product.get("title")
        if not title or not isinstance(title, str):
            skipped.append({"description": order_label, "reason": "product missing title"})
            continue
        product_id = product.get("id")
        amount, currency = _product_amount(product)
        entries.append(
            (title.strip(), str(product_id) if product_id else None, amount, currency)
        )

    if not entries:
        return [], skipped

    # Split-evenly fallback: only when NO product carried its own amount but
    # an order total is derivable (mirrors humble.py's bundle split).
    if all(amount is None for _, _, amount, _ in entries):
        total, total_currency = _order_total(order)
        if total is not None:
            shares = _split_amount(total, len(entries))
            entries = [
                (title, product_id, share, currency or total_currency)
                for (title, product_id, _, currency), share in zip(
                    entries, shares, strict=True
                )
            ]

    records = [
        PurchaseRecord(
            title=title,
            platform=PLATFORM,
            purchase_source=PURCHASE_SOURCE,
            acquired_at=acquired_at,
            price_paid=amount,
            price_currency=(
                _order_currency(order, currency) if amount is not None else None
            ),
            store_identifier=product_id,
        )
        for title, product_id, amount, currency in entries
    ]
    return records, skipped


def _looks_like_login_html(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return "text/html" in content_type or response.text.lstrip()[:1] == "<"


async def _fetch_pages(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> list[dict] | None:
    """All order dicts across pages, or None on a 401 (caller may retry)."""
    orders: list[dict] = []
    total_pages = 1
    page = 1
    while page <= min(total_pages, _MAX_PAGES):
        resp = await client.get(
            _ORDERS_URL,
            params={"canceled": "0", "completed": "1", "page": page},
            headers=headers,
        )
        if resp.status_code in (401, 403):
            return None
        resp.raise_for_status()
        if _looks_like_login_html(resp):
            # An expired session bounces to an HTML login page.
            return None
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Unexpected GOG orders payload: {type(data).__name__}"
            )
        page_orders = data.get("orders")
        if isinstance(page_orders, list):
            orders.extend(o for o in page_orders if isinstance(o, dict))
        reported = data.get("totalPages")
        if isinstance(reported, int) and reported > 0:
            total_pages = reported
        page += 1
    return orders


async def fetch_gog_purchases(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[list[PurchaseRecord], list[dict]]:
    """Fetch the full GOG order history as purchase records.

    Auth: galaxy_tokens.json bearer token first; a 401 with a token retries
    once with the cookies.txt jar when one exists. Raises RuntimeError with
    lgogdownloader re-login advice on missing/stale auth; the orchestrator
    catches per source. ``transport`` exists for tests (httpx.MockTransport).
    """
    token = _load_access_token()
    cookies = _load_cookie_jar()
    if token is None and cookies is None:
        raise _missing_session_error()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        ),
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=30, transport=transport
    ) as client:
        orders = None
        if token is not None:
            orders = await _fetch_pages(
                client, {**headers, "Authorization": f"Bearer {token}"}
            )
            if orders is None and cookies is not None:
                logger.info("GOG bearer token rejected — retrying with cookie jar")
        if orders is None and cookies is not None:
            client.cookies.update(cookies)
            orders = await _fetch_pages(client, headers)
        if orders is None:
            raise RuntimeError(_AUTH_ERROR)

    records: list[PurchaseRecord] = []
    skipped: list[dict] = []
    for order in orders:
        order_records, order_skipped = parse_order(order)
        records.extend(order_records)
        skipped.extend(order_skipped)

    logger.info(
        "GOG: fetched %d orders → %d purchases, %d skipped",
        len(orders), len(records), len(skipped),
    )
    return records, skipped
