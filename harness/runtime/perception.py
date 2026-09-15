"""Long-running perception guard with pause-time observation aggregation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from harness.embodiment.ports import PerceptionBackend
from harness.runtime.models import ResumeObservation, RuntimeEvent, RuntimeEventPriority, RuntimeEventType


SubmitPerception = Callable[..., Awaitable[RuntimeEvent | None]]


class PauseObservationBuffer:
    def __init__(self, *, significant_limit: int = 16) -> None:
        self.significant_limit = significant_limit
        self.clear()

    def clear(self) -> None:
        self.paused_at: datetime | None = None
        self.first_receive_seq: int | None = None
        self.last_receive_seq: int | None = None
        self.count = 0
        self.latest_world: dict[str, Any] = {}
        self.significant_changes: list[dict[str, Any]] = []

    def begin(self, paused_at: datetime) -> None:
        self.clear()
        self.paused_at = paused_at

    def add(self, event: RuntimeEvent) -> None:
        if self.paused_at is None:
            self.paused_at = event.received_at
        first, last = event.sequence_range
        self.first_receive_seq = first if self.first_receive_seq is None else min(self.first_receive_seq, first)
        self.last_receive_seq = last if self.last_receive_seq is None else max(self.last_receive_seq, last)
        self.count += event.coalesced_count
        self.latest_world = dict(event.payload)
        if event.event_type == RuntimeEventType.ENVIRONMENT_CHANGE and len(self.significant_changes) < self.significant_limit:
            self.significant_changes.append(dict(event.payload))

    def build(self, *, resumed_at: datetime) -> ResumeObservation | None:
        if self.count == 0 or self.paused_at is None:
            return None
        assert self.first_receive_seq is not None
        assert self.last_receive_seq is not None
        return ResumeObservation(
            latest_world=self.latest_world,
            significant_changes=self.significant_changes,
            first_receive_seq=self.first_receive_seq,
            last_receive_seq=self.last_receive_seq,
            observation_count=self.count,
            paused_at=self.paused_at,
            resumed_at=resumed_at,
            duration_seconds=max(0.0, (resumed_at - self.paused_at).total_seconds()),
        )


class PerceptionGuard:
    _VOLATILE_FIELDS = {
        "captured_at",
        "created_at",
        "freshness_seconds",
        "observation_id",
        "observed_at",
    }

    def __init__(
        self,
        backend: PerceptionBackend,
        submit: SubmitPerception,
        *,
        interval_seconds: float = 5.0,
        max_state_age_seconds: float | None = None,
        source: str = "perception",
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_state_age_seconds is not None and max_state_age_seconds <= 0:
            raise ValueError("max_state_age_seconds must be positive")
        self.backend = backend
        self.submit = submit
        self.interval_seconds = interval_seconds
        self.max_state_age_seconds = max_state_age_seconds
        self.source = source
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_fingerprint: str | None = None
        self._last_stale: bool | None = None
        self.submitted_observations = 0
        self.suppressed_observations = 0

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._last_fingerprint = None
            self._last_stale = None
            self._task = asyncio.create_task(self.run(), name="runtime-perception-guard")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self.interval_seconds + 1)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)

    async def run(self) -> None:
        while not self._stop.is_set():
            moment = datetime.now(UTC)
            try:
                observation = await self.backend.observe(now=moment)
                payload = self._normalise(observation)
                stale = self._is_stale(payload)
                if stale:
                    if self._last_stale is not True:
                        await self._submit_freshness_diagnostic(moment, stale=True, payload=payload)
                    self._last_stale = True
                    self.suppressed_observations += 1
                else:
                    if self._last_stale is True:
                        await self._submit_freshness_diagnostic(moment, stale=False, payload=payload)
                        self._last_fingerprint = None
                    self._last_stale = False
                    if self._is_semantic_change(payload):
                        await self.submit(
                            event_type=RuntimeEventType.PERCEPTION,
                            priority=RuntimeEventPriority.PERCEPTION,
                            source=self.source,
                            source_time=moment,
                            payload=payload,
                        )
                        self.submitted_observations += 1
                    else:
                        self.suppressed_observations += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The first healthy observation after an outage must be emitted,
                # even when its semantic content matches the pre-outage state.
                self._last_fingerprint = None
                self._last_stale = None
                await self.submit(
                    event_type=RuntimeEventType.DIAGNOSTIC,
                    priority=RuntimeEventPriority.CONTROL,
                    source=self.source,
                    source_time=moment,
                    payload={"perception_error": str(exc)},
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    @staticmethod
    def _normalise(observation: Any) -> dict[str, Any]:
        if isinstance(observation, BaseModel):
            return observation.model_dump(mode="json")
        if isinstance(observation, dict):
            return dict(observation)
        return {"value": observation}

    def _is_semantic_change(self, payload: dict[str, Any]) -> bool:
        fingerprint = self._semantic_fingerprint(payload)
        if fingerprint == self._last_fingerprint:
            return False
        self._last_fingerprint = fingerprint
        return True

    def _is_stale(self, payload: dict[str, Any]) -> bool:
        if self.max_state_age_seconds is None:
            return False
        raw = payload.get("freshness_seconds")
        if raw is None:
            return False
        try:
            freshness = float(raw)
        except (TypeError, ValueError):
            return False
        return math.isfinite(freshness) and freshness > self.max_state_age_seconds

    async def _submit_freshness_diagnostic(
        self,
        moment: datetime,
        *,
        stale: bool,
        payload: dict[str, Any],
    ) -> None:
        await self.submit(
            event_type=RuntimeEventType.DIAGNOSTIC,
            priority=RuntimeEventPriority.CONTROL,
            source=self.source,
            source_time=moment,
            payload={
                "perception_stale": stale,
                "freshness_seconds": payload.get("freshness_seconds"),
                "max_state_age_seconds": self.max_state_age_seconds,
            },
        )

    @classmethod
    def _semantic_fingerprint(cls, payload: dict[str, Any]) -> str:
        semantic = cls._semantic_value(payload)
        encoded = json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _semantic_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            digest = value.get("digest")
            evidence_ref = (
                "ref" in value
                and isinstance(digest, str)
                and bool(digest.strip())
            )
            return {
                key: cls._semantic_value(item)
                for key, item in value.items()
                if key not in cls._VOLATILE_FIELDS
                and not (evidence_ref and key == "ref")
            }
        if isinstance(value, list):
            return [cls._semantic_value(item) for item in value]
        return value
