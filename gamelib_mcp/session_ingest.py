"""Single-use browser links for pasting session cookies outside the chat.

``create_session_ingest_link`` mints a nonce URL (``/ingest/{nonce}``); the
user opens it in a browser, pastes their Cookie Editor JSON export into the
form, and the POST handler saves it through the exact same
``tools.admin.set_*_session`` path the chat tools use. The nonce is the only
credential: minting already happens behind the MCP OAuth owner check, the
link expires after ``_INGEST_TTL_SECONDS`` and is consumed on first
successful save.

The nonce store is a module-level dict by design: one deployment is one
owner running a single-process server (docs/adr/0001-single-user.md), so
there is nothing to share. A server restart invalidates all outstanding
links (mint a new one); running multiple workers would give each worker its
own store and break the feature — the deployment runs a single ``mcp.run``
worker.
"""

import hmac
import html
import os
import secrets
import time
from dataclasses import dataclass

from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import HTMLResponse


@dataclass(frozen=True)
class IngestProvider:
    key: str
    label: str
    # Attribute on gamelib_mcp.tools.admin — dispatching through the existing
    # setter keeps env-var/filename/label knowledge in one place.
    setter_name: str
    export_url: str
    hint: str


INGEST_PROVIDERS: dict[str, IngestProvider] = {
    "nintendo": IngestProvider(
        key="nintendo",
        label="Nintendo Account",
        setter_name="set_nintendo_session",
        export_url="https://accounts.nintendo.com/portal/vgcs/",
        hint=(
            "This one accounts.nintendo.com session drives both Switch "
            "ownership sync and eShop purchase import."
        ),
    ),
    "humble": IngestProvider(
        key="humble",
        label="Humble Bundle",
        setter_name="set_humble_session",
        export_url="https://www.humblebundle.com/",
        hint="Only the _simpleauth_sess cookie is strictly needed.",
    ),
    "steam_refresh": IngestProvider(
        key="steam_refresh",
        label="Steam refresh token",
        setter_name="set_steam_refresh_session",
        export_url="https://login.steampowered.com/",
        hint=(
            "After a 'remember me' login, export the steamRefresh_steam cookie "
            "from login.steampowered.com — it mints fresh store cookies on demand "
            "for ~200 days, so no more re-pasting when steamLoginSecure lapses."
        ),
    ),
    "steam_store": IngestProvider(
        key="steam_store",
        label="Steam store (legacy)",
        setter_name="set_steam_store_session",
        export_url="https://store.steampowered.com/account/",
        hint=(
            "Legacy fallback (short-lived): steamLoginSecure is required, "
            "sessionid recommended. Prefer provider=\"steam_refresh\"."
        ),
    ),
}

_INGEST_TTL_SECONDS = 15 * 60
# Single user minting links by hand — more pending links than this means
# something is stuck; evict oldest rather than grow unbounded.
_MAX_PENDING_LINKS = 16
# Cookie exports are a few KB; anything near this cap is not a cookie export.
_MAX_BODY_BYTES = 1_000_000


@dataclass
class _IngestLink:
    provider: str
    expires_at: float  # time.monotonic() deadline


_ingest_links: dict[str, _IngestLink] = {}


def _prune() -> None:
    now = time.monotonic()
    for nonce in [n for n, link in _ingest_links.items() if link.expires_at <= now]:
        del _ingest_links[nonce]


def _lookup(nonce: str) -> _IngestLink | None:
    """Find a live link for ``nonce`` without leaking timing about near-misses.

    Compares against every stored nonce with ``hmac.compare_digest`` and no
    early exit (the store holds ≤ _MAX_PENDING_LINKS entries, so the full scan
    costs nothing).
    """
    _prune()
    found: _IngestLink | None = None
    candidate = nonce.encode()
    for stored, link in _ingest_links.items():
        if hmac.compare_digest(stored.encode(), candidate):
            found = link
    return found


def _ingest_base_url() -> str:
    # Matches SecurityConfig's normalization of MCP_PUBLIC_BASE_URL (required
    # in oauth mode); read from the env rather than main to avoid an import
    # cycle. In local disabled-auth mode the var is typically unset — fall
    # back to the server's own listen address.
    base = os.getenv("MCP_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    return f"http://localhost:{os.getenv('PORT', '8000')}"


def mint_ingest_link(provider: str) -> dict:
    """Mint a single-use ``/ingest/{nonce}`` URL for the given provider."""
    spec = INGEST_PROVIDERS.get(provider)
    if spec is None:
        valid = ", ".join(sorted(INGEST_PROVIDERS))
        raise ToolError(
            f"Unknown provider '{provider}'. Valid providers: {valid}. "
            "(Parental Controls playtime is not cookie-based — use "
            "set_nintendo_pctl_session.)"
        )
    _prune()
    while len(_ingest_links) >= _MAX_PENDING_LINKS:
        oldest = min(_ingest_links, key=lambda n: _ingest_links[n].expires_at)
        del _ingest_links[oldest]
    nonce = secrets.token_urlsafe(32)
    _ingest_links[nonce] = _IngestLink(
        provider=provider, expires_at=time.monotonic() + _INGEST_TTL_SECONDS
    )
    return {
        "url": f"{_ingest_base_url()}/ingest/{nonce}",
        "provider": provider,
        "expires_in_minutes": _INGEST_TTL_SECONDS // 60,
    }


# SECURITY: no response page may ever echo the submitted cookie text back.
# Only cookie counts, file paths, provider labels, and _save_session_cookies
# ToolError messages (which never contain cookie values) may appear in HTML.

# referrer-policy is "same-origin", NOT "no-referrer": the paste form POSTs back
# to this same server, and the HttpSecurityMiddleware Origin allowlist gates that
# POST. Under "no-referrer" the browser sends `Origin: null` on a form navigation
# (Fetch spec), which isn't allowlisted, so the submission would be rejected with
# "Forbidden". "same-origin" makes the browser send the page's real Origin (which
# oauth mode auto-allowlists from MCP_PUBLIC_BASE_URL) while still never leaking
# the nonce-bearing URL as a Referer to any cross-origin destination.
_INGEST_HEADERS = {
    "cache-control": "no-store",
    "referrer-policy": "same-origin",
    "x-content-type-options": "nosniff",
    "content-security-policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
    ),
}

