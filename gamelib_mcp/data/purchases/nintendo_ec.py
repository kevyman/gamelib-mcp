"""Nintendo eShop purchase-history importer.

Reads the transaction history behind https://ec.nintendo.com/my/transactions/
via the Savanna GraphQL API that page calls. Two steps per sync:

1. ``GET https://ec.nintendo.com/api/auth/session`` with the browser session
   cookie ``__Secure-next-auth.session-token`` returns a short-lived Nintendo
   Account ``idToken`` (~15 min) plus the account's ``country``/``language``.
2. ``GET https://wb.lp1.savanna.srv.nintendo.net/graphql`` with that ``idToken``
   in the query string (auth is the token, NOT cookies) returns paginated
   transactions. The query is a *persisted query* — the server only accepts the
   SHA-256 hash of the exact query Nintendo's web build ships; arbitrary GraphQL
   and introspection are rejected. If Nintendo redeploys and the hash/client-id
   drift, ``NINTENDO_EC_QUERY_HASH`` / ``NINTENDO_EC_CLIENT_ID`` override the
   pinned defaults without a code change.

Set the session cookie with the ``set_nintendo_ec_session`` MCP tool (export
your ec.nintendo.com cookies while logged in on the transactions page — the
export includes ``__Secure-next-auth.session-token``).

Response schema (``data.account.transactionHistories.transactionHistories[]``):
- ``TransactionHistory``: ``transactionType`` (PURCHASE/REDEEM), ``itemType``
  (APPLICATION/BUNDLE/DLC/…), ``title``, ``datetime`` (ISO+offset), ``amount``
  (``{formattedValue}`` like ``"€ 12,49"`` — a localized string, the only price
  info exposed; or ``null``), ``labelPlatform`` (HAC=Switch, BEE=Switch 2).
- ``ExternalEcTransactionHistory``: an account-merge/redemption grant with no
  price, type, or top-level title — carries ``grantedItems[]`` instead.

Import policy:
- Only ``transactionType == PURCHASE`` rows become records. ``REDEEM`` (code/
  voucher redemptions, ``amount: null``) is not a purchase → skipped.
- ``itemType`` in {APPLICATION, BUNDLE, DLC} imports (a missing itemType is
  tolerated); consumables/subscriptions/tickets land in skipped.
- ``ExternalEcTransactionHistory`` grants are not purchases and carry no price;
  each granted item is reported individually in ``skipped`` (never silently
  dropped) so the count is honest.
- ``amount.formattedValue`` is parsed to (price, currency). A missing amount on
  an imported purchase yields price None (record kept, unpriced) rather than
  guessing a value. An unparseable amount likewise keeps the record unpriced.
- A transaction missing title or date is skipped with a reason, never dropped
  silently. All Switch/Switch 2 purchases land on the ``switch2`` platform,
  matching how the rest of the codebase treats both.
"""

import json
import logging
import os
import re

import httpx

from . import PurchaseRecord, normalize_purchase_date
from gamelib_mcp.data.db import default_data_dir

logger = logging.getLogger(__name__)

PLATFORM = "switch2"
PURCHASE_SOURCE = "eshop"

_SESSION_URL = "https://ec.nintendo.com/api/auth/session"
_GRAPHQL_URL = "https://wb.lp1.savanna.srv.nintendo.net/graphql"
_SESSION_COOKIE = "__Secure-next-auth.session-token"

# Pinned to Nintendo's current eShop web build. If Nintendo redeploys and these
# drift, the GraphQL call 400s (INVALID_PARAM) — override via env without a
# code change. Introspection is blocked, so there is no way to self-heal these.
_OPERATION_NAME = "TransactionsClientRootClient"
_DEFAULT_QUERY_HASH = "aa2c23b02481e2c5caba64f6a52e81b2ddcbc311c299ee111dd28c41f245e1e6"
_DEFAULT_CLIENT_ID = "042e4bd1f0eec144167dbc0c63f1d17876e7b9ec322713b613614214e3675df9"
# shopId 3 = Europe; regional. Single-user app, override if the account is
# registered to another region's shop.
_DEFAULT_SHOP_ID = 3

