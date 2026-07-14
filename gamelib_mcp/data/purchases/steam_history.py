"""Steam purchase-history importer.

Scrapes two logged-in store pages (there is no Web API for either):
- ``/account/licenses/`` — every license with its acquisition date and type
  ("Store Purchase", "Complimentary", "Gift/Guest Pass", "Retail", ...).
- ``/account/history/`` — the wallet/purchase history table, including an
  AJAX "load more" continuation (``g_historyCursor`` →
  ``/account/AjaxLoadMoreHistory/``).

Auth is the ``steamLoginSecure`` browser cookie stored as a ``{name: value}``
JSON file (same shape as the Humble/Nintendo cookie files) — set it with the
``set_steam_store_session`` MCP tool. ``sessionid`` is recommended too (the
load-more endpoint wants it).

Record building:
- Records come primarily from history "Purchase" rows: title, date, price.
  Multi-item carts split the row total evenly (last share absorbs rounding,
  humble.py convention). Market Transactions, In-Game Purchases AND Gift
  Purchases (bought FOR someone else — not a library acquisition) land in
  skipped with reasons.
- "Refund" rows are their own history row and Steam leaves the original
  purchase row in place, so both must be read together or refunded money
  gets booked as spend. ``apply_refunds`` drops the refunded item from its
  purchase row *before* the cart split, subtracting the amount the refund row
  says was actually returned (see its docstring for why the ordering matters).
- Licenses page rows with a Complimentary or Gift/Guest Pass acquisition type
  add zero-price records (purchase_source "free"/"gift") when their package
  name doesn't already match a history record (normalized-title containment).
  Common package suffixes ("XYZ Retail", "XYZ Beta") are stripped cheaply;
  anything fancier is left to the batch fuzzy matcher downstream.
- Totals are parsed defensively ("$19.99", "19,99€", "CDN$ 12.00", ...) into
  amount + best-effort currency, falling back to USD.
"""

import json
import logging
import os
import re
from datetime import date as _date

import httpx
from bs4 import BeautifulSoup, Tag

from . import PurchaseRecord
from gamelib_mcp.data.db import default_data_dir

logger = logging.getLogger(__name__)

PLATFORM = "steam"
PURCHASE_SOURCE = "steam"

_LICENSES_URL = "https://store.steampowered.com/account/licenses/"
_HISTORY_URL = "https://store.steampowered.com/account/history/"
_AJAX_MORE_URL = "https://store.steampowered.com/account/AjaxLoadMoreHistory/"
# Hard cap on load-more follow-ups.
_MAX_AJAX_CALLS = 50

_AUTH_ERROR = (
    "Steam store page request was not authenticated (steamLoginSecure cookie "
    "missing or expired) — re-run set_steam_store_session with fresh cookies "
    "from store.steampowered.com."
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})")

# Longest tokens first so "CDN$" wins over "$".
_CURRENCY_TOKENS = (
    ("CDN$", "CAD"), ("A$", "AUD"), ("NZ$", "NZD"), ("HK$", "HKD"),
    ("R$", "BRL"), ("S$", "SGD"), ("US$", "USD"), ("zł", "PLN"),
    ("€", "EUR"), ("£", "GBP"), ("₽", "RUB"), ("¥", "JPY"),
    ("₩", "KRW"), ("$", "USD"),
)
_ISO_CURRENCY_RE = re.compile(r"\b([A-Z]{3})\b")
_NUMBER_RE = re.compile(r"\d[\d.,\s\xa0]*")

_CURSOR_RE = re.compile(r"g_historyCursor\s*=\s*(\{.*?\})\s*;", re.S)

# Classes Steam renders inside the items cell that are badges, not line items.
_ITEM_BADGE_CLASSES = frozenset({"wth_item_refunded"})

_PACKAGE_SUFFIX_RE = re.compile(
    r"\s+(steam store and retail key|retail key|retail|beta testing|beta|demo"
    r"|guest pass|gift|comp)$",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _load_steam_cookies() -> dict[str, str] | None:
    """Load Steam store cookies from STEAM_STORE_COOKIES_FILE (same shape as
    the Nintendo/Humble cookie files: {name: value} or a Cookie Editor array)."""
    fallback_path = str(default_data_dir() / "steam_store_cookies.json")
    configured_path = os.getenv("STEAM_STORE_COOKIES_FILE") or fallback_path
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
            logger.warning("Failed to load Steam store cookies from %s: %s", path, exc)
            return None

    if raw is None:
        return None

    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw if isinstance(c, dict) and "name" in c and "value" in c}
    if isinstance(raw, dict):
        return raw
    return None


