"""Synthetic OneBot wire calls with real inbox, frozen plans and QQ memory."""
from __future__ import annotations

import asyncio
import base64
from contextlib import closing
from datetime import timedelta
import io
import json
from uuid import uuid4

from PIL import Image
import pytest
from websockets.asyncio.server import serve

from harness.llm.client import ChatResult
from harness.memory.shared import SharedMemoryStore, SharedReadScope
from harness.qq import decision, direct
from harness.qq.direct_config import DirectQQSettings
from harness.qq.direct_images import DirectImageObservation
from harness.qq.direct_store import normalize_message, utcnow
from harness.qq.onebot import OneBotClient, OneBotError
from harness.qq.session import QQSessionSettings


SELF, MAIN, GUEST, MEMBER = "29999", "10001", "10002", "20001"


def incoming(number=1, group=MAIN, *, images=False, member=MEMBER, age=0):
    message = [{"type": "text", "data": {"text": "看看这个" if images else "你好"}}]
    if images:
        message.append({"type": "image", "data": {"file": "test-image.png"}})
    return {"post_type": "message", "message_type": "group", "self_id": int(SELF),
        "group_id": int(group), "user_id": int(member), "message_id": number,
        "time": (utcnow() - timedelta(seconds=age)).timestamp(), "message": message,
        "sender": {"user_id": int(member), "nickname": "synthetic member"}}


class ImageModel:
    def __init__(self):
        self.calls = []

    def complete_chat_result(self, messages, **kwargs):
        self.calls.append(messages)
        return ChatResult(text=json.dumps({"description": "绿色矩形", "ocr_text": "测试图片",
                                          "uncertainties": []}))

    def close(self):
        pass


class FrozenCognition:
    def __init__(self):
        self.actor = None
        self.calls = []
        self.plans = []
        self.texts = ["收到。"]
        self.persist = True
        self.transform = None
        self.hook = None

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        group = kwargs["group_id"]
        context = self.actor.memory.group_context(group_id=group)
        ids = [item.record_id for person in context.members for item in person.observations]
        refs = [ref for person in context.members for item in person.observations for ref in item.evidence]
        result = decision.QQReplyDecision(decision_id="qq-decision:" + uuid4().hex,
            input_hash=uuid4().hex, created_at=utcnow(), action="speak", reason="测试回执链",
            reply="\n\n".join(self.texts), reply_segments=self.texts, decision_source="model", outcome="ready",
            group_id=group, self_member_id=SELF, trigger_member_id=kwargs["trigger_member_id"],
            participants=context.participants, source_record_ids=ids, new_observation_record_ids=ids,
            evidence_refs=refs, delivery_refs=[], model="synthetic", persona_version="shared-misty", preset_id="shared-misty")
        if self.persist:
            with closing(decision._connect(self.actor.session.memory_db)) as db, db:
                db.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)", (
                    result.decision_id, result.input_hash, group, "completed", result.model_dump_json()))
        self.plans.append(result)
        if self.hook:
            self.hook()
        return self.transform(result) if self.transform else result

    def close(self):
        pass


class FakeOneBot:
    def __init__(self):
        self.calls = []
        self.sent = {}
        self.send_error = None
        self.receipt_change = None
        self.after_send = None
        self.closed = asyncio.Event()
        self.last_error_type = None
        self.history = []
        self.image_bytes = io.BytesIO()
        Image.new("RGB", (40, 24), "green").save(self.image_bytes, format="PNG")

    async def call(self, action, params=None, timeout=10):
        params = params or {}
        self.calls.append((action, params, timeout))
        if action == "get_login_info":
            return {"user_id": int(SELF), "nickname": "Misty"}
        if action == "get_status":
            return {"online": True, "good": True}
        if action == "get_group_list":
            return [{"group_id": int(group), "group_name": "synthetic group"} for group in (MAIN, GUEST)]
        if action == "get_group_member_info":
            return params | {"nickname": "Misty", "card": ""}
        if action == "get_image":
            return {"base64": base64.b64encode(self.image_bytes.getvalue()).decode("ascii"),
                    "file": "/bridge/synthetic-cache.png"}
        if action == "send_group_msg":
            ident = str(800 + len(self.sent))
            self.sent[ident] = {"message_id": int(ident), "group_id": params["group_id"],
                "sender": {"user_id": int(SELF)}, "message": params["message"]}
            if self.after_send:
                await self.after_send()
            if self.send_error:
                raise self.send_error
            return {"message_id": int(ident)}
        if action == "get_msg":
            result = self.sent[str(params["message_id"])]
            return self.receipt_change(result) if self.receipt_change else result
        if action == "get_group_msg_history":
            return {"messages": self.history}
        raise AssertionError(action)

    async def close(self):
        self.closed.set()


