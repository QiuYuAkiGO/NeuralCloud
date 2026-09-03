"""MistyAI HTTP adapter. Native DTOs do not escape this module."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from harness.embodiment.capabilities import (
    ACTION_EXECUTE,
    ACTION_RECONCILE,
    ACTION_SAFE_POINT,
    ACTION_VERIFY,
    EVIDENCE_REFERENCE,
    OBSERVATION_SNAPSHOT,
    PHYSIOLOGY_BODY_STATE,
    PHYSIOLOGY_VIRTUAL_RESOURCE_LEDGER,
    CapabilityDescriptor,
    CapabilityStatus,
    HealthStatus,
    ProviderCapabilities,
    ProviderHealth,
)
from harness.embodiment.contracts import (
    BodyStateSnapshot,
    EnergyState,
    EvidenceRef,
    PerceptionSnapshot,
)
from harness.embodiment.providers.mistyai_dto import (
    NativeAction,
    NativeActionResult,
    NativeBodyState,
    NativeCapabilities,
    NativeError,
    NativeHealth,
    NativeObservation,
)
from harness.runtime.models import ActionBackendResult, ActionExecution

PROVIDER_ID = "mistyai"
TERMINAL_NATIVE_STATES = frozenset(
    {"execution_succeeded", "failed", "cancelled", "unknown"}
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MistyAIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str = "transport_error",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.details = details or {}

    @property
    def uncertain_post(self) -> bool:
        return self.status_code is None or self.status_code >= 500


class MistyAIClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def capabilities(self) -> NativeCapabilities:
        return NativeCapabilities.model_validate(await self._request("GET", "/v1/capabilities"))

    async def health(self) -> NativeHealth:
        return NativeHealth.model_validate(await self._request("GET", "/v1/health"))

    async def observe(self) -> NativeObservation:
        return NativeObservation.model_validate(
            await self._request(
                "POST",
                "/v1/observations:capture",
                json={"schema_version": "body-contract-v1"},
            )
        )

    async def submit_action(self, payload: dict[str, Any], *, action_id: str) -> NativeAction | None:
        try:
            response = await self._request("POST", "/v1/actions", json=payload)
            return NativeAction.model_validate(response)
        except MistyAIError as exc:
            if not exc.uncertain_post:
                raise
            # The request may have committed. Query using the caller-assigned id;
            # never replay a side-effecting POST after an uncertain response.
            try:
                return await self.get_action(action_id)
            except MistyAIError:
                return None

    async def get_action(self, action_id: str) -> NativeAction:
        return NativeAction.model_validate(
            await self._request("GET", f"/v1/actions/{action_id}")
        )

    async def request_safe_point(self, action_id: str) -> NativeActionResult:
        return await self._action_result(
            f"/v1/actions/{action_id}:request-safe-point"
        )

    async def verify(self, action_id: str) -> NativeActionResult:
        return await self._action_result(f"/v1/actions/{action_id}:verify")

    async def reconcile(self, action_id: str) -> NativeActionResult:
        return await self._action_result(f"/v1/actions/{action_id}:reconcile")

    async def body_state(self) -> NativeBodyState:
        return NativeBodyState.model_validate(
            await self._request(
                "POST",
                "/v1/body-state:capture",
                json={"schema_version": "body-contract-v1"},
            )
        )

    async def _action_result(self, path: str) -> NativeActionResult:
        return NativeActionResult.model_validate(
            await self._request(
                "POST",
                path,
                json={"schema_version": "body-contract-v1"},
            )
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, json=json)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise MistyAIError(str(exc) or type(exc).__name__) from exc
        if response.is_success:
            payload = response.json()
            if not isinstance(payload, dict):
                raise MistyAIError("provider returned a non-object JSON response")
            return payload
        try:
            native = NativeError.model_validate(response.json())
            detail = native.error
            raise MistyAIError(
                detail.message,
                status_code=response.status_code,
                code=detail.code,
                retryable=detail.retryable,
                details=detail.details,
            )
        except MistyAIError:
            raise
        except Exception as exc:
            raise MistyAIError(
                f"provider HTTP {response.status_code}",
                status_code=response.status_code,
                code="invalid_error_response",
            ) from exc


class MistyAIActionBackend:
    def __init__(
        self,
        client: MistyAIClient,
        *,
        poll_interval_seconds: float = 0.2,
        clock: Any = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")
        self.client = client
        self.poll_interval_seconds = poll_interval_seconds
        self.clock = clock or _utc_now

    async def execute(self, execution: ActionExecution) -> ActionBackendResult:
        if execution.embodied_action is None:
            return ActionBackendResult(
                success=False,
                summary="structured embodied action is required",
            )
        payload = self._action_payload(execution)
        try:
            action = await self.client.submit_action(payload, action_id=execution.action_id)
            if action is None:
                return ActionBackendResult(
                    success=False,
                    summary="action submission outcome could not be proven",
                    unknown=True,
                )
            while action.state not in TERMINAL_NATIVE_STATES:
                if self.clock() >= execution.deadline:
                    return ActionBackendResult(
                        success=False,
                        summary="action polling deadline expired without a proven result",
                        unknown=True,
                    )
                await asyncio.sleep(self.poll_interval_seconds)
                action = await self.client.get_action(execution.action_id)
            if action.result is None:
                return ActionBackendResult(
                    success=False,
                    summary=f"provider returned terminal state {action.state} without a result",
                    unknown=True,
                )
            return self._canonical_result(action.result, observed_at=action.updated_at)
        except MistyAIError as exc:
            return self._provider_failure(exc)

    async def request_safe_point(self, execution: ActionExecution) -> ActionBackendResult:
        try:
            result = await self.client.request_safe_point(execution.action_id)
            return self._canonical_result(result)
        except MistyAIError as exc:
            return self._provider_failure(exc)

    async def verify(self, execution: ActionExecution) -> ActionBackendResult:
        try:
            result = await self.client.verify(execution.action_id)
            return self._canonical_result(result)
        except MistyAIError as exc:
            return self._provider_failure(exc)

    async def reconcile(self, execution: ActionExecution) -> ActionBackendResult:
        try:
            result = await self.client.reconcile(execution.action_id)
            return self._canonical_result(result)
        except MistyAIError as exc:
            return self._provider_failure(exc, reconcile=True)

    @staticmethod
    def _action_payload(execution: ActionExecution) -> dict[str, Any]:
        assert execution.embodied_action is not None
        risk_class = "high" if execution.risk_class == "high_impact" else execution.risk_class
        deadline = execution.deadline.isoformat().replace("+00:00", "Z")
        body: dict[str, Any] = {
            "schema_version": "body-contract-v1",
            "execution": {
                "action_id": execution.action_id,
                "goal_id": execution.goal_id,
                "goal": execution.goal,
                "intent": {
                    "skill_id": execution.embodied_action.intent.intent_id,
                    "arguments": execution.embodied_action.intent.arguments,
                },
                "success_criteria": [
                    item.model_dump(mode="json")
                    for item in execution.embodied_action.success_criteria
                ],
                "idempotency_key": execution.idempotency_key,
                "risk_class": risk_class,
                "reversible": execution.reversible,
                "verification_method": execution.verification_method,
                "required_approvals": execution.required_approvals,
                "approvals": execution.approvals,
                "current_step": execution.current_step,
                "safe_point": execution.safe_point,
                "continuation": execution.continuation,
                "version_vector": execution.version_vector.flatten(),
                "deadline": deadline,
            },
        }
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return {**body, "request_hash": hashlib.sha256(canonical).hexdigest()}

    @staticmethod
    def _canonical_result(
        result: NativeActionResult,
        *,
        observed_at: datetime | None = None,
    ) -> ActionBackendResult:
        moment = observed_at or _utc_now()
        return ActionBackendResult(
            success=result.success,
            summary=result.summary,
            evidence_refs=[_evidence(ref, created_at=moment) for ref in result.evidence_refs],
            external_effect_ref=result.external_effect_ref,
            safe_point=result.safe_point,
            continuation=result.continuation,
            unknown=result.unknown,
            retry_safe=result.retry_safe,
        )

    @staticmethod
    def _provider_failure(exc: MistyAIError, *, reconcile: bool = False) -> ActionBackendResult:
        deterministic = exc.status_code in {401, 422} or exc.code in {
            "authentication_failed",
            "validation_failed",
            "unsupported_unstructured_goal",
            "unknown_skill",
            "policy_denied",
            "approval_missing",
            "capability_not_available",
            "path_outside_authorized_root",
            "ambiguous_target",
            "deadline_expired",
        }
        unknown = reconcile or (exc.uncertain_post and not deterministic)
        return ActionBackendResult(
            success=False,
            summary=f"{PROVIDER_ID}:{exc.code}: {exc}",
            unknown=unknown,
            retry_safe=exc.retryable,
            continuation={
                "provider_error": {
                    "code": exc.code,
                    "status_code": exc.status_code,
                    "retryable": exc.retryable,
                    "details": exc.details,
                }
            },
        )


class MistyAIPerceptionBackend:
    def __init__(self, client: MistyAIClient) -> None:
        self.client = client

    async def observe(self, *, now: datetime | None = None) -> PerceptionSnapshot:
        native = await self.client.observe()
        moment = now or _utc_now()
        confidence = 1.0
        if "unavailable" in native.backend_health.values():
            confidence = 0.0
        elif "degraded" in native.backend_health.values():
            confidence = 0.5
        extension = native.model_dump(mode="json", exclude={"frame"})
        return PerceptionSnapshot(
            provider_id=PROVIDER_ID,
            body_instance_id=native.body_instance_id,
            revision=native.environment_revision,
            captured_at=native.captured_at,
            summary=f"desktop observation revision {native.environment_revision}",
            confidence=confidence,
            freshness_seconds=max(0.0, (moment - native.captured_at).total_seconds()),
            evidence_refs=[
                EvidenceRef(
                    provider_id=PROVIDER_ID,
                    ref=native.frame.evidence_ref,
                    digest=native.frame.sha256,
                    media_type=native.frame.media_type,
                    created_at=native.captured_at,
                )
            ],
            extensions={"mistyai.desktop.v1": extension},
        )


class MistyAICapabilityProvider:
    def __init__(self, client: MistyAIClient) -> None:
        self.client = client
        self._last_capabilities: NativeCapabilities | None = None

    async def capabilities(self) -> ProviderCapabilities:
        native = await self.client.capabilities()
        self._last_capabilities = native
        names = [
            ACTION_EXECUTE,
            OBSERVATION_SNAPSHOT,
            EVIDENCE_REFERENCE,
            ACTION_SAFE_POINT,
            ACTION_VERIFY,
            ACTION_RECONCILE,
        ]
        if native.extensions.body_state_v1:
            names.append(PHYSIOLOGY_BODY_STATE)
        if native.extensions.virtual_resource_ledger:
            names.append(PHYSIOLOGY_VIRTUAL_RESOURCE_LEDGER)
        return ProviderCapabilities(
            provider_id=PROVIDER_ID,
            provider_kind="desktop-body",
            provider_revision=native.contract_versions[0] if native.contract_versions else None,
            body_instance_id=native.body_instance_id,
            capabilities=[
                CapabilityDescriptor(
                    capability=name,
                    status=CapabilityStatus.AVAILABLE,
                    implementation_version="body-contract-v1",
                )
                for name in names
            ],
            observed_at=_utc_now(),
        )

    async def health(self) -> ProviderHealth:
        native = await self.client.health()
        observation = _worst_health(native.environment.status, native.observation.status)
        mapping = {
            ACTION_EXECUTE: HealthStatus(native.action.status),
            ACTION_SAFE_POINT: HealthStatus(native.action.status),
            ACTION_VERIFY: HealthStatus(native.action.status),
            ACTION_RECONCILE: HealthStatus(native.action.status),
            OBSERVATION_SNAPSHOT: observation,
            EVIDENCE_REFERENCE: HealthStatus(native.evidence_store.status),
        }
        if self._last_capabilities and self._last_capabilities.extensions.body_state_v1:
            mapping[PHYSIOLOGY_BODY_STATE] = (
                HealthStatus(native.body_state.status)
                if native.body_state is not None
                else HealthStatus.UNAVAILABLE
            )
        if self._last_capabilities and self._last_capabilities.extensions.virtual_resource_ledger:
            mapping[PHYSIOLOGY_VIRTUAL_RESOURCE_LEDGER] = (
                HealthStatus(native.body_state.status)
                if native.body_state is not None
                else HealthStatus.UNAVAILABLE
            )
        return ProviderHealth(
            provider_id=PROVIDER_ID,
            body_instance_id=native.body_instance_id,
            status=HealthStatus(native.status),
            capability_health=mapping,
            observed_at=native.checked_at,
            detail={
                "active_action_id": native.active_action_id,
                "manual_takeover_active": native.manual_takeover_active,
            },
        )


class MistyAIBodyStateProvider:
    def __init__(self, client: MistyAIClient) -> None:
        self.client = client

    async def snapshot(self) -> BodyStateSnapshot:
        native = await self.client.body_state()
        return BodyStateSnapshot(
            provider_id=PROVIDER_ID,
            body_instance_id=native.body_instance_id,
            revision=native.revision,
            captured_at=native.captured_at,
            energy=EnergyState.model_validate(native.energy.model_dump(mode="json")),
            evidence_refs=[_evidence(ref, created_at=native.captured_at) for ref in native.evidence_refs],
        )


def _evidence(ref: str, *, created_at: datetime) -> EvidenceRef:
    digest = ref.rsplit("/", 1)[-1] if ref.startswith("evidence://sha256/") else None
    return EvidenceRef(
        provider_id=PROVIDER_ID,
        ref=ref,
        digest=digest,
        created_at=created_at,
    )


def _worst_health(*values: str) -> HealthStatus:
    ranking = {"healthy": 0, "degraded": 1, "unavailable": 2}
    return HealthStatus(max(values, key=lambda item: ranking[item]))
