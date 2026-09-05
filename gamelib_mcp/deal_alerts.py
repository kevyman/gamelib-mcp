"""Deal alerts — push a message when a wishlist price crosses a line the user
already drew for himself.

Two triggers, both of them lines that already exist in the data rather than a
new number to configure (07-06 roadmap item 3, reshaped): a price at or below
the `target_price` a recorded verdict named (`below_assessed_target`), and a
price at ITAD's all-time low with an active discount (`at_history_low` plus a
non-zero cut). There is deliberately no `alert_price` column and no
`set_wishlist_alert` tool — ADR 0004's surface budget says a feature that can
ride on data the user already entered should not mint a tool of its own.

Debounce lives in ``game_wishlist.last_alert_key``: the key encodes the event
AND the price that produced it (``target:19.99`` / ``low:12.49``), so the same
deal never repeats while a FURTHER drop mints a new key and speaks again. It is
a debounce, not a mute.

Contract: this never raises and never fails the refresh that called it. A
webhook that is down loses an alert, which is recoverable; a webhook that takes
the library sync down with it is not. Nothing is stamped unless the POST
actually succeeded — stamping a failed send would silence the retry, and the
missed price drop is exactly what the feature exists to prevent.
"""

import logging
import os
import re
from datetime import date
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .data.db import load_wishlist_alert_state, stamp_wishlist_alerts
from .tools.deals import get_wishlist_deals

logger = logging.getLogger(__name__)

# Discord rejects a message body over 2,000 characters and Slack truncates
# around the same size, so chunks are packed under a margin below both.
_MAX_CHUNK_CHARS = 1_900
_HEADER = "Wishlist deal alerts:"
_POST_TIMEOUT_SECONDS = 15.0
_DISCORD_HEADER = "## 🏷️ Wishlist deals"
# Reserve space for the heading, total deal count, and page numbers.
_DISCORD_HEADER_BUDGET = 100


def _is_discord(url: str) -> bool:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        # Detection must not bypass per-chunk delivery failure accounting.
        return False
    return host in {
        "discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com",
    }


def _discord_text(value: Any, limit: int = 100) -> str:
    """Keep provider text on one line and outside Discord's Markdown syntax."""
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~])", r"\\\1", text)


def _format_discord_line(deal: dict, reason: str) -> str:
    """A price-first row and quiet metadata, bounded before adding Markdown."""
    title = _discord_text(deal.get("name") or "Untitled game")
    url = str(deal.get("deal_url") or "")
    try:
        parsed = urlsplit(url)
        valid_url = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        valid_url = False
    if valid_url:
        # Escape delimiters without changing query parameters or existing escapes.
        url = quote(url, safe=":/?&=%#@+;,$!~-._")
        if len(url) <= 900:
            title = f"[{title}](<{url}>)"

    price = f"{deal['price']:.2f}"
    currency = deal.get("currency")
    if currency in {"EUR", "USD", "GBP"}:
        symbol = {"EUR": "€", "USD": "$", "GBP": "£"}[currency]
        amount = f"{symbol}{price}"
    else:
        amount = f"{price} {_discord_text(currency, 12)}".strip()
    cut = deal.get("cut_pct")
    discount = f" `−{cut:g}%`" if cut else ""

    platform = str(deal.get("platform") or "")
    platform = {"gog": "GOG", "ps5": "PS5", "switch2": "Switch 2"}.get(
        platform, platform.title()
    )
    details = [_discord_text(platform, 40)] if platform else []
    shop = str(deal.get("shop") or "")
    shop = {"dekudeals": "DekuDeals"}.get(shop, shop)
    if shop and shop.casefold() != platform.casefold():
        details.append(_discord_text(shop, 60))
    details.append("🎯 Target reached" if reason == "reached your target price" else "All-time low")
    ends = deal.get("deal_ends_at")
    if ends:
        try:
            day = date.fromisoformat(str(ends)[:10])
        except ValueError:
            pass  # An invalid provider date must not prevent the alert.
        else:
            details.append(f"Ends {day.day} {day:%b}")
    return f"**{amount}**{discount} **{title}**\n-# {' · '.join(details)}"


def _text_length(text: str) -> int:
    # Count astral emoji conservatively as two units for Discord's limit.
    return len(text.encode("utf-16-le")) // 2


def is_deal_alerts_configured() -> bool:
    """True when a webhook URL is set. Absent/blank = the feature is off."""
    return bool(os.getenv("DEAL_ALERT_WEBHOOK_URL", "").strip())


def _trigger_for(deal: dict) -> tuple[str, str] | None:
    """(debounce key, human reason) for one deal entry, or None if it is quiet.

    The target beats the all-time low when both hold: "you said you'd buy it
    at this price" is a decision the user already made, while "cheapest ever"
    is only an observation. The low additionally requires a real discount —
    a permanently-priced game sits at its own all-time low forever and would
    otherwise alert the first time it was ever seen.
    """
    price = deal.get("price")
    if price is None:
        return None
    if deal.get("below_assessed_target"):
        return f"target:{price:.2f}", "reached your target price"
    if deal.get("at_history_low") and (deal.get("cut_pct") or 0) > 0:
        return f"low:{price:.2f}", "at its all-time low"
    return None


def _format_line(deal: dict, reason: str) -> str:
    """One deal, one line: what it is, what it costs, why you are being told."""
    price = f"{deal['price']:.2f}"
    currency = deal.get("currency")
    amount = f"{price} {currency}" if currency else price
    parts = [f"{deal.get('name')} — {amount} on {deal.get('platform')}"]
    shop = deal.get("shop")
    if shop:
        parts[0] += f" ({shop})"
    cut = deal.get("cut_pct")
    if cut:
        parts.append(f"-{cut}%")
    parts.append(reason)
    ends = deal.get("deal_ends_at")
    if ends:
        # Date only: the hour a sale ends is noise in a push notification.
        parts.append(f"ends {str(ends)[:10]}")
    url = deal.get("deal_url")
    if url:
        parts.append(str(url))
    return " — ".join(parts)


