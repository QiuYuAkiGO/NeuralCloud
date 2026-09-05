from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from harness.research.grounded_agency.manifest import (
    ArtifactIdentity,
    GroundedAgencyCondition,
    GroundedAgencyEvaluation,
    SensorTransformSpec,
)
from harness.research.grounded_agency.neutrality import (
    NEUTRALITY_RULES_DIGEST,
    NEUTRALITY_RULES_VERSION,
)
from harness.research.manifest import ExperimentManifest, load_experiment_manifest


ROOT = Path(__file__).resolve().parents[3]
TREATMENTS = ("none", "opaque_current", "opaque_history", "semantic_summary", "engineered_policy")
MEMORIES = ("none", "objective_events", "body_causal_beliefs")
ACTIONS = ("legacy_reply", "model_structured", "engineered_argmin")
REPORTS = ("normal", "action_only", "disabled")
ENCODINGS = ("continuous", "ordinal_symbols", "categorical_symbols")


def _sensor() -> dict[str, Any]:
    return {
        "transform_id": "sensor-test-v1",
        "seed": 7,
        "channel_alias": "c7",
        "encoding": "ordinal_symbols",
        "direction": "direct",
        "noise_profile": "none",
        "delay_profile": "none",
    }


def _artifact() -> dict[str, Any]:
    return {
        "neuralcloud_commit": "1" * 40,
        "mistyai_commit": "2" * 40,
        "model_id": "mock-test-v1",
        "model_digest": "3" * 64,
        "tokenizer_digest": "4" * 64,
        "prompt_digest": "5" * 64,
        "persona_digest": "6" * 64,
        "environment_digest": "7" * 64,
        "evaluator_digest": "8" * 64,
    }


def _evaluation() -> dict[str, Any]:
    return {
        "primary_result": "structured_decision",
        "decision_schema_ref": "schema://grounded-agency/decision-v1",
        "requires_text_reply": False,
    }


def _condition(treatment: str = "opaque_current") -> dict[str, Any]:
    engineered = treatment == "engineered_policy"
    return {
        "protocol_version": "grounded-agency-v1",
        "condition_id": "test-condition-v1",
        "interoception_treatment": treatment,
        "memory_treatment": "none",
        "action_selection": "engineered_argmin" if engineered else "legacy_reply",
        "report_channel": "normal",
        "semantic_homeostasis": engineered,
        "homeostatic_policy_ranking": engineered,
        "derived_emotion_visible": False,
        "observer_social_presence": "absent",
        "sensor_transform": _sensor() if treatment.startswith("opaque_") else None,
        "artifact_identity": _artifact(),
        "evaluation": _evaluation(),
    }


def _payload(treatment: str = "opaque_current") -> dict[str, Any]:
    payload = json.loads((ROOT / "presets/runtime/development-demo.json").read_text(encoding="utf-8"))
    payload["homeostatic_expression"] = {"mode": "off"}
    payload["grounded_agency"] = _condition(treatment)
    payload["condition"] = "argmin_efe" if treatment == "engineered_policy" else "llm_open"
    return payload


@pytest.mark.parametrize("treatment", TREATMENTS)
@pytest.mark.parametrize("memory", MEMORIES)
def test_treatments_and_memories_round_trip_with_stable_hash(treatment: str, memory: str) -> None:
    payload = _payload(treatment)
    payload["grounded_agency"]["memory_treatment"] = memory

    manifest = ExperimentManifest.model_validate(payload)
    restored = ExperimentManifest.model_validate_json(manifest.model_dump_json())

    assert restored == manifest
    assert restored.content_hash() == manifest.content_hash()
    assert restored.grounded_agency.memory_treatment == memory
    assert len(manifest.content_hash()) == 64


@pytest.mark.parametrize("treatment", ("opaque_current", "opaque_history"))
@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("direction", ("direct", "inverted"))
def test_opaque_sensor_encodings_and_directions(treatment: str, encoding: str, direction: str) -> None:
    payload = _payload(treatment)
    payload["grounded_agency"]["sensor_transform"].update(encoding=encoding, direction=direction)
    manifest = ExperimentManifest.model_validate(payload)

    assert manifest.grounded_agency.sensor_transform.encoding == encoding
    assert manifest.grounded_agency.sensor_transform.direction == direction