def parse_steam_date(text: str) -> str | None:
    """'12 Mar, 2021' → '2021-03-12' via a locale-independent month map."""
    match = _DATE_RE.search(text)
    if not match:
        return None
    day, month_text, year = match.groups()
    month = _MONTHS.get(month_text.lower())
    if month is None:
        return None
    try:
        return _date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def parse_price_string(text: str) -> tuple[float | None, str]:
    """'19,99€' → (19.99, 'EUR'); best-effort currency, fallback USD."""
    currency = None
    for token, code in _CURRENCY_TOKENS:
        if token in text:
            currency = code
            break
    if currency is None:
        iso = _ISO_CURRENCY_RE.search(text)
        currency = iso.group(1) if iso else "USD"

    match = _NUMBER_RE.search(text)
    if not match:
        return None, currency
    number = re.sub(r"[\s\xa0]", "", match.group(0))
    if "," in number and "." in number:
        # The rightmost separator is the decimal one; drop the other.
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        head, _, tail = number.rpartition(",")
        if len(tail) == 2 and head:
            number = head.replace(",", "") + "." + tail
        else:
            number = number.replace(",", "")
    try:
        return float(number), currency
    except ValueError:
        return None, currency


def _cell_text(cell: Tag) -> str:
    return " ".join(cell.get_text(" ", strip=True).split())


