"""Production QQ expression boundary with synthetic inputs and fake HTTP only."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from gui.presets import load_builtin
from harness.cognitive.shared_memory import ScopedQQMemoryView, SharedQQBinding
from harness.homeostasis.models import HomeostaticInteroceptiveSummary, _SEMANTIC_NEEDS
from harness.llm.client import MockLLM, OpenAICompatClient, RoutingLLMClient
from harness.qq import decision
from harness.qq.conversation import private_scope_id
from harness.qq.memory import QQVisualMemoryStore
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.telemetry import QQTurnTrace
from harness.research.manifest import load_experiment_manifest
from harness.runtime.adapters import DomainSnapshot
from harness.runtime.models import RuntimeEvent, RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle
from harness.runtime.supervisor import RuntimeSupervisor
from infra.trace.request_spans import current_trace_context
from infra.trace.diagnostics import observation_scope
from tests.test_basic_persona_continuity import _host
from tests.test_qq_decision import add_member


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs/milestones/evidence/qq-expression-wiring-baseline-2026-09-14.json"


def summary(**changes):
    return HomeostaticInteroceptiveSummary.model_validate({
        "profile_ref": "functional-proxy-v1@3", "snapshot_revision": 7,
        "salience": "marked", "episode_signature": "synthetic-expression-episode",
        "dominant_needs": [{"dimension": "cognitive_load", "direction": "high",
                            "normalized_deviation": .6, "trend": "worsening", "confidence": .9,
                            "semantic_need": _SEMANTIC_NEEDS[("cognitive_load", "high")]}],
        **changes,
    })


def install_summary(runtime, value):
    runtime._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=value.snapshot_revision,
        payload={"profile_id": "functional-proxy-v1", "profile_revision": 3,
                 "interoceptive_summary": value.model_dump(mode="json")})


async def exercise(tmp_path, monkeypatch, *, transport="onebot", private=False, mode="responses",
                   supplied=None, idle_intent=None, trusted=None, replay=False, reply=None,
                   deadline_seconds=55, replay_critical=False):
    """Do not create a body actor, socket, live model request, or delivery.

    The production Runtime dispatch/host/decision/pipeline/client run unchanged.
    Existing body-source verification is stubbed because there is no real QQ
    source; its separate test suite covers authorization and body provenance.
    """
    monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
    monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
    host, _ = _host(tmp_path, monkeypatch, ["unused"])
    group_id = private_scope_id("29999", "20001") if private else "10001"
    store = QQVisualMemoryStore(tmp_path / "synthetic-qq.sqlite3")
    source_text = "合成场景：今天的讨论有些密集，你想继续吗？"
    if private:
        from tests.test_qq_protocol_memory import protocol_profile, protocol_observation
        _, profile = protocol_profile(store, group=group_id, member="20001")
        store.record_protocol_observation(protocol_observation(profile, text=source_text, image_analysis=[]))
    else:
        add_member(store, "20001", group=group_id, text=source_text)
    settings = host.chat.settings.model_copy(update={
        "llm_model": "synthetic-expression-model", "llm_base_url": "https://synthetic.invalid/v1",
        "llm_api_key": "synthetic-unused", "record_interaction": False, "call_log_enabled": False,
        "parser_llm_enabled": False, "planner_llm_enabled": False, "tier_llm_enabled": False,
        "continuation_enabled": False, "aux_llm_preload": False, "trace_dir": str(tmp_path / "traces"),
    })
    http_calls = []

    def respond(request):
        assert str(request.url) == "https://synthetic.invalid/v1/chat/completions"
        body = json.loads(request.content)
        gate = decision.is_qq_gate_messages(body["messages"])
        stage = "qq_gate" if gate else "character_expression"
        http_calls.append({"stage": stage, "payload": body, "trace_context": current_trace_context()})
        text = ('{"action":"speak","reason":"合成场景中可以表达具体偏好。"}' if gate else
                reply or "我想先歇一小会儿，等思路清楚了再接着聊。[END]")
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})

    main = OpenAICompatClient(settings)
    main._client.close()
    main._client = httpx.Client(transport=httpx.MockTransport(respond))
    llm = RoutingLLMClient(main, MockLLM())
    owner_memory = host.chat.memory
    view = ScopedQQMemoryView(owner_memory, group_id=group_id, member_id="20001")
    binding = SharedQQBinding(settings, load_builtin("misty"), llm, view, {}, ())
    host.shared.qq_settings = SimpleNamespace(memory_db=store.path)
    monkeypatch.setattr(host.shared, "qq_binding", lambda **kwargs: binding)
    source_checks = []
    monkeypatch.setattr("harness.runtime.qq_turns._source", lambda *args: source_checks.append(True))
    manifest = load_experiment_manifest(ROOT / "presets/runtime/development-demo.json")
    manifest = manifest.model_copy(update={"homeostatic_expression":
        manifest.homeostatic_expression.model_copy(update={"mode": mode})})
    runtime = RuntimeSupervisor(manifest=manifest, cognitive=host, homeostasis=object(),
                                state_dir=tmp_path / "runtime", command_timeout_seconds=60)
    # This fixture bypasses startup; results still require a running Runtime.
    runtime.lifecycle = RuntimeLifecycle.RUNNING
    state = trusted or summary()
    install_summary(runtime, state)
    runtime._cognitive_snapshot = await host.get_snapshot()
    now = datetime.now(timezone.utc)
    trace = QQTurnTrace(event_id="qq-watch-event:" + "a" * 32, first_seen_at=now)
    request = QQSharedPrepareRequest(transport=transport, group_id=group_id, self_member_id="29999",
        trigger_member_id=None if idle_intent else "20001", idle_intent=idle_intent,
        homeostatic_interoception=supplied, deadline_at=now + timedelta(seconds=deadline_seconds), trace={
            "trace_id": trace.trace_id, "event_id": trace.event_id, "first_seen_at": now,
            "first_seen_mono": trace.first_seen_mono})
    event = RuntimeEvent(event_id="synthetic-runtime-qq-turn", receive_seq=1,
        correlation_id=trace.trace_id, event_type=RuntimeEventType.QQ_TURN,
        priority=RuntimeEventPriority.USER, source="qq_body", source_time=now, received_at=now,
        idempotency_key="synthetic-qq-turn", manifest_revision=1, payload=request.model_dump(mode="json"))
    before = owner_memory.get_state().model_dump(mode="json")
    diagnostics = []
    try:
        with observation_scope(diagnostics.append):
            result = await runtime._dispatch_qq_turn(event, 1)
        assert result.status == "completed", result.detail
        assert result.detail["decision"]["action"] == "speak", result.detail
        assert source_checks and owner_memory is view.owner
        assert before["persona"] == owner_memory.get_state().model_dump(mode="json")["persona"]
        assert view.get_state().psych is None
        intents = [row.data["envelope"] for row in runtime.journal.iter_records()
                   if row.record_type == "domain_command_intent"]
        if replay:
            install_summary(runtime, summary(snapshot_revision=8,
                salience="critical" if replay_critical else "marked", episode_signature="changed-synthetic-episode"))
            count = len(http_calls)
            again = await runtime._dispatch_qq_turn(event, 2)
            assert len(http_calls) == count
            if not replay_critical:
                assert again.detail == result.detail
            repeated = [row.data["envelope"] for row in runtime.journal.iter_records()
                        if row.record_type == "domain_command_intent"][-1]
            assert repeated["payload"] == intents[0]["payload"]
            assert repeated["version_vector"] == intents[0]["version_vector"]
        return {"transport": transport, "private": private, "mode": mode,
                "supplied_summary": supplied is not None, "trusted_summary": state.model_dump(mode="json"),
                "runtime_command": intents[0], "http_calls": http_calls,
                "diagnostics": diagnostics,
                "decision": result.detail["decision"], "source_checks_stubbed": True,
                "replay_detail": again.detail if replay else None,
                "owner_persona_unchanged": True, "psych_scope_filter_retained": True,
                "external_side_effects": 0}
    finally:
        runtime.goals.close()
        runtime.capabilities.close()
        llm.close()
        await host.close()


@pytest.mark.parametrize("transport", ["onebot", "desktop"])
def test_synthetic_path_reaches_final_http(tmp_path, monkeypatch, transport):
    captured = asyncio.run(exercise(tmp_path, monkeypatch, transport=transport,
        supplied=summary() if transport == "desktop" else None))
    assert [call["stage"] for call in captured["http_calls"]] == ["qq_gate", "character_expression"]


def test_critical_state_fences_replay_without_new_model_calls(tmp_path, monkeypatch):
    captured = asyncio.run(exercise(tmp_path, monkeypatch, replay=True, replay_critical=True))
    assert captured["replay_detail"] == {
        "error_type": "qq_turn_rejected", "reason": "critical_homeostasis_before_effect"}
    assert [call["stage"] for call in captured["http_calls"]] == ["qq_gate", "character_expression"]
    final = captured["http_calls"][-1]["payload"]
    assert final["model"] == "synthetic-expression-model"
    assert "合成场景" in json.dumps(final["messages"], ensure_ascii=False)
    assert final["messages"][0]["role"] == "system"
    assert [item["role"] for item in final["messages"][:4]] == ["system", "user", "user", "assistant"]


@pytest.mark.parametrize("mode", ["responses", "full"])
@pytest.mark.parametrize("channel", ["direct_group", "direct_private", "desktop"])
def test_runtime_summary_reaches_final_expression_with_same_revision(tmp_path, monkeypatch, mode, channel):
    from harness.compiler.compiler import render_homeostatic_interoception

    captured = asyncio.run(exercise(tmp_path, monkeypatch, mode=mode,
        transport="desktop" if channel == "desktop" else "onebot", private=channel == "direct_private",
        supplied=summary(snapshot_revision=1, profile_ref="spoofed@1")))
    expected = render_homeostatic_interoception(summary())
    command = captured["runtime_command"]
    assert command["payload"]["homeostatic_interoception"] == summary().model_dump(mode="json")
    assert command["version_vector"]["homeostasis_state"] == 7
    gate, final = captured["http_calls"]
    gate_context = decision.combined_user_payload(gate["payload"]["messages"])
    persona_text = json.dumps(final["payload"]["messages"], ensure_ascii=False)
    assert expected in persona_text
    assert gate_context["homeostatic_interoception"]["response"] == expected
    assert final["payload"]["messages"][:3] == gate["payload"]["messages"]
    visible = json.dumps([gate["payload"]["messages"], final["payload"]["messages"]], ensure_ascii=False)
    for raw in ("spoofed@1", "normalized_deviation", "snapshot_revision", "synthetic-expression-episode"):
        assert raw not in visible
    assert gate["trace_context"]["phase"] == "qq_gate"
    assert final["trace_context"]["phase"] == "qq_persona"
    assert final["trace_context"]["command_id"] == command["command_id"]
    assert final["trace_context"]["correlation_id"] == command["correlation_id"]
    assert final["trace_context"]["stage"] == "generate"
    http = [event for event in captured["diagnostics"] if event["record_type"] == "http_request_prepared"]
    assert len(http) == 2
    metadata = http[-1]["diagnostic_context"]
    assert metadata["channel"] == "qq" and metadata["homeostasis_revision"] == 7
    assert metadata["profile_revision"] == 3
    assert metadata["conversation_kind"] == ("private" if channel == "direct_private" else "group")
    assert metadata["summary_hash"] == hashlib.sha256(json.dumps(summary().model_dump(mode="json"),
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert metadata["revision"] == 0
    compiled = next(item["compiled_context"] for item in captured["diagnostics"] if "compiled_context" in item)
    segment = next(item for item in compiled["segments"] if item["kind"] == "homeostatic_interoception")
    assert segment["text"] == expected and "homeostasis:7" in segment["source_ids"]


@pytest.mark.parametrize("mode", ["off", "shadow"])
@pytest.mark.parametrize("transport", ["onebot", "desktop"])
def test_disabled_expression_strips_body_supplied_summary(tmp_path, monkeypatch, mode, transport):
    captured = asyncio.run(exercise(tmp_path, monkeypatch, mode=mode, transport=transport, supplied=summary()))
    assert captured["runtime_command"]["payload"]["homeostatic_interoception"] is None
    for call in captured["http_calls"]:
        visible = json.dumps(call["payload"]["messages"], ensure_ascii=False)
        assert "homeostatic_interoception" not in visible
        assert "这是角色自身的内在感受线索" not in visible


@pytest.mark.parametrize("change", [{"salience": "critical"}, {"profile_ref": "incorrect@3"}])
def test_untrusted_or_critical_runtime_projection_stops_before_any_model(tmp_path, monkeypatch, change):
    from harness.runtime.supervisor import DomainCommandError

    monkeypatch.setattr(OpenAICompatClient, "complete_chat_result",
                        lambda *args, **kwargs: pytest.fail("invalid projection reached a model"))
    with pytest.raises(DomainCommandError):
        asyncio.run(exercise(tmp_path, monkeypatch, trusted=summary(**change)))


def test_accepted_command_replay_reuses_frozen_summary_after_state_changes(tmp_path, monkeypatch):
    captured = asyncio.run(exercise(tmp_path, monkeypatch, replay=True))
    assert captured["runtime_command"]["payload"]["homeostatic_interoception"]["snapshot_revision"] == 7
    assert len(captured["http_calls"]) == 2


def bound_idle(value=None):
    from tests.test_qq_idle_decision import current_intent

    value = value or summary()
    return current_intent(homeostatic_interoception=value, homeostasis_revision=value.snapshot_revision,
                          episode_signature=value.episode_signature)


def test_matching_idle_summary_is_preserved_and_replayable(tmp_path, monkeypatch):
    captured = asyncio.run(exercise(tmp_path, monkeypatch, transport="desktop", idle_intent=bound_idle(),
                                   supplied=summary(), replay=True))
    payload = captured["runtime_command"]["payload"]
    assert payload["idle_intent"]["homeostatic_interoception"] == payload["homeostatic_interoception"]


@pytest.mark.parametrize("mode,stale", [("off", False), ("shadow", False), ("responses", True)])
def test_disabled_or_stale_idle_cannot_be_rebound_into_a_fresh_opportunity(tmp_path, monkeypatch, mode, stale):
    from harness.runtime.supervisor import DomainCommandError

    value = summary(snapshot_revision=6) if stale else summary()
    monkeypatch.setattr(OpenAICompatClient, "complete_chat_result",
                        lambda *args, **kwargs: pytest.fail("invalid idle reached a model"))
    with pytest.raises(DomainCommandError, match="QQ idle"):
        asyncio.run(exercise(tmp_path, monkeypatch, transport="desktop", mode=mode,
                            idle_intent=bound_idle(value), supplied=value))


def test_baseline_proves_original_transport_difference_before_fix():
    from harness.compiler.compiler import render_homeostatic_interoception

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    direct, desktop = baseline["captures"]
    expected = render_homeostatic_interoception(summary())
    assert baseline["label"] == "before_wiring_fix" and not baseline["live_model"]
    assert direct["runtime_command"]["payload"]["homeostatic_interoception"] is None
    for capture in (direct, desktop):
        assert expected not in capture["http_calls"][-1]["payload"]["messages"][0]["content"]
    assert expected in desktop["http_calls"][-1]["payload"]["messages"][1]["content"]


def export_evidence(destination, *, label):
    """Explicit developer utility; normal pytest runs never update evidence."""
    from tempfile import TemporaryDirectory

    captures = []
    for transport in ("onebot", "desktop"):
        with TemporaryDirectory(prefix="nc-expression-") as folder, pytest.MonkeyPatch.context() as patch:
            captures.append(asyncio.run(exercise(Path(folder), patch, transport=transport,
                supplied=summary() if transport == "desktop" else None)))
    payload = {"schema_version": "qq-expression-synthetic-http-evidence-v1", "label": label,
        "synthetic": True, "live_model": False, "release_eligible": False,
        "limits": ["Synthetic QQ source and scripted HTTP response; no actual QQ delivery or model-quality claim.",
                   "Runtime/Cognitive command path and final OpenAI-compatible HTTP payload are exercised."],
        "source_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
            "harness/runtime/supervisor.py", "harness/qq/decision.py", "harness/cognitive/service.py")},
        "captures": captures}
    Path(destination).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