@pytest.mark.parametrize("treatment", TREATMENTS)
@pytest.mark.parametrize("action", ACTIONS)
def test_action_ownership_matrix(treatment: str, action: str) -> None:
    payload = _payload(treatment)
    payload["grounded_agency"]["action_selection"] = action
    payload["condition"] = "argmin_efe" if action == "engineered_argmin" else "llm_open"

    if (treatment == "engineered_policy") != (action == "engineered_argmin"):
        with pytest.raises(ValidationError):
            ExperimentManifest.model_validate(payload)
    else:
        assert ExperimentManifest.model_validate(payload).grounded_agency.action_selection == action


@pytest.mark.parametrize("report", REPORTS)
@pytest.mark.parametrize("action", ACTIONS)
def test_report_channels_support_declared_structured_results(report: str, action: str) -> None:
    payload = _payload("engineered_policy" if action == "engineered_argmin" else "none")
    payload["grounded_agency"].update(action_selection=action, report_channel=report)

    manifest = ExperimentManifest.model_validate(payload)

    assert manifest.grounded_agency.evaluation.primary_result == "structured_decision"
    assert manifest.grounded_agency.evaluation.decision_schema_ref
    assert manifest.grounded_agency.evaluation.requires_text_reply is False


@pytest.mark.parametrize("treatment", ("opaque_current", "opaque_history"))
@pytest.mark.parametrize("field", ("semantic_homeostasis", "homeostatic_policy_ranking", "derived_emotion_visible"))
def test_opaque_rejects_semantic_or_engineered_visibility(treatment: str, field: str) -> None:
    payload = _payload(treatment)
    payload["grounded_agency"][field] = True
    with pytest.raises(ValidationError, match=field):
        ExperimentManifest.model_validate(payload)


@pytest.mark.parametrize("treatment", ("opaque_current", "opaque_history"))
@pytest.mark.parametrize("mode", ("shadow", "responses", "full"))
def test_opaque_rejects_parent_semantic_expression(treatment: str, mode: str) -> None:
    payload = _payload(treatment)
    payload["homeostatic_expression"] = {"mode": mode}
    with pytest.raises(ValidationError, match="homeostatic_expression.mode=off"):
        ExperimentManifest.model_validate(payload)


@pytest.mark.parametrize("treatment", ("opaque_current", "opaque_history"))
@pytest.mark.parametrize("missing", (True, False))
def test_opaque_requires_declared_sensor(treatment: str, missing: bool) -> None:
    payload = _payload(treatment)
    if missing:
        del payload["grounded_agency"]["sensor_transform"]
    else:
        payload["grounded_agency"]["sensor_transform"] = None
    with pytest.raises(ValidationError, match="requires sensor_transform"):
        ExperimentManifest.model_validate(payload)


@pytest.mark.parametrize("field", ("homeostatic_policy_ranking", "runtime_auto_select_homeostasis"))
def test_model_owned_action_rejects_external_selectors(field: str) -> None:
    payload = _payload("none")
    payload["grounded_agency"].update(action_selection="model_structured", **{field: True})
    with pytest.raises(ValidationError, match="ranking and Runtime auto selection"):
        ExperimentManifest.model_validate(payload)


def test_model_owned_action_rejects_parent_argmin() -> None:
    payload = _payload("none")
    payload["grounded_agency"]["action_selection"] = "model_structured"
    payload["condition"] = "argmin_efe"
    with pytest.raises(ValidationError, match="model_structured forbids condition=argmin_efe"):
        ExperimentManifest.model_validate(payload)


@pytest.mark.parametrize("condition", ("llm_top3", "llm_open"))
def test_engineered_action_requires_parent_argmin(condition: str) -> None:
    payload = _payload("engineered_policy")
    payload["condition"] = condition
    with pytest.raises(ValidationError, match="engineered_argmin requires condition=argmin_efe"):
        ExperimentManifest.model_validate(payload)


def test_engineered_action_requires_policy_ranking() -> None:
    payload = _payload("engineered_policy")
    payload["grounded_agency"]["homeostatic_policy_ranking"] = False
    with pytest.raises(ValidationError, match="requires homeostatic_policy_ranking"):
        ExperimentManifest.model_validate(payload)


