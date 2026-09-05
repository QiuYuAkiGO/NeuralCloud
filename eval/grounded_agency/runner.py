"""Bounded GMV trials through production Runtime, compiler, sensor and action ports."""

from __future__ import annotations

import asyncio
import inspect
import json
import platform
import subprocess
import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from harness.config import Settings
from harness.agency.models import AgencyConfig
from harness.core.models import PersonaCore, PersonaIdentity
from harness.embodiment.capabilities import (
    ACTION_EXECUTE, ACTION_RECONCILE, ACTION_SAFE_POINT, ACTION_VERIFY, EVIDENCE_REFERENCE, CapabilityRequirement,
)
from harness.embodiment.manager import EmbodimentManager
from harness.memory.service import build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline
from harness.research.grounded_agency.manifest import (
    ArtifactIdentity, GroundedAgencyCondition, GroundedAgencyEvaluation, GroundedAgencyRuntimeOptions, SensorTransformSpec,
)
from harness.research.manifest import EmbodimentCondition, ExperimentManifest
from harness.runtime.action import ActionCoordinator
from harness.runtime.adapters import PassiveHomeostasisAdapter, PipelineCognitiveAdapter
from harness.runtime.models import RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle
from harness.runtime.supervisor import RuntimeSupervisor

from eval.grounded_agency.evidence import (
    EvidenceStore, canonical_json, check_trial, digest, validate_run, write_json,
)
from eval.grounded_agency.fixtures import FakeDecisionLLM, FixtureClock, ScenarioSimulator
from eval.grounded_agency.scenarios import load_scenario
from eval.grounded_agency.schema import (
    AgencyGoalSummary, DecisionRecord, GMVScenario, GMVTrialEvidence, GMVTrialSpec, RunResult, StepRecord, TrialIntegrityReport,
)

CONDITIONS = {
    "C0": "no_body", "C1": "semantic_prompt", "C2": "opaque_current", "C3": "opaque_history",
    "C4": "opaque_history_plus_objective_memory", "C5": "engineered_policy_control", "C6": "sham_sensor",
}
PROMPT_IDENTITY = "gmv-runtime-structured-v1"
PERSONA = {"name": "Participant", "core_traits": [], "values": [], "speech_style": [], "knowledge_boundary": [], "locked": True}
ModelFactory = Callable[[int], Any]
ProviderFactory = Callable[[GMVScenario, int, FixtureClock], Any]


def condition_name(value: str) -> str:
    if value in CONDITIONS:
        return CONDITIONS[value]
    if value not in CONDITIONS.values():
        raise ValueError(f"unknown condition: {value}")
    return value


def repository_commit() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
                            cwd=Path(__file__).resolve().parents[2])
    return result.stdout.strip()


def artifact_identity(*, model_id: str = FakeDecisionLLM.model_id, environment_digest: str | None = None) -> ArtifactIdentity:
    return ArtifactIdentity(
        neuralcloud_commit=repository_commit(), model_id=model_id,
        model_digest=digest({"model_id": model_id}), tokenizer_digest=digest("builtin-character-estimator-v1"),
        prompt_digest=digest(PROMPT_IDENTITY), persona_digest=digest(PERSONA), environment_digest=environment_digest,
    )