_PAGE_LIMIT = 50
# Hard cap: 40 pages × 50 = 2000 transactions, beyond any plausible history.
_MAX_PAGES = 40

_IMPORTABLE_ITEM_TYPES = frozenset({"application", "bundle", "dlc"})

# Currency-symbol → ISO 4217, for parsing amount.formattedValue (which carries
# no explicit currency code). Longest prefixes first so "R$"/"US$" win over "$".
_CURRENCY_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("R$", "BRL"),
    ("US$", "USD"),
    ("CA$", "CAD"),
    ("A$", "AUD"),
    ("NZ$", "NZD"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("¥", "JPY"),
    ("₩", "KRW"),
    ("₽", "RUB"),
    ("$", "USD"),
)
_ISO_CODE_RE = re.compile(r"\b([A-Z]{3})\b")

_AUTH_ERROR = (
    "Nintendo eShop session is missing or expired — re-run set_nintendo_ec_session "
    "with a fresh cookie export from ec.nintendo.com (logged in, on the "
    "transactions page)."
)


def _has_session_cookie(cookies: dict[str, str]) -> bool:
    """True when the export carries the NextAuth session cookie.

    NextAuth splits a large session cookie into numbered chunks
    (``__Secure-next-auth.session-token.0``, ``.1``, …); browsers send every
    chunk and ``/api/auth/session`` reassembles them server-side. Accept either
    the unsuffixed cookie or any chunk so chunked exports aren't rejected.
    """
    if _SESSION_COOKIE in cookies:
        return True
    prefix = _SESSION_COOKIE + "."
    return any(
        name.startswith(prefix) and name[len(prefix):].isdigit() for name in cookies
    )


def _load_ec_cookies() -> dict[str, str] | None:
    """Load eShop session cookies from NINTENDO_EC_COOKIES_FILE.

    Mirrors data/nintendo.py::_load_vgcs_cookies: configured path first, then
    the default path; accepts both {name: value} and Cookie Editor array JSON.
    """
    fallback_path = str(default_data_dir() / "nintendo_ec_cookies.json")
    configured_path = os.getenv("NINTENDO_EC_COOKIES_FILE") or fallback_path
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
            logger.warning("Failed to load Nintendo eShop cookies from %s: %s", path, exc)
            return None

    if raw is None:
        return None

    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw if isinstance(c, dict) and "name" in c and "value" in c}
    if isinstance(raw, dict):
        return raw
    return None


def _skip(skipped: list[dict], title: object, reason: str) -> None:
    skipped.append({
        "title": title if isinstance(title, str) and title else "(unknown title)",
        "reason": reason,
    })


def _detect_currency(formatted: str) -> str | None:
    match = _ISO_CODE_RE.search(formatted)
    if match:
        return match.group(1)
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in formatted:
            return code
    return None


def _parse_number(digits: str) -> float | None:
    """Parse a localized number string, inferring the decimal separator.

    Handles European ``1.234,56`` and US ``1,234.56`` grouping, plain integers,
    and grouped integers like ``1,480`` (¥) → 1480.
    """
    negative = digits.startswith("-")
    digits = digits.lstrip("-")
    if not digits:
        return None
    has_comma = "," in digits
    has_dot = "." in digits
    if has_comma and has_dot:
        decimal = "," if digits.rfind(",") > digits.rfind(".") else "."
        grouping = "." if decimal == "," else ","
        digits = digits.replace(grouping, "").replace(decimal, ".")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        parts = digits.split(sep)
        # A single separator with a 1–2 digit tail is a decimal point; anything
        # else (3-digit tail, or multiple separators) is thousands grouping.
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            digits = digits.replace(sep, ".")
        else:
            digits = digits.replace(sep, "")
    try:
        value = float(digits)
    except ValueError:
        return None
    return -value if negative else value