@pytest.mark.parametrize("evaluation", (None, {"primary_result": "natural_language", "requires_text_reply": True}))
def test_model_owned_action_requires_declared_decision_schema(evaluation: dict[str, Any] | None) -> None:
    payload = _payload("none")
    payload["grounded_agency"].update(action_selection="model_structured", evaluation=evaluation)
    with pytest.raises(ValidationError, match="requires evaluation decision_schema_ref"):
        ExperimentManifest.model_validate(payload)


@pytest.mark.parametrize("report", ("action_only", "disabled"))
@pytest.mark.parametrize(
    "evaluation",
    (
        None,
        {"primary_result": "natural_language", "requires_text_reply": False},
        {"primary_result": "structured_decision", "requires_text_reply": False},
        {**_evaluation(), "requires_text_reply": True},
    ),
)
def test_no_text_channels_reject_incompatible_evaluation(report: str, evaluation: dict[str, Any] | None) -> None:
    payload = _payload("none")
    payload["grounded_agency"].update(report_channel=report, evaluation=evaluation)
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(payload)


def test_normal_legacy_reply_may_use_natural_language_or_no_evaluation() -> None:
    payload = _payload("none")
    for evaluation in (None, {"primary_result": "natural_language", "requires_text_reply": True}):
        payload["grounded_agency"]["evaluation"] = evaluation
        manifest = ExperimentManifest.model_validate(payload)
        assert manifest.grounded_agency.evaluation is None or manifest.grounded_agency.evaluation.requires_text_reply


@pytest.mark.parametrize(
    "path",
    (
        (),
        ("grounded_agency",),
        ("grounded_agency", "sensor_transform"),
        ("grounded_agency", "artifact_identity"),
        ("grounded_agency", "evaluation"),
    ),
)
def test_unknown_fields_are_rejected_at_every_contract_level(path: tuple[str, ...]) -> None:
    payload = _payload()
    target = payload
    for part in path:
        target = target[part]
    target["undeclared_treatment"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExperimentManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "field", "replacement"),
    (
        ((), "revision", 99),
        (("grounded_agency",), "condition_id", "different"),
        (("grounded_agency", "sensor_transform"), "seed", 99),
        (("grounded_agency", "artifact_identity"), "prompt_digest", "a" * 64),
        (("grounded_agency", "evaluation"), "requires_text_reply", True),
    ),
)
def test_contracts_are_frozen(path: tuple[str, ...], field: str, replacement: Any) -> None:
    manifest = ExperimentManifest.model_validate(_payload())
    target: Any = manifest
    for part in path:
        target = getattr(target, part)
    with pytest.raises(ValidationError, match="Instance is frozen"):
        setattr(target, field, replacement)


@pytest.mark.parametrize(
    ("model", "payload", "field", "value"),
    (
        (GroundedAgencyCondition, _condition(), "interoception_treatment", "opaque"),
        (GroundedAgencyCondition, _condition(), "memory_treatment", "unrestricted"),
        (GroundedAgencyCondition, _condition(), "action_selection", "automatic"),
        (GroundedAgencyCondition, _condition(), "report_channel", "silent"),
        (GroundedAgencyCondition, _condition(), "observer_social_presence", "unknown"),
        (GroundedAgencyCondition, _condition(), "protocol_version", "grounded-agency-v2"),
        (SensorTransformSpec, _sensor(), "encoding", "semantic"),
        (SensorTransformSpec, _sensor(), "direction", "adaptive"),
        (SensorTransformSpec, _sensor(), "seed", -1),
        (SensorTransformSpec, _sensor(), "seed", True),
        (SensorTransformSpec, _sensor(), "seed", "7"),
        (GroundedAgencyEvaluation, _evaluation(), "primary_result", "score"),
        (GroundedAgencyEvaluation, _evaluation(), "requires_text_reply", "false"),
    ),
)
def test_invalid_treatment_and_sensor_values_are_rejected(model: Any, payload: dict[str, Any], field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, field: value})


