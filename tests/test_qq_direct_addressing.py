"""Freshness exceptions require the exact wire request and frozen source binding."""
import asyncio
from contextlib import closing
import json

import pytest

from harness.qq import decision
from harness.qq.direct_store import normalize_message, verified_protocol_addressing
from harness.qq.onebot import OneBotError
from harness.qq.telemetry import QQDeadline
from tests.test_qq_direct_actor import SELF, MAIN, MEMBER, consume, incoming
from tests.test_qq_direct_actor import actor_factory as actor_factory
from tests.test_qq_private_actor import private_actor as private_actor, private_message


OTHER = "20002"


def addressed(number=1, *, kind="at", member=MEMBER):
    payload = incoming(number, member=member)
    if kind == "text":
        payload["message"][0]["data"]["text"] = f"@{SELF} 你好"
    elif kind == "reply":
        payload["message"].insert(0, {"type": "reply", "data": {"id": "800"}})
    elif kind != "ambient":
        target = OTHER if kind == "other" else "all" if kind == "all" else SELF
        payload["message"].insert(0, {"type": "at", "data": {"qq": target}})
    return payload


def freeze_addressing(actor, cognition, *, source="verified_self_mention", changes=None, persist=True):
    """Freeze a synthetic decision with real observation evidence, without a model call."""
    def transform(result):
        result = result.model_copy(update={"addressing": "direct", "addressing_source": source,
            "addressing_source_record_ids": list(result.source_record_ids),
            "addressing_evidence_refs": list(result.evidence_refs)} | (changes or {}))
        if persist:
            with closing(decision._connect(actor.session.memory_db)) as db, db:
                db.execute("UPDATE qq_reply_decisions SET record_json=? WHERE decision_id=?",
                           (result.model_dump_json(), result.decision_id))
        cognition.plans[-1] = result
        return result
    cognition.transform = transform


@pytest.mark.parametrize("kind,expected", [("at", "self_mention"), ("text", "ambient"),
    ("other", "ambient"), ("all", "ambient"), ("reply", "ambient"), ("ambient", "ambient")])
def test_protocol_addressing_uses_segments_not_rendered_text(kind, expected):
    assert normalize_message(addressed(kind=kind), self_id=SELF,
                             groups=[MAIN])["protocol_addressing"] == expected


@pytest.mark.parametrize("timing", ["queued", "cognition", "between_segments"])
def test_exact_at_request_survives_other_member_chatter(actor_factory, timing):
    actor, cognition, _ = actor_factory()
    freeze_addressing(actor, cognition)
    cognition.texts = ["第一段", "第二段"]
    payload = addressed()

    async def run():
        await actor.verify_connection()
        if timing == "queued":
            await actor.on_event(payload)
            row = actor.store.pending(actor.run_id)
            await actor.on_event(incoming(2, member=OTHER))
            await actor.process(row)
            result = actor.store.get(row["event_id"])
        else:
            if timing == "cognition":
                loop = asyncio.get_running_loop()
                cognition.hook = lambda: asyncio.run_coroutine_threadsafe(
                    actor.on_event(incoming(2, member=OTHER)), loop).result(timeout=2)
            else:
                async def after_send():
                    actor.client.after_send = None
                    await actor.on_event(incoming(2, member=OTHER))
                actor.client.after_send = after_send
            result = await consume(actor, payload)
        await actor.on_event(payload)
        assert actor.store.get(result["event_id"])["state"] == "sent"
        assert len(cognition.calls) == 1 and len(actor.client.sent) == 2
        return result

    row = asyncio.run(run())
    assert row["state"] == "sent"
    assert actor.store.counts()["sends"] == {"sent": 2}


