from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TypedDict

from .status import CapabilityStatus, CheckStatus, IntegrationStatus


class LastSyncMeta(TypedDict, total=False):
    last_attempt_at: str
    last_error_classification: str
    last_error_summary: str
    last_success_at: str
    last_finished_at: str


def inspect_all_integrations(
    last_sync_by_platform: dict[str, LastSyncMeta] | None = None,
) -> dict[str, IntegrationStatus]:
    last_sync_by_platform = last_sync_by_platform or {}
    return {
        "steam": _safe_inspect("steam", inspect_steam, last_sync_by_platform.get("steam")),
        "epic": _safe_inspect("epic", inspect_epic, last_sync_by_platform.get("epic")),
        "gog": _safe_inspect("gog", inspect_gog, last_sync_by_platform.get("gog")),
        "nintendo": _safe_inspect("nintendo", inspect_nintendo, last_sync_by_platform.get("nintendo")),
        "ps5": _safe_inspect("ps5", inspect_psn, last_sync_by_platform.get("ps5")),
    }


def inspect_all_integrations_dict(
    last_sync_by_platform: dict[str, LastSyncMeta] | None = None,
) -> dict[str, dict[str, object]]:
    return {
        platform: status.to_dict()
        for platform, status in inspect_all_integrations(last_sync_by_platform).items()
    }


def inspect_steam(last_sync: LastSyncMeta | None = None) -> IntegrationStatus:
    api_key = bool(os.getenv("STEAM_API_KEY"))
    steam_id = bool(os.getenv("STEAM_ID"))
    detected_inputs = _detected_env_inputs(
        ("STEAM_API_KEY", api_key),
        ("STEAM_ID", steam_id),
    )

    if api_key and steam_id:
        return IntegrationStatus(
            platform="steam",
            overall_status="ready",
            active_backend="steam-web-api",
            summary="Steam Web API credentials are configured.",
            capabilities=[
                CapabilityStatus("ownership", "ready", "Owned games can be fetched from Steam."),
                CapabilityStatus("playtime", "ready", "Playtime is available from Steam."),
            ],
            checks=[
                CheckStatus("steam_api_key", "pass", "STEAM_API_KEY is set"),
                CheckStatus("steam_id", "pass", "STEAM_ID is set"),
            ],
            required_inputs=["STEAM_API_KEY", "STEAM_ID"],
            detected_inputs=detected_inputs,
            remediation_steps=[],
            last_sync=last_sync or {},
        )

    missing = []
    if not api_key:
        missing.append("STEAM_API_KEY")
    if not steam_id:
        missing.append("STEAM_ID")

    overall_status = "partially_configured" if detected_inputs else "unconfigured"
    summary = (
        "Steam is partially configured; set the remaining credential."
        if detected_inputs
        else "Steam is not configured."
    )

    return IntegrationStatus(
        platform="steam",
        overall_status=overall_status,
        active_backend="steam-web-api" if detected_inputs else None,
        summary=summary,
        capabilities=[
            CapabilityStatus("ownership", overall_status, "Steam credentials are incomplete."),
            CapabilityStatus("playtime", overall_status, "Steam credentials are incomplete."),
        ],
        checks=[
            CheckStatus("steam_api_key", "pass" if api_key else "fail", _env_check_summary("STEAM_API_KEY", api_key)),
            CheckStatus("steam_id", "pass" if steam_id else "fail", _env_check_summary("STEAM_ID", steam_id)),
        ],
        required_inputs=["STEAM_API_KEY", "STEAM_ID"],
        detected_inputs=detected_inputs,
        remediation_steps=[f"Set `{name}`." for name in missing],
        last_sync=last_sync or {},
    )


