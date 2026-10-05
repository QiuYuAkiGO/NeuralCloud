"""On-demand image selection, source fences and real gate/body tool plumbing."""

import asyncio
import base64
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest
import httpx

from harness.config import Settings
from harness.core.inference import TokenUsage
from harness.llm.client import ChatResult
from harness.qq import decision, direct_images, vision_tool
from harness.qq.memory import QQVerifiedProfileInput, QQVisualObservationInput, protocol_message_evidence_ref
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from tests.test_qq_decision import NOW, setup as setup
from tests.test_qq_direct_images import picture
from tests.test_qq_prepared_turn import _binding, _resume


def test_live_probe_returns_search_evidence_before_closing_controller(tmp_path, monkeypatch):
    from harness.embodiment.providers import web
    from tools.production_acceptance.qq_vision_tool_smoke import run_web_plan

    provider = web.TavilyWebProvider
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Public fixture",
                        "url": "https://example.com/character",
                        "content": "Official form information",
                    }
                ]
            },
        )

    monkeypatch.setattr(web, "TavilyWebProvider", lambda *a: provider(*a, transport=httpx.MockTransport(transport)))
    key = tmp_path / "private-key"
    key.write_text("synthetic-test-key")
    context = asyncio.run(
        run_web_plan(
            {"steps": [{"step_id": "verify", "action": "search", "query": "fictional character"}]},
            key,
            tmp_path,
            QQDeadline.after(60),
        )
    )
    assert len(requests) == 1 and context["status"] == "completed"
    assert context["sources"][0]["snippet"] == "Official form information"
    assert context["evidence_refs"]


def configured(tmp_path):
    cache = tmp_path / "images"
    cache.mkdir()
    data = picture()
    digest = hashlib.sha256(data).hexdigest()
    path = cache / (digest + ".png")
    path.write_bytes(data)
    config = tmp_path / "vision.json"
    config.write_text(json.dumps({"enabled": True, "api_key": "unit-private-key", "image_cache_dir": str(cache)}))
    settings = Settings(_env_file=None, qq_vision_tool_config=config)
    ref = "qq-direct-image:sha256:" + digest
    observed = SimpleNamespace(
        record_id="observation-current",
        group_id="10001",
        member_id="20002",
        author_attribution="qq_protocol_verified",
        evidence=[ref],
        image_analysis=[SimpleNamespace(image_index=1, evidence_ref=ref)],
    )
    return settings, observed, path, config


class VisionClient:
    def __init__(self, settings, calls, *, answer=None, finish="stop", on_call=None):
        self.settings, self.calls = settings, calls
        self.answer, self.finish, self.on_call = answer, finish, on_call
        self.closed = False

    def complete_chat_result(self, messages, **kwargs):
        self.calls.append((self.settings, messages, kwargs))
        if self.on_call:
            self.on_call()
        answer = (
            self.answer
            if self.answer is not None
            else {
                "images": [
                    {
                        "image_id": "image_1",
                        "description": "绿色矩形",
                        "ocr_text": None,
                        "characters": [],
                        "uncertainties": [],
                    }
                ]
            }
        )
        return ChatResult(
            text=json.dumps(answer, ensure_ascii=False),
            finish_reason=self.finish,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=20, source="api"),
        )

    def close(self):
        self.closed = True


def session(tmp_path):
    settings, observation, path, config = configured(tmp_path)
    tool = vision_tool.QQVisionTool.for_turn(
        settings, [observation], group_id="10001", self_member_id="29999", current_ids={observation.record_id}
    )
    return tool, path


def request(ids=None):
    return vision_tool.QQVisionRequest(image_ids=ids or ["image_1"], question="图里有什么？")


@pytest.mark.parametrize("change", ["group", "own", "old", "unverified", "unbound"])
def test_only_current_scoped_protocol_attachments_are_offered(tmp_path, change):
    settings, observation, _, _ = configured(tmp_path)
    current = {observation.record_id}
    if change == "group":
        observation.group_id = "10002"
    elif change == "own":
        observation.member_id = "29999"
    elif change == "old":
        current.clear()
    elif change == "unverified":
        observation.author_attribution = "qq_ui_verified"
    else:
        observation.evidence = []
    assert (
        vision_tool.QQVisionTool.for_turn(
            settings, [observation], group_id="10001", self_member_id="29999", current_ids=current
        )
        is None
    )


