"""Bounded polling bridge; all QQ work stays on the watcher's existing actor."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from harness.embodiment.qq_contracts import QQScope


class QQCapabilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: QQScope
    run_id: str = Field(min_length=1, max_length=160)
    reported_at: AwareDatetime
    read_ready: bool = Field(strict=True)
    send_ready: bool = Field(strict=True)
    paused: bool = Field(default=False, strict=True)
    uncertain: bool = Field(default=False, strict=True)
    observation_refs: list[str] = Field(default_factory=list, max_length=16)


class QQCapabilityClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: QQScope
    run_id: str = Field(min_length=1, max_length=160)
    generation: int = Field(ge=0, strict=True)
    opportunity_id: str | None = Field(default=None, min_length=1, max_length=256)


class QQCapabilityAck(QQCapabilityClaim):
    opportunity_id: str = Field(min_length=1, max_length=256)
    claim_token: str = Field(min_length=1, max_length=128)


class QQCapabilityClient:
    """Uses the authenticated existing Runtime connection, never another server."""

    def __init__(self, shared_client, ledger):
        self.client = shared_client.client
        self.ledger = ledger
        self.owner: Literal["legacy", "shared", "off"] = "legacy"
        self.generation = 0
        self.send_enabled = False
        self.valid_until: datetime | None = None
        saved = ledger.capability_owner()
        if saved:
            self.owner, self.generation = saved["owner"], saved["generation"]

    def _post(self, path, payload):
        response = self.client.post(path, json=payload, timeout=2, follow_redirects=False)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("qq_capability_response_invalid")
        return value

    def report(self, report: QQCapabilityReport):
        value = self._post("/v1/qq/capabilities/report", report.model_dump(mode="json"))
        owner, generation = value.get("owner"), value.get("generation")
        if owner not in {"legacy", "shared", "off"} or type(generation) is not int or generation < 0:
            raise ValueError("qq_capability_owner_invalid")
        valid = datetime.fromisoformat(value["valid_until"].replace("Z", "+00:00"))
        if valid.tzinfo is None:
            raise ValueError("qq_capability_expiry_invalid")
        self.owner, self.generation = owner, generation
        self.send_enabled = value.get("qq_send_enabled") is True
        self.valid_until = valid
        self.ledger.set_capability_owner(owner=owner, generation=generation)
        return value

    def claim(self, scope: QQScope, run_id: str, *, now: datetime):
        if self.owner != "shared" or self.valid_until is None or now >= self.valid_until:
            return None
        request = QQCapabilityClaim(scope=scope, run_id=run_id, generation=self.generation)
        value = self._post("/v1/qq/opportunities/claim", request.model_dump(mode="json"))
        row = value.get("opportunity")
        if row is None:
            return None
        if (not isinstance(row, dict) or row.get("scope") != scope.model_dump(mode="json")
                or row.get("run_id") != run_id or row.get("generation") != self.generation):
            raise ValueError("qq_capability_claim_binding_invalid")
        deadline = datetime.fromisoformat(row["deadline_at"].replace("Z", "+00:00"))
        if deadline.tzinfo is None or not 0 < (deadline - now).total_seconds() <= 60:
            raise ValueError("qq_capability_claim_expired")
        # Durable before capture, model preparation, or any GUI side effect.
        self.ledger.register_capability_claim(row)
        return row

    def acknowledge(self, row):
        request = QQCapabilityAck(scope=QQScope.model_validate(row["scope"]), run_id=row["run_id"],
            generation=row["generation"], opportunity_id=row["opportunity_id"], claim_token=row["claim_token"])
        value = self._post("/v1/qq/opportunities/ack", request.model_dump(mode="json"))
        if value.get("status") in {"completed", "wait", "rejected", "expired", "failed", "superseded"}:
            self.ledger.finish_capability_ack(row["opportunity_id"])
        return value

    def check_claim(self, row, *, now):
        if self.owner != "shared" or self.valid_until is None or now >= self.valid_until:
            raise ValueError("qq_capability_authority_expired")
        request = QQCapabilityClaim(scope=QQScope.model_validate(row["scope"]), run_id=row["run_id"],
            generation=row["generation"], opportunity_id=row["opportunity_id"])
        current = self._post("/v1/qq/opportunities/claim", request.model_dump(mode="json")).get("opportunity")
        if current is None or current["claim_token"] != row["claim_token"]:
            raise ValueError("qq_capability_claim_superseded")


__all__ = ["QQCapabilityReport", "QQCapabilityClaim", "QQCapabilityAck", "QQCapabilityClient"]
