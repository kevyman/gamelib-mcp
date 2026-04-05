"""Platform credential setup helper.

Usage: python -m gamelib_mcp.setup_platform <platform>

Supported platforms:
  gog    — opens GOG OAuth2 flow, writes GOG_REFRESH_TOKEN to .env
  epic   — prints legendary auth instructions
  psn    — prints NPSSO cookie extraction instructions
  switch — prints nxapi session token instructions
"""

import sys


def _setup_gog() -> None:
    """Run GOG OAuth2 flow and write refresh token to .env."""
    import asyncio
    import os
    import webbrowser

    import aiohttp
    from dotenv import set_key

    CLIENT_ID = "46899977096215655"
    CLIENT_SECRET = "9d85c43b1718a031d5b64228ecd1a9eb"
    AUTH_URL = (
        f"https://auth.gog.com/auth?client_id={CLIENT_ID}"
        "&redirect_uri=https://embed.gog.com/on_login_success?origin=client"
        "&response_type=code&layout=client2"
    )

    print("Opening GOG login page in your browser...")
    webbrowser.open(AUTH_URL)
    code = input(
        "\nAfter logging in, copy the 'code' query parameter from the redirect URL and paste it here:\n> "
    ).strip()

    async def _exchange():
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://auth.gog.com/token",
                params={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "https://embed.gog.com/on_login_success?origin=client",
                },
            ) as resp:
                resp.raise_for_status()
                return (await resp.json())["refresh_token"]

    refresh_token = asyncio.run(_exchange())
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    set_key(env_path, "GOG_REFRESH_TOKEN", refresh_token)
    print("GOG_REFRESH_TOKEN written to .env")


def _setup_epic() -> None:
    print(
        "Epic Games auth is handled by the legendary CLI.\n"
        "Run:  legendary auth\n"
        "Follow the browser prompts, then set EPIC_LEGENDARY_PATH in .env if legendary\n"
        "uses a non-default config directory."
    )


def _setup_psn() -> None:
    print(
        "PSN auth requires a one-time manual step:\n"
        "1. Log in to your PSN account in a browser.\n"
        "2. Visit: https://ca.account.sony.com/api/v1/ssocookie\n"
        "3. Copy the value of the 'npsso' field.\n"
        "4. Add to .env:  PSN_NPSSO=<value>"
    )


def _setup_switch() -> None:
    print(
        "Nintendo Switch auth requires nxapi and a one-time session token:\n"
        "1. Install nxapi: https://github.com/samuelthomas2774/nxapi\n"
        "2. Run: nxapi nso auth\n"
        "3. Follow the prompts to authenticate with your Nintendo account.\n"
        "4. Copy the session token and add to .env:  NINTENDO_SESSION_TOKEN=<value>"
    )


_HANDLERS = {
    "gog": _setup_gog,
    "epic": _setup_epic,
    "psn": _setup_psn,
    "switch": _setup_switch,
}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in _HANDLERS:
        print("Usage: python -m gamelib_mcp.setup_platform <platform>")
        print(f"Platforms: {', '.join(_HANDLERS)}")
        sys.exit(1)
    _HANDLERS[sys.argv[1]]()
