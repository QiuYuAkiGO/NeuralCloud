"""Protocol attribution and image recall share Misty's retained QQ authority."""

from datetime import timedelta
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from harness.memory.shared import SharedMemoryStore, SharedReadScope
from harness.qq import decision
from harness.qq.context_budget import project_group_context
from harness.qq.memory import (
    QQImageAnalysis, QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput,
    protocol_message_evidence_ref,
)
from harness.qq.shared import QQSharedCognitiveClient, QQSharedPrepareRequest
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from harness.runtime.qq_turns import _source
from tests.test_qq_decision import NOW, isolated_static_parser_cache as isolated_static_parser_cache, setup as setup
from tests.test_qq_decision_segments import plan
from tests import test_qq_session as session_fixtures
from tests.test_qq_session import client as session_client


session_setup = session_fixtures.setup


EVIDENCE = "qq-protocol://sha256/" + "a" * 64
IMAGE = "qq-direct-image:sha256:" + "b" * 64


@pytest.mark.parametrize("member,message", [(123, "1"), ("0", "1"), ("20003", 123),
                                            ("20003", ""), ("20003", "1\n"), ("20003", "x")])
def test_reused_protocol_id_validators_remain_strict(member, message):
    with pytest.raises(ValueError):
        protocol_message_evidence_ref("10001", member, message)


def protocol_profile(store, *, group="10001", member="20003"):
    incoming = QQVerifiedProfileInput(group_id=group, member_id=member, display_name="协议成员",
        display_name_source="qq_metadata", verification="qq_protocol_verified", capture_id="protocol-profile:1",
        observed_at=NOW, group_evidence=[EVIDENCE], member_evidence=[EVIDENCE])
    return incoming, store.record_protocol_profile(incoming).profile


def protocol_observation(profile, **updates):
    return QQVisualObservationInput(**{
        "group_id": profile.group_id, "member_id": profile.member_id, "profile_record_id": profile.record_id,
        "capture_id": "protocol-event:1", "capture_item_id": "protocol-item:1", "observed_at": NOW,
        "text": "看看这个 [图片 1]", "evidence": [EVIDENCE, IMAGE], "author_attribution": "qq_protocol_verified",
        "attribution_evidence": [EVIDENCE, protocol_message_evidence_ref(profile.group_id, profile.member_id, "-123")],
        "message_id": "-123", "message_id_source": "qq_metadata", "image_analysis": [{"image_index": 1,
            "status": "described", "evidence_ref": IMAGE, "description": "MODEL_IMAGE_DESCRIPTION",
            "ocr_text": "MODEL_IMAGE_OCR", "uncertainties": ["MODEL_IMAGE_UNCERTAINTY"]}], **updates})


def test_protocol_records_preserve_pixels_and_shared_scope_without_ui_attribution(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "qq.sqlite3")
    incoming, profile = protocol_profile(store)
    observation = protocol_observation(profile)
    assert store.record_protocol_observation(observation).created
    assert not store.record_protocol_observation(observation).created
    with pytest.raises(ValueError, match="UI collection"):
        store.record_profile(incoming)
    with pytest.raises(ValueError, match="UI collection"):
        store.record_observation(observation)
    context = store.group_context(group_id="10001")
    projected = project_group_context(context.model_dump(mode="json"))
    assert projected["members"][0]["observations"][0]["image_analysis"][0]["authority"] == "model_inference"
    shared = SharedMemoryStore(tmp_path / "shared.sqlite3")
    result = shared.import_qq_database(store.path, allowed_group_ids=["10001"], self_member_id="29999")
    assert result.success
    records = shared.query(SharedReadScope(audience="local:main", kinds=("qq_observation",))).records
    assert len(records) == 1 and records[0].text == observation.text
    assert records[0].image_analysis[0].description == "MODEL_IMAGE_DESCRIPTION"
    assert records[0].model_projection()["image_analysis"][0]["authority"] == "model_inference"
    assert json.loads(records[0].source.original_json)["author_attribution"] == "qq_protocol_verified"
    assert not shared.query(SharedReadScope(audience="qq:group:1060979317")).records
    assert shared.import_qq_database(store.path, allowed_group_ids=["10001"], self_member_id="29999").success


