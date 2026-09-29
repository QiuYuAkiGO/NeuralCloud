"""Authorized autonomous contacts use the original protocol outbox and receipts."""
# ruff: noqa: F811 -- pytest fixtures are imported from the protocol test rig.
import asyncio
from contextlib import closing
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harness.qq import decision
from harness.qq.direct_capabilities import QQDirectCapabilityBridge, SEND
from harness.qq.direct_source import validate_direct_source
from harness.qq.direct_store import utcnow
from harness.qq.shared import QQSharedPrepareRequest, QQSharedTraceOrigin
from tests.test_qq_direct_actor import MAIN, actor_factory, incoming, messages  # noqa: F401
from tests.test_qq_private_actor import PRIVATE, private_actor, private_message  # noqa: F401
from tests.test_qq_decision import setup, isolated_static_parser_cache  # noqa: F401


def bridge_for(actor, monkeypatch):
    path = actor.settings.state_db.with_suffix(".config.json")
    path.write_text(actor.settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG", str(path))
    actor.state["observation_gap"] = False
    actor.write_status()
    supervisor = SimpleNamespace(production_runtime=SimpleNamespace(epoch=0),
        capabilities=SimpleNamespace(owner=lambda _: "shared", generation=2))
    return QQDirectCapabilityBridge(supervisor, actor.session, path, actor.settings.state_db.with_suffix(".caps.sqlite3"))


def enqueue(bridge, group):
    offer = next(item for item in bridge.catalog() if item["capability_id"] == SEND and item["scope"]["group_id"] == group)
    row = {**offer, "opportunity_id": "opportunity:" + uuid4().hex, "generation": 2,
        "selection_ref": "capability-choice:test", "arguments": {},
        "deadline_at": (utcnow() + timedelta(seconds=30)).isoformat(),
        "production_context": {"schema_version": "production-context-v1", "epoch": 0, "restricted_dialogue": False}}
    bridge.enqueue(row)
    return row


def cognition(actor, *, hook=None, speak=True):
    def call(**kwargs):
        intent = kwargs["idle_intent"]
        trace = kwargs["trace"]
        request = QQSharedPrepareRequest(transport="onebot", group_id=intent.group_id,
            self_member_id=intent.self_member_id, idle_intent=intent, deadline_at=intent.deadline_at,
            trace=QQSharedTraceOrigin(trace_id=trace.trace_id, event_id=trace.event_id,
                first_seen_at=trace.first_seen_at, first_seen_mono=trace.first_seen_mono))
        validate_direct_source(actor.session, request)
        sources = messages(actor, intent.group_id)
        result = decision.QQReplyDecision(decision_id="qq-decision:" + uuid4().hex, input_hash=uuid4().hex,
            created_at=utcnow(), action="speak" if speak else "wait", reason="Choose contact from visible evidence",
            initiated_by="production", idle_intent_id=intent.intent_id,
            reply="后来怎么样了？" if speak else None, reply_segments=["后来怎么样了？"] if speak else [],
            decision_source="model", outcome="ready" if speak else "natural_wait",
            group_id=intent.group_id, self_member_id=intent.self_member_id, participants=[],
            source_record_ids=[source.record_id for source in sources], new_observation_record_ids=[],
            evidence_refs=[], delivery_refs=[], model="synthetic", persona_version="continuous", preset_id="misty")
        with closing(decision._connect(actor.session.memory_db)) as db, db:
            db.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)",
                (result.decision_id, result.input_hash, intent.group_id, "completed", result.model_dump_json()))
        if hook:
            hook()
        return result
    return call