def inspect_epic(last_sync: LastSyncMeta | None = None) -> IntegrationStatus:
    root = _epic_root()
    user_path = root / "user.json"
    metadata_path = root / "metadata"
    has_user = user_path.is_file()
    has_metadata = metadata_path.is_dir() and any(metadata_path.glob("*.json"))
    playtime_stale = (last_sync or {}).get("last_error_classification") == "auth_stale"

    if has_user and has_metadata and playtime_stale:
        return IntegrationStatus(
            platform="epic",
            overall_status="degraded",
            active_backend="legendary-cache",
            summary="Ownership is ready but playtime auth is stale.",
            capabilities=[
                CapabilityStatus("ownership", "ready", "Metadata cache present"),
                CapabilityStatus("playtime", "stale", "Refresh token rejected or expired"),
            ],
            checks=[
                CheckStatus("legendary_user_json", "pass", "user.json found"),
                CheckStatus("legendary_metadata", "pass", "metadata cache found"),
                CheckStatus("epic_playtime_token", "warn", "Playtime auth is stale"),
            ],
            required_inputs=["EPIC_LEGENDARY_PATH or /legendary mount"],
            detected_inputs=[str(user_path), str(metadata_path)],
            remediation_steps=[
                "Run `legendary auth` on the host.",
                "Run `legendary list --force-refresh` on the host.",
                "Confirm the Legendary path is mounted read-only into the container.",
            ],
            last_sync=last_sync or {},
        )

    if has_user and has_metadata:
        return IntegrationStatus(
            platform="epic",
            overall_status="ready",
            active_backend="legendary-cache",
            summary="Legendary credentials and metadata cache are present.",
            capabilities=[
                CapabilityStatus("ownership", "ready", "Metadata cache present"),
                CapabilityStatus("playtime", "ready", "Playtime can be refreshed with the cached auth state."),
            ],
            checks=[
                CheckStatus("legendary_user_json", "pass", "user.json found"),
                CheckStatus("legendary_metadata", "pass", "metadata cache found"),
            ],
            required_inputs=["EPIC_LEGENDARY_PATH or /legendary mount"],
            detected_inputs=[str(user_path), str(metadata_path)],
            remediation_steps=[],
            last_sync=last_sync or {},
        )

    if has_user or has_metadata:
        return IntegrationStatus(
            platform="epic",
            overall_status="partially_configured",
            active_backend="legendary-cache",
            summary="Legendary state is incomplete; both auth and metadata are required.",
            capabilities=[
                CapabilityStatus("ownership", "partially_configured", "Legendary metadata cache is incomplete."),
                CapabilityStatus("playtime", "partially_configured", "Legendary auth state is incomplete."),
            ],
            checks=[
                CheckStatus("legendary_user_json", "pass" if has_user else "fail", "user.json found" if has_user else "user.json missing"),
                CheckStatus("legendary_metadata", "pass" if has_metadata else "fail", "metadata cache found" if has_metadata else "metadata cache missing"),
            ],
            required_inputs=["EPIC_LEGENDARY_PATH or /legendary mount"],
            detected_inputs=_existing_paths(user_path, metadata_path),
            remediation_steps=[
                "Mount the full Legendary config directory read-only into the container.",
                "Run `legendary auth` and `legendary list --force-refresh` on the host.",
            ],
            last_sync=last_sync or {},
        )

    return IntegrationStatus(
        platform="epic",
        overall_status="unconfigured",
        active_backend=None,
        summary="Legendary credentials and metadata cache were not detected.",
        capabilities=[
            CapabilityStatus("ownership", "unconfigured", "Legendary metadata cache is not mounted."),
            CapabilityStatus("playtime", "unconfigured", "Legendary auth state is not mounted."),
        ],
        checks=[
            CheckStatus("legendary_user_json", "fail", "user.json missing"),
            CheckStatus("legendary_metadata", "fail", "metadata cache missing"),
        ],
        required_inputs=["EPIC_LEGENDARY_PATH or /legendary mount"],
        detected_inputs=[],
        remediation_steps=[
            "Mount the Legendary config directory read-only into the container.",
            "Run `legendary auth` and `legendary list --force-refresh` on the host.",
        ],
        last_sync=last_sync or {},
    )


