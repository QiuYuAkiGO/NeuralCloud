"""Real revision/checkpoint boundaries with synthetic model and protocol clients."""
import asyncio
from contextlib import closing
import json

import pytest

from harness.qq import decision
from harness.qq.direct_store import canonical, normalize_message, utcnow
from harness.qq.reply_requests import ReplyRequestChanged
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from tests.test_qq_decision import setup  # noqa: F401
from tests.test_qq_direct_actor import actor_factory, incoming, SELF, MAIN  # noqa: F401
from tests.test_qq_prepared_turn import _binding
from tests.test_qq_reply_requests import request_for
from tests.test_qq_private_actor import private_actor, private_message, PRIVATE  # noqa: F401
from harness.qq.turn_checkpoints import QQTurnCheckpointStore, QQTurnStateError


def test_cross_author_batch_can_prepare_without_losing_its_individual_authors(setup, actor_factory):  # noqa: F811
    actor, _, _ = actor_factory()
    actor.settings = actor.settings.model_copy(update={"qq_reply_scheduler": "coalesced"})
    kwargs = setup[3] | {"memory_db": actor.session.memory_db}
    async def execute():
        await actor.verify_connection()
        ids = []
        for number, member in [(1, "20001"), (2, "20002")]:
            payload = incoming(number, member=member)
            await actor.on_event(payload)
            event = normalize_message(payload, self_id=SELF, groups=[MAIN])
            ids.append(event["event_id"])
            await actor.observe(actor.store.get(event["event_id"])["event"], QQDeadline.after(10))
        return decision.prepare_qq_turn(**kwargs, trigger_member_id="20001",
            verified_self_aliases=actor.aliases[MAIN], _request_source_event_ids=ids,
            turn_binding=_binding(transport="onebot", event_id="qq-turn:first", trigger_member_id="20001"),
            deadline=QQDeadline.after(60), trace=QQTurnTrace())
    result = asyncio.run(execute())
    assert result["kind"] == "prepared"