@pytest.mark.parametrize("updates", [
    {"message_id": None, "message_id_source": None}, {"message_id_source": "qq_visible_identifiers"},
    {"attribution_evidence": [EVIDENCE]}, {"evidence": [EVIDENCE]},
    {"claims": [{"statement": "assertion", "supporting_quote": "MODEL_IMAGE_DESCRIPTION"}]},
])
def test_protocol_observation_requires_real_message_binding_and_never_quotes_model_output(tmp_path, updates):
    _, profile = protocol_profile(QQVisualMemoryStore(tmp_path / "qq.sqlite3"))
    with pytest.raises(ValueError):
        protocol_observation(profile, **updates)


@pytest.mark.parametrize("updates", [
    {"image_index": 0}, {"image_index": 9}, {"status": "unknown", "description": "guess"},
    {"status": "described", "description": "guess"},
    {"status": "unknown", "error": "private exception content"},
    {"status": "unknown", "uncertainties": ["a"] * 17},
])
def test_image_interpretation_is_bounded_and_unknown_is_not_a_description(updates):
    with pytest.raises(ValueError):
        QQImageAnalysis.model_validate({"image_index": 1, "status": "unknown", **updates})


def test_image_description_reaches_existing_gate_and_persona_as_data_only(setup):
    store, llm, _, kwargs, _ = setup
    _, profile = protocol_profile(store)
    observation = store.record_protocol_observation(protocol_observation(profile)).observation
    result = decision.decide_qq_reply(**kwargs, trigger_member_id=profile.member_id)
    assert observation.record_id in result.source_record_ids and result.action == "speak"
    for call in (llm.gate_messages[0], llm.chat_calls[0]):
        user = "\n".join(item["content"] for item in call if item["role"] == "user")
        system = "\n".join(item["content"] for item in call if item["role"] == "system")
        assert "MODEL_IMAGE_DESCRIPTION" in user and "MODEL_IMAGE_OCR" in user
        assert "MODEL_IMAGE_UNCERTAINTY" in user and '"authority":"model_inference"' in user
        assert "MODEL_IMAGE_DESCRIPTION" not in system
        assert "不是作者原话" in system
    assert observation.text == "看看这个 [图片 1]"


def protocol_segment(setup, result, index, message_id):
    return decision.record_qq_protocol_segment_delivery(memory_db=setup[0].path, group_id="10001",
        self_member_id="29999", decision_id=result.decision_id, segment_index=index,
        delivered_text=result.reply_segments[index], protocol_message_id=message_id,
        evidence_refs=[EVIDENCE, protocol_message_evidence_ref("10001", "29999", message_id)],
        delivered_at=NOW + timedelta(seconds=index))


def test_protocol_segments_keep_real_message_id_prefix_and_import_to_same_memory(setup, tmp_path):
    result = plan(setup, count=2)
    first = protocol_segment(setup, result, 0, "500")
    assert protocol_segment(setup, result, 0, "500") == first
    with pytest.raises(ValueError):
        protocol_segment(setup, result, 1, "500")
    second = protocol_segment(setup, result, 1, "501")
    refs = sorted(set(first.evidence_refs + second.evidence_refs))
    options = dict(memory_db=setup[0].path, group_id="10001", self_member_id="29999",
        decision_id=result.decision_id, delivered_text=result.reply, protocol_message_ids=["500", "501"],
        evidence_refs=refs, delivered_at=NOW + timedelta(seconds=2))
    receipt = decision.record_qq_protocol_delivery(**options)
    assert receipt.verification == "qq_protocol_verified" and receipt.segment_receipt_ids == [first.receipt_id, second.receipt_id]
    assert decision.get_qq_delivery(memory_db=setup[0].path, group_id="10001", decision_id=result.decision_id) == receipt
    with pytest.raises(ValueError):
        decision.record_qq_protocol_delivery(**{**options, "protocol_message_ids": ["501", "500"]})
    shared = SharedMemoryStore(tmp_path / "shared.sqlite3")
    assert shared.import_qq_database(setup[0].path, allowed_group_ids=["10001"], self_member_id="29999").success
    visible = shared.query(SharedReadScope(audience="local:main", kinds=("qq_delivery",))).records
    assert len(visible) == 1 and json.loads(visible[0].source.original_json)["protocol_message_ids"] == ["500", "501"]