def inspect_gog(last_sync: LastSyncMeta | None = None) -> IntegrationStatus:
    root = _gog_root()
    binary = shutil.which("lgogdownloader")
    has_mount = root.exists()
    auth_stale = (last_sync or {}).get("last_error_classification") == "auth_stale"

    if has_mount and binary is None:
        return IntegrationStatus(
            platform="gog",
            overall_status="degraded",
            active_backend="lgogdownloader",
            summary="GOG session files are present but the lgogdownloader binary is missing in the container.",
            capabilities=[CapabilityStatus("ownership", "degraded", "Runtime dependency missing")],
            checks=[CheckStatus("lgogdownloader_binary", "fail", "lgogdownloader not found in PATH")],
            required_inputs=["lgogdownloader binary", "LGOGDOWNLOADER_CONFIG_PATH mount"],
            detected_inputs=[str(root)],
            remediation_steps=[
                "Install `lgogdownloader` in the container image.",
                "Keep the GOG config directory mounted read-only into the container.",
            ],
            last_sync=last_sync or {},
        )

    if has_mount and binary is not None and auth_stale:
        return IntegrationStatus(
            platform="gog",
            overall_status="stale",
            active_backend="lgogdownloader",
            summary="GOG session auth appears stale and needs to be refreshed.",
            capabilities=[CapabilityStatus("ownership", "stale", "GOG auth must be refreshed before ownership can be listed reliably")],
            checks=[
                CheckStatus("lgogdownloader_binary", "pass", "lgogdownloader found in PATH"),
                CheckStatus("lgogdownloader_config", "pass", "Config directory found"),
                CheckStatus("gog_session_auth", "warn", "Recent GOG auth failed and the session should be refreshed"),
            ],
            required_inputs=["lgogdownloader binary", "LGOGDOWNLOADER_CONFIG_PATH mount"],
            detected_inputs=[binary, str(root)],
            remediation_steps=[
                "Run `lgogdownloader --login` on the host to refresh the session.",
                "Keep the GOG config directory mounted read-only into the container.",
            ],
            last_sync=last_sync or {},
        )

    if has_mount and binary is not None:
        return IntegrationStatus(
            platform="gog",
            overall_status="ready",
            active_backend="lgogdownloader",
            summary="lgogdownloader and its config directory are available.",
            capabilities=[CapabilityStatus("ownership", "ready", "GOG ownership can be listed locally")],
            checks=[
                CheckStatus("lgogdownloader_binary", "pass", "lgogdownloader found in PATH"),
                CheckStatus("lgogdownloader_config", "pass", "Config directory found"),
            ],
            required_inputs=["lgogdownloader binary", "LGOGDOWNLOADER_CONFIG_PATH mount"],
            detected_inputs=[binary, str(root)],
            remediation_steps=[],
            last_sync=last_sync or {},
        )

    if binary is not None or has_mount:
        return IntegrationStatus(
            platform="gog",
            overall_status="partially_configured",
            active_backend="lgogdownloader",
            summary="GOG requires both the lgogdownloader binary and a mounted config directory.",
            capabilities=[CapabilityStatus("ownership", "partially_configured", "GOG setup is incomplete")],
            checks=[
                CheckStatus(
                    "lgogdownloader_binary",
                    "pass" if binary is not None else "fail",
                    "lgogdownloader found in PATH" if binary is not None else "lgogdownloader not found in PATH",
                ),
                CheckStatus(
                    "lgogdownloader_config",
                    "pass" if has_mount else "fail",
                    "Config directory found" if has_mount else "Config directory missing",
                ),
            ],
            required_inputs=["lgogdownloader binary", "LGOGDOWNLOADER_CONFIG_PATH mount"],
            detected_inputs=[item for item in [binary, str(root) if has_mount else None] if item is not None],
            remediation_steps=[
                "Install `lgogdownloader` in the container image.",
                "Mount the lgogdownloader config directory read-only into the container.",
            ],
            last_sync=last_sync or {},
        )

    return IntegrationStatus(
        platform="gog",
        overall_status="unconfigured",
        active_backend=None,
        summary="GOG is not configured.",
        capabilities=[CapabilityStatus("ownership", "unconfigured", "No GOG runtime or session files detected")],
        checks=[
            CheckStatus("lgogdownloader_binary", "fail", "lgogdownloader not found in PATH"),
            CheckStatus("lgogdownloader_config", "fail", "Config directory missing"),
        ],
        required_inputs=["lgogdownloader binary", "LGOGDOWNLOADER_CONFIG_PATH mount"],
        detected_inputs=[],
        remediation_steps=[
            "Install `lgogdownloader` in the container image.",
            "Run `lgogdownloader --login` on the host and mount the config directory read-only.",
        ],
        last_sync=last_sync or {},
    )


