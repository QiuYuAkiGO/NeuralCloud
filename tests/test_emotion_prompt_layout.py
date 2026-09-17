from __future__ import annotations

import json
import asyncio

from gui.presets import load_builtin
from harness.emotion import EmotionService
from harness.emotion.service import _response_schema
from harness.qq.telemetry import QQTurnTrace, qq_trace_scope
from tests.test_emotion_service import FakeAux, request, settings


class CaptureAux(FakeAux):
    def __init__(self):
        super().__init__()
        self.raw = []

    def complete_json(self, *, system, user, **kwargs):
        self.raw.append((user, kwargs))
        return super().complete_json(system=system, user=user, **kwargs)


def test_two_steps_keep_current_semantics_and_frontload_stable_persona():
    aux = CaptureAux()
    service = EmotionService(settings(), aux)
    original = request(persona=load_builtin("misty").persona)
    trace = QQTurnTrace()
    with qq_trace_scope(trace):
        frame = service.observe(original)
        guidance = service.guidance(frame)
    assert len(aux.raw) == 2
    appraisal_text, appraisal_options = aux.raw[0]
    guidance_text, guidance_options = aux.raw[1]
    assert appraisal_text.startswith('{"persona":') and guidance_text.startswith('{"persona":')
    appraisal, directed = json.loads(appraisal_text), json.loads(guidance_text)
    assert appraisal["messages"][0]["text"] == original.messages[0].text
    assert appraisal["messages"][0]["source_id"] == original.messages[0].source_id
    assert "speaker" not in appraisal["messages"][0] and "scope" not in appraisal["messages"][0]
    assert directed["shared_affect"] == frame.labels
    assert directed["weights"] == frame.weights and directed["dialogue_trends"] == frame.dialogue_trends
    assert directed["homeostatic_needs"] == frame.homeostatic_needs
    assert appraisal_options["timeout_seconds"] == 8 and guidance_options["timeout_seconds"] == 4
    assert "schema" not in appraisal and "schema" not in directed
    assert frame.diagnostics["response_schema_bytes"] > 0
    assert guidance.diagnostics["prompt_layout_version"] == "persona-first-v1"
    stages = {item["name"] for item in trace.snapshot()["stages"]}
    assert {"emotion.appraisal.input", "emotion.appraisal.model",
            "emotion.guidance.input", "emotion.guidance.model"} <= stages
    counters = trace.snapshot()["counters"]
    for stage in ("appraisal", "guidance"):
        assert counters[f"emotion_{stage}_model_calls"] == counters[f"emotion_{stage}_results"] == 1
        assert counters[f"emotion_{stage}_fallbacks"] == counters[f"emotion_{stage}_reused_results"] == 0
    assert original.messages[0].text not in json.dumps(trace.snapshot(), ensure_ascii=False)
    replay = QQTurnTrace()
    with qq_trace_scope(replay):
        service.observe(original)
        service.guidance(frame)
    for stage in ("appraisal", "guidance"):
        assert replay.snapshot()["counters"][f"emotion_{stage}_model_calls"] == 0
        assert replay.snapshot()["counters"][f"emotion_{stage}_results"] == 0
        assert replay.snapshot()["counters"][f"emotion_{stage}_reused_results"] == 1
    assert len(aux.raw) == 2


def test_cached_schema_never_shares_mutable_provider_arguments():
    first = _response_schema("appraisal")
    untouched = _response_schema("appraisal")
    first["properties"].clear()
    assert _response_schema("appraisal") == untouched


def test_fallback_without_model_and_restart_reuse_are_distinct():
    aux = CaptureAux()
    aux.aux_distinct = False
    service = EmotionService(settings(), aux)
    original = request()
    trace = QQTurnTrace()
    with qq_trace_scope(trace):
        frame = service.observe(original)
        guidance = service.guidance(frame)
    assert frame.status == guidance.status == "fallback" and not aux.raw
    for stage in ("appraisal", "guidance"):
        counters = trace.snapshot()["counters"]
        assert counters[f"emotion_{stage}_model_calls"] == counters[f"emotion_{stage}_reused_results"] == 0
        assert counters[f"emotion_{stage}_results"] == counters[f"emotion_{stage}_fallbacks"] == 1
    restored = EmotionService(settings(), CaptureAux())
    restored.load(service.snapshot())
    replay = QQTurnTrace()
    with qq_trace_scope(replay):
        assert restored.observe(original) == frame
        assert restored.guidance(frame) == guidance
    for stage in ("appraisal", "guidance"):
        counters = replay.snapshot()["counters"]
        assert counters[f"emotion_{stage}_model_calls"] == counters[f"emotion_{stage}_fallbacks"] == 0
        assert counters[f"emotion_{stage}_results"] == 0 and counters[f"emotion_{stage}_reused_results"] == 1


def test_invalid_model_result_counts_a_real_call_and_one_committed_fallback():
    aux = FakeAux(appraisal=lambda payload: {"events": []}, guidance="我已经联系了群主")
    service = EmotionService(settings(), aux)
    trace = QQTurnTrace()
    with qq_trace_scope(trace):
        frame = service.observe(request())
        guidance = service.guidance(frame)
    assert frame.status == guidance.status == "fallback" and len(aux.calls) == 2
    for stage in ("appraisal", "guidance"):
        counters = trace.snapshot()["counters"]
        assert counters[f"emotion_{stage}_model_calls"] == counters[f"emotion_{stage}_fallbacks"] == 1
        assert counters[f"emotion_{stage}_results"] == 1 and counters[f"emotion_{stage}_reused_results"] == 0


def test_owner_appraisal_keeps_exact_qq_request_trace_identity(tmp_path):
    from infra.trace.request_spans import current_trace_context
    from tests.test_basic_persona_continuity import _command
    from tests.test_cognitive_incremental_store import _owner

    async def run():
        owner, _, aux = _owner(tmp_path, emotion="on")
        seen = []
        original = aux.complete_json
        def invoke(**kwargs):
            seen.append(current_trace_context())
            return original(**kwargs)
        aux.complete_json = invoke
        owner.emotion_input_provider = lambda command: request(context_id=command.payload["context_id"])
        trace_id = "qq-trace:" + "d" * 32
        command = _command("ObserveEmotion", 1, {"context_id": "qq-current-frame",
            "source_command_type": "PrepareQQTurn", "source_payload": {"trace": {"trace_id": trace_id}}})
        result = await owner.observe_emotion(command)
        assert result.status == "applied"
        assert seen[0]["qq_trace_id"] == trace_id
        assert seen[0]["correlation_id"] == command.correlation_id
        await owner.close()
    asyncio.run(run())