@pytest.fixture
def actor_factory(tmp_path):
    def build(*, guest_send=False):
        root = tmp_path / uuid4().hex
        root.mkdir()
        token = root / "test.token"
        token.write_text("synthetic-token-only-" + "1" * 32)
        session = QQSessionSettings(memory_db=root / "memory.sqlite3", token_file=token,
            allowed_group_ids=[MAIN, GUEST], self_member_id=SELF)
        session_path = root / "session.json"
        session_path.write_text(session.model_dump_json(), encoding="utf-8")
        control = root / "direct.control.json"
        control.write_text('{"paused":false,"stop":false}')
        desktop = root / "watcher.status.json"
        desktop.write_text(json.dumps({"running": False, "heartbeat_at": utcnow().isoformat()}))
        settings = DirectQQSettings(enabled=True, token_file=token, self_member_id=SELF,
            group_roster=[{"group_id": MAIN, "label": "main", "observe": True, "send": True},
                          {"group_id": GUEST, "label": "guest", "observe": True, "send": guest_send}],
            session_config_path=session_path, state_db=root / "direct.sqlite3", status_file=root / "direct.status.json",
            control_file=control, evidence_dir=root / "evidence", image_cache_dir=root / "images",
            desktop_status_file=desktop, shared_control_token_file=token)
        cognition, llm = FrozenCognition(), ImageModel()
        actor = direct.DirectQQActor(settings, cognition=cognition, llm=llm)
        cognition.actor = actor
        actor.client = FakeOneBot()
        actor.connected = True
        return actor, cognition, llm
    return build


async def consume(actor, payload):
    await actor.on_event(payload)
    row = actor.store.pending(actor.run_id)
    if row:
        await actor.process(row)
        return actor.store.get(row["event_id"])
    return None


def messages(actor, group):
    return [item for person in actor.memory.group_context(group_id=group).members for item in person.observations]


def test_protocol_send_uses_exact_group_and_real_verified_receipt(actor_factory):
    actor, cognition, _ = actor_factory(guest_send=True)
    cognition.texts = ["收到。", "稍后一起看。"]

    async def execute():
        await actor.verify_connection()
        first = await consume(actor, incoming(group=MAIN))
        second = await consume(actor, incoming(2, group=GUEST))
        return first, second

    first, second = asyncio.run(execute())
    assert first["state"] == second["state"] == "sent", actor.state
    sends = [call for call in actor.client.calls if call[0] == "send_group_msg"]
    assert [call[1]["group_id"] for call in sends] == [int(MAIN), int(MAIN), int(GUEST), int(GUEST)]
    assert all(call[1]["auto_escape"] is True for call in sends)
    assert all(call["transport"] == "onebot" for call in cognition.calls)
    for group, plan in zip((MAIN, GUEST), cognition.plans):
        receipt = decision.get_qq_delivery(memory_db=actor.session.memory_db, group_id=group, decision_id=plan.decision_id)
        assert receipt.verification == "qq_protocol_verified" and receipt.delivered_segments == cognition.texts
        assert len(receipt.protocol_message_ids) == 2 and len(receipt.segment_receipt_ids) == 2
        assert receipt.source_record_ids == [messages(actor, group)[0].record_id]
    assert actor.store.counts()["sends"] == {"sent": 4}


def test_readonly_group_records_pixels_and_shared_image_analysis_without_sending(actor_factory):
    actor, cognition, model = actor_factory()

    async def execute():
        await actor.verify_connection()
        return await consume(actor, incoming(group=GUEST, images=True))

    row = asyncio.run(execute())
    assert row["state"] == "observed" and not cognition.calls and not actor.client.sent
    observation = messages(actor, GUEST)[0]
    assert observation.text == "看看这个[图片 1]"
    analysis = observation.image_analysis[0]
    assert analysis.description == "绿色矩形" and analysis.ocr_text == "测试图片"
    assert analysis.authority == "model_inference" and analysis.evidence_ref
    assert len(list(actor.settings.image_cache_dir.glob("*.png"))) == 1 and len(model.calls) == 1
    shared = SharedMemoryStore(actor.settings.state_db.with_name("shared.sqlite3"))
    assert shared.import_qq_database(actor.session.memory_db, allowed_group_ids=[MAIN, GUEST], self_member_id=SELF).success
    imported = shared.query(SharedReadScope(audience=f"qq:group:{GUEST}", kinds=("qq_observation",))).records
    assert len(imported) == 1 and imported[0].text == observation.text
    assert imported[0].image_analysis[0].ocr_text == "测试图片"
    assert not shared.query(SharedReadScope(audience=f"qq:group:{MAIN}", kinds=("qq_observation",))).records


def test_on_demand_image_is_received_without_an_unavailable_error(actor_factory):
    actor, cognition, model = actor_factory()
    actor.settings = actor.settings.model_copy(update={"qq_image_analysis": "on_demand"})

    async def execute():
        await actor.verify_connection()
        return await consume(actor, incoming(group=GUEST, images=True))

    row = asyncio.run(execute())
    assert row["state"] == "observed" and not cognition.calls and not actor.client.sent
    analysis = messages(actor, GUEST)[0].image_analysis[0]
    assert analysis.status == "unknown" and analysis.error is None and analysis.evidence_ref
    assert analysis.description is None and analysis.ocr_text is None
    assert "图片已保存" in analysis.uncertainties[0]
    assert len(list(actor.settings.image_cache_dir.glob("*.png"))) == 1 and not model.calls