def inspect_nintendo(last_sync: LastSyncMeta | None = None) -> IntegrationStatus:
    nxapi_bin = shutil.which(os.getenv("NXAPI_BIN", "nxapi"))
    has_session_token = bool(os.getenv("NINTENDO_SESSION_TOKEN"))
    cookies_path = Path(os.getenv("NINTENDO_COOKIES_FILE", "data/nintendo_cookies.json")).expanduser()
    has_cookies = cookies_path.is_file()
    auth_stale = (last_sync or {}).get("last_error_classification") == "auth_stale"

    if has_session_token and nxapi_bin is not None and auth_stale:
        detected_inputs = _detected_env_inputs(("NINTENDO_SESSION_TOKEN", has_session_token))
        detected_inputs.append(nxapi_bin)
        return IntegrationStatus(
            platform="nintendo",
            overall_status="stale",
            active_backend="nxapi",
            summary="Nintendo auth is stale and the nxapi session token must be refreshed.",
            capabilities=[
                CapabilityStatus("ownership", "stale", "Nintendo auth must be refreshed before play activity can be read."),
                CapabilityStatus("playtime", "stale", "Nintendo auth must be refreshed before playtime can be read."),
            ],
            checks=[
                CheckStatus("nxapi_binary", "pass", "nxapi found in PATH"),
                CheckStatus("nintendo_session_token", "warn", "Recent Nintendo auth failed and the session token should be refreshed"),
            ],
            required_inputs=["nxapi binary", "NINTENDO_SESSION_TOKEN or NINTENDO_COOKIES_FILE"],
            detected_inputs=detected_inputs,
            remediation_steps=[
                "Re-run `nxapi nso auth` and update `NINTENDO_SESSION_TOKEN`.",
                "Restart the container after updating the token.",
            ],
            last_sync=last_sync or {},
        )

    if has_session_token and nxapi_bin is not None:
        detected_inputs = _detected_env_inputs(("NINTENDO_SESSION_TOKEN", has_session_token))
        detected_inputs.append(nxapi_bin)
        if has_cookies:
            detected_inputs.append(str(cookies_path))
        return IntegrationStatus(
            platform="nintendo",
            overall_status="ready",
            active_backend="nxapi",
            summary="nxapi is available and Nintendo session auth is configured.",
            capabilities=[
                CapabilityStatus("ownership", "ready", "Nintendo play activity can be read."),
                CapabilityStatus("playtime", "ready", "Playtime is available through nxapi."),
            ],
            checks=[
                CheckStatus("nxapi_binary", "pass", "nxapi found in PATH"),
                CheckStatus("nintendo_session_token", "pass", "NINTENDO_SESSION_TOKEN is set"),
                CheckStatus("nintendo_cookies_file", "pass" if has_cookies else "warn", "Cookie fallback file found" if has_cookies else "Cookie fallback file not present"),
            ],
            required_inputs=["nxapi binary", "NINTENDO_SESSION_TOKEN or NINTENDO_COOKIES_FILE"],
            detected_inputs=detected_inputs,
            remediation_steps=[],
            last_sync=last_sync or {},
        )

    if has_cookies:
        return IntegrationStatus(
            platform="nintendo",
            overall_status="ready",
            active_backend="vgcs-cookie",
            summary="Nintendo cookie fallback is available for ownership-only sync.",
            capabilities=[
                CapabilityStatus("ownership", "ready", "VGCS cookies are available."),
            ],
            checks=[
                CheckStatus("nintendo_cookies_file", "pass", "Cookie fallback file found"),
                CheckStatus("nxapi_binary", "warn" if nxapi_bin is None else "pass", "nxapi not found in PATH" if nxapi_bin is None else "nxapi found in PATH"),
            ],
            required_inputs=["NINTENDO_COOKIES_FILE or NINTENDO_SESSION_TOKEN"],
            detected_inputs=[str(cookies_path)],
            remediation_steps=[
                "Set `NINTENDO_SESSION_TOKEN` and install `nxapi` if you need playtime data.",
            ],
            last_sync=last_sync or {},
        )

    if has_session_token or nxapi_bin is not None:
        detected_inputs = _detected_env_inputs(("NINTENDO_SESSION_TOKEN", has_session_token))
        if nxapi_bin is not None:
            detected_inputs.append(nxapi_bin)
        overall_status = "degraded" if has_session_token and nxapi_bin is None else "partially_configured"
        summary = (
            "Nintendo session auth is present, but the nxapi binary is missing."
            if has_session_token and nxapi_bin is None
            else "Nintendo nxapi setup is incomplete."
        )
        return IntegrationStatus(
            platform="nintendo",
            overall_status=overall_status,
            active_backend="nxapi" if nxapi_bin is not None or has_session_token else None,
            summary=summary,
            capabilities=[
                CapabilityStatus("ownership", overall_status, "Nintendo setup is incomplete."),
                CapabilityStatus("playtime", overall_status, "Nintendo setup is incomplete."),
            ],
            checks=[
                CheckStatus("nxapi_binary", "pass" if nxapi_bin is not None else "fail", "nxapi found in PATH" if nxapi_bin is not None else "nxapi not found in PATH"),
                CheckStatus("nintendo_session_token", "pass" if has_session_token else "fail", _env_check_summary("NINTENDO_SESSION_TOKEN", has_session_token)),
            ],
            required_inputs=["nxapi binary", "NINTENDO_SESSION_TOKEN or NINTENDO_COOKIES_FILE"],
            detected_inputs=detected_inputs,
            remediation_steps=[
                "Install `nxapi` in the container image.",
                "Set `NINTENDO_SESSION_TOKEN`, or provide `NINTENDO_COOKIES_FILE` for ownership-only fallback.",
            ],
            last_sync=last_sync or {},
        )

    return IntegrationStatus(
        platform="nintendo",
        overall_status="unconfigured",
        active_backend=None,
        summary="Nintendo is not configured.",
        capabilities=[
            CapabilityStatus("ownership", "unconfigured", "No Nintendo auth was detected."),
            CapabilityStatus("playtime", "unconfigured", "No Nintendo auth was detected."),
        ],
        checks=[
            CheckStatus("nxapi_binary", "fail", "nxapi not found in PATH"),
            CheckStatus("nintendo_session_token", "fail", "NINTENDO_SESSION_TOKEN is not set"),
            CheckStatus("nintendo_cookies_file", "fail", "Cookie fallback file missing"),
        ],
        required_inputs=["nxapi binary", "NINTENDO_SESSION_TOKEN or NINTENDO_COOKIES_FILE"],
        detected_inputs=[],
        remediation_steps=[
            "Install `nxapi` and set `NINTENDO_SESSION_TOKEN`, or mount a `NINTENDO_COOKIES_FILE` for ownership-only fallback.",
        ],
        last_sync=last_sync or {},
    )


