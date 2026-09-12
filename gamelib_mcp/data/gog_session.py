"""Shared lgogdownloader session loading for GOG's JSON endpoints.

Both GOG callers — the library listing (``data/gog.py``) and the order-history
importer (``data/purchases/gog_orders.py``) — talk to ``embed.gog.com`` as the
logged-in account and reuse the one session lgogdownloader already stores in
its config dir: a ``galaxy_tokens.json`` access token (sent as a Bearer header)
preferred, a Netscape-format ``cookies.txt`` jar for gog.com domains as the
fallback. Neither present → the same "run lgogdownloader --login" advice.

The config dir itself is ``data/gog.py``'s ``_config_dir()``; it is imported
lazily inside each function so this module stays importable from ``gog.py``
without a cycle.
"""

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TOKENS_FILENAME = "galaxy_tokens.json"
_COOKIE_JAR_FILENAME = "cookies.txt"

LOGIN_ADVICE = "run lgogdownloader --login"

_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"


def browser_headers() -> dict[str, str]:
    """Headers every GOG account request sends (JSON, browser-ish UA)."""
    return {"User-Agent": _USER_AGENT, "Accept": "application/json"}


def missing_session_error() -> RuntimeError:
    # Deliberately matches data/gog.py's unconfigured phrasing.
    from gamelib_mcp.data.gog import _config_dir

    return RuntimeError(
        f"lgogdownloader session files missing in {_config_dir()}; {LOGIN_ADVICE}"
    )


def stale_session_message(what: str) -> str:
    """The "session expired" message for a named GOG request kind."""
    return (
        f"GOG {what} request was not authenticated (lgogdownloader session "
        f"expired) — {LOGIN_ADVICE} again to refresh it."
    )


def load_access_token() -> str | None:
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


def load_cookie_jar() -> dict[str, str] | None:
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


def looks_like_login_html(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return "text/html" in content_type or response.text.lstrip()[:1] == "<"


async def authenticated_get_json(
    client: httpx.AsyncClient, url: str, params: dict[str, Any]
) -> dict | None:
    """GET a GOG JSON endpoint as the logged-in account.

    Bearer token first, cookie jar on a 401/403 or an HTML login bounce.
    Returns the decoded object, or None when neither credential authenticates
    (including when neither is stored) — callers decide which error that is.
    Any other transport/HTTP error propagates, and a non-object payload raises.
    """
    headers = browser_headers()
    token = load_access_token()
    if token is not None:
        payload = await _get_json_once(
            client, url, params, {**headers, "Authorization": f"Bearer {token}"}
        )
        if payload is not None:
            return payload

    cookies = load_cookie_jar()
    if cookies is None:
        return None
    if token is not None:
        logger.info("GOG bearer token rejected — retrying with cookie jar")
    client.cookies.update(cookies)
    return await _get_json_once(client, url, params, headers)


async def _get_json_once(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    headers: dict[str, str],
) -> dict | None:
    resp = await client.get(url, params=params, headers=headers)
    if resp.status_code in (401, 403):
        return None
    resp.raise_for_status()
    if looks_like_login_html(resp):
        # An expired session bounces to an HTML login page.
        return None
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected GOG payload: {type(data).__name__}")
    return data
