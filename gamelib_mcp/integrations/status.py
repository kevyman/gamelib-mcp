from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CapabilityStatus:
    name: str
    status: str
    summary: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
        }


@dataclass(slots=True)
class CheckStatus:
    name: str
    status: str
    summary: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
        }


@dataclass(slots=True)
class IntegrationStatus:
    platform: str
    overall_status: str
    active_backend: str | None
    summary: str
    capabilities: list[CapabilityStatus] = field(default_factory=list)
    checks: list[CheckStatus] = field(default_factory=list)
    required_inputs: list[str] = field(default_factory=list)
    detected_inputs: list[str] = field(default_factory=list)
    remediation_steps: list[str] = field(default_factory=list)
    last_sync: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "overall_status": self.overall_status,
            "active_backend": self.active_backend,
            "summary": self.summary,
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "checks": [check.to_dict() for check in self.checks],
            "required_inputs": list(self.required_inputs),
            "detected_inputs": list(self.detected_inputs),
            "remediation_steps": list(self.remediation_steps),
            "last_sync": dict(self.last_sync),
        }