def test_repeated_sticker_skips_second_vision_call(actor_factory):
    actor, cognition, model = actor_factory()
    from harness.config import Settings
    actor.settings = actor.settings.model_copy(update={"qq_media_cache_enabled": True})
    model.settings = Settings(_env_file=None, llm_model="vision-cache-test",
        llm_base_url="https://vision.invalid/v1", call_log_enabled=False, record_interaction=False)

    async def execute():
        await actor.verify_connection()
        first = await consume(actor, incoming(1, group=GUEST, images=True))
        second = await consume(actor, incoming(2, group=GUEST, images=True))
        return first, second

    first, second = asyncio.run(execute())
    assert first["state"] == second["state"] == "observed" and not cognition.calls
    assert len(model.calls) == 1
    analyses = [item.image_analysis[0] for item in messages(actor, GUEST)]
    assert len(analyses) == 2 and {item.status for item in analyses} == {"described"}
    assert analyses[0].description == analyses[1].description == "绿色矩形"


def test_sendable_image_reserves_reply_budget(actor_factory, monkeypatch):
    actor, _, _ = actor_factory()
    seen = []

    async def image(*args, **kwargs):
        seen.append(kwargs.get("reserve_seconds"))
        return DirectImageObservation(status="described", evidence_ref="qq-direct-image:sha256:" + "a" * 64,
            description="图片说明", ocr_text=None)

    monkeypatch.setattr(direct, "load_and_describe_image", image)

    async def execute():
        await actor.verify_connection()
        return await consume(actor, incoming(images=True))

    asyncio.run(execute())
    assert seen == [direct.REPLY_RESERVE_SECONDS]



def test_long_ocr_is_bounded_without_rewriting_author_message(actor_factory, monkeypatch):
    actor, _, _ = actor_factory()

    async def image(*args, **kwargs):
        return DirectImageObservation(status="described", evidence_ref="qq-direct-image:sha256:" + "a" * 64,
            description="图片说明", ocr_text="字" * 9000, uncertainties=["不确定"] * 20)

    monkeypatch.setattr(direct, "load_and_describe_image", image)

    async def execute():
        await actor.verify_connection()
        return await consume(actor, incoming(group=GUEST, images=True))

    row = asyncio.run(execute())
    analysis = messages(actor, GUEST)[0].image_analysis[0]
    assert row["state"] == "observed" and len(analysis.ocr_text) == 6000
    assert len(analysis.uncertainties) == 16 and "截断" in analysis.uncertainties[-1]
    assert messages(actor, GUEST)[0].text == "看看这个[图片 1]"


def test_image_unknown_retains_received_message_without_inventing_content(actor_factory, monkeypatch):
    actor, _, _ = actor_factory()

    async def image(*args, **kwargs):
        return DirectImageObservation(error="download_failed", uncertainties=["图片不可用"])

    monkeypatch.setattr(direct, "load_and_describe_image", image)

    async def execute():
        await actor.verify_connection()
        return await consume(actor, incoming(group=GUEST, images=True))

    row = asyncio.run(execute())
    assert row["state"] == "observed"
    analysis = messages(actor, GUEST)[0].image_analysis[0]
    assert analysis.status == "unknown" and analysis.description is None and analysis.ocr_text is None


def test_duplicate_event_cannot_rethink_or_send_again(actor_factory):
    actor, cognition, _ = actor_factory()
    payload = incoming()

    async def execute():
        await actor.verify_connection()
        first = await consume(actor, payload)
        assert await consume(actor, payload) is None
        return first

    assert asyncio.run(execute())["state"] == "sent"
    assert len(cognition.calls) == len(actor.client.sent) == 1
    assert len(messages(actor, MAIN)) == 1


@pytest.mark.parametrize("kind", ["historical", "stale", "own"])
def test_ineligible_events_are_observed_without_reply(actor_factory, kind):
    actor, cognition, _ = actor_factory()

    async def execute():
        await actor.verify_connection()
        payload = incoming(member=SELF if kind == "own" else MEMBER, age=65 if kind == "stale" else 0)
        await actor.on_event(payload, historical=kind == "historical")
        row = actor.store.pending(actor.run_id)
        await actor.process(row)
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "observed"
    assert not cognition.calls and not actor.client.sent


@pytest.mark.parametrize("tamper", ["unfrozen", "body", "input_hash", "source_records"])
def test_unfrozen_or_altered_decision_is_rejected_before_send_api(actor_factory, tamper):
    actor, cognition, _ = actor_factory()
    if tamper == "unfrozen":
        cognition.persist = False
    elif tamper == "body":
        cognition.transform = lambda result: result.model_copy(update={"reply": "变造正文", "reply_segments": ["变造正文"]})
    elif tamper == "input_hash":
        cognition.transform = lambda result: result.model_copy(update={"input_hash": "changed"})
    else:
        cognition.transform = lambda result: result.model_copy(update={"new_observation_record_ids": []})

    async def execute():
        await actor.verify_connection()
        return await consume(actor, incoming())

    row = asyncio.run(execute())
    assert row["state"] == "failed" and not actor.client.sent
    assert actor.state["last_error_type"] == "direct_frozen_plan_mismatch"


def test_original_received_deadline_expires_before_thinking(actor_factory):
    actor, cognition, _ = actor_factory()

    async def execute():
        await actor.verify_connection()
        event = normalize_message(incoming(age=65), self_id=SELF, groups=actor.groups,
                                  received_at=utcnow() - timedelta(seconds=65))
        actor.store.receive(event, run_id=actor.run_id, evidence_ref=actor.store.evidence({"synthetic": True}))
        row = actor.store.pending(actor.run_id)
        await actor.process(row)
        return actor.store.get(row["event_id"])

    row = asyncio.run(execute())
    assert row["state"] == "expired" and not cognition.calls and not actor.client.sent


