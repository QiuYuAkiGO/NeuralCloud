"""Private friend transport, exact-peer receipts and existing shared ingress."""
import asyncio
from datetime import datetime, timedelta
import json

import pytest

from harness.qq.conversation import private_scope_id
from harness.qq.direct import DirectQQActor
from harness.qq.direct_config import DirectQQSettings
from harness.qq.direct_source import validate_direct_source
from harness.qq.direct_store import normalize_message, normalize_evidence
from harness.qq.onebot import OneBotError
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.telemetry import QQDeadline
from tests.test_qq_direct_actor import (SELF, MAIN, GUEST, MEMBER, FakeOneBot, FrozenCognition,
    actor_factory, incoming, consume, messages)  # noqa: F401

PRIVATE = private_scope_id(SELF, MEMBER)


def private_message(number=1, *, member=MEMBER, images=False):
    raw = incoming(number, member=member, images=images)
    raw.pop("group_id")
    return raw | {"message_type": "private", "sub_type": "friend", "target_id": int(MEMBER)}


class PrivateOneBot(FakeOneBot):
    def __init__(self):
        super().__init__()
        self.private_history = []
        self.friend = True
        self.history_error = False

    async def call(self, action, params=None, timeout=10):
        if action not in {"get_friend_list", "send_private_msg", "get_friend_msg_history"}:
            return await super().call(action, params, timeout)
        params = params or {}
        self.calls.append((action, params, timeout))
        if action == "get_friend_list":
            return [{"user_id": int(MEMBER), "nickname": "秋语"}] if self.friend else []
        assert params["user_id"] == MEMBER and "group_id" not in params
        if action == "send_private_msg":
            message = private_message(800 + len(self.sent), member=SELF)
            message["message"] = params["message"]
            message["post_type"] = "message_sent"
            self.sent[str(message["message_id"])] = message
            self.private_history.append(message)
            return {"message_id": message["message_id"]}
        if self.history_error:
            raise OneBotError("onebot_action_failed")
        return {"messages": self.private_history}


@pytest.fixture
def private_actor(actor_factory):  # noqa: F811 - imported pytest fixture
    old, _, llm = actor_factory(guest_send=True)
    session = old.session.model_copy(update={"allowed_private_peer_ids": [MEMBER]})
    old.settings.session_config_path.write_text(session.model_dump_json(), encoding="utf-8")
    settings = DirectQQSettings.model_validate(old.settings.model_dump() | {
        "private_contacts": [{"peer_id": MEMBER, "label": "秋语", "observe": True, "send": True}]})
    cognition = FrozenCognition()
    actor = DirectQQActor(settings, cognition=cognition, llm=llm)
    cognition.actor = actor
    actor.client = PrivateOneBot()
    actor.connected = True
    return actor, cognition, llm


def test_private_image_reply_uses_same_cognition_and_exact_friend_receipt(private_actor):
    actor, cognition, llm = private_actor

    async def run():
        await actor.verify_connection()
        row = await consume(actor, private_message(images=True))
        assert row["state"] == "sent"
        assert cognition.calls[0]["group_id"] == PRIVATE
        assert cognition.calls[0]["transport"] == "onebot"
        assert llm.calls and messages(actor, PRIVATE)[0].image_analysis[0].status == "described"
        assert not messages(actor, MAIN) and not messages(actor, GUEST)
        actions = [item[0] for item in actor.client.calls]
        assert "send_private_msg" in actions and "send_group_msg" not in actions and "get_msg" not in actions
        assert actor.store.counts()["sends"] == {"sent": 1}
        assert await consume(actor, private_message(member=SELF)) is None
    asyncio.run(run())


def test_private_and_group_messages_with_same_id_are_separate(private_actor):
    actor, cognition, _ = private_actor

    async def run():
        await actor.verify_connection()
        assert (await consume(actor, private_message(44)))["state"] == "sent"
        assert (await consume(actor, incoming(44, group=GUEST)))["state"] == "sent"
        assert [call["group_id"] for call in cognition.calls] == [PRIVATE, GUEST]
        assert len(messages(actor, PRIVATE)) == len(messages(actor, GUEST)) == 1
    asyncio.run(run())


