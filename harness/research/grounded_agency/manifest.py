"""Frozen GA-01 declarations; execution routes are implemented in later stages."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from harness.research.grounded_agency.neutrality import (
    NEUTRALITY_RULES_DIGEST,
    NEUTRALITY_RULES_VERSION,
)

if TYPE_CHECKING:
    from harness.research.manifest import ExperimentManifest

NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Commit = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")


class SensorTransformSpec(FrozenContract):
    transform_id: NonBlank
    seed: int = Field(ge=0, strict=True)
    channel_alias: NonBlank
    encoding: Literal["continuous", "ordinal_symbols", "categorical_symbols"]
    direction: Literal["direct", "inverted"]
    noise_profile: NonBlank
    delay_profile: NonBlank


class ArtifactIdentity(FrozenContract):
    neuralcloud_commit: Commit
    mistyai_commit: Commit | None = None
    model_id: NonBlank
    model_digest: Digest | None = None
    tokenizer_digest: Digest | None = None
    prompt_digest: Digest
    persona_digest: Digest
    environment_digest: Digest | None = None
    evaluator_digest: Digest | None = None


class GroundedAgencyEvaluation(FrozenContract):
    """Output requirements for a future evaluator, without implementing a runner."""

    primary_result: Literal["natural_language", "structured_decision"]
    decision_schema_ref: NonBlank | None = None
    requires_text_reply: StrictBool

    @model_validator(mode="after")
    def _structured_schema(self) -> GroundedAgencyEvaluation:
        if self.primary_result == "structured_decision" and self.decision_schema_ref is None:
            raise ValueError("structured_decision requires decision_schema_ref")
        return self


class GroundedAgencyRuntimeOptions(FrozenContract):
    """Explicit execution limits; absent in the original GA-01 declarations."""

    permitted_intents: tuple[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]+\.v[0-9]+$")], ...] = ()
    max_decision_repairs: int = Field(default=1, ge=0, le=2, strict=True)
    history_limit: int = Field(default=6, ge=1, le=8, strict=True)
    opaque_token_budget: int = Field(default=256, ge=128, le=1024, strict=True)
    sensor_bins: int = Field(default=9, ge=5, le=9, strict=True)

    @model_validator(mode="after")
    def _unique_intents(self) -> GroundedAgencyRuntimeOptions:
        if len(set(self.permitted_intents)) != len(self.permitted_intents):
            raise ValueError("permitted_intents must be unique")
        return self


class GroundedAgencyCondition(FrozenContract):
    protocol_version: Literal["grounded-agency-v1"]
    condition_id: NonBlank
    interoception_treatment: Literal[
        "none", "opaque_current", "opaque_history", "semantic_summary", "engineered_policy"
    ]
    memory_treatment: Literal["none", "objective_events", "body_causal_beliefs"]
    action_selection: Literal["legacy_reply", "model_structured", "engineered_argmin"]
    report_channel: Literal["normal", "action_only", "disabled"]
    semantic_homeostasis: StrictBool
    homeostatic_policy_ranking: StrictBool
    derived_emotion_visible: StrictBool
    observer_social_presence: Literal["present", "absent"]
    sensor_transform: SensorTransformSpec | None = None
    artifact_identity: ArtifactIdentity
    runtime_auto_select_homeostasis: StrictBool = False
    evaluation: GroundedAgencyEvaluation | None = None
    neutrality_rules_version: NonBlank = NEUTRALITY_RULES_VERSION
    neutrality_rules_digest: Digest = NEUTRALITY_RULES_DIGEST
    runtime: GroundedAgencyRuntimeOptions | None = None

    @property
    def is_opaque(self) -> bool:
        return self.interoception_treatment in {"opaque_current", "opaque_history"}

    @model_validator(mode="after")
    def _validate_treatments(self) -> GroundedAgencyCondition:
        if self.is_opaque:
            if self.sensor_transform is None:
                raise ValueError("opaque interoception requires sensor_transform")
            forbidden = {
                "semantic_homeostasis": self.semantic_homeostasis,
                "homeostatic_policy_ranking": self.homeostatic_policy_ranking,
                "derived_emotion_visible": self.derived_emotion_visible,
            }
            enabled = [name for name, value in forbidden.items() if value]
            if enabled:
                raise ValueError(f"opaque interoception forbids: {', '.join(enabled)}")
        if self.action_selection == "model_structured":
            if self.interoception_treatment == "engineered_policy":
                raise ValueError("model_structured forbids engineered_policy")
            if self.homeostatic_policy_ranking or self.runtime_auto_select_homeostasis:
                raise ValueError("model_structured forbids ranking and Runtime auto selection")
            if self.evaluation is None or self.evaluation.decision_schema_ref is None:
                raise ValueError("model_structured requires evaluation decision_schema_ref")
        if (self.interoception_treatment == "engineered_policy") != (
            self.action_selection == "engineered_argmin"
        ):
            raise ValueError("engineered_policy requires engineered_argmin and vice versa")
        if self.action_selection == "engineered_argmin" and not self.homeostatic_policy_ranking:
            raise ValueError("engineered_argmin requires homeostatic_policy_ranking")
        if self.report_channel in {"action_only", "disabled"}:
            if self.evaluation is None:
                raise ValueError("action_only/disabled requires an evaluation contract")
            if self.evaluation.primary_result != "structured_decision":
                raise ValueError("action_only/disabled requires structured_decision as primary_result")
            if self.evaluation.requires_text_reply:
                raise ValueError("action_only/disabled forbids requires_text_reply")
        return self


class GroundedAgencyBinding(FrozenContract):
    """A run's immutable identity anchor, checked again before accepting evidence."""

    experiment_id: NonBlank
    condition_id: NonBlank
    revision: int = Field(ge=1)
    manifest_hash: Digest

    def assert_matches(self, manifest: ExperimentManifest) -> None:
        condition = manifest.grounded_agency
        if (
            condition is None
            or manifest.experiment_id != self.experiment_id
            or condition.condition_id != self.condition_id
            or manifest.revision != self.revision
            or manifest.content_hash() != self.manifest_hash
        ):
            raise ValueError("grounded agency condition drift: bind a new manifest revision for a new run")
