from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from pydantic import ValidationError

from harness.cognitive.basic_terminal import (
    BasicTerminalActionPlanner,
    DEFAULT_TERMINAL_APP_ID,
    GUI_TERMINAL_INTENTS,
    normalize_terminal_observation,
    terminal_command_text,
    terminal_completion_marker,
    terminal_start_marker,
)
from harness.contracts.decisions import DeferDecision, GoalProposal, ReplyDecision
from harness.embodiment.providers.mistyai import (
    MistyAICapabilityProvider,
    MistyAIClient,
    MistyAIError,
    MistyAIPerceptionBackend,
)
from harness.research.manifest import BasicTerminalCondition, ExperimentManifest
from tests.test_embodiment_providers import NOW, _misty_handler
from tests.test_runtime_supervisor import _manifest


APP = DEFAULT_TERMINAL_APP_ID
REF = "evidence://sha256/" + "a" * 64
TASK = "terminal-task-1"
REQUEST = "请打开 Linux 终端执行 pwd，并核验实际输出。"


class Model:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def complete_chat(self, messages, **kwargs):
        self.calls.append(messages)
        return json.dumps(self.value)


def observation():
    window = {"app_id": APP, "window_id": "window-1", "focused": True}
    return {
        "observation_id": "obs-1",
        "active_window": window,
        "windows": [window],
        "a11y_nodes": [{"app_id": APP, "role": "terminal", "name": "Terminal"}],
        "terminal_text": "",
        "terminal_text_evidence_ref": REF,
        "evidence_refs": [REF],
    }


def act(intent_id, arguments, criteria=None):
    return {
        "kind": "act",
        "intent": {"intent_id": intent_id, "arguments": arguments},
        "success_criteria": criteria or [{"kind": "window", "app_id": APP, "condition": "present_and_focused"}],
    }


def type_action():
    return act(
        "ui.type_text.v1",
        {"selector": {"observation_id": "obs-1", "app_id": APP, "role": "terminal", "name": "Terminal"}, "text": "pwd"},
        [{"kind": "a11y_node", "condition": "present", "app_id": APP, "role": "terminal", "name": "Terminal"}],
    )


def history():
    return [
        {
            "intent_id": "ui.type_text.v1",
            "arguments": {"text": terminal_command_text("pwd", TASK)},
            "state": "succeeded",
            "evidence_refs": [REF],
        },
        {
            "intent_id": "ui.key_chord.v1",
            "arguments": {"keys": ["enter"]},
            "state": "succeeded",
            "evidence_refs": [REF],
        },
    ]


def decide(value, **kwargs):
    model = Model(value)
    options = {
        "text": REQUEST,
        "provider_intents": GUI_TERMINAL_INTENTS,
        "permitted_intents": GUI_TERMINAL_INTENTS,
        "terminal_app_id": APP,
        "observation": observation(),
        "task_id": TASK,
    }
    options.update(kwargs)
    return BasicTerminalActionPlanner(model).decide(**options), model


@pytest.mark.parametrize("text", ["你好", "晚上好", "终端是什么？", "不要打开终端", "Tell me about the terminal"])
def test_greetings_and_discussion_never_request_model_actions(text):
    result, model = decide(act("app.open.v1", {"app_id": APP}), text=text)
    assert result is None
    assert model.calls == []


def test_model_selects_gui_goal_without_homeostasis_policy_or_security_control():
    result, model = decide(act("app.open.v1", {"app_id": APP}))
    assert isinstance(result, GoalProposal)
    assert result.embodied_action.intent.intent_id == "app.open.v1"
    assert result.permission_class == "low"
    menu = json.loads(model.calls[0][0]["content"])["available_intents"]
    assert "shell.run.v1" not in {item["intent_id"] for item in menu}


def test_command_is_only_gui_text_with_task_exit_marker_and_preserved_permissions():
    result, _ = decide(type_action())
    assert isinstance(result, GoalProposal)
    assert result.embodied_action.intent.intent_id == "ui.type_text.v1"
    assert result.embodied_action.intent.arguments["text"] == terminal_command_text("pwd", TASK)
    assert result.permission_class == "medium" and result.required_approvals == ["researcher"]
    assert not result.reversible


@pytest.mark.parametrize("change", ["permission", "shell", "other_app", "stale", "protected", "newline", "repeat"])
def test_model_cannot_expand_permissions_capabilities_or_targets(change):
    value = type_action()
    options = {}
    if change == "permission":
        value["permission_class"] = "low"
    elif change == "shell":
        value["intent"] = {"intent_id": "shell.run.v1", "arguments": {"command_id": "pwd"}}
    elif change == "other_app":
        value = act("app.open.v1", {"app_id": "unrelated-app"})
    elif change == "stale":
        value["intent"]["arguments"]["selector"]["observation_id"] = "older"
    elif change == "protected":
        options["observation"] = observation()
        options["observation"]["a11y_nodes"][0]["protected"] = True
    elif change == "newline":
        value["intent"]["arguments"]["text"] = "pwd\n"
    elif change == "repeat":
        options["history"] = history()
    result, _ = decide(value, **options)
    assert isinstance(result, DeferDecision)


