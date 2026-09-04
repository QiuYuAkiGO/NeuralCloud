"""Strict MistyAI body-contract-v1 HTTP client and polling monitor."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from harness.contracts.body import (
    BODY_CONTRACT_VERSION,
    BodyStateCaptureResponse,
    BodyStateSnapshot,
    MistyCapabilities,
    evidence_digest,
)


class BodyMonitorError(RuntimeError):
    pass


class MistyBodyClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def validate_contract(self, *, body_instance_id: str) -> MistyCapabilities:
        payload = await self._json("GET", "/v1/capabilities")
        capabilities = MistyCapabilities.model_validate(payload)
        if capabilities.schema_version != BODY_CONTRACT_VERSION:
            raise BodyMonitorError("MistyAI body contract version mismatch")
        if BODY_CONTRACT_VERSION not in capabilities.contract_versions:
            raise BodyMonitorError("MistyAI did not advertise body-contract-v1")
        if capabilities.body_instance_id != body_instance_id:
            raise BodyMonitorError("MistyAI body_instance_id mismatch")
        required = {"body_state_v1", "virtual_resource_ledger"}
        missing = [name for name in required if not capabilities.extensions.get(name, False)]
        if missing:
            raise BodyMonitorError(f"MistyAI body capabilities unavailable: {', '.join(missing)}")
        if capabilities.extensions.get("experiment_control", False):
            raise BodyMonitorError("ordinary MistyAI service token exposed experiment control")
        return capabilities

    async def capture(self) -> BodyStateSnapshot:
        payload = await self._json(
            "POST",
            "/v1/body-state:capture",
            json={"schema_version": BODY_CONTRACT_VERSION},
        )
        return BodyStateCaptureResponse.model_validate(payload).body_state

    async def verify_evidence(self, snapshot: BodyStateSnapshot) -> None:
        for reference in snapshot.evidence_refs:
            digest = evidence_digest(reference)
            try:
                response = await self.client.get(f"/v1/evidence/{digest}")
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise BodyMonitorError(f"body evidence download failed: {digest}") from exc
            if hashlib.sha256(response.content).hexdigest() != digest:
                raise BodyMonitorError(f"body evidence digest mismatch: {digest}")

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, object]:
        try:
            response = await self.client.request(method, path, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BodyMonitorError(f"MistyAI {method} {path} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise BodyMonitorError("MistyAI returned a non-object response")
        return payload


class MistyBodyMonitor:
    def __init__(
        self,
        *,
        client: MistyBodyClient,
        body_instance_id: str,
        poll_interval_seconds: float,
        max_state_age_seconds: float,
        future_clock_tolerance_seconds: float,
        on_snapshot: Callable[[BodyStateSnapshot], Awaitable[None]],
        on_degraded: Callable[[str], Awaitable[None]],
        on_recovered: Callable[[], Awaitable[None]],
    ) -> None:
        self.client = client
        self.body_instance_id = body_instance_id
        self.poll_interval_seconds = poll_interval_seconds
        self.max_state_age = timedelta(seconds=max_state_age_seconds)
        self.future_tolerance = timedelta(seconds=future_clock_tolerance_seconds)
        self.on_snapshot = on_snapshot
        self.on_degraded = on_degraded
        self.on_recovered = on_recovered
        self._last_snapshot: BodyStateSnapshot | None = None
        self._last_fingerprint: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._degraded = False

    @property
    def last_snapshot(self) -> BodyStateSnapshot | None:
        return self._last_snapshot

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.client.validate_contract(body_instance_id=self.body_instance_id)
        await self.poll_once()
        self._task = asyncio.create_task(self._run(), name="mistyai-body-monitor")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.client.close()

    async def poll_once(self, *, now: datetime | None = None) -> BodyStateSnapshot | None:
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        snapshot = await self.client.capture()
        if snapshot.body_instance_id != self.body_instance_id:
            raise BodyMonitorError("captured body_instance_id mismatch")
        captured_at = snapshot.captured_at.astimezone(UTC)
        if current_time - captured_at > self.max_state_age:
            raise BodyMonitorError("captured body state is stale")
        if captured_at - current_time > self.future_tolerance:
            raise BodyMonitorError("captured body state is from the future")
        await self.client.verify_evidence(snapshot)
        fingerprint = hashlib.sha256(
            json.dumps(
                snapshot.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        previous = self._last_snapshot
        if previous is not None:
            if snapshot.revision < previous.revision:
                raise BodyMonitorError("body revision moved backwards")
            if snapshot.revision == previous.revision:
                if fingerprint != self._last_fingerprint:
                    raise BodyMonitorError("same body revision returned different content")
                return None
        await self.on_snapshot(snapshot)
        self._last_snapshot = snapshot
        self._last_fingerprint = fingerprint
        if self._degraded:
            self._degraded = False
            await self.on_recovered()
        return snapshot

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval_seconds)
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._degraded = True
                await self.on_degraded(f"{type(exc).__name__}: {exc}")
