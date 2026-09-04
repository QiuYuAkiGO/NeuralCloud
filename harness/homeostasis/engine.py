"""Deterministic seven-factor homeostasis domain service."""

from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal, cast

from harness.contracts.envelope import DomainCommandEnvelope, DomainCommandResult, VersionVector
from harness.embodiment.contracts import (
    HomeostaticObservation as EmbodimentHomeostaticObservation,
    evidence_ref_strings,
)
from harness.homeostasis.models import (
    DIMENSIONS,
    CoreAffect,
    DerivedEmotion,
    DimensionName,
    EventAppraisal,
    ExpectedFreeEnergyBreakdown,
    FactorBelief,
    HomeostaticObservation as LegacyHomeostaticObservation,
    HomeostaticProfile,
    HomeostaticSnapshot,
    HomeostaticTransitionTrace,
    PolicyCandidate,
    initial_snapshot,
)
from harness.homeostasis.ledger import CommandLedger
from harness.runtime.adapters import DomainSnapshot, ServiceHealth


def utc_now() -> datetime:
    return datetime.now(UTC)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _aware(value: Any, default: datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        result = default
    return result if result.tzinfo is not None else result.replace(tzinfo=UTC)


class HomeostasisEngine:
    """A replayable domain service compatible with ``HomeostasisServiceClient``."""

    def __init__(
        self,
        profile: HomeostaticProfile,
        *,
        state_dir: Path | str,
        now: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
        ledger_cache_size: int = 512,
    ) -> None:
        self.profile = profile
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "snapshot.json"
        self.ledger_path = self.state_dir / "command-ledger.json"
        self.ledger_store_path = self.state_dir / "command-ledger.sqlite3"
        self.trace_path = self.state_dir / "transitions.jsonl"
        self._lock = asyncio.Lock()
        self._projection_errors: dict[str, str] = {}
        self._boundary_heartbeat_streaks: dict[DimensionName, int] = {}
        self._boundary_heartbeat_sides: dict[
            DimensionName, Literal["low", "high"]
        ] = {}
        self._command_ledger = CommandLedger(
            self.state_dir,
            cache_size=ledger_cache_size,
            legacy_filename=self.ledger_path.name,
            filename=self.ledger_store_path.name,
        )
        self._clock = clock or ((lambda: now) if now is not None else utc_now)
        self._started_at = now or self._clock()
        self.snapshot = initial_snapshot(profile, now=self._started_at)
        self._load()
        self._validate_profile_identity()
        self.snapshot = self.snapshot.model_copy(
            update={"candidates": self._rank_policies(self.snapshot.dimensions)}
        )

    async def health(self) -> ServiceHealth:
        async with self._lock:
            return ServiceHealth(
                service="homeostasis",
                healthy=not self.snapshot.watchdog and not self._projection_errors,
                revision=self.snapshot.revision,
                acknowledged_receive_seq=self.snapshot.acknowledged_receive_seq,
                detail={
                    "profile_id": self.profile.profile_id,
                    "profile_revision": self.profile.revision,
                    "profile_ref": self.profile.profile_ref,
                    "watchdog": list(self.snapshot.watchdog),
                    "projection_errors": dict(self._projection_errors),
                    "boundary_heartbeat_streaks": dict(
                        self._boundary_heartbeat_streaks
                    ),
                },
            )

    async def get_snapshot(self) -> DomainSnapshot:
        async with self._lock:
            return DomainSnapshot(
                service="homeostasis",
                revision=self.snapshot.revision,
                acknowledged_receive_seq=self.snapshot.acknowledged_receive_seq,
                acknowledged_event_id=self.snapshot.acknowledged_event_id,
                component_revisions={"state": self.snapshot.revision},
                payload={
                    **self.snapshot.model_dump(mode="json"),
                    "profile": self.profile.model_dump(mode="json"),
                },
            )

    async def apply_event(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command, kind="event")

    async def apply_heartbeat(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command, kind="heartbeat")

    async def apply_outcome(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command, kind="outcome")

    async def assimilate_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        """Assimilate measured state without interpreting it as an event delta."""
        return await self._assimilate_observation(command, legacy=False)

    async def apply_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        """Assimilate an absolute sensor reading without event caps or coupling."""
        return await self._assimilate_observation(command, legacy=True)

    async def _assimilate_observation(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
        *,
        legacy: bool,
    ) -> DomainCommandResult[dict[str, Any]]:

        async with self._lock:
            request_hash = command.effective_request_hash()
            key = f"{command.command_type}\0{command.idempotency_key}"
            previous = self._command_ledger.get(key)
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    return self._failure(
                        command,
                        request_hash,
                        "idempotency key reused with different request",
                        "conflict",
                    )
                self._repair_projections_if_needed()
                restored = DomainCommandResult[dict[str, Any]].model_validate(previous["result"])
                return restored.model_copy(update={"status": "idempotent_replay", "idempotency_hit": True})
            if command.is_expired(now=self._clock()):
                return self._failure(command, request_hash, "command deadline expired", "rejected")
            if command.expected_revision is not None and command.expected_revision != self.snapshot.revision:
                return self._failure(
                    command,
                    request_hash,
                    f"expected revision {command.expected_revision}, current {self.snapshot.revision}",
                    "conflict",
                )

            raw = command.payload.get("observation") or command.payload
            if legacy:
                observation = LegacyHomeostaticObservation.model_validate(raw)
                observed_at = observation.captured_at
                precision = self.profile.source_precision["system_sensor"]
                evidence = list(observation.evidence_refs)
                trace_metadata: dict[str, Any] = {
                    "body_instance_id": observation.body_instance_id,
                    "episode_id": observation.episode_id,
                    "body_revision": observation.body_revision,
                    "experiment_mode": observation.experiment_mode,
                }
            else:
                observation = EmbodimentHomeostaticObservation.model_validate(raw)
                observed_at = observation.observed_at
                precision = observation.precision
                evidence = evidence_ref_strings(observation.evidence_refs)
                trace_metadata = {}
            unknown = set(observation.observed_values) - set(DIMENSIONS)
            if unknown:
                return self._failure(
                    command,
                    request_hash,
                    f"unknown observed dimensions: {sorted(unknown)}",
                    "rejected",
                )
            effective_at = max(observed_at, self.snapshot.updated_at)
            factors = dict(self.snapshot.dimensions)
            applied: dict[DimensionName, float] = (
                {} if legacy else {key: 0.0 for key in DIMENSIONS}
            )
            before: dict[DimensionName, float] = {}
            after: dict[DimensionName, float] = {}
            for dimension_name, measured in observation.observed_values.items():
                dimension = cast(DimensionName, dimension_name)
                current = self.snapshot.dimensions[dimension]
                spec = self.profile.dimensions[dimension]
                before[dimension] = current.value
                value = _clamp(
                    current.value + precision * (float(measured) - current.value),
                    spec.hard_bounds[0],
                    spec.hard_bounds[1],
                )
                after[dimension] = value
                delta = value - current.value
                applied[dimension] = delta
                factors[dimension] = FactorBelief(
                    dimension=dimension,
                    value=value,
                    velocity=delta,
                    posterior=self._posterior(value, spec.baseline_band),
                    target_band=spec.baseline_band,
                    precision=precision,
                    dominant_evidence=(evidence + current.dominant_evidence)[:5],
                    updated_at=effective_at,
                )

            revision = self.snapshot.revision + 1
            boundary_streaks, boundary_sides = self._next_boundary_heartbeat_streaks(
                factors,
                heartbeat=False,
            )
            watchdog = self._watchdog(factors, boundary_streaks)
            affect = self._derive_affect(factors)
            emotion = self._derive_emotion(affect, factors, effective_at)
            next_snapshot = HomeostaticSnapshot(
                revision=revision,
                profile_id=self.profile.profile_id,
                profile_revision=self.profile.revision,
                dimensions=factors,
                core_affect=affect,
                emotion=emotion,
                candidates=self._rank_policies(factors),
                acknowledged_receive_seq=max(
                    self.snapshot.acknowledged_receive_seq, command.receive_seq
                ),
                acknowledged_event_id=command.event_id,
                last_heartbeat_at=self.snapshot.last_heartbeat_at,
                updated_at=effective_at,
                stable_snapshot_ref=(
                    f"snapshot://{self.profile.profile_id}/{revision}"
                    if not watchdog
                    else self.snapshot.stable_snapshot_ref
                ),
                watchdog=watchdog,
            )
            trace = HomeostaticTransitionTrace(
                revision=revision,
                command_type=command.command_type,
                event_id=command.event_id,
                receive_seq=command.receive_seq,
                occurred_at=effective_at,
                source=observation.source,
                precision=precision,
                suggested_impacts=(
                    {} if legacy else {key: 0.0 for key in DIMENSIONS}
                ),
                applied_impacts=applied,
                coupling_impacts=(
                    {} if legacy else {key: 0.0 for key in DIMENSIONS}
                ),
                watchdog=watchdog,
                observed_values=dict(observation.observed_values),
                assimilation_gain=precision,
                values_before=before,
                values_after=after,
                evidence_refs=evidence,
                **trace_metadata,
            )
            result = DomainCommandResult[dict[str, Any]](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=request_hash,
                committed_revision=revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=next_snapshot.acknowledged_receive_seq,
                result={
                    "revision": revision,
                    "snapshot": next_snapshot.model_dump(mode="json"),
                    "trace": trace.model_dump(mode="json"),
                },
                completed_at=self._clock(),
            )
            self._command_ledger.commit(
                key,
                request_hash=request_hash,
                result=result.model_dump(mode="json"),
                snapshot=next_snapshot.model_dump(mode="json"),
                engine_state=self._engine_state_payload(
                    boundary_streaks,
                    boundary_sides,
                ),
                trace=trace.model_dump(mode="json"),
            )
            self.snapshot = next_snapshot
            self._boundary_heartbeat_streaks = boundary_streaks
            self._boundary_heartbeat_sides = boundary_sides
            self._project_snapshot_mirror()
            self._project_trace(trace)
            return result

    async def propose_policies(self, *, version_vector: VersionVector) -> list[dict[str, Any]]:
        del version_vector
        async with self._lock:
            return [item.model_dump(mode="json") for item in self.snapshot.candidates]

    async def close(self) -> None:
        async with self._lock:
            self._persist()

    async def _apply(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
        *,
        kind: Literal["event", "heartbeat", "outcome"],
    ) -> DomainCommandResult[dict[str, Any]]:
        async with self._lock:
            request_hash = command.effective_request_hash()
            key = f"{command.command_type}\0{command.idempotency_key}"
            previous = self._command_ledger.get(key)
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    return self._failure(command, request_hash, "idempotency key reused with different request", "conflict")
                self._repair_projections_if_needed()
                restored = DomainCommandResult[dict[str, Any]].model_validate(previous["result"])
                return restored.model_copy(update={"status": "idempotent_replay", "idempotency_hit": True})
            if command.is_expired(now=self._clock()):
                return self._failure(command, request_hash, "command deadline expired", "rejected")
            if command.expected_revision is not None and command.expected_revision != self.snapshot.revision:
                return self._failure(
                    command,
                    request_hash,
                    f"expected revision {command.expected_revision}, current {self.snapshot.revision}",
                    "conflict",
                )

            event = dict(command.payload.get("event") or command.payload)
            occurred_at = _aware(event.get("source_time") or event.get("observed_at"), command.issued_at)
            effective_at = max(occurred_at, self.snapshot.updated_at)
            appraisal = self._appraise(event, kind=kind, event_id=command.event_id)
            factors, applied, coupling = self._integrate(appraisal, effective_at, heartbeat=kind == "heartbeat")
            affect = self._derive_affect(factors)
            emotion = self._derive_emotion(affect, factors, effective_at)
            boundary_streaks, boundary_sides = self._next_boundary_heartbeat_streaks(
                factors,
                heartbeat=kind == "heartbeat",
            )
            watchdog = self._watchdog(factors, boundary_streaks)
            revision = self.snapshot.revision + 1
            heartbeat_at = effective_at if kind == "heartbeat" else self.snapshot.last_heartbeat_at
            candidates = self._rank_policies(factors)
            next_snapshot = HomeostaticSnapshot(
                revision=revision,
                profile_id=self.profile.profile_id,
                profile_revision=self.profile.revision,
                dimensions=factors,
                core_affect=affect,
                emotion=emotion,
                candidates=candidates,
                acknowledged_receive_seq=max(self.snapshot.acknowledged_receive_seq, command.receive_seq),
                acknowledged_event_id=command.event_id,
                last_heartbeat_at=heartbeat_at,
                updated_at=effective_at,
                stable_snapshot_ref=(
                    f"snapshot://{self.profile.profile_id}/{revision}"
                    if not watchdog
                    else self.snapshot.stable_snapshot_ref
                ),
                watchdog=watchdog,
            )
            trace = HomeostaticTransitionTrace(
                revision=revision,
                command_type=command.command_type,
                event_id=command.event_id,
                receive_seq=command.receive_seq,
                occurred_at=effective_at,
                source=appraisal.source,
                precision=appraisal.precision,
                suggested_impacts=appraisal.suggested_impacts,
                applied_impacts=applied,
                coupling_impacts=coupling,
                watchdog=watchdog,
            )
            result_payload = {
                "revision": revision,
                "snapshot": next_snapshot.model_dump(mode="json"),
                "trace": trace.model_dump(mode="json"),
            }
            result = DomainCommandResult[dict[str, Any]](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=request_hash,
                committed_revision=revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=next_snapshot.acknowledged_receive_seq,
                result=result_payload,
                completed_at=self._clock(),
            )
            self._command_ledger.commit(
                key,
                request_hash=request_hash,
                result=result.model_dump(mode="json"),
                snapshot=next_snapshot.model_dump(mode="json"),
                engine_state=self._engine_state_payload(
                    boundary_streaks,
                    boundary_sides,
                ),
                trace=trace.model_dump(mode="json"),
            )
            self.snapshot = next_snapshot
            self._boundary_heartbeat_streaks = boundary_streaks
            self._boundary_heartbeat_sides = boundary_sides
            self._project_snapshot_mirror()
            self._project_trace(trace)
            return result

    def _appraise(self, event: dict[str, Any], *, kind: str, event_id: str) -> EventAppraisal:
        payload = dict(event.get("payload") or {})
        explicit = payload.get("appraisal") or event.get("appraisal")
        if isinstance(explicit, dict):
            source = str(explicit.get("source") or "runtime")
            precision = float(explicit.get("precision", self.profile.source_precision.get(source, 0.8)))
            impacts = {key: float(value) for key, value in dict(explicit.get("suggested_impacts") or {}).items()}
            return EventAppraisal(
                source=source,
                precision=_clamp(precision, 0.0, 1.0),
                suggested_impacts=impacts,
                evidence_refs=[event_id],
            )
        event_type = str(event.get("event_type") or kind).lower()
        text = str(payload.get("text") or payload.get("summary") or "").lower()
        source = str(event.get("source") or ("verified_outcome" if kind == "outcome" else "runtime"))
        impacts = dict(self.profile.observation_model.get(event_type, {}))
        if kind == "event" and "user" in event_type:
            impacts.setdefault("social_connection", 0.035)
            impacts.setdefault("cognitive_load", min(0.04, len(text) / 4000.0))
        if any(token in text for token in ("谢谢", "喜欢", "支持", "thank", "glad")):
            impacts["social_connection"] = impacts.get("social_connection", 0.0) + 0.04
            impacts["safety_predictability"] = impacts.get("safety_predictability", 0.0) + 0.025
        if any(token in text for token in ("讨厌", "离开", "危险", "失败", "hate", "danger")):
            impacts["safety_predictability"] = impacts.get("safety_predictability", 0.0) - 0.05
            impacts["cognitive_load"] = impacts.get("cognitive_load", 0.0) + 0.04
        if kind == "outcome":
            success = self._outcome_succeeded(event, payload)
            impacts.setdefault("competence", 0.05 if success else -0.04)
            impacts.setdefault("safety_predictability", 0.02 if success else -0.02)
        return EventAppraisal(
            source=source,
            precision=self.profile.source_precision.get(source, self.profile.source_precision.get("runtime", 0.8)),
            suggested_impacts=impacts,
            evidence_refs=[event_id],
        )

    @staticmethod
    def _outcome_succeeded(event: dict[str, Any], payload: dict[str, Any]) -> bool:
        def nested(root: dict[str, Any], *path: str) -> Any:
            value: Any = root
            for key in path:
                if not isinstance(value, dict) or key not in value:
                    return None
                value = value[key]
            return value

        candidates = (
            nested(payload, "success"),
            nested(event, "success"),
            nested(payload, "evidence", "success"),
            nested(event, "evidence", "success"),
            nested(payload, "action", "outcome", "success"),
            nested(event, "action", "outcome", "success"),
            nested(payload, "action", "state"),
            nested(event, "action", "state"),
        )
        for value in candidates:
            if value is None:
                continue
            if isinstance(value, str):
                return value.strip().lower() in {"true", "1", "yes", "succeeded", "success"}
            return bool(value)
        return False

    def _integrate(
        self,
        appraisal: EventAppraisal,
        now: datetime,
        *,
        heartbeat: bool,
    ) -> tuple[dict[DimensionName, FactorBelief], dict[DimensionName, float], dict[DimensionName, float]]:
        elapsed = max(0.0, (now - self.snapshot.updated_at).total_seconds())
        if heartbeat:
            elapsed = min(elapsed, float(self.profile.max_heartbeat_step_seconds))
        else:
            elapsed = 0.0
        requested_base_delta: dict[DimensionName, float] = {
            key: 0.0 for key in DIMENSIONS
        }
        for dimension in DIMENSIONS:
            current = self.snapshot.dimensions[dimension]
            spec = self.profile.dimensions[dimension]
            target = sum(spec.baseline_band) / 2.0
            if elapsed:
                direction = 1.0 if target > current.value else -1.0
                if abs(target - current.value) < 1e-12:
                    direction = 0.0
                requested_base_delta[dimension] += direction * min(
                    abs(target - current.value), spec.drift_per_hour * elapsed / 3600.0
                )
            suggested = float(appraisal.suggested_impacts.get(dimension, 0.0))
            bounded = _clamp(suggested, -spec.max_event_delta, spec.max_event_delta)
            requested_base_delta[dimension] += bounded * appraisal.precision

        base_values: dict[DimensionName, float] = {}
        realised_base_delta: dict[DimensionName, float] = {}
        for dimension in DIMENSIONS:
            current = self.snapshot.dimensions[dimension]
            spec = self.profile.dimensions[dimension]
            base_value = _clamp(
                current.value + requested_base_delta[dimension],
                spec.hard_bounds[0],
                spec.hard_bounds[1],
            )
            base_values[dimension] = base_value
            realised_base_delta[dimension] = base_value - current.value

        coupling_source_delta = (
            realised_base_delta
            if self.profile.coupling_semantics == "realized_delta_v2"
            else requested_base_delta
        )
        requested_coupling: dict[DimensionName, float] = {
            key: 0.0 for key in DIMENSIONS
        }
        for source, value in coupling_source_delta.items():
            for target_dimension, coefficient in self.profile.coupling.get(source, {}).items():
                if target_dimension != source:
                    requested_coupling[target_dimension] += value * float(coefficient)
        factors: dict[DimensionName, FactorBelief] = {}
        applied: dict[DimensionName, float] = {}
        realised_coupling: dict[DimensionName, float] = {}
        for dimension in DIMENSIONS:
            current = self.snapshot.dimensions[dimension]
            spec = self.profile.dimensions[dimension]
            value = _clamp(
                base_values[dimension] + requested_coupling[dimension],
                spec.hard_bounds[0],
                spec.hard_bounds[1],
            )
            delta = value - current.value
            applied[dimension] = delta
            realised_coupling[dimension] = value - base_values[dimension]
            factors[dimension] = FactorBelief(
                dimension=dimension,
                value=value,
                velocity=delta,
                posterior=self._posterior(value, spec.baseline_band),
                target_band=spec.baseline_band,
                precision=appraisal.precision,
                dominant_evidence=(appraisal.evidence_refs + current.dominant_evidence)[:5],
                updated_at=now,
            )
        reported_coupling = (
            realised_coupling
            if self.profile.coupling_semantics == "realized_delta_v2"
            else requested_coupling
        )
        return factors, applied, reported_coupling

    @staticmethod
    def _posterior(
        value: float, target: tuple[float, float]
    ) -> dict[Literal["low", "in_band", "high"], float]:
        low, high = target
        center = (low + high) / 2.0
        width = max(0.05, high - low)
        low_score = math.exp(-((value - (low - width / 2)) / width) ** 2)
        in_score = math.exp(-((value - center) / width) ** 2)
        high_score = math.exp(-((value - (high + width / 2)) / width) ** 2)
        total = low_score + in_score + high_score
        values = [low_score / total, in_score / total, high_score / total]
        values[-1] = 1.0 - values[0] - values[1]
        return {"low": values[0], "in_band": values[1], "high": values[2]}

    def _derive_affect(self, factors: dict[DimensionName, FactorBelief]) -> CoreAffect:
        deviations: dict[DimensionName, float] = {}
        for dimension, factor in factors.items():
            low, high = factor.target_band
            deviations[dimension] = max(low - factor.value, factor.value - high, 0.0)
        mean_deviation = sum(deviations.values()) / len(deviations)
        safety = factors["safety_predictability"].value
        autonomy = factors["autonomy"].value
        competence = factors["competence"].value
        load = factors["cognitive_load"].value
        energy = factors["energy_reserve"].value
        return CoreAffect(
            valence=_clamp(0.45 - 1.7 * mean_deviation + 0.2 * (safety - 0.5), -1.0, 1.0),
            arousal=_clamp(0.25 + 0.55 * load + 0.25 * (1.0 - energy), 0.0, 1.0),
            dominance=_clamp((autonomy + competence + safety) / 3.0, 0.0, 1.0),
            confidence=sum(item.precision for item in factors.values()) / len(factors),
        )

    def _derive_emotion(
        self,
        affect: CoreAffect,
        factors: dict[DimensionName, FactorBelief],
        now: datetime,
    ) -> DerivedEmotion:
        support = sorted(
            DIMENSIONS,
            key=lambda key: 1.0 - factors[key].posterior["in_band"],
            reverse=True,
        )[:2]
        if affect.valence < -self.profile.emotion_threshold and affect.arousal > 0.55:
            label = "strained"
        elif factors["social_connection"].posterior["low"] > 0.55:
            label = "lonely"
        elif affect.valence > self.profile.emotion_threshold and affect.dominance > 0.55:
            label = "engaged"
        elif affect.arousal < 0.35:
            label = "settled"
        else:
            label = "attentive"
        previous = self.snapshot.emotion
        held = (now - previous.entered_at).total_seconds() < previous.minimum_hold_seconds
        if held and previous.intensity >= abs(affect.valence):
            return previous
        return DerivedEmotion(
            label=label,
            intensity=_clamp(max(abs(affect.valence), affect.arousal * 0.7), 0.0, 1.0),
            entered_at=now if label != previous.label else previous.entered_at,
            minimum_hold_seconds=self.profile.emotion_minimum_hold_seconds,
            supporting_factors=[
                f"{key}:{max(factors[key].posterior, key=lambda band: factors[key].posterior[band])}"
                for key in support
            ],
        )

    def _rank_policies(self, factors: dict[DimensionName, FactorBelief]) -> list[PolicyCandidate]:
        actions: dict[DimensionName, tuple[str, str, dict[DimensionName, float]]] = {
            "energy_reserve": ("rest", "降低交互强度并恢复资源", {"energy_reserve": 0.10, "cognitive_load": -0.05}),
            "cognitive_load": ("simplify", "分解当前任务并降低刺激", {"cognitive_load": -0.10, "competence": 0.02}),
            "safety_predictability": ("clarify", "求证并建立明确边界", {"safety_predictability": 0.09}),
            "competence": ("small_step", "选择一个可验证的小目标", {"competence": 0.08}),
            "autonomy": ("renegotiate", "重新协商目标与选择权", {"autonomy": 0.09}),
            "social_connection": ("connect", "进行低压力的主动联系", {"social_connection": 0.10}),
            "novelty": ("explore", "安排一项轻量探索", {"novelty": 0.09}),
        }
        scored: list[
            tuple[float, DimensionName, tuple[str, str, dict[DimensionName, float]]]
        ] = []
        for dimension in DIMENSIONS:
            factor = factors[dimension]
            spec = self.profile.dimensions[dimension]
            low, high = factor.target_band
            error = max(low - factor.value, factor.value - high, 0.0)
            weight = spec.lower_weight if factor.value < low else spec.upper_weight
            scored.append((error * weight, dimension, actions[dimension]))
        scored.sort(key=lambda item: (-item[0], item[1]))
        candidates: list[PolicyCandidate] = []
        for rank, (error, dimension, (policy_id, label, impacts)) in enumerate(scored[:3], start=1):
            ambiguity = 1.0 - factors[dimension].precision
            risk = sum(abs(value) for value in impacts.values()) * 0.2
            information_gain = ambiguity * 0.25
            pragmatic = -error
            total = pragmatic + ambiguity * 0.25 + risk - information_gain
            candidates.append(
                PolicyCandidate(
                    policy_id=policy_id,
                    label=label,
                    source_drive=dimension,
                    expected_impacts=impacts,
                    success_criteria=[f"{dimension} moves toward target band"],
                    efe=ExpectedFreeEnergyBreakdown(
                        pragmatic_value=pragmatic,
                        ambiguity=ambiguity,
                        risk=risk,
                        information_gain=information_gain,
                        total=total,
                    ),
                    rank=rank,
                )
            )
        return candidates

    def _next_boundary_heartbeat_streaks(
        self,
        factors: dict[DimensionName, FactorBelief],
        *,
        heartbeat: bool,
    ) -> tuple[
        dict[DimensionName, int],
        dict[DimensionName, Literal["low", "high"]],
    ]:
        if self.profile.watchdog_boundary_heartbeats is None:
            return {}, {}
        previous = self._boundary_heartbeat_streaks
        previous_sides = self._boundary_heartbeat_sides
        streaks: dict[DimensionName, int] = {}
        sides: dict[DimensionName, Literal["low", "high"]] = {}
        for dimension in DIMENSIONS:
            observed = self.snapshot.dimensions[dimension] if heartbeat else factors[dimension]
            spec = self.profile.dimensions[dimension]
            side: Literal["low", "high"] | None = None
            if observed.value <= spec.hard_bounds[0] + 1e-9:
                side = "low"
            elif observed.value >= spec.hard_bounds[1] - 1e-9:
                side = "high"
            if side is None:
                streaks[dimension] = 0
                continue
            sides[dimension] = side
            same_side = previous_sides.get(dimension) == side
            if heartbeat:
                streaks[dimension] = previous.get(dimension, 0) + 1 if same_side else 1
            elif same_side:
                streaks[dimension] = previous.get(dimension, 0)
            else:
                streaks[dimension] = 0
        return streaks, sides

    def _watchdog(
        self,
        factors: dict[DimensionName, FactorBelief],
        boundary_streaks: dict[DimensionName, int],
    ) -> list[str]:
        reasons: list[str] = []
        for dimension, factor in factors.items():
            if not math.isfinite(factor.value):
                reasons.append(f"{dimension}:non_finite")
            if max(factor.posterior.values()) > 0.999999:
                reasons.append(f"{dimension}:posterior_saturated")
            threshold = self.profile.watchdog_boundary_heartbeats
            if threshold is not None and boundary_streaks.get(dimension, 0) >= threshold:
                reasons.append(f"{dimension}:hard_bound_sustained")
        return reasons

    def _failure(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
        request_hash: str,
        error: str,
        status: Literal["failed", "rejected", "conflict"],
    ) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]](
            command_id=command.command_id,
            command_type=command.command_type,
            status=status,
            request_hash=request_hash,
            error=error,
            completed_at=self._clock(),
        )

    def _validate_profile_identity(self) -> None:
        if self.snapshot.profile_id != self.profile.profile_id:
            raise ValueError("snapshot/profile id mismatch")
        if self.snapshot.profile_revision == self.profile.revision:
            return
        if self.snapshot.profile_revision > self.profile.revision:
            raise ValueError("cannot start an older profile over a newer snapshot")
        factors: dict[DimensionName, FactorBelief] = {}
        for dimension in DIMENSIONS:
            previous = self.snapshot.dimensions[dimension]
            spec = self.profile.dimensions[dimension]
            value = _clamp(previous.value, spec.hard_bounds[0], spec.hard_bounds[1])
            factors[dimension] = previous.model_copy(
                update={
                    "value": value,
                    "velocity": value - previous.value,
                    "posterior": self._posterior(value, spec.baseline_band),
                    "target_band": spec.baseline_band,
                    "updated_at": self._started_at,
                }
            )
        affect = self._derive_affect(factors)
        emotion = self._derive_emotion(affect, factors, self._started_at)
        self.snapshot = self.snapshot.model_copy(
            update={
                "revision": self.snapshot.revision + 1,
                "profile_revision": self.profile.revision,
                "dimensions": factors,
                "core_affect": affect,
                "emotion": emotion,
                "candidates": self._rank_policies(factors),
                "updated_at": self._started_at,
                "stable_snapshot_ref": f"snapshot://{self.profile.profile_id}/profile-migration-r{self.profile.revision}",
                "watchdog": [],
            }
        )
        self._boundary_heartbeat_streaks = {}
        self._boundary_heartbeat_sides = {}
        self._persist()

    def _load(self) -> None:
        stored_snapshot = self._command_ledger.load_snapshot()
        if stored_snapshot is not None:
            self.snapshot = HomeostaticSnapshot.model_validate(stored_snapshot)
        elif self.state_path.is_file():
            self.snapshot = HomeostaticSnapshot.model_validate_json(self.state_path.read_text(encoding="utf-8"))
        engine_state = self._command_ledger.load_engine_state()
        raw_streaks = engine_state.get("boundary_heartbeat_streaks") or {}
        raw_sides = engine_state.get("boundary_heartbeat_sides") or {}
        if not isinstance(raw_streaks, dict) or not isinstance(raw_sides, dict):
            raise ValueError("stored boundary heartbeat state must be an object")
        self._boundary_heartbeat_streaks = {
            cast(DimensionName, key): int(value)
            for key, value in raw_streaks.items()
            if key in DIMENSIONS and int(value) >= 0
        }
        self._boundary_heartbeat_sides = {
            cast(DimensionName, key): cast(Literal["low", "high"], value)
            for key, value in raw_sides.items()
            if key in DIMENSIONS and value in {"low", "high"}
        }
        self._project_snapshot_mirror()
        self._repair_trace_projection()

    def _repair_projections_if_needed(self) -> None:
        if not self._projection_errors:
            return
        self._project_snapshot_mirror()
        self._repair_trace_projection()

    def _persist(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._command_ledger.save_snapshot(
            self.snapshot.model_dump(mode="json"),
            engine_state=self._engine_state_payload(
                self._boundary_heartbeat_streaks,
                self._boundary_heartbeat_sides,
            ),
        )
        self._project_snapshot_mirror()

    @staticmethod
    def _engine_state_payload(
        boundary_streaks: dict[DimensionName, int],
        boundary_sides: dict[DimensionName, Literal["low", "high"]],
    ) -> dict[str, Any]:
        return {
            "boundary_heartbeat_streaks": dict(boundary_streaks),
            "boundary_heartbeat_sides": dict(boundary_sides),
        }

    def _project_snapshot_mirror(self) -> None:
        try:
            self._atomic(self.state_path, self.snapshot.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            self._projection_errors["snapshot"] = str(exc)
        else:
            self._projection_errors.pop("snapshot", None)

    def _project_trace(self, trace: HomeostaticTransitionTrace) -> None:
        if "trace" in self._projection_errors or not self.trace_path.is_file():
            self._repair_trace_projection()
            return
        try:
            self._append_trace(trace)
        except OSError as exc:
            self._projection_errors["trace"] = str(exc)
        else:
            self._projection_errors.pop("trace", None)

    def _repair_trace_projection(self) -> None:
        if not self._command_ledger.has_traces():
            return
        try:
            if self._trace_projection_matches():
                self._projection_errors.pop("trace", None)
                return
            temporary = self.trace_path.with_name(
                f".{self.trace_path.name}.{os.getpid()}.tmp"
            )
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                for trace in self._command_ledger.iter_traces():
                    handle.write(
                        json.dumps(
                            trace,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.trace_path)
        except OSError as exc:
            self._projection_errors["trace"] = str(exc)
        else:
            self._projection_errors.pop("trace", None)

    def _trace_projection_matches(self) -> bool:
        if not self.trace_path.is_file():
            return False
        try:
            with self.trace_path.open("r", encoding="utf-8") as handle:
                for expected in self._command_ledger.iter_traces():
                    line = handle.readline()
                    if not line or json.loads(line) != expected:
                        return False
                return handle.readline() == ""
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False

    @staticmethod
    def _atomic(path: Path, value: str) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)

    def _append_trace(self, trace: HomeostaticTransitionTrace) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.trace_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(trace.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