@pytest.mark.parametrize(
    "field",
    ("semantic_homeostasis", "homeostatic_policy_ranking", "derived_emotion_visible", "runtime_auto_select_homeostasis"),
)
@pytest.mark.parametrize("value", ("false", 0, None))
def test_boolean_flags_require_explicit_boolean_values(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        GroundedAgencyCondition.model_validate({**_condition("none"), field: value})


@pytest.mark.parametrize(
    ("model", "payload", "field"),
    (
        (GroundedAgencyCondition, _condition(), "condition_id"),
        (GroundedAgencyCondition, _condition(), "neutrality_rules_version"),
        (SensorTransformSpec, _sensor(), "transform_id"),
        (SensorTransformSpec, _sensor(), "channel_alias"),
        (SensorTransformSpec, _sensor(), "noise_profile"),
        (SensorTransformSpec, _sensor(), "delay_profile"),
        (ArtifactIdentity, _artifact(), "model_id"),
        (GroundedAgencyEvaluation, _evaluation(), "decision_schema_ref"),
    ),
)
@pytest.mark.parametrize("value", ("", " \t\n"))
def test_contract_identifiers_cannot_be_blank(model: Any, payload: dict[str, Any], field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, field: value})


@pytest.mark.parametrize("field", ("model_id", "prompt_digest", "persona_digest", "neuralcloud_commit"))
def test_artifact_identity_requires_recorded_model_prompt_persona_and_code(field: str) -> None:
    artifact = _artifact()
    del artifact[field]
    with pytest.raises(ValidationError, match="Field required"):
        ArtifactIdentity.model_validate(artifact)


