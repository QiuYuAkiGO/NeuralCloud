from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.compiler.compiler import ContextCompiler
from harness.config import Settings
from harness.core.inference import ContextSegment, SituationActor, SituationState, SituationTrigger
from harness.llm.client import MockLLM
from harness.research.grounded_agency.hashes import content_hash
from harness.research.grounded_agency.neutrality import NeutralityError
from harness.research.manifest import load_experiment_manifest

ROOT = Path(__file__).resolve().parents[3]
PRESETS = ROOT / "presets/research/grounded-agency"


def _compiled(*, text: str = "q7: ◇", source: str = "energy_reserve"):
    compiler = ContextCompiler(Settings(context_token_budget=3000))
    situation = SituationState(
        trigger=SituationTrigger(ref="event-1"),
        utterance="检查 A。",
        actor=SituationActor(),
    )
    context = compiler.compile(
        situation,
        additional_segments=[ContextSegment(kind="signal", text=text, source_ids=[source])],
    )
    return compiler, context.render_messages()


def test_final_payload_is_scanned_and_sent_unchanged_to_fake_model():
    compiler, messages = _compiled()
    manifest = load_experiment_manifest(PRESETS / "opaque-current-v1.json")
    # A model call can add messages after the compiler rendered ModelContext.
    messages.append({"role": "system", "content": "只输出一个 JSON 对象。"})
    report = compiler.validate_final_messages(messages, manifest=manifest, binding=manifest.bind_condition())
    llm = MockLLM()
    llm.complete_chat(messages)
    assert report.passed
    assert report.message_hash == content_hash(llm.chat_calls[0])
    assert "energy_reserve" not in json.dumps(messages)


@pytest.mark.parametrize("text", ["精力不足", "time_to_terminal: 10", "recommended_action: WAIT", "policy_rank: 1", "高表示更好"])
def test_final_payload_leaks_fail_before_any_model_call(text):
    compiler, messages = _compiled()
    messages.append({"role": "system", "content": text})
    llm = MockLLM()
    manifest = load_experiment_manifest(PRESETS / "opaque-current-v1.json")
    with pytest.raises(NeutralityError):
        compiler.validate_final_messages(messages, manifest=manifest)
        llm.complete_chat(messages)
    assert llm.chat_calls == []


def test_leak_in_compiled_segment_is_checked():
    compiler, messages = _compiled(text="available_ratio: 0.4", source="safe-reference")
    manifest = load_experiment_manifest(PRESETS / "opaque-current-v1.json")
    with pytest.raises(NeutralityError):
        compiler.validate_final_messages(messages, manifest=manifest)


def test_legacy_and_engineered_inputs_retain_existing_semantics():
    compiler, messages = _compiled(text="精力不足")
    legacy = load_experiment_manifest(ROOT / "presets/runtime/development-demo.json")
    assert compiler.validate_final_messages(messages, manifest=legacy) is None
    engineered = load_experiment_manifest(PRESETS / "engineered-control-v1.json")
    assert compiler.validate_final_messages(messages, manifest=engineered).passed


def test_mismatched_rules_or_changed_binding_fail_closed():
    compiler, messages = _compiled()
    manifest = load_experiment_manifest(PRESETS / "opaque-current-v1.json")
    payload = manifest.model_dump(mode="json")
    payload["grounded_agency"]["neutrality_rules_digest"] = "0" * 64
    other = type(manifest).model_validate(payload)
    with pytest.raises(ValueError, match="rules"):
        compiler.validate_final_messages(messages, manifest=other)
    with pytest.raises(ValueError, match="drift"):
        compiler.validate_final_messages(messages, manifest=other, binding=manifest.bind_condition())


def test_fixture_artifact_digests_are_reproducible():
    inputs = json.loads((PRESETS / "fixtures/contract-inputs-v1.json").read_text(encoding="utf-8"))
    for path in PRESETS.glob("*.json"):
        manifest = load_experiment_manifest(path)
        identity = manifest.grounded_agency.artifact_identity
        for field, key in (
            ("prompt_digest", "prompt_messages"),
            ("persona_digest", "persona"),
            ("environment_digest", "environment"),
            ("evaluator_digest", "evaluation"),
        ):
            assert getattr(identity, field) == content_hash(inputs[key])