@pytest.mark.parametrize("changes", [{"sub_type": "group"}, {"sub_type": "other"},
    {"user_id": 77777}, {"group_id": int(MAIN)}])
def test_unlisted_or_temporary_private_cannot_enter(changes):
    assert normalize_message(private_message() | changes, self_id=SELF, groups=[MAIN], private_peers=[MEMBER]) is None


def test_own_private_needs_peer_bound_history():
    raw = private_message(member=SELF)
    assert normalize_message(raw, self_id=SELF, groups=[], private_peers=[MEMBER]) is None
    envelope = {"action": "get_friend_msg_history", "self_id": SELF, "peer_id": MEMBER, "result": raw}
    event = normalize_evidence(envelope, self_id=SELF, groups=[], private_peers=[MEMBER])
    assert event["own"] and event["group_id"] == PRIVATE
    with pytest.raises(ValueError):
        normalize_evidence(envelope | {"peer_id": "77777"}, self_id=SELF, groups=[], private_peers=[MEMBER])


def test_napcat_ws_target_is_conversation_peer_not_incoming_recipient():
    incoming = private_message()
    event = normalize_message(incoming, self_id=SELF, groups=[], private_peers=[MEMBER])
    assert event["member_id"] == MEMBER and event["group_id"] == PRIVATE
    for wrong in (int(SELF), 77777):
        with pytest.raises(ValueError, match="private_target_mismatch"):
            normalize_message(incoming | {"target_id": wrong}, self_id=SELF, groups=[], private_peers=[MEMBER])


def test_empty_private_history_does_not_disable_live_channel(private_actor):
    actor, _, _ = private_actor

    async def run():
        await actor.verify_connection()
        actor.client.history_error = True
        await actor.history()
        assert actor.active() and actor.state["private_history_error"] == "history_unavailable_or_empty"
        actor.client.history_error = False
        assert (await consume(actor, private_message()))["state"] == "sent"
    asyncio.run(run())


def test_private_unknown_receipt_blocks_resend_and_reconciles_only_by_friend_history(private_actor):
    actor, cognition, _ = private_actor

    async def run():
        await actor.verify_connection()
        actor.client.history_error = True
        row = await consume(actor, private_message())
        assert row["state"] == "unknown" and actor.store.uncertain()
        actor.client.history_error = False
        await actor.reconcile_unknown()
        assert actor.store.get(row["event_id"])["state"] == "sent" and not actor.store.uncertain()
        assert len([c for c in actor.client.calls if c[0] == "send_private_msg"]) == 1
    asyncio.run(run())


def test_actual_private_observation_passes_runtime_source_and_revocation_fails(private_actor, monkeypatch):
    actor, _, _ = private_actor
    path = actor.settings.state_db.parent / "direct.json"
    path.write_text(actor.settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG", str(path))

    async def run():
        await actor.verify_connection()
        await actor.on_event(private_message(images=True))
        row = actor.store.pending(actor.run_id)
        await actor.observe(row["event"], QQDeadline.after(30))
        seen = datetime.fromisoformat(row["received_at"])
        request = QQSharedPrepareRequest(transport="onebot", group_id=PRIVATE, self_member_id=SELF,
            trigger_member_id=MEMBER, deadline_at=seen + timedelta(seconds=60), trace={
                "trace_id": "qq-trace:" + "a" * 32, "event_id": row["event_id"], "first_seen_at": seen, "first_seen_mono": 0})
        assert validate_direct_source(actor.session, request) is None
        with pytest.raises(ValueError, match="scope_invalid"):
            validate_direct_source(actor.session.model_copy(update={"allowed_private_peer_ids": []}), request)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["private_contacts"][0]["send"] = False
        path.write_text(json.dumps(payload))
        with pytest.raises(ValueError, match="scope_invalid"):
            validate_direct_source(actor.session, request)
    asyncio.run(run())


def test_nonfriend_channel_fails_connection_without_sending(private_actor):
    actor, _, _ = private_actor
    actor.client.friend = False
    with pytest.raises(OneBotError, match="not_friend"):
        asyncio.run(actor.verify_connection())
    assert not actor.client.sent
