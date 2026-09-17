"""Persisted image admission limits preserve complete protocol source bindings."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from harness.qq import direct
from harness.qq.direct_capabilities import QQDirectCapabilityBridge, QQDirectReadArguments, QQDirectReadScope
from harness.qq.direct_config import DirectQQSettings
from harness.qq.direct_source import validate_direct_source
from harness.qq.direct_store import utcnow
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.telemetry import QQDeadline
from tests.test_qq_direct_actor import GUEST, MAIN, MEMBER, SELF, actor_factory, incoming, messages  # noqa: F401
from tests.test_qq_direct_stale_images import event_row, mention


def pictures(number=1, *, count=3, **kwargs):
    payload = incoming(number, images=True, **kwargs)
    payload["message"] += [deepcopy(payload["message"][-1]) for _ in range(count - 1)]
    return payload


def configure(actor, mode, *, scheduler="shadow", allowlist=None, parallel=False):
    actor.settings = DirectQQSettings.model_validate(actor.settings.model_dump() | {
        "qq_image_mode": mode, "qq_reply_scheduler": scheduler,
        "qq_latency_conversations": allowlist, "qq_media_parallel_enabled": parallel})


@pytest.mark.parametrize("mode", ["all", "one", "off"])
def test_image_mode_roundtrips_and_is_independent_of_empty_rollout_allowlist(actor_factory, mode):  # noqa: F811
    actor, _, _ = actor_factory()
    configure(actor, mode, allowlist=[])
    path = actor.settings.state_db.parent / "image-policy.json"
    path.write_text(actor.settings.model_dump_json(), encoding="utf-8")
    restored = DirectQQSettings.read(path)
    assert restored.qq_image_mode == actor.scope_policy(MAIN)["image_mode"] == mode
    assert actor.scope_policy(MAIN)["scheduler"] == "shadow"
    legacy = actor.settings.model_dump()
    legacy.pop("qq_image_mode")
    assert DirectQQSettings.model_validate(legacy).qq_image_mode == "all"


@pytest.mark.parametrize("value", [True, False, 0, 1, "disabled", None, []])
def test_invalid_image_modes_do_not_enable_work(actor_factory, value):  # noqa: F811
    actor, _, _ = actor_factory()
    with pytest.raises(ValueError):
        DirectQQSettings.model_validate(actor.settings.model_dump() | {"qq_image_mode": value})


@pytest.mark.parametrize("mode,expected", [("all", 3), ("one", 1), ("off", 0)])
@pytest.mark.parametrize("parallel", [False, True])
def test_single_message_counts_all_images_but_only_admitted_images_reach_loader(
        actor_factory, mode, expected, parallel):  # noqa: F811
    actor, cognition, vision = actor_factory()
    configure(actor, mode, parallel=parallel)
    async def run():
        await actor.verify_connection()
        raw = pictures()
        await actor.on_event(raw)
        await actor.process(event_row(actor, raw))
        return event_row(actor, raw)
    row = asyncio.run(run())
    assert row["state"] == "sent" and len(cognition.calls) == 1
    assert len(vision.calls) == len([call for call in actor.client.calls if call[0] == "get_image"]) == expected
    observation = row["event"]["bound_observation"]
    assert len(observation["image_analysis"]) == 3 and observation["text"] == row["event"]["text"]
    assert [image["image_index"] for image in observation["image_analysis"]] == [1, 2, 3]
    for image in observation["image_analysis"][expected:]:
        assert image["status"] == "unknown" and image["error"] == "unavailable"
        assert image["evidence_ref"] is image["analysis_ref"] is image["description"] is image["ocr_text"] is None
        assert "未下载或识别" in image["uncertainties"][0]
    trace = json.loads(actor.settings.state_db.with_suffix(".trace.jsonl").read_text().splitlines()[-1])
    assert trace["counters"]["input_image_count"] == 3
    assert trace["counters"]["image_mode_" + mode] == 1
    assert trace["counters"].get("image_policy_skipped", 0) == 3 - expected


def test_off_still_passes_source_verification_and_read_capability_never_reanalyzes(
        actor_factory, monkeypatch):  # noqa: F811
    actor, _, vision = actor_factory()
    configure(actor, "off")
    config_path = actor.settings.state_db.parent / "direct.json"
    config_path.write_text(actor.settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG", str(config_path))
    async def forbidden(*args, **kwargs):
        raise AssertionError("off must not call image acquisition or inference")
    monkeypatch.setattr(direct, "load_and_describe_image", forbidden)
    async def run():
        await actor.verify_connection()
        payload = mention(pictures(count=8))
        await actor.on_event(payload)
        await actor.process(event_row(actor, payload))
        return event_row(actor, payload)
    row = asyncio.run(run())
    assert row["state"] == "sent" and not vision.calls
    event = row["event"]
    first_seen = datetime.fromisoformat(event["received_at"])
    request = QQSharedPrepareRequest(transport="onebot", group_id=MAIN, self_member_id=SELF,
        trigger_member_id=MEMBER, deadline_at=first_seen + timedelta(seconds=60),
        trace={"trace_id": "qq-trace:" + "b" * 32, "event_id": event["event_id"],
               "first_seen_at": first_seen, "first_seen_mono": 0})
    validate_direct_source(actor.session, request, completed_replay=True)
    bridge = object.__new__(QQDirectCapabilityBridge)
    bridge.config, bridge.clock = actor.session, utcnow
    result = bridge._read(actor.settings, QQDirectReadScope(group_id=MAIN, self_member_id=SELF),
                         QQDirectReadArguments(), utcnow() + timedelta(seconds=10))
    retained = result["receipt"]["messages"][0]["observation"]
    assert retained == event["bound_observation"] and not vision.calls
    assert not [call for call in actor.client.calls if call[0] == "get_image"]
    assert not actor.settings.image_cache_dir.exists()


@pytest.mark.parametrize("mode,expected", [("one", 1), ("off", 0)])
def test_one_admission_is_for_entire_coalesced_request_not_each_source(actor_factory, mode, expected):  # noqa: F811
    actor, cognition, vision = actor_factory()
    configure(actor, mode, scheduler="coalesced", parallel=True)
    async def run():
        await actor.verify_connection()
        for number in range(1, 4):
            await actor.on_event(pictures(number))
        row = actor.store.pending(actor.run_id)
        await actor.process(row)
        return row
    row = asyncio.run(run())
    assert len(vision.calls) == expected and len(cognition.calls) == 1
    assert actor.store.get(row["event_id"])["state"] == "sent"
    observations = messages(actor, MAIN)
    assert len(observations) == 3 and sum(len(value.image_analysis) for value in observations) == 9
    assert sum(image.status == "described" for value in observations for image in value.image_analysis) == expected


def test_request_revision_does_not_replenish_first_attachment_slot_or_ttl(actor_factory, monkeypatch):  # noqa: F811
    actor, cognition, vision = actor_factory()
    configure(actor, "one", scheduler="coalesced")
    original_image = direct.load_and_describe_image
    captured = []
    async def image(*args, **kwargs):
        captured.append(kwargs["deadline"].deadline_mono)
        result = await original_image(*args, **kwargs)
        await actor.on_event(pictures(2))
        return result
    async def run():
        await actor.verify_connection()
        await actor.on_event(mention(pictures(1)))
        old = actor.store.pending(actor.run_id)
        monkeypatch.setattr(direct, "load_and_describe_image", image)
        await actor.process(old)
        fresh = actor.store.pending(actor.run_id)
        assert fresh["reply_request"]["revision"] == old["reply_request"]["revision"] + 1
        assert fresh["reply_request"]["deadline_at"] == old["reply_request"]["deadline_at"]
        monkeypatch.setattr(direct, "load_and_describe_image", original_image)
        await actor.process(fresh)
        return fresh
    row = asyncio.run(run())
    assert len(vision.calls) == 1 and len(captured) == 1
    assert len(cognition.calls) == 1 and len(actor.client.sent) == 1
    assert actor.store.get(row["event_id"])["state"] == "sent"
    assert sum(image.status == "described" for value in messages(actor, MAIN) for image in value.image_analysis) == 1


def test_text_anchor_can_admit_first_supplement_image_only(actor_factory):  # noqa: F811
    actor, _, vision = actor_factory()
    configure(actor, "one", scheduler="coalesced")
    async def run():
        await actor.verify_connection()
        await actor.on_event(mention(incoming(1)))
        await actor.on_event(pictures(2))
        await actor.on_event(pictures(3))
        row = actor.store.pending(actor.run_id)
        await actor.process(row)
        return row
    row = asyncio.run(run())
    assert actor.store.get(row["event_id"])["state"] == "sent" and len(vision.calls) == 1
    sources = [actor.store.get(source)["event"]["bound_observation"]
               for source in row["reply_request"]["source_event_ids"]]
    assert sources[0]["image_analysis"] == []
    assert sources[1]["image_analysis"][0]["status"] == "described"
    assert all(image["status"] == "unknown" for image in sources[1]["image_analysis"][1:]
               + sources[2]["image_analysis"])


def test_failed_first_image_does_not_admit_the_next_attachment(actor_factory, monkeypatch):  # noqa: F811
    actor, _, vision = actor_factory()
    configure(actor, "one", scheduler="coalesced")
    calls = []
    async def unavailable(*args, **kwargs):
        calls.append(1)
        return direct.DirectImageObservation(error="download_failed")
    monkeypatch.setattr(direct, "load_and_describe_image", unavailable)
    async def run():
        await actor.verify_connection()
        await actor.on_event(pictures(1))
        await actor.on_event(pictures(2))
        row = actor.store.pending(actor.run_id)
        await actor.process(row)
        return row
    row = asyncio.run(run())
    assert actor.store.get(row["event_id"])["state"] == "sent"
    assert calls == [1] and not vision.calls
    assert all(image.status == "unknown" for value in messages(actor, MAIN) for image in value.image_analysis)


@pytest.mark.parametrize("kind", ["history", "own", "read_only"])
@pytest.mark.parametrize("mode", ["one", "off"])
def test_restricted_background_observation_has_no_media_work(actor_factory, kind, mode):  # noqa: F811
    actor, cognition, vision = actor_factory()
    configure(actor, mode)
    async def run():
        await actor.verify_connection()
        payload = pictures(group=GUEST if kind == "read_only" else MAIN, member=SELF if kind == "own" else MEMBER)
        await actor.on_event(payload, historical=kind == "history")
        row = event_row(actor, payload)
        await actor.process(row)
        return actor.store.get(row["event_id"])
    row = asyncio.run(run())
    assert row["state"] == "observed" and not cognition.calls and not vision.calls
    assert not [call for call in actor.client.calls if call[0] == "get_image"]
    assert all(image["status"] == "unknown" for image in row["event"]["bound_observation"]["image_analysis"])


def test_received_policy_is_frozen_and_old_three_field_policy_is_compatible(actor_factory):  # noqa: F811
    actor, _, vision = actor_factory()
    configure(actor, "off")
    async def run():
        await actor.verify_connection()
        payload = pictures()
        await actor.on_event(payload)
        row = event_row(actor, payload)
        configure(actor, "all")
        assert actor.event_policy(row["event"])["image_mode"] == "off"
        await actor.process(row)
        return row
    row = asyncio.run(run())
    assert not vision.calls
    old = deepcopy(row["event"])
    old["_direct_policy"].pop("image_mode")
    assert actor.event_policy(old)["image_mode"] == "all"


def test_off_reuses_frozen_ocr_without_mutating_it_or_starting_more_work(actor_factory):  # noqa: F811
    actor, _, vision = actor_factory()
    async def run():
        await actor.verify_connection()
        payload = pictures()
        await actor.on_event(payload)
        await actor.observe(event_row(actor, payload)["event"], QQDeadline.after(10))
        row = event_row(actor, payload)
        before = deepcopy(row["event"]["bound_observation"])
        configure(actor, "off")
        result = await actor.observe(row["event"], QQDeadline.after(10), image_admission=frozenset())
        assert result.model_dump(mode="json") == before
        assert event_row(actor, payload)["event"]["bound_observation"] == before
    asyncio.run(run())
    assert len(vision.calls) == 3


def test_off_startup_does_not_require_vision_model_configuration(actor_factory, monkeypatch):  # noqa: F811
    actor, cognition, _ = actor_factory()
    configure(actor, "off")
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled image mode must not construct a vision client")
    monkeypatch.setattr(direct, "load_vision_settings", forbidden)
    replacement = direct.DirectQQActor(actor.settings, cognition=cognition)
    assert replacement.llm is None
    assert replacement.state["images_enabled"] is False
    assert replacement.state["qq_image_mode"] == "off"