@pytest.mark.parametrize(
    "field",
    ("model_digest", "tokenizer_digest", "prompt_digest", "persona_digest", "environment_digest", "evaluator_digest"),
)
@pytest.mark.parametrize("value", ("abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65))
def test_artifact_digests_require_canonical_sha256(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactIdentity.model_validate({**_artifact(), field: value})


@pytest.mark.parametrize("field", ("neuralcloud_commit", "mistyai_commit"))
@pytest.mark.parametrize("value", ("HEAD", "abc1234", "A" * 40, "x" * 40, "a" * 41))
def test_artifact_commits_require_full_lowercase_hashes(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactIdentity.model_validate({**_artifact(), field: value})


@pytest.mark.parametrize("length", (40, 64))
def test_artifact_accepts_full_sha1_and_sha256_commits(length: int) -> None:
    artifact = ArtifactIdentity.model_validate(
        {**_artifact(), "neuralcloud_commit": "a" * length, "mistyai_commit": "b" * length}
    )
    assert len(artifact.neuralcloud_commit) == length


def test_optional_artifact_metadata_may_be_absent() -> None:
    artifact = ArtifactIdentity.model_validate(
        {name: _artifact()[name] for name in ("neuralcloud_commit", "model_id", "prompt_digest", "persona_digest")}
    )
    assert artifact.mistyai_commit is None
    assert artifact.model_digest is None
    assert artifact.tokenizer_digest is None
    assert artifact.environment_digest is None
    assert artifact.evaluator_digest is None


@pytest.mark.parametrize("field", tuple(_artifact()))
def test_every_artifact_identity_change_changes_manifest_hash(field: str) -> None:
    payload = _payload()
    original = ExperimentManifest.model_validate(payload)
    payload["grounded_agency"]["artifact_identity"][field] = (
        "different-model" if field == "model_id" else "a" * (40 if field.endswith("commit") else 64)
    )
    changed = ExperimentManifest.model_validate(payload)

    assert changed.content_hash() != original.content_hash()


def _reverse_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _reverse_keys(item) for key, item in reversed(tuple(value.items()))}
    if isinstance(value, list):
        return [_reverse_keys(item) for item in value]
    return value


def test_nested_key_order_does_not_change_canonical_identity() -> None:
    payload = _payload()
    original = ExperimentManifest.model_validate(payload)
    reordered = ExperimentManifest.model_validate(_reverse_keys(payload))
    assert reordered.canonical_json() == original.canonical_json()
    assert reordered.content_hash() == hashlib.sha256(original.canonical_json().encode("utf-8")).hexdigest()


def test_neutrality_rules_are_recorded_frozen_and_hash_significant() -> None:
    manifest = ExperimentManifest.model_validate(_payload())
    ga = manifest.grounded_agency
    assert ga.neutrality_rules_version == NEUTRALITY_RULES_VERSION
    assert ga.neutrality_rules_digest == NEUTRALITY_RULES_DIGEST
    for field, value in (("neutrality_rules_version", "historical-rules"), ("neutrality_rules_digest", "e" * 64)):
        with pytest.raises(ValidationError, match="Instance is frozen"):
            setattr(ga, field, value)
        payload = manifest.model_dump(mode="json")
        payload["grounded_agency"][field] = value
        assert ExperimentManifest.model_validate(payload).content_hash() != manifest.content_hash()


def test_binding_detects_treatment_drift_under_identical_condition_and_revision() -> None:
    original = ExperimentManifest.model_validate(_payload())
    binding = original.bind_condition()
    binding.assert_matches(ExperimentManifest.model_validate_json(original.model_dump_json()))
    assert binding.condition_id == original.grounded_agency.condition_id
    assert binding.manifest_hash == original.content_hash()

    payload = original.model_dump(mode="json")
    payload["grounded_agency"]["memory_treatment"] = "objective_events"
    drifted = ExperimentManifest.model_validate(payload)
    with pytest.raises(ValueError, match="condition drift"):
        binding.assert_matches(drifted)
    with pytest.raises(ValidationError, match="Instance is frozen"):
        binding.manifest_hash = drifted.content_hash()


@pytest.mark.parametrize("change", ("experiment_id", "condition_id", "revision", "remove_condition", "prompt_digest"))
def test_binding_rejects_changed_run_identity(change: str) -> None:
    original = ExperimentManifest.model_validate(_payload())
    payload = original.model_dump(mode="json")
    if change == "experiment_id":
        payload["experiment_id"] = "different-experiment"
    elif change == "condition_id":
        payload["grounded_agency"]["condition_id"] = "different-condition"
    elif change == "revision":
        payload["revision"] += 1
    elif change == "remove_condition":
        payload["grounded_agency"] = None
    else:
        payload["grounded_agency"]["artifact_identity"]["prompt_digest"] = "f" * 64
    with pytest.raises(ValueError, match="condition drift"):
        original.bind_condition().assert_matches(ExperimentManifest.model_validate(payload))


def test_with_revision_validates_new_treatment_and_requires_a_fresh_binding() -> None:
    original = ExperimentManifest.model_validate(_payload())
    changed_condition = original.grounded_agency.model_dump(mode="json")
    changed_condition["memory_treatment"] = "body_causal_beliefs"

    revised = original.with_revision(grounded_agency=changed_condition)

    assert revised.revision == original.revision + 1
    assert revised.experiment_id == original.experiment_id
    assert original.grounded_agency.memory_treatment == "none"
    assert revised.grounded_agency.memory_treatment == "body_causal_beliefs"
    with pytest.raises(ValueError, match="condition drift"):
        original.bind_condition().assert_matches(revised)
    revised.bind_condition().assert_matches(revised)


@pytest.mark.parametrize("update", ({"revision": 3}, {"revision": 2}, {"experiment_id": "other"}, {"undeclared": True}))
def test_with_revision_rejects_invalid_revision_or_contract_updates(update: dict[str, Any]) -> None:
    manifest = ExperimentManifest.model_validate(_payload())
    with pytest.raises(ValueError):
        manifest.with_revision(**update)


@pytest.mark.parametrize("deep", (False, True))
def test_unchanged_model_copy_preserves_bound_identity(deep: bool) -> None:
    manifest = ExperimentManifest.model_validate(_payload())
    copied = manifest.model_copy(deep=deep)
    assert copied == manifest
    manifest.bind_condition().assert_matches(copied)


@pytest.mark.parametrize("update", ({"random_seed": 8}, {"grounded_agency": None}))
def test_grounded_agency_model_copy_cannot_silently_change_existing_revision(update: dict[str, Any]) -> None:
    manifest = ExperimentManifest.model_validate(_payload())
    with pytest.raises(ValueError, match="new manifest revision"):
        manifest.model_copy(update=update)
    revised = manifest.model_copy(update={**update, "revision": manifest.revision + 1})
    assert revised.revision == manifest.revision + 1
    assert revised.content_hash() != manifest.content_hash()


@pytest.mark.parametrize("method", ("with_revision", "model_copy"))
def test_revision_paths_revalidate_invalid_copied_nested_instances(method: str) -> None:
    manifest = ExperimentManifest.model_validate(_payload())
    # BaseModel.model_copy bypasses validation; the receiving parent must revalidate it.
    invalid_condition = manifest.grounded_agency.model_copy(update={"semantic_homeostasis": True})
    with pytest.raises(ValidationError, match="semantic_homeostasis"):
        if method == "with_revision":
            manifest.with_revision(grounded_agency=invalid_condition)
        else:
            manifest.model_copy(update={"grounded_agency": invalid_condition, "revision": manifest.revision + 1})


@pytest.mark.parametrize("nested", ("sensor_transform", "artifact_identity", "evaluation"))
def test_nested_model_instances_cannot_bypass_revalidation(nested: str) -> None:
    condition = GroundedAgencyCondition.model_validate(_condition())
    replacements = {
        "sensor_transform": {"encoding": "semantic"},
        "artifact_identity": {"prompt_digest": "invalid"},
        "evaluation": {"decision_schema_ref": None},
    }
    invalid_nested = getattr(condition, nested).model_copy(update=replacements[nested])
    payload = _condition()
    payload[nested] = invalid_nested
    with pytest.raises(ValidationError):
        GroundedAgencyCondition.model_validate(payload)


@pytest.mark.parametrize(
    ("filename", "expected_hash"),
    (
        ("development-demo.json", "19e75310d9f46b270dd3e373795d21ea6b31a645be90253aead1bcee21345e9a"),
        ("grounded-mistyai-v1.json", "4b88816d549be617fd0bedb5f43f263d95880401e89e0f5428c60074d3729b07"),
    ),
)
def test_legacy_manifest_hashes_remain_exactly_compatible(filename: str, expected_hash: str) -> None:
    manifest = load_experiment_manifest(ROOT / "presets/runtime" / filename)
    assert manifest.grounded_agency is None
    assert "grounded_agency" not in json.loads(manifest.canonical_json())
    assert manifest.content_hash() == expected_hash
    explicit_none = ExperimentManifest.model_validate({**manifest.model_dump(mode="json"), "grounded_agency": None})
    assert explicit_none.content_hash() == expected_hash
    with pytest.raises(ValueError, match="must be configured"):
        manifest.bind_condition()


def test_legacy_model_copy_retains_existing_unvalidated_copy_behavior() -> None:
    manifest = load_experiment_manifest(ROOT / "presets/runtime/development-demo.json")
    copied = manifest.model_copy(update={"random_seed": -1})
    assert copied.random_seed == -1
    assert copied.revision == manifest.revision


@pytest.mark.parametrize("grounded_agency", ({}, False, "", [], 0))
def test_legacy_model_copy_cannot_bypass_validation_with_falsey_opt_in(grounded_agency: Any) -> None:
    manifest = load_experiment_manifest(ROOT / "presets/runtime/development-demo.json")
    with pytest.raises(ValidationError):
        manifest.model_copy(update={"grounded_agency": grounded_agency})


def test_legacy_model_copy_with_explicit_none_keeps_legacy_behavior() -> None:
    manifest = load_experiment_manifest(ROOT / "presets/runtime/development-demo.json")
    copied = manifest.model_copy(update={"grounded_agency": None, "random_seed": -1})
    assert copied.grounded_agency is None
    assert copied.random_seed == -1
    assert copied.revision == manifest.revision
    assert manifest.model_copy(update={"grounded_agency": None}).content_hash() == manifest.content_hash()


@pytest.mark.parametrize(
    ("filename", "treatment"),
    (
        ("no-body-v1.json", "none"),
        ("opaque-current-v1.json", "opaque_current"),
        ("engineered-control-v1.json", "engineered_policy"),
    ),
)
def test_research_presets_are_complete_round_trippable_manifests(filename: str, treatment: str) -> None:
    manifest = load_experiment_manifest(ROOT / "presets/research/grounded-agency" / filename)
    assert manifest.grounded_agency.interoception_treatment == treatment
    restored = ExperimentManifest.model_validate_json(manifest.model_dump_json())
    manifest.bind_condition().assert_matches(restored)
