"""Mask credential values that ride in URL query strings.

Several providers authenticate with a query parameter (the Steam Web API and
IsThereAnyDeal both take ``key=``), and httpx echoes the full request URL in
its ``HTTPStatusError`` message. Any sync/refresh failure that is turned into
text for a log line or a stored status field (``integration_sync_*_last_error_summary``,
``library_sync_error``) passes through :func:`redact_secrets` so the key never
reaches logs, the meta table, or the clients that read it back.

``tools.common.describe_failure`` additionally strips whole query strings from
exception text; this helper is the narrower net applied at the persist/echo
boundaries, where the text may come from a provider-built ``error_summary``
rather than an exception.
"""

import re
from typing import TypeVar, overload

_T = TypeVar("_T")

SECRET_QUERY_PARAMS: tuple[str, ...] = (
    "key",
    "api_key",
    "apikey",
    "access_token",
    "token",
    "npsso",
    "client_secret",
)

REDACTED = "***"

# A secret parameter only counts when it starts a query component (right after
# ``?`` or ``&``), so ``monkey=`` or ``steam_key=`` never match. The value runs
# to the next separator, fragment, whitespace or quote — httpx wraps the URL in
# single quotes inside its error message.
_SECRET_PARAM_RE = re.compile(
    r"(?P<prefix>[?&](?:" + "|".join(SECRET_QUERY_PARAMS) + r")=)[^&#\s'\"<>]+",
    re.IGNORECASE,
)


@overload
def redact_secrets(text: str) -> str: ...
@overload
def redact_secrets(text: Exception) -> str: ...
@overload
def redact_secrets(text: _T) -> _T: ...
def redact_secrets(text: object) -> object:
    """Replace the value of every secret query parameter in ``text`` with ``***``.

    Everything else is left intact. An exception is rendered with ``str()``
    first; any other non-``str`` input (``None`` included) is returned unchanged.
    """
    if isinstance(text, Exception):
        text = str(text)
    if not isinstance(text, str):
        return text
    return _SECRET_PARAM_RE.sub(lambda m: m.group("prefix") + REDACTED, text)
