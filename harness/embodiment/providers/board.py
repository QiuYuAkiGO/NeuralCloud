"""Negotiated board operations; transport owns no cognition or retrying writer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import hashlib
import ipaddress
import json
from pathlib import Path
import threading
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from harness.embodiment.board_contracts import (
    BoardAppendRequest, BoardEntry, BoardEvidenceRequest, BoardReadRequest, BoardReadResult, BoardReceipt,
    BoardReconcileRequest, BoardReconcileResult, BoardScope, BoardStatus,
)
from harness.embodiment.capabilities import (
    BOARD_APPEND, BOARD_READ, BOARD_RECONCILE, EVIDENCE_REFERENCE, EmbodimentState,
    HealthStatus, ProviderCapabilities, ProviderHealth,
)
from harness.embodiment.manager import EmbodimentManager
from harness.research.manifest import EmbodimentCondition

BOARD_CAPABILITIES = (BOARD_READ, BOARD_APPEND, BOARD_RECONCILE, EVIDENCE_REFERENCE)
PROVIDER_ID = "misty-linux-thought-board"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class BoardError(RuntimeError):
    """Stable diagnostics never include board text, token, or raw HTTP bodies."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BoardTransport(Protocol):
    async def capabilities(self) -> ProviderCapabilities: ...
    async def health(self) -> ProviderHealth: ...
    async def read(self, request: BoardReadRequest) -> BoardReadResult: ...
    async def append(self, request: BoardAppendRequest) -> BoardReceipt: ...
    async def reconcile(self, request: BoardReconcileRequest) -> BoardReconcileResult: ...
    async def evidence(self, request: BoardEvidenceRequest) -> BoardEntry: ...
    async def close(self) -> None: ...


class BoardClient:
    """Connection-reusing private HTTP transport, with redirects and retries disabled."""

    def __init__(self, base_url: str, token: str | None = None, *, token_file: str | Path | None = None,
                 timeout_seconds: float = 5, transport: httpx.AsyncBaseTransport | None = None):
        parts = urlsplit(base_url)
        try:
            loopback = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if not loopback or parts.scheme != "http" or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
            raise ValueError("board endpoint must be a private loopback HTTP origin")
        if (token is None) == (token_file is None):
            raise ValueError("board client requires exactly one credential source")
        secret = Path(token_file).read_text(encoding="utf-8").strip() if token_file is not None else token
        if not isinstance(secret, str) or not 32 <= len(secret) <= 4096 or any(not 33 <= ord(char) <= 126 for char in secret):
            raise ValueError("secure_board_token_required")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("invalid_board_timeout")
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": "Bearer " + secret},
            timeout=timeout_seconds, follow_redirects=False, trust_env=False, transport=transport)

    async def _request(self, method: str, route: str, model, request=None):
        try:
            response = await self._client.request(method, route, json=request.model_dump(mode="json") if request is not None else None)
        except httpx.HTTPError as exc:
            raise BoardError("board_transport_unavailable") from exc
        codes = {401: "board_authentication_failed", 403: "scope_not_allowed", 404: "board_entry_not_found",
                 409: "board_idempotency_conflict", 408: "request_expired", 503: "board_unavailable"}
        if response.status_code != 200:
            raise BoardError(codes.get(response.status_code, "board_http_error"))
        try:
            return model.model_validate(response.json())
        except Exception as exc:
            raise BoardError("invalid_board_response") from exc

    async def capabilities(self):
        return await self._request("GET", "/v1/capabilities", ProviderCapabilities)

    async def health(self):
        return await self._request("GET", "/health", ProviderHealth)

    async def status(self):
        return await self._request("GET", "/v1/status", BoardStatus)

    async def read(self, request: BoardReadRequest):
        return await self._request("POST", "/v1/board/read", BoardReadResult, request)

    async def append(self, request: BoardAppendRequest):
        return await self._request("POST", "/v1/board/append", BoardReceipt, request)

    async def reconcile(self, request: BoardReconcileRequest):
        return await self._request("POST", "/v1/board/reconcile", BoardReconcileResult, request)

    async def evidence(self, request: BoardEvidenceRequest):
        return await self._request("POST", "/v1/board/evidence", BoardEntry, request)

    async def close(self):
        await self._client.aclose()


