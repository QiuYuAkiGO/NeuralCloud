"""Deterministic seven-factor homeostasis domain service."""

from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal, cast

from pydantic import ValidationError

from harness.files import replace_path

from harness.contracts.envelope import DomainCommandEnvelope, DomainCommandResult, VersionVector
from harness.embodiment.contracts import (
    HomeostaticObservation as EmbodimentHomeostaticObservation,
    evidence_ref_strings,
)
from harness.homeostasis.appraisal import ApplyPsychologicalAppraisal, PsychologicalAppraisal
from harness.homeostasis.native_appraisal import NativeAppraisalReceipt, apply_native
from harness.homeostasis.dynamics import (
    CognitiveExposure, DynamicsState, PSYCHOLOGICAL, advance, expose, appraise as appraise_dynamics,
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
    HomeostaticInteroceptiveSummary,
    HomeostaticSalience,
    HomeostaticSnapshot,
    HomeostaticTransitionTrace,
    PolicyCandidate,
    QQDriveContext,
    QQHomeostaticFact,
    QQFactLookup,
    QQFactStatus,
    build_interoceptive_summary,
    derive_homeostatic_salience,
    initial_snapshot,
    normalized_homeostatic_deviation,
)
from harness.homeostasis.ledger import CommandLedger, CommandLedgerConflictError, StaleSnapshotError
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