def test_live_incoming_is_selected_before_history_and_own_echo_backlog(actor_factory):
    actor, _, _ = actor_factory()

    async def execute():
        await actor.verify_connection()
        await actor.on_event(incoming(1, age=30), historical=True)
        await actor.on_event(incoming(2, member=SELF))
        await actor.on_event(incoming(3))
        row = actor.store.pending(actor.run_id)
        assert row["event"]["message_id"] == "3"
        await actor.process(row)
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "sent"
    assert len(actor.client.sent) == 1


@pytest.mark.parametrize("kind", ["historical", "own", "read_only"])
def test_readonly_picture_work_has_twelve_second_item_budget(actor_factory, monkeypatch, kind):
    actor, cognition, _ = actor_factory()
    seen = []

    async def image(*args, **kwargs):
        seen.append(kwargs["deadline"].remaining())
        return DirectImageObservation(error="vision_unavailable")

    monkeypatch.setattr(direct, "load_and_describe_image", image)

    async def execute():
        await actor.verify_connection()
        payload = incoming(images=True, member=SELF if kind == "own" else MEMBER,
                           group=GUEST if kind == "read_only" else MAIN)
        await actor.on_event(payload, historical=kind == "historical")
        row = actor.store.pending(actor.run_id)
        await actor.process(row)
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "observed"
    assert len(seen) == 1 and 0 < seen[0] <= 12
    assert not cognition.calls and not actor.client.sent


def test_queued_live_deadline_still_starts_at_original_received_time(actor_factory, monkeypatch):
    actor, cognition, _ = actor_factory()
    seen = []

    async def image(*args, **kwargs):
        seen.append(kwargs["deadline"].remaining())
        return DirectImageObservation(error="vision_unavailable")

    monkeypatch.setattr(direct, "load_and_describe_image", image)

    async def execute():
        await actor.verify_connection()
        payload = incoming(images=True, age=20)
        event = normalize_message(payload, self_id=SELF, groups=actor.groups,
                                  received_at=utcnow() - timedelta(seconds=20))
        actor.store.receive(event, run_id=actor.run_id, evidence_ref=actor.store.evidence(payload))
        row = actor.store.pending(actor.run_id)
        await actor.process(row)
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "sent"
    assert len(seen) == 1 and 35 < seen[0] <= 40
    assert 30 < cognition.calls[0]["deadline"].remaining() <= 40


def test_new_event_during_shared_cognition_preempts_old_reply(actor_factory):
    actor, cognition, _ = actor_factory()

    async def execute():
        await actor.verify_connection()
        loop = asyncio.get_running_loop()
        cognition.hook = lambda: asyncio.run_coroutine_threadsafe(actor.on_event(incoming(2)), loop).result(timeout=2)
        return await consume(actor, incoming())

    row = asyncio.run(execute())
    assert row["state"] == "failed" and not actor.client.sent
    assert actor.state["last_error_type"] == "direct_newer_context"
    assert actor.store.pending(actor.run_id) is not None


def test_history_backfill_during_cognition_does_not_preempt_live_reply(actor_factory):
    actor, cognition, _ = actor_factory()

    async def execute():
        await actor.verify_connection()
        loop = asyncio.get_running_loop()
        cognition.hook = lambda: asyncio.run_coroutine_threadsafe(
            actor.on_event(incoming(2, age=20), historical=True), loop).result(timeout=2)
        return await consume(actor, incoming())

    assert asyncio.run(execute())["state"] == "sent"
    assert len(actor.client.sent) == 1


def test_connection_epoch_change_during_cognition_prevents_send(actor_factory):
    actor, cognition, _ = actor_factory()
    cognition.hook = lambda: setattr(actor, "epoch", actor.epoch + 1)

    async def execute():
        await actor.verify_connection()
        return await consume(actor, incoming())

    assert asyncio.run(execute())["state"] == "failed"
    assert not actor.client.sent and actor.state["last_error_type"] == "direct_owner_unavailable"


def test_new_event_after_first_segment_keeps_verified_prefix_and_cancels_remainder(actor_factory):
    actor, cognition, _ = actor_factory()
    cognition.texts = ["第一段", "第二段"]

    async def execute():
        await actor.verify_connection()
        actor.client.after_send = lambda: actor.on_event(incoming(2))
        return await consume(actor, incoming())

    row = asyncio.run(execute())
    assert row["state"] == "partial" and len(actor.client.sent) == 1
    receipts = decision.get_qq_segment_deliveries(memory_db=actor.session.memory_db,
        group_id=MAIN, decision_id=cognition.plans[0].decision_id)
    assert len(receipts) == 1 and receipts[0].delivered_text == "第一段"
    assert not actor.store.uncertain()


def test_disconnect_after_attempt_is_unknown_and_never_resent(actor_factory):
    actor, cognition, _ = actor_factory()
    actor.client.send_error = OneBotError("onebot_disconnected")
    payload = incoming()

    async def execute():
        await actor.verify_connection()
        first = await consume(actor, payload)
        actor.epoch += 1
        actor.client.send_error = None
        assert await consume(actor, payload) is None
        other = await consume(actor, incoming(2))
        return first, other

    first, other = asyncio.run(execute())
    assert first["state"] == "unknown" and other["state"] == "failed"
    assert len(actor.client.sent) == 1 and actor.store.uncertain() and len(cognition.calls) == 1
    assert decision.get_qq_delivery(memory_db=actor.session.memory_db, group_id=MAIN,
                                   decision_id=cognition.plans[0].decision_id) is None
    actor.store.start_run(SELF)
    assert actor.store.uncertain() and actor.store.counts()["sends"] == {"unknown": 1}