def parse_licenses(html: str) -> list[dict]:
    """``table.account_table`` rows → [{name, date, acquisition_type}]."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.account_table")
    if table is None:
        return []
    licenses: list[dict] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue  # header (th) or malformed row
        name_cell = cells[1]
        # The name cell can carry a "Remove" link div — drop child divs.
        for div in name_cell.find_all("div"):
            div.decompose()
        name = _cell_text(name_cell)
        if not name:
            continue
        licenses.append({
            "name": name,
            "date": parse_steam_date(_cell_text(cells[0])),
            "acquisition_type": _cell_text(cells[2]),
        })
    return licenses


def _row_type(cell: Tag) -> str:
    """First line of the type cell — the second div is the payment method."""
    div = cell.find("div")
    return _cell_text(div) if div is not None else _cell_text(cell)


def _is_item_badge(div: Tag) -> bool:
    """Decoration div inside the items cell (the "Refund" flag), not an item."""
    classes: list[str] = div.get("class") or []  # type: ignore[assignment]
    return any(cls in _ITEM_BADGE_CLASSES for cls in classes)


def _row_items(cell: Tag) -> list[str]:
    divs = cell.find_all("div")
    if divs:
        # Steam badges a refunded line with a sibling <div class="wth_item_refunded">
        # Refund</div> *inside* the items cell. It is decoration, not an item —
        # left in, it becomes a phantom item that steals a share of the row total.
        items = [_cell_text(d) for d in divs if not _is_item_badge(d)]
        return [i for i in items if i]
    text = _cell_text(cell)
    return [text] if text else []


def _parse_history_rows(
    soup: BeautifulSoup,
) -> tuple[list[dict], list[dict], list[dict]]:
    """(purchase rows, refund rows, skipped) from the wallet-history rows.

    Purchase and refund rows share the shape {date, items, total, currency};
    refunds are kept apart so ``apply_refunds`` can cancel what they undo.
    """
    purchases: list[dict] = []
    refunds: list[dict] = []
    skipped: list[dict] = []
    for row in soup.find_all("tr"):
        type_cell = row.find("td", class_="wht_type")
        items_cell = row.find("td", class_="wht_items")
        if type_cell is None or items_cell is None:
            continue
        row_type = _row_type(type_cell)
        items = _row_items(items_cell)
        title = items[0] if items else "(unknown item)"
        kind = row_type.strip().lower()
        if kind not in ("purchase", "refund"):
            skipped.append({
                "title": title,
                "reason": f"history row type '{row_type}' is not a game purchase",
            })
            continue
        if not items:
            skipped.append({"title": title, "reason": f"{row_type} row has no items"})
            continue

        date_cell = row.find("td", class_="wht_date")
        total_cell = row.find("td", class_="wht_total")
        total, currency = (
            parse_price_string(_cell_text(total_cell))
            if total_cell is not None
            else (None, "USD")
        )
        entry = {
            "date": parse_steam_date(_cell_text(date_cell)) if date_cell is not None else None,
            "items": items,
            "total": total,
            "currency": currency,
        }
        (refunds if kind == "refund" else purchases).append(entry)
    return purchases, refunds, skipped


def _extract_cursor(html: str) -> dict | None:
    match = _CURSOR_RE.search(html)
    if not match:
        return None
    try:
        cursor = json.loads(match.group(1))
    except ValueError:
        return None
    return cursor if isinstance(cursor, dict) else None


def parse_wallet_history(
    html: str,
) -> tuple[list[dict], list[dict], list[dict], dict | None]:
    """Full history page → (purchase rows, refund rows, skipped, load-more cursor)."""
    purchases, refunds, skipped = _parse_history_rows(BeautifulSoup(html, "lxml"))
    return purchases, refunds, skipped, _extract_cursor(html)


def parse_history_fragment(html: str) -> tuple[list[dict], list[dict], list[dict]]:
    """AJAX load-more fragment (bare <tr> rows) → (purchase rows, refund rows, skipped)."""
    # lxml drops orphan <tr> tags without a table context.
    return _parse_history_rows(BeautifulSoup(f"<table>{html}</table>", "lxml"))


def _split_amount(amount: float, count: int) -> list[float]:
    """Even split rounded to cents; the last share absorbs the remainder."""
    if count <= 0:
        return []
    share = round(amount / count, 2)
    shares = [share] * count
    shares[-1] = round(amount - share * (count - 1), 2)
    return shares


def strip_package_suffix(name: str) -> str:
    """Cheaply strip common license-package suffixes ('XYZ Retail', 'XYZ Beta')."""
    previous = None
    while previous != name:
        previous = name
        name = _PACKAGE_SUFFIX_RE.sub("", name).strip()
    return name


def _normalize_title(name: str) -> str:
    return _NON_ALNUM_RE.sub(" ", name.lower()).strip()


def _purchase_records(purchases: list[dict]) -> list[PurchaseRecord]:
    records: list[PurchaseRecord] = []
    for purchase in purchases:
        items = purchase["items"]
        total = purchase["total"]
        shares: list[float | None]
        if total is None:
            shares = [None] * len(items)  # unpriced beats a guessed price
        else:
            shares = list(_split_amount(total, len(items)))
        for title, share in zip(items, shares, strict=True):
            records.append(
                PurchaseRecord(
                    title=title,
                    platform=PLATFORM,
                    purchase_source=PURCHASE_SOURCE,
                    acquired_at=purchase["date"],
                    price_paid=share,
                    price_currency=purchase["currency"] if share is not None else None,
                )
            )
    return records


def apply_refunds(
    purchases: list[dict], refunds: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Remove refunded items from their purchase rows, *before* the cart split.

    Steam bills a refund as its own history row and leaves the original purchase
    row untouched, so reading only the purchase rows books money that came back.

    This deliberately runs ahead of ``_purchase_records``. A partial-cart refund
    has to subtract what Steam actually returned — the refund row's own total —
    from the cart total. The per-item share ``_purchase_records`` would compute
    is only an even-split *estimate* of what that item cost, so cancelling a
    built record would subtract the estimate instead of the real amount (a $30
    two-item cart with a $5 refund would drop to $15, not $25). Dropping the
    item here and subtracting the refund total leaves the remaining items to
    split what is genuinely left.

    Each refunded item is matched to the *latest* purchase row containing it,
    dated at or before the refund — so a re-purchase after a refund keeps its
    row, and a title bought twice but refunded once only loses one. A refund
    with nothing to cancel (its purchase predates the history window) is
    reported in skipped rather than silently ignored.
    """
    rows = [dict(purchase, items=list(purchase["items"])) for purchase in purchases]
    skipped: list[dict] = []
    for refund in refunds:
        refund_date = refund["date"]
        for title in refund["items"]:
            normalized = _normalize_title(title)
            if not normalized:
                continue
            candidates = [
                index
                for index, row in enumerate(rows)
                if any(_normalize_title(item) == normalized for item in row["items"])
                and (
                    refund_date is None
                    or row["date"] is None
                    or row["date"] <= refund_date
                )
            ]
            if not candidates:
                skipped.append({
                    "title": title,
                    "reason": "refund with no matching purchase row in this history window",
                })
                continue
            # Latest purchase at or before the refund; index breaks date ties.
            row = rows[max(candidates, key=lambda i: (rows[i]["date"] or "", i))]
            item_count = len(row["items"])
            for position, item in enumerate(row["items"]):
                if _normalize_title(item) == normalized:
                    del row["items"][position]
                    break
            if row["total"] is not None:
                if refund["total"] is not None and refund["currency"] == row["currency"]:
                    returned = refund["total"]
                else:
                    # No comparable refund amount — the even share is all we have.
                    returned = row["total"] / item_count
                row["total"] = max(0.0, round(row["total"] - returned, 2))
            skipped.append({
                "title": title,
                "reason": (
                    f"refunded on {refund_date or 'an unknown date'} — removed from the "
                    "matching purchase row"
                ),
            })
    return [row for row in rows if row["items"]], skipped


