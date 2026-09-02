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
from typing import Any

import httpx

from .data.db import load_wishlist_alert_state, stamp_wishlist_alerts
from .tools.deals import get_wishlist_deals

logger = logging.getLogger(__name__)

# Discord rejects a message body over 2,000 characters and Slack truncates
# around the same size, so chunks are packed under a margin below both.
_MAX_CHUNK_CHARS = 1_900
_HEADER = "Wishlist deal alerts:"
_POST_TIMEOUT_SECONDS = 15.0


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
    triggered: list[tuple[int, str, str]], limit: int = _MAX_CHUNK_CHARS
) -> list[list[tuple[int, str, str]]]:
    """Pack (game_id, key, line) triples into chunks whose rendered text fits.

    The header is repeated on every chunk (so a second notification is
    self-describing) and its length is charged to every chunk's budget, which
    is what keeps ``_render`` under ``limit`` rather than approximately so. A
    single line longer than the budget still gets its own chunk; ``_render``
    truncates it rather than dropping the alert.
    """
    budget = limit - len(_HEADER) - 1
    chunks: list[list[tuple[int, str, str]]] = []
    current: list[tuple[int, str, str]] = []
    length = 0
    for item in triggered:
        cost = len(item[2]) + (1 if current else 0)
        if current and length + cost > budget:
            chunks.append(current)
            current, length = [], 0
            cost = len(item[2])
        current.append(item)
        length += cost
    if current:
        chunks.append(current)
    return chunks


def _render(chunk: list[tuple[int, str, str]], limit: int = _MAX_CHUNK_CHARS) -> str:
    text = "\n".join([_HEADER, *(line for _, _, line in chunk)])
    return text[:limit]


async def _post(client: httpx.AsyncClient, url: str, text: str) -> bool:
    """POST one chunk; True only on a 2xx. Never raises.

    The body carries the text under BOTH `content` (Discord) and `text`
    (Slack), so one env var works for either service without asking the user
    which one it is; each ignores the key it does not know.
    """
    try:
        response = await client.post(url, json={"content": text, "text": text})
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
            candidates.append((int(game_id), key, _format_line(deal, reason)))

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
            for chunk in _chunk(triggered):
                if await _post(client, url, _render(chunk)):
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