@pytest.mark.parametrize("change", [
    lambda message: message | {"group_id": int(GUEST)},
    lambda message: message | {"sender": {"user_id": int(MEMBER)}},
    lambda message: message | {"message_id": 123456},
    lambda message: message | {"message": [{"type": "text", "data": {"text": "different"}}]},
])
def test_api_ack_alone_is_not_delivery_proof(actor_factory, change):
    actor, cognition, _ = actor_factory()
    actor.client.receipt_change = change

    async def execute():
        await actor.verify_connection()
        return await consume(actor, incoming())

    assert asyncio.run(execute())["state"] == "unknown"
    assert actor.store.uncertain() and len(actor.client.sent) == 1
    assert not decision.get_qq_segment_deliveries(memory_db=actor.session.memory_db,
        group_id=MAIN, decision_id=cognition.plans[0].decision_id)


def test_missing_get_msg_group_requires_exact_group_history_match(actor_factory):
    actor, _, _ = actor_factory()
    actor.client.receipt_change = lambda message: {key: value for key, value in message.items() if key != "group_id"}

    async def copy_history():
        actor.client.history = list(actor.client.sent.values())

    actor.client.after_send = copy_history

    async def execute():
        await actor.verify_connection()
        return await consume(actor, incoming())

    assert asyncio.run(execute())["state"] == "sent"
    assert len([call for call in actor.client.calls if call[0] == "get_group_msg_history"]) == 1


def test_temporary_get_msg_failure_recovers_full_receipt_without_second_send(actor_factory):
    actor, cognition, _ = actor_factory()
    actor.client.receipt_change = lambda _: (_ for _ in ()).throw(OneBotError("onebot_action_failed"))

    async def execute():
        await actor.verify_connection()
        row = await consume(actor, incoming())
        assert row["state"] == "unknown" and actor.store.uncertain()
        actor.client.receipt_change = None
        await actor.reconcile_unknown()
        assert not actor.store.uncertain()
        await actor.reconcile_unknown()
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "sent"
    assert len(actor.client.sent) == 1
    receipt = decision.get_qq_delivery(memory_db=actor.session.memory_db, group_id=MAIN,
                                      decision_id=cognition.plans[0].decision_id)
    assert receipt.protocol_message_ids == ["800"]
    assert actor.store.counts()["sends"] == {"sent": 1}


def test_recovery_after_expired_restart_records_prefix_without_sending_remainder(actor_factory, monkeypatch):
    actor, cognition, _ = actor_factory()
    cognition.texts = ["第一段", "过期的第二段"]
    actor.client.receipt_change = lambda _: (_ for _ in ()).throw(OneBotError("onebot_action_failed"))

    async def execute():
        await actor.verify_connection()
        row = await consume(actor, incoming())
        assert row["state"] == "unknown"
        real_now = utcnow
        monkeypatch.setattr(direct, "utcnow", lambda: real_now() + timedelta(seconds=70))
        # A new process opens the same durable stores; original decisions are
        # never resumed, even when receipt recovery proves the first send.
        replacement = direct.DirectQQActor(actor.settings, cognition=FrozenCognition(), llm=ImageModel())
        replacement.client = actor.client
        replacement.connected = True
        replacement.client.receipt_change = None
        await replacement.verify_connection()
        await replacement.reconcile_unknown()
        assert replacement.store.pending(replacement.run_id) is None
        assert not replacement.store.uncertain()
        return replacement.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "partial"
    assert len(actor.client.sent) == 1
    prefix = decision.get_qq_segment_deliveries(memory_db=actor.session.memory_db, group_id=MAIN,
                                               decision_id=cognition.plans[0].decision_id)
    assert len(prefix) == 1 and prefix[0].delivered_text == "第一段"
    assert decision.get_qq_delivery(memory_db=actor.session.memory_db, group_id=MAIN,
                                   decision_id=cognition.plans[0].decision_id) is None


def test_recovery_without_ack_message_id_never_queries_or_retries_send(actor_factory):
    actor, _, _ = actor_factory()
    actor.client.send_error = OneBotError("onebot_disconnected")

    async def execute():
        await actor.verify_connection()
        row = await consume(actor, incoming())
        before = len(actor.client.calls)
        actor.client.send_error = None
        await actor.reconcile_unknown()
        assert len(actor.client.calls) == before
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "unknown"
    assert actor.store.uncertain() and len(actor.client.sent) == 1


async def own_echo(actor, *, number=800, variant=None):
    payload = actor.client.sent["800"] | {"post_type": "message_sent", "message_type": "group",
        "self_id": int(SELF), "user_id": int(SELF), "message_id": number, "time": int(utcnow().timestamp())}
    if variant == "group":
        payload["group_id"] = int(GUEST)
    elif variant == "sender":
        payload |= {"user_id": int(MEMBER), "sender": {"user_id": int(MEMBER)}}
    elif variant == "text":
        payload["message"] = [{"type": "text", "data": {"text": "收到。 "}}]
    elif variant == "old":
        payload["time"] -= 2
    await actor.on_event(payload, historical=variant == "historical")


