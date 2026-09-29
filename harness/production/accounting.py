"""Operator allocation of preserved usage whose original body was unknown."""

from __future__ import annotations

import hmac

from pydantic import BaseModel, ConfigDict, Field


class UsageAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    assignment_id: str = Field(min_length=1, max_length=240)
    expected_usage_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_instance_id: str = Field(min_length=1, max_length=128)
    episode_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)


def install_operator_accounting_routes(app, supervisor, *, token, service_token=None):
    from fastapi import Header, HTTPException, Query

    def authorize(authorization):
        # Equal credentials cannot establish an operator-only boundary. Leave
        # this optional operation disabled until distinct tokens are configured.
        if (not token or (service_token and hmac.compare_digest(token, service_token))
                or not hmac.compare_digest(authorization or "", "Bearer " + token)):
            raise HTTPException(401, "operator control token required")
        runtime = getattr(supervisor, "production_runtime", None)
        if runtime is None:
            raise HTTPException(409, "production mechanism is not enabled")
        return runtime

    @app.get("/v1/production/accounting/unassigned")
    async def unassigned(limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0),
                         authorization: str | None = Header(default=None)):
        runtime = authorize(authorization)
        await runtime.refresh_body()
        body = runtime.context_snapshot("owner")["body"]
        target = ({key: body["snapshot"].get(key) for key in ("body_instance_id", "episode_id", "captured_at",
            "sample_sequence", "ledger_revision", "evidence_refs")} if body["available"] else None)
        return {"unassigned_count": runtime.store.unassigned_usage_count(),
                "usage": runtime.store.unassigned_usage(limit=limit, offset=offset), "verified_current_target": target}

    @app.post("/v1/production/model-usage/{attempt_id}/assignment")
    async def assign(attempt_id: str, request: UsageAssignmentRequest,
                     authorization: str | None = Header(default=None)):
        runtime = authorize(authorization)
        try:
            return await runtime.assign_usage({"attempt_id": attempt_id, **request.model_dump(mode="json")})
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
