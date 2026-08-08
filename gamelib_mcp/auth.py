"""Authentication configuration for the HTTP MCP server.

FastMCP owns the OAuth 2.1 protocol surface.  This module keeps environment
validation, GitHub-provider construction, and the single-owner authorization
policy out of the tool-registration module.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from fastmcp.server.auth import AuthContext, AuthProvider
from fastmcp.server.auth.auth import PrivateKeyJWTClientAuthenticator
from fastmcp.server.auth.providers.github import GitHubProvider

AuthMode = Literal["oauth", "disabled"]

# Fixed callback for Claude.ai web/desktop/mobile (https://claude.com/docs/connectors/building/authentication);
# ChatGPT mints a per-connector suffix under its own path, hence the wildcard.
_ALLOWED_CLIENT_REDIRECT_URIS = [
    "https://chatgpt.com/connector/oauth/*",
    "https://claude.ai/api/mcp/auth_callback",
]
# GitHub OAuth Apps issue non-expiring, API-key-style user tokens and return no
# refresh_token, so FastMCP has none to hand the client: an expired access token
# can only be replaced by a full interactive re-auth.  Keep the access token's
# lifetime long enough that this stays rare.
_ACCESS_TOKEN_LIFETIME_SECONDS = 30 * 24 * 60 * 60
# Only consulted if the upstream provider returns a refresh_token — inert under an
# OAuth App, live if this is ever migrated to a GitHub App with expiring tokens.
_REFRESH_TOKEN_LIFETIME_SECONDS = 30 * 24 * 60 * 60


def _normalize_token_audience(url: str) -> str:
    """Collapse duplicate slashes in the path of a token-endpoint URL."""

    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    return scheme + sep + re.sub(r"/{2,}", "/", rest)


def _patch_cimd_token_audience() -> None:
    """Normalize the private_key_jwt audience FastMCP expects from CIMD clients.

    FastMCP (≤3.4.6, CIMD is beta) builds it as ``f"{self.base_url}/token"``
    (oauth_proxy/proxy.py), but ``base_url`` is a pydantic AnyHttpUrl that
    stringifies a bare origin with a trailing slash, so the expected audience
    becomes ``https://host//token``. ChatGPT signs its client assertion with the
    single-slash token endpoint advertised in the discovery metadata, so every
    private_key_jwt token exchange 401s (claude.ai is unaffected — its CIMD
    document uses ``token_endpoint_auth_method: none``). Patching the class
    attribute reaches FastMCP's own call site; a fixed upstream URL passes
    through unchanged, at which point this shim can be deleted.
    """

    original = PrivateKeyJWTClientAuthenticator.__init__
    if getattr(original, "_gamelib_audience_fix", False):
        return

    @functools.wraps(original)
    def patched_init(
        self: PrivateKeyJWTClientAuthenticator,
        provider: Any,
        cimd_manager: Any,
        token_endpoint_url: str,
    ) -> None:
        original(
            self,
            provider,
            cimd_manager,
            _normalize_token_audience(token_endpoint_url),
        )

    patched_init._gamelib_audience_fix = True  # type: ignore[attr-defined]
    PrivateKeyJWTClientAuthenticator.__init__ = patched_init  # type: ignore[method-assign]


@dataclass(frozen=True)
class OAuthSecurityConfig:
    """OAuth settings that only exist together — never partially populated."""

    public_base_url: str
    github_client_id: str
    github_client_secret: str = field(repr=False)
    oauth_jwt_signing_key: str = field(repr=False)
    github_user_ids: frozenset[str]


@dataclass(frozen=True)
class SecurityConfig:
    """Validated process-lifetime security configuration."""

    auth_mode: AuthMode
    admin_token: str = field(repr=False)
    allowed_origins: frozenset[str]
    oauth: OAuthSecurityConfig | None = None

    def build_auth_provider(self, *, client_storage: Any = None) -> AuthProvider | None:
        """Build the FastMCP OAuth provider, or return None for explicit local mode."""

        if self.oauth is None:
            return None

        _patch_cimd_token_audience()
        return GitHubProvider(
            client_id=self.oauth.github_client_id,
            client_secret=self.oauth.github_client_secret,
            base_url=self.oauth.public_base_url,
            required_scopes=["read:user"],
            allowed_client_redirect_uris=_ALLOWED_CLIENT_REDIRECT_URIS,
            client_storage=client_storage,
            jwt_signing_key=self.oauth.oauth_jwt_signing_key,
            require_authorization_consent=True,
            cache_ttl_seconds=60,
            max_cache_size=32,
            fastmcp_access_token_expiry_seconds=_ACCESS_TOKEN_LIFETIME_SECONDS,
            fallback_refresh_token_expiry_seconds=_REFRESH_TOKEN_LIFETIME_SECONDS,
        )

    def owner_authorization_check(self):
        """Return a FastMCP auth check restricted to the configured GitHub IDs."""

        if self.oauth is None:
            raise RuntimeError("Owner authorization requires OAuth mode")
        allowed_user_ids = self.oauth.github_user_ids

        def is_configured_owner(context: AuthContext) -> bool:
            if context.token is None:
                return False
            subject = str(context.token.claims.get("sub", ""))
            return subject in allowed_user_ids

        return is_configured_owner


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _secret(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name)
    if len(value) < 32:
        raise RuntimeError(f"{name} must contain at least 32 characters")
    return value


def _public_base_url(value: str) -> tuple[str, str]:
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path:
        raise RuntimeError("MCP_PUBLIC_BASE_URL must be an HTTPS origin without a path")
    return normalized, f"{parsed.scheme}://{parsed.netloc}"


def _github_user_ids(environ: Mapping[str, str]) -> frozenset[str]:
    raw = _required(environ, "MCP_OAUTH_GITHUB_USER_IDS")
    user_ids = {value.strip() for value in raw.split(",") if value.strip()}
    for user_id in user_ids:
        if not user_id.isdecimal() or int(user_id) <= 0:
            raise RuntimeError(
                "MCP_OAUTH_GITHUB_USER_IDS must be a comma-separated list of positive numeric GitHub IDs"
            )
    return frozenset(user_ids)


def load_security_config(environ: Mapping[str, str] | None = None) -> SecurityConfig:
    """Load security settings, rejecting missing or ambiguous auth configuration."""

    values = os.environ if environ is None else environ
    mode = values.get("MCP_AUTH_MODE", "").strip().lower()
    if mode not in {"oauth", "disabled"}:
        raise RuntimeError(
            "MCP_AUTH_MODE must be explicitly set to 'oauth' or 'disabled'"
        )

    admin_token = _secret(values, "MCP_ADMIN_AUTH_TOKEN")
    allowed_origins = {
        origin.strip().rstrip("/")
        for origin in values.get("MCP_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    }

    if mode == "disabled":
        return SecurityConfig(
            auth_mode="disabled",
            admin_token=admin_token,
            allowed_origins=frozenset(allowed_origins),
        )

    public_base_url, public_origin = _public_base_url(
        _required(values, "MCP_PUBLIC_BASE_URL")
    )
    allowed_origins.add(public_origin)

    _required(values, "FASTMCP_HOME")

    return SecurityConfig(
        auth_mode="oauth",
        admin_token=admin_token,
        allowed_origins=frozenset(allowed_origins),
        oauth=OAuthSecurityConfig(
            public_base_url=public_base_url,
            github_client_id=_required(values, "GITHUB_OAUTH_CLIENT_ID"),
            github_client_secret=_required(values, "GITHUB_OAUTH_CLIENT_SECRET"),
            oauth_jwt_signing_key=_secret(values, "MCP_OAUTH_JWT_SIGNING_KEY"),
            github_user_ids=_github_user_ids(values),
        ),
    )
