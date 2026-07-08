"""Nintendo eShop purchase-history importer.

Reads the transaction history behind https://ec.nintendo.com/my/transactions/
via its paginated JSON API. Auth is browser session cookies for
ec.nintendo.com, stored as a ``{name: value}`` JSON file (the same shape and
loading pattern as ``data/nintendo.py``'s VGCS cookies) — set them with the
``set_nintendo_ec_session`` MCP tool.

Import policy:
- transaction_type: only purchase-like rows ("purchase"; a missing type is
  tolerated as a purchase) are imported; refunds/redownloads land in skipped.
- content_type: "title"/"bundle"/"aoc" (DLC) are imported (a missing
  content_type is tolerated); consumables/subscription items land in skipped.
- amount may be an object {currency, raw_value, formatted_value} or missing.
  A missing amount on an imported purchase means a free download → price 0.0
  (purchase_source stays "eshop"); raw_value 0/"0" likewise. An amount object
  whose raw_value doesn't parse yields price None (record kept, unpriced)
  rather than guessing.
- A transaction missing title or date is skipped with a reason, never dropped
  silently.
"""

import json
import logging
import os

import httpx

from . import PurchaseRecord, normalize_purchase_date

logger = logging.getLogger(__name__)

PLATFORM = "switch2"
PURCHASE_SOURCE = "eshop"

_TRANSACTIONS_URL = "https://ec.nintendo.com/api/my/transactions"
_PAGE_LIMIT = 50
# Hard cap: 40 pages × 50 = 2000 transactions, beyond any plausible history.
_MAX_PAGES = 40

_IMPORTABLE_CONTENT_TYPES = frozenset({"title", "bundle", "aoc"})

_AUTH_ERROR = (
    "Nintendo eShop transaction fetch was not authenticated (session cookies "
    "missing or expired) — re-run set_nintendo_ec_session with fresh cookies "
    "from ec.nintendo.com."
)


def _load_ec_cookies() -> dict[str, str] | None:
    """Load eShop session cookies from NINTENDO_EC_COOKIES_FILE.

    Mirrors data/nintendo.py::_load_vgcs_cookies: configured path first, then
    the default path; accepts both {name: value} and Cookie Editor array JSON.
    """
    configured_path = os.getenv("NINTENDO_EC_COOKIES_FILE", "data/nintendo_ec_cookies.json")
    candidate_paths = [configured_path]
    fallback_path = "data/nintendo_ec_cookies.json"
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
            logger.warning("Failed to load Nintendo eShop cookies from %s: %s", path, exc)
            return None

    if raw is None:
        return None

    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw if isinstance(c, dict) and "name" in c and "value" in c}
    if isinstance(raw, dict):
        return raw
    return None


def _skip(skipped: list[dict], transaction: dict, reason: str) -> None:
    skipped.append({
        "title": transaction.get("title") or "(unknown title)",
        "reason": reason,
    })


def _parse_amount(transaction: dict) -> tuple[float | None, str | None]:
    """(price_paid, price_currency) from a transaction's optional amount.

    Missing amount → (0.0, None): the row already passed the purchase-like
    gate, and Nintendo omits the amount block on free downloads.
    """
    amount = transaction.get("amount")
    if amount is None:
        return 0.0, None
    if not isinstance(amount, dict):
        return None, None
    currency = amount.get("currency")
    currency = str(currency) if currency else None
    raw_value = amount.get("raw_value")
    if raw_value is None or isinstance(raw_value, (dict, list, bool)):
        return None, currency
    try:
        price = float(raw_value)
    except (TypeError, ValueError):
        return None, currency
    if price < 0:
        return None, currency
    return price, currency


def parse_transactions(
    transactions: list,
) -> tuple[list[PurchaseRecord], list[dict]]:
    """Split raw transaction dicts into (importable records, skipped)."""
    records: list[PurchaseRecord] = []
    skipped: list[dict] = []
    for transaction in transactions:
        if not isinstance(transaction, dict):
            skipped.append({"description": repr(transaction), "reason": "not a transaction object"})
            continue

        transaction_type = str(transaction.get("transaction_type") or "purchase").lower()
        if transaction_type != "purchase":
            _skip(skipped, transaction, f"transaction_type '{transaction_type}' is not a purchase")
            continue

        content_type = str(transaction.get("content_type") or "title").lower()
        if content_type not in _IMPORTABLE_CONTENT_TYPES:
            _skip(skipped, transaction, f"content_type '{content_type}' is not importable")
            continue

        title = transaction.get("title")
        if not title or not isinstance(title, str):
            _skip(skipped, transaction, "missing title")
            continue

        acquired_at = normalize_purchase_date(transaction.get("date"))
        if acquired_at is None:
            _skip(skipped, transaction, "missing or unparseable date")
            continue

        price, currency = _parse_amount(transaction)
        title_id = transaction.get("title_id")
        records.append(
            PurchaseRecord(
                title=title.strip(),
                platform=PLATFORM,
                purchase_source=PURCHASE_SOURCE,
                acquired_at=acquired_at,
                price_paid=price,
                price_currency=currency,
                store_identifier=str(title_id) if title_id else None,
            )
        )
    return records, skipped


def _looks_like_login_html(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return "text/html" in content_type or response.text.lstrip()[:1] == "<"


async def fetch_eshop_purchases(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[list[PurchaseRecord], list[dict]]:
    """Fetch the full eShop transaction history as purchase records.

    Paginates ?limit=50&offset=N until a page returns fewer than the limit
    (or the reported total is reached), hard-capped at _MAX_PAGES. Raises
    RuntimeError on missing/stale auth; the orchestrator catches per source.
    ``transport`` exists for tests (httpx.MockTransport) — production callers
    pass nothing.
    """
    cookies = _load_ec_cookies()
    if not cookies:
        raise RuntimeError(
            "No Nintendo eShop session cookies found (NINTENDO_EC_COOKIES_FILE "
            "not set or missing) — run set_nintendo_ec_session first."
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        ),
        "Accept": "application/json",
    }

    raw_transactions: list = []
    async with httpx.AsyncClient(
        cookies=cookies, follow_redirects=True, timeout=30, transport=transport
    ) as client:
        offset = 0
        total: int | None = None
        for _page in range(_MAX_PAGES):
            resp = await client.get(
                _TRANSACTIONS_URL,
                params={"limit": _PAGE_LIMIT, "offset": offset},
                headers=headers,
            )
            if resp.status_code in (401, 403):
                raise RuntimeError(_AUTH_ERROR)
            resp.raise_for_status()
            if _looks_like_login_html(resp):
                # An expired session bounces to an HTML login page.
                raise RuntimeError(_AUTH_ERROR)

            data = resp.json()
            if isinstance(data, dict):
                page_transactions = data.get("transactions") or []
                page_total = data.get("total")
                if isinstance(page_total, int):
                    total = page_total
            elif isinstance(data, list):
                page_transactions = data
            else:
                raise RuntimeError(
                    f"Unexpected eShop transactions payload: {type(data).__name__}"
                )

            raw_transactions.extend(page_transactions)
            offset += len(page_transactions)
            if len(page_transactions) < _PAGE_LIMIT:
                break
            if total is not None and offset >= total:
                break

    records, skipped = parse_transactions(raw_transactions)
    logger.info(
        "Nintendo eShop: fetched %d transactions → %d purchases, %d skipped",
        len(raw_transactions), len(records), len(skipped),
    )
    return records, skipped