def inspect_psn(last_sync: LastSyncMeta | None = None) -> IntegrationStatus:
    has_npsso = bool(os.getenv("PSN_NPSSO"))
    auth_stale = (last_sync or {}).get("last_error_classification") == "auth_stale"
    if auth_stale:
        return IntegrationStatus(
            platform="ps5",
            overall_status="stale",
            active_backend="psnawp",
            summary="PSN auth is stale and the NPSSO token must be re-extracted.",
            capabilities=[
                CapabilityStatus("ownership", "stale", "PSN auth must be refreshed before ownership can be read."),
                CapabilityStatus("playtime", "stale", "PSN auth must be refreshed before playtime can be read."),
            ],
            checks=[CheckStatus("psn_npsso", "warn", "Recent PSN auth failed and NPSSO must be refreshed")],
            required_inputs=["PSN_NPSSO"],
            detected_inputs=["PSN_NPSSO"] if has_npsso else [],
            remediation_steps=[
                "Re-extract `PSN_NPSSO` from a fresh PlayStation browser session cookie.",
            ],
            last_sync=last_sync or {},
        )

    if has_npsso:
        return IntegrationStatus(
            platform="ps5",
            overall_status="ready",
            active_backend="psnawp",
            summary="PSN NPSSO is configured.",
            capabilities=[
                CapabilityStatus("ownership", "ready", "Played PSN titles can be listed."),
                CapabilityStatus("playtime", "ready", "Playtime is available from PSN title stats."),
            ],
            checks=[CheckStatus("psn_npsso", "pass", "PSN_NPSSO is set")],
            required_inputs=["PSN_NPSSO"],
            detected_inputs=["PSN_NPSSO"],
            remediation_steps=[],
            last_sync=last_sync or {},
        )

    return IntegrationStatus(
        platform="ps5",
        overall_status="unconfigured",
        active_backend=None,
        summary="PSN is not configured.",
        capabilities=[
            CapabilityStatus("ownership", "unconfigured", "PSN_NPSSO is not set."),
            CapabilityStatus("playtime", "unconfigured", "PSN_NPSSO is not set."),
        ],
        checks=[CheckStatus("psn_npsso", "fail", "PSN_NPSSO is not set")],
        required_inputs=["PSN_NPSSO"],
        detected_inputs=[],
        remediation_steps=[
            "Set `PSN_NPSSO` from a valid PlayStation browser session cookie.",
        ],
        last_sync=last_sync or {},
    )