@pytest.mark.parametrize("private", [False, True])
def test_undelivered_completed_old_body_preserves_original_at_authority_on_new_revision(
        setup, actor_factory, private_actor, monkeypatch, private):  # noqa: F811
    from harness.runtime.qq_sources import verified_qq_source

    actor, _, _ = private_actor if private else actor_factory()
    group = PRIVATE if private else MAIN
    message = private_message if private else incoming
    actor.settings = actor.settings.model_copy(update={"qq_reply_scheduler": "coalesced"})
    config_path = actor.settings.state_db.parent / "direct.json"
    config_path.write_text(actor.settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG", str(config_path))
    kwargs = setup[3] | {"memory_db": actor.session.memory_db, "group_id": group}
    async def execute():
        await actor.verify_connection()
        first = message(1)
        if not private:
            first["message"].insert(0, {"type": "at", "data": {"qq": SELF}})
        await actor.on_event(first)
        row = actor.store.pending(actor.run_id, scheduler_mode="coalesced")
        anchor = await actor.observe(row["event"], QQDeadline.after(10))
        original_binding = _binding(transport="onebot", event_id="qq-turn:first", trigger_member_id="20001", group_id=group)
        prepared = decision.prepare_qq_turn(**kwargs, trigger_member_id="20001",
            verified_self_aliases=actor.aliases[group], _request_source_event_ids=[row["event_id"]],
            turn_binding=original_binding, deadline=QQDeadline.after(60), trace=QQTurnTrace())
        old = decision.resume_qq_response(memory_db=actor.session.memory_db, config_path=kwargs["config_path"],
            preset_path=kwargs["preset_path"], prepare_id=prepared["prepare_id"], snapshot_hash=prepared["snapshot_hash"],
            turn_binding=original_binding, deadline=QQDeadline.after(60))
        assert old.outcome == "ready" and anchor.record_id in old.new_observation_record_ids
        assert not actor.client.sent
        second = message(2)
        await actor.on_event(second)
        fresh = actor.store.pending(actor.run_id, scheduler_mode="coalesced")
        supplement = fresh["reply_request"]["source_event_ids"][1]
        await actor.observe(actor.store.get(supplement)["event"], QQDeadline.after(10))
        actor.store.reply_requests.check(fresh["event"], state="preparing")
        actor.store.update(fresh["event_id"], state="preparing", reply_request=fresh["event"]["_reply_request"])
        authority = verified_qq_source(request_for(fresh), actor.session)["source"]["record_id"]
        assert authority == anchor.record_id
        revised_binding = original_binding | {"event_id": "qq-turn:revised"}
        revised = decision.prepare_qq_turn(**kwargs, trigger_member_id="20001",
            verified_self_aliases=actor.aliases[group], _request_source_event_ids=fresh["reply_request"]["source_event_ids"],
            _request_authority_record_id=authority,
            turn_binding=revised_binding, deadline=QQDeadline.after(60), trace=QQTurnTrace())
        result = decision.resume_qq_response(memory_db=actor.session.memory_db, config_path=kwargs["config_path"],
            preset_path=kwargs["preset_path"], prepare_id=revised["prepare_id"], snapshot_hash=revised["snapshot_hash"],
            turn_binding=revised_binding, deadline=QQDeadline.after(60))
        if not private:
            await actor.on_event(incoming(3, member="20003"))
        assert result.addressing == "direct", "pending authority anchor was erased from addressing candidates"
        assert anchor.record_id not in result.new_observation_record_ids
        assert result.addressing_source_record_ids == [anchor.record_id]
        actor.check_send(fresh["event"], QQDeadline.after(10), actor.epoch, decision=result)
        # The exemption applies only to this active request. Removing it keeps
        # the old seen anchor from acquiring new authority as a legacy turn.
        legacy = {key: value for key, value in fresh["event"].items() if key != "_reply_request"}
        from harness.qq.onebot import OneBotError
        if not private:
            with pytest.raises(OneBotError, match="direct_newer_context"):
                actor.check_send(legacy, QQDeadline.after(10), actor.epoch, decision=result)
        # A subsequent correction from the requester still revokes the frozen revision.
        await actor.on_event(message(4))
        with pytest.raises(ReplyRequestChanged, match="revision_changed"):
            actor.check_send(fresh["event"], QQDeadline.after(10), actor.epoch, decision=result)
        return result
    result = asyncio.run(execute())
    assert result.outcome == "ready"


def test_cancelled_checkpoint_cannot_be_completed_by_late_body(setup):  # noqa: F811
    _, _, _, kwargs, _ = setup
    binding = _binding()
    prepared = decision.prepare_qq_turn(**kwargs, trigger_member_id="20002", turn_binding=binding,
        deadline=QQDeadline.after(60), trace=QQTurnTrace())
    store = QQTurnCheckpointStore(kwargs["memory_db"])
    row = store.load(prepared["prepare_id"])
    store.terminate(binding, "cancelled")
    with pytest.raises(QQTurnStateError):
        store.complete(binding, row["snapshot"]["decision"])


@pytest.mark.parametrize("changed", ["pause", "owner", "epoch", "revision", "other_unknown"])
def test_authority_change_after_attempted_commit_prevents_wire_send(actor_factory, monkeypatch, changed):  # noqa: F811
    actor, _, _ = actor_factory()
    actor.settings = actor.settings.model_copy(update={"qq_reply_scheduler": "coalesced"})
    original = actor.store.update_send
    attempts = []
    def update(value, status, **changes):
        result = original(value, status, **changes)
        if status == "attempted" and result["message_id"] is None:
            attempts.append(result)
            if changed == "pause":
                actor.settings.control_file.write_text('{"paused":true,"stop":false}')
            elif changed == "owner":
                actor.settings.desktop_status_file.write_text(json.dumps({
                    "running": True, "heartbeat_at": utcnow().isoformat()}))
            elif changed == "epoch":
                actor.epoch += 1
            elif changed == "revision":
                # Simulate a concurrently changed persisted request. Ingress
                # normally refuses revisions once an outbox intent exists.
                with closing(actor.store.connect()) as db, db:
                    db.execute("UPDATE qq_reply_requests SET revision=revision+1 WHERE request_id=?",
                               (result["reply_request"]["request_id"],))
            else:
                other = result | {"send_id": "qq-direct-send:other", "event_id": "qq-watch-event:other",
                                  "decision_id": "qq-decision:other", "status": "unknown"}
                with closing(actor.store.connect()) as db, db:
                    db.execute("INSERT INTO qq_direct_sends VALUES(?,?,?,?,?,'unknown',NULL,NULL,?)",
                        (other["send_id"], other["event_id"], other["decision_id"], other["group_id"], 0, canonical(other)))
        return result
    monkeypatch.setattr(actor.store, "update_send", update)
    async def execute():
        await actor.verify_connection()
        await actor.on_event(incoming())
        await actor.process(actor.store.pending(actor.run_id, scheduler_mode="coalesced"))
    asyncio.run(execute())
    assert len(attempts) == 1 and not actor.client.sent
    with closing(actor.store.connect()) as db:
        row = db.execute("SELECT * FROM qq_direct_sends WHERE send_id=?", (attempts[0]["send_id"],)).fetchone()
    assert row["status"] == "cancelled" and row["message_id"] is None
    stored = json.loads(row["record_json"])
    expected = {"revision": "direct_request_revision_changed", "other_unknown": "direct_delivery_unknown"}
    assert stored["cancel_reason"] == expected.get(changed, "direct_owner_unavailable")
    assert stored["cancel_phase"] == "before_protocol_call"
    assert actor.store.uncertain() is (changed == "other_unknown")


@pytest.mark.parametrize("changed", [None, "missing", "modified", "ack", "unknown"])
def test_only_exact_untransmitted_attempt_may_be_exempted(actor_factory, changed):  # noqa: F811
    actor, _, _ = actor_factory()
    async def execute():
        await actor.verify_connection()
        await actor.on_event(incoming())
        row = actor.store.pending(actor.run_id)
        await actor.observe(row["event"], QQDeadline.after(10))
        plan = actor.cognition(group_id=MAIN, trigger_member_id="20001")
        actor.store.update(row["event_id"], state="ready", decision_id=plan.decision_id)
        send = actor.store.reserve(row["event"], plan, 0, plan.reply_segments[0])
        return actor.store.update_send(send, "attempted", attempted_at=utcnow().isoformat())
    send = asyncio.run(execute())
    assert actor.store.uncertain()
    if changed == "missing":
        send = send | {"send_id": "qq-direct-send:missing"}
    elif changed == "modified":
        send = send | {"text": "not the frozen text"}
    elif changed == "ack":
        send = actor.store.update_send(send, "attempted", message_id="800")
    elif changed == "unknown":
        send = actor.store.update_send(send, "unknown")
    assert actor.store.uncertain(untransmitted_send=send) is (changed is not None)
