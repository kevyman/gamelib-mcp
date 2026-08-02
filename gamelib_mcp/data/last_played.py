"""The cross-platform last-played contract.

``game_platforms.last_played`` is an ISO ``YYYY-MM-DD`` date meaning "the last
day this platform's own source says you played this game". It is deliberately
coarse — a date, not a timestamp — because every source reports it differently
(PSN a datetime, Steam a unix epoch, Xbox an ISO-8601 string with 7-digit
fractional seconds) and nothing downstream needs better than day resolution.

It is a *signal*, not a derived value: no code computes it from playtime, and a
source that does not report one leaves it NULL. NULL means "unknown", never
"never played" — readers must branch on that rather than treating NULL as an
old date. ``tools/history.py`` is the main consumer: it suppresses a play-history
window delta for a game whose last_played predates the window, since cumulative
snapshots attribute growth to the sync that observed it rather than to the day
it was played (a corrected total otherwise reads as a play session).

Coverage is best-effort and uneven, which is expected:
  psn       — every row (title_stats last_played_date_time)
  steam     — GetOwnedGames rtime_last_played, mirrored from steam_platform_data
  switch2   — Parental Controls daily summaries
  xbox      — OpenXBL titleHistory.lastTimePlayed, when present
  epic      — the playtime API's last-played field, when present
  gog       — none; lgogdownloader exposes no playtime or last-played at all
"""

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Field names seen (or plausibly used) for a last-played timestamp on the Epic
# and Xbox payloads. Both are probed defensively rather than pinned to one key:
# these are undocumented third-party shapes, the value is strictly optional, and
# a rename must degrade to NULL rather than break an ownership sync.
EPIC_LAST_PLAYED_KEYS = ("lastPlayedDateTime", "lastPlayed", "lastModified")
XBOX_LAST_PLAYED_KEYS = ("lastTimePlayed", "lastPlayed")


def coerce_last_played_date(value: object) -> str | None:
    """Best-effort coercion of a source timestamp to an ISO ``YYYY-MM-DD`` date.

    Accepts a unix epoch (int/float, or a string of digits) or an ISO-8601
    string, including the trailing-``Z`` and 7-digit-fractional-second forms
    Xbox emits, which ``datetime.fromisoformat`` rejects before Python 3.11.
    Returns None for anything unparseable, for a non-positive epoch (0 is
    Steam's "never played"), and for a timestamp before 1980 — no real play
    session predates the platforms, so those are sentinel values, not dates.
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().lstrip("-").isdigit()
    ):
        try:
            epoch = float(value)
        except (TypeError, ValueError):
            return None
        if epoch <= 0:
            return None
        try:
            parsed = datetime.fromtimestamp(epoch, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
        return _guard_sentinel(parsed)

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    # "…Z" and "…+00:00Z" both appear in the wild; normalize to an offset.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Xbox emits 7 fractional digits; fromisoformat accepts at most 6.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        for char in tail:
            if char.isdigit():
                digits += char
            else:
                break
        remainder = tail[len(digits):]
        if digits:
            text = f"{head}.{digits[:6]}{remainder}"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # A bare date ("2024-03-02") already round-trips above; anything else
        # is a shape we do not model, and last-played is strictly optional.
        logger.debug("Unparseable last-played timestamp: %r", value)
        return None
    return _guard_sentinel(parsed)


def _guard_sentinel(parsed: datetime) -> str | None:
    if parsed.year < 1980:
        return None
    return parsed.date().isoformat()


def extract_last_played(payload: object, keys: tuple[str, ...]) -> str | None:
    """Return the first parseable last-played date among ``keys`` on a dict."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key not in payload:
            continue
        coerced = coerce_last_played_date(payload[key])
        if coerced is not None:
            return coerced
    return None