def _trace_affect(summary: Any, affect: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if summary is not None:
        payload["salience"] = getattr(summary, "salience", None)
        payload["dominant_needs"] = [item.dimension for item in getattr(summary, "dominant_needs", [])]
    if affect is not None:
        payload["affect_valence"] = affect.valence
        payload["affect_arousal"] = affect.arousal
        payload["affect_dominance"] = affect.dominance
    return payload


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
        self._qq_activity_at = self._started_at
        self._dynamics: DynamicsState | None = None
        self._loaded_snapshot_schema = "homeostasis-state-v3"
        self.snapshot = initial_snapshot(profile, now=self._started_at)
        self._load()
        prior_mode = (self.snapshot.dynamics or {}).get("mode", "off")
        if profile.cognitive_dynamics.mode == "active" and prior_mode != "active" and self._dynamics is not None:
            # Activation preserves displayed values; shadow-only latent history
            # survives, but adopting a new calculator must not jump the UI.
            self._dynamics.load = self.snapshot.dimensions["cognitive_load"].value
            self._dynamics.novelty = self.snapshot.dimensions["novelty"].value
            self._dynamics.responses = {k: self.snapshot.dimensions[k].value-self._dynamics.baselines[k] for k in PSYCHOLOGICAL}
        if profile.cognitive_dynamics.mode != "off" and self._dynamics is None:
            self._dynamics = DynamicsState.migrate(
                {k: v.value for k, v in self.snapshot.dimensions.items()}, profile.preferences, self.snapshot.updated_at)
        self._validate_profile_identity()
        self._upgrade_legacy_snapshot_schema()
        candidates = self._rank_policies(self.snapshot.dimensions)
        summary = self.snapshot.interoceptive_summary
        if (
            summary is None
            or summary.snapshot_revision != self.snapshot.revision
            or summary.profile_ref != self.profile.profile_ref
        ):
            summary = self._derive_interoceptive_summary(
                revision=self.snapshot.revision,
                factors=self.snapshot.dimensions,
                affect=self.snapshot.core_affect,
                emotion=self.snapshot.emotion,
                candidates=candidates,
                watchdog=self.snapshot.watchdog,
            )
        self.snapshot = self.snapshot.model_copy(
            update={
                "candidates": candidates,
                "interoceptive_summary": summary,
            }
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
                    "dynamics_mode": self.profile.cognitive_dynamics.mode,
                    "regulation_pressure": self._dynamics.pressure() if self._dynamics else {},
                },
            )

    async def read_traces(
        self, *, since: str | None = None, until: str | None = None, limit: int = 5000,
    ) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 20000))
        traces: list[dict[str, Any]] = []
        for payload in self._command_ledger.iter_traces():
            occurred = str(payload.get("occurred_at") or "")
            if since and occurred < since:
                continue
            if until and occurred >= until:
                continue
            traces.append(payload)
            if len(traces) >= cap:
                break
        return traces

    async def read_report_traces(self, *, since: str, until: str) -> list[dict[str, Any]]:
        # The report projection owns its SQLite connection and never occupies
        # the domain's event loop while scanning persisted telemetry.
        task = asyncio.create_task(asyncio.to_thread(self._command_ledger.report_traces, since, until))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

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

    async def read_qq_fact(self, lookup: QQFactLookup) -> QQFactStatus:
        lookup = QQFactLookup.model_validate(lookup.model_dump())
        async with self._lock:
            stored = self._command_ledger.qq_fact_status(lookup.fact_key)
            return QQFactStatus(fact_key=lookup.fact_key, status="applied" if stored else "unknown",
                fact_hash=stored[0] if stored else None, committed_revision=stored[1] if stored else None)

    async def apply_heartbeat(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command, kind="heartbeat")

    async def apply_outcome(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command, kind="outcome")

    async def apply_appraisal(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        """Apply an evidence-bound model suggestion with server-owned precision."""
        return await self._apply(command, kind="appraisal")

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
                return self._enrich_replay(restored)
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
            next_dynamics = advance(self._dynamics, effective_at, self.profile.cognitive_dynamics) if self._dynamics and self.profile.cognitive_dynamics.mode != "off" else self._dynamics
            if self.profile.cognitive_dynamics.mode == "active":
                if set(observation.observed_values) != {"energy_reserve"}:
                    return self._failure(command, request_hash, "cognitive dynamics accepts measured energy only", "rejected")
                factors = self._dynamics_factors(next_dynamics, effective_at)
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
            salience = derive_homeostatic_salience(
                self.profile,
                factors,
                previous=self._previous_salience(),
                watchdog=watchdog,
            )
            emotion = self._derive_emotion(
                affect,
                factors,
                effective_at,
                salience=salience,
            )
            candidates = self._rank_policies(factors, dynamics=next_dynamics)
            summary = self._derive_interoceptive_summary(
                revision=revision,
                factors=factors,
                affect=affect,
                emotion=emotion,
                candidates=candidates,
                watchdog=watchdog,
                salience=salience,
                dynamics=next_dynamics,
            )
            next_snapshot = HomeostaticSnapshot(
                dynamics=self._dynamics_view(next_dynamics),
                regulation_pressure=next_dynamics.pressure() if next_dynamics else {},
                revision=revision,
                profile_id=self.profile.profile_id,
                profile_revision=self.profile.revision,
                dimensions=factors,
                core_affect=affect,
                emotion=emotion,
                candidates=candidates,
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
                interoceptive_summary=summary,
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
                **_trace_affect(summary, affect),
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
                    "interoceptive_summary": summary.model_dump(mode="json"),
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
                    dynamics_state=next_dynamics,
                ),
                trace=trace.model_dump(mode="json"),
            )
            self.snapshot = next_snapshot
            self._dynamics = next_dynamics
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
        kind: Literal["event", "heartbeat", "outcome", "appraisal"],
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
                return self._enrich_replay(restored)
            if command.is_expired(now=self._clock()):
                return self._failure(command, request_hash, "command deadline expired", "rejected")
            heartbeat_rebased = False
            if kind == "appraisal" and command.payload.get("allow_heartbeat_rebase") is True:
                try:
                    candidate = ApplyPsychologicalAppraisal.model_validate(command.payload)
                    heartbeat_rebased = (candidate.basis_snapshot_at is not None
                        and 0 <= (self._clock() - candidate.basis_snapshot_at).total_seconds() <= 60
                        and command.expected_revision == candidate.appraisal.basis_snapshot_revision
                        and self._command_ledger.heartbeat_only_between(
                            candidate.appraisal.basis_snapshot_revision, self.snapshot.revision))
                except ValidationError:
                    pass
            if command.expected_revision is not None and command.expected_revision != self.snapshot.revision and not heartbeat_rebased:
                return self._failure(
                    command,
                    request_hash,
                    f"expected revision {command.expected_revision}, current {self.snapshot.revision}",
                    "conflict",
                )

            psychological: PsychologicalAppraisal | None = None
            if kind == "appraisal":
                if command.command_type != "ApplyAppraisal":
                    return self._failure(command, request_hash, "appraisal requires ApplyAppraisal command", "rejected")
                try:
                    trusted = ApplyPsychologicalAppraisal.model_validate(command.payload)
                except ValidationError:
                    return self._failure(command, request_hash, "invalid psychological appraisal or Runtime evidence binding", "rejected")
                psychological = trusted.appraisal
                now = self._clock()
                if psychological.created_at > now or now >= psychological.expires_at:
                    return self._failure(command, request_hash, "appraisal is not currently valid", "rejected")
                if psychological.basis_snapshot_revision != self.snapshot.revision and not heartbeat_rebased:
                    return self._failure(command, request_hash, "appraisal basis snapshot revision is stale", "conflict")
                if self._command_ledger.appraisal_is_committed(
                    episode_id=psychological.episode_id,
                    appraisal_id=psychological.appraisal_id,
                    evidence_set_hash=psychological.evidence_set_hash,
                    observed_event_refs=psychological.observed_event_refs,
                ):
                    return self._failure(command, request_hash, "appraisal identity or episode evidence set already committed", "conflict")
                # No text, success flag or explicit legacy appraisal enters the
                # keyword/outcome appraisal path for this command.
                event = {}
                occurred_at = psychological.created_at
            else:
                event = dict(command.payload.get("event") or command.payload)
                occurred_at = _aware(event.get("source_time") or event.get("observed_at"), command.issued_at)
            effective_at = max(occurred_at, self.snapshot.updated_at)
            fact = None
            new_fact = False
            activity_at = self._qq_activity_at
            payload = dict(event.get("payload") or {})
            try:
                drive_context = QQDriveContext.model_validate(payload.get("qq_drive_context", {}))
                if psychological is not None:
                    appraisal = EventAppraisal(
                        source=f"psychological:{psychological.appraisal_kind}",
                        precision=min(0.35, self.profile.source_precision.get("llm_appraisal", 0.0)) * psychological.model_confidence,
                        suggested_impacts=dict(psychological.suggested_impacts),
                        evidence_refs=list(psychological.observed_event_refs),
                    )
                elif "qq_fact" in payload:
                    drive = self.profile.qq_social_drive
                    if ((drive is None or not drive.enabled) and self.profile.cognitive_dynamics.mode != "active") or kind == "heartbeat":
                        return self._failure(command, request_hash, "QQ facts require an enabled profile and event/outcome command", "rejected")
                    if "appraisal" in payload or "appraisal" in event:
                        return self._failure(command, request_hash, "QQ facts cannot override their deterministic appraisal", "rejected")
                    fact = QQHomeostaticFact.model_validate(payload["qq_fact"])
                    previous_fact_hash = self._command_ledger.qq_fact_hash(fact.fact_key)
                    if previous_fact_hash is not None and previous_fact_hash != fact.fact_hash:
                        return self._failure(command, request_hash, "QQ fact identity reused with different evidence", "conflict")
                    new_fact = previous_fact_hash is None
                    if new_fact and not -5 <= (self._clock() - fact.occurred_at).total_seconds() <= 300:
                        return self._failure(command, request_hash, "QQ fact must be a recent verified event", "rejected")
                    appraisal = (EventAppraisal(source="verified_outcome", precision=.9, suggested_impacts={})
                        if self.profile.cognitive_dynamics.mode == "active" else self._qq_fact_appraisal(fact, apply=new_fact,
                        regulation_feedback=command.payload.get("regulation_feedback") is True))
                    if new_fact and fact.kind in {"verified_message", "verified_interaction", "verified_novelty"}:
                        activity_at = max(activity_at, min(fact.occurred_at, command.issued_at))
                elif "cognitive_exposure" in payload or 'native_appraisal' in payload:
                    appraisal = EventAppraisal(source="runtime", precision=.8, suggested_impacts={})
                else:
                    appraisal = self._appraise(event, kind=kind, event_id=command.event_id)
            except ValidationError:
                return self._failure(command, request_hash, "invalid QQ homeostatic fact or context", "rejected")
            next_dynamics = self._dynamics
            exposure = None
            new_exposure = False
            native_receipt = None
            new_native = False
            causal_evidence = []
            if next_dynamics is not None and self.profile.cognitive_dynamics.mode != "off":
                next_dynamics = advance(next_dynamics, effective_at, self.profile.cognitive_dynamics)
                if self.profile.cognitive_dynamics.mode == "shadow" and kind == "heartbeat":
                    previous_sample = next_dynamics.shadow_last_sample_at
                    if previous_sample is not None:
                        gap = max(0, (effective_at-previous_sample).total_seconds())
                        next_dynamics.shadow_max_gap_seconds = max(next_dynamics.shadow_max_gap_seconds, gap)
                        next_dynamics.shadow_observed_seconds = next_dynamics.shadow_observed_seconds+gap if gap <= 30 else 0
                    next_dynamics.shadow_last_sample_at = effective_at
                    next_dynamics.shadow_samples += 1
                if "cognitive_exposure" in payload:
                    try:
                        exposure = CognitiveExposure.model_validate(payload["cognitive_exposure"])
                    except ValidationError:
                        return self._failure(command, request_hash, "invalid cognitive exposure", "rejected")
                    if fact is not None or psychological is not None or kind == "heartbeat":
                        return self._failure(command, request_hash, "exposure requires an independent processing receipt", "rejected")
                    stored_hash = self._command_ledger.exposure_status(exposure.exposure_id)
                    if stored_hash is not None and stored_hash != exposure.receipt_hash:
                        return self._failure(command, request_hash, "exposure identity reused with different receipt", "conflict")
                    new_exposure = stored_hash is None
                    if new_exposure:
                        if (self._clock()-exposure.attended_at).total_seconds() < -5:
                            return self._failure(command, request_hash, "cognitive exposure must describe recent processing", "rejected")
                        next_dynamics = expose(next_dynamics, exposure, self.profile.cognitive_dynamics,
                                               repeated_content=self._command_ledger.content_seen(exposure.content_hash))
                        effective_at = max(effective_at, next_dynamics.updated_at)
                        if exposure.assessment and exposure.evidence_refs and exposure.context_id != "attention:control" and (effective_at-exposure.attended_at).total_seconds() <= 300:
                            # Immutable causal content is also a dedup key: changing
                            # command/appraisal IDs cannot manufacture new evidence.
                            refs = [*exposure.evidence_refs, "content:" + exposure.content_hash]
                            eligible = self._command_ledger.eligible_affect_dimensions(refs, set(exposure.assessment.impacts))
                            next_dynamics = appraise_dynamics(next_dynamics, effective_at,
                                exposure.assessment.impacts, exposure.assessment.confidence,
                                self.profile.cognitive_dynamics, eligible_dimensions=eligible)
                            causal_evidence = [(ref, dimension) for ref in refs for dimension in eligible]
                if 'native_appraisal' in payload:
                    try:
                        native_receipt = NativeAppraisalReceipt.model_validate(payload['native_appraisal'])
                    except ValidationError:
                        return self._failure(command, request_hash, 'invalid native appraisal', 'rejected')
                    if fact is not None or psychological is not None or exposure is not None or kind == 'heartbeat':
                        return self._failure(command, request_hash, 'native appraisal requires an independent interpretation', 'rejected')
                    old_hash = self._command_ledger.native_appraisal_status(native_receipt.receipt_id)
                    if old_hash is not None and old_hash != native_receipt.receipt_hash:
                        return self._failure(command, request_hash, 'native appraisal identity conflict', 'conflict')
                    new_native = old_hash is None
                    if new_native:
                        refs = native_receipt.source_refs
                        dimensions = set(native_receipt.assessment.impacts) if native_receipt.assessment else set()
                        eligible = self._command_ledger.eligible_affect_dimensions(refs, dimensions)
                        before_native = next_dynamics.native_appraisals
                        next_dynamics = apply_native(next_dynamics, native_receipt, effective_at, self.profile.cognitive_dynamics,
                            eligible_dimensions=eligible)
                        if next_dynamics.native_appraisals > before_native:
                            causal_evidence = [(ref, dimension) for ref in refs for dimension in eligible]
                if psychological is not None:
                    eligible = self._command_ledger.eligible_affect_dimensions(
                        psychological.observed_event_refs, set(psychological.suggested_impacts).intersection(PSYCHOLOGICAL))
                    if any(ref.startswith("qq-attention:") for ref in psychological.observed_event_refs):
                        eligible = set()
                    next_dynamics = appraise_dynamics(next_dynamics, effective_at,
                        psychological.suggested_impacts, psychological.model_confidence, self.profile.cognitive_dynamics,
                        eligible_dimensions=eligible,
                        verified_outcome=(psychological.appraisal_kind == "event_interpretation"
                            and bool(set(psychological.observed_event_refs).intersection(trusted.verified_outcome_refs))))
                    causal_evidence = [(ref, dimension) for ref in psychological.observed_event_refs for dimension in eligible]
            shadow_only = self.profile.cognitive_dynamics.mode == "shadow" and (
                "cognitive_exposure" in payload or 'native_appraisal' in payload or command.payload.get("passive_only") is True or payload.get("passive_only") is True)
            if shadow_only:
                factors = dict(self.snapshot.dimensions)
                applied = {k: 0.0 for k in DIMENSIONS}
                coupling = dict(applied)
            elif self.profile.cognitive_dynamics.mode == "active":
                factors = self._dynamics_factors(next_dynamics, effective_at)
                applied = {k: factors[k].value-self.snapshot.dimensions[k].value for k in DIMENSIONS}
                coupling = {k: 0.0 for k in DIMENSIONS}
            else:
                factors, applied, coupling = self._integrate(
                    appraisal, effective_at, heartbeat=kind == "heartbeat", qq_resting=drive_context.resting,
                )
            if psychological is not None and self.profile.cognitive_dynamics.mode != "active":
                # Model suggestions may affect only the six psychological
                # dimensions. Cross-factor coupling cannot modify energy or
                # relabel its authoritative evidence/precision either.
                factors["energy_reserve"] = self.snapshot.dimensions["energy_reserve"]
                applied["energy_reserve"] = 0.0
                coupling["energy_reserve"] = 0.0
            boundary_streaks, boundary_sides = self._next_boundary_heartbeat_streaks(
                factors,
                heartbeat=kind == "heartbeat",
            )
            if shadow_only:
                boundary_streaks, boundary_sides = dict(self._boundary_heartbeat_streaks), dict(self._boundary_heartbeat_sides)
            watchdog = self._watchdog(factors, boundary_streaks)
            if shadow_only:
                watchdog = list(self.snapshot.watchdog)
            revision = self.snapshot.revision + 1
            heartbeat_at = effective_at if kind == "heartbeat" and not shadow_only else self.snapshot.last_heartbeat_at
            candidates = self._rank_policies(factors, dynamics=next_dynamics)
            affect = self._derive_affect(factors)
            salience = derive_homeostatic_salience(
                self.profile,
                factors,
                previous=self._previous_salience(),
                watchdog=watchdog,
            )
            emotion = self._derive_emotion(
                affect,
                factors,
                effective_at,
                salience=salience,
            )
            summary = self._derive_interoceptive_summary(
                revision=revision,
                factors=factors,
                affect=affect,
                emotion=emotion,
                candidates=candidates,
                watchdog=watchdog,
                salience=salience,
                dynamics=next_dynamics,
            )
            if shadow_only:
                affect, emotion, candidates = self.snapshot.core_affect, self.snapshot.emotion, self.snapshot.candidates
                summary = self.snapshot.interoceptive_summary.model_copy(update={"snapshot_revision": revision})
            next_snapshot = HomeostaticSnapshot(
                dynamics=self._dynamics_view(next_dynamics),
                regulation_pressure=next_dynamics.pressure() if next_dynamics else {},
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
                interoceptive_summary=summary,
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
                evidence_refs=(
                    list(psychological.observed_event_refs) if psychological is not None
                    else list(fact.evidence_refs) if fact is not None else []
                ),
                episode_id=psychological.episode_id if psychological is not None else None,
                values_before=(
                    {name: factor.value for name, factor in self.snapshot.dimensions.items()}
                ),
                values_after=(
                    {name: factor.value for name, factor in factors.items()}
                ),
                **_trace_affect(summary, affect),
            )
            result_payload = {
                "revision": revision,
                "snapshot": next_snapshot.model_dump(mode="json"),
                "interoceptive_summary": summary.model_dump(mode="json"),
                "trace": trace.model_dump(mode="json"),
            }
            if fact is not None:
                result_payload["qq_fact"] = {"fact_id": fact.fact_id, "kind": fact.kind, "applied": new_fact}
            if psychological is not None:
                result_payload["appraisal"] = {
                    "episode_id": psychological.episode_id,
                    "appraisal_id": psychological.appraisal_id,
                    "appraisal_kind": psychological.appraisal_kind,
                    "basis_snapshot_revision": psychological.basis_snapshot_revision,
                    "applied_to_revision": self.snapshot.revision,
                    "heartbeat_rebased": heartbeat_rebased,
                    "evidence_set_hash": psychological.evidence_set_hash,
                    "model_confidence": psychological.model_confidence,
                    "effective_precision": appraisal.precision,
                    "applied": True,
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
            try:
                self._command_ledger.commit(
                    key,
                    request_hash=request_hash,
                    result=result.model_dump(mode="json"),
                    snapshot=next_snapshot.model_dump(mode="json"),
                    engine_state=self._engine_state_payload(
                        boundary_streaks,
                        boundary_sides,
                        qq_activity_at=activity_at,
                        dynamics_state=next_dynamics,
                    ),
                    trace=trace.model_dump(mode="json"),
                    qq_fact=(fact.fact_key, fact.fact_hash) if fact is not None and new_fact else None,
                    psychological_appraisal=(
                        (psychological.episode_id, psychological.appraisal_id, psychological.evidence_set_hash,
                         psychological.observed_event_refs)
                        if psychological is not None else None
                    ),
                    cognitive_exposure=(exposure.exposure_id, exposure.receipt_hash, exposure.content_hash) if new_exposure else None,
                    native_appraisal=(native_receipt.receipt_id, native_receipt.receipt_hash) if new_native else None,
                    causal_evidence=causal_evidence,
                )
            except (CommandLedgerConflictError, StaleSnapshotError):
                if psychological is None:
                    raise
                return self._failure(command, request_hash, "appraisal state or semantic evidence changed during commit", "conflict")
            self.snapshot = next_snapshot
            self._dynamics = next_dynamics
            self._qq_activity_at = activity_at
            self._boundary_heartbeat_streaks = boundary_streaks
            self._boundary_heartbeat_sides = boundary_sides
            self._project_snapshot_mirror()
            self._project_trace(trace)
            return result

    def _dynamics_view(self, state):
        return state.view(self.profile.cognitive_dynamics.mode, habituation_seconds=self.profile.cognitive_dynamics.habituation_seconds) if state and self.profile.cognitive_dynamics.mode != "off" else None

    def _dynamics_factors(self, state, at):
        factors = dict(self.snapshot.dimensions)
        for name, value in state.values().items():
            old = factors[name]
            band = self.profile.dimensions[name].baseline_band
            if name == "cognitive_load" or (name == "novelty" and (
                    state.boredom < .5 or state.fatigue >= .5 or state.load >= .45 or not state.wants_engagement)):
                band = (0.0, band[1])
            factors[name] = old.model_copy(update={"value": value, "velocity": value-old.value,
                "posterior": self._posterior(value, band), "target_band": band, "updated_at": at})
        energy = factors["energy_reserve"]
        previous_at = self._dynamics.updated_at if self._dynamics else self.snapshot.updated_at
        stale_after = energy.updated_at+timedelta(seconds=self.profile.cognitive_dynamics.sensor_stale_seconds)
        stale_elapsed = max(0, (at-max(previous_at, stale_after)).total_seconds())
        factors["energy_reserve"] = energy.model_copy(update={"precision": energy.precision*math.exp(-stale_elapsed/3600), "velocity": 0.0})
        return factors

    def _qq_fact_appraisal(self, fact: QQHomeostaticFact, *, apply: bool, regulation_feedback: bool = False) -> EventAppraisal:
        drive = self.profile.qq_social_drive
        assert drive is not None and drive.enabled
        impacts: dict[DimensionName, float] = {}
        if apply:
            if fact.kind == "verified_message":
                impacts = {"novelty": drive.message_novelty_recovery, "cognitive_load": 0.005}
            elif fact.kind == "verified_interaction":
                impacts = {"novelty": drive.interaction_novelty_recovery, "cognitive_load": 0.01}
                if not regulation_feedback:
                    impacts["social_connection"] = drive.interaction_social_recovery
            elif fact.kind == "verified_novelty":
                impacts = {"novelty": drive.experience_novelty_recovery}
        # A send receipt proves an attempt, not a social reward. Silence is
        # neither a rejection nor a reason to increase sending.
        return EventAppraisal(
            source="verified_outcome", precision=self.profile.source_precision.get("verified_outcome", 0.9),
            suggested_impacts=impacts, evidence_refs=list(fact.evidence_refs),
        )

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
        qq_resting: bool = False,
    ) -> tuple[dict[DimensionName, FactorBelief], dict[DimensionName, float], dict[DimensionName, float]]:
        requested_base_delta: dict[DimensionName, float] = {
            key: 0.0 for key in DIMENSIONS
        }
        for dimension in DIMENSIONS:
            current = self.snapshot.dimensions[dimension]
            spec = self.profile.dimensions[dimension]
            target = sum(spec.baseline_band) / 2.0
            # A sensor sample or unrelated event advances the global snapshot
            # clock without integrating every factor. Use each factor's clock
            # so those events cannot consume its pending heartbeat recovery.
            # A fresh reading still starts its own clock at capture time: it
            # must not receive drift from before the observed physical state.
            elapsed = (
                min(
                    max(0.0, (now - current.updated_at).total_seconds()),
                    float(self.profile.max_heartbeat_step_seconds),
                )
                if heartbeat
                else 0.0
            )
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

        if heartbeat:
            self._apply_qq_idle_drift(requested_base_delta, now, resting=qq_resting)

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
                updated_at=now if heartbeat or delta != 0.0 else current.updated_at,
            )
        reported_coupling = (
            realised_coupling
            if self.profile.coupling_semantics == "realized_delta_v2"
            else requested_coupling
        )
        return factors, applied, reported_coupling

    def _apply_qq_idle_drift(self, deltas: dict[DimensionName, float], now: datetime, *, resting: bool) -> None:
        drive = self.profile.qq_social_drive
        if drive is None or not drive.enabled or resting:
            return
        energy = self.snapshot.dimensions["energy_reserve"]
        load = self.snapshot.dimensions["cognitive_load"]
        # Needs recede while resources recover or a current task is demanding.
        # Existing recovery of every dimension remains independent of this term.
        energy_scale = _clamp((energy.value - energy.target_band[0]) / max(0.01, sum(energy.target_band) / 2 - energy.target_band[0]), 0, 1)
        load_scale = _clamp((load.target_band[1] - load.value) / max(0.01, load.target_band[1] - sum(load.target_band) / 2), 0, 1)
        scale = min(energy_scale, load_scale)
        idle_since = self._qq_activity_at + timedelta(seconds=drive.idle_grace_seconds)
        for dimension, rate, floor in (
            ("social_connection", drive.social_depletion_per_hour, drive.social_floor),
            ("novelty", drive.novelty_depletion_per_hour, drive.novelty_floor),
        ):
            current = self.snapshot.dimensions[dimension]
            elapsed = min(max(0.0, (now - max(current.updated_at, idle_since)).total_seconds()),
                          float(self.profile.max_heartbeat_step_seconds))
            available = max(0.0, current.value + deltas[dimension] - floor)
            deltas[dimension] -= min(available, rate * elapsed / 3600 * scale)

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
            deviations[dimension] = normalized_homeostatic_deviation(
                self.profile,
                factor,
            )[1]
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
        *,
        salience: HomeostaticSalience,
    ) -> DerivedEmotion:
        support = sorted(
            DIMENSIONS,
            key=lambda key: 1.0 - factors[key].posterior["in_band"],
            reverse=True,
        )[:2]
        materially_unstable = salience in {"marked", "critical"}
        if materially_unstable and affect.arousal > 0.55:
            label = "strained"
        elif affect.valence < -self.profile.emotion_threshold and affect.arousal > 0.55:
            label = "strained"
        elif factors["social_connection"].posterior["low"] > 0.55:
            label = "lonely"
        elif affect.valence > self.profile.emotion_threshold and affect.dominance > 0.55:
            label = "engaged"
        elif affect.arousal < 0.35 and not materially_unstable:
            label = "settled"
        else:
            label = "attentive"
        previous = self.snapshot.emotion
        held = (now - previous.entered_at).total_seconds() < previous.minimum_hold_seconds
        force_material_label = materially_unstable and (
            label == "strained" or previous.label == "settled"
        )
        if held and previous.intensity >= abs(affect.valence) and not force_material_label:
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

    def _previous_salience(self) -> HomeostaticSalience | None:
        summary = self.snapshot.interoceptive_summary
        return summary.salience if summary is not None else None

    def _derive_interoceptive_summary(
        self,
        *,
        revision: int,
        factors: dict[DimensionName, FactorBelief],
        affect: CoreAffect,
        emotion: DerivedEmotion,
        candidates: list[PolicyCandidate],
        watchdog: list[str],
        salience: HomeostaticSalience | None = None,
        dynamics: DynamicsState | None = None,
    ) -> HomeostaticInteroceptiveSummary:
        summary = build_interoceptive_summary(
            self.profile,
            revision=revision,
            factors=factors,
            affect=affect,
            emotion=emotion,
            candidates=candidates,
            watchdog=watchdog,
            previous_salience=self._previous_salience(),
            salience=salience,
        )
        dynamics = dynamics or self._dynamics
        if self.profile.cognitive_dynamics.mode == "active" and dynamics:
            summary = summary.model_copy(update={"schema_version": "homeostatic-interoception-v2",
                "regulation_pressure": dynamics.pressure(), "fatigue": dynamics.fatigue, "boredom": dynamics.boredom})
        return summary

    def _enrich_replay(
        self,
        restored: DomainCommandResult[dict[str, Any]],
    ) -> DomainCommandResult[dict[str, Any]]:
        """Add the v2 projection to legacy successful replay payloads."""

        payload = dict(restored.result or {})
        if isinstance(payload.get("interoceptive_summary"), dict):
            return restored.model_copy(
                update={"status": "idempotent_replay", "idempotency_hit": True}
            )

        raw_snapshot = payload.get("snapshot")
        if isinstance(raw_snapshot, dict):
            historical = HomeostaticSnapshot.model_validate(raw_snapshot)
            if historical.interoceptive_summary is not None:
                summary = historical.interoceptive_summary
            elif (
                historical.profile_id == self.profile.profile_id
                and historical.profile_revision == self.profile.revision
            ):
                summary = self._derive_interoceptive_summary(
                    revision=historical.revision,
                    factors=historical.dimensions,
                    affect=historical.core_affect,
                    emotion=historical.emotion,
                    candidates=historical.candidates,
                    watchdog=historical.watchdog,
                )
            else:
                # Old profile material is no longer authoritative enough to safely
                # recreate semantic text; return the current signed projection.
                historical = self.snapshot
                summary = self.snapshot.interoceptive_summary
        else:
            # The oldest ledger format stored only revision + trace. Enrich it with
            # the current authoritative projection without rewriting ledger history.
            historical = self.snapshot
            summary = self.snapshot.interoceptive_summary

        if summary is None:  # Defensive: __init__ establishes this invariant.
            raise RuntimeError("homeostasis snapshot is missing interoceptive summary")
        payload.update(
            {
                "revision": historical.revision,
                "snapshot": historical.model_copy(
                    update={"interoceptive_summary": summary}
                ).model_dump(mode="json"),
                "interoceptive_summary": summary.model_dump(mode="json"),
            }
        )
        return restored.model_copy(
            update={
                "status": "idempotent_replay",
                "idempotency_hit": True,
                # A legacy result without a snapshot cannot represent the
                # schema-migration revision.  Once it is enriched with the
                # authoritative v2 snapshot, keep the transport revision in
                # lockstep so Runtime never observes a regressing revision or
                # a summary/version-vector mismatch.
                "committed_revision": historical.revision,
                "result": payload,
            }
        )

    def _rank_policies(self, factors: dict[DimensionName, FactorBelief], *, dynamics=None) -> list[PolicyCandidate]:
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
            action = actions[dimension]
            if self.profile.cognitive_dynamics.mode == "active":
                dynamics = dynamics or self._dynamics
                if dimension == "cognitive_load" and factor.value <= high:
                    continue
                if dimension == "novelty" and factor.value < low and (not dynamics or dynamics.boredom < .5
                        or dynamics.fatigue >= .5 or dynamics.load >= .45 or not dynamics.wants_engagement):
                    continue
                if error <= 0:
                    continue
            if factor.value > high and dimension == "social_connection":
                action = ("quiet", "降低社交密度并留出独处空间", {"social_connection": -0.10})
            elif factor.value > high and dimension == "novelty":
                action = ("familiar", "减少新刺激并回到熟悉节奏", {"novelty": -0.09})
            scored.append((error * weight, dimension, action))
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
        if self.profile.cognitive_dynamics.mode == "active" or self.profile.watchdog_boundary_heartbeats is None:
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
            if self.profile.cognitive_dynamics.mode != "active" and max(factor.posterior.values()) > 0.999999:
                reasons.append(f"{dimension}:posterior_saturated")
            threshold = self.profile.watchdog_boundary_heartbeats
            if self.profile.cognitive_dynamics.mode != "active" and threshold is not None and boundary_streaks.get(dimension, 0) >= threshold:
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
            factors[dimension] = previous.model_copy(
                update={
                    "posterior": self._posterior(previous.value, spec.baseline_band),
                    "target_band": spec.baseline_band,
                }
            )
        affect = self._derive_affect(factors)
        watchdog = self._watchdog(factors, {}) if self.profile.cognitive_dynamics.mode == "active" else list(self.snapshot.watchdog)
        salience = derive_homeostatic_salience(
            self.profile,
            factors,
            previous=self._previous_salience(),
            watchdog=watchdog,
        )
        emotion = self._derive_emotion(
            affect,
            factors,
            self._started_at,
            salience=salience,
        )
        revision = self.snapshot.revision + 1
        candidates = self._rank_policies(factors)
        summary = self._derive_interoceptive_summary(
            revision=revision,
            factors=factors,
            affect=affect,
            emotion=emotion,
            candidates=candidates,
            watchdog=watchdog,
            salience=salience,
        )
        self.snapshot = self.snapshot.model_copy(
            update={
                "revision": revision,
                "profile_revision": self.profile.revision,
                "dimensions": factors,
                "core_affect": affect,
                "emotion": emotion,
                "candidates": candidates,
                "stable_snapshot_ref": (
                    f"snapshot://{self.profile.profile_id}/profile-migration-r{self.profile.revision}"
                    if not watchdog
                    else self.snapshot.stable_snapshot_ref
                ),
                "watchdog": watchdog,
                "interoceptive_summary": summary,
                "dynamics": self._dynamics_view(self._dynamics),
                "regulation_pressure": self._dynamics.pressure() if self._dynamics else {},
            }
        )
        self._persist()

    def _upgrade_legacy_snapshot_schema(self) -> None:
        """Materialize a v2 summary once without modifying factor evidence."""

        if (self.snapshot.interoceptive_summary is not None and self._loaded_snapshot_schema == "homeostasis-state-v3"
                and (self.snapshot.dynamics or {}).get("mode", "off") == self.profile.cognitive_dynamics.mode
                and (self.profile.cognitive_dynamics.mode == "off" or self.snapshot.dynamics is not None)):
            return
        factors = self.snapshot.dimensions
        watchdog = self._watchdog(factors, {}) if self.profile.cognitive_dynamics.mode == "active" else self.snapshot.watchdog
        affect = self._derive_affect(factors)
        salience = derive_homeostatic_salience(
            self.profile,
            factors,
            watchdog=watchdog,
        )
        emotion = self._derive_emotion(
            affect,
            factors,
            self.snapshot.updated_at,
            salience=salience,
        )
        revision = self.snapshot.revision + 1
        candidates = self._rank_policies(factors)
        summary = self._derive_interoceptive_summary(
            revision=revision,
            factors=factors,
            affect=affect,
            emotion=emotion,
            candidates=candidates,
            watchdog=watchdog,
            salience=salience,
        )
        self.snapshot = self.snapshot.model_copy(
            update={
                "revision": revision,
                "core_affect": affect,
                "emotion": emotion,
                "candidates": candidates,
                "interoceptive_summary": summary,
                "watchdog": watchdog,
                "dynamics": self._dynamics_view(self._dynamics),
                "regulation_pressure": self._dynamics.pressure() if self._dynamics else {},
                "stable_snapshot_ref": (
                    f"snapshot://{self.profile.profile_id}/schema-migration-v3"
                    if not self.snapshot.watchdog
                    else self.snapshot.stable_snapshot_ref
                ),
            }
        )
        self._persist()

    def _load(self) -> None:
        stored_snapshot = self._command_ledger.load_snapshot()
        if stored_snapshot is not None:
            self._loaded_snapshot_schema = stored_snapshot.get("schema_version", "homeostasis-state-v1")
            self.snapshot = HomeostaticSnapshot.model_validate(stored_snapshot)
        elif self.state_path.is_file():
            stored_snapshot = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._loaded_snapshot_schema = stored_snapshot.get("schema_version", "homeostasis-state-v1")
            self.snapshot = HomeostaticSnapshot.model_validate(stored_snapshot)
        engine_state = self._command_ledger.load_engine_state()
        if engine_state.get("cognitive_dynamics") is not None:
            self._dynamics = DynamicsState.model_validate(engine_state["cognitive_dynamics"])
        if engine_state.get("qq_activity_at") is not None:
            self._qq_activity_at = _aware(engine_state["qq_activity_at"], self.snapshot.updated_at)
        else:
            self._qq_activity_at = self.snapshot.updated_at
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

    def _engine_state_payload(
        self,
        boundary_streaks: dict[DimensionName, int],
        boundary_sides: dict[DimensionName, Literal["low", "high"]],
        *,
        qq_activity_at: datetime | None = None,
        dynamics_state: DynamicsState | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "boundary_heartbeat_streaks": dict(boundary_streaks),
            "boundary_heartbeat_sides": dict(boundary_sides),
        }
        if self.profile.qq_social_drive is not None and self.profile.qq_social_drive.enabled:
            result["qq_activity_at"] = (qq_activity_at or self._qq_activity_at).isoformat()
        dynamics_state = dynamics_state or self._dynamics
        if dynamics_state is not None:
            result["cognitive_dynamics"] = dynamics_state.model_dump(mode="json")
        return result

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
            replace_path(temporary, self.trace_path)
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
        replace_path(temporary, path)

    def _append_trace(self, trace: HomeostaticTransitionTrace) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.trace_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(trace.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
