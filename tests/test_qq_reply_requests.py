"""Coalescing preserves raw observations, original authority and outbox safety."""
from contextlib import closing
import asyncio
from datetime import datetime, timedelta
import json

import pytest

from harness.qq.direct_store import canonical, normalize_message, utcnow
from harness.qq.reply_requests import ReplyRequestChanged, binding, validate_reply_request
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.telemetry import QQDeadline
from tests.test_qq_direct_actor import SELF, MAIN, GUEST, actor_factory, incoming  # noqa: F401


@pytest.fixture
def actor(actor_factory):  # noqa: F811 - imported fixture
    value, _, _ = actor_factory(guest_send=True)
    value.settings = value.settings.model_copy(update={"qq_reply_scheduler": "coalesced"})
    return value


async def add(actor, number, *, member="20001", group=MAIN, at=False, historical=False):
    raw = incoming(number, member=member, group=group)
    if at:
        raw["message"].insert(0, {"type": "at", "data": {"qq": SELF}})
    await actor.on_event(raw, historical=historical)
    event = normalize_message(raw, self_id=SELF, groups=actor.groups)
    return actor.store.get(event["event_id"])["event"]


def claim(actor):
    return actor.store.pending(actor.run_id, scheduler_mode="coalesced")


def request_for(row):
    event = row["event"]
    first_seen = datetime.fromisoformat(event["received_at"])
    return QQSharedPrepareRequest(transport="onebot", group_id=event["group_id"],
        self_member_id=SELF, trigger_member_id=event["member_id"],
        deadline_at=first_seen + timedelta(seconds=60), **binding(row["reply_request"]),
        trace={"trace_id": "qq-trace:" + "a" * 32, "event_id": event["event_id"],
               "first_seen_at": first_seen, "first_seen_mono": 0})