def _parse_amount(amount: object) -> tuple[float | None, str | None]:
    """(price_paid, price_currency) from a transaction's optional amount.

    ``amount`` is ``{"formattedValue": "€ 12,49", ...}`` or None. The API
    exposes only the localized formatted string — no numeric field and no
    currency code — so both are parsed out of it. Missing/unparseable → None
    (record kept, unpriced) rather than guessing.
    """
    if not isinstance(amount, dict):
        return None, None
    formatted = amount.get("formattedValue")
    if not isinstance(formatted, str) or not formatted.strip():
        return None, None
    formatted = formatted.replace("\xa0", " ").strip()
    currency = _detect_currency(formatted)
    stripped = re.sub(r"[^0-9,.\-]", "", formatted)
    if not stripped:
        return None, currency
    return _parse_number(stripped), currency


def parse_transactions(
    transactions: list,
) -> tuple[list[PurchaseRecord], list[dict]]:
    """Split raw transaction dicts into (importable purchases, skipped)."""
    records: list[PurchaseRecord] = []
    skipped: list[dict] = []
    for transaction in transactions:
        if not isinstance(transaction, dict):
            skipped.append({"description": repr(transaction), "reason": "not a transaction object"})
            continue

        typename = transaction.get("__typename")
        if typename == "ExternalEcTransactionHistory":
            # Account-merge / external redemption grant: not a purchase and
            # priceless. Report each granted item so a multi-game grant is
            # visible in the skip report, never silently dropped.
            granted = transaction.get("grantedItems")
            if isinstance(granted, list) and granted:
                for item in granted:
                    title = item.get("title") if isinstance(item, dict) else None
                    _skip(skipped, title, "external eShop grant (not a purchase)")
            else:
                _skip(skipped, None, "external eShop grant with no items")
            continue

        transaction_type = str(transaction.get("transactionType") or "").upper()
        if transaction_type != "PURCHASE":
            _skip(
                skipped,
                transaction.get("title"),
                f"transaction_type '{transaction_type or 'unknown'}' is not a purchase",
            )
            continue

        item_type = str(transaction.get("itemType") or "application").lower()
        if item_type not in _IMPORTABLE_ITEM_TYPES:
            _skip(skipped, transaction.get("title"), f"item_type '{item_type}' is not importable")
            continue

        title = transaction.get("title")
        if not title or not isinstance(title, str):
            _skip(skipped, title, "missing title")
            continue

        acquired_at = normalize_purchase_date(transaction.get("datetime"))
        if acquired_at is None:
            _skip(skipped, title, "missing or unparseable date")
            continue

        price, currency = _parse_amount(transaction.get("amount"))
        records.append(
            PurchaseRecord(
                title=title.strip(),
                platform=PLATFORM,
                purchase_source=PURCHASE_SOURCE,
                acquired_at=acquired_at,
                price_paid=price,
                price_currency=currency,
                # A BUNDLE's title is a multi-game bundle name, not a single
                # game — flagged so import_purchases routes it to
                # bundles_needing_split rather than the single-game matcher.
                is_bundle=item_type == "bundle",
            )
        )
    return records, skipped