def _chunk(
    triggered: list[tuple[int, str, str]], limit: int = _MAX_CHUNK_CHARS,
    *, discord: bool = False,
) -> list[list[tuple[int, str, str]]]:
    """Pack (game_id, key, line) triples into chunks whose rendered text fits.

    Each chunk reserves space for its header. An oversized plain-text row
    gets its own chunk and ``_render`` truncates it. Discord rows are bounded
    when formatted to preserve whole links and Markdown delimiters;
    ``_render_discord`` rejects any assembled page still exceeding the limit.
    """
    budget = limit - (_DISCORD_HEADER_BUDGET if discord else len(_HEADER) + 1)
    separator_size = 2 if discord else 1
    chunks: list[list[tuple[int, str, str]]] = []
    current: list[tuple[int, str, str]] = []
    length = 0
    for item in triggered:
        cost = _text_length(item[2]) + (separator_size if current else 0)
        if current and length + cost > budget:
            chunks.append(current)
            current, length = [], 0
            cost = _text_length(item[2])
        current.append(item)
        length += cost
    if current:
        chunks.append(current)
    return chunks


def _render(chunk: list[tuple[int, str, str]], limit: int = _MAX_CHUNK_CHARS) -> str:
    text = "\n".join([_HEADER, *(line for _, _, line in chunk)])
    return text[:limit]


def _render_discord(
    chunk: list[tuple[int, str, str]], page: int, pages: int, total: int,
    limit: int = _MAX_CHUNK_CHARS,
) -> str:
    """Render a complete page; reject overflow so no incomplete alert is stamped."""
    summary = f"{total} {'deal' if total == 1 else 'deals'}"
    if pages > 1:
        summary += f" · {page}/{pages}"
    text = f"{_DISCORD_HEADER}\n-# {summary}\n\n" + "\n\n".join(
        line for _, _, line in chunk
    )
    if _text_length(text) > limit:
        raise ValueError(f"Discord deal page exceeds {limit} UTF-16 units")
    return text


async def _post(client: httpx.AsyncClient, url: str, text: str, *, discord: bool = False) -> bool:
    """POST one chunk; True only on a 2xx. Never raises.

    Discord gets native Markdown with unfurls and mentions disabled. Other
    endpoints retain the plain content/text payload for Slack compatibility.
    """
    try:
        body = (
            {"content": text, "flags": 4, "allowed_mentions": {"parse": []}}
            if discord else {"content": text, "text": text}
        )
        response = await client.post(url, json=body)
    except Exception as exc:  # noqa: BLE001 - isolation boundary: a dead webhook must not fail a refresh
        logger.warning("Deal alert POST failed: %s", exc)
        return False
    if response.status_code // 100 != 2:
        logger.warning("Deal alert POST returned HTTP %s", response.status_code)
        return False
    return True


async def run_deal_alerts() -> dict[str, Any]:
    """Check wishlist deals and notify about the newly-triggered ones.

    Returns {"configured", "checked" (deals examined), "triggered" (deals
    whose event is new since their last alert), "sent" and "failed" (games in
    delivered / undelivered chunks). Never raises: every failure is logged and
    reported in the return value.
    """
    result: dict[str, Any] = {
        "configured": is_deal_alerts_configured(),
        "checked": 0,
        "triggered": 0,
        "sent": 0,
        "failed": 0,
    }
    if not result["configured"]:
        return result

    url = os.environ["DEAL_ALERT_WEBHOOK_URL"].strip()
    try:
        discord = _is_discord(url)
        # No arguments: re-prices through get_wishlist_deals' own 12h TTL
        # rather than around it. An alert run is a background caller and has
        # no business forcing live fetches on every library refresh.
        deals_response = await get_wishlist_deals()
        deals = deals_response.get("deals") or []
        result["checked"] = len(deals)

        candidates: list[tuple[int, str, str]] = []
        for deal in deals:
            trigger = _trigger_for(deal)
            game_id = deal.get("game_id")
            if trigger is None or game_id is None:
                continue
            key, reason = trigger
            line = _format_discord_line(deal, reason) if discord else _format_line(deal, reason)
            candidates.append((int(game_id), key, line))

        state = await load_wishlist_alert_state(game_id for game_id, _, _ in candidates)
        triggered = [
            item
            for item in candidates
            if (state.get(item[0]) or {}).get("last_alert_key") != item[1]
        ]
        result["triggered"] = len(triggered)
        if not triggered:
            return result

        async with httpx.AsyncClient(timeout=_POST_TIMEOUT_SECONDS) as client:
            chunks = _chunk(triggered, discord=discord)
            for page, chunk in enumerate(chunks, 1):
                try:
                    text = (
                        _render_discord(chunk, page, len(chunks), len(triggered))
                        if discord else _render(chunk)
                    )
                except ValueError as exc:
                    logger.warning("Deal alert page %s could not be rendered: %s", page, exc)
                    result["failed"] += len(chunk)
                    continue
                if await _post(client, url, text, discord=discord):
                    await stamp_wishlist_alerts({gid: key for gid, key, _ in chunk})
                    result["sent"] += len(chunk)
                else:
                    result["failed"] += len(chunk)
    except Exception as exc:
        # Isolation boundary: alerts never fail their caller (the library
        # refresh). logger.exception keeps the traceback; the caller gets a
        # result dict that says what happened.
        logger.exception("Deal alert run failed")
        result["error"] = str(exc)
    return result