def test_capability_missing_fails_closed():
    result, _ = decide(act("app.open.v1", {"app_id": APP}), provider_intents=["control.observe.v1"])
    assert isinstance(result, DeferDecision)


@pytest.mark.parametrize("state", ["failed", "unknown", "cancelled"])
def test_failed_history_stops_without_model_retry(state):
    result, model = decide(type_action(), history=[{"state": state}], step=1)
    assert isinstance(result, DeferDecision) and not model.calls


def test_budget_exhaustion_stops_without_call():
    result, model = decide(type_action(), step=12, max_steps=12)
    assert isinstance(result, DeferDecision) and not model.calls


def test_enter_requires_verified_input_and_focused_terminal():
    value = act("ui.key_chord.v1", {"keys": ["enter"]})
    denied, _ = decide(value)
    accepted, _ = decide(value, history=history()[:1], step=1)
    assert isinstance(denied, DeferDecision)
    assert isinstance(accepted, GoalProposal)


def test_complete_requires_actual_exit_marker_and_exact_a11y_output_evidence():
    value = {"kind": "complete", "observed_text": "/home/misty-body", "evidence_ref": REF}
    observed = observation()
    observed["terminal_text"] = (
        terminal_start_marker(TASK) + "\n/home/misty-body\n" + terminal_completion_marker(TASK) + ":0\n"
    )
    result, _ = decide(value, history=history(), observation=observed, step=3)
    assert isinstance(result, ReplyDecision) and "退出码 0" in result.text
    observed["terminal_text"] = terminal_command_text("pwd", TASK)
    denied, _ = decide(value, history=history(), observation=observed, step=3)
    assert isinstance(denied, DeferDecision)
    observed["terminal_text"] = (
        terminal_start_marker(TASK) + "\n/home/misty-body\n" + terminal_completion_marker(TASK) + ":0\n"
    )
    observed["terminal_text_evidence_ref"] = "other-proof"
    denied, _ = decide(value, history=history(), observation=observed, step=3)
    assert isinstance(denied, DeferDecision)


def test_nonzero_exit_is_reported_as_failure_not_success():
    observed = observation()
    observed["terminal_text"] = (
        terminal_start_marker(TASK) + "\ncommand failed\n" + terminal_completion_marker(TASK) + ":2\n"
    )
    result, _ = decide(
        {"kind": "complete", "observed_text": "command failed", "evidence_ref": REF},
        history=history(),
        observation=observed,
        step=3,
    )
    assert isinstance(result, ReplyDecision)
    assert "退出码为 2" in result.text and "已执行完成" not in result.text


@pytest.mark.parametrize(
    "missing", ["enter", "typed_evidence", "enter_evidence", "reversed", "quote", "begin", "old_output"]
)
def test_completion_cannot_invent_submission_or_reuse_unrelated_output(missing):
    observed = observation()
    observed["terminal_text"] = (
        "earlier output\n"
        + terminal_start_marker(TASK)
        + "\nactual output\n"
        + terminal_completion_marker(TASK)
        + ":0\n"
    )
    trace = history()
    value = {"kind": "complete", "observed_text": "actual output", "evidence_ref": REF}
    if missing == "enter":
        trace = trace[:1]
    elif missing == "typed_evidence":
        trace[0]["evidence_refs"] = []
    elif missing == "enter_evidence":
        trace[1]["evidence_refs"] = []
    elif missing == "reversed":
        trace.reverse()
    elif missing == "quote":
        value["observed_text"] = "fabricated output"
    elif missing == "begin":
        observed["terminal_text"] = observed["terminal_text"].replace(terminal_start_marker(TASK), "wrong marker")
    elif missing == "old_output":
        value["observed_text"] = "earlier output"
    result, _ = decide(value, history=trace, observation=observed, step=3)
    assert isinstance(result, DeferDecision)


def test_verified_command_with_empty_stdout_can_complete_without_inventing_output():
    observed = observation()
    observed["terminal_text"] = terminal_start_marker(TASK) + "\n\n" + terminal_completion_marker(TASK) + ":0\n"
    result, _ = decide(
        {"kind": "complete", "observed_text": "", "evidence_ref": REF}, history=history(), observation=observed, step=3
    )
    assert isinstance(result, ReplyDecision) and "无终端输出" in result.text


def test_selector_supports_actual_atspi_null_window_id_and_rejects_invented_binding():
    value = type_action()
    observed = observation()
    observed["a11y_nodes"][0]["window_id"] = None
    value["intent"]["arguments"]["selector"]["window_id"] = None
    result, _ = decide(value, observation=observed)
    assert isinstance(result, GoalProposal)
    assert result.embodied_action.intent.arguments["selector"]["window_id"] is None
    value["intent"]["arguments"]["selector"]["window_id"] = "window-1"
    result, _ = decide(value, observation=observed)
    assert isinstance(result, DeferDecision)