def _looks_like_login_html(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return "text/html" in content_type or response.text.lstrip()[:1] == "<"


async def _fetch_id_token(client: httpx.AsyncClient) -> tuple[str, str, str]:
    """Exchange the session cookie for a fresh (idToken, country, language).

    Raises RuntimeError on missing/expired session.
    """
    resp = await client.get(_SESSION_URL, headers={"Accept": "application/json"})
    if resp.status_code in (401, 403):
        raise RuntimeError(_AUTH_ERROR)
    resp.raise_for_status()
    if _looks_like_login_html(resp):
        raise RuntimeError(_AUTH_ERROR)
    data = resp.json()
    id_token = data.get("idToken") if isinstance(data, dict) else None
    if not id_token or not isinstance(id_token, str):
        # An expired session returns {} / no idToken with a 200.
        raise RuntimeError(_AUTH_ERROR)
    country = str(data.get("country") or "US")
    locale = data.get("localeInfo")
    language = str((locale or {}).get("language") or "en") if isinstance(locale, dict) else "en"
    return id_token, country, language


async def fetch_eshop_purchases(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[list[PurchaseRecord], list[dict]]:
    """Fetch the full eShop transaction history as purchase records.

    Resolves a session ``idToken`` then paginates the Savanna GraphQL API
    (?limit=50&offset=N) until a page returns fewer than the limit (or the
    reported total is reached), hard-capped at _MAX_PAGES. Raises RuntimeError
    on missing/stale auth; the orchestrator catches per source. ``transport``
    exists for tests (httpx.MockTransport) — production callers pass nothing.
    """
    cookies = _load_ec_cookies()
    if not cookies:
        raise RuntimeError(
            "No Nintendo eShop session cookies found (NINTENDO_EC_COOKIES_FILE "
            "not set or missing) — run set_nintendo_ec_session first."
        )
    if not _has_session_cookie(cookies):
        raise RuntimeError(
            f"Nintendo eShop cookie export is missing '{_SESSION_COOKIE}' — export "
            "your ec.nintendo.com cookies while logged in, then re-run "
            "set_nintendo_ec_session."
        )

    query_hash = os.getenv("NINTENDO_EC_QUERY_HASH") or _DEFAULT_QUERY_HASH
    client_id = os.getenv("NINTENDO_EC_CLIENT_ID") or _DEFAULT_CLIENT_ID
    try:
        shop_id = int(os.getenv("NINTENDO_EC_SHOP_ID") or _DEFAULT_SHOP_ID)
    except ValueError:
        shop_id = _DEFAULT_SHOP_ID

    user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
    )
    extensions = json.dumps(
        {"persistedQuery": {"sha256Hash": query_hash, "version": 1}},
        separators=(",", ":"),
    )

    raw_transactions: list = []
    async with httpx.AsyncClient(
        cookies=cookies, follow_redirects=True, timeout=30, transport=transport
    ) as client:
        id_token, country, language = await _fetch_id_token(client)

        headers = {
            "User-Agent": user_agent,
            "Accept": "application/graphql-response+json, application/json",
            "content-type": "application/json",
            "x-nintendo-savanna-client-id": client_id,
            "Origin": "https://ec.nintendo.com",
            "Referer": "https://ec.nintendo.com/",
        }

        offset = 0
        total: int | None = None
        for _page in range(_MAX_PAGES):
            variables = json.dumps(
                {
                    "country": country,
                    "idToken": id_token,
                    "language": language,
                    "limit": _PAGE_LIMIT,
                    "offset": offset,
                    "shopId": shop_id,
                },
                separators=(",", ":"),
            )
            resp = await client.get(
                _GRAPHQL_URL,
                params={
                    "operationName": _OPERATION_NAME,
                    "variables": variables,
                    "extensions": extensions,
                },
                headers=headers,
            )
            if resp.status_code in (401, 403):
                raise RuntimeError(_AUTH_ERROR)
            resp.raise_for_status()

            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError(
                    f"Unexpected eShop transactions payload: {type(data).__name__}"
                )
            if data.get("errors"):
                raise RuntimeError(
                    f"Nintendo eShop GraphQL error: {json.dumps(data['errors'])[:300]}"
                )

            segment = (
                ((data.get("data") or {}).get("account") or {}).get("transactionHistories")
                or {}
            )
            page_transactions = segment.get("transactionHistories") or []
            offset_info = segment.get("offsetInfo")
            if isinstance(offset_info, dict) and isinstance(offset_info.get("total"), int):
                total = offset_info["total"]

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
