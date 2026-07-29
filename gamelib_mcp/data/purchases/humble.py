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
  Key-delivery suffixes ("… Steam Key", "… Registration Key") are Humble
  packaging noise, not game identity, and are stripped from the title.
- Orders without tpks fall back to ``subproducts``; those have no platform
  signal, so they land on "other" (deliberately NOT guessed as steam).
  A subproduct whose downloads are all non-game media (ebook/audio/video —
  Humble Book Bundles have subproducts too) is excluded so novels never
  become library games; a subproduct with no downloads at all is kept
  (a bare key delivery is indistinguishable from a game there).
- In-game currency/consumable SKUs are excluded the same way (shared
  ``is_consumable_title``), including the tails Humble bolts onto a real game
  name ("Quake Champions Early Access plus 50 Shards, 100 Platinum, 2000
  Favor") — under create_missing those would mint a phantom owned game.
- Orders with neither tpks nor subproducts (soundtrack-only, ebook rewards,
  …) are skipped with a reason; excluded non-game subproducts are reported
  the same way. The order price splits only across the kept games.
- Humble exposes no per-item content typing, so an addon-ish NAME
  (match_addon_name: "… DLC", "… Season Pass", "… Soundtrack", …) becomes
  the record's content_type hint — those match exact-name-only and mint
  nested instead of as phantom base games.
- Multi-game orders split ``amount_spent`` evenly, rounded to 2 decimals with
  the last item absorbing the rounding remainder so the parts sum exactly to
  the order total; ``bundle_name`` is set only for category "bundle" orders
  with more than one game (per spec — subscription months still split, but
  keep purchase_source "subscription" as their grouping signal).
- category "subscriptioncontent"/"subscriptionplan" (Humble Choice) →
  purchase_source "subscription"; everything else → "humble". amount_spent 0
  (freebies) → price 0.0. A missing currency is assumed USD.
- Subscription plan payments ("Annual Plan", "12-Month Classic Plan",
  "Month-to-Month Classic Plan") are separate game-less orders — the monthly
  Choice drops themselves carry amount_spent 0. records_from_orders
  attributes chronologically via a FIFO credit queue: each plan payment
  pushes N month-credits (its price split N ways), each zero-priced Choice
  drop consumes the oldest credit as its month price and splits it across
  the drop's games. Stacked purchases (two annuals bought close together)
  simply queue 24 credits. Unconsumed credits and zero-amount plan orders
  (gifts/promos) are reported in skipped, so no subscription money silently
  vanishes.
"""

import asyncio
import json
import logging
import os
import re
from collections import deque

import httpx

from gamelib_mcp.data.content import classify_title_override, match_addon_name
from gamelib_mcp.data.db import default_data_dir

from . import PurchaseRecord, is_consumable_title, normalize_purchase_date

logger = logging.getLogger(__name__)

PURCHASE_SOURCE = "humble"

_ORDER_LIST_URL = "https://www.humblebundle.com/api/v1/user/order"
_ORDER_DETAIL_URL = "https://www.humblebundle.com/api/v1/order/{gamekey}"
# Politeness delay between sequential order-detail requests.
_REQUEST_DELAY_SECONDS = 0.2

_KEY_TYPE_TO_PLATFORM = {"steam": "steam", "gog": "gog"}

# Key-delivery tails on tpk/subproduct titles ("Dynamite Jack Steam Key",
# "Galcon Fusion Registration Key", "Frozen Synapse Steam/Multiplayer Key",
# "Hero Academy Gold Pack Content Code", "Destiny 2 - Expansion Pass -
# Blizzard Key") — packaging noise that defeats library name matching and
# would mint duplicate rows. "Multiplayer Key" is Humble's old giftable
# second copy of the same game. Requires a qualifier word so a game
# legitimately named "… Key" never trips it.
_KEY_QUALIFIER = (
    r"(?:steam|gog|origin|uplay|epic|desura|registration|multiplayer"
    r"|blizzard|content)"
)
_KEY_SUFFIX_RE = re.compile(
    rf"[\s:–—-]+{_KEY_QUALIFIER}(?:\s*/\s*{_KEY_QUALIFIER})*\s+(?:key|code)\s*$",
    re.IGNORECASE,
)

# Humble human_names can embed literal HTML ("… DLC<br />(DLC Bundle #1)").
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Marketing/promo tpk names that are not games at all ("Tropico 3 Free Key
# Expiration", "X-COM: UFO Defense Free Game Redemption Deadline", "Tomb
# Raider Monthly Outlast Deluxe Edition Cross-Promo", "… 2 Card Packs
# (Skyrim) …" consumables). Excluded like non-game media so their share of
# the order price redistributes to the real games.
_PROMO_NAME_RE = re.compile(
    r"cross-?promo|redemption deadline|key expiration|\bcard packs?\b"
    r"|\bevent tickets?\b",
    re.IGNORECASE,
)

# An enumerated multi-game SKU ("Peggle Deluxe, Bejeweled 3, Bookworm Deluxe,
# Escape Rosecliff Island, and Feeding Frenzy 2") names several games in one
# key — route it to bundles_needing_split instead of minting one giant row.
# Two ", " separators AND an Oxford ", and " required: single games with
# commas in the NAME exist ("Hack, Slash, Loot", "Cook, Serve, Delicious!",
# "RIVE: Wreck, Hack, Die, Retry") and must not divert; every real
# enumerated SKU observed ends its list with ", and ". Numeric commas
# ("Warhammer 40,000") have no trailing space and never trip this.
def _looks_like_enumerated_bundle(title: str) -> bool:
    return title.count(", ") >= 2 and ", and " in title

# Subproduct download platforms that signal a real game vs. bundled media.
# Unknown/future platform values deliberately count as game-ish — only a
# downloads list that is ENTIRELY known non-game media excludes a subproduct.
_NON_GAME_DOWNLOAD_PLATFORMS = frozenset({"ebook", "audio", "video"})

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


def _clean_title(name: str) -> str:
    """Strip embedded HTML and key-delivery tails (iterated so stacked tails
    peel off)."""
    cleaned = " ".join(_HTML_TAG_RE.sub(" ", name).split())
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = _KEY_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned or name.strip()


def _is_non_game_subproduct(sub: dict) -> bool:
    """True when every download on the subproduct is known non-game media
    (ebook/audio/video). No downloads at all = ambiguous = keep."""
    platforms = {
        str(d.get("platform") or "").lower()
        for d in sub.get("downloads") or []
        if isinstance(d, dict)
    }
    platforms.discard("")
    return bool(platforms) and platforms <= _NON_GAME_DOWNLOAD_PLATFORMS


def _order_games(order: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """Extract ([(title, platform)], excluded_non_game_titles) from an order —
    tpks first, subproducts as the fallback."""
    games: list[tuple[str, str]] = []
    non_game: list[str] = []
    tpks = (order.get("tpkd_dict") or {}).get("all_tpks") or []
    for tpk in tpks:
        if not isinstance(tpk, dict):
            continue
        name = tpk.get("human_name")
        if not name or not isinstance(name, str):
            continue
        if _PROMO_NAME_RE.search(name) or is_consumable_title(name):
            non_game.append(name.strip())
            continue
        key_type = str(tpk.get("key_type") or "").lower()
        games.append((_clean_title(name), _KEY_TYPE_TO_PLATFORM.get(key_type, "other")))
    if games or non_game:
        # tpks are authoritative when they yield anything — never also read
        # subproducts (they mirror the same items).
        return games, non_game

    for sub in order.get("subproducts") or []:
        if not isinstance(sub, dict):
            continue
        name = sub.get("human_name")
        if not name or not isinstance(name, str):
            continue
        if (
            _is_non_game_subproduct(sub)
            or _PROMO_NAME_RE.search(name)
            or is_consumable_title(name)
        ):
            non_game.append(name.strip())
            continue
        # No platform signal on a subproduct — "other" beats a wrong guess.
        games.append((_clean_title(name), "other"))
    return games, non_game


def _order_amount(order: dict) -> float:
    try:
        return float(order.get("amount_spent") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _plan_month_count(name: str) -> int:
    """Months of Choice coverage a plan payment buys, from its product name
    ("12-Month Classic Plan" → 12, "Annual Plan" → 12, month-to-month → 1)."""
    numbered = re.search(r"(\d+)[-\s]?month", name, re.IGNORECASE)
    if numbered:
        count = int(numbered.group(1))
        return count if count > 0 else 1
    if re.search(r"annual|year", name, re.IGNORECASE):
        return 12
    return 1


def records_from_order(
    order: dict, *, price_override: tuple[float, str] | None = None
) -> tuple[list[PurchaseRecord], list[dict]]:
    """Convert one order-detail payload into (records, skipped).

    ``price_override`` = (amount, currency) replaces the order's own
    amount_spent/currency — how a plan credit funds a zero-priced Choice drop.
    """
    product = order.get("product") or {}
    order_name = product.get("human_name") or order.get("gamekey") or "(unknown order)"
    category = str(product.get("category") or "").lower()

    games, non_game = _order_games(order)
    skipped: list[dict] = []
    if non_game:
        skipped.append(
            {
                "description": str(order_name),
                "reason": (
                    f"{len(non_game)} non-game item(s) excluded "
                    f"(ebook/audio/video or promo): {', '.join(non_game[:5])}"
                    + (", …" if len(non_game) > 5 else "")
                ),
            }
        )
    if not games:
        if not skipped:
            skipped.append(
                {"description": str(order_name), "reason": "no game keys or subproducts"}
            )
        return [], skipped

    if price_override is not None:
        amount_spent, currency = price_override
    else:
        amount_spent = _order_amount(order)
        currency = str(order.get("currency") or "USD")
    acquired_at = normalize_purchase_date(order.get("created"))
    source = "subscription" if category.startswith("subscription") else PURCHASE_SOURCE
    bundle_name = (
        str(order_name) if category == "bundle" and len(games) > 1 else None
    )
    shares = _split_amount(amount_spent, len(games))

    records = []
    for (title, platform), share in zip(games, shares, strict=True):
        # Humble has no per-item content typing — an addon-ish NAME is the
        # only nested signal, so DLC/soundtrack keys match exact-name-only
        # and mint nested instead of as phantom base games. Known DLC whose
        # name carries no addon-ish word ("Outlast: Whistleblower") comes
        # from the title-override table instead.
        addon = match_addon_name(title)
        if addon is None:
            override = classify_title_override(title)
            if override is not None and not override.is_primary_library_item:
                addon = (override.content_type, "title override")
        records.append(
            PurchaseRecord(
                title=title,
                platform=platform,
                purchase_source=source,
                acquired_at=acquired_at,
                price_paid=share,
                price_currency=currency,
                bundle_name=bundle_name,
                content_type=addon[0] if addon is not None else None,
                # One key naming several games — divert to
                # bundles_needing_split rather than minting a giant row.
                is_bundle=_looks_like_enumerated_bundle(title),
            )
        )
    return records, skipped


def records_from_orders(orders: list[dict]) -> tuple[list[PurchaseRecord], list[dict]]:
    """Convert all order payloads, attributing subscription plan payments to
    the Choice drops they funded via a FIFO credit queue.

    Two passes over the date-sorted history, deliberately NOT requiring a
    credit to predate the drop it funds: the bundle is revealed the first
    Tuesday of a month while subscriber auto-billing runs the last Tuesday
    (and since the 2022 flat-price change a month can be bought at any point
    in it), so a drop's content order routinely precedes its charge. Pass 1
    collects every plan order (subscription category, no games): a paid one
    pushes N month-credits (its price split N ways). Pass 2 walks the
    zero-priced subscription drops in the same date order, each consuming
    the oldest credit as its month price. Stacked plan purchases just queue
    more credits; a drop left without a credit stays price 0 (trial/free
    month); leftover credits are months paid for but not (yet) delivered.
    """

    def order_facts(order: dict) -> tuple[str, str, list[tuple[str, str]], bool]:
        product = order.get("product") or {}
        category = str(product.get("category") or "").lower()
        name = str(
            product.get("human_name") or order.get("gamekey") or "(unknown order)"
        )
        games, _ = _order_games(order)
        is_plan = category.startswith("subscription") and not games
        return category, name, games, is_plan

    ordered = sorted(
        orders, key=lambda o: normalize_purchase_date(o.get("created")) or ""
    )

    credits: deque[tuple[float, str]] = deque()
    records: list[PurchaseRecord] = []
    skipped: list[dict] = []
    for order in ordered:
        _, name, _, is_plan = order_facts(order)
        if not is_plan:
            continue
        amount = _order_amount(order)
        currency = str(order.get("currency") or "USD")
        when = normalize_purchase_date(order.get("created")) or "undated"
        if amount > 0:
            months = _plan_month_count(name)
            for share in _split_amount(amount, months):
                credits.append((share, currency))
            skipped.append(
                {
                    "description": name,
                    "reason": (
                        f"{when}: subscription plan payment {amount:.2f} {currency} "
                        f"→ {months} month credit(s) attributed to Choice drops"
                    ),
                }
            )
        else:
            skipped.append(
                {
                    "description": name,
                    "reason": (
                        f"{when}: subscription plan order with no recorded "
                        "payment (gift or promo?) — funds no drops"
                    ),
                }
            )

    for order in ordered:
        category, _, games, is_plan = order_facts(order)
        if is_plan:
            continue
        override = None
        if (
            category.startswith("subscription")
            and games
            and _order_amount(order) == 0
            and credits
        ):
            override = credits.popleft()
        order_records, order_skipped = records_from_order(
            order, price_override=override
        )
        records.extend(order_records)
        skipped.extend(order_skipped)

    if credits:
        by_currency: dict[str, tuple[int, float]] = {}
        for share, currency in credits:
            count, total = by_currency.get(currency, (0, 0.0))
            by_currency[currency] = (count + 1, total + share)
        for currency, (count, total) in sorted(by_currency.items()):
            skipped.append(
                {
                    "description": "subscription plan credits",
                    "reason": (
                        f"{count} unconsumed month credit(s) ({total:.2f} {currency}) "
                        "— months paid for but not (yet) delivered, e.g. skipped/"
                        "paused months or the plan's remaining term"
                    ),
                }
            )
    return records, skipped


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

    orders: list[dict] = []
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
            orders.append(order)

    # Cross-order plan-payment attribution needs the full history in hand.
    records, skipped = records_from_orders(orders)
    logger.info(
        "Humble: fetched %d orders → %d purchases, %d skipped",
        len(gamekeys), len(records), len(skipped),
    )
    return records, skipped