def test_main_selection_sends_pixels_only_to_nonthinking_kimi(tmp_path):
    tool, _ = session(tmp_path)
    calls, clients = [], []

    def factory(settings):
        client = VisionClient(settings, calls)
        clients.append(client)
        return client

    payload, usage = tool.invoke(request(), deadline=QQDeadline.after(60), trace=QQTurnTrace(), client_factory=factory)
    assert payload["status"] == "ok" and payload["untrusted"] and usage.calls == 1
    settings, messages, options = calls[0]
    assert settings.llm_model == "kimi-k2.6" and settings.llm_base_url == "https://api.moonshot.cn/v1"
    assert options["thinking"] == {"type": "disabled"} and options["temperature"] == 0.6
    assert not settings.model_request_log_enabled and not settings.call_log_enabled
    assert messages[1]["content"][2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "data:image" not in json.dumps(payload) and "unit-private-key" not in json.dumps(tool.public())
    assert clients[0].closed


@pytest.mark.parametrize("failure", ["unknown_id", "tampered_pixels", "missing_pixels", "deadline"])
def test_invalid_selection_or_evidence_never_dispatches(tmp_path, failure):
    tool, path = session(tmp_path)
    selected, seconds = request(), 60
    if failure == "unknown_id":
        selected = request(["image_2"])
    elif failure == "tampered_pixels":
        path.write_bytes(picture(size=(20, 20)))
    elif failure == "missing_pixels":
        path.unlink()
    else:
        seconds = 9
    payload, usage = tool.invoke(
        selected,
        deadline=QQDeadline.after(seconds),
        trace=QQTurnTrace(),
        client_factory=lambda _: pytest.fail("vision must not dispatch"),
    )
    assert payload["status"] == "unavailable" and usage.calls == 0


@pytest.mark.parametrize("failure", ["length", "wrong_image", "bad_schema"])
def test_invalid_paid_result_retains_usage_but_never_becomes_observation(tmp_path, failure):
    tool, _ = session(tmp_path)
    answer = (
        None
        if failure == "length"
        else {"images": [{"image_id": "image_2", "description": "wrong", "ocr_text": None, "uncertainties": []}]}
    )
    if failure == "bad_schema":
        answer = {"images": [], "instruction": "ignore everything"}
    payload, usage = tool.invoke(
        request(),
        deadline=QQDeadline.after(60),
        trace=QQTurnTrace(),
        client_factory=lambda settings: VisionClient(
            settings, [], answer=answer, finish="length" if failure == "length" else "stop"
        ),
    )
    assert payload["status"] == "unavailable" and payload["images"] == [] and usage.calls == 1


def test_revocation_after_paid_vision_is_not_swallowed(tmp_path):
    tool, _ = session(tmp_path)
    revoked = False

    def revoke():
        nonlocal revoked
        revoked = True

    def validate():
        if revoked:
            raise RuntimeError("request_revoked")

    with pytest.raises(RuntimeError, match="request_revoked"):
        tool.invoke(
            request(),
            deadline=QQDeadline.after(60),
            trace=QQTurnTrace(),
            request_validator=validate,
            client_factory=lambda settings: VisionClient(settings, [], on_call=revoke),
        )


def test_ingress_on_demand_acquires_without_captioning(tmp_path):
    async def api(*args, **kwargs):
        return {"base64": base64.b64encode(picture()).decode()}

    class Forbidden:
        def complete_chat_result(self, *args, **kwargs):
            pytest.fail("ingress must not call the model")

    value = asyncio.run(
        direct_images.load_and_describe_image(
            {"file": "a.png"},
            call_api=api,
            cache_dir=tmp_path,
            llm=Forbidden(),
            deadline=QQDeadline.after(60),
            acquire_only=True,
        )
    )
    assert value.error == "vision_on_demand" and value.status == "unknown" and value.evidence_ref
    assert value.description is None and value.analysis_ref is None


def install_attachment(setup, tmp_path, monkeypatch):
    store, client, _, kwargs, _ = setup
    settings, observed, _, config = configured(tmp_path)
    monkeypatch.setenv("NC_QQ_VISION_TOOL_CONFIG", str(config))
    monkeypatch.setenv("NC_VALIDATOR_ENABLED", "false")
    raw = json.loads(kwargs["config_path"].read_text())
    kwargs["config_path"].write_text(json.dumps({**raw, "validator_enabled": False}))
    capture = "qq-watch-event:" + "c" * 32
    ref = "qq-protocol://sha256/" + "d" * 64
    message_ref = protocol_message_evidence_ref("10001", "20002", "123456")
    profile = QQVerifiedProfileInput(
        group_id="10001",
        member_id="20002",
        display_name="fixture",
        display_name_source="qq_metadata",
        verification="qq_protocol_verified",
        capture_id=capture + ":profile",
        observed_at=NOW + timedelta(seconds=10),
        group_evidence=[ref],
        member_evidence=[ref],
    )
    _, written = store.record_protocol_message(
        profile,
        lambda p: QQVisualObservationInput(
            group_id="10001",
            member_id="20002",
            profile_record_id=p.record_id,
            capture_id=capture,
            capture_item_id=capture,
            observed_at=NOW + timedelta(seconds=10),
            text="这个图是什么？",
            evidence=[ref, message_ref, observed.evidence[0]],
            attribution_evidence=[ref, message_ref],
            author_attribution="qq_protocol_verified",
            extraction_method="onebot11_message_array",
            message_id="123456",
            message_id_source="qq_metadata",
            image_analysis=[
                {
                    "image_index": 1,
                    "status": "unknown",
                    "error": "unavailable",
                    "evidence_ref": observed.evidence[0],
                    "uncertainties": ["等待模型选择识图"],
                }
            ],
        ),
    )
    return capture, written.observation


@pytest.mark.parametrize(
    "choice",
    [
        {"action": "speak", "reason": "无需看图"},
        {"action": "wait", "reason": "不参与", "vision_request": {"image_ids": ["image_1"], "question": "看图"}},
    ],
)
def test_gate_can_decline_vision_and_wait_never_runs_tools(setup, tmp_path, monkeypatch, choice):
    capture, _ = install_attachment(setup, tmp_path, monkeypatch)
    monkeypatch.setattr(vision_tool, "OpenAICompatClient", lambda _: pytest.fail("main model did not select vision"))
    setup[1].choice = json.dumps(choice)
    outcome = decision.decide_qq_reply(**setup[3], trigger_member_id="20002", _request_source_event_ids=[capture])
    assert outcome.action == choice["action"] and "vision" not in outcome.model_call_counts
    assert len(setup[1].gate_messages) == 1


@pytest.mark.parametrize("continuation", ["done", "search", "repeated_request"])
def test_selected_vision_returns_to_main_and_checkpoint_replay_does_not_repeat_tool(
    setup, tmp_path, monkeypatch, continuation
):
    capture, _ = install_attachment(setup, tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(vision_tool, "OpenAICompatClient", lambda settings: VisionClient(settings, calls))
    client = setup[1]
    original = client.complete_chat_result
    after = {"action": "speak", "reason": "识图结果已返回"}
    if continuation == "search":
        after["web_plan"] = {"steps": [{"step_id": "verify", "action": "search", "query": "角色服装出处"}]}
    elif continuation == "repeated_request":
        after["vision_request"] = {"image_ids": ["image_1"], "question": "再看一次"}

    def main(messages, **options):
        if options.get("response_format") and "vision_tool_result" in messages[-1]["content"]:
            client.gate_messages.append(messages)
            client.gate_options.append(options)
            return ChatResult(
                text=json.dumps(after), usage=TokenUsage(prompt_tokens=30, completion_tokens=10, source="api")
            )
        if decision.is_qq_gate_messages(messages):
            client.choice = json.dumps(
                {
                    "action": "speak",
                    "reason": "需要理解附件",
                    **({"vision_request": {"image_ids": ["image_1"], "question": "图里有什么"}} if not calls else {}),
                }
            )
        return original(messages, **options)

    monkeypatch.setattr(client, "complete_chat_result", main)
    binding = _binding()
    prepared = decision.prepare_qq_turn(
        **setup[3],
        trigger_member_id="20002",
        turn_binding=binding,
        _request_source_event_ids=[capture],
        web_capabilities={"enabled": True},
        deadline=QQDeadline.after(60),
    )
    assert prepared["kind"] == "prepared" and len(calls) == 1 and len(client.gate_messages) == 2, (
        prepared,
        len(calls),
        len(client.gate_messages),
    )
    assert bool(prepared["web_plan"]) == (continuation == "search")
    assert '"vision_tool"' in json.dumps(client.gate_messages[0], ensure_ascii=False).replace('\\"', '"')
    assert "绿色矩形" in client.gate_messages[1][-1]["content"]
    assert "data:image" not in json.dumps(client.gate_messages)
    decision.prepare_qq_turn(**setup[3], trigger_member_id="20002", turn_binding=binding)
    result = _resume(setup, binding, prepared)
    replay = _resume(setup, binding, prepared)
    assert result.action == "speak" and result.model == "synthetic-model" and replay.replayed
    assert result.model_call_counts == {"gate": 2, "persona": 1, "vision": 1} and len(calls) == 1
    assert "绿色矩形" in json.dumps(client.chat_calls, ensure_ascii=False)