def merge_license_records(
    licenses: list[dict], purchase_records: list[PurchaseRecord]
) -> list[PurchaseRecord]:
    """Zero-price records for Complimentary / Gift/Guest Pass licenses that no
    history record already covers (normalized-title containment match)."""
    purchased = [t for t in (_normalize_title(r.title) for r in purchase_records) if t]
    records: list[PurchaseRecord] = []
    for license_row in licenses:
        kind = license_row["acquisition_type"].lower()
        if "complimentary" in kind or "free" in kind:
            source = "free"
        elif "gift" in kind or "guest pass" in kind:
            source = "gift"
        else:
            # Store Purchase / Retail licenses are Page B's territory.
            continue
        name = strip_package_suffix(license_row["name"])
        normalized = _normalize_title(name)
        if not normalized:
            continue
        if any(normalized in t or t in normalized for t in purchased):
            continue
        records.append(
            PurchaseRecord(
                title=name,
                platform=PLATFORM,
                purchase_source=source,
                acquired_at=license_row["date"],
                price_paid=0.0,
                price_currency=None,
            )
        )
    return records


def _check_page_auth(response: httpx.Response) -> None:
    if response.status_code in (401, 403):
        raise RuntimeError(_AUTH_ERROR)
    if "store.steampowered.com/login" in response.text or "/login" in str(response.url):
        # Logged-out requests bounce to the login page.
        raise RuntimeError(_AUTH_ERROR)


async def _fetch_history_pages(
    client: httpx.AsyncClient, headers: dict[str, str], sessionid: str | None
) -> tuple[list[dict], list[dict], list[dict]]:
    """History page + load-more follow-ups → (purchase rows, refund rows, skipped)."""
    resp = await client.get(_HISTORY_URL, headers=headers)
    _check_page_auth(resp)
    resp.raise_for_status()
    purchases, refunds, skipped, cursor = parse_wallet_history(resp.text)

    calls = 0
    while cursor and calls < _MAX_AJAX_CALLS:
        calls += 1
        data: dict[str, str] = {f"cursor[{k}]": str(v) for k, v in cursor.items()}
        if sessionid:
            data["sessionid"] = sessionid
        more_resp = await client.post(_AJAX_MORE_URL, data=data, headers=headers)
        if more_resp.status_code in (401, 403):
            raise RuntimeError(_AUTH_ERROR)
        more_resp.raise_for_status()
        try:
            payload = more_resp.json()
        except ValueError:
            break
        if not isinstance(payload, dict):
            break
        more_purchases, more_refunds, more_skipped = parse_history_fragment(
            str(payload.get("html") or "")
        )
        purchases.extend(more_purchases)
        refunds.extend(more_refunds)
        skipped.extend(more_skipped)
        next_cursor = payload.get("cursor")
        cursor = next_cursor if isinstance(next_cursor, dict) else None
    return purchases, refunds, skipped


async def fetch_steam_purchases(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[list[PurchaseRecord], list[dict]]:
    """Fetch Steam licenses + purchase history as purchase records.

    Raises RuntimeError on missing/stale auth (login redirect or 401/403);
    the orchestrator catches per source. ``transport`` exists for tests
    (httpx.MockTransport).
    """
    cookies = _load_steam_cookies()
    if not cookies:
        raise RuntimeError(
            "No Steam store session cookies found (STEAM_STORE_COOKIES_FILE "
            "not set or missing) — run set_steam_store_session first."
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        ),
    }

    async with httpx.AsyncClient(
        cookies=cookies, follow_redirects=True, timeout=30, transport=transport
    ) as client:
        licenses_resp = await client.get(_LICENSES_URL, headers=headers)
        _check_page_auth(licenses_resp)
        licenses_resp.raise_for_status()
        licenses = parse_licenses(licenses_resp.text)

        purchases, refunds, skipped = await _fetch_history_pages(
            client, headers, cookies.get("sessionid")
        )

    purchase_count = len(purchases)
    # Refunds first: they adjust the cart totals the split is computed from.
    # Also before the license merge — a refunded title is no longer "covered by
    # history", so a leftover free/gift license for it should still register.
    purchases, refund_skipped = apply_refunds(purchases, refunds)
    skipped.extend(refund_skipped)
    records = _purchase_records(purchases)
    records.extend(merge_license_records(licenses, records))

    logger.info(
        "Steam: %d licenses + %d history purchases - %d refunds → %d records, %d skipped",
        len(licenses), purchase_count, len(refunds), len(records), len(skipped),
    )
    return records, skipped
