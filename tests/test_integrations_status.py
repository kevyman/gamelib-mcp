from typing import get_type_hints

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
