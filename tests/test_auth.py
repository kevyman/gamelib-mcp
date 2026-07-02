import asyncio
import json
import os
import subprocess
import sys
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, AuthContext
from key_value.aio.stores.memory import MemoryStore

from gamelib_mcp.auth import load_security_config


def _oauth_environment() -> dict[str, str]:
    return {
        "MCP_AUTH_MODE": "oauth",
        "MCP_ADMIN_AUTH_TOKEN": "a" * 32,
        "MCP_PUBLIC_BASE_URL": "https://gamelibmcp.johnwilkos.com",
        "GITHUB_OAUTH_CLIENT_ID": "github-client-id",
        "GITHUB_OAUTH_CLIENT_SECRET": "github-client-secret",
        "MCP_OAUTH_JWT_SIGNING_KEY": "j" * 32,
        "MCP_OAUTH_GITHUB_USER_ID": "12233501",
        "FASTMCP_HOME": "/data/fastmcp",
        "MCP_ALLOWED_ORIGINS": "https://chatgpt.com",
    }


async def _invoke_asgi(
    app,
    path: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    events: list[dict] = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        events.append(message)

    await app(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query_string,
            "headers": headers or [],
            "server": ("gamelibmcp.johnwilkos.com", 443),
            "client": ("127.0.0.1", 12345),
        },
        receive,
        send,
    )

    start = next(event for event in events if event["type"] == "http.response.start")
    response_body = b"".join(
        event.get("body", b"")
        for event in events
        if event["type"] == "http.response.body"
    )
    response_headers = {
        key.decode().lower(): value.decode() for key, value in start.get("headers", [])
    }
    return start["status"], response_headers, response_body


def _oauth_app():
    config = load_security_config(_oauth_environment())
    provider = config.build_auth_provider(client_storage=MemoryStore())
    return FastMCP("auth-test", auth=provider).http_app()


def _route(app, path: str):
    return next(route for route in app.routes if route.path == path)


def test_auth_mode_must_be_explicit():
    with pytest.raises(RuntimeError, match="MCP_AUTH_MODE"):
        load_security_config({"MCP_ADMIN_AUTH_TOKEN": "a" * 32})


@pytest.mark.parametrize(
    "missing_name",
    [
        "MCP_ADMIN_AUTH_TOKEN",
        "MCP_PUBLIC_BASE_URL",
        "GITHUB_OAUTH_CLIENT_ID",
        "GITHUB_OAUTH_CLIENT_SECRET",
        "MCP_OAUTH_JWT_SIGNING_KEY",
        "MCP_OAUTH_GITHUB_USER_ID",
        "FASTMCP_HOME",
    ],
)
def test_oauth_configuration_fails_closed_when_required_value_is_missing(
    missing_name: str,
):
    environ = _oauth_environment()
    del environ[missing_name]

    with pytest.raises(RuntimeError, match=missing_name):
        load_security_config(environ)


def test_public_origin_is_automatically_allowed():
    config = load_security_config(_oauth_environment())

    assert config.allowed_origins == frozenset(
        {
            "https://chatgpt.com",
            "https://gamelibmcp.johnwilkos.com",
        }
    )


def test_owner_authorization_accepts_only_immutable_github_user_id():
    config = load_security_config(_oauth_environment())
    check = config.owner_authorization_check()

    owner = AccessToken(
        token="owner-token",
        client_id="client",
        scopes=["read:user"],
        claims={"sub": "12233501", "login": "kevyman"},
    )
    other_user = AccessToken(
        token="other-token",
        client_id="client",
        scopes=["read:user"],
        claims={"sub": "999", "login": "someone-else"},
    )

    assert check(AuthContext(token=owner, component=Mock())) is True
    assert check(AuthContext(token=other_user, component=Mock())) is False
    assert check(AuthContext(token=None, component=Mock())) is False


def test_oauth_metadata_and_unauthenticated_challenge_are_mcp_compliant():
    app = _oauth_app()

    resource_status, _, resource_body = asyncio.run(
        _invoke_asgi(
            _route(app, "/.well-known/oauth-protected-resource/mcp").endpoint,
            "/.well-known/oauth-protected-resource/mcp",
        )
    )
    issuer_status, _, issuer_body = asyncio.run(
        _invoke_asgi(
            _route(app, "/.well-known/oauth-authorization-server").endpoint,
            "/.well-known/oauth-authorization-server",
        )
    )
    mcp_status, mcp_headers, _ = asyncio.run(
        _invoke_asgi(_route(app, "/mcp").endpoint, "/mcp")
    )

    resource_metadata = json.loads(resource_body)
    issuer_metadata = json.loads(issuer_body)
    assert resource_status == 200
    assert resource_metadata["resource"] == "https://gamelibmcp.johnwilkos.com/mcp"
    assert resource_metadata["authorization_servers"] == [
        "https://gamelibmcp.johnwilkos.com/"
    ]
    assert resource_metadata["scopes_supported"] == ["read:user"]
    assert issuer_status == 200
    assert issuer_metadata["registration_endpoint"] == (
        "https://gamelibmcp.johnwilkos.com/register"
    )
    assert issuer_metadata["code_challenge_methods_supported"] == ["S256"]
    assert issuer_metadata["scopes_supported"] == ["read:user"]
    assert app.state.fastmcp_server.auth._jwt_issuer.audience == (
        "https://gamelibmcp.johnwilkos.com/mcp"
    )
    assert mcp_status == 401
    assert "resource_metadata=" in mcp_headers["www-authenticate"]
    assert (
        "/.well-known/oauth-protected-resource/mcp" in mcp_headers["www-authenticate"]
    )


@pytest.mark.parametrize(
    ("redirect_uri", "expected_status"),
    [
        ("https://chatgpt.com/connector/oauth/callback-id", 302),
        ("https://evil.example/oauth/callback", 400),
    ],
)
def test_dynamic_registration_restricts_client_redirects(
    redirect_uri: str,
    expected_status: int,
):
    app = _oauth_app()
    payload = json.dumps(
        {
            "client_name": "ChatGPT",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
    ).encode()

    async def register_and_authorize() -> int:
        registration_status, _, registration_body = await _invoke_asgi(
            app,
            "/register",
            method="POST",
            body=payload,
            headers=[(b"content-type", b"application/json")],
        )
        assert registration_status == 201
        client_id = json.loads(registration_body)["client_id"]
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": "read:user",
                "state": "test-state",
                "code_challenge": "a" * 43,
                "code_challenge_method": "S256",
                "resource": "https://gamelibmcp.johnwilkos.com/mcp",
            }
        ).encode()
        authorization_status, _, _ = await _invoke_asgi(
            app,
            "/authorize",
            query_string=query,
        )
        return authorization_status

    assert asyncio.run(register_and_authorize()) == expected_status


def test_main_wires_oauth_provider_into_production_http_app(tmp_path):
    environ = os.environ.copy()
    environ.update(_oauth_environment())
    environ["FASTMCP_HOME"] = str(tmp_path / "fastmcp")
    environ["DATABASE_URL"] = f"file:{tmp_path / 'oauth-smoke.sqlite'}"
    script = """
import json
from gamelib_mcp.main import mcp

app = mcp.http_app()
print(json.dumps(sorted(route.path for route in app.routes)))
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=environ,
    )
    paths = json.loads(result.stdout)

    assert "/mcp" in paths
    assert "/register" in paths
    assert "/authorize" in paths
    assert "/token" in paths
    assert "/.well-known/oauth-protected-resource/mcp" in paths
    assert "/.well-known/oauth-authorization-server" in paths
