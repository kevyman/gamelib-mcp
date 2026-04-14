from pathlib import Path
from typing import get_type_hints
from unittest.mock import patch

from gamelib_mcp.integrations.inspectors import (
    inspect_all_integrations,
    inspect_all_integrations_dict,
)
from gamelib_mcp.integrations.status import (
    CapabilityStatus,
    CheckStatus,
    IntegrationStatus,
)


def test_integration_status_serializes_expected_fields():
    status = IntegrationStatus(
        platform="epic",
        overall_status="degraded",
        active_backend="legendary-cache",
        summary="Ownership is ready but playtime auth is stale.",
        capabilities=[
            CapabilityStatus(name="ownership", status="ready", summary="Metadata cache present"),
            CapabilityStatus(name="playtime", status="stale", summary="Refresh token rejected"),
        ],
        checks=[
            CheckStatus(name="legendary_user_json", status="pass", summary="user.json found"),
            CheckStatus(name="epic_playtime_token", status="warn", summary="Refresh token rejected"),
        ],
        required_inputs=["EPIC_LEGENDARY_PATH:/legendary"],
        detected_inputs=["/legendary/user.json", "/legendary/metadata"],
        remediation_steps=[
            "Run `legendary auth` on the host.",
            "Run `legendary list --force-refresh` on the host.",
        ],
        last_sync={"last_success_at": "2026-04-13T12:00:00+00:00"},
    )

    payload = status.to_dict()

    assert payload["platform"] == "epic"
    assert payload["overall_status"] == "degraded"
    assert payload["capabilities"][1]["status"] == "stale"
    assert payload["checks"][1]["status"] == "warn"
    assert payload["remediation_steps"][0].startswith("Run `legendary auth`")


def test_integration_status_serializes_optional_backend_as_none():
    status = IntegrationStatus(
        platform="gog",
        overall_status="unavailable",
        active_backend=None,
        summary="No backend detected.",
    )

    payload = status.to_dict()

    assert payload["platform"] == "gog"
    assert payload["active_backend"] is None
    assert get_type_hints(IntegrationStatus)["active_backend"] == str | None


def test_inspect_epic_reports_degraded_when_metadata_exists_but_playtime_is_stale(
    tmp_path: Path,
):
    legendary_dir = tmp_path / "legendary"
    metadata_dir = legendary_dir / "metadata"
    metadata_dir.mkdir(parents=True)
    (legendary_dir / "user.json").write_text('{"refresh_token":"stale"}', encoding="utf-8")
    (metadata_dir / "game.json").write_text("{}", encoding="utf-8")

    with patch.dict("os.environ", {"EPIC_LEGENDARY_PATH": str(legendary_dir)}, clear=False):
        statuses = inspect_all_integrations(
            last_sync_by_platform={"epic": {"last_error_classification": "auth_stale"}}
        )

    epic = statuses["epic"]

    assert epic.overall_status == "degraded"
    assert epic.capabilities[0].name == "ownership"
    assert epic.capabilities[1].status == "stale"


def test_inspect_gog_reports_degraded_when_binary_missing_but_mount_exists(tmp_path: Path):
    config_dir = tmp_path / "lgogdownloader"
    config_dir.mkdir()

    with (
        patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": str(config_dir)}, clear=False),
        patch("gamelib_mcp.integrations.inspectors.shutil.which", return_value=None),
    ):
        statuses = inspect_all_integrations()

    assert statuses["gog"].overall_status == "degraded"
    assert "binary" in statuses["gog"].summary.lower()


def test_inspect_nintendo_reports_degraded_when_token_present_but_nxapi_missing(tmp_path: Path):
    missing_cookies_path = tmp_path / "missing_nintendo_cookies.json"

    with (
        patch.dict(
            "os.environ",
            {
                "NINTENDO_SESSION_TOKEN": "token",
                "NINTENDO_COOKIES_FILE": str(missing_cookies_path),
            },
            clear=True,
        ),
        patch("gamelib_mcp.integrations.inspectors.shutil.which", side_effect=lambda name: None),
    ):
        statuses = inspect_all_integrations()

    nintendo = statuses["nintendo"]

    assert nintendo.overall_status == "degraded"
    assert nintendo.active_backend == "nxapi"


def test_inspect_nintendo_cookie_fallback_reports_vgcs_cookie_ownership_only(tmp_path: Path):
    cookies_path = tmp_path / "nintendo_cookies.json"
    cookies_path.write_text("{}", encoding="utf-8")

    with (
        patch.dict(
            "os.environ",
            {"NINTENDO_COOKIES_FILE": str(cookies_path)},
            clear=True,
        ),
        patch("gamelib_mcp.integrations.inspectors.shutil.which", side_effect=lambda name: None),
    ):
        statuses = inspect_all_integrations()

    nintendo = statuses["nintendo"]

    assert nintendo.active_backend == "vgcs-cookie"
    assert [capability.name for capability in nintendo.capabilities] == ["ownership"]
    assert all(capability.name != "playtime" for capability in nintendo.capabilities)


def test_inspect_psn_reports_stale_when_auth_requires_reextract():
    with patch.dict("os.environ", {}, clear=True):
        statuses = inspect_all_integrations(
            last_sync_by_platform={"ps5": {"last_error_classification": "auth_stale"}}
        )

    psn = statuses["ps5"]

    assert psn.overall_status == "stale"
    assert psn.active_backend == "psnawp"


def test_inspect_steam_reports_ready_when_credentials_present():
    with patch.dict(
        "os.environ",
        {"STEAM_API_KEY": "key", "STEAM_ID": "steam-id"},
        clear=True,
    ):
        statuses = inspect_all_integrations()

    steam = statuses["steam"]

    assert steam.overall_status == "ready"
    assert steam.active_backend == "steam-web-api"


def test_inspect_steam_reports_unconfigured_when_credentials_missing():
    with patch.dict("os.environ", {}, clear=True):
        statuses = inspect_all_integrations()

    steam = statuses["steam"]

    assert steam.overall_status == "unconfigured"
    assert steam.active_backend is None


def test_inspect_all_integrations_dict_serializes_statuses():
    with patch.dict(
        "os.environ",
        {"STEAM_API_KEY": "key", "STEAM_ID": "steam-id"},
        clear=True,
    ):
        payload = inspect_all_integrations_dict()

    assert payload["steam"]["platform"] == "steam"
    assert payload["steam"]["overall_status"] == "ready"
    assert isinstance(payload["steam"]["capabilities"], list)


def test_inspect_epic_reports_partially_configured_when_only_user_json_exists(tmp_path: Path):
    legendary_dir = tmp_path / "legendary"
    legendary_dir.mkdir(parents=True)
    (legendary_dir / "user.json").write_text('{"refresh_token":"present"}', encoding="utf-8")

    with patch.dict(
        "os.environ",
        {"EPIC_LEGENDARY_PATH": str(legendary_dir)},
        clear=True,
    ):
        statuses = inspect_all_integrations()

    epic = statuses["epic"]

    assert epic.overall_status == "partially_configured"
    assert epic.active_backend == "legendary-cache"
