"""Obsolete ambient turns retain pixels without delaying live turns with vision."""
import asyncio
import base64
from copy import deepcopy
import hashlib
import json

import pytest

from harness.qq import direct_images
from harness.qq.direct_capabilities import QQDirectCapabilityBridge
from harness.qq.direct_media import media_work
from harness.qq.direct_store import normalize_message
from harness.qq.memory import QQVisualObservation
from harness.qq.telemetry import QQDeadline
from tests.test_qq_direct_actor import (  # noqa: F401
    GUEST, MAIN, MEMBER, SELF, actor_factory, incoming, messages,
)
from tests.test_qq_direct_images import picture
from tests.test_qq_direct_media_cache import ConfiguredVision
from tests.test_qq_private_actor import PRIVATE, private_actor, private_message  # noqa: F401


def event_row(actor, payload):
    event = normalize_message(payload, self_id=SELF, groups=actor.groups,
                              private_peers=actor.private_peers)
    return actor.store.get(event["event_id"])


def mention(payload):
    payload["message"].insert(0, {"type": "at", "data": {"qq": SELF}})
    return payload


def test_picture_burst_keeps_every_source_but_only_latest_turn_uses_vision(actor_factory):  # noqa: F811
    actor, cognition, vision = actor_factory()

    async def run():
        await actor.verify_connection()
        payloads = [incoming(number, images=True) for number in range(1, 9)]
        for payload in payloads:
            await actor.on_event(payload)
        before = [event_row(actor, payload)["event"] for payload in payloads]
        for _ in payloads:
            await actor.process(actor.store.pending(actor.run_id))
        assert actor.store.pending(actor.run_id) is None
        return before, [event_row(actor, payload) for payload in payloads]

    originals, rows = asyncio.run(run())
    assert len(vision.calls) == len(cognition.calls) == len(actor.client.sent) == 1
    assert [row["terminal_reason"] for row in rows] == ["direct_newer_context"] * 7 + ["sent"]
    digest = hashlib.sha256(actor.client.image_bytes.getvalue()).hexdigest()
    assert (actor.settings.image_cache_dir / (digest + ".png")).read_bytes() == actor.client.image_bytes.getvalue()
    for index, (original, row) in enumerate(zip(originals, rows)):
        event = row["event"]
        assert all(event[key] == original[key] for key in original)
        observation = event["bound_observation"]
        assert observation["text"] == original["text"]
        assert observation["capture_id"] == original["event_id"]
        assert observation["observed_at"] == original["message_time"].replace("+00:00", "Z")
        assert original["event_evidence_ref"] in observation["attribution_evidence"]
        image = observation["image_analysis"][0]
        assert image["evidence_ref"] == "qq-direct-image:sha256:" + digest
        # Deferred content is still an exact verifiable protocol observation,
        # not a new inferred message or an unbound image from another author.
        bridge = object.__new__(QQDirectCapabilityBridge)
        bridge.config = actor.session
        bridge._verify(actor.settings, QQVisualObservation.model_validate(observation))
        if index < 7:
            assert image["status"] == "unknown" and image["error"] == "unavailable"
            assert image["description"] is image["ocr_text"] is image["analysis_ref"] is None
            assert any("识图已延后" in value for value in image["uncertainties"])
        else:
            assert image["status"] == "described" and image["ocr_text"]
    traces = [json.loads(line) for line in actor.settings.state_db.with_suffix(".trace.jsonl").read_text().splitlines()]
    assert sum(trace["counters"].get("image_analysis_deferred", 0) for trace in traces) == 7


def test_deferred_unknown_is_not_a_cache_hit_for_later_explicit_request(actor_factory):  # noqa: F811
    actor, _, _ = actor_factory()
    actor.settings = actor.settings.model_copy(update={"qq_media_cache_enabled": True})
    actor.llm = ConfiguredVision()

    async def run():
        await actor.verify_connection()
        old = incoming(1, images=True)
        await actor.on_event(old)
        await actor.on_event(incoming(2))
        await actor.process(event_row(actor, old))
        bound = deepcopy(event_row(actor, old)["event"]["bound_observation"])
        assert not actor.llm.calls and not list(actor.settings.image_cache_dir.rglob("*.json"))
        current = mention(incoming(3, images=True))
        await actor.on_event(current)
        await actor.process(event_row(actor, current))
        assert event_row(actor, old)["event"]["bound_observation"] == bound
        return event_row(actor, current)

    current = asyncio.run(run())
    assert current["state"] == "sent" and len(actor.llm.calls) == 1
    image = current["event"]["bound_observation"]["image_analysis"][0]
    assert image["status"] == "described" and image["analysis_ref"]