def test_production_idle_runs_existing_gate_and_persona_without_semantic_drive(setup):
    from tests.test_qq_idle_decision import current_intent
    from harness.qq.idle import QQIdleIntent
    _, client, _, kwargs, _ = setup
    legacy = current_intent().model_dump(mode="python")
    intent = QQIdleIntent.model_validate(legacy | {"origin": "production", "homeostatic_interoception": None,
        "opportunity_ref": "production:test", "selection_ref": "selection:test", "scheduler_generation": 1})
    result = decision.decide_qq_reply(**kwargs, idle_intent=intent)
    assert result.outcome == "ready" and result.initiated_by == "production"
    assert result.new_observation_record_ids == result.seen_observation_record_ids == []
    assert result.addressing == "ambient" and result.trigger_member_id is None
    payload = decision.combined_user_payload(client.gate_messages[0])
    assert not payload.get("homeostatic_interoception")
    assert payload["initiation"]["origin"] == "production"
    assert "具身主体的自主机会" in client.gate_messages[0][0]["content"]


@pytest.mark.parametrize("private", [False, True])
def test_production_contact_uses_original_sender_scoped_evidence_and_empty_addressed_sources(actor_factory, private_actor, monkeypatch, private):
    actor = private_actor[0] if private else actor_factory()[0]
    group = PRIVATE if private else MAIN

    async def run():
        await actor.verify_connection()
        await actor.on_event(private_message() if private else incoming(), historical=True)
        await actor.process(actor.store.pending(actor.run_id))
        bridge = bridge_for(actor, monkeypatch)
        actor.cognition = cognition(actor)
        row = enqueue(bridge, group)
        assert await actor.process_contact()
        result = bridge.result(row["opportunity_id"])
        assert result["status"] == "completed", result
        assert len(actor.client.sent) == 1
        receipt = decision.get_qq_delivery(memory_db=actor.session.memory_db, group_id=group, decision_id=result["decision_id"])
        assert receipt.source_record_ids == [] and receipt.verification == "qq_protocol_verified"
        verified = bridge.verified_deliveries()
        assert len(verified) == 1 and verified[0][1].decision_id == result["decision_id"]
        assert bridge.verified_deliveries(after=verified[0][0]) == []
        assert len(messages(actor, group)) == 1  # no fabricated incoming observation
        assert not await actor.process_contact()
        assert len(actor.client.sent) == 1
    asyncio.run(run())


def test_contact_cannot_smuggle_body_or_expand_roster(actor_factory, monkeypatch):
    actor = actor_factory()[0]

    async def run():
        await actor.verify_connection()
        await actor.on_event(incoming(), historical=True)
        await actor.process(actor.store.pending(actor.run_id))
        bridge = bridge_for(actor, monkeypatch)
        row = enqueue(bridge, MAIN)
        with pytest.raises(ValueError, match="not_authorized"):
            bridge.enqueue({**row, "opportunity_id": "forged", "arguments": {"text": "private goal secret"}})
        with pytest.raises(ValueError, match="scope_invalid"):
            bridge.enqueue({**row, "opportunity_id": "other", "scope": {**row["scope"], "group_id": "77777"}})
        bridge.revoke_contacts()
        assert bridge.result(row["opportunity_id"])["status"] == "rejected"
        assert not await actor.process_contact()
        assert not actor.client.sent
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["revoked", "wait", "unknown"])
def test_contact_revocation_wait_and_unknown_do_not_trigger_replacement(actor_factory, monkeypatch, outcome):
    actor = actor_factory()[0]

    async def run():
        await actor.verify_connection()
        await actor.on_event(incoming(), historical=True)
        await actor.process(actor.store.pending(actor.run_id))
        bridge = bridge_for(actor, monkeypatch)
        row = enqueue(bridge, MAIN)
        if outcome == "unknown":
            from harness.qq.onebot import OneBotError
            actor.client.send_error = OneBotError("onebot_request_timeout")
        actor.cognition = cognition(actor, hook=bridge.revoke_contacts if outcome == "revoked" else None,
                                   speak=outcome != "wait")
        assert await actor.process_contact()
        result = bridge.result(row["opportunity_id"])
        assert result["status"] == {"revoked": "rejected", "wait": "wait", "unknown": "uncertain"}[outcome]
        count = len([call for call in actor.client.calls if call[0] == "send_group_msg"])
        assert count == (1 if outcome == "unknown" else 0)
        assert not await actor.process_contact()
        assert len([call for call in actor.client.calls if call[0] == "send_group_msg"]) == count
    asyncio.run(run())
