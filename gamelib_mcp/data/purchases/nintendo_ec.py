"""Nintendo eShop purchase-history importer.

Reads the transaction history behind https://ec.nintendo.com/my/transactions/
via the Savanna GraphQL API that page calls.

Auth (why we store *accounts.nintendo.com* cookies, not the eShop session):
The eShop session cookie ``__Secure-next-auth.session-token`` is a NextAuth JWE
with a hard 1-hour lifetime — storing it directly means re-pasting hourly. But a
logged-in browser never re-prompts, because when that cookie lapses the eShop web
app silently re-runs a Nintendo Account OAuth code exchange that only succeeds
thanks to the long-lived central login session on ``accounts.nintendo.com`` (the
``NASID``/``NATID``/``NAID``-family cookies — the same session that keeps you
signed in across every Nintendo web property, good for weeks-to-months). So this
module reuses *those* cookies — the very same accounts.nintendo.com session that
VGCS ownership sync already stores (``set_nintendo_session`` → ``NINTENDO_COOKIES_FILE``)
— and replicates that silent handshake on demand (:func:`_establish_ec_session`),
minting a fresh session on every import with no keep-warm loop and no dependence
on process uptime:

1. ``GET  /api/auth/csrf``               → NextAuth CSRF token (+ csrf cookie).
2. ``POST /api/auth/signin/nintendo``    (``callbackUrl``/``csrfToken``/``json=true``)
   → the ``accounts.nintendo.com/connect/1.0.0/authorize`` URL, and a
   ``__Secure-next-auth.state`` cookie.
3. ``GET  <authorize URL>``              with the accounts cookies → 302 to the
   callback carrying an OAuth ``code`` (no login page while the account session
   is alive; a redirect to ``/login`` means it finally expired → re-export).
4. ``GET  /api/auth/callback/nintendo``  validates ``state``, exchanges the code
   server-side, and sets a fresh ``__Secure-next-auth.session-token``.

Then, as before:

5. ``GET  /api/auth/session``            → short-lived Nintendo Account ``idToken``
   (~15 min) plus the account's ``country``/``language``.
6. ``GET  https://wb.lp1.savanna.srv.nintendo.net/graphql`` with that ``idToken``
   in the query string (auth is the token, NOT cookies) returns paginated
   transactions. The query is a *persisted query* — the server only accepts the
   SHA-256 hash of the exact query Nintendo's web build ships; arbitrary GraphQL
   and introspection are rejected. If Nintendo redeploys and the hash/client-id
   drift, ``NINTENDO_EC_QUERY_HASH`` / ``NINTENDO_EC_CLIENT_ID`` override the
   pinned defaults without a code change.

Configure with the ``set_nintendo_session`` MCP tool (export your
accounts.nintendo.com cookies while logged in) — the same tool/session used for
Switch ownership, so eShop purchase import needs no extra setup. The legacy
``set_nintendo_ec_session`` path (a raw ec.nintendo.com session-token, valid ≤1h)
still works as a fallback when no account cookies are stored, but has to be
re-pasted every hour. The longer-lived mobile ``session_token`` flow used by
``nintendo_pctl.py`` is NOT available for this web OAuth client.

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

_BASE_URL = "https://ec.nintendo.com"
_SESSION_URL = f"{_BASE_URL}/api/auth/session"
_CSRF_URL = f"{_BASE_URL}/api/auth/csrf"
_SIGNIN_URL = f"{_BASE_URL}/api/auth/signin/nintendo"
_CALLBACK_URL = f"{_BASE_URL}/my/transactions/1"
_GRAPHQL_URL = "https://wb.lp1.savanna.srv.nintendo.net/graphql"
_SESSION_COOKIE = "__Secure-next-auth.session-token"
_DEFAULT_COOKIES_FILENAME = "nintendo_ec_cookies.json"
# The eShop importer shares the one Nintendo Account session that VGCS ownership
# already stores (data/nintendo.py, set_nintendo_session) — same accounts.nintendo.com
# login cookies, same file. No separate export.
_ACCOUNT_COOKIES_ENV = "NINTENDO_COOKIES_FILE"
_ACCOUNT_COOKIES_FILENAME = "nintendo_cookies.json"
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

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
    "Nintendo eShop session is missing or expired — re-run set_nintendo_session "
    "with a fresh cookie export from accounts.nintendo.com (logged in)."
)
# Raised when the silent OAuth handshake bounced to a login page: the central
# accounts.nintendo.com session itself has expired (months, not hours), so a
# fresh export from that domain is the only fix.
_ACCOUNTS_AUTH_ERROR = (
    "Nintendo Account session (accounts.nintendo.com) has expired — sign in again "
    "in your browser, re-export your accounts.nintendo.com cookies, and re-run "
    "set_nintendo_session. (The same session also drives Switch ownership sync.)"
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


def _load_cookies(env_var: str, default_filename: str, label: str) -> dict[str, str] | None:
    """Load a stored cookie export as {name: value}.

    Mirrors data/nintendo.py::_load_vgcs_cookies: configured path (``env_var``)
    first, then the default beside the database; accepts both {name: value} and
    Cookie Editor array JSON.
    """
    fallback_path = str(default_data_dir() / default_filename)
    configured_path = os.getenv(env_var) or fallback_path
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
            logger.warning("Failed to load %s cookies from %s: %s", label, path, exc)
            return None

    if raw is None:
        return None

    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw if isinstance(c, dict) and "name" in c and "value" in c}
    if isinstance(raw, dict):
        return raw
    return None


def _load_account_cookies() -> dict[str, str] | None:
    """Load the shared Nintendo Account session (accounts.nintendo.com cookies).

    This is the SAME file VGCS ownership sync uses (``NINTENDO_COOKIES_FILE``,
    written by ``set_nintendo_session``) — one login session powers both
    ownership and eShop purchase history, so there is nothing extra to export.
    """
    return _load_cookies(_ACCOUNT_COOKIES_ENV, _ACCOUNT_COOKIES_FILENAME, "Nintendo Account")


def _load_ec_cookies() -> dict[str, str] | None:
    """Load the legacy ec.nintendo.com session cookies (raw session-token, ≤1h)."""
    return _load_cookies(
        "NINTENDO_EC_COOKIES_FILE", _DEFAULT_COOKIES_FILENAME, "Nintendo eShop"
    )


async def _establish_ec_session(
    client: httpx.AsyncClient, accounts_cookies: dict[str, str]
) -> None:
    """Mint a fresh eShop session by replaying the browser's silent SSO handshake.

    Runs csrf → signin → authorize → callback (see module docstring). On success
    the client's jar carries a fresh ``__Secure-next-auth.session-token`` and the
    caller can proceed to :func:`_fetch_id_token`. The accounts cookies are seeded
    into the jar so the ``accounts.nintendo.com`` ``authorize`` hop is satisfied
    without a login prompt.

    Raises RuntimeError(_ACCOUNTS_AUTH_ERROR) when the handshake bounces to a
    login page (the central account session has expired) or otherwise fails to
    produce a session cookie.
    """
    for name, value in accounts_cookies.items():
        client.cookies.set(name, value)

    headers = {"User-Agent": _USER_AGENT, "Referer": f"{_BASE_URL}/"}

    csrf_resp = await client.get(_CSRF_URL, headers={**headers, "Accept": "application/json"})
    csrf_resp.raise_for_status()
    csrf_token = None
    try:
        csrf_token = csrf_resp.json().get("csrfToken")
    except Exception:
        csrf_token = None
    if not csrf_token:
        raise RuntimeError(_ACCOUNTS_AUTH_ERROR)

    signin_resp = await client.post(
        _SIGNIN_URL,
        data={"callbackUrl": _CALLBACK_URL, "csrfToken": csrf_token, "json": "true"},
        headers={
            **headers,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    signin_resp.raise_for_status()
    try:
        authorize_url = signin_resp.json().get("url")
    except Exception:
        authorize_url = None
    if not authorize_url or "authorize" not in authorize_url:
        raise RuntimeError(_ACCOUNTS_AUTH_ERROR)

    # Follows authorize → callback → transactions. The 302 to the callback carries
    # the OAuth code; the ec cookies set during csrf/signin (state) validate it.
    final = await client.get(authorize_url, headers=headers)
    landed_on_login = "/login" in str(final.url) or "accounts.nintendo.com" in final.url.host
    if landed_on_login or not _has_session_cookie(_jar_names(client)):
        raise RuntimeError(_ACCOUNTS_AUTH_ERROR)


def _jar_names(client: httpx.AsyncClient) -> dict[str, str]:
    """Current jar as {name: value} (host-scoped cookies win name collisions)."""
    flat: dict[str, str] = {}
    for cookie in sorted(client.cookies.jar, key=lambda c: bool(c.domain)):
        if cookie.value is not None:
            flat[cookie.name] = cookie.value
    return flat


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

    Auth resolves in one of two ways: preferred is a stored accounts.nintendo.com
    session, from which :func:`_establish_ec_session` mints a fresh eShop session
    on the spot; the fallback is a legacy raw ec.nintendo.com session-token used
    directly (valid ≤1h). Then paginates the Savanna GraphQL API (?limit=50&
    offset=N) until a page returns fewer than the limit (or the reported total is
    reached), hard-capped at _MAX_PAGES. Raises RuntimeError on missing/stale
    auth; the orchestrator catches per source. ``transport`` exists for tests
    (httpx.MockTransport) — production callers pass nothing.
    """
    accounts_cookies = _load_account_cookies()
    ec_cookies = _load_ec_cookies()
    if not accounts_cookies and not (ec_cookies and _has_session_cookie(ec_cookies)):
        raise RuntimeError(
            "No Nintendo Account session found — run set_nintendo_session with a "
            "cookie export from accounts.nintendo.com (logged in). The same session "
            "also drives Switch ownership sync."
        )

    query_hash = os.getenv("NINTENDO_EC_QUERY_HASH") or _DEFAULT_QUERY_HASH
    client_id = os.getenv("NINTENDO_EC_CLIENT_ID") or _DEFAULT_CLIENT_ID
    try:
        shop_id = int(os.getenv("NINTENDO_EC_SHOP_ID") or _DEFAULT_SHOP_ID)
    except ValueError:
        shop_id = _DEFAULT_SHOP_ID

    extensions = json.dumps(
        {"persistedQuery": {"sha256Hash": query_hash, "version": 1}},
        separators=(",", ":"),
    )

    raw_transactions: list = []
    # Legacy path seeds the raw session-token; the accounts path seeds nothing
    # here and mints the session via the SSO handshake below.
    seed_cookies = {} if accounts_cookies else (ec_cookies or {})
    async with httpx.AsyncClient(
        cookies=seed_cookies, follow_redirects=True, timeout=30, transport=transport
    ) as client:
        if accounts_cookies:
            await _establish_ec_session(client, accounts_cookies)
        id_token, country, language = await _fetch_id_token(client)

        headers = {
            "User-Agent": _USER_AGENT,
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
