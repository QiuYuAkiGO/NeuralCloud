"""Media rollout switches, queued priority, shutdown and atomic observation writes."""
import asyncio
from contextlib import closing
from copy import deepcopy
import json

import pytest

from harness.config import Settings
from harness.qq.direct_media import MediaWork
from harness.qq.direct_store import normalize_message
from harness.qq.shared import SharedQQUnavailable, _import_remote_trace
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from tests.test_qq_direct_actor import SELF, MAIN, actor_factory, incoming  # noqa: F401
from tests.test_qq_direct_media_cache import ConfiguredVision


def test_cache_disabled_performs_independent_inference_without_artifacts(tmp_path):
    from harness.qq import direct_images
    import base64
    from tests.test_qq_direct_images import picture
    vision = ConfiguredVision()
    async def api(*args, **kwargs):
        return {"base64": base64.b64encode(picture()).decode()}
    async def execute():
        return await asyncio.gather(*(direct_images.load_and_describe_image({"file": "same.png"},
            call_api=api, cache_dir=tmp_path, llm=vision, deadline=QQDeadline.after(10), cache_enabled=False)
            for _ in range(2)))
    values = asyncio.run(execute())
    assert all(value.status == "described" and value.analysis_ref is None for value in values)
    assert len(vision.calls) == 2 and not list(tmp_path.rglob("*.json"))


def test_priority_prefers_live_over_queued_history_without_preempting_running():
    async def execute():
        work = MediaWork()
        started, release = asyncio.Event(), asyncio.Event()
        order = []
        async def first():
            order.append("running_history")
            started.set()
            await release.wait()
        async def record(name):
            order.append(name)
        a = asyncio.create_task(work.limited(work.vision, first, QQDeadline.after(5), None, "queue", priority=10))
        await started.wait()
        b = asyncio.create_task(work.limited(work.vision, lambda: record("queued_history"), QQDeadline.after(5), None, "queue", priority=10))
        c = asyncio.create_task(work.limited(work.vision, lambda: record("live"), QQDeadline.after(5), None, "queue", priority=0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(a, b, c)
        return order
    assert asyncio.run(execute()) == ["running_history", "live", "queued_history"]


def test_close_drains_running_work_and_prevents_queued_work_starting():
    async def execute():
        work = MediaWork()
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def first():
            started.set()
            await release.wait()
            calls.append("finished")
        async def queued():
            calls.append("must_not_start")
        a = asyncio.create_task(work.limited(work.vision, first, QQDeadline.after(5), None, "queue"))
        await started.wait()
        b = asyncio.create_task(work.limited(work.vision, queued, QQDeadline.after(5), None, "queue"))
        closing = asyncio.create_task(work.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
        await a
        with pytest.raises(asyncio.CancelledError):
            await b
        assert work.closed and calls == ["finished"]
    asyncio.run(execute())


def test_profile_and_observation_rollback_together(actor_factory, monkeypatch):  # noqa: F811
    actor, _, _ = actor_factory()
    original = actor.memory._write_observation
    def fail(*args):
        raise RuntimeError("synthetic write failure")
    async def execute():
        await actor.verify_connection()
        payload = incoming()
        await actor.on_event(payload)
        event = normalize_message(payload, self_id=SELF, groups=[MAIN])
        event = actor.store.get(event["event_id"])["event"]
        monkeypatch.setattr(actor.memory, "_write_observation", fail)
        with pytest.raises(RuntimeError):
            await actor.observe(event, QQDeadline.after(60))
        with closing(actor.memory._connect()) as db:
            assert db.execute("SELECT COUNT(*) FROM qq_visual_profiles").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM qq_visual_observations").fetchone()[0] == 0
        monkeypatch.setattr(actor.memory, "_write_observation", original)
        return await actor.observe(event, QQDeadline.after(60))
    result = asyncio.run(execute())
    assert result.author_attribution == "qq_protocol_verified"
    with closing(actor.memory._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM qq_visual_profiles").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM qq_visual_observations").fetchone()[0] == 1


def test_eight_images_keep_all_artifact_refs_and_bound_reuse_avoids_inference(actor_factory):  # noqa: F811
    actor, _, _ = actor_factory()
    actor.settings = actor.settings.model_copy(update={"qq_media_cache_enabled": True, "qq_media_parallel_enabled": True})
    actor.llm = ConfiguredVision()
    async def execute():
        await actor.verify_connection()
        payload = incoming(images=True)
        payload["message"] += [deepcopy(payload["message"][-1]) for _ in range(7)]
        await actor.on_event(payload)
        event = normalize_message(payload, self_id=SELF, groups=[MAIN])
        first = await actor.observe(actor.store.get(event["event_id"])["event"], QQDeadline.after(60))
        actor.settings = actor.settings.model_copy(update={"qq_media_cache_enabled": False})
        actor.llm.error = RuntimeError("bound source must not be inferred again")
        second = await actor.observe(actor.store.get(event["event_id"])["event"], QQDeadline.after(60))
        return first, second
    first, second = asyncio.run(execute())
    assert first == second and len(first.image_analysis) == 8 and len(actor.llm.calls) == 1
    assert all(item.analysis_ref for item in first.image_analysis) and len(first.evidence) <= 16


@pytest.mark.parametrize("corruption", [None, "credential_field", "credential_entry", "source", "version", "policy", "changed"])
def test_remote_effective_configuration_is_allowlisted_and_immutable(corruption):
    origin = QQTurnTrace(first_seen_mono=0, clock=lambda: 1)
    origin.bind_configuration(Settings(_env_file=None))
    remote = origin.snapshot()
    target = QQTurnTrace(first_seen_mono=0, clock=lambda: 1)
    remote["trace_id"] = target.trace_id
    if corruption == "credential_field":
        remote["effective_configuration"]["api_key"] = "must-not-copy"
    elif corruption == "credential_entry":
        remote["effective_configuration"]["values"]["llm_api_key"] = {"value": "must-not-copy", "source": "runtime"}
    elif corruption == "source":
        remote["effective_configuration"]["values"]["llm_model"]["source"] = "unknown-source"
    elif corruption == "version":
        remote["effective_configuration"]["version"] = "qq-config:" + "0" * 64
    elif corruption == "policy":
        remote["effective_configuration"]["policy_version"] = "qq-policy:" + "0" * 64
    elif corruption == "changed":
        target.bind_configuration(Settings(_env_file=None, llm_model="different"))
    if corruption:
        with pytest.raises(SharedQQUnavailable):
            _import_remote_trace(target, remote, started=0, ended=1)
        assert "must-not-copy" not in json.dumps(target.snapshot())
    else:
        _import_remote_trace(target, remote, started=0, ended=1)
        assert target.snapshot()["effective_configuration"] == remote["effective_configuration"]
        remote["effective_configuration"]["values"]["llm_model"]["value"] = "mutated"
        assert target.snapshot()["effective_configuration"] != remote["effective_configuration"]