def test_queued_ambient_batch_sends_once_and_keeps_each_observation(actor):
    async def execute():
        await actor.verify_connection()
        events = [await add(actor, number, member=str(20000 + number)) for number in range(1, 4)]
        row = claim(actor)
        assert row["reply_request"]["source_event_ids"] == [event["event_id"] for event in events]
        await actor.process(row)
        return events, row
    events, row = asyncio.run(execute())
    assert len(actor.cognition.calls) == 1 and len(actor.client.sent) == 1
    assert actor.store.get(events[0]["event_id"])["state"] == "sent"
    assert all(actor.store.get(event["event_id"])["source_record_id"] for event in events)
    assert all(actor.store.get(event["event_id"])["state"] == "observed" for event in events[1:])
    assert actor.cognition.calls[0]["source_event_ids"] == row["reply_request"]["source_event_ids"]
    assert len({actor.store.get(event["event_id"])["source_record_id"] for event in events}) == 3
    assert claim(actor) is None


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_legacy_and_shadow_keep_selection_and_no_coalescing(actor, mode):
    actor.settings = actor.settings.model_copy(update={"qq_reply_scheduler": mode})
    async def execute():
        first = await add(actor, 1)
        await add(actor, 2)
        return first
    first = asyncio.run(execute())
    row = actor.store.pending(actor.run_id, scheduler_mode=mode)
    assert row["event_id"] == first["event_id"] and "reply_request" not in row
    with closing(actor.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM qq_reply_requests").fetchone()[0] == 0


def test_same_author_supplement_revises_without_new_anchor_or_ttl(actor):
    async def execute():
        first = await add(actor, 1, at=True)
        old = claim(actor)
        actor.store.reply_requests.check(old["event"], state="preparing")
        second = await add(actor, 2)
        with pytest.raises(ReplyRequestChanged, match="revision_changed"):
            actor.store.reply_requests.check(old["event"])
        return first, second, old, claim(actor)
    first, second, old, new = asyncio.run(execute())
    assert new["reply_request"]["revision"] == 2
    assert new["reply_request"]["request_id"] == old["reply_request"]["request_id"]
    assert new["reply_request"]["authority_anchor"] == first["event_id"]
    assert new["reply_request"]["first_seen_at"] == first["received_at"]
    assert new["reply_request"]["deadline_at"] == old["reply_request"]["deadline_at"]
    assert new["reply_request"]["source_event_ids"] == [first["event_id"], second["event_id"]]


def test_new_explicit_and_other_author_never_join_explicit_request(actor):
    async def execute():
        first = await add(actor, 1, at=True)
        other = await add(actor, 2, member="20002")
        second = await add(actor, 3, at=True)
        return first, other, second
    events = asyncio.run(execute())
    with closing(actor.store.connect()) as db:
        rows = db.execute("SELECT * FROM qq_reply_requests ORDER BY first_seen_at").fetchall()
    assert len(rows) == 3
    assert all(row["revision"] == 1 for row in rows)
    assert [json.loads(row["source_event_ids"]) for row in rows] == [[event["event_id"]] for event in events]


def test_fairness_three_explicit_then_one_ambient_and_no_extra_wait(actor):
    async def execute():
        ambient = await add(actor, 1)
        for number in range(2, 7):
            await add(actor, number, member=str(20000 + number), at=True)
        rows = []
        for _ in range(6):
            row = claim(actor)
            rows.append(row)
            actor.store.update(row["event_id"], state="observed")
            actor.store.reply_requests.finish(row["event"], "observed")
        return ambient, rows
    ambient, rows = asyncio.run(execute())
    assert [row["reply_request"]["addressing"] for row in rows] == [
        "self_mention", "self_mention", "self_mention", "ambient", "self_mention", "self_mention"]
    assert rows[3]["event_id"] == ambient["event_id"]


def test_no_cross_scope_or_history_merge_and_no_ttl_refresh_after_expiry(actor, monkeypatch):
    from harness.qq import reply_requests
    async def execute():
        first = await add(actor, 1)
        await add(actor, 2, group=GUEST)
        await add(actor, 3, historical=True)
        monkeypatch.setattr(reply_requests, "utcnow", lambda: utcnow() + timedelta(seconds=61))
        last = await add(actor, 4)
        return first, last
    first, last = asyncio.run(execute())
    with closing(actor.store.connect()) as db:
        rows = db.execute("SELECT * FROM qq_reply_requests ORDER BY first_seen_at").fetchall()
    assert len(rows) == 3
    assert rows[0]["authority_anchor"] == first["event_id"]
    assert rows[-1]["authority_anchor"] == last["event_id"]


def test_revision_during_model_does_not_send_or_overwrite_new_queued_intent(actor):
    async def execute():
        await actor.verify_connection()
        await add(actor, 1, at=True)
        row = claim(actor)
        raw = incoming(2)
        source = normalize_message(raw, self_id=SELF, groups=actor.groups)
        def revise():
            actor.store.receive(source, run_id=actor.run_id, evidence_ref=actor.store.evidence(raw))
            actor.enqueue_reply(actor.store.get(source["event_id"])["event"])
        actor.cognition.hook = revise
        await actor.process(row)
        actor.cognition.hook = None
        return row, claim(actor)
    old, fresh = asyncio.run(execute())
    assert not actor.client.sent
    assert fresh["reply_request"]["revision"] == old["reply_request"]["revision"] + 1
    assert actor.store.get(fresh["event_id"])["decision_id"] is None
    assert actor.store.get(fresh["event_id"])["state"] == "received"


def test_frozen_old_revision_cannot_update_ready_or_reserve(actor):
    async def execute():
        await actor.verify_connection()
        await add(actor, 1, at=True)
        row = claim(actor)
        await actor.observe(row["event"], QQDeadline.after(10))
        decision = actor.cognition(group_id=MAIN, trigger_member_id="20001")
        actor.store.update(row["event_id"], state="ready", decision_id=decision.decision_id)
        await add(actor, 2)
        return row, decision
    old, plan = asyncio.run(execute())
    with pytest.raises(ReplyRequestChanged):
        actor.store.update(old["event_id"], state="ready", decision_id=plan.decision_id,
                           reply_request=old["event"]["_reply_request"])
    with pytest.raises(ReplyRequestChanged):
        actor.store.reserve(old["event"], plan, 0, plan.reply_segments[0])
    assert not actor.client.sent


def test_reserved_outbox_prevents_revising_the_old_request(actor):
    async def execute():
        await actor.verify_connection()
        await add(actor, 1, at=True)
        row = claim(actor)
        await actor.observe(row["event"], QQDeadline.after(10))
        plan = actor.cognition(group_id=MAIN, trigger_member_id="20001")
        actor.store.update(row["event_id"], state="ready", decision_id=plan.decision_id)
        send = actor.store.reserve(row["event"], plan, 0, plan.reply_segments[0])
        await add(actor, 2)
        return row, send, claim(actor)
    old, send, fresh = asyncio.run(execute())
    assert fresh["reply_request"]["request_id"] != old["reply_request"]["request_id"]
    assert actor.store.reply_requests.check(old["event"])["revision"] == 1
    assert send["reply_request"] == old["event"]["_reply_request"]


@pytest.mark.parametrize("corruption", [None, "raw", "bound", "profile", "revision", "scope", "sources"])
def test_runtime_readonly_batch_source_validation(actor, monkeypatch, corruption):
    config_path = actor.settings.state_db.parent / "direct.json"
    config_path.write_text(actor.settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG", str(config_path))
    async def execute():
        await actor.verify_connection()
        await add(actor, 1)
        await add(actor, 2, member="20002")
        row = claim(actor)
        for event_id in row["reply_request"]["source_event_ids"]:
            await actor.observe(actor.store.get(event_id)["event"], QQDeadline.after(10))
        actor.store.reply_requests.check(row["event"], state="preparing")
        return row
    row = asyncio.run(execute())
    request = request_for(row)
    second = actor.store.get(row["reply_request"]["source_event_ids"][1])
    if corruption == "raw":
        path = actor.settings.evidence_dir / (second["event"]["event_evidence_ref"].rsplit("/", 1)[1] + ".json")
        path.write_text("{}", encoding="utf-8")
    elif corruption in {"bound", "profile"}:
        event = second["event"]
        if corruption == "bound":
            event["bound_observation"]["text"] = "tampered"
        else:
            event["bound_observation"]["profile_record_id"] = "qq-profile:missing"
        with closing(actor.store.connect()) as db, db:
            db.execute("UPDATE qq_direct_events SET record_json=? WHERE event_id=?", (canonical(event), second["event_id"]))
    elif corruption == "revision":
        request = request.model_copy(update={"revision": request.revision + 1})
    elif corruption == "scope":
        request = request.model_copy(update={"group_id": GUEST})
    elif corruption == "sources":
        request = request.model_copy(update={"source_event_ids": request.source_event_ids[:1]})
    if corruption:
        with pytest.raises(ValueError):
            validate_reply_request(actor.settings.state_db, request, memory_config=actor.session)
    else:
        result = validate_reply_request(actor.settings.state_db, request, memory_config=actor.session)
        assert len(result["sources"]) == len(result["source_record_ids"]) == 2
        assert [value["member_id"] for value in result["sources"]] == ["20001", "20002"]
        assert actor.store.get(second["event_id"])["state"] == "bound"


def test_partial_batch_fields_and_wrong_anchor_are_rejected(actor):
    async def execute():
        await add(actor, 1)
        return claim(actor)
    row = asyncio.run(execute())
    request = request_for(row)
    for changes in ({"revision": None}, {"authority_anchor": "qq-watch-event:" + "b" * 32},
                    {"source_event_ids": request.source_event_ids * 2}):
        with pytest.raises(ValueError):
            QQSharedPrepareRequest.model_validate(request.model_dump() | changes)


def test_restarted_run_cannot_replay_old_batch(actor):
    async def execute():
        await add(actor, 1)
        return claim(actor)
    row = asyncio.run(execute())
    actor.store.start_run(SELF)
    with pytest.raises(ReplyRequestChanged, match="binding_invalid"):
        actor.store.reply_requests.check(row["event"])


def test_twenty_first_supplement_starts_independent_ambient_with_own_ttl(actor):
    async def execute():
        events = [await add(actor, 1, at=True)]
        for number in range(2, 22):
            events.append(await add(actor, number))
        return events
    events = asyncio.run(execute())
    with closing(actor.store.connect()) as db:
        rows = db.execute("SELECT * FROM qq_reply_requests ORDER BY first_seen_at").fetchall()
    assert len(rows) == 2
    assert len(json.loads(rows[0]["source_event_ids"])) == 20
    assert rows[1]["addressing"] == "ambient" and rows[1]["authority_anchor"] == events[-1]["event_id"]
    assert rows[1]["deadline_at"] == (datetime.fromisoformat(events[-1]["received_at"]) + timedelta(seconds=60)).isoformat()
