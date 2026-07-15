"""Platform credential setup helper.

Usage: python -m gamelib_mcp.setup_platform <platform>

Supported platforms:
  gog    — opens GOG OAuth2 flow, writes GOG_REFRESH_TOKEN to .env
  epic   — prints legendary auth instructions
  psn    — prints NPSSO cookie extraction instructions
  switch — prints Nintendo Switch setup instructions
"""

import sys


def _setup_gog() -> None:
    print(
        "GOG auth is handled by the lgogdownloader CLI.\n"
        "Run:  lgogdownloader --login\n"
        "Follow the browser prompts to authenticate.\n"
        "Then mount ~/.config/lgogdownloader/ into Docker (see deploy.md) so the\n"
        "sync container can access the stored session."
    )


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
        "Nintendo Switch setup uses two independent sources:\n"
        "\n"
        "Ownership (VGCS digital library):\n"
        "1. Open https://accounts.nintendo.com/portal/vgcs/ in your browser (stay logged in).\n"
        "2. Install the 'Cookie Editor' browser extension.\n"
        "3. Call create_session_ingest_link(provider='nintendo') and open the link.\n"
        "4. Paste the Cookie Editor JSON export into the form and submit.\n"
        "\n"
        "Playtime (Parental Controls API):\n"
        "1. Call set_nintendo_pctl_session() with no argument → returns a login_url.\n"
        "2. Open the URL, sign in, right-click 'Select this person' → Copy Link.\n"
        "3. Call set_nintendo_pctl_session() again with that npf://auth link.\n"
        "\n"
        'Then run refresh_library(["switch2"]) to sync.'
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
