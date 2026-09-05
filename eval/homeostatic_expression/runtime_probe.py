"""Real RuntimeSupervisor watchdog probe for every release evaluation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.contracts.envelope import (
    DomainCommandEnvelope,
    DomainCommandResult,
    VersionVector,
)
from harness.homeostasis import load_profile
from harness.homeostasis.models import (
    HomeostaticDominantNeed,
    HomeostaticEmotionHint,
    HomeostaticInteroceptiveSummary,
    HomeostaticSnapshot,
    initial_snapshot,
)
from harness.research.manifest import ExperimentManifest
from harness.runtime import (
    DomainSnapshot,
    InMemoryDeliveryBackend,
    PipelineCognitiveAdapter,
    RuntimeLifecycle,
    RuntimeSupervisor,
    ServiceHealth,
)
from harness.runtime.models import DeliveryResult

from .production import ProductionPairPipeline


class CountingDeliveryBackend(InMemoryDeliveryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def deliver(
        self,
        *,
        text: str,
        segments: list[str],
        channel: str,
        idempotency_key: str,
        deadline: datetime,
    ) -> DeliveryResult:
        self.attempts += 1
        return await super().deliver(
            text=text,
            segments=segments,
            channel=channel,
            idempotency_key=idempotency_key,
            deadline=deadline,
        )


class CriticalHomeostasisProbeService:
    """Contract-valid service that becomes critical on the probed user event."""

    def __init__(self, *, profile_path: Path, now: datetime) -> None:
        self.profile = load_profile(profile_path)
        self.snapshot = initial_snapshot(self.profile, now=now)
        self.apply_event_calls = 0
        self.closed = False

    def _snapshot_payload(self) -> dict[str, Any]:
        return {
            **self.snapshot.model_dump(mode="json"),
            "profile": self.profile.model_dump(mode="json"),
        }

    async def health(self) -> ServiceHealth:
        reasons = list(self.snapshot.watchdog)
        return ServiceHealth(
            service="homeostasis",
            healthy=not reasons,
            revision=self.snapshot.revision,
            acknowledged_receive_seq=self.snapshot.acknowledged_receive_seq,
            detail={"watchdog": reasons, "projection_errors": {}},
        )

    async def get_snapshot(self) -> DomainSnapshot:
        return DomainSnapshot(
            service="homeostasis",
            revision=self.snapshot.revision,
            acknowledged_receive_seq=self.snapshot.acknowledged_receive_seq,
            acknowledged_event_id=self.snapshot.acknowledged_event_id,
            component_revisions={"state": self.snapshot.revision},
            payload=self._snapshot_payload(),
        )

    async def apply_event(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
    ) -> DomainCommandResult[dict[str, Any]]:
        self.apply_event_calls += 1
        revision = self.snapshot.revision + 1
        summary = HomeostaticInteroceptiveSummary(
            profile_ref=self.profile.profile_ref,
            snapshot_revision=revision,
            salience="critical",
            dominant_needs=[
                HomeostaticDominantNeed(
                    dimension="cognitive_load",
                    direction="high",
                    normalized_deviation=1.0,
                    trend="worsening",
                    confidence=1.0,
                    semantic_need="需要降低负荷并简化当前任务",
                )
            ],
            emotion_hint=HomeostaticEmotionHint(label="strained", intensity=1.0),
            policy_refs=[],
            episode_signature="eval:critical-runtime-probe",
        )
        self.snapshot = HomeostaticSnapshot.model_validate(
            self.snapshot.model_copy(
                update={
                    "revision": revision,
                    "acknowledged_receive_seq": command.receive_seq,
                    "acknowledged_event_id": command.event_id,
                    "updated_at": command.issued_at,
                    "watchdog": ["cognitive_load:hard_bound_sustained"],
                    "interoceptive_summary": summary,
                }
            ).model_dump(mode="json")
        )
        payload = {
            "revision": revision,
            "snapshot": self.snapshot.model_dump(mode="json"),
            "interoceptive_summary": summary.model_dump(mode="json"),
        }
        return DomainCommandResult[dict[str, Any]](
            command_id=command.command_id,
            command_type=command.command_type,
            status="applied",
            request_hash=command.effective_request_hash(),
            committed_revision=revision,
            acknowledged_event_id=command.event_id,
            acknowledged_receive_seq=command.receive_seq,
            result=payload,
            completed_at=command.issued_at,
        )

    async def apply_heartbeat(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self.apply_event(command)

    async def apply_outcome(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self.apply_event(command)

    async def assimilate_observation(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self.apply_event(command)

    async def apply_observation(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self.apply_event(command)

    async def propose_policies(
        self,
        *,
        version_vector: VersionVector,
    ) -> list[dict[str, Any]]:
        del version_vector
        return []

    async def close(self) -> None:
        self.closed = True


def run_critical_runtime_probe(
    *,
    production: ProductionPairPipeline,
    manifest: ExperimentManifest,
    profile_path: Path,
    state_dir: Path,
) -> dict[str, Any]:
    """Run the real supervisor path and count every forbidden side effect."""

    return asyncio.run(
        _run_critical_runtime_probe(
            production=production,
            manifest=manifest,
            profile_path=profile_path,
            state_dir=state_dir,
        )
    )


async def _run_critical_runtime_probe(
    *,
    production: ProductionPairPipeline,
    manifest: ExperimentManifest,
    profile_path: Path,
    state_dir: Path,
) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    before_main = len(production.router.main_calls)
    before_aux = len(production.router.auxiliary_calls)
    cognitive = PipelineCognitiveAdapter(
        production.pipeline,
        state_path=state_dir / "cognitive-state.json",
        autonomous_log_dir=state_dir / "autonomous",
        default_session_id="sess_critical_runtime_probe",
    )
    homeostasis = CriticalHomeostasisProbeService(
        profile_path=profile_path,
        now=datetime.now(UTC),
    )
    delivery = CountingDeliveryBackend()
    supervisor = RuntimeSupervisor(
        manifest=manifest,
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=state_dir / "runtime",
        runtime_instance_id="runtime-homeostatic-expression-critical-probe",
        delivery_backend=delivery,
        maintenance_drain_seconds=1.0,
    )
    dispatch_result = None
    lifecycle_after_dispatch = supervisor.lifecycle
    error: str | None = None
    try:
        await supervisor.boot_and_recover()
        event = await supervisor.submit_user_message(
            "critical runtime probe",
            session_id="sess_critical_runtime_probe",
            event_id="evt_homeostatic_expression_critical_probe",
            correlation_id="corr_homeostatic_expression_critical_probe",
        )
        if event is None:
            raise RuntimeError("runtime rejected critical probe event")
        queued = await supervisor.queue.get(timeout=0)
        if queued is None:
            raise RuntimeError("critical probe event was not queued")
        dispatch_result = await supervisor.dispatch(queued)
        lifecycle_after_dispatch = supervisor.lifecycle
    except Exception as exc:  # noqa: BLE001 - evidence must retain probe failures
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if supervisor.lifecycle != RuntimeLifecycle.STOPPED:
            try:
                await supervisor.stop(reason="critical evaluation probe complete")
            except Exception as exc:  # noqa: BLE001
                suffix = f"{type(exc).__name__}: {exc}"
                error = f"{error}; stop={suffix}" if error else f"stop={suffix}"

    counts = {
        "auxiliary_model_calls": len(production.router.auxiliary_calls) - before_aux,
        "main_model_calls": len(production.router.main_calls) - before_main,
        "delivery_calls": delivery.attempts,
    }
    paused_synchronously = lifecycle_after_dispatch == RuntimeLifecycle.PAUSED
    watchdog_decision = dispatch_result is not None and dispatch_result.decision_kind == "watchdog_pause"
    passed = (
        error is None
        and all(value == 0 for value in counts.values())
        and paused_synchronously
        and watchdog_decision
        and homeostasis.apply_event_calls == 1
    )
    return {
        "record_type": "critical_runtime_probe",
        "implementation": {
            "runtime": "RuntimeSupervisor",
            "cognitive": "PipelineCognitiveAdapter[InferencePipeline]",
            "homeostasis": "contract-valid critical transition probe",
            "delivery": "CountingDeliveryBackend",
        },
        "counts": counts,
        "homeostasis_apply_event_calls": homeostasis.apply_event_calls,
        "critical_revision": homeostasis.snapshot.revision,
        "critical_salience": (
            homeostasis.snapshot.interoceptive_summary.salience
            if homeostasis.snapshot.interoceptive_summary is not None
            else None
        ),
        "watchdog": list(homeostasis.snapshot.watchdog),
        "dispatch_status": dispatch_result.status if dispatch_result is not None else None,
        "decision_kind": (dispatch_result.decision_kind if dispatch_result is not None else None),
        "lifecycle_after_dispatch": lifecycle_after_dispatch.value,
        "paused_synchronously": paused_synchronously,
        "error": error,
        "passed": passed,
    }