@pytest.mark.parametrize("kind", ["explicit", "explicit_hint_changed", "private", "coalesced", "read_only", "history", "own"])
def test_current_explicit_batch_and_observation_only_images_are_not_deferred(
        actor_factory, private_actor, kind):  # noqa: F811
    actor, cognition, vision = private_actor if kind == "private" else actor_factory()
    if kind == "coalesced":
        actor.settings = actor.settings.model_copy(update={"qq_reply_scheduler": "coalesced"})

    async def run():
        await actor.verify_connection()
        if kind == "private":
            first, second = private_message(1, images=True), private_message(2)
        else:
            first = incoming(1, images=True, group=GUEST if kind == "read_only" else MAIN,
                             member=SELF if kind == "own" else MEMBER)
            second = incoming(2, group=first["group_id"])
            if kind.startswith("explicit"):
                mention(first)
        await actor.on_event(first, historical=kind == "history")
        await actor.on_event(second)
        row = actor.store.pending(actor.run_id) if kind == "coalesced" else event_row(actor, first)
        if kind == "explicit_hint_changed":
            row["event"]["protocol_addressing"] = "ambient"
        await actor.process(row)
        return actor.store.get(row["event_id"])

    row = asyncio.run(run())
    assert len(vision.calls) == 1
    image = row["event"]["bound_observation"]["image_analysis"][0]
    assert image["status"] == "described" and image["ocr_text"]
    if kind == "coalesced":
        assert row["state"] == "sent" and len(cognition.calls) == 1
        assert len(messages(actor, MAIN)) == 2


def test_already_bound_ocr_is_never_replaced_by_deferred_unknown(actor_factory):  # noqa: F811
    actor, _, vision = actor_factory()

    async def run():
        await actor.verify_connection()
        payload = incoming(1, images=True)
        await actor.on_event(payload)
        row = event_row(actor, payload)
        bound = await actor.observe(row["event"], QQDeadline.after(60))
        await actor.on_event(incoming(2))
        await actor.process(event_row(actor, payload))
        current = event_row(actor, payload)
        assert current["event"]["bound_observation"] == bound.model_dump(mode="json")
        return current

    assert asyncio.run(run())["terminal_reason"] == "direct_newer_context"
    assert len(vision.calls) == 1


def test_new_ingress_during_acquisition_defers_before_vision(actor_factory):  # noqa: F811
    actor, cognition, vision = actor_factory()
    original_call = actor.client.call

    async def call(action, *args, **kwargs):
        result = await original_call(action, *args, **kwargs)
        if action == "get_image":
            await actor.on_event(incoming(2))
        return result

    actor.client.call = call

    async def run():
        await actor.verify_connection()
        first = incoming(1, images=True)
        await actor.on_event(first)
        await actor.process(event_row(actor, first))
        return event_row(actor, first)

    row = asyncio.run(run())
    assert row["terminal_reason"] == "direct_newer_context"
    assert row["event"]["bound_observation"]["image_analysis"][0]["evidence_ref"]
    assert not vision.calls and not cognition.calls and not actor.client.sent


def test_remaining_images_defer_when_next_message_arrives_during_first_inference(actor_factory):  # noqa: F811
    actor, _, vision = actor_factory()
    complete = vision.complete_chat_result

    async def run():
        await actor.verify_connection()
        loop = asyncio.get_running_loop()
        def describe(*args, **kwargs):
            result = complete(*args, **kwargs)
            asyncio.run_coroutine_threadsafe(actor.on_event(incoming(2)), loop).result(timeout=2)
            return result
        vision.complete_chat_result = describe
        first = incoming(1, images=True)
        first["message"].append(deepcopy(first["message"][-1]))
        await actor.on_event(first)
        await actor.process(event_row(actor, first))
        return event_row(actor, first)

    row = asyncio.run(run())
    assert len(vision.calls) == 1 and row["terminal_reason"] == "direct_newer_context"
    analyses = row["event"]["bound_observation"]["image_analysis"]
    assert [value["status"] for value in analyses] == ["described", "unknown"]
    assert all(value["evidence_ref"] for value in analyses)


def test_queued_deferral_does_not_poison_another_messages_shared_image(tmp_path):
    vision = ConfiguredVision()
    async def run():
        work = media_work()
        await work.vision.acquire()
        changed = False
        queued = asyncio.Event()
        checked = 0
        def defer():
            nonlocal checked
            checked += 1
            queued.set()
            return changed
        async def api(*args, **kwargs):
            return {"base64": base64.b64encode(picture()).decode()}
        async def load(callback=None):
            return await direct_images.load_and_describe_image({"file": "same.png"}, call_api=api,
                cache_dir=tmp_path, llm=vision, deadline=QQDeadline.after(10), defer_analysis=callback)
        old = asyncio.create_task(load(defer))
        await queued.wait()
        # The first validity check passed, but the turn changes while the
        # actual vision slot is occupied. A second request needs the same pixels.
        current = asyncio.create_task(load())
        changed = True
        work.vision.release()
        old_result, current_result = await asyncio.gather(old, current)
        assert checked >= 2
        return old_result, current_result
    old, current = asyncio.run(run())
    assert old.status == "unknown" and old.error == "vision_deferred"
    assert current.status == "described" and current.analysis_ref and len(vision.calls) == 1


def test_successful_cache_artifact_is_retained_even_for_obsolete_caller(tmp_path):
    vision = ConfiguredVision()
    async def api(*args, **kwargs):
        return {"base64": base64.b64encode(picture()).decode()}
    async def load(callback=None):
        return await direct_images.load_and_describe_image({"file": "same.png"}, call_api=api,
            cache_dir=tmp_path, llm=vision, deadline=QQDeadline.after(10), defer_analysis=callback)
    original = asyncio.run(load())
    again = asyncio.run(load(lambda: True))
    assert original == again and original.status == "described" and len(vision.calls) == 1