def test_missing_ack_recovers_from_unique_own_echo_after_restart_without_resend(actor_factory):
    actor, cognition, _ = actor_factory()
    actor.client.send_error = OneBotError("onebot_request_timeout")
    actor.client.after_send = lambda: own_echo(actor)

    async def execute():
        await actor.verify_connection()
        row = await consume(actor, incoming())
        assert row["state"] == "unknown"
        assert actor.store.outstanding_reconciliation() == []
        replacement = direct.DirectQQActor(actor.settings, cognition=FrozenCognition(), llm=ImageModel())
        replacement.client, replacement.connected = actor.client, True
        before = len(actor.client.calls)
        await replacement.reconcile_unknown()
        assert [call[0] for call in actor.client.calls[before:]] == ["get_msg"]
        assert not replacement.store.uncertain()
        await replacement.reconcile_unknown()
        return replacement.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "sent"
    assert len(actor.client.sent) == 1
    receipt = decision.get_qq_delivery(memory_db=actor.session.memory_db, group_id=MAIN,
                                      decision_id=cognition.plans[0].decision_id)
    assert receipt.protocol_message_ids == ["800"]
    with closing(actor.store.connect()) as db:
        send = json.loads(db.execute("SELECT record_json FROM qq_direct_sends").fetchone()[0])
    assert send["recovery_echo_ref"] in receipt.evidence_refs
    assert send["recovery_proof_ref"] in receipt.evidence_refs


@pytest.mark.parametrize("variant", ["group", "sender", "text", "old", "historical", "duplicate", "evidence"])
def test_missing_ack_ambiguous_or_invalid_echo_keeps_unknown_without_protocol_calls(actor_factory, variant):
    actor, _, _ = actor_factory()
    actor.client.send_error = OneBotError("onebot_request_timeout")

    async def echo():
        await own_echo(actor, variant=variant)
        if variant == "duplicate":
            await own_echo(actor, number=801)
        if variant == "evidence":
            with closing(actor.store.connect()) as db:
                row = db.execute("SELECT record_json FROM qq_direct_events WHERE message_id='800'").fetchone()
            digest = json.loads(row[0])["event_evidence_ref"].rsplit("/", 1)[-1]
            (actor.store.evidence_dir / (digest + ".json")).write_text("{}")

    actor.client.after_send = echo

    async def execute():
        await actor.verify_connection()
        row = await consume(actor, incoming())
        before = len(actor.client.calls)
        await actor.reconcile_unknown()
        assert len(actor.client.calls) == before
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "unknown"
    assert actor.store.uncertain() and len(actor.client.sent) == 1
    assert actor.store.outstanding_reconciliation() == []


def test_missing_ack_echo_requires_independent_protocol_confirmation_before_binding(actor_factory):
    actor, _, _ = actor_factory()
    actor.client.send_error = OneBotError("onebot_request_timeout")
    actor.client.after_send = lambda: own_echo(actor)
    actor.client.receipt_change = lambda result: result | {"group_id": int(GUEST)}

    async def execute():
        await actor.verify_connection()
        row = await consume(actor, incoming())
        await actor.reconcile_unknown()
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "unknown"
    assert actor.store.uncertain() and len(actor.client.sent) == 1
    assert actor.store.outstanding_reconciliation() == []


@pytest.mark.parametrize("variant", ["exact", "wrong_group", "duplicate", "disconnected"])
def test_missing_ack_echo_recovery_survives_bridge_message_cache_loss(actor_factory, variant):
    actor, _, _ = actor_factory()
    actor.client.send_error = OneBotError("onebot_request_timeout")
    actor.client.after_send = lambda: own_echo(actor)

    async def execute():
        await actor.verify_connection()
        row = await consume(actor, incoming())
        actor.client.history = list(actor.client.sent.values())
        if variant == "wrong_group":
            actor.client.history = [actor.client.history[0] | {"group_id": int(GUEST)}]
        elif variant == "duplicate":
            actor.client.history *= 2
        code = "onebot_disconnected" if variant == "disconnected" else "onebot_action_failed"
        actor.client.receipt_change = lambda _: (_ for _ in ()).throw(OneBotError(code))
        before = len(actor.client.calls)
        await actor.reconcile_unknown()
        reads = actor.client.calls[before:]
        assert [call[0] for call in reads] == (["get_msg"] if variant == "disconnected"
                                             else ["get_msg", "get_group_msg_history"])
        if variant != "disconnected":
            assert reads[-1][1] == {"group_id": int(MAIN), "count": 200}
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == ("sent" if variant == "exact" else "unknown")
    assert actor.store.uncertain() is (variant != "exact")
    assert len(actor.client.sent) == 1