@pytest.mark.parametrize("timing", ["queued", "cognition", "between_segments"])
def test_explicit_request_still_yields_to_original_authors_followup(actor_factory, timing):
    actor, cognition, _ = actor_factory()
    freeze_addressing(actor, cognition)
    cognition.texts = ["第一段", "第二段"]

    async def run():
        await actor.verify_connection()
        if timing == "queued":
            await actor.on_event(addressed())
            row = actor.store.pending(actor.run_id)
            await actor.on_event(incoming(2))
            await actor.process(row)
            return actor.store.get(row["event_id"])
        if timing == "cognition":
            loop = asyncio.get_running_loop()
            cognition.hook = lambda: asyncio.run_coroutine_threadsafe(
                actor.on_event(incoming(2)), loop).result(timeout=2)
        else:
            actor.client.after_send = lambda: actor.on_event(incoming(2))
        return await consume(actor, addressed())

    row = asyncio.run(run())
    assert row["state"] == ("partial" if timing == "between_segments" else "failed")
    assert len(actor.client.sent) == (1 if timing == "between_segments" else 0)
    assert len(cognition.calls) == (0 if timing == "queued" else 1)
    assert actor.state["last_error_type"] == "direct_newer_context"
    assert not actor.store.uncertain()


@pytest.mark.parametrize("kind,source", [("ambient", "gate_direct_followup"),
    ("text", "verified_self_mention"), ("other", "verified_self_mention"),
    ("all", "verified_self_mention"), ("reply", "verified_self_mention"),
    ("at", "gate_direct_followup"), ("at", "verified_private_message")])
def test_model_direct_or_nonself_protocol_cannot_authorize_exception(actor_factory, kind, source):
    actor, cognition, _ = actor_factory()
    freeze_addressing(actor, cognition, source=source)

    async def run():
        await actor.verify_connection()
        loop = asyncio.get_running_loop()
        cognition.hook = lambda: asyncio.run_coroutine_threadsafe(
            actor.on_event(incoming(2, member=OTHER)), loop).result(timeout=2)
        return await consume(actor, addressed(kind=kind))

    assert asyncio.run(run())["state"] == "failed"
    assert not actor.client.sent and actor.state["last_error_type"] == "direct_newer_context"


@pytest.mark.parametrize("changes", [
    {"addressing_source_record_ids": ["qq-observation:another-source"]},
    {"addressing_evidence_refs": ["qq-protocol://sha256/" + "0" * 64]},
    {"new_observation_record_ids": []}, {"trigger_member_id": OTHER},
])
def test_exception_requires_current_observation_and_address_evidence(actor_factory, changes):
    actor, cognition, _ = actor_factory()
    freeze_addressing(actor, cognition, changes=changes)

    async def run():
        await actor.verify_connection()
        loop = asyncio.get_running_loop()
        cognition.hook = lambda: asyncio.run_coroutine_threadsafe(
            actor.on_event(incoming(2, member=OTHER)), loop).result(timeout=2)
        return await consume(actor, addressed())

    assert asyncio.run(run())["state"] == "failed"
    assert not actor.client.sent and actor.state["last_error_type"] == "direct_newer_context"


def test_unfrozen_addressing_cannot_bypass_send_plan_check(actor_factory):
    actor, cognition, _ = actor_factory()
    freeze_addressing(actor, cognition, persist=False)

    async def run():
        await actor.verify_connection()
        loop = asyncio.get_running_loop()
        cognition.hook = lambda: asyncio.run_coroutine_threadsafe(
            actor.on_event(incoming(2, member=OTHER)), loop).result(timeout=2)
        return await consume(actor, addressed())

    assert asyncio.run(run())["state"] == "failed"
    assert not actor.client.sent and actor.state["last_error_type"] == "direct_frozen_plan_mismatch"


@pytest.mark.parametrize("changes", [
    {"addressing_source": "gate_direct_followup"},
    {"addressing_source_record_ids": ["qq-observation:another-source"]},
    {"addressing_evidence_refs": ["qq-protocol://sha256/" + "0" * 64]},
    {"trigger_member_id": OTHER},
])
def test_returned_addressing_fields_must_match_frozen_plan(actor_factory, changes):
    actor, cognition, _ = actor_factory()
    freeze_addressing(actor, cognition)
    freeze = cognition.transform
    cognition.transform = lambda result: freeze(result).model_copy(update=changes)

    async def run():
        await actor.verify_connection()
        return await consume(actor, addressed())

    assert asyncio.run(run())["state"] == "failed"
    assert not actor.client.sent and actor.state["last_error_type"] == "direct_frozen_plan_mismatch"