def test_selector_ambiguity_matches_mistyai_grounder_contract():
    observed = observation()
    observed["a11y_nodes"] *= 2
    result, _ = decide(type_action(), observation=observed)
    assert isinstance(result, DeferDecision)


def test_terminal_manifest_is_opt_in_and_preserves_legacy_hash():
    baseline = _manifest()
    payload = baseline.model_dump(mode="json")
    payload.pop("basic_terminal")
    legacy = ExperimentManifest.model_validate(payload)
    assert not legacy.basic_terminal.enabled
    assert legacy.content_hash() == baseline.content_hash()
    payload["basic_terminal"] = {"enabled": True}
    with pytest.raises(ValidationError, match="basic terminal requires"):
        ExperimentManifest.model_validate(payload)
    payload["embodiment"] = {
        "mode": "observe",
        "body_provider": "mistyai",
        "body_instance_id": "body-test",
        "capability_requirements": [
            {"capability": name, "required": True}
            for name in (
                "action.execute.v1",
                "action.verify.v1",
                "observation.snapshot.v1",
                "evidence.reference.v1",
            )
        ],
    }
    enabled = ExperimentManifest.model_validate(payload)
    assert enabled.basic_terminal.enabled and enabled.content_hash() != legacy.content_hash()
    payload["embodiment"]["capability_requirements"][1]["required"] = False
    with pytest.raises(ValidationError, match="basic terminal requires"):
        ExperimentManifest.model_validate(payload)


@pytest.mark.parametrize("intents", [("shell.run.v1",), ("file.create_text.v1",), (), ("app.open.v1", "app.open.v1")])
def test_terminal_manifest_never_expands_to_shell_or_file_capabilities(intents):
    with pytest.raises(ValidationError):
        BasicTerminalCondition(enabled=True, permitted_intents=intents)


def test_projection_uses_text_interface_never_terminal_title_or_protected_text():
    desktop = {
        "observation_id": "obs-1",
        "a11y_ref": REF,
        "a11y_tree": {
            "nodes": [
                {"app_id": APP, "role": "terminal", "name": "Title only"},
                {"app_id": APP, "role": "terminal", "text": "real output"},
                {"app_id": APP, "role": "password text", "text": "redacted"},
            ]
        },
    }
    result = normalize_terminal_observation({"extensions": {"mistyai.desktop.v1": desktop}})
    assert result["terminal_text"] == "real output"
    assert result["terminal_text_evidence_ref"] == REF
    assert "redacted" not in json.dumps(result)
    desktop["a11y_tree"]["nodes"] = desktop["a11y_tree"]["nodes"][:1]
    assert not normalize_terminal_observation(desktop)["terminal_text"]


def test_provider_reads_hash_verified_a11y_text_and_publishes_native_skill_set():
    tree = {
        "schema_version": "a11y-tree-v1",
        "nodes": [
            {"app_id": APP, "role": "terminal", "name": "Terminal", "text": "actual output"},
            {"app_id": APP, "role": "password text", "text": "redacted"},
        ],
    }
    content = json.dumps(tree).encode()
    digest = hashlib.sha256(content).hexdigest()
    reference = "evidence://sha256/" + digest

    def handler(request):
        if request.url.path == f"/v1/evidence/{digest}":
            return httpx.Response(200, content=content, headers={"content-type": "application/vnd.mistyai.a11y+json"})
        response = _misty_handler(request)
        if request.url.path == "/v1/observations:capture":
            payload = response.json()
            payload["a11y_ref"] = reference
            return httpx.Response(200, json=payload)
        return response

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://fixture") as transport:
            client = MistyAIClient("http://fixture", token="test", client=transport)
            capabilities = MistyAICapabilityProvider(client)
            assert capabilities.supported_intents() == frozenset()
            await capabilities.capabilities()
            assert capabilities.supported_intents() == frozenset({"control.wait.v1"})
            snapshot = await MistyAIPerceptionBackend(client).observe(now=NOW)
            projected = normalize_terminal_observation(snapshot)
            assert projected["terminal_text"] == "actual output"
            assert projected["terminal_text_evidence_ref"] == reference
            assert "redacted" not in snapshot.model_dump_json()

    asyncio.run(run())


def test_provider_rejects_tampered_a11y_evidence():
    def handler(request):
        response = _misty_handler(request)
        if request.url.path == "/v1/observations:capture":
            payload = response.json()
            payload["a11y_ref"] = REF
            return httpx.Response(200, json=payload)
        if request.url.path.endswith("a" * 64):
            return httpx.Response(
                200, content=b"tampered", headers={"content-type": "application/vnd.mistyai.a11y+json"}
            )
        return response

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://fixture") as transport:
            client = MistyAIClient("http://fixture", token="test", client=transport)
            with pytest.raises(MistyAIError, match="SHA-256"):
                await MistyAIPerceptionBackend(client).observe(now=NOW)

    asyncio.run(run())