@pytest.mark.parametrize("stage", ["before_reserve", "after_reserve", "after_attempted"])
def test_insufficient_send_budget_never_attempts_protocol_mutation(actor_factory, monkeypatch, stage):
    actor, _, _ = actor_factory()
    original_send, original_reserve = actor.send_decision, actor.store.reserve
    original_update = actor.store.update_send
    clock = [59.988 if stage == "before_reserve" else 0]

    async def nearly_expired(event, plan, deadline, epoch):
        await original_send(event, plan, direct.QQDeadline(60, clock=lambda: clock[0]), epoch)

    def reserve(*args):
        result = original_reserve(*args)
        if stage == "after_reserve":
            clock[0] = 59.988
        return result

    def update(value, status, **changes):
        result = original_update(value, status, **changes)
        if status == "attempted" and stage == "after_attempted":
            clock[0] = 59.988
        return result

    monkeypatch.setattr(actor, "send_decision", nearly_expired)
    monkeypatch.setattr(actor.store, "reserve", reserve)
    monkeypatch.setattr(actor.store, "update_send", update)

    async def execute():
        await actor.verify_connection()
        return await consume(actor, incoming())

    assert asyncio.run(execute())["state"] == "failed"
    assert actor.state["last_error_type"] == "direct_send_budget_insufficient"
    assert actor.store.counts()["sends"] == ({} if stage == "before_reserve" else {"cancelled": 1})
    assert not actor.store.uncertain() and not actor.client.sent
    if stage == "after_attempted":
        with closing(actor.store.connect()) as db:
            send = json.loads(db.execute("SELECT record_json FROM qq_direct_sends").fetchone()[0])
        assert send["attempted_at"] and send["cancelled_at"]
        assert send["cancel_phase"] == "before_protocol_call"
        assert send["cancel_reason"] == "direct_send_budget_insufficient"
        assert send["message_id"] is None and send["receipt_id"] is None


@pytest.mark.parametrize("tamper", ["group", "sender", "text"])
def test_readonly_recovery_keeps_unknown_when_message_binding_mismatches(actor_factory, tamper):
    actor, _, _ = actor_factory()
    change = ({"group_id": int(GUEST)} if tamper == "group" else
              {"sender": {"user_id": int(MEMBER)}} if tamper == "sender" else
              {"message": [{"type": "text", "data": {"text": "not the frozen body"}}]})
    actor.client.receipt_change = lambda message: message | change

    async def execute():
        await actor.verify_connection()
        row = await consume(actor, incoming())
        await actor.reconcile_unknown()
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "unknown"
    assert actor.store.uncertain() and len(actor.client.sent) == 1
    assert actor.state["last_reconciliation_error"] == "direct_reconciliation_unresolved"


def test_native_receipt_written_before_outbox_crash_is_reused_on_recovery(actor_factory, monkeypatch):
    actor, cognition, _ = actor_factory()
    original = actor.store.update_send
    failed = [False]

    def fail_once(value, status, **changes):
        if status == "sent" and not failed[0]:
            failed[0] = True
            raise OSError("synthetic commit failure")
        return original(value, status, **changes)

    monkeypatch.setattr(actor.store, "update_send", fail_once)

    async def execute():
        await actor.verify_connection()
        row = await consume(actor, incoming())
        assert row["state"] == "unknown"
        before = decision.get_qq_segment_deliveries(memory_db=actor.session.memory_db,
            group_id=MAIN, decision_id=cognition.plans[0].decision_id)
        assert len(before) == 1
        await actor.reconcile_unknown()
        after = decision.get_qq_segment_deliveries(memory_db=actor.session.memory_db,
            group_id=MAIN, decision_id=cognition.plans[0].decision_id)
        assert after == before
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "sent"
    assert len(actor.client.sent) == 1 and not actor.store.uncertain()


def test_aggregate_storage_failure_keeps_uncertainty_until_readonly_retry(actor_factory, monkeypatch):
    actor, _, _ = actor_factory()
    actor.client.receipt_change = lambda _: (_ for _ in ()).throw(OneBotError("onebot_action_failed"))
    original = direct.record_qq_protocol_delivery
    failures = [0]

    def fail_once(**kwargs):
        failures[0] += 1
        if failures[0] == 1:
            raise OSError("synthetic aggregate storage failure")
        return original(**kwargs)

    async def execute():
        await actor.verify_connection()
        row = await consume(actor, incoming())
        actor.client.receipt_change = None
        monkeypatch.setattr(direct, "record_qq_protocol_delivery", fail_once)
        await actor.reconcile_unknown()
        assert actor.store.uncertain()
        await actor.reconcile_unknown()
        return actor.store.get(row["event_id"])

    assert asyncio.run(execute())["state"] == "sent"
    assert not actor.store.uncertain() and len(actor.client.sent) == 1


def test_desktop_owner_pause_and_stop_prevent_second_sender(actor_factory):
    actor, _, _ = actor_factory()
    assert actor.active()
    actor.settings.desktop_status_file.write_text(json.dumps({"running": True, "heartbeat_at": utcnow().isoformat()}))
    assert not actor.active()
    actor.settings.desktop_status_file.write_text(json.dumps({"running": False, "heartbeat_at": utcnow().isoformat()}))
    actor.settings.control_file.write_text('{"paused":false,"stop":true}')
    assert actor.paused() and actor.stop.is_set() and not actor.active()
    actor.write_status()
    status = json.loads(actor.settings.status_file.read_text())
    assert status["self_member_id"] == SELF and status["repository_root"]


