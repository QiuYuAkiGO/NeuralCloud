"""Transport-neutral domain service ports and in-process reference adapters."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from harness.contracts.decisions import (
    ActionOutcomeEvidence,
    AutonomousAssessment,
    CommitEvidence,
    DeferDecision,
    DecisionEvidence,
    DeliveryEvidence,
    GoalProposal,
    NoActionDecision,
    PreparedResponse,
    ReplyDecision,
)
from harness.cognitive.autonomous import (
    assessment_id_for,
    load_assessment_record,
    run_or_load_assessment,
)
from harness.cognitive.action_planner import ActionDecisionPlanner
from harness.contracts.envelope import (
    CONTRACT_SCHEMA_VERSION,
    DomainCommandEnvelope,
    DomainCommandResult,
    VersionVector,
)
from harness.core.inference import ContextSegment, FinalResponse, InteroceptiveState, SessionTurn
from harness.core.models import InteractionEvent
from harness.homeostasis.models import HomeostaticInteroceptiveSummary
from harness.pipeline.pipeline import InferencePipeline


class DomainSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CONTRACT_SCHEMA_VERSION
    service: str
    revision: int = Field(ge=0)
    acknowledged_receive_seq: int = Field(default=0, ge=0)
    acknowledged_event_id: str | None = None
    component_revisions: dict[str, int] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class ServiceHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    healthy: bool = True
    schema_version: str = CONTRACT_SCHEMA_VERSION
    revision: int = Field(default=0, ge=0)
    acknowledged_receive_seq: int = Field(default=0, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)


class CognitiveServiceClient(Protocol):
    async def health(self) -> ServiceHealth: ...

    async def get_snapshot(self) -> DomainSnapshot: ...

    async def prepare_response(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[PreparedResponse]: ...

    async def assess_autonomous(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[AutonomousAssessment]: ...

    async def commit_interaction(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def pause_maintenance(self, *, timeout: float) -> bool: ...

    async def resume_maintenance(self) -> None: ...

    async def ui_snapshot(self) -> dict[str, Any]: ...

    async def control(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class HomeostasisServiceClient(Protocol):
    async def health(self) -> ServiceHealth: ...

    async def get_snapshot(self) -> DomainSnapshot: ...

    async def apply_event(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def apply_heartbeat(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def apply_outcome(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def assimilate_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def apply_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def propose_policies(self, *, version_vector: VersionVector) -> list[dict[str, Any]]: ...

    async def close(self) -> None: ...


class _PreparedEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: InteractionEvent
    final: FinalResponse
    prepared: PreparedResponse
    autonomous_assessment_id: str | None = None
    autonomous_record_ref: str | None = None
    autonomous_memory_ref: str | None = None
    research: bool = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validated_homeostatic_interoception(
    command: DomainCommandEnvelope[dict[str, Any]],
) -> HomeostaticInteroceptiveSummary | None:
    profile_marker = command.payload.get("homeostatic_profile_ref")
    require_summary = False
    if profile_marker is not None:
        if not isinstance(profile_marker, str) or not profile_marker.strip():
            raise ValueError("homeostatic_profile_ref must be a non-empty string")
        _, separator, revision_text = profile_marker.rpartition("@")
        if not separator:
            raise ValueError("homeostatic_profile_ref must end with @<revision>")
        try:
            profile_revision = int(revision_text)
        except ValueError as exc:
            raise ValueError(
                "homeostatic_profile_ref must end with an integer revision"
            ) from exc
        if profile_revision < 1:
            raise ValueError("homeostatic_profile_ref revision must be positive")
        require_summary = profile_revision >= 3

    raw = command.payload.get("homeostatic_interoception")
    if raw is None:
        if require_summary:
            raise ValueError(
                "homeostatic interoception is required for profile revision 3 or later"
            )
        return None
    summary = HomeostaticInteroceptiveSummary.model_validate(raw)
    if profile_marker is not None and summary.profile_ref != profile_marker:
        raise ValueError(
            "homeostatic interoception profile mismatch: "
            f"summary={summary.profile_ref}, marker={profile_marker}"
        )
    if command.version_vector is None:
        raise ValueError("homeostatic interoception requires a version vector")
    expected_revision = command.version_vector.homeostasis_state
    if summary.snapshot_revision != expected_revision:
        raise ValueError(
            "homeostatic interoception revision mismatch: "
            f"summary={summary.snapshot_revision}, version_vector={expected_revision}"
        )
    if summary.salience == "critical":
        raise ValueError("critical homeostatic state must pause before cognitive invocation")
    return summary


class PipelineCognitiveAdapter:
    """Idempotent PrepareResponse/CommitInteraction boundary for M2 Pipeline.

    The optional state file persists the command ledger, prepared results,
    revisions, acknowledged cursor and Session projection. Memory durability is
    still owned by the configured ``MemoryService`` backend.
    """

    def __init__(
        self,
        pipeline: InferencePipeline,
        *,
        character_id: str = "character",
        state_path: Path | str | None = None,
        autonomous_log_dir: Path | str | None = None,
        default_session_id: str = "sess_default",
        action_planner: ActionDecisionPlanner | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.character_id = character_id
        self.state_path = Path(state_path) if state_path is not None else None
        if autonomous_log_dir is not None:
            self.autonomous_log_dir = Path(autonomous_log_dir)
        elif self.state_path is not None:
            self.autonomous_log_dir = self.state_path.parent / "autonomous"
        else:
            self.autonomous_log_dir = Path("data/cognitive/autonomous")
        self.default_session_id = default_session_id
        self.action_planner = action_planner or ActionDecisionPlanner()
        self._lock = asyncio.Lock()
        self._revision = 0
        self._cursor = 0
        self._event_id: str | None = None
        self._component_revisions = {
            "world": 0,
            "relationship": 0,
            "memory": 0,
            "session": 0,
            "maintenance": 0,
        }
        self._ledger: dict[tuple[str, str], DomainCommandResult[Any]] = {}
        self._request_hashes: dict[tuple[str, str], str] = {}
        self._prepared: dict[str, _PreparedEntry] = {}
        self._research_manifest = None
        self._research_manifest_hash: str | None = None
        self.research_traces: dict[str, dict[str, Any]] = {}
        self._maintenance_paused = False
        self._load_state()

    async def health(self) -> ServiceHealth:
        async with self._lock:
            return ServiceHealth(
                service="cognitive",
                revision=self._revision,
                acknowledged_receive_seq=self._cursor,
                detail={"maintenance_paused": self._maintenance_paused},
            )

    async def get_snapshot(self) -> DomainSnapshot:
        async with self._lock:
            return DomainSnapshot(
                service="cognitive",
                revision=self._revision,
                acknowledged_receive_seq=self._cursor,
                acknowledged_event_id=self._event_id,
                component_revisions=dict(self._component_revisions),
                payload={"prepared_count": len(self._prepared)},
            )

    async def prepare_response(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[PreparedResponse]:
        async with self._lock:
            replay = self._lookup_replay(command)
            if replay is not None:
                self._persist_state()
                return DomainCommandResult[PreparedResponse].model_validate(replay.model_dump(mode="json"))
            rejected = self._preflight(command)
            if rejected is not None:
                return DomainCommandResult[PreparedResponse].model_validate(rejected.model_dump(mode="json"))

            payload = command.payload
            if payload.get("mode") == "grounded_agency_decision":
                return await self._prepare_grounded(command)
            observer = getattr(self.pipeline, "observer", None)
            bind = getattr(observer, "bind", None)
            if callable(bind):
                bind(str(payload.get("call_id") or command.correlation_id))
            mode = str(payload.get("mode") or "")
            source_time = payload.get("source_time") or command.issued_at
            autonomous = mode == "autonomous"
            reconsideration = mode == "reconsideration"
            internal = autonomous or reconsideration
            session_id = str(payload.get("session_id") or self.default_session_id)
            try:
                homeostatic_interoception = _validated_homeostatic_interoception(command)
            except Exception as exc:
                result = self._failure(command, f"invalid homeostatic interoception: {exc}")
                self._remember(command, result)
                self._persist_state()
                return DomainCommandResult[PreparedResponse].model_validate(
                    result.model_dump(mode="json")
                )
            assessment = None
            if autonomous:
                try:
                    assessment = self._load_speak_assessment(payload)
                except Exception as exc:
                    result = self._failure(command, f"invalid autonomous assessment: {exc}")
                    self._remember(command, result)
                    self._persist_state()
                    return DomainCommandResult[PreparedResponse].model_validate(
                        result.model_dump(mode="json")
                    )
            event = InteractionEvent(
                event_id=command.event_id,
                timestamp=source_time,
                channel=str(payload.get("channel") or "chat"),
                speaker=str(payload.get("speaker") or ("runtime" if internal else "user")),
                modality="text",
                raw_text=str(payload.get("text") or ("（主动表达准备）" if internal else "")),
                session_id=session_id,
            )
            interoception_payload = payload.get("interoception")
            interoception: InteroceptiveState | None = None
            context_segments: list[ContextSegment] = []
            if isinstance(interoception_payload, dict):
                if "resource_label" in interoception_payload:
                    cognitive_payload = dict(interoception_payload)
                    source_revision = cognitive_payload.pop("source_revision", None)
                    if "evidence_refs" not in cognitive_payload:
                        if not isinstance(source_revision, int) or source_revision < 0:
                            raise ValueError(
                                "redacted interoception requires a non-negative source_revision"
                            )
                        cognitive_payload["evidence_refs"] = [
                            f"body-state:{source_revision}"
                        ]
                    interoception = InteroceptiveState.model_validate(cognitive_payload)
                elif "resource_level" in interoception_payload:
                    level = float(interoception_payload["resource_level"])
                    trend = float(interoception_payload["resource_trend"])
                    if level <= 0.1:
                        semantic_level = "critical"
                    elif level < 0.3:
                        semantic_level = "low"
                    elif level < 0.7:
                        semantic_level = "nominal"
                    elif level < 0.98:
                        semantic_level = "high"
                    else:
                        semantic_level = "full"
                    source_revision = int(interoception_payload["source_revision"])
                    interoception = InteroceptiveState(
                        resource_label="internal_resource",
                        level=semantic_level,
                        trend=max(-1.0, min(1.0, trend)),
                        time_to_action_limit_seconds=interoception_payload.get(
                            "estimated_time_to_limit_seconds"
                        ),
                        confidence=float(interoception_payload.get("confidence", 1.0)),
                        captured_at=source_time,
                        evidence_refs=[f"body-state:{source_revision}"],
                    )
            try:
                if autonomous and assessment is not None:
                    final = await asyncio.to_thread(
                        self.pipeline.prepare_autonomous,
                        event_id=command.event_id,
                        assessment_id=assessment.assessment_id,
                        assessment_text=assessment.body,
                        record_ref=assessment.record_ref,
                        homeostasis=dict(payload.get("homeostasis") or {}),
                        policies=[dict(item) for item in (payload.get("policies") or [])],
                        session_id=session_id,
                        channel=str(payload.get("channel") or "chat"),
                        now=command.issued_at,
                        call_id=payload.get("call_id"),
                        interoception=interoception,
                        homeostatic_interoception=homeostatic_interoception,
                    )
                else:
                    final = await asyncio.to_thread(
                        self.pipeline.prepare,
                        event,
                        now=command.issued_at,
                        call_id=payload.get("call_id"),
                        context_segments=context_segments,
                        interoception=interoception,
                        homeostatic_interoception=homeostatic_interoception,
                    )
                now = _utc_now()
                read_versions = command.version_vector or self._version_vector()
                decision: ReplyDecision | GoalProposal | NoActionDecision | DeferDecision
                if reconsideration:
                    decision = NoActionDecision(
                        reason="continue current goal after interoceptive reconsideration"
                    )
                else:
                    planned = self.action_planner.decide(payload, mode=mode)
                    if planned is not None:
                        decision = planned
                    elif final.text.strip():
                        decision = ReplyDecision(
                            text=final.text,
                            segments=list(final.segments) or [final.text],
                            channel=str(payload.get("channel") or "chat"),
                        )
                    else:
                        decision = NoActionDecision(
                            reason="cognitive pipeline produced no reply"
                        )
                prepared = PreparedResponse(
                    event_id=command.event_id,
                    correlation_id=command.correlation_id,
                    session_id=event.session_id,
                    decision=decision,
                    read_versions=read_versions,
                    prepared_at=now,
                    expires_at=command.deadline,
                    model_version=self.pipeline.settings.model_version,
                    prompt_version=self.pipeline.settings.prompt_version,
                )
            except Exception as exc:
                result = self._failure(command, str(exc))
                self._remember(command, result)
                self._persist_state()
                return DomainCommandResult[PreparedResponse].model_validate(result.model_dump(mode="json"))

            self._prepared[prepared.prepare_id] = _PreparedEntry(
                event=event,
                final=final,
                prepared=prepared,
                autonomous_assessment_id=assessment.assessment_id if assessment else None,
                autonomous_record_ref=assessment.record_ref if assessment else None,
                autonomous_memory_ref=(
                    (str(payload.get("internal_memory_ref") or "") or None)
                    if assessment
                    else None
                ),
            )
            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            result = DomainCommandResult[PreparedResponse](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=command.effective_request_hash(),
                committed_revision=self._revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=command.receive_seq,
                result=prepared,
                completed_at=_utc_now(),
            )
            self._remember(command, result)
            self._persist_state()
            return result

    def bind_research_manifest(self, manifest) -> None:
        if manifest.grounded_agency is None:
            raise ValueError("a grounded agency manifest is required")
        digest = manifest.content_hash()
        if self._research_manifest_hash is not None and digest != self._research_manifest_hash:
            raise ValueError("cognitive research manifest drift: create an isolated state directory")
        self._research_manifest = manifest
        self._research_manifest_hash = digest

    async def _prepare_grounded(self, command):
        from harness.cognitive.grounded_agency import GroundedAgencyDecisionModel
        from harness.core.inference import SituationActor, SituationState, SituationTrigger
        from harness.core.models import CharacterSnapshot
        from harness.research.grounded_agency.hashes import content_hash
        from harness.research.grounded_agency.manifest import GroundedAgencyRuntimeOptions
        from harness.research.interoception.contracts import OpaqueInteroceptiveObservation
        from harness.runtime.experimental_decision import ExperimentalDecisionCompiler

        try:
            manifest = self._research_manifest
            if manifest is None or command.payload.get("grounded_manifest_hash") != manifest.content_hash():
                raise ValueError("research command does not match the bound manifest")
            ga = manifest.grounded_agency
            options = ga.runtime or GroundedAgencyRuntimeOptions()
            payload = command.payload
            observations = tuple(OpaqueInteroceptiveObservation.model_validate(item) for item in payload.get("opaque_interoception", []))
            if observations and ga.sensor_transform is None:
                raise ValueError("opaque observations supplied to a non-opaque condition")
            additional_segments: list[ContextSegment] = []
            if ga.semantic_homeostasis and payload.get("interoception") is not None:
                from harness.compiler.compiler import _interoception_text

                summary = {**payload["interoception"], "evidence_refs": ["semantic-comparator"]}
                additional_segments.append(ContextSegment(kind="semantic_comparator", text=_interoception_text(summary)))
            situation = SituationState(
                trigger=SituationTrigger(ref=command.event_id), utterance=str(payload.get("text") or ""),
                actor=SituationActor(), snapshot=CharacterSnapshot(persona=self.pipeline.memory.get_persona()),
                world=self.pipeline.memory.stores.world.latest(),
            )
            context = self.pipeline.compiler.compile(
                situation, opaque_interoception=observations, opaque_token_budget=options.opaque_token_budget,
                additional_segments=additional_segments,
                required_user_context=True,
            )
            messages = context.render_messages()
            self.pipeline.compiler.validate_final_messages(messages, manifest=manifest)
            usage = None
            text = ""
            if ga.action_selection == "model_structured":
                result = await asyncio.to_thread(
                    GroundedAgencyDecisionModel(self.pipeline.llm, max_repairs=options.max_decision_repairs).decide,
                    messages, manifest_intents=options.permitted_intents,
                    provider_intents=payload.get("provider_intents", ()), opaque=ga.is_opaque,
                    rules_version=ga.neutrality_rules_version, rules_digest=ga.neutrality_rules_digest,
                )
                decision = ExperimentalDecisionCompiler().compile(
                    result.decision, manifest_intents=options.permitted_intents,
                    provider_intents=payload.get("provider_intents", ()), decision_id=command.event_id,
                )
                usage = result.usage
                trace = result.model_dump(mode="json")
            elif ga.action_selection == "engineered_argmin":
                policies = payload.get("policies") or []
                if policies and all("experimental_decision" in item for item in policies):
                    from harness.cognitive.decision_parser import ExperimentalDecisionParser

                    selected = min(policies, key=lambda item: (item["rank"], str(item.get("policy_id", ""))))
                    parsed = ExperimentalDecisionParser(options.permitted_intents).parse(selected["experimental_decision"])
                    decision = ExperimentalDecisionCompiler().compile(
                        parsed, manifest_intents=options.permitted_intents,
                        provider_intents=payload.get("provider_intents", ()), decision_id=command.event_id,
                    )
                    trace = {"decision": parsed.model_dump(mode="json"), "messages": messages,
                             "input_context_hash": content_hash(messages), "selected_policy_id": selected.get("policy_id")}
                else:
                    decision = self.action_planner.decide(payload, mode="autonomous") or NoActionDecision(reason="no engineered candidate")
                    trace = {"decision": decision.model_dump(mode="json"), "messages": messages, "input_context_hash": content_hash(messages)}
            else:
                response = await asyncio.to_thread(self.pipeline.character._complete, messages, stream=False)
                text = response.text
                usage = response.usage
                if ga.report_channel == "normal" and text.strip():
                    decision = ReplyDecision(text=text, segments=[text], channel=str(payload.get("channel") or "chat"))
                else:
                    decision = NoActionDecision(reason="research report channel has no delivery")
                trace = {"decision": decision.model_dump(mode="json"), "messages": messages,
                         "input_context_hash": content_hash(messages), "raw_output_hash": content_hash(text)}
            event = InteractionEvent(
                event_id=command.event_id, timestamp=payload.get("source_time") or command.issued_at,
                raw_text=str(payload.get("text") or ""), session_id=str(payload.get("session_id") or "grounded-agency"),
                channel=str(payload.get("channel") or "runtime"), speaker="user",
            )
            prepared = PreparedResponse(
                prepare_id="prepare-ga-" + content_hash([command.event_id, command.effective_request_hash()])[:24],
                event_id=command.event_id, correlation_id=command.correlation_id, session_id=event.session_id,
                decision=decision, read_versions=command.version_vector or self._version_vector(),
                prepared_at=_utc_now(), expires_at=command.deadline,
                model_version=manifest.model_version, prompt_version=manifest.prompt_version,
            )
            final = FinalResponse(
                text=text, path="fast", situation_id=situation.situation_id, context_id=context.context_id,
                event_id=event.event_id, **({"usage": usage} if usage is not None else {}),
            )
            self._prepared[prepared.prepare_id] = _PreparedEntry(event=event, final=final, prepared=prepared, research=True)
            trace.update(
                manifest_hash=manifest.content_hash(), artifact_identity=ga.artifact_identity.model_dump(mode="json"),
                observation_hashes=[content_hash(item) for item in observations], prepare_id=prepared.prepare_id,
                compiled_decision=decision.model_dump(mode="json"),
            )
            self.research_traces[command.event_id] = trace
            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            output = DomainCommandResult[PreparedResponse](
                command_id=command.command_id, command_type=command.command_type, status="applied",
                request_hash=command.effective_request_hash(), committed_revision=self._revision,
                acknowledged_event_id=command.event_id, acknowledged_receive_seq=self._cursor,
                result=prepared, completed_at=_utc_now(),
            )
        except Exception as exc:
            output = self._failure(command, str(exc))
        self._remember(command, output)
        self._persist_state()
        return output

    async def assess_autonomous(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[AutonomousAssessment]:
        async with self._lock:
            replay = self._lookup_replay(command)
            if replay is not None:
                self._persist_state()
                return DomainCommandResult[AutonomousAssessment].model_validate(
                    replay.model_dump(mode="json")
                )
            rejected = self._preflight(command)
            if rejected is not None:
                return DomainCommandResult[AutonomousAssessment].model_validate(
                    rejected.model_dump(mode="json")
                )

            payload = command.payload
            session_id = str(payload.get("session_id") or self.default_session_id)
            channel = str(payload.get("channel") or "chat")
            assessment_id = assessment_id_for(command.idempotency_key)
            try:
                homeostatic_interoception = _validated_homeostatic_interoception(command)
                run = await asyncio.to_thread(
                    run_or_load_assessment,
                    pipeline=self.pipeline,
                    log_dir=self.autonomous_log_dir,
                    assessment_id=assessment_id,
                    evaluated_at=command.issued_at,
                    session_id=session_id,
                    channel=channel,
                    homeostasis=dict(payload.get("homeostasis") or {}),
                    policies=[dict(item) for item in (payload.get("policies") or [])],
                    homeostatic_interoception=homeostatic_interoception,
                )
                memory_ref: str | None = None
                if run.decision == "speak":
                    memory_ref = f"reflection_{assessment_id.removeprefix('assessment_')}"
                    if self.pipeline.memory.stores.episodic.get(memory_ref) is None:
                        reflection = InteractionEvent(
                            event_id=memory_ref,
                            timestamp=run.evaluated_at,
                            channel="internal",
                            speaker="character",
                            modality="text",
                            raw_text=run.body,
                            session_id=session_id,
                            evidence=[f"autonomous-text:{run.record_ref}"],
                        )
                        self.pipeline.memory.record_event(
                            reflection,
                            generate_insight=False,
                            curate=False,
                            event_type="internal_reflection",
                            now=run.evaluated_at,
                        )
                        self._revision += 1
                        self._component_revisions["memory"] = self._revision
                assessment = AutonomousAssessment(
                    assessment_id=run.assessment_id,
                    decision=run.decision,
                    record_ref=run.record_ref,
                    evaluator=run.evaluator,
                    internal_memory_ref=memory_ref,
                    session_id=session_id,
                    channel=channel,
                    evaluated_at=run.evaluated_at,
                    model_version=run.model_version,
                    prompt_version=run.prompt_version,
                )
            except Exception as exc:
                result = self._failure(command, str(exc))
                self._remember(command, result)
                self._persist_state()
                return DomainCommandResult[AutonomousAssessment].model_validate(
                    result.model_dump(mode="json")
                )

            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            result = DomainCommandResult[AutonomousAssessment](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=command.effective_request_hash(),
                committed_revision=self._revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=self._cursor,
                result=assessment,
                completed_at=_utc_now(),
            )
            self._remember(command, result)
            self._persist_state()
            return result

    async def commit_interaction(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        async with self._lock:
            replay = self._lookup_replay(command)
            if replay is not None:
                self._persist_state()
                return DomainCommandResult[dict[str, Any]].model_validate(replay.model_dump(mode="json"))
            rejected = self._preflight(command)
            if rejected is not None:
                return DomainCommandResult[dict[str, Any]].model_validate(rejected.model_dump(mode="json"))

            prepare_id = str(command.payload.get("prepare_id") or "")
            entry = self._prepared.get(prepare_id)
            if entry is None:
                result = self._failure(command, f"unknown prepare_id: {prepare_id}", status="rejected")
                self._remember(command, result)
                return result
            try:
                evidence: DeliveryEvidence | ActionOutcomeEvidence | DecisionEvidence = TypeAdapter(
                    CommitEvidence
                ).validate_python(command.payload.get("evidence"))
            except Exception as exc:
                result = self._failure(command, f"invalid commit evidence: {exc}", status="rejected")
                self._remember(command, result)
                return result
            if evidence.prepare_id != prepare_id:
                result = self._failure(command, "evidence prepare_id mismatch", status="rejected")
                self._remember(command, result)
                return result

            self._commit_entry(entry, evidence)
            self._revision += 1
            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            self._component_revisions["session"] = self._revision
            self._component_revisions["memory"] = self._revision
            payload = {
                "prepare_id": prepare_id,
                "evidence_kind": evidence.evidence_kind,
                "revision": self._revision,
            }
            result = DomainCommandResult[dict[str, Any]](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=command.effective_request_hash(),
                committed_revision=self._revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=self._cursor,
                result=payload,
                completed_at=_utc_now(),
            )
            self._remember(command, result)
            self._persist_state()
            return result

    async def pause_maintenance(self, *, timeout: float) -> bool:
        self._maintenance_paused = True
        drain = getattr(self.pipeline.memory, "drain_maintenance", None)
        if not callable(drain):
            return True
        try:
            if inspect.iscoroutinefunction(drain):
                result = await asyncio.wait_for(drain(timeout=timeout), timeout=timeout)
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(drain, timeout=timeout),
                    timeout=timeout,
                )
            return bool(result)
        except Exception:
            return False

    async def resume_maintenance(self) -> None:
        self._maintenance_paused = False
        resume = getattr(self.pipeline.memory, "resume_maintenance", None)
        if callable(resume):
            if inspect.iscoroutinefunction(resume):
                await resume()
            else:
                await asyncio.to_thread(resume)

    async def close(self) -> None:
        self._persist_state()
        close = getattr(self.pipeline.memory, "close", None)
        if callable(close):
            await asyncio.to_thread(close)

    def _commit_entry(
        self,
        entry: _PreparedEntry,
        evidence: DeliveryEvidence | ActionOutcomeEvidence | DecisionEvidence,
    ) -> None:
        if entry.research:
            # Research evidence is maintained separately from legacy relationship enrichment.
            return
        event = entry.event
        session = self.pipeline.session_store
        if entry.autonomous_assessment_id is None:
            if not session.contains_event(event.session_id, event.event_id):
                session.append(
                    event.session_id,
                    "user",
                    event.raw_text,
                    speaker=event.speaker,
                    event_id=event.event_id,
                    timestamp=event.timestamp,
                )
            if self.pipeline.memory.stores.episodic.get(event.event_id) is None:
                # A structured action has already been planned, approved, executed,
                # and independently verified before its outcome reaches this commit.
                # Re-running persona insight and curator LLM calls here adds no
                # decision value, can exceed the Runtime command deadline, and used
                # to leave the supervisor trying to pause while Cognitive was still
                # blocked in synchronous enrichment.  Keep the objective audit event,
                # but reserve enrichment for conversational deliveries.
                enrich = not isinstance(evidence, ActionOutcomeEvidence)
                self.pipeline.memory.record_event(
                    event,
                    generate_insight=enrich,
                    curate=enrich,
                    now=event.timestamp,
                )

        if isinstance(evidence, DeliveryEvidence) and evidence.success:
            for index, segment in enumerate(evidence.delivered_segments, start=1):
                segment_event_id = f"{entry.final.response_id}:s{index}"
                if not session.contains_event(event.session_id, segment_event_id):
                    session.append(
                        event.session_id,
                        "character",
                        segment,
                        speaker=self.pipeline._speaker_label("character"),
                        event_id=segment_event_id,
                        timestamp=evidence.delivered_at,
                    )
            reply_event_id = f"{entry.final.response_id}:delivered"
            reply = InteractionEvent(
                event_id=reply_event_id,
                timestamp=evidence.delivered_at,
                channel=event.channel,
                speaker="character",
                modality=event.modality,
                raw_text="\n\n".join(evidence.delivered_segments),
                session_id=event.session_id,
                evidence=[evidence.external_ref or f"delivery:{evidence.delivery_id}"],
            )
            if self.pipeline.memory.stores.episodic.get(reply_event_id) is None:
                self.pipeline.memory.record_event(
                    reply,
                    generate_insight=False,
                    curate=False,
                    now=evidence.delivered_at,
                )

    def _load_speak_assessment(self, payload: dict[str, Any]):
        assessment_id = str(payload.get("assessment_id") or "")
        record_ref = str(payload.get("record_ref") or "")
        if not assessment_id or not record_ref:
            raise ValueError("assessment_id and record_ref are required")
        root = self.autonomous_log_dir.resolve()
        target = Path(record_ref).resolve()
        if not target.is_relative_to(root):
            raise ValueError("record_ref is outside the autonomous log directory")
        run = load_assessment_record(target)
        if run.assessment_id != assessment_id:
            raise ValueError("assessment_id does not match record")
        if run.decision != "speak":
            raise ValueError("only SPEAK assessments can prepare autonomous output")
        return run

    def _lookup_replay(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[Any] | None:
        key = (command.command_type, command.idempotency_key)
        previous = self._ledger.get(key)
        if previous is None:
            return None
        if self._request_hashes[key] != command.effective_request_hash():
            return self._failure(command, "idempotency key reused with different request", status="conflict")
        return previous.model_copy(
            update={"status": "idempotent_replay", "idempotency_hit": True}
        )

    def _preflight(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[Any] | None:
        if command.is_expired(now=_utc_now()):
            return self._failure(command, "command deadline expired", status="rejected")
        if command.expected_revision is not None and command.expected_revision != self._revision:
            return self._failure(
                command,
                f"expected revision {command.expected_revision}, current {self._revision}",
                status="conflict",
            )
        return None

    def _failure(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
        error: str,
        *,
        status: Literal["failed", "rejected", "conflict"] = "failed",
    ) -> DomainCommandResult[Any]:
        return DomainCommandResult[Any](
            command_id=command.command_id,
            command_type=command.command_type,
            status=status,
            request_hash=command.effective_request_hash(),
            error=error,
            completed_at=_utc_now(),
        )

    def _remember(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
        result: DomainCommandResult[Any],
    ) -> None:
        key = (command.command_type, command.idempotency_key)
        self._ledger[key] = result
        self._request_hashes[key] = command.effective_request_hash()

    def _version_vector(self) -> VersionVector:
        return VersionVector(
            cognitive_world=self._component_revisions["world"],
            cognitive_relationship=self._component_revisions["relationship"],
            cognitive_memory=self._component_revisions["memory"],
            cognitive_session=self._component_revisions["session"],
            cognitive_maintenance=self._component_revisions["maintenance"],
        )

    def _persist_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "revision": self._revision,
            "cursor": self._cursor,
            "event_id": self._event_id,
            "component_revisions": self._component_revisions,
            "request_hashes": {f"{key[0]}\0{key[1]}": value for key, value in self._request_hashes.items()},
            "ledger": {
                f"{key[0]}\0{key[1]}": value.model_dump(mode="json") for key, value in self._ledger.items()
            },
            "prepared": {key: value.model_dump(mode="json") for key, value in self._prepared.items()},
            "research_manifest_hash": self._research_manifest_hash,
            "research_traces": self.research_traces,
            "sessions": {
                key: [turn.model_dump(mode="json") for turn in turns]
                for key, turns in self.pipeline.session_store.snapshot().items()
            },
        }
        temporary = self.state_path.with_name(f".{self.state_path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._revision = int(payload.get("revision", 0))
        self._cursor = int(payload.get("cursor", 0))
        self._event_id = payload.get("event_id")
        self._research_manifest_hash = payload.get("research_manifest_hash")
        self.research_traces = dict(payload.get("research_traces") or {})
        self._component_revisions.update(payload.get("component_revisions") or {})
        for encoded, request_hash in (payload.get("request_hashes") or {}).items():
            self._request_hashes[tuple(encoded.split("\0", 1))] = request_hash
        for encoded, result in (payload.get("ledger") or {}).items():
            self._ledger[tuple(encoded.split("\0", 1))] = DomainCommandResult[Any].model_validate(result)
        self._prepared = {
            key: _PreparedEntry.model_validate(value)
            for key, value in (payload.get("prepared") or {}).items()
        }
        sessions = {
            key: [SessionTurn.model_validate(turn) for turn in turns]
            for key, turns in (payload.get("sessions") or {}).items()
        }
        self.pipeline.session_store.restore(sessions)


class PassiveHomeostasisAdapter:
    """Passive monotonic reference service used until the V2 engine is supplied."""

    def __init__(
        self,
        *,
        policies: list[dict[str, Any]] | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        self._revision = 0
        self._cursor = 0
        self._event_id: str | None = None
        self._policies = list(policies or [])
        self.state_path = Path(state_path) if state_path is not None else None
        self._ledger: dict[tuple[str, str], tuple[str, DomainCommandResult[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()
        self._load_state()

    async def health(self) -> ServiceHealth:
        async with self._lock:
            return ServiceHealth(
                service="homeostasis",
                revision=self._revision,
                acknowledged_receive_seq=self._cursor,
            )

    async def get_snapshot(self) -> DomainSnapshot:
        async with self._lock:
            return DomainSnapshot(
                service="homeostasis",
                revision=self._revision,
                acknowledged_receive_seq=self._cursor,
                acknowledged_event_id=self._event_id,
                component_revisions={"state": self._revision},
                payload={"policies": self._policies},
            )

    async def apply_event(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def apply_heartbeat(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def apply_outcome(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def assimilate_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def apply_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def propose_policies(self, *, version_vector: VersionVector) -> list[dict[str, Any]]:
        return list(self._policies)

    async def close(self) -> None:
        self._persist_state()

    async def _apply(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        async with self._lock:
            key = (command.command_type, command.idempotency_key)
            request_hash = command.effective_request_hash()
            previous = self._ledger.get(key)
            if previous is not None:
                if previous[0] != request_hash:
                    return DomainCommandResult[dict[str, Any]](
                        command_id=command.command_id,
                        command_type=command.command_type,
                        status="conflict",
                        request_hash=request_hash,
                        error="idempotency key reused with different request",
                        completed_at=_utc_now(),
                    )
                replay = previous[1].model_copy(
                    update={"status": "idempotent_replay", "idempotency_hit": True}
                )
                self._persist_state()
                return replay
            if command.is_expired(now=_utc_now()):
                return DomainCommandResult[dict[str, Any]](
                    command_id=command.command_id,
                    command_type=command.command_type,
                    status="rejected",
                    request_hash=request_hash,
                    error="command deadline expired",
                    completed_at=_utc_now(),
                )
            if command.expected_revision is not None and command.expected_revision != self._revision:
                return DomainCommandResult[dict[str, Any]](
                    command_id=command.command_id,
                    command_type=command.command_type,
                    status="conflict",
                    request_hash=request_hash,
                    error=f"expected revision {command.expected_revision}, current {self._revision}",
                    completed_at=_utc_now(),
                )
            self._revision += 1
            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            result = DomainCommandResult[dict[str, Any]](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=request_hash,
                committed_revision=self._revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=self._cursor,
                result={"revision": self._revision, "policies": list(self._policies)},
                completed_at=_utc_now(),
            )
            self._ledger[key] = (request_hash, result)
            self._persist_state()
            return result

    def _persist_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "revision": self._revision,
            "cursor": self._cursor,
            "event_id": self._event_id,
            "policies": self._policies,
            "ledger": {
                f"{key[0]}\0{key[1]}": {
                    "request_hash": request_hash,
                    "result": result.model_dump(mode="json"),
                }
                for key, (request_hash, result) in self._ledger.items()
            },
        }
        temporary = self.state_path.with_name(f".{self.state_path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._revision = int(payload.get("revision", 0))
        self._cursor = int(payload.get("cursor", 0))
        self._event_id = payload.get("event_id")
        if "policies" in payload:
            self._policies = list(payload["policies"])
        for encoded, entry in (payload.get("ledger") or {}).items():
            key = tuple(encoded.split("\0", 1))
            self._ledger[key] = (
                entry["request_hash"],
                DomainCommandResult[dict[str, Any]].model_validate(entry["result"]),
            )
