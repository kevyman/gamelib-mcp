"""Single-use browser links for handing the server a secret outside the chat.

``create_session_ingest_link`` mints a nonce URL (``/ingest/{nonce}``); the
user opens it in a browser, pastes what the provider's steps asked for, and
the POST handler saves it through the matching ``tools.session_admin.set_*_session``
function. The nonce is the only credential: minting already happens behind the
MCP OAuth owner check, the link expires after ``_INGEST_TTL_SECONDS`` and is
consumed on first successful save.

Most providers are a plain cookie paste. ``nintendo_pctl`` is an interactive
login: a ``prepare_name`` hook mints Nintendo's PKCE sign-in URL when the form
first renders, the page offers it as a button, and the user pastes back the
``npf://`` link — which carries a one-time code, hence the same
keep-it-out-of-the-chat treatment as a cookie.

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
from dataclasses import dataclass, field

from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import HTMLResponse


@dataclass(frozen=True)
class IngestProvider:
    key: str
    label: str
    # Attribute on gamelib_mcp.tools.session_admin — dispatching through the existing
    # setter keeps env-var/filename/label knowledge in one place.
    setter_name: str
    export_url: str
    hint: str
    # Numbered, plain-language instructions rendered as the form's <ol>. Written
    # for someone who has never done this before — spell out every click.
    steps: tuple[str, ...]
    # The single cookie the export MUST contain for this provider to work. Shown
    # as a prominent "make sure you see this" callout on the form (and enforced
    # server-side by the matching setter's validator). None = no single gate.
    required_cookie: str | None = None

    # --- interactive-login flows ------------------------------------------
    # Attribute on gamelib_mcp.tools.session_admin: an async () -> dict run ONCE per
    # link, before the form first renders, whose result is stored on the link
    # and handed back to the setter as `state`. Nintendo's Parental Controls
    # login needs it — the URL the user signs in through embeds a PKCE
    # challenge, and only the matching verifier can redeem the code they paste
    # back. None (every cookie provider) = nothing to prepare.
    prepare_name: str | None = None
    # Rendered as a link when prepare_name is set; the prepared state must then
    # carry "login_url".
    login_link_label: str = "Sign in"
    placeholder: str = "Paste the cookie export JSON here"
    submit_label: str = "Save cookies"


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
        steps=(
            (
                "In your browser, open https://accounts.nintendo.com/portal/vgcs/ and "
                "make sure you are signed in to your Nintendo account."
            ),
            (
                "Click the Cookie Editor extension icon (install \"Cookie Editor\" from "
                "your browser's extension store first if you don't have it)."
            ),
            "Click Export (the export/copy icon) to copy all cookies as JSON.",
            "Paste what was copied into the box below and click Save.",
        ),
    ),
    "epic": IngestProvider(
        key="epic",
        label="Epic Games",
        setter_name="set_epic_session",
        export_url="https://www.epicgames.com/account/transactions",
        hint=(
            "These www.epicgames.com cookies drive purchase-history import "
            "(prices). They are separate from the Legendary launcher session "
            "that syncs Epic ownership."
        ),
        steps=(
            (
                "In your browser, open https://www.epicgames.com/account/transactions "
                "and make sure you are signed in to your Epic Games account."
            ),
            (
                "Click the Cookie Editor extension icon (install \"Cookie Editor\" from "
                "your browser's extension store first if you don't have it)."
            ),
            "Click Export (the export/copy icon) to copy all cookies as JSON.",
            "Paste what was copied into the box below and click Save.",
        ),
    ),
    "humble": IngestProvider(
        key="humble",
        label="Humble Bundle",
        setter_name="set_humble_session",
        export_url="https://www.humblebundle.com/",
        hint="Only the _simpleauth_sess cookie is strictly needed.",
        steps=(
            (
                "In your browser, open https://www.humblebundle.com/ and make sure you "
                "are signed in."
            ),
            (
                "Click the Cookie Editor extension icon (install \"Cookie Editor\" first "
                "if needed). The cookie that matters here is called _simpleauth_sess."
            ),
            "Click Export to copy all cookies as JSON.",
            "Paste what was copied into the box below and click Save.",
        ),
    ),
    "steam_refresh": IngestProvider(
        key="steam_refresh",
        label="Steam login (recommended)",
        setter_name="set_steam_refresh_session",
        export_url="https://login.steampowered.com/",
        hint=(
            "This is the recommended way to connect Steam. The token you paste lasts "
            "about 200 days and refreshes itself, so you should not have to redo this "
            "for months."
        ),
        steps=(
            (
                "First, SIGN OUT of Steam in your browser: go to https://store.steampowered.com/, "
                "click your account name at the top right, and choose \"Sign out\". This step "
                "matters — the cookie we need is only created by a fresh sign-in."
            ),
            (
                "Press F12 to open your browser's Developer Tools and click the \"Network\" tab. "
                "Tick the \"Preserve log\" checkbox (so nothing clears when the page redirects), and "
                "in the filter box type: login.steampowered.com . Leave Developer Tools open. "
                "(Don't use the Cookie Editor extension or the Application/Storage tab for this one — "
                "the cookie lives on a page Steam immediately redirects you away from, so they can't "
                "show it.)"
            ),
            (
                "Now go to https://store.steampowered.com/login/ and sign back in. IMPORTANT: "
                "tick the \"Remember me\" checkbox BEFORE you click Sign in."
            ),
            (
                "In the Network list you'll now see requests to login.steampowered.com. Click one of "
                "them (a row marked 302, or one named \"finalizelogin\", both work), then open its "
                "\"Cookies\" sub-tab."
            ),
            (
                "Scroll to find steamRefresh_steam (it appears under \"Request Cookies\" or "
                "\"Response Cookies\"). Right-click its value and choose \"Copy Value\". It's a long "
                "string. (If you don't see it, make sure \"Preserve log\" is ticked and redo the "
                "sign-in with the Network tab already open.)"
            ),
            (
                "Paste that value straight into the box below — just the value on its own, no quotes "
                "and no JSON formatting needed — then click Save."
            ),
        ),
        required_cookie="steamRefresh_steam",
    ),
    "steam_store": IngestProvider(
        key="steam_store",
        label="Steam store cookies (legacy)",
        setter_name="set_steam_store_session",
        export_url="https://store.steampowered.com/account/",
        hint=(
            "Legacy fallback — these cookies expire after about a day and you'll have "
            "to redo this often. Use the \"steam_refresh\" option instead if you can."
        ),
        steps=(
            (
                "In your browser, open https://store.steampowered.com/account/ and make "
                "sure you are signed in. Check the address bar says store.steampowered.com "
                "— NOT steamcommunity.com. Steam uses different cookies for each, and only "
                "the store one works here."
            ),
            (
                "Click the Cookie Editor extension icon (install \"Cookie Editor\" first if "
                "needed). Confirm you can see a cookie named steamLoginSecure in the list."
            ),
            "Click Export to copy all cookies as JSON.",
            "Paste what was copied into the box below and click Save.",
        ),
        required_cookie="steamLoginSecure",
    ),
    "nintendo_pctl": IngestProvider(
        key="nintendo_pctl",
        label="Nintendo Parental Controls",
        setter_name="set_nintendo_pctl_session",
        export_url="https://accounts.nintendo.com/",
        hint=(
            "This is the Switch PLAYTIME source — per-game minutes, including "
            "games played on your console under another account. Ownership is "
            "separate: that's the \"nintendo\" option."
        ),
        steps=(
            (
                "Click the \"Sign in to Nintendo\" button below. It opens Nintendo's "
                "own login page in a new tab."
            ),
            (
                "Sign in with the Nintendo account your console is registered to for "
                "Parental Controls."
            ),
            (
                "You'll land on a \"Select this person\" screen. Do NOT left-click that "
                "button — RIGHT-click it and choose \"Copy link address\" (Chrome/Edge) "
                "or \"Copy Link\" (Firefox/Safari)."
            ),
            (
                "Come back to this tab and paste the copied link — it starts with npf — "
                "into the box below, then click Save."
            ),
        ),
        prepare_name="prepare_nintendo_pctl_login",
        login_link_label="Sign in to Nintendo",
        placeholder="Paste the npf:// link you copied here",
        submit_label="Save session",
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
    # Result of the provider's prepare hook, if it has one: opaque per-link
    # state (the PKCE verifier and the sign-in URL built from it) that the
    # setter needs to complete the flow. Dies with the link, like the nonce.
    state: dict[str, str] = field(default_factory=dict)


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
        raise ToolError(f"Unknown provider '{provider}'. Valid providers: {valid}.")
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


# SECURITY: no response page may ever echo the submitted text back — cookie
# export, refresh token, or npf:// login link alike. Only counts, file paths,
# provider labels, the prepared sign-in URL, and setter ToolError messages
# (which never quote what was submitted) may appear in HTML.

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
    "li{margin:.4rem 0}"
    "code{background:#f2f2f2;padding:.1rem .3rem;border-radius:3px}"
    ".callout{background:#fff8e1;border:1px solid #f0d060;border-radius:6px;"
    "padding:.75rem 1rem;margin:1rem 0}"
    "a.button{display:inline-block;padding:.5rem 1.5rem;background:#e60012;"
    "color:#fff;text-decoration:none;border-radius:4px;font-size:1rem}"
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


def _render_form_page(
    nonce: str,
    spec: IngestProvider,
    error: str | None = None,
    state: dict[str, str] | None = None,
) -> str:
    error_block = f'<p class="error">{html.escape(error)}</p>' if error else ""
    steps = "".join(f"<li>{html.escape(step)}</li>" for step in spec.steps)
    login_url = (state or {}).get("login_url")
    login_block = (
        f'<p><a class="button" href="{html.escape(login_url)}" target="_blank" '
        f'rel="noopener noreferrer">{html.escape(spec.login_link_label)}</a></p>'
        if login_url
        else ""
    )
    if spec.required_cookie:
        callout = (
            '<p class="callout">&#9888;&#65039; <strong>Before you paste:</strong> make sure what '
            f"you copied is the <code>{html.escape(spec.required_cookie)}</code> cookie. If you "
            "can't find that name, re-read the steps above — saving anything else will fail.</p>"
        )
    else:
        callout = ""
    return (
        f"{error_block}"
        f"<p>Connect your <strong>{html.escape(spec.label)}</strong> session. "
        "Follow these steps exactly:</p>"
        f"<ol>{steps}</ol>"
        f"{login_block}"
        f"{callout}"
        f'<p class="note">{html.escape(spec.hint)}</p>'
        f'<form method="post" action="/ingest/{html.escape(nonce)}" autocomplete="off">'
        '<textarea name="payload" autocomplete="off" spellcheck="false" '
        f'placeholder="{html.escape(spec.placeholder)}"></textarea>'
        f'<p><button type="submit">{html.escape(spec.submit_label)}</button></p>'
        "</form>"
        '<p class="note">This link is single-use and expires 15 minutes after it was created. '
        "What you paste is sent only to this server and is never shown back on this page.</p>"
    )


async def _ensure_prepared(link: _IngestLink, spec: IngestProvider) -> None:
    """Run the provider's prepare hook once, on the link's first render.

    Re-preparing on reload would be a bug, not a refresh: the sign-in URL the
    user already opened embeds the challenge for THIS verifier, so replacing it
    would make the code they paste unredeemable.
    """
    if spec.prepare_name is None or link.state:
        return
    from .tools import session_admin

    link.state = await getattr(session_admin, spec.prepare_name)()


async def _handle_get(nonce: str) -> HTMLResponse:
    link = _lookup(nonce)
    if link is None:
        return _not_found_response()
    spec = INGEST_PROVIDERS[link.provider]
    await _ensure_prepared(link, spec)
    return _page(
        f"{spec.label} session", _render_form_page(nonce, spec, state=link.state), 200
    )


async def _handle_post(request: Request, nonce: str) -> HTMLResponse:
    link = _lookup(nonce)
    if link is None:
        return _not_found_response()
    spec = INGEST_PROVIDERS[link.provider]

    content_length = request.headers.get("content-length", "")
    if not content_length.isdigit() or int(content_length) > _MAX_BODY_BYTES:
        return _page(
            "Submission too large",
            "<p>That is far bigger than anything this form expects. "
            "Go back and paste just what the steps asked for.</p>",
            413,
        )

    form = await request.form()
    payload = str(form.get("payload", "")).strip()

    # Re-check after the await: the nonce could have been consumed while the
    # body was being read. From here to the pop everything is synchronous, so
    # no lock is needed in this single-process asyncio server.
    if _lookup(nonce) is None:
        return _not_found_response()

    if not payload:
        return _page(
            f"{spec.label} session",
            _render_form_page(
                nonce, spec, error="Nothing pasted — follow the steps above.",
                state=link.state,
            ),
            400,
        )

    from .tools import session_admin

    setter = getattr(session_admin, spec.setter_name)
    try:
        # A prepare-hook provider's setter needs the state that hook produced;
        # the cookie setters take the paste and nothing else.
        result = await (
            setter(payload, state=link.state) if spec.prepare_name else setter(payload)
        )
    except ToolError as exc:
        # Validation failed — leave the nonce live so the user can fix the
        # paste and resubmit. ToolError text never contains submitted values.
        return _page(
            f"{spec.label} session",
            _render_form_page(nonce, spec, error=str(exc), state=link.state),
            400,
        )

    _ingest_links.pop(nonce, None)
    count = result.get("cookie_count")
    saved = f"saved {count} cookies to" if count is not None else "saved your session to"
    return _page(
        "Session saved",
        f"<p><strong>{html.escape(spec.label)}</strong>: {saved} "
        f"<code>{html.escape(str(result['path']))}</code>.</p>"
        "<p>You can close this page. This link no longer works.</p>",
        200,
    )


async def handle_ingest_request(request: Request) -> HTMLResponse:
    """Route entry point for GET/POST /ingest/{nonce}."""
    nonce = request.path_params["nonce"]
    if request.method == "POST":
        return await _handle_post(request, nonce)
    return await _handle_get(nonce)