def test_ui_receipt_entrypoint_refuses_protocol_claim_and_protocol_requires_message_evidence(setup):
    result = plan(setup, count=1)
    options = dict(memory_db=setup[0].path, group_id="10001", self_member_id="29999",
        decision_id=result.decision_id, delivered_text=result.reply, evidence_refs=[EVIDENCE])
    with pytest.raises(ValueError):
        decision.record_qq_delivery(**options, verification="qq_protocol_verified")
    with pytest.raises(ValueError):
        decision.record_qq_protocol_delivery(**options, protocol_message_ids=["500"])


def test_session_ui_routes_reject_protocol_inputs(session_setup):
    settings, store, _, _, gate = session_setup
    incoming, profile = protocol_profile(store)
    client = session_client(settings, gate)
    assert client.post("/v1/profiles", json=incoming.model_dump(mode="json")).status_code == 422
    assert client.post("/v1/observations", json=protocol_observation(profile).model_dump(mode="json")).status_code == 422


def shared_request(**updates):
    return QQSharedPrepareRequest(group_id="10001", self_member_id="29999", deadline_at=NOW + timedelta(seconds=60),
        trace={"trace_id": "qq-trace:" + "1" * 32, "event_id": "qq-watch-event:" + "2" * 32,
            "first_seen_at": NOW, "first_seen_mono": 0}, **updates)


def test_onebot_ingress_delegates_only_to_native_source_authority(monkeypatch):
    calls = []
    module = SimpleNamespace(validate_direct_source=lambda config, request, **kw: calls.append((config, request, kw)))
    monkeypatch.setitem(sys.modules, "harness.qq.direct_source", module)
    request = shared_request(transport="onebot")
    config = object()
    _source(config, None, request, completed_replay=True)
    assert calls == [(config, request, {"completed_replay": True})]
    assert shared_request().transport == "desktop"


def test_shared_request_aliases_must_match_transport_and_original_deadline():
    alias = dict(group_id="10001", self_member_id="29999", alias="29999", evidence_refs=[EVIDENCE],
        verification="qq_protocol_verified")
    assert shared_request(transport="onebot", verified_self_aliases=[alias]).verified_self_aliases[0].alias == "29999"
    with pytest.raises(ValueError, match="transport"):
        shared_request(verified_self_aliases=[alias])
    raw = shared_request(transport="onebot").model_dump()
    raw["deadline_at"] = NOW + timedelta(seconds=61)
    with pytest.raises(ValueError, match="deadline_extended"):
        QQSharedPrepareRequest.model_validate(raw)


def test_shared_client_routes_protocol_turn_to_existing_runtime(tmp_path):
    token = tmp_path / "token"
    token.write_text("test-local-token")
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        trace = payload["trace"]
        result = decision.QQReplyDecision(decision_id="qq-decision:onebot-test", input_hash="test",
            created_at=trace["first_seen_at"], action="wait", reason="等一会。", decision_source="model",
            outcome="natural_wait", group_id=payload["group_id"], self_member_id=payload["self_member_id"],
            participants=[], source_record_ids=[], new_observation_record_ids=[], evidence_refs=[], delivery_refs=[],
            model="synthetic", persona_version="shared-misty", preset_id="shared-misty", trace_id=trace["trace_id"])
        return httpx.Response(200, json={"decision": result.model_dump(mode="json"), "trace": {
            "trace_id": trace["trace_id"], "elapsed_ms": 0, "stages": [], "counters": {}}})

    with httpx.Client(base_url="http://127.0.0.1:8770", transport=httpx.MockTransport(respond)) as client:
        shared = QQSharedCognitiveClient("http://127.0.0.1:8770", token, client=client)
        result = shared(transport="onebot", group_id="10001", self_member_id="29999",
            deadline=QQDeadline.after(60), trace=QQTurnTrace(event_id="qq-watch-event:" + "3" * 32))
    assert result.preset_id == "shared-misty" and calls[0]["transport"] == "onebot"
