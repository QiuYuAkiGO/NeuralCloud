import copy
import json

import httpx
import pytest

from eval.emotional_expression_diagnostics.emotion_multiturn import run_offline_multiturn


@pytest.fixture(scope="module")
def comparison(tmp_path_factory):
    output = tmp_path_factory.mktemp("multiturn") / "evidence"
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail("offline comparison attempted a network request")
        patch.setattr(httpx.Client, "send", forbidden)
        summary = run_offline_multiturn(output)
    records = [json.loads(line) for line in (output / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    return output, summary, records


def test_actual_director_and_main_requests_differ_only_after_weight_intervention(comparison):
    _, _, records = comparison
    different_briefs = 0
    different_replies = 0
    for dynamic, fixed in zip(records[::2], records[1::2], strict=True):
        assert (dynamic["arm"], fixed["arm"]) == ("dynamic", "fixed")
        assert dynamic["emotion_evidence"]["input"] == fixed["emotion_evidence"]["input"]
        assert dynamic["window_sha256"] == fixed["window_sha256"]
        assert dynamic["main_history"] == fixed["main_history"]
        assert dynamic["main_history_sha256"] == fixed["main_history_sha256"]
        assert dynamic["generation_fixture_sha256"] == fixed["generation_fixture_sha256"]
        assert dynamic["non_emotion_context_sha256"] == fixed["non_emotion_context_sha256"]
        frames = [copy.deepcopy(row["emotion_evidence"]["frame"]) for row in (dynamic, fixed)]
        for frame in frames:
            frame.pop("weights")
            frame.pop("dominant_source")
            frame["diagnostics"].pop("elapsed_ms", None)
        assert frames[0] == frames[1]
        da, dg = dynamic["auxiliary_requests"]
        fa, fg = fixed["auxiliary_requests"]
        assert da["execution"] == "delegate_call" and fa["execution"] == "replayed_frozen_appraisal"
        assert da["request_sha256"] == fa["request_sha256"] and da["response"] == fa["response"]
        assert dg["execution"] == fg["execution"] == "delegate_call"
        for call in (da, fa, dg, fg):
            output_contract = call["response_format"]
            assert output_contract["type"] == "json_schema"
            assert output_contract["json_schema"]["strict"] is True
            assert output_contract["json_schema"]["name"] == "emotion_" + call["stage"]
        assert dg["user"]["weights"] != fg["user"]["weights"]
        for guidance_call in (dg, fg):
            assert set(guidance_call["response"]) == {"feeling", "delivery", "needs"}
            assert "window" not in guidance_call["user"]
            assert "speech_style" not in guidance_call["user"]["persona"]
            schema = guidance_call["response_format"]["json_schema"]["schema"]
            assert set(schema["required"]) == {"feeling", "delivery", "needs"}
            for field in ("feeling", "needs"):
                assert guidance_call["response"][field] == guidance_call["user"]["required_expression"][field]
        treatment_fields = {"weights", "dominant_source", "semantic_priority"}
        assert {key: value for key, value in dg["user"].items() if key not in treatment_fields} == {
            key: value for key, value in fg["user"].items() if key not in treatment_fields}
        assert fixed["emotion_evidence"]["frame"]["dominant_source"] == fg["user"]["dominant_source"] == "homeostasis"
        assert all(value == 1 / 3 for value in fg["user"]["weights"].values())
        assert "首先直接体现最强需要" in fg["user"]["semantic_priority"]
        if dg["user"]["dominant_source"] == "dialogue":
            assert dg["user"]["semantic_priority"] != fg["user"]["semantic_priority"]
        briefs = [row["emotion_evidence"]["guidance"]["text"] for row in (dynamic, fixed)]
        for row, brief in zip((dynamic, fixed), briefs, strict=True):
            assert row["successful_model_guidance"]
            assert len(row["main_calls"]) == 1
            prompt = json.dumps(row["main_calls"][0]["messages"], ensure_ascii=False)
            assert prompt.count(brief) == 1
            assert "normalized_deviation" not in prompt and '"weights"' not in prompt
        if briefs[0] != briefs[1]:
            different_briefs += 1
            different_replies += dynamic["final"]["text"] != fixed["final"]["text"]
    assert different_briefs >= 15 and different_replies >= 15


def test_multiturn_state_accumulates_and_recovers_without_generated_reply_feedback(comparison):
    _, _, records = comparison
    groups = {}
    for row in records:
        groups.setdefault((row["scenario"], row["arm"]), []).append(row)
    assert len(groups) == 8 and all(len(rows) == 5 for rows in groups.values())
    for rows in groups.values():
        assert [len(row["window"]) for row in rows] == [1, 3, 5, 7, 9]
        for index, row in enumerate(rows):
            request = row["emotion_evidence"]["input"]
            assert len(request["new_source_ids"]) == 1
            assert all(message["role"] == "user" for message in request["messages"]
                       if message["source_id"] in request["new_source_ids"])
            assert all(message["text"] == "嗯，我听到了。" for message in row["window"] if message["role"] == "character")
            assert row["emotion_evidence"]["frame"]["revision"] == index + 1
            main_history = row["main_history"]
            assert len(main_history) == 2 * index
            assert [item["event_id"] for item in main_history] == [item["source_id"] for item in row["window"][:-1]]
            messages = row["main_calls"][0]["messages"]
            prompt = "\n".join(item["content"] for item in messages)
            # Check the actual compiler/main boundary contains the full ordered
            # fixture, including previous character turns, not just its hash.
            if main_history:
                serialized_history = "\n".join(f"{item['speaker']}: {item['text']}" for item in main_history)
                assert serialized_history in prompt
            assert all(item["text"] == "嗯，我听到了。" for item in main_history if item["role"] == "character")
    strong = groups[("strong_dialogue", "dynamic")]
    amounts = [row["emotion_evidence"]["frame"]["labels"]["anger"] for row in strong]
    assert amounts[0] > .9
    assert 0 < amounts[1] < amounts[0]
    assert amounts[2] > amounts[1] * .8  # New mild stimulus offsets most three-minute decay.
    recovery = groups[("recovery", "dynamic")]
    before = recovery[1]["emotion_evidence"]["frame"]["labels"]["anger"]
    after = recovery[2]["emotion_evidence"]["frame"]["labels"]["anger"]
    assert after < before / 2
    assert "正在缓和" in recovery[2]["expression_segment"]["text"]


def test_offline_multiturn_evidence_is_not_release_or_quality_evidence(comparison):
    from harness.emotion.service import PROMPT_VERSION
    from harness.emotion.semantics import GUIDANCE_VALIDATION_VERSION
    from eval.homeostatic_expression.production import json_hash

    output, summary, records = comparison
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["emotion_prompt_version"] == PROMPT_VERSION
    assert manifest["emotion_validation_version"] == GUIDANCE_VALIDATION_VERSION
    assert manifest["emotion_response_format"] == {"type": "json_schema", "strict": True, "validation": "pydantic"}
    saved_hash = manifest.pop("spec_sha256")
    assert saved_hash == json_hash(manifest)
    altered_guard = {**manifest, "emotion_validation_version": "another-guard-version"}
    assert saved_hash != json_hash(altered_guard)
    manifest["emotion_prompt_version"] = "another-prompt-version"
    assert saved_hash != json_hash(manifest)
    assert summary["records"] == summary["main_calls"] == summary["successful_guidance_records"] == 40
    assert summary["auxiliary_requests"] == 80 and summary["auxiliary_delegate_calls"] == 60
    assert summary["main_truncated_calls"] == 0 and summary["main_finish_reason_unavailable_calls"] == 0
    assert "role and quality may still be wrong" in summary["metric_definitions"]["successful_guidance_records"]
    assert summary["director_role_review"] == "not_scored"
    assert manifest["metric_definitions"] == summary["metric_definitions"]
    assert not summary["live"] and not summary["release_eligible"]
    assert summary["independent_human_ratings"] == 0 and summary["quality_result"] == "not_evaluated"
    assert all(not row["live"] and not row["release_eligible"] and not row["quality_scored"] for row in records)
    with pytest.raises(FileExistsError):
        run_offline_multiturn(output)


def test_live_aux_requires_explicit_target_and_keeps_main_scripted(tmp_path, monkeypatch):
    from eval.emotional_expression_diagnostics import emotion_multiturn as module

    constructed = []

    class FakeAux(module._ScriptedAux):
        def __init__(self, settings, *, extra_payload, request_observer):
            super().__init__()
            constructed.append((settings, extra_payload))
            request_observer({"record_type": "synthetic_transport_test"})

        def close(self):
            pass

    monkeypatch.setattr(module, "OpenAICompatClient", FakeAux)
    monkeypatch.setattr("gui.persist.load_gui_settings", lambda *_: pytest.fail("main config was read without explicit opt-in"))
    with pytest.raises(SystemExit):
        module.main(["--out", str(tmp_path / "rejected"), "--aux-base-url", "http://localhost:11435/v1"])
    assert not constructed and not (tmp_path / "rejected").exists()
    result = module.run_live_aux_multiturn(tmp_path / "real-mode-wiring", base_url="http://localhost:11435/v1",
        model="explicit-test-model", api_key="test-private-key", scenario_ids=["recovery"])
    assert result["live_aux"] is True and result["live_main"] is False
    assert result["records"] == 10 and result["auxiliary_delegate_calls"] == 15
    assert result["release_eligible"] is False
    assert constructed[0][0].llm_model == "explicit-test-model"
    assert constructed[0][0].llm_base_url == "http://localhost:11435/v1"
    assert constructed[0][0].emotion_appraisal_timeout_seconds == 8
    assert constructed[0][0].emotion_guidance_timeout_seconds == 4
    assert "test-private-key" not in "".join(path.read_text(encoding="utf-8")
        for path in (tmp_path / "real-mode-wiring").glob("*.json*"))


def test_output_schema_reaches_serialized_http_and_partitions_appraisal_cache():
    from harness.config import Settings
    from harness.emotion.models import EmotionAppraisal
    from harness.llm.client import OpenAICompatClient
    from eval.emotional_expression_diagnostics.emotion_multiturn import _CapturedClient
    from eval.homeostatic_expression.production import CapturingGenerationRouter

    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"events": []}'}, "finish_reason": "stop"}]})

    delegate = OpenAICompatClient(Settings(_env_file=None, llm_base_url="https://synthetic.invalid/v1",
                                          llm_model="synthetic", llm_api_key="synthetic"))
    delegate._client.close()
    delegate._client = httpx.Client(transport=httpx.MockTransport(transport))
    captured = _CapturedClient(delegate, {})
    router = CapturingGenerationRouter(captured, temperature=.1, max_tokens=240)
    schema = {"type": "json_schema", "json_schema": {"name": "emotion_appraisal", "strict": True,
        "schema": EmotionAppraisal.model_json_schema()}}
    different = copy.deepcopy(schema)
    different["json_schema"]["schema"]["properties"]["contract_revision"] = {"type": "string", "const": "second"}
    try:
        for output_format in (schema, different, schema):
            assert router.complete_json(system="EMOTION_APPRAISAL_V1", user='{"new_source_ids": []}',
                max_tokens=1536, timeout_seconds=8, response_format=output_format) == {"events": []}
    finally:
        delegate.close()
    assert [request["response_format"] for request in requests] == [schema, different]
    assert [call["response_format"] for call in router.auxiliary_calls] == [schema, different, schema]
    assert [call["execution"] for call in captured.calls] == ["delegate_call", "delegate_call", "replayed_frozen_appraisal"]
    assert captured.calls[0]["request_sha256"] != captured.calls[1]["request_sha256"]
    assert captured.calls[0]["request_sha256"] == captured.calls[2]["request_sha256"]


def test_absent_output_contract_does_not_change_legacy_delegate_signature():
    from eval.emotional_expression_diagnostics.emotion_multiturn import _CapturedClient
    from eval.homeostatic_expression.production import CapturingGenerationRouter

    class Legacy:
        def complete_json(self, *, system, user, max_tokens=None):
            return {"events": []}

    captured = _CapturedClient(Legacy(), {})
    router = CapturingGenerationRouter(captured, temperature=.1, max_tokens=240)
    assert router.complete_json(system="EMOTION_APPRAISAL_V1", user="{}", response_format=None) == {"events": []}
    assert "response_format" not in router.auxiliary_calls[0]
    assert "response_format" not in captured.calls[0]


@pytest.mark.parametrize("main_finish_reason", ["stop", "length"])
def test_explicit_live_main_config_uses_real_http_path_without_persisting_credentials(tmp_path, monkeypatch, main_finish_reason):
    from gui import persist
    from harness.llm.client import OpenAICompatClient
    from eval.emotional_expression_diagnostics import emotion_multiturn as module

    main_config = tmp_path / "explicit-main.json"
    main_config.write_text(json.dumps({"mode": "live", "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash", "api_key": "PRIVATE_MAIN_TEST_KEY"}), encoding="utf-8")
    loaded, requests, constructed = [], [], []
    load = persist.load_gui_settings

    def load_explicit(path):
        loaded.append(path)
        return load(path)

    auxiliary = module._ScriptedAux()

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["model"] == "explicit-aux":
            output = auxiliary.complete_json(system=payload["messages"][0]["content"],
                user=payload["messages"][1]["content"], response_format=payload["response_format"])
            text = json.dumps(output, ensure_ascii=False)
        else:
            text = "嗯，先理一项吧。"
        return httpx.Response(200, json={"model": payload["model"],
            "choices": [{"message": {"content": text},
                         "finish_reason": "stop" if payload["model"] == "explicit-aux" else main_finish_reason}]})

    def http_client(settings, **options):
        constructed.append(settings.llm_model)
        client = OpenAICompatClient(settings, **options)
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(transport))
        return client

    monkeypatch.setattr(persist, "load_gui_settings", load_explicit)
    monkeypatch.setattr(module, "OpenAICompatClient", http_client)
    with pytest.raises(SystemExit):
        module.main(["--out", str(tmp_path / "rejected"), "--live-main-config", str(main_config)])
    assert not loaded and not constructed and not requests
    output = tmp_path / "live-main-http-test"
    result = module.run_live_aux_multiturn(output, base_url="https://synthetic.invalid/aux/v1",
        model="explicit-aux", api_key="PRIVATE_AUX_TEST_KEY", scenario_ids=["strong_dialogue"], live_main_config=main_config)
    assert loaded == [main_config] and constructed == ["explicit-aux", "deepseek-v4-flash"]
    assert result["live_aux"] is True and result["live_main"] is True and result["live"] is True
    assert result["models"]["main"] == "deepseek-v4-flash"
    main_calls = [payload for payload in requests if payload["model"] == "deepseek-v4-flash"]
    assert len(main_calls) == result["main_calls"] == 10
    assert result["main_truncated_calls"] == (10 if main_finish_reason == "length" else 0)
    assert result["main_finish_reason_unavailable_calls"] == 0
    assert all(payload["max_tokens"] == 240 and payload["temperature"] == .1
               and payload["reasoning_effort"] == "low" for payload in main_calls)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["live_main"] and manifest["main_request_options"]["reasoning_effort"] == "low"
    records = [json.loads(line) for line in (output / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(row["live_main"] and row["models"]["main"] == "deepseek-v4-flash"
               and row["evidence_kind"] == "real_aux_real_main_multiturn" for row in records)
    assert all(row["main_calls"][0]["response_metadata"]["finish_reason"] == main_finish_reason for row in records)
    saved = "".join(path.read_text(encoding="utf-8") for path in output.glob("*.json*"))
    assert "PRIVATE_MAIN_TEST_KEY" not in saved and "PRIVATE_AUX_TEST_KEY" not in saved
    assert not result["release_eligible"] and result["quality_result"] == "not_evaluated"


def test_mixed_feelings_survive_strong_need_and_omissions_are_not_counted_as_accepted():
    from harness.llm.client import RoutingLLMClient
    from eval.emotional_expression_diagnostics import emotion_multiturn as module

    class MixedAux(module._ScriptedAux):
        omit_anger = False
        omit_recovery = False

        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            if "EMOTION_APPRAISAL_V1" in kwargs["system"]:
                for event in result["events"]:
                    if event["signals"]:
                        event["signals"] = [{"label": "calm", "intensity": .8, "confidence": 1},
                                            {"label": "anger", "intensity": .7, "confidence": 1}]
            else:
                if self.omit_anger:
                    result["feeling"] = "明显平静，正在缓和，仍有余韵。"
                if self.omit_recovery and any(item["trend"] == "recovering"
                                             for item in json.loads(kwargs["user"])["required_feelings"]):
                    result["feeling"] = "明显平静，同时明显愤怒。"
            return result

    scenario = {"scenario_id": "mixed_under_need", "turns": [
        {"minute": minute, "text": text, "homeostatic_strength": .95, "trend": "steady",
         "previous_fixture_reply": "嗯，我听到了。"}
        for minute, text in ((0, module.ANGRY), (2, module.NEUTRAL))]}

    def run(**faults):
        clients = {}
        for arm in module.ARMS:
            aux = MixedAux()
            for key, value in faults.items():
                setattr(aux, key, value)
            clients[arm] = RoutingLLMClient(module._scripted_main(), aux)
        return module.evaluate_scenario(scenario, clients=clients, settings=module.offline_settings("qwen-scripted"))

    correct = run()
    for row in correct:
        assert row["emotion_evidence"]["frame"]["dominant_source"] == "homeostasis"
        guidance_call = row["auxiliary_requests"][1]
        assert [item["label"] for item in guidance_call["user"]["required_feelings"]] == ["calm", "anger"]
        text = row["emotion_evidence"]["guidance"]["text"]
        assert text.startswith("首要需要：") and "愤怒" in text and "平静" in text
        assert row["successful_model_guidance"]
    missing_mood = run(omit_anger=True)
    assert all(not row["successful_model_guidance"] for row in missing_mood)
    assert all(row["emotion_evidence"]["guidance"]["diagnostics"]["fallback_reason"] == "emotion_guidance_feeling_missing"
               for row in missing_mood)
    missing_recovery = run(omit_recovery=True)
    assert all(row["successful_model_guidance"] for row in missing_recovery[:2])
    assert all(not row["successful_model_guidance"] for row in missing_recovery[2:])
    assert all(row["main_calls"] and not row["quality_scored"] and not row["release_eligible"] for row in missing_mood)