def make_manifest(scenario: GMVScenario, condition: str, *, seed: int = 0,
                  identity: ArtifactIdentity | None = None) -> ExperimentManifest:
    name = condition_name(condition)
    opaque = name in {"opaque_current", "opaque_history", "opaque_history_plus_objective_memory", "sham_sensor"}
    engineered = name == "engineered_policy_control"
    ga = GroundedAgencyCondition(
        protocol_version="grounded-agency-v1", condition_id=name,
        interoception_treatment=("opaque_current" if name in {"opaque_current", "sham_sensor"} else
            "opaque_history" if opaque else "semantic_summary" if name == "semantic_prompt" else
            "engineered_policy" if engineered else "none"),
        memory_treatment="objective_events" if name == "opaque_history_plus_objective_memory" else "none",
        action_selection="engineered_argmin" if engineered else "model_structured",
        report_channel="normal" if scenario.allow_language_report else "action_only",
        semantic_homeostasis=name == "semantic_prompt", homeostatic_policy_ranking=engineered,
        derived_emotion_visible=False, observer_social_presence="present" if scenario.observer_present else "absent",
        sensor_transform=SensorTransformSpec(
            transform_id="gmv-symbols-v1", seed=seed, channel_alias="s0", encoding="ordinal_symbols",
            direction="direct", noise_profile="none", delay_profile="none",
        ) if opaque else None,
        artifact_identity=identity or artifact_identity(environment_digest=digest(scenario)),
        evaluation=GroundedAgencyEvaluation(primary_result="structured_decision",
            decision_schema_ref="experimental-decision-v1", requires_text_reply=False),
        runtime=GroundedAgencyRuntimeOptions(permitted_intents=scenario.permitted_intents),
        agency=AgencyConfig(enabled=True, minimum_reconsideration_interval=1) if scenario.agency_enabled else None,
    )
    return ExperimentManifest(
        experiment_id=f"gmv-{scenario.scenario_id}", revision=1, control_mode="development_debug",
        condition="argmin_efe" if engineered else "llm_open", persona_version="neutral-fixture-v1",
        homeostatic_profile="passive-v1", random_seed=seed,
        initial_snapshot_ref=f"frozen://{digest(scenario.initial_body_state)}", model_version=ga.artifact_identity.model_id,
        prompt_version=PROMPT_IDENTITY, schema_version="gmv-trial-v1", a_version="fixture-v1", b_version="fixture-v1",
        c_version="fixture-v1", precision_version="fixture-v1", coupling_version="fixture-v1",
        priority_policy="runtime-v1", environment=scenario.environment_fixture, retention="local-research",
        watchdog_action="pause_preserve_state", grounded_agency=ga,
        embodiment=EmbodimentCondition(mode="active", body_instance_id="gmv-body",
            capability_requirements=[CapabilityRequirement(capability=name, required=True) for name in (
                ACTION_EXECUTE, ACTION_VERIFY, ACTION_SAFE_POINT, ACTION_RECONCILE, EVIDENCE_REFERENCE,
            )]),
    )


class FixtureHomeostasis(PassiveHomeostasisAdapter):
    """An explicitly engineered resource-cost baseline; never used for model choices."""

    def __init__(self, provider: Any, *, state_path: Path) -> None:
        super().__init__(state_path=state_path)
        self.provider = provider

    async def propose_policies(self, *, version_vector):
        menu = self.provider.visible_menu()
        candidates = []
        for operation in menu["operations"]:
            arguments = operation["arguments"]
            is_task = operation["intent_id"] == "task.execute.v1"
            if is_task:
                option = next(item for item in self.provider.scenario.task_spec.options if item.option_id == arguments["option_id"])
                score = option.cost_units * self.provider.multiplier
            else:
                score = self.provider.station.travel_cost_units + self.provider.station.use_cost_units - self.provider.station.gain_units
            candidates.append((score, operation, is_task))
        return [{
            "policy_id": f"engineered-{rank}", "label": operation["label"], "rank": rank,
            "experimental_decision": {
                "kind": "act", "intent": {"intent_id": operation["intent_id"], "arguments": operation["arguments"]},
                "success_criteria": [{"kind": "scenario", "condition": "task_completed" if is_task else "station_used",
                    "target_id": operation["arguments"].get("option_id", operation["arguments"].get("station_id"))}],
                "confidence": 1.0,
            },
        } for rank, (_, operation, is_task) in enumerate(sorted(candidates, key=lambda item: (item[0], item[1]["label"])), 1)]