def test_existing_explicit_addressing_parser_provides_required_bound_proof(actor_factory):
    actor, cognition, _ = actor_factory()
    freeze_addressing(actor, cognition)
    freeze = cognition.transform

    def transform(result):
        context = actor.memory.group_context(group_id=MAIN)
        observations = [item for person in context.members for item in person.observations]
        addressing = decision._explicit_addressing(observations, actor.aliases[MAIN], context)
        frozen = freeze(result)
        assert addressing["addressing"] == "direct"
        assert addressing["addressing_source"] == frozen.addressing_source
        assert addressing["addressing_source_record_ids"] == frozen.addressing_source_record_ids
        assert set(frozen.addressing_evidence_refs).issubset(addressing["addressing_evidence_refs"])
        return frozen

    cognition.transform = transform

    async def run():
        await actor.verify_connection()
        loop = asyncio.get_running_loop()
        cognition.hook = lambda: asyncio.run_coroutine_threadsafe(
            actor.on_event(incoming(2, member=OTHER)), loop).result(timeout=2)
        return await consume(actor, addressed())

    assert asyncio.run(run())["state"] == "sent"


@pytest.mark.parametrize("change", ["hash", "identity", "hint", "missing_hint", "historical", "own"])
def test_wire_proof_ignores_untrusted_hints_and_rejects_changed_evidence(actor_factory, change):
    actor, _, _ = actor_factory()

    async def run():
        await actor.verify_connection()
        await actor.on_event(addressed(kind="text" if change == "hint" else "at"))
        return actor.store.pending(actor.run_id)["event"]

    event = asyncio.run(run())
    if change == "hash":
        digest = event["event_evidence_ref"].rsplit("/", 1)[1]
        (actor.settings.evidence_dir / (digest + ".json")).write_text("{}")
    elif change == "identity":
        event["member_id"] = OTHER
    elif change == "hint":
        event["protocol_addressing"] = "self_mention"
    elif change == "missing_hint":
        event.pop("protocol_addressing")
    else:
        event[change] = True
    assert verified_protocol_addressing(event, evidence_dir=actor.settings.evidence_dir,
        self_id=SELF, groups=actor.groups) == ("self_mention" if change == "missing_hint" else "ambient")


@pytest.mark.parametrize("guard,code", [("deadline", "deadline"), ("epoch", "direct_owner_unavailable"),
    ("pause", "direct_owner_unavailable"), ("unknown", "direct_delivery_unknown")])
def test_explicit_exception_retains_original_send_guards(actor_factory, monkeypatch, guard, code):
    actor, _, _ = actor_factory()

    async def run():
        await actor.verify_connection()
        await actor.on_event(addressed())
        row = actor.store.pending(actor.run_id)
        await actor.on_event(incoming(2, member=OTHER))
        return row["event"]

    event = asyncio.run(run())
    epoch = actor.epoch
    deadline = QQDeadline.after(10)
    if guard == "deadline":
        deadline = QQDeadline.after(0)
    elif guard == "epoch":
        actor.epoch += 1
    elif guard == "pause":
        actor.settings.control_file.write_text(json.dumps({"paused": True, "stop": False}))
    else:
        monkeypatch.setattr(actor.store, "uncertain", lambda: True)
    with pytest.raises((OneBotError, TimeoutError), match=code):
        actor.check_send(event, deadline, epoch)
    assert not actor.client.sent


def test_verified_private_request_remains_bound_to_peer_and_its_followup(private_actor):
    actor, cognition, _ = private_actor
    freeze_addressing(actor, cognition, source="verified_private_message")

    async def run():
        await actor.verify_connection()
        await actor.on_event(private_message())
        row = actor.store.pending(actor.run_id)
        event = row["event"]
        assert event["protocol_addressing"] == "private"
        assert verified_protocol_addressing(event, evidence_dir=actor.settings.evidence_dir,
            self_id=SELF, groups=actor.groups, private_peers=actor.private_peers) == "private"
        assert verified_protocol_addressing(event, evidence_dir=actor.settings.evidence_dir,
            self_id=SELF, groups=actor.groups, private_peers=[]) == "ambient"
        await actor.on_event(incoming(2, member=OTHER))
        loop = asyncio.get_running_loop()
        cognition.hook = lambda: asyncio.run_coroutine_threadsafe(
            actor.on_event(private_message(3)), loop).result(timeout=2)
        await actor.process(row)
        return actor.store.get(row["event_id"])

    assert asyncio.run(run())["state"] == "failed"
    assert not actor.client.sent and actor.state["last_error_type"] == "direct_newer_context"