BoardHTTPClient = BoardClient


class BoardProvider:
    def __init__(self, condition: EmbodimentCondition, client: BoardTransport, scope: BoardScope, *,
                 clock: Callable[[], datetime] = _utc_now):
        self.scope = BoardScope.model_validate(scope.model_dump())
        self.client = client
        self._clock = clock
        self.manager = EmbodimentManager(condition.model_copy(deep=True), self)
        self._condition_hash = _digest(self.manager.condition.model_dump(mode="json"))
        self._scope_hash = _digest(self.scope.model_dump())
        self._capability_hash: str | None = None
        self._activated = False
        self._uncertain: dict[str, BoardAppendRequest] = {}
        self._write_lock = threading.Lock()

    async def _call(self, method, *args, deadline: datetime):
        remaining = (deadline - self._clock()).total_seconds()
        if remaining <= 0:
            raise BoardError("request_expired")
        try:
            result = await asyncio.wait_for(method(*args), timeout=remaining)
        except BoardError:
            raise
        except TimeoutError as exc:
            raise BoardError("request_expired") from exc
        except Exception as exc:
            code = getattr(exc, "code", "board_driver_unavailable")
            if code not in {"scope_not_allowed", "board_idempotency_conflict", "board_entry_not_found", "request_expired"}:
                code = "board_driver_unavailable"
            raise BoardError(code) from exc
        if self._clock() >= deadline:
            raise BoardError("request_expired")
        return result

    async def capabilities(self) -> ProviderCapabilities:
        result = await self._call(self.client.capabilities, deadline=self._clock() + timedelta(seconds=3))
        value = ProviderCapabilities.model_validate(result.model_dump())
        if value.provider_id != PROVIDER_ID or value.body_instance_id != self.scope.body_instance_id:
            raise BoardError("board_identity_mismatch")
        for descriptor in value.capabilities:
            if descriptor.capability in BOARD_CAPABILITIES and (
                    descriptor.limits.get("board_id") != self.scope.board_id
                    or descriptor.limits.get("owner_id") != self.scope.owner_id):
                raise BoardError("board_identity_mismatch")
        if not -2 <= (self._clock() - value.observed_at).total_seconds() <= 5:
            raise BoardError("stale_board_capabilities")
        identity = _digest(value.model_dump(mode="json", exclude={"observed_at"}))
        if self._capability_hash is not None and identity != self._capability_hash:
            self.manager.set_terminal("board_provider_activation_changed")
            raise BoardError("provider_activation_changed")
        return value

    async def health(self) -> ProviderHealth:
        try:
            result = await self._call(self.client.health, deadline=self._clock() + timedelta(seconds=3))
            value = ProviderHealth.model_validate(result.model_dump())
            if (value.provider_id != PROVIDER_ID or value.body_instance_id != self.scope.body_instance_id
                    or not -2 <= (self._clock() - value.observed_at).total_seconds() <= 5):
                raise BoardError("invalid_board_health")
            if self._uncertain:
                health = dict(value.capability_health)
                health[BOARD_APPEND] = HealthStatus.UNAVAILABLE
                value = value.model_copy(update={"capability_health": health,
                    "status": HealthStatus.DEGRADED if value.status != HealthStatus.UNAVAILABLE else value.status})
            return value
        except Exception:
            return ProviderHealth(provider_id=PROVIDER_ID, body_instance_id=self.scope.body_instance_id,
                status=HealthStatus.UNAVAILABLE, observed_at=self._clock(), detail={"reason_code": "board_health_unavailable"})

    async def activate(self) -> BoardStatus:
        # A provider that has never negotiated anything can retry discovery.
        # Once a revision was negotiated, health refresh cannot expand it.
        if self._activated and self.manager.status().negotiated is not None:
            return await self.status()
        self._activated = True
        try:
            initial = await self.capabilities()
            self._capability_hash = _digest(initial.model_dump(mode="json", exclude={"observed_at"}))
        except Exception:
            pass
        await self.manager.activate()
        # If the first fetch failed, activation may recover but permissions stay
        # frozen to the successfully negotiated revision.
        if self._capability_hash is None and self.manager.status().negotiated is not None:
            initial = await self.capabilities()
            self._capability_hash = _digest(initial.model_dump(mode="json", exclude={"observed_at"}))
        return await self.status()

    async def _refresh(self):
        if not self._activated:
            raise BoardError("provider_not_activated")
        if (_digest(self.manager.condition.model_dump(mode="json")) != self._condition_hash
                or _digest(self.scope.model_dump()) != self._scope_hash):
            self.manager.set_terminal("board_provider_activation_changed")
            raise BoardError("provider_activation_changed")
        return await self.manager.refresh()

    async def status(self) -> BoardStatus:
        if self._activated:
            try:
                await self._refresh()
            except BoardError:
                pass
        return BoardStatus(scope=self.scope, embodiment=self.manager.status(), uncertain_entries=tuple(sorted(self._uncertain)))

    def _request_time(self, request):
        if request.scope != self.scope:
            raise BoardError("scope_not_allowed")
        if request.requested_at > self._clock() + timedelta(seconds=2):
            raise BoardError("request_time_invalid")
        if self._clock() >= request.deadline:
            raise BoardError("request_expired")

    async def _guard(self, request, capability: str):
        self._request_time(request)
        state = await self._call(self._refresh, deadline=request.deadline)
        if (state.negotiated is None or not state.negotiated.required_satisfied
                or state.state in {EmbodimentState.DISABLED, EmbodimentState.TERMINAL}
                or not self.manager.supports(capability) or not self.manager.supports(EVIDENCE_REFERENCE)):
            raise BoardError("capability_unavailable")
        if capability == BOARD_APPEND and (self.manager.condition.mode not in {"active", "grounded"}
                or state.state not in {EmbodimentState.AVAILABLE, EmbodimentState.DEGRADED}):
            raise BoardError("capability_unavailable")
        self._request_time(request)

    def _receipt(self, receipt: BoardReceipt, *, entry_id: str, decision_id: str, text_sha256: str):
        if (receipt.scope != self.scope or receipt.entry_id != entry_id or receipt.decision_id != decision_id
                or receipt.text_sha256 != text_sha256 or receipt.created_at > self._clock() + timedelta(seconds=2)
                or not receipt.evidence_ref.startswith("board-evidence://sha256/")):
            raise BoardError("board_receipt_mismatch")

    def _entry(self, entry: BoardEntry):
        self._receipt(entry.receipt, entry_id=entry.receipt.entry_id, decision_id=entry.receipt.decision_id,
                      text_sha256=hashlib.sha256(entry.text.encode("utf-8")).hexdigest())

    async def read(self, request: BoardReadRequest) -> BoardReadResult:
        request = BoardReadRequest.model_validate(request.model_dump())
        await self._guard(request, BOARD_READ)
        result = BoardReadResult.model_validate((await self._call(self.client.read, request, deadline=request.deadline)).model_dump())
        if (result.scope != self.scope or len(result.entries) > request.limit
                or not -2 <= (self._clock() - result.observed_at).total_seconds() <= 5):
            raise BoardError("board_read_mismatch")
        sequence = request.after_sequence
        for entry in result.entries:
            self._entry(entry)
            if entry.receipt.sequence <= sequence:
                raise BoardError("board_read_mismatch")
            sequence = entry.receipt.sequence
        return result

    async def append(self, request: BoardAppendRequest, *, pre_effect_guard: Callable[[], bool] | None = None) -> BoardReceipt:
        request = BoardAppendRequest.model_validate(request.model_dump())
        if not self._write_lock.acquire(blocking=False):
            raise BoardError("board_write_busy")
        entered = False
        try:
            await self._guard(request, BOARD_APPEND)
            async def dispatch_append():
                nonlocal entered
                if pre_effect_guard is not None and not pre_effect_guard():
                    raise BoardError("board_pre_effect_cancelled")
                self._request_time(request)
                entered = True
                return await self.client.append(request)
            receipt = BoardReceipt.model_validate((await self._call(dispatch_append, deadline=request.deadline)).model_dump())
            self._receipt(receipt, entry_id=request.entry_id, decision_id=request.decision_id,
                          text_sha256=hashlib.sha256(request.text.encode("utf-8")).hexdigest())
            now = self._clock()
            evidence_request = BoardEvidenceRequest(scope=self.scope, entry_id=request.entry_id, requested_at=now,
                deadline=min(request.deadline, now + timedelta(seconds=15)))
            evidence = BoardEntry.model_validate((await self._call(self.client.evidence, evidence_request, deadline=request.deadline)).model_dump())
            self._entry(evidence)
            if (evidence.receipt != receipt or evidence.text != request.text or evidence.title != request.title
                    or evidence.source_event_ids != request.source_event_ids):
                raise BoardError("board_receipt_mismatch")
            return receipt
        except BaseException as exc:
            # An explicit conflict is a proven no-op. Timeouts, cancellation and
            # malformed success responses can hide a durable commit.
            if entered and not (isinstance(exc, BoardError) and exc.code in {
                "board_idempotency_conflict", "scope_not_allowed", "board_authentication_failed",
            }):
                self._uncertain[request.entry_id] = request
                if isinstance(exc, Exception):
                    raise BoardError("board_append_outcome_unknown") from exc
            raise
        finally:
            self._write_lock.release()

    async def reconcile(self, request: BoardReconcileRequest) -> BoardReconcileResult:
        request = BoardReconcileRequest.model_validate(request.model_dump())
        # Reconciliation must remain possible while append is unavailable.
        await self._guard(request, BOARD_RECONCILE)
        result = BoardReconcileResult.model_validate((await self._call(self.client.reconcile, request, deadline=request.deadline)).model_dump())
        if result.scope != request.scope or result.entry_id != request.entry_id or result.decision_id != request.decision_id:
            raise BoardError("board_receipt_mismatch")
        if result.receipt is not None:
            self._receipt(result.receipt, entry_id=request.entry_id, decision_id=request.decision_id, text_sha256=request.text_sha256)
            now = self._clock()
            evidence_request = BoardEvidenceRequest(scope=self.scope, entry_id=request.entry_id, requested_at=now,
                deadline=min(request.deadline, now + timedelta(seconds=15)))
            entry = BoardEntry.model_validate((await self._call(self.client.evidence, evidence_request, deadline=request.deadline)).model_dump())
            self._entry(entry)
            if entry.receipt != result.receipt:
                raise BoardError("board_receipt_mismatch")
            frozen = self._uncertain.get(request.entry_id)
            if frozen is not None:
                if (frozen.decision_id != request.decision_id or frozen.text != entry.text
                        or frozen.title != entry.title or frozen.source_event_ids != entry.source_event_ids):
                    raise BoardError("board_receipt_mismatch")
                self._uncertain.pop(request.entry_id)
        return result

    async def evidence(self, request: BoardEvidenceRequest) -> BoardEntry:
        request = BoardEvidenceRequest.model_validate(request.model_dump())
        await self._guard(request, EVIDENCE_REFERENCE)
        entry = BoardEntry.model_validate((await self._call(self.client.evidence, request, deadline=request.deadline)).model_dump())
        self._entry(entry)
        if entry.receipt.entry_id != request.entry_id:
            raise BoardError("board_receipt_mismatch")
        return entry

    async def close(self):
        await self.client.close()