class RuntimeTrialDriver:
    """One isolated production Supervisor and its owned services per trial."""

    def __init__(self, scenario: GMVScenario, manifest: ExperimentManifest, provider: Any, model: Any,
                 state_dir: Path, *, timeout_seconds: float = 30.0, sensor_ratio_override: float | None = None) -> None:
        settings = Settings(parser_llm_enabled=False, planner_llm_enabled=False, tier_llm_enabled=False,
                            validator_sample_rate=0, continuation_enabled=False, call_log_enabled=False,
                            context_token_budget=scenario.context_token_budget)
        memory = build_inmemory_service(settings=settings, llm=model)
        memory.bootstrap_persona(PersonaCore(persona_id="gmv-persona", version="neutral-fixture-v1",
            identity=PersonaIdentity.model_validate(PERSONA), created_at=FixtureClock()()))
        self.memory = memory
        self.cognitive = PipelineCognitiveAdapter(InferencePipeline(memory, settings=settings, llm=model),
            state_path=state_dir / "cognitive.json", autonomous_log_dir=state_dir / "autonomous", default_session_id="gmv-session")
        self.homeostasis = FixtureHomeostasis(provider, state_path=state_dir / "homeostasis.json")
        self.runtime = RuntimeSupervisor(
            manifest=manifest, cognitive=self.cognitive, homeostasis=self.homeostasis, state_dir=state_dir / "runtime",
            runtime_instance_id="gmv-runtime", embodiment_manager=EmbodimentManager(manifest.embodiment, provider),
            action_coordinator=ActionCoordinator(provider), command_timeout_seconds=timeout_seconds,
            action_timeout_seconds=timeout_seconds, maintenance_drain_seconds=min(timeout_seconds, 1.0),
            grounded_sensor_transform=(lambda _body: sensor_ratio_override) if sensor_ratio_override is not None else None,
            research_clock=provider.fixture_clock,
            research_world_signature=lambda: digest({key: value for key, value in provider.visible_menu().items() if key != "step"}),
        )
        if sensor_ratio_override is not None:
            if not 0 <= sensor_ratio_override <= 1:
                raise ValueError("sensor override ratio must be in [0,1]")
        self.provider, self.model = provider, model
        self.closed = False

    async def start(self) -> None:
        await self.runtime.boot_and_recover()
        if self.runtime.lifecycle != RuntimeLifecycle.RUNNING:
            raise RuntimeError("provider activation did not yield a running Runtime")

    async def dispatch(self, kind: RuntimeEventType, payload: dict[str, Any], *, event_id: str, moment: datetime):
        await self.runtime.ingress.accept(event_type=kind, priority=RuntimeEventPriority.IMPORTANT_ENVIRONMENT,
            payload=payload, source="gmv-runner", source_time=moment, event_id=event_id,
            correlation_id=event_id, idempotency_key=event_id)
        if self.runtime.lifecycle == RuntimeLifecycle.PAUSED and kind in {RuntimeEventType.BODY_STATE, RuntimeEventType.ENVIRONMENT_CHANGE}:
            return None  # Runtime durably records these in its pause observation buffer.
        event = await self.runtime.queue.get(timeout=0)
        while event is not None and event.event_id != event_id and event.event_type in {
            RuntimeEventType.RESUME_OBSERVATION, RuntimeEventType.ACTION_RESULT, RuntimeEventType.BODY_STATE,
        }:
            await self.runtime.dispatch(event)
            event = await self.runtime.queue.get(timeout=0)
        if event is None or event.event_id != event_id:
            raise RuntimeError("trial queue contains unexpected residual events")
        result = await self.runtime.dispatch(event)
        if result.status in {"failed", "deferred"}:
            raise RuntimeError(str(result.detail))
        return result

    async def observe_body(self, body, *, step: int) -> None:
        await self.dispatch(RuntimeEventType.BODY_STATE, {"body_state": body.model_dump(mode="json")},
                            event_id=f"body-{step}-{body.revision}", moment=body.captured_at)

    async def decide(self, text: str, *, step: int, moment: datetime, entity_observations: list | None = None,
                     agency_trigger: str | None = None) -> tuple[dict[str, Any], Any]:
        event_id = f"{getattr(self, 'decision_namespace', '')}step-{step}"
        payload = {"text": text, "session_id": "gmv-session", "channel": "experiment"}
        if entity_observations:
            payload["entity_observations"] = entity_observations
        if agency_trigger is not None:
            payload["agency_trigger"] = agency_trigger
        result = await self.dispatch(RuntimeEventType.ENVIRONMENT_CHANGE, payload, event_id=event_id, moment=moment)
        action_id = result.detail.get("action_id") or (result.detail.get("action") or {}).get("action_id")
        if action_id:
            while True:
                event = await self.runtime.queue.get(timeout=0.1)
                if event is not None:
                    await self.runtime.dispatch(event)
                    if event.event_type == RuntimeEventType.ACTION_RESULT and event.payload.get("action_id") == action_id:
                        break
        action = self.runtime.action_coordinator.get(action_id) if action_id else None
        return self.cognitive.research_traces.get(event_id, {}), action

    async def close(self) -> None:
        try:
            try:
                await self.runtime.stop(reason="bounded trial finished")
            except Exception:
                # A failed bounded trial must not leave its owned backend coroutine
                # running. Its uncertain outcome remains in the Runtime journal.
                pending = [task for task in self.runtime._action_tasks.values() if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await self.runtime.stop(reason="bounded trial cleanup after cancelled backend")
        finally:
            for target in (self.provider, self.model):
                method = getattr(target, "aclose", None) or getattr(target, "close", None)
                if method is not None:
                    result = method()
                    if inspect.isawaitable(result):
                        await result
            self.closed = self.runtime.lifecycle == RuntimeLifecycle.STOPPED


async def run_trial(*, run_dir: Path, scenario: GMVScenario, manifest: ExperimentManifest,
                    trial_id: str, repetition: int = 0, seed: int = 0, model_factory: ModelFactory | None = None,
                    provider_factory: ProviderFactory | None = None, timeout_seconds: float = 30.0,
                    sensor_ratio_override: float | None = None, semantic_prompt: str | None = None,
                    intervention_id: str | None = None, analysis_plan_hash: str | None = None,
                    frozen_source_snapshot_hash: str | None = None,
                    frozen_belief_snapshot: Any | None = None, include_beliefs: bool = True) -> GMVTrialEvidence:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if semantic_prompt and manifest.grounded_agency.is_opaque:
        raise ValueError("semantic prompt interventions cannot be inserted into an opaque condition")
    root = run_dir.resolve()
    state_dir = root / "state" / trial_id
    state_dir.mkdir(parents=True, exist_ok=False)
    store = EvidenceStore(root, trial_id)
    clock = FixtureClock()
    started_at = datetime.now(UTC)
    baseline_tasks = set(asyncio.all_tasks())
    provider = model = None
    driver = None
    body_pointers, sensor_pointers, context_pointers, outcome_pointers, snapshots, runtime_pointers = [], [], [], [], [], []
    records, steps, decisions, context_hashes, transform_pointers, agency_pointers, agency_summary = [], [], [], [], [], [], []
    memory_audit_pointers = []
    initial_hashes, final_hashes = {}, {}
    status, error, closed = "completed", None, False
    before = None
    final_body = None

    def snapshot(kind: str, payload: Any, hashes: dict[str, str]) -> None:
        pointer = store.write(f"snapshot-{kind}", payload)
        snapshots.append(pointer)
        hashes[kind] = pointer.digest

    try:
        frozen = frozen_belief_snapshot
        if scenario.frozen_snapshot_ref is not None:
            if frozen is not None:
                raise ValueError("only one frozen belief snapshot source may be supplied")
            frozen = json.loads(Path(scenario.frozen_snapshot_ref).read_text(encoding="utf-8"))
        if frozen is not None:
            from harness.body_belief import BodyBeliefStore
            from harness.body_belief.models import FrozenBeliefSnapshot
            frozen = FrozenBeliefSnapshot.model_validate(frozen.model_dump(mode="json") if hasattr(frozen, "model_dump") else frozen)
            BodyBeliefStore._validate_snapshot(frozen)
            moments = [moment for item in frozen.experiences for moment in
                       (item.completed_at, item.observation_after.observed_at if item.observation_after else item.completed_at)]
            if moments and max(moments) >= clock():
                clock.advance((max(moments) - clock()).total_seconds() + 1)
        provider = (provider_factory or ScenarioSimulator)(scenario, seed, clock)
        model = (model_factory or FakeDecisionLLM)(seed)
        initial = await provider.snapshot()
        snapshot("body", initial, initial_hashes)
        snapshot("environment", provider.environment_snapshot(), initial_hashes)
        snapshot("memory", {"persona": PERSONA, "objective_events": []}, initial_hashes)
        driver = RuntimeTrialDriver(scenario, manifest, provider, model, state_dir,
            timeout_seconds=timeout_seconds,
            sensor_ratio_override=sensor_ratio_override if sensor_ratio_override is not None else
                (0.5 if manifest.grounded_agency.condition_id == "sham_sensor" else None))
        if frozen is not None:
            driver.decision_namespace = f"snapshot-{frozen.content_hash()[:20]}-"
            if driver.runtime._research_memory is None:
                if any((frozen.experiences, frozen.candidates, frozen.beliefs, frozen.reliabilities)):
                    raise ValueError("nonempty frozen belief snapshot requires a configured body memory treatment")
            else:
                driver.runtime._research_memory.service.restore(frozen, include_beliefs=include_beliefs)
            snapshot("body_belief", frozen, initial_hashes)
        await asyncio.wait_for(driver.start(), timeout=timeout_seconds)
        snapshot("runtime", driver.runtime.status(), initial_hashes)

        async def execute_steps() -> None:
            nonlocal before, final_body, status
            for step in range(scenario.horizon_steps):
                provider.transition(step)
                agency_trigger = None
                for scheduled_event in scenario.agency_events:
                    if scheduled_event.step != step:
                        continue
                    if scheduled_event.kind == "interruption":
                        await driver.runtime.pause(reason="scripted higher priority interruption")
                    elif scheduled_event.kind == "resume":
                        # The observation is physically refreshed before Runtime resumes.
                        fresh_body = await provider.snapshot()
                        await driver.observe_body(fresh_body, step=step)
                        agency_pointers.append(store.write("resume-observation", {
                            "body": fresh_body.model_dump(mode="json"), "environment": provider.environment_snapshot(),
                            "observed_at": clock().isoformat(),
                        }))
                        await driver.runtime.resume(reason="scripted resume after fresh observation")
                        agency_trigger = "recovery"
                    elif scheduled_event.kind == "checkpoint":
                        driver.runtime.write_checkpoint()
                        agency_trigger = "safe_point"
                    elif scheduled_event.kind == "deadline":
                        agency_trigger = "deadline"
                    elif scheduled_event.kind == "world_change":
                        provider.apply_world_change(scheduled_event.world_change)
                    elif scheduled_event.kind == "delayed_outcome":
                        provider.next_action_delay_seconds = scheduled_event.delay_seconds
                if driver.runtime.lifecycle == RuntimeLifecycle.PAUSED:
                    clock.advance(1)
                    continue
                before = await provider.snapshot()
                before_pointer = store.write("body-truth", before)
                body_pointers.append(before_pointer)
                scheduled = next((item for item in scenario.observation_schedule if item.step == step), None)
                if not scenario.observation_schedule or (scheduled is not None and scheduled.body):
                    await driver.observe_body(before, step=step)
                    if driver.runtime._opaque_store is not None:
                        envelope = driver.runtime._opaque_store.envelopes()[-1]
                        sensor_pointers.append(store.write("sensor-envelope", envelope))
                        transform_pointers[:] = [store.write("sensor-transform", record) for record in driver.runtime._opaque_store.records()]
                if before.energy.terminal and scenario.terminal_ends_trial:
                    status, final_body = "terminal", before
                    break
                menu = provider.visible_menu()
                if scenario.observation_schedule and (scheduled is None or not scheduled.environment):
                    menu = {"step": step, "operations": []}
                text = scenario.task_spec.instruction + "\nGMV_MENU=" + canonical_json(menu)
                if semantic_prompt:
                    text += "\n" + semantic_prompt
                entity_hook = getattr(provider, "entity_observations", None)
                entities = entity_hook(step) if callable(entity_hook) else None
                if inspect.isawaitable(entities):
                    entities = await entities
                trace, action = await driver.decide(text, step=step, moment=clock(),
                    entity_observations=entities, agency_trigger=agency_trigger)
                decision = trace.get("decision")
                if not decision:
                    raise RuntimeError("Runtime produced no auditable decision trace")
                if trace.get("success") is False:
                    raise RuntimeError("model structured decision failed validation or transport")
                decision_id = f"decision-{step}"
                action_id = f"action-{step}" if action is not None else None
                decisions.append(decision)
                attempts = trace.get("attempts") or [{"messages": trace.get("messages", [])}]
                for attempt in attempts:
                    messages = attempt["messages"]
                    context_hash = digest(messages)
                    context_pointers.append(store.write("model-context", {
                        "messages": messages, "context_hash": context_hash, "manifest_hash": manifest.content_hash(),
                        "decision_id": decision_id,
                    }))
                    context_hashes.append(context_hash)
                records.append(DecisionRecord(step=step, decision_id=decision_id, context_hash=context_hash,
                    raw_output_hash=trace.get("raw_output_hash"), action_id=action_id, decision_kind=decision["kind"],
                    source=manifest.grounded_agency.action_selection, model_called=manifest.grounded_agency.action_selection == "model_structured"))
                if action is not None:
                    outcome_pointers.append(store.write("action-outcome", {
                        "decision_id": decision_id, "action_id": action_id,
                        "intent_id": action.embodied_action.intent.intent_id,
                        "arguments": action.embodied_action.intent.arguments,
                        "success": action.state.value == "SUCCEEDED", "state": action.state.value,
                        "summary": (action.outcome or {}).get("summary"),
                    }))
                after = await provider.snapshot()
                after_pointer = store.write("body-truth", after)
                body_pointers.append(after_pointer)
                action_info = provider.last_action if action is not None else {}
                steps.append(StepRecord(step=step, decision_id=decision_id, action_id=action_id,
                    body_before_hash=before_pointer.digest, body_after_hash=after_pointer.digest,
                    before_ratio=before.energy.available_ratio, after_ratio=after.energy.available_ratio,
                    action_success=action.state.value == "SUCCEEDED" if action is not None else None,
                    terminal=after.energy.terminal, **action_info))
                final_body = after
                if after.energy.terminal and scenario.terminal_ends_trial:
                    status = "terminal"
                    break
                if action is None:
                    clock.advance(1)
            if status == "completed" and not outcome_pointers:
                status = "no_action"
        await asyncio.wait_for(execute_steps(), timeout=timeout_seconds)
    except TimeoutError:
        status, error = "timeout", "trial deadline exceeded"
    except Exception as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"
    finally:
        if driver is not None and getattr(driver.runtime, "_agency_service", None) is not None:
            agency_state = driver.runtime._agency_service.store.snapshot()
            agency_pointers.append(store.write("agency-state", agency_state))
            agency_objects = agency_state["objects"]
            for goal in agency_objects.get("goal", {}).values():
                plan = agency_objects.get("plan", {}).get(goal["goal_id"], {})
                outcomes = [item for item in agency_objects.get("action_outcome", {}).values() if item["goal_id"] == goal["goal_id"]]
                agency_summary.append(AgencyGoalSummary(goal_id=goal["goal_id"], status=goal["status"], origin=goal["origin"],
                    revision=goal["revision"], plan_revision=plan.get("revision"),
                    completed_step_ids=tuple(sorted(item["step_id"] for item in outcomes if item["state"] == "succeeded")),
                    unknown_step_ids=tuple(sorted(item["step_id"] for item in outcomes if item["state"] == "unknown")),
                    prospective_requirement_count=len(plan.get("prospective_requirements", []))))
            for trace in driver.runtime._agency_service.traces.values():
                agency_pointers.append(store.write("agency-trace", trace))
        # Capture while the port is active; reread the serialized snapshots after close.
        try:
            final_body = await provider.snapshot()
            snapshot("body", final_body, final_hashes)
            snapshot("environment", provider.environment_snapshot(), final_hashes)
        except Exception as exc:
            error = (error + "; " if error else "") + f"final snapshot: {exc}"
        if driver is not None:
            try:
                if driver.runtime._research_memory is not None:
                    memory = driver.runtime._research_memory.service
                    if not await memory.drain(timeout_seconds):
                        raise RuntimeError("body belief appraisal did not drain before its final snapshot")
                    await memory.pause()
                    snapshot("body_belief", memory.snapshot(), final_hashes)
            except Exception as exc:
                error = (error + "; " if error else "") + f"shutdown: {exc}"
                status = "failed"
            try:
                await asyncio.wait_for(driver.close(), timeout=max(2.0, timeout_seconds))
                closed = driver.closed
            except Exception as exc:
                error = (error + "; " if error else "") + f"shutdown: {exc}"
                status = "failed"
        else:
            for target in (provider, model):
                close = getattr(target, "aclose", None) or getattr(target, "close", None)
                if close:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
            closed = True
        # Flush and reread final state after services stopped; failures retain evidence.
        if driver is not None:
            for label, filename in (("cognitive", "cognitive.json"), ("homeostasis", "homeostasis.json"), ("runtime", "runtime/checkpoint.json")):
                path = state_dir / filename
                if path.is_file():
                    snapshot(label, json.loads(path.read_text(encoding="utf-8")), final_hashes)
            runtime_pointers.append(store.write("runtime-journal", [r.model_dump(mode="json") for r in driver.runtime.journal.records()]))
            audit_path = state_dir / "runtime" / "body-beliefs.sqlite3"
            if audit_path.is_file():
                try:
                    with sqlite3.connect(audit_path.as_uri() + "?mode=ro", uri=True) as audit_db:
                        audit_rows = audit_db.execute("SELECT data FROM audit ORDER BY rowid").fetchall()
                    memory_audit_pointers.append(store.write("body-memory-audit", [json.loads(row[0]) for row in audit_rows]))
                except Exception as exc:
                    error = (error + "; " if error else "") + f"final memory audit: {exc}"
                    status = "failed"
        leftovers = [task for task in asyncio.all_tasks() - baseline_tasks if not task.done() and task is not asyncio.current_task()]
        no_tasks = not leftovers
        for task in leftovers:
            task.cancel()
        if leftovers:
            await asyncio.gather(*leftovers, return_exceptions=True)

    report = TrialIntegrityReport(passed=True, invalid_for_analysis=False,
        checks={"services_closed": closed, "no_background_tasks": no_tasks}) if closed and no_tasks else TrialIntegrityReport(
            passed=False, invalid_for_analysis=True, checks={"services_closed": closed, "no_background_tasks": no_tasks})
    spec = GMVTrialSpec(trial_id=trial_id, scenario_id=scenario.scenario_id, condition_id=manifest.grounded_agency.condition_id,
        repetition=repetition, seed=seed, manifest_hash=manifest.content_hash(), initial_snapshot_hashes=initial_hashes,
        model_sample_seed=seed, state_path=state_dir.relative_to(root).as_posix(),
        initial_resource_ratio=scenario.initial_body_state.available_units / scenario.initial_body_state.capacity_units,
        intervention_id=intervention_id, analysis_plan_hash=analysis_plan_hash,
        frozen_source_snapshot_hash=frozen_source_snapshot_hash)
    trial = GMVTrialEvidence(trial=spec, started_at=started_at, completed_at=datetime.now(UTC), terminal_status=status,
        body_truth_events=tuple(body_pointers), sensor_observations=tuple(sensor_pointers),
        sensor_transform_records=tuple(transform_pointers),
        model_context_hashes=tuple(context_hashes), model_contexts=tuple(context_pointers), decisions=tuple(decisions),
        decision_records=tuple(records), action_outcomes=tuple(outcome_pointers), step_records=tuple(steps),
        snapshots=tuple(snapshots), runtime_evidence=tuple(runtime_pointers), final_snapshot_hashes=final_hashes,
        agency_evidence=tuple(agency_pointers),
        agency_summary=tuple(agency_summary),
        body_memory_audit=tuple(memory_audit_pointers),
        integrity=report, error=error, fixture_only=bool(getattr(model, "fixture_only", False)))
    return trial.model_copy(update={"integrity": check_trial(root, trial, opaque=manifest.grounded_agency.is_opaque,
        require_body_memory_audit=manifest.grounded_agency.memory_treatment != "none")})


async def run_experiment(output_dir: Path | str, scenarios: Sequence[GMVScenario | str] = ("action_only_v1",),
                         conditions: Sequence[str] = ("opaque_current",), *, repetitions: int = 1, seed: int = 0,
                         provider: str = "simulator", live: bool = False, model_factory: ModelFactory | None = None,
                         provider_factory: ProviderFactory | None = None, timeout_seconds: float = 30.0,
                         model_id: str | None = None, sensor_ratio_override: float | None = None,
                         semantic_prompt: str | None = None, intervention_id: str | None = None,
                         analysis_plan_hash: str | None = None, frozen_source_snapshot_hash: str | None = None,
                         condition_overrides: dict[str, GroundedAgencyCondition] | None = None,
                         frozen_belief_snapshot: Any | None = None, include_beliefs: bool = True) -> RunResult:
    if type(repetitions) is not int or repetitions < 1 or type(seed) is not int or seed < 0:
        raise ValueError("repetitions must be positive and seed nonnegative integers")
    if not live and (provider != "simulator" or provider_factory is not None and not getattr(provider_factory, "fixture_only", False)):
        raise ValueError("non-simulator providers require explicit live=True")
    if not live and model_factory is not None and not getattr(model_factory, "fixture_only", False):
        raise ValueError("custom model factories require fixture_only=True or explicit live=True")
    if live and (model_factory is None or not model_id or (provider != "simulator" and provider_factory is None)):
        raise ValueError("live execution requires an explicit model factory/model identity and a factory for non-simulator providers")
    if not scenarios or not conditions:
        raise ValueError("at least one scenario and condition is required")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("run output directory must be empty")
    for directory in ("manifests", "scenarios", "evidence", "logs"):
        (root / directory).mkdir()
    resolved = [load_scenario(source) for source in scenarios]
    names = [condition_name(name) for name in conditions]
    identity = artifact_identity(model_id=model_id or FakeDecisionLLM.model_id)
    lock = Path(__file__).resolve().parents[2] / "uv.lock"
    if not lock.exists():
        lock = Path(__file__).resolve().parents[2] / "pyproject.toml"
    metadata = {
        "schema_version": "gmv-run-v1", "neuralcloud_commit": identity.neuralcloud_commit, "mistyai_commit": None,
        "python_version": sys.version, "dependency_lock_digest": digest(lock.read_text(encoding="utf-8")),
        "dependency_versions": {"pydantic": version("pydantic")}, "model_id": identity.model_id,
        "prompt_digest": identity.prompt_digest, "persona_digest": identity.persona_digest,
        "tokenizer_identity": "builtin-character-estimator-v1", "scenario_order": [s.scenario_id for s in resolved],
        "condition_order": names, "run_seed": seed, "started_at": datetime.now(UTC).isoformat(), "completed_at": None,
        "platform": platform.platform(), "host": platform.node(), "provider": provider, "live": live,
        "release_eligible": False, "fixture_only": not live,
        "scientific_claim": "No hypothesis conclusion; fake-model outputs validate execution plumbing only.",
        "manifests": {}, "scenarios": {}, "condition_manifest_groups": [], "trial_count": 0, "services_closed": False,
    }
    write_json(root / "metadata.json", metadata)
    trials = []
    with (root / "trials.jsonl").open("w", encoding="utf-8", newline="\n") as journal:
        for scenario in resolved:
            levels = scenario.initial_resource_levels or (scenario.initial_body_state.available_units / scenario.initial_body_state.capacity_units,)
            for level_index, ratio in enumerate(levels):
                concrete = scenario.model_copy(update={"initial_body_state": scenario.initial_body_state.model_copy(
                    update={"available_units": ratio * scenario.initial_body_state.capacity_units})})
                sha = digest(concrete)
                scenario_path = f"scenarios/{sha}.json"
                write_json(root / scenario_path, concrete)
                metadata["scenarios"][sha] = {"path": scenario_path, "scenario_id": concrete.scenario_id}
                manifests = [make_manifest(concrete, name, seed=seed, identity=identity) for name in names]
                if condition_overrides:
                    manifests = [ExperimentManifest.model_validate({
                        **manifest.model_dump(mode="json"),
                        "grounded_agency": condition_overrides[manifest.grounded_agency.condition_id].model_dump(mode="json"),
                        "condition": "argmin_efe" if condition_overrides[manifest.grounded_agency.condition_id].action_selection == "engineered_argmin" else "llm_open",
                    }) if manifest.grounded_agency.condition_id in condition_overrides else manifest for manifest in manifests]
                metadata["condition_manifest_groups"].append([manifest.content_hash() for manifest in manifests])
                for manifest in manifests:
                    sha = manifest.content_hash()
                    metadata["manifests"][sha] = f"manifests/{sha}.json"
                    write_json(root / metadata["manifests"][sha], manifest)
                    for repetition in range(repetitions):
                        trial_id = f"{concrete.scenario_id}-{manifest.grounded_agency.condition_id}-l{level_index}-r{repetition}"
                        trial = await run_trial(run_dir=root, scenario=concrete, manifest=manifest, trial_id=trial_id,
                            repetition=repetition, seed=seed + repetition, model_factory=model_factory,
                            provider_factory=provider_factory, timeout_seconds=timeout_seconds,
                            sensor_ratio_override=sensor_ratio_override, semantic_prompt=semantic_prompt, intervention_id=intervention_id,
                            analysis_plan_hash=analysis_plan_hash, frozen_source_snapshot_hash=frozen_source_snapshot_hash,
                            frozen_belief_snapshot=frozen_belief_snapshot, include_beliefs=include_beliefs)
                        trials.append(trial)
                        journal.write(canonical_json(trial) + "\n")
                        journal.flush()
                        metadata["trial_count"] = len(trials)
                        write_json(root / "metadata.json", metadata)
    metadata["completed_at"] = datetime.now(UTC).isoformat()
    metadata["services_closed"] = all(trial.integrity.checks.get("services_closed") for trial in trials)
    write_json(root / "metadata.json", metadata)
    report = validate_run(root, write_report=True)
    return RunResult(run_dir=str(root), trials=tuple(trials), integrity=report)