def test_incoming_storage_failure_reaches_connection_gap_handler(actor_factory, monkeypatch):
    actor, _, _ = actor_factory()
    monkeypatch.setattr(actor.store, "evidence", lambda _: (_ for _ in ()).throw(OSError("private disk failure")))
    with pytest.raises(OneBotError, match="onebot_event_callback_failed"):
        asyncio.run(actor.on_event(incoming()))
    assert actor.state["last_error_type"] == "direct_incoming_storage_failed"


@pytest.mark.parametrize("error,code", [
    (OSError("private websocket URL and token"), "onebot_connection_unavailable"),
    (OneBotError("onebot_account_offline"), "onebot_account_offline"),
])
def test_reconnect_logs_only_safe_state_changes(actor_factory, monkeypatch, caplog, error, code):
    actor, _, _ = actor_factory()
    actor.connected = False
    attempts = []

    class ReconnectingClient(FakeOneBot):
        async def open(self):
            attempts.append(self)
            if len(attempts) <= 2:
                raise error

    actor.client_factory = lambda *args, **kwargs: ReconnectingClient()
    original_wait = asyncio.wait_for

    async def fast_backoff(awaitable, *, timeout):
        if timeout == 3:
            awaitable.close()
            await asyncio.sleep(0)
            raise TimeoutError
        return await original_wait(awaitable, timeout)

    async def finish_after_recovery():
        actor.stop.set()

    monkeypatch.setattr(direct.asyncio, "wait_for", fast_backoff)
    monkeypatch.setattr(actor, "history", finish_after_recovery)
    caplog.set_level("INFO", logger=direct.__name__)
    asyncio.run(actor.run())
    assert len(attempts) == 3
    messages = [record.getMessage() for record in caplog.records if record.name == direct.__name__]
    assert messages == [f"qq_direct_disconnected error_type={code}; retrying", "qq_direct_connected"]
    assert "private" not in caplog.text


def test_forward_websocket_auth_and_interleaved_event_can_request_metadata():
    async def execute():
        headers = []
        received = []
        callback_done = asyncio.Event()
        token = "synthetic-auth-token-" + "a" * 32

        async def server(socket):
            headers.append(socket.request.headers.get("Authorization"))
            async for raw in socket:
                request = json.loads(raw)
                if request["action"] == "get_status":
                    await socket.send(json.dumps(incoming()))
                    await socket.send(json.dumps({"status": "ok", "retcode": 0,
                        "echo": request["echo"], "data": {"online": True}}))
                else:
                    await socket.send(json.dumps({"status": "ok", "retcode": 0,
                        "echo": request["echo"], "data": {"user_id": int(SELF)}}))

        async with serve(server, "127.0.0.1", 0) as host:
            port = host.sockets[0].getsockname()[1]

            async def on_event(payload):
                received.append(payload["message_id"])
                assert (await client.call("get_login_info", timeout=2))["user_id"] == int(SELF)
                callback_done.set()

            client = OneBotClient(f"ws://127.0.0.1:{port}", token, on_event=on_event)
            try:
                await client.open()
                assert (await client.call("get_status", timeout=2))["online"]
                await asyncio.wait_for(callback_done.wait(), 2)
            finally:
                await client.close()
        assert headers == ["Bearer " + token] and received == [1]
        assert not client._pending
    asyncio.run(execute())


def test_forward_websocket_disconnect_fails_pending_call_without_retry():
    async def execute():
        requests = []

        async def server(socket):
            request = json.loads(await socket.recv())
            requests.append(request)
            await socket.close()

        async with serve(server, "127.0.0.1", 0) as host:
            port = host.sockets[0].getsockname()[1]
            client = OneBotClient(f"ws://127.0.0.1:{port}", "test-token-" + "b" * 32,
                                  on_event=lambda _: None)
            try:
                await client.open()
                with pytest.raises(OneBotError, match="onebot_disconnected"):
                    await client.call("send_group_msg", {"group_id": int(MAIN), "message": []}, timeout=2)
            finally:
                await client.close()
        assert len(requests) == 1 and not client._pending
    asyncio.run(execute())


def test_forward_websocket_event_callback_failure_marks_gap():
    async def execute():
        async def server(socket):
            await socket.send(json.dumps(incoming()))
            await socket.wait_closed()

        async def on_event(payload):
            raise OSError("private storage detail")

        async with serve(server, "127.0.0.1", 0) as host:
            port = host.sockets[0].getsockname()[1]
            client = OneBotClient(f"ws://127.0.0.1:{port}", "test-token-" + "c" * 32, on_event=on_event)
            try:
                await client.open()
                await asyncio.wait_for(client.closed.wait(), 2)
                assert client.last_error_type == "onebot_event_callback_failed"
            finally:
                await client.close()
    asyncio.run(execute())


def test_forward_websocket_full_event_queue_disconnects_with_explicit_gap():
    async def execute():
        callback_block = asyncio.Event()

        async def server(socket):
            for index in range(140):
                await socket.send(json.dumps(incoming(index)))
            await socket.wait_closed()

        async def on_event(payload):
            await callback_block.wait()

        async with serve(server, "127.0.0.1", 0) as host:
            port = host.sockets[0].getsockname()[1]
            client = OneBotClient(f"ws://127.0.0.1:{port}", "test-token-" + "d" * 32, on_event=on_event)
            try:
                await client.open()
                await asyncio.wait_for(client.closed.wait(), 2)
                assert client.last_error_type == "onebot_event_queue_overflow"
            finally:
                await client.close()
    asyncio.run(execute())