def _safe_inspect(
    platform: str,
    inspector,
    last_sync: LastSyncMeta | None = None,
) -> IntegrationStatus:
    try:
        return inspector(last_sync)
    except Exception as exc:
        return IntegrationStatus(
            platform=platform,
            overall_status="error",
            active_backend=None,
            summary=str(exc),
            capabilities=[],
            checks=[CheckStatus("inspector_error", "fail", str(exc))],
            required_inputs=[],
            detected_inputs=[],
            remediation_steps=["Check server logs for the inspector traceback and fix the underlying runtime issue."],
            last_sync=last_sync or {},
        )


def _epic_root() -> Path:
    configured = os.getenv("EPIC_LEGENDARY_PATH") or os.getenv("LEGENDARY_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()

    xdg_config_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "legendary"

    return Path.home() / ".config" / "legendary"


def _gog_root() -> Path:
    configured = os.getenv("LGOGDOWNLOADER_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "lgogdownloader"


def _detected_env_inputs(*pairs: tuple[str, bool]) -> list[str]:
    return [name for name, detected in pairs if detected]


def _env_check_summary(name: str, present: bool) -> str:
    return f"{name} is set" if present else f"{name} is not set"


def _existing_paths(*paths: Path) -> list[str]:
    return [str(path) for path in paths if path.exists()]
