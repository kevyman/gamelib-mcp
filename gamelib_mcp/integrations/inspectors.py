from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TypedDict

from ..data.epic import _legendary_config_path as _epic_root
from ..data.gog import _AUTH_FILE_TOKENS as _GOG_AUTH_FILE_TOKENS
from ..data.gog import _config_dir as _gog_root
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
    has_auth = _has_gog_auth_files(root)
    auth_stale = (last_sync or {}).get("last_error_classification") == "auth_stale"

    if has_mount and binary is None:
        return IntegrationStatus(
            platform="gog",
            overall_status="degraded",
            active_backend=None,
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

    if has_mount and binary is not None and has_auth and auth_stale:
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

    if has_mount and binary is not None and has_auth:
        return IntegrationStatus(
            platform="gog",
            overall_status="ready",
            active_backend="lgogdownloader",
            summary="lgogdownloader and its session files are available.",
            capabilities=[CapabilityStatus("ownership", "ready", "GOG ownership can be listed locally")],
            checks=[
                CheckStatus("lgogdownloader_binary", "pass", "lgogdownloader found in PATH"),
                CheckStatus("lgogdownloader_config", "pass", "Config directory found"),
                CheckStatus("gog_session_auth", "pass", "GOG session files found"),
            ],
            required_inputs=["lgogdownloader binary", "LGOGDOWNLOADER_CONFIG_PATH mount"],
            detected_inputs=[binary, str(root)],
            remediation_steps=[],
            last_sync=last_sync or {},
        )

    if binary is not None or has_mount:
        missing_steps = []
        if binary is None:
            missing_steps.append("Install `lgogdownloader` in the container image.")
        if not has_mount:
            missing_steps.append("Mount the lgogdownloader config directory read-only into the container.")
        if has_mount and binary is not None and not has_auth:
            missing_steps.append("Run `lgogdownloader --login` on the host and mount the resulting session files.")
        return IntegrationStatus(
            platform="gog",
            overall_status="partially_configured",
            active_backend="lgogdownloader",
            summary="GOG requires the lgogdownloader binary, a mounted config directory, and session auth files.",
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
                CheckStatus(
                    "gog_session_auth",
                    "pass" if has_auth else "fail",
                    "GOG session files found" if has_auth else "GOG session files missing",
                ),
            ],
            required_inputs=["lgogdownloader binary", "LGOGDOWNLOADER_CONFIG_PATH mount"],
            detected_inputs=[item for item in [binary, str(root) if has_mount else None] if item is not None],
            remediation_steps=missing_steps,
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
    cookies_path = Path(os.getenv("NINTENDO_COOKIES_FILE", "data/nintendo_cookies.json")).expanduser()
    has_cookies = cookies_path.is_file()
    pctl_path = Path(
        os.getenv("NINTENDO_PCTL_SESSION_FILE", "data/nintendo_pctl_session.json")
    ).expanduser()
    has_pctl = pctl_path.is_file()
    auth_stale = (last_sync or {}).get("last_error_classification") == "auth_stale"

    if has_cookies:
        if auth_stale:
            capabilities = [
                CapabilityStatus("ownership", "stale", "Nintendo cookies may be expired; re-run set_nintendo_session."),
            ]
            checks = [
                CheckStatus("nintendo_cookies_file", "warn", "Cookie file present but recent auth failed"),
            ]
            detected_inputs = [str(cookies_path)]
            summary = "Nintendo auth is stale; re-run set_nintendo_session."
            remediation_steps: list[str] = ["Re-run set_nintendo_session to refresh the VGCS session cookies."]
            if has_pctl:
                capabilities.append(
                    CapabilityStatus("playtime", "stale", "Parental Controls auth may be stale; re-run set_nintendo_pctl_session.")
                )
                checks.append(
                    CheckStatus("nintendo_pctl_session", "warn", "Parental Controls token present but recent auth failed")
                )
                detected_inputs.append(str(pctl_path))
                summary = "Nintendo auth is stale; re-run set_nintendo_session and/or set_nintendo_pctl_session."
                remediation_steps.append("Re-run set_nintendo_pctl_session to refresh the Parental Controls token.")
            return IntegrationStatus(
                platform="nintendo",
                overall_status="stale",
                active_backend="vgcs-cookie",
                summary=summary,
                capabilities=capabilities,
                checks=checks,
                required_inputs=["NINTENDO_COOKIES_FILE"],
                detected_inputs=detected_inputs,
                remediation_steps=remediation_steps,
                last_sync=last_sync or {},
            )

        capabilities = [CapabilityStatus("ownership", "ready", "VGCS cookies are available.")]
        checks = [
            CheckStatus("nintendo_cookies_file", "pass", "Cookie fallback file found"),
        ]
        detected_inputs = [str(cookies_path)]
        summary = "Nintendo ownership via VGCS cookies."
        remediation_steps = []
        if has_pctl:
            capabilities.append(
                CapabilityStatus("playtime", "ready", "Parental Controls playtime is configured.")
            )
            checks.append(
                CheckStatus("nintendo_pctl_session", "pass", "Parental Controls session token found")
            )
            detected_inputs.append(str(pctl_path))
            summary = "Nintendo ownership via VGCS cookies; playtime via Parental Controls."
        return IntegrationStatus(
            platform="nintendo",
            overall_status="ready",
            active_backend="vgcs-cookie",
            summary=summary,
            capabilities=capabilities,
            checks=checks,
            required_inputs=["NINTENDO_COOKIES_FILE"],
            detected_inputs=detected_inputs,
            remediation_steps=remediation_steps,
            last_sync=last_sync or {},
        )

    if has_pctl:
        # Playtime-only setup: Parental Controls token present but no ownership backend.
        # sync_nintendo still runs the playtime layer, so this is a supported state.
        stale = auth_stale
        playtime_status = "stale" if stale else "ready"
        return IntegrationStatus(
            platform="nintendo",
            overall_status="stale" if stale else "partially_configured",
            active_backend="parental-controls",
            summary=(
                "Nintendo Switch playtime auth is stale; refresh the Parental Controls token."
                if stale
                else "Nintendo Switch playtime via Parental Controls; ownership sync not configured."
            ),
            capabilities=[
                CapabilityStatus(
                    "ownership",
                    "unconfigured",
                    "Set NINTENDO_COOKIES_FILE via set_nintendo_session for digital ownership.",
                ),
                CapabilityStatus(
                    "playtime",
                    playtime_status,
                    "Parental Controls auth is stale; re-run set_nintendo_pctl_session."
                    if stale
                    else "Parental Controls playtime is configured.",
                ),
            ],
            checks=[
                CheckStatus(
                    "nintendo_pctl_session",
                    "warn" if stale else "pass",
                    "Parental Controls token present but recent sync failed auth"
                    if stale
                    else "Parental Controls session token found",
                ),
                CheckStatus("nintendo_cookies_file", "warn", "Cookie fallback file not present"),
            ],
            required_inputs=[
                "NINTENDO_PCTL_SESSION_FILE (playtime) and/or NINTENDO_COOKIES_FILE (ownership)"
            ],
            detected_inputs=[str(pctl_path)],
            remediation_steps=[
                "Re-run set_nintendo_pctl_session to refresh the Parental Controls token."
                if stale
                else "Run set_nintendo_session to add Switch digital ownership.",
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
            CheckStatus("nintendo_cookies_file", "fail", "Cookie fallback file missing"),
        ],
        required_inputs=["NINTENDO_COOKIES_FILE"],
        detected_inputs=[],
        remediation_steps=[
            "Mount a NINTENDO_COOKIES_FILE for ownership sync (set_nintendo_session), "
            "and/or set NINTENDO_PCTL_SESSION_FILE for playtime (set_nintendo_pctl_session).",
        ],
        last_sync=last_sync or {},
    )


def inspect_psn(last_sync: LastSyncMeta | None = None) -> IntegrationStatus:
    has_npsso = bool(os.getenv("PSN_NPSSO"))
    auth_stale = (last_sync or {}).get("last_error_classification") == "auth_stale"
    if auth_stale and has_npsso:
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


def _has_gog_auth_files(root: Path) -> bool:
    if not root.is_dir():
        return False
    return any(
        path.is_file() and any(token in path.name.lower() for token in _GOG_AUTH_FILE_TOKENS)
        for path in root.iterdir()
    )


def _detected_env_inputs(*pairs: tuple[str, bool]) -> list[str]:
    return [name for name, detected in pairs if detected]


def _env_check_summary(name: str, present: bool) -> str:
    return f"{name} is set" if present else f"{name} is not set"


def _existing_paths(*paths: Path) -> list[str]:
    return [str(path) for path in paths if path.exists()]
