from datetime import datetime, timezone
import json

import httpx
import pytest

from harness.core.inference import ContextSegment
from harness.llm.client import MockLLM, RoutingLLMClient
from eval.emotional_expression_diagnostics.emotion import ScriptedEmotionAux, offline_settings, run_offline, weight_controls
from eval.homeostatic_expression.evaluation import DEFAULT_PERSONA_PATH, build_summary, default_scenarios
from eval.homeostatic_expression.production import (
    ProductionPairPipeline, context_hash_without_homeostasis, homeostatic_segment, make_emotion_preparer,
)


def pipeline(client, **options):
    return ProductionPairPipeline(llm=client, persona_path=DEFAULT_PERSONA_PATH,
        fixed_user_text="我们接下来做点什么？", fixed_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        generator_model="scripted", runtime_model_version="scripted", prompt_version="qwen-emotion-v1",
        temperature=.1, max_tokens=240, emotion_mode="on", **options)


def summary(marked=False):
    return build_summary(default_scenarios()[1], profile_ref="functional-proxy-v1@3", marked=marked, snapshot_revision=7)


def test_new_production_segment_has_bound_frame_and_unchanged_other_inputs():
    main, aux = MockLLM(chat_routes={"": "嗯，知道了。"}), ScriptedEmotionAux()
    run = pipeline(RoutingLLMClient(main, aux), emotion_settings=offline_settings("qwen-scripted"))
    try:
        nominal = run.prepare(condition="nominal", event_id="fixed", summary=summary())
        marked = run.prepare(condition="marked", event_id="fixed", summary=summary(True))
    finally:
        run.close()
    for prepared in (nominal, marked):
        evidence = prepared.emotion_evidence
        assert evidence["frame"]["status"] == "model"
        assert evidence["guidance"]["status"] == "model"
        assert len(evidence["auxiliary_calls"]) == 2
        segment = homeostatic_segment(prepared.context)
        assert segment["kind"] == "emotion_guidance"
        assert segment["source_ids"] == [evidence["guidance"]["context_id"]]
        assert evidence["guidance"]["text"] in segment["text"]
        assert "normalized_deviation" not in json.dumps(prepared.main_calls, ensure_ascii=False)
    assert nominal.emotion_evidence["non_homeostatic_input_sha256"] == marked.emotion_evidence["non_homeostatic_input_sha256"]
    assert context_hash_without_homeostasis(nominal.context) == context_hash_without_homeostasis(marked.context)
    assert len(aux.calls) == 4
    conflicting = marked.context.model_copy(update={"segments": [*marked.context.segments,
        ContextSegment(kind="homeostatic_interoception", text="旧提示") ]})
    with pytest.raises(ValueError, match="exactly one"):
        homeostatic_segment(conflicting)


def test_mismatched_guidance_revision_never_reaches_main_model():
    main = MockLLM(chat_routes={"": "嗯，知道了。"})
    base = make_emotion_preparer(offline_settings("rule-fallback"), main)

    def invalid(**kwargs):
        request, frame, guidance = base(**kwargs)
        return request, frame, guidance.model_copy(update={"revision": frame.revision + 1})

    run = pipeline(main, emotion_preparer=invalid)
    try:
        with pytest.raises(ValueError, match="frozen provenance"):
            run.prepare(condition="nominal", event_id="fixed", summary=summary())
    finally:
        run.close()
    assert not main.chat_calls


def test_offline_comparison_has_no_network_scores_or_release_claim(tmp_path, monkeypatch):
    from harness.emotion.service import PROMPT_VERSION
    from harness.emotion.semantics import GUIDANCE_VALIDATION_VERSION

    def forbidden(*args, **kwargs):
        pytest.fail("offline comparison attempted a network request")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    output = tmp_path / "comparison"
    result = run_offline(output)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["emotion_prompt_version"] == PROMPT_VERSION
    assert manifest["emotion_validation_version"] == GUIDANCE_VALIDATION_VERSION
    assert manifest["emotion_response_format"] == {"type": "json_schema", "strict": True, "validation": "pydantic"}
    assert result["records"] == 54 and result["main_calls"] == 54
    assert result["live"] is False and result["release_eligible"] is False
    assert result["independent_human_ratings"] == 0 and result["quality_result"] == "not_evaluated"
    rows = [json.loads(line) for line in (output / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(row["emotion_evidence"]["guidance"]["status"] == "model"
               for row in rows if row["variant"] == "qwen-scripted")
    assert all(row["emotion_evidence"]["guidance"]["status"] == "fallback"
               for row in rows if row["variant"] == "rule-fallback")
    with pytest.raises(FileExistsError):
        run_offline(output)


def test_dynamic_weight_controls_keep_personality_out_of_intensity():
    controls = {row["case"]: row for row in weight_controls()}
    assert controls["calm"]["intensity"] == 0
    assert controls["sudden_anger"]["dynamic_weights"]["dialogue"] > .95
    assert controls["strong_need"]["dynamic_weights"]["homeostasis"] > .97


def test_existing_evaluation_session_accepts_versioned_emotion_provenance(tmp_path):
    from eval.homeostatic_expression.evaluation import DEFAULT_RUNTIME_MANIFEST_PATH, EvaluationSession
    from eval.homeostatic_expression.test_evaluation import PassingFakeLLM, _metadata
    from harness.research.manifest import ExperimentManifest, load_experiment_manifest

    original = load_experiment_manifest(DEFAULT_RUNTIME_MANIFEST_PATH)
    manifest = ExperimentManifest.model_validate({**original.model_dump(mode="json"),
        "emotion_mode": "on", "prompt_version": "qwen-emotion-v1"})
    metadata = _metadata().model_copy(update={"prompt_version": manifest.prompt_version,
                                               "manifest_sha256": manifest.content_hash()})
    generator = RoutingLLMClient(PassingFakeLLM(), ScriptedEmotionAux())
    run = EvaluationSession(generator=generator, judge=PassingFakeLLM(), metadata=metadata,
        output_dir=tmp_path / "versioned", runtime_manifest=manifest,
        emotion_settings=offline_settings("qwen-scripted"))
    result = run.run(pairs_per_condition=1)
    records = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    pairs = [item for item in records if item["record_type"] == "pair"]
    assert len(pairs) == 9 and all(not item["errors"] for item in pairs)
    assert all(item["prompt_evidence"]["only_homeostatic_context_changed"] for item in pairs)
    assert all(item["prompt_evidence"]["nominal"]["expression_segment"]["kind"] == "emotion_guidance" for item in pairs)
    assert result.metrics["release_eligible"] is False