_PAGE_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;"
    "padding:0 1rem;line-height:1.5}"
    "textarea{width:100%;min-height:12rem;font-family:monospace}"
    "button{padding:.5rem 1.5rem;font-size:1rem}"
    ".error{color:#b00020}"
    ".note{color:#555;font-size:.9rem}"
)


def _page(title: str, body: str, status_code: int) -> HTMLResponse:
    document = (
        "<!doctype html>"
        f"<html><head><title>{html.escape(title)}</title>"
        f"<style>{_PAGE_STYLE}</style></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(document, status_code=status_code, headers=_INGEST_HEADERS)


def _not_found_response() -> HTMLResponse:
    # Identical body for unknown, expired, and already-used nonces — the page
    # must not reveal whether a nonce ever existed.
    return _page(
        "Link invalid or expired",
        "<p>This link is invalid or has expired. "
        "Ask your assistant for a new one.</p>",
        404,
    )


def _render_form_page(nonce: str, spec: IngestProvider, error: str | None = None) -> str:
    error_block = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return (
        f"{error_block}"
        f"<p>Store your <strong>{html.escape(spec.label)}</strong> session cookies.</p>"
        "<ol>"
        f"<li>Open <code>{html.escape(spec.export_url)}</code> in your browser, logged in.</li>"
        '<li>Use the "Cookie Editor" extension: click its icon &rarr; Export &rarr; copy the JSON.</li>'
        "<li>Paste the JSON below and submit.</li>"
        "</ol>"
        f'<p class="note">{html.escape(spec.hint)}</p>'
        f'<form method="post" action="/ingest/{html.escape(nonce)}" autocomplete="off">'
        '<textarea name="cookies" autocomplete="off" spellcheck="false" '
        'placeholder="Paste the cookie export JSON here"></textarea>'
        "<p><button type=\"submit\">Save cookies</button></p>"
        "</form>"
        '<p class="note">This link is single-use and expires 15 minutes after it was created. '
        "Cookies are sent only to this server and are never shown back on this page.</p>"
    )


def _handle_get(nonce: str) -> HTMLResponse:
    link = _lookup(nonce)
    if link is None:
        return _not_found_response()
    spec = INGEST_PROVIDERS[link.provider]
    return _page(f"{spec.label} session", _render_form_page(nonce, spec), 200)


async def _handle_post(request: Request, nonce: str) -> HTMLResponse:
    link = _lookup(nonce)
    if link is None:
        return _not_found_response()
    spec = INGEST_PROVIDERS[link.provider]

    content_length = request.headers.get("content-length", "")
    if not content_length.isdigit() or int(content_length) > _MAX_BODY_BYTES:
        return _page(
            "Submission too large",
            "<p>That doesn't look like a cookie export. "
            "Go back and paste the JSON from Cookie Editor.</p>",
            413,
        )

    form = await request.form()
    cookies = str(form.get("cookies", "")).strip()

    # Re-check after the await: the nonce could have been consumed while the
    # body was being read. From here to the pop everything is synchronous, so
    # no lock is needed in this single-process asyncio server.
    if _lookup(nonce) is None:
        return _not_found_response()

    if not cookies:
        return _page(
            f"{spec.label} session",
            _render_form_page(nonce, spec, error="Paste your cookie export JSON."),
            400,
        )

    from .tools import admin as admin_tools

    try:
        result = await getattr(admin_tools, spec.setter_name)(cookies)
    except ToolError as exc:
        # Validation failed — leave the nonce live so the user can fix the
        # paste and resubmit. ToolError text never contains cookie values.
        return _page(
            f"{spec.label} session",
            _render_form_page(nonce, spec, error=str(exc)),
            400,
        )

    _ingest_links.pop(nonce, None)
    return _page(
        "Cookies saved",
        f"<p><strong>{html.escape(spec.label)}</strong>: saved "
        f"{result['cookie_count']} cookies to "
        f"<code>{html.escape(str(result['path']))}</code>.</p>"
        "<p>You can close this page. This link no longer works.</p>",
        200,
    )


async def handle_ingest_request(request: Request) -> HTMLResponse:
    """Route entry point for GET/POST /ingest/{nonce}."""
    nonce = request.path_params["nonce"]
    if request.method == "POST":
        return await _handle_post(request, nonce)
    return _handle_get(nonce)
