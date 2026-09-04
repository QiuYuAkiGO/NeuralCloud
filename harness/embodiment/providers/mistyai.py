"""MistyAI HTTP adapter. Native DTOs do not escape this module."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

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
    NativeBodyStateCaptureResponse,
    NativeCapabilities,
    NativeError,
    NativeHealth,
    NativeObservation,
)
from harness.runtime.models import ActionBackendResult, ActionExecution

PROVIDER_ID = "mistyai"
EVIDENCE_REF_PATTERN = re.compile(r"^evidence://sha256/([0-9a-f]{64})$")
EVIDENCE_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPE_PATTERN = re.compile(r"^[a-z0-9!#$%&'*+.^_`|~-]+/[a-z0-9!#$%&'*+.^_`|~-]+$")
DEFAULT_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
DEFAULT_EVIDENCE_TIMEOUT_SECONDS = 10.0
DEFAULT_EVIDENCE_CACHE_ENTRIES = 128
DEFAULT_MAX_EVIDENCE_REFS = 32
TERMINAL_NATIVE_STATES = frozenset(
    {"execution_succeeded", "failed", "cancelled", "unknown"}
)

_NativePayload = TypeVar("_NativePayload", bound=BaseModel)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_native(
    model: type[_NativePayload],
    payload: dict[str, Any],
    *,
    surface: str,
) -> _NativePayload:
    try:
        return model.model_validate(payload)
    except Exception as exc:
        raise MistyAIError(
            f"provider returned an invalid {surface} response",
            code="invalid_provider_response",
            details={"surface": surface, "error_type": type(exc).__name__},
        ) from exc


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
        evidence_timeout_seconds: float = DEFAULT_EVIDENCE_TIMEOUT_SECONDS,
        max_evidence_bytes: int = DEFAULT_MAX_EVIDENCE_BYTES,
        evidence_cache_entries: int = DEFAULT_EVIDENCE_CACHE_ENTRIES,
        max_evidence_refs: int = DEFAULT_MAX_EVIDENCE_REFS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if evidence_timeout_seconds <= 0:
            raise ValueError("evidence timeout must be positive")
        if max_evidence_bytes <= 0:
            raise ValueError("maximum evidence size must be positive")
        if evidence_cache_entries <= 0:
            raise ValueError("evidence cache size must be positive")
        if max_evidence_refs <= 0:
            raise ValueError("maximum evidence reference count must be positive")
        self._owns_client = client is None
        self.evidence_timeout_seconds = evidence_timeout_seconds
        self.max_evidence_bytes = max_evidence_bytes
        self.evidence_cache_entries = evidence_cache_entries
        self.max_evidence_refs = max_evidence_refs
        self._verified_evidence: OrderedDict[str, str] = OrderedDict()
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def capabilities(self) -> NativeCapabilities:
        return _validate_native(
            NativeCapabilities,
            await self._request("GET", "/v1/capabilities"),
            surface="capabilities",
        )

    async def health(self) -> NativeHealth:
        return _validate_native(
            NativeHealth,
            await self._request("GET", "/v1/health"),
            surface="health",
        )

    async def observe(self) -> NativeObservation:
        observation = _validate_native(
            NativeObservation,
            await self._request(
                "POST",
                "/v1/observations:capture",
                json={"schema_version": "body-contract-v1"},
            ),
            surface="observation",
        )
        await self.verify_evidence_ref(
            observation.frame.evidence_ref,
            expected_digest=observation.frame.sha256,
            expected_media_type=observation.frame.media_type,
        )
        if observation.a11y_ref is not None:
            await self.verify_evidence_ref(observation.a11y_ref)
        return observation

    async def submit_action(self, payload: dict[str, Any], *, action_id: str) -> NativeAction | None:
        try:
            response = await self._request("POST", "/v1/actions", json=payload)
            return await self._validated_action(response, expected_action_id=action_id)
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
        return await self._validated_action(
            await self._request("GET", f"/v1/actions/{action_id}"),
            expected_action_id=action_id,
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
        response = _validate_native(
            NativeBodyStateCaptureResponse,
            await self._request(
                "POST",
                "/v1/body-state:capture",
                json={"schema_version": "body-contract-v1"},
            ),
            surface="body-state",
        )
        await self.verify_evidence_refs(response.body_state.evidence_refs)
        return response.body_state

    async def _action_result(self, path: str) -> NativeActionResult:
        result = _validate_native(
            NativeActionResult,
            await self._request(
                "POST",
                path,
                json={"schema_version": "body-contract-v1"},
            ),
            surface="action-result",
        )
        await self._verify_action_result(result)
        return result

    async def _validated_action(
        self,
        payload: dict[str, Any],
        *,
        expected_action_id: str,
    ) -> NativeAction:
        action = _validate_native(NativeAction, payload, surface="action")
        if action.action_id != expected_action_id:
            raise MistyAIError(
                "provider returned an action for a different request",
                code="invalid_provider_response",
                details={
                    "surface": "action",
                    "expected_action_id": expected_action_id,
                    "actual_action_id": action.action_id,
                },
            )
        if action.result is not None:
            await self._verify_action_result(action.result)
        return action

    async def _verify_action_result(self, result: NativeActionResult) -> None:
        if result.success and not result.evidence_refs:
            raise MistyAIError(
                "successful action result has no evidence",
                code="evidence_missing",
            )
        await self.verify_evidence_refs(result.evidence_refs)

    async def verify_evidence_refs(self, references: list[str]) -> None:
        if len(references) > self.max_evidence_refs:
            raise MistyAIError(
                "provider returned too many evidence references",
                code="evidence_fanout_exceeded",
                details={
                    "references": len(references),
                    "maximum_references": self.max_evidence_refs,
                },
            )
        try:
            async with asyncio.timeout(self.evidence_timeout_seconds):
                for reference in references:
                    await self.verify_evidence_ref(reference)
        except TimeoutError as exc:
            raise MistyAIError(
                "evidence set exceeded its verification deadline",
                code="evidence_download_failed",
            ) from exc

    async def verify_evidence_ref(
        self,
        reference: str,
        *,
        expected_digest: str | None = None,
        expected_media_type: str | None = None,
    ) -> str:
        match = EVIDENCE_REF_PATTERN.fullmatch(reference)
        if match is None:
            raise MistyAIError(
                "provider returned an invalid evidence reference",
                code="invalid_evidence_ref",
                details={"reference": reference},
            )
        digest = match.group(1)
        if expected_digest is not None and digest != expected_digest:
            raise MistyAIError(
                "evidence reference does not match declared digest",
                code="evidence_digest_mismatch",
                details={"expected": expected_digest, "actual": digest},
            )
        normalized_expected = (
            self._normalize_media_type(expected_media_type)
            if expected_media_type is not None
            else None
        )
        cached_media_type = self._verified_evidence.get(digest)
        if cached_media_type is not None:
            if normalized_expected is not None and cached_media_type != normalized_expected:
                raise MistyAIError(
                    "evidence media type does not match observation metadata",
                    code="evidence_media_type_mismatch",
                    details={"expected": normalized_expected, "actual": cached_media_type},
                )
            self._verified_evidence.move_to_end(digest)
            return digest
        await self.get_evidence(digest, expected_media_type=normalized_expected)
        return digest

    async def get_evidence(
        self,
        digest: str,
        *,
        expected_media_type: str | None = None,
    ) -> bytes:
        if EVIDENCE_DIGEST_PATTERN.fullmatch(digest) is None:
            raise MistyAIError(
                "evidence digest must be 64 lowercase hexadecimal characters",
                code="invalid_evidence_digest",
                details={"digest": digest},
            )
        normalized_expected = (
            self._normalize_media_type(expected_media_type)
            if expected_media_type is not None
            else None
        )
        try:
            async with asyncio.timeout(self.evidence_timeout_seconds):
                async with self.client.stream(
                    "GET",
                    f"/v1/evidence/{digest}",
                    timeout=self.evidence_timeout_seconds,
                ) as response:
                    if not response.is_success:
                        raise MistyAIError(
                            f"evidence download returned HTTP {response.status_code}",
                            status_code=response.status_code,
                            code="evidence_download_failed",
                            retryable=response.status_code >= 500,
                        )
                    media_type = self._normalize_media_type(response.headers.get("content-type"))
                    if normalized_expected is not None and media_type != normalized_expected:
                        raise MistyAIError(
                            "evidence media type does not match observation metadata",
                            code="evidence_media_type_mismatch",
                            details={"expected": normalized_expected, "actual": media_type},
                        )
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_length = int(content_length)
                        except ValueError as exc:
                            raise MistyAIError(
                                "evidence response has an invalid Content-Length",
                                code="invalid_evidence_content_length",
                            ) from exc
                        if declared_length < 0 or declared_length > self.max_evidence_bytes:
                            raise MistyAIError(
                                "evidence response exceeds the configured size limit",
                                code="evidence_too_large",
                                details={
                                    "declared_bytes": declared_length,
                                    "maximum_bytes": self.max_evidence_bytes,
                                },
                            )
                    content = bytearray()
                    hasher = hashlib.sha256()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > self.max_evidence_bytes:
                            raise MistyAIError(
                                "evidence response exceeds the configured size limit",
                                code="evidence_too_large",
                                details={"maximum_bytes": self.max_evidence_bytes},
                            )
                        content.extend(chunk)
                        hasher.update(chunk)
        except MistyAIError:
            raise
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
            raise MistyAIError(
                str(exc) or type(exc).__name__,
                code="evidence_download_failed",
            ) from exc
        actual_digest = hasher.hexdigest()
        if actual_digest != digest:
            raise MistyAIError(
                "downloaded evidence failed SHA-256 verification",
                code="evidence_digest_mismatch",
                details={"expected": digest, "actual": actual_digest},
            )
        self._remember_verified_evidence(digest, media_type)
        return bytes(content)

    @staticmethod
    def _normalize_media_type(value: str | None) -> str:
        media_type = (value or "").partition(";")[0].strip().lower()
        if MEDIA_TYPE_PATTERN.fullmatch(media_type) is None:
            raise MistyAIError(
                "evidence response has an invalid Content-Type",
                code="invalid_evidence_media_type",
                details={"content_type": value},
            )
        return media_type

    def _remember_verified_evidence(self, digest: str, media_type: str) -> None:
        self._verified_evidence[digest] = media_type
        self._verified_evidence.move_to_end(digest)
        while len(self._verified_evidence) > self.evidence_cache_entries:
            self._verified_evidence.popitem(last=False)

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
            try:
                payload = response.json()
            except Exception as exc:
                raise MistyAIError(
                    "provider returned invalid JSON",
                    code="invalid_provider_response",
                    details={"path": path, "error_type": type(exc).__name__},
                ) from exc
            if not isinstance(payload, dict):
                raise MistyAIError(
                    "provider returned a non-object JSON response",
                    code="invalid_provider_response",
                    details={"path": path},
                )
            return payload
        raise self._response_error(response)

    @staticmethod
    def _response_error(response: httpx.Response) -> MistyAIError:
        try:
            native = NativeError.model_validate(response.json())
            detail = native.error
            return MistyAIError(
                detail.message,
                status_code=response.status_code,
                code=detail.code,
                retryable=detail.retryable,
                details=detail.details,
            )
        except Exception as exc:
            return MistyAIError(
                f"provider HTTP {response.status_code}",
                status_code=response.status_code,
                code="invalid_error_response",
                details={"parse_error": type(exc).__name__},
            )


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
        evidence_unverified = exc.code.startswith("evidence_") or exc.code in {
            "invalid_evidence_ref",
            "invalid_evidence_digest",
            "invalid_evidence_content_length",
            "invalid_provider_response",
        }
        unknown = evidence_unverified or reconcile or (exc.uncertain_post and not deterministic)
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
        action = HealthStatus(native.action.status)
        restricted = native.agency_state == "TERMINAL" or native.manual_takeover_active
        if native.manual_takeover_active:
            observation = HealthStatus.UNAVAILABLE
        action_execute = HealthStatus.UNAVAILABLE if restricted else action
        overall = HealthStatus(native.status)
        if restricted and overall == HealthStatus.HEALTHY:
            overall = HealthStatus.DEGRADED
        mapping = {
            ACTION_EXECUTE: action_execute,
            ACTION_SAFE_POINT: action,
            ACTION_VERIFY: action,
            ACTION_RECONCILE: action,
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
            status=overall,
            capability_health=mapping,
            observed_at=native.checked_at,
            detail={
                "active_action_id": native.active_action_id,
                "manual_takeover_active": native.manual_takeover_active,
                "agency_state": native.agency_state,
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
            energy=EnergyState(
                capacity_units=native.energy.capacity_units,
                available_units=native.energy.available_units,
                available_ratio=native.energy.available_ratio,
                delta_units_per_minute=native.energy.delta_units_per_minute,
                charging=native.energy.charging,
                estimated_time_to_empty_seconds=(
                    native.energy.estimated_time_to_terminal_seconds
                ),
                terminal=native.energy.terminal,
            ),
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
