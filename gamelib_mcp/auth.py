"""Authentication configuration for the HTTP MCP server.

FastMCP owns the OAuth 2.1 protocol surface.  This module keeps environment
validation, GitHub-provider construction, and the single-owner authorization
policy out of the tool-registration module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from fastmcp.server.auth import AuthContext, AuthProvider
from fastmcp.server.auth.providers.github import GitHubProvider

AuthMode = Literal["oauth", "disabled"]

# Fixed callback for Claude.ai web/desktop/mobile (https://claude.com/docs/connectors/building/authentication);
# ChatGPT mints a per-connector suffix under its own path, hence the wildcard.
_ALLOWED_CLIENT_REDIRECT_URIS = [
    "https://chatgpt.com/connector/oauth/*",
    "https://claude.ai/api/mcp/auth_callback",
]
_ACCESS_TOKEN_LIFETIME_SECONDS = 60 * 60
_REFRESH_TOKEN_LIFETIME_SECONDS = 30 * 24 * 60 * 60


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
