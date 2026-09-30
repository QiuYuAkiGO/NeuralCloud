"""First-launch contract: no placeholder groups and no false-positive probes."""
import asyncio
import json

import pytest

from harness.qq.direct import DirectQQActor
from harness.qq.direct_config import DirectQQSettings
from harness.qq.session import QQSessionSettings
from tests.test_qq_private_actor import (
    PRIVATE, PrivateOneBot, consume, incoming, private_message,
)
from tests.test_qq_private_actor import private_actor  # noqa: F401 - imported pytest fixture
from tests.test_qq_direct_actor import actor_factory  # noqa: F401 - pytest fixture dependency
from tools.selfcheck import probes_production as probes


@pytest.fixture
def private_only(private_actor):  # noqa: F811 - imported fixture
    old, cognition, llm = private_actor
    session = QQSessionSettings.model_validate(old.session.model_dump() | {"allowed_group_ids": []})
    old.settings.session_config_path.write_text(session.model_dump_json(), encoding="utf-8")
    settings = DirectQQSettings.model_validate(old.settings.model_dump() | {"group_roster": []})
    actor = DirectQQActor(settings, cognition=cognition, llm=llm)
    cognition.actor = actor
    actor.client = PrivateOneBot()
    actor.connected = True
    return actor, cognition


def test_private_only_round_trip_never_reads_or_sends_groups(private_only):
    actor, cognition = private_only

    async def scenario():
        await actor.verify_connection()
        assert not actor.groups
        assert await consume(actor, incoming()) is None
        assert await consume(actor, private_message() | {"user_id": 77777}) is None
        raw = private_message()
        assert (await consume(actor, raw))["state"] == "sent"
        await consume(actor, raw)
        await actor.history()
        assert len(cognition.calls) == 1
        actions = [action for action, _, _ in actor.client.calls]
        assert not any("group" in action for action in actions)
        assert actions.count("send_private_msg") == 1
    asyncio.run(scenario())


def test_private_only_config_rejects_empty_or_self_scope(private_only):
    actor, _ = private_only
    with pytest.raises(ValueError, match="conversation_required"):
        QQSessionSettings.model_validate(actor.session.model_dump() | {"allowed_private_peer_ids": []})
    with pytest.raises(ValueError, match="conversation_invalid"):
        QQSessionSettings.model_validate(actor.session.model_dump() | {"allowed_private_peer_ids": [actor.session.self_member_id]})
    with pytest.raises(ValueError, match="observed_conversation_required"):
        DirectQQSettings.model_validate(actor.settings.model_dump() | {"private_contacts": []})
    assert not DirectQQSettings.model_validate(actor.settings.model_dump() | {
        "enabled": False, "private_contacts": []}).enabled


@pytest.mark.parametrize("phase", ["receive", "queued", "send"])
def test_private_revocation_stops_collection_model_and_delivery(private_only, monkeypatch, phase):
    actor, cognition = private_only
    path = actor.settings.state_db.parent / "current-direct.json"
    payload = actor.settings.model_dump(mode="json")
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG", str(path))

    def revoke():
        payload["private_contacts"][0].update(observe=False, send=False)
        payload["enabled"] = False
        path.write_text(json.dumps(payload), encoding="utf-8")

    async def scenario():
        await actor.verify_connection()
        if phase == "receive":
            revoke()
            assert await consume(actor, private_message()) is None
        elif phase == "queued":
            await actor.on_event(private_message())
            row = actor.store.pending(actor.run_id)
            revoke()
            await actor.process(row)
            assert actor.store.get(row["event_id"])["state"] == "failed"
        else:
            cognition.hook = revoke
            assert (await consume(actor, private_message()))["state"] == "failed"
        before = len(actor.client.calls)
        await actor.history()
        assert len(actor.client.calls) == before
        assert not actor.client.sent
        assert len(cognition.calls) == (1 if phase == "send" else 0)
        assert PRIVATE not in actor.current_conversations(send=True)
    asyncio.run(scenario())


class ProbeApi:
    def __init__(self, *, diary=None):
        self.diary = diary or {"today_count": 0, "turns": []}

    def json(self, method, path, **kwargs):
        if path == "/v1/messages":
            return 200, {"event_id": "event-test"}
        if path == "/v1/diary/invite":
            return 200, {"entry_id": "entry-test"}
        if path == "/v1/diary":
            return 200, self.diary
        raise AssertionError((method, path))


@pytest.fixture
def probe_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NC_SELF_CHECK_LEDGER_DIR", str(tmp_path))
    monkeypatch.setattr(probes, "wait_for", lambda predicate, **kwargs: predicate())
    monkeypatch.setattr(probes, "_active_session", lambda runtime: "sess_test")


@pytest.mark.parametrize("kind,status,delivered,event_id,expected", [
    ("reply", "completed", True, "event-test", "pass"),
    ("wait", "completed", True, "event-test", "fail"),
    (None, "completed", True, "event-test", "fail"),
    ("reply", "failed", True, "event-test", "fail"),
    ("reply", "completed", False, "event-test", "fail"),
    ("reply", "completed", True, "unrelated-event", "fail"),
])
@pytest.mark.parametrize("wrapped", [True, False])
def test_chat_probe_requires_its_reply_and_delivery(probe_env, monkeypatch, kind, status, delivered, event_id, expected, wrapped):
    data = {"event_id": event_id, "decision_kind": kind, "status": status,
            "detail": {"delivered": delivered, "delivery": {"success": delivered, "delivered_segments": 1}}}
    record = {"data": data}
    monkeypatch.setattr(probes, "_completed", lambda *args: {"payload": record} if wrapped else record)
    assert probes._chat_reply(ProbeApi(), timeout=0).status == expected


@pytest.mark.parametrize("diary,expected", [
    ({"today_count": 0, "turns": []}, "fail"),
    ({"wrote_today": True, "today_count": 1, "turns": [{"entry_id": "old", "status": "committed"}]}, "fail"),
    ({"today_count": 1, "turns": [{"entry_id": "entry-test", "status": "wait"}]}, "fail"),
    ({"today_count": 1, "turns": [{"entry_id": "entry-test", "status": "committed"}]}, "pass"),
])
def test_diary_probe_requires_exact_committed_entry(probe_env, diary, expected):
    assert probes._diary(ProbeApi(diary=diary), timeout=0).status == expected
