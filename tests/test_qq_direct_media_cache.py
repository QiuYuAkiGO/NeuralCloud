"""Local-only cache identity, shared-work bounds and message evidence regressions."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from harness.qq import direct_images as images, direct_media as media
from harness.qq.direct_store import equivalent_image_segments, normalize_message
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from tests.test_qq_direct_actor import SELF, MAIN, actor_factory, incoming  # noqa: F401
from tests.test_qq_direct_images import Vision, picture


class ConfiguredVision(Vision):
    def __init__(self, *, version="vision-revision-1", **kwargs):
        super().__init__(**kwargs)
        self.settings = SimpleNamespace(llm_model="synthetic-vision", model_version=version,
            llm_base_url="https://synthetic.invalid/v1", call_log_enabled=False, record_interaction=False)


async def load(cache, vision, *, data=None, deadline=None, api=None, trace=None, http_client=None):
    async def default_api(*args, **kwargs):
        return {"base64": base64.b64encode(data if data is not None else picture()).decode()}
    return await images.load_and_describe_image({"file": "synthetic.png"},
        cache_dir=cache, llm=vision, call_api=api or default_api,
        deadline=deadline or QQDeadline.after(10), trace=trace, http_client=http_client)


def test_verified_bytes_reuse_immutable_analysis_and_skip_decode(tmp_path, monkeypatch):
    original_inspect = images._inspect
    decoded = []
    def inspect(*args):
        decoded.append(1)
        return original_inspect(*args)
    monkeypatch.setattr(images, "_inspect", inspect)
    first, second = ConfiguredVision(), ConfiguredVision()
    a = asyncio.run(load(tmp_path, first))
    path = tmp_path / "analysis" / (a.analysis_ref.rsplit(":", 1)[1] + ".json")
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    a.description = "caller-local mutation"
    a.uncertainties.append("caller-local mutation")
    b = asyncio.run(load(tmp_path, second))
    assert len(first.calls) == 1 and not second.calls and len(decoded) == 1
    assert b.status == "described" and b.description != a.description
    assert b.analysis_ref == a.analysis_ref and b.uncertainties == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    contents = b"".join(item.read_bytes() for item in tmp_path.rglob("*.json"))
    assert b"synthetic.invalid" not in contents and b"synthetic-vision" not in contents


@pytest.mark.parametrize("change", ["model", "version", "endpoint", "prompt", "processing", "bytes", "extra_payload"])
def test_model_prompt_processing_and_pixel_changes_invalidate(tmp_path, monkeypatch, change):
    first, second = ConfiguredVision(), ConfiguredVision()
    a = asyncio.run(load(tmp_path, first))
    data = None
    if change == "model":
        second.settings.llm_model = "another-model"
    elif change == "version":
        second.settings.model_version = "vision-revision-2"
    elif change == "endpoint":
        second.settings.llm_base_url = "https://another.invalid/v1"
    elif change == "prompt":
        monkeypatch.setattr(images, "_PROMPT", images._PROMPT + " extra")
    elif change == "processing":
        monkeypatch.setattr(images, "MAX_FRAME_EDGE", images.MAX_FRAME_EDGE - 1)
    elif change == "extra_payload":
        second._extra_payload = {"options": {"num_ctx": 1234}}
    else:
        data = picture(size=(30, 30))
    b = asyncio.run(load(tmp_path, second, data=data))
    assert b.status == "described" and len(second.calls) == 1
    assert b.analysis_ref != a.analysis_ref


@pytest.mark.parametrize("version", ["mock", "live", "unknown", "latest", "ollama", "demo", None, ""])
def test_unversioned_model_cache_does_not_survive_process_epoch(tmp_path, monkeypatch, version):
    vision = ConfiguredVision(version=version)
    a = asyncio.run(load(tmp_path, vision))
    b = asyncio.run(load(tmp_path, vision))
    assert a.analysis_ref == b.analysis_ref and len(vision.calls) == 1
    monkeypatch.setattr(media, "_PROCESS_MODEL_EPOCH", "next-process")
    c = asyncio.run(load(tmp_path, vision))
    assert c.analysis_ref != a.analysis_ref and len(vision.calls) == 2


def test_failed_analysis_retries_and_corrupt_artifact_is_not_trusted(tmp_path):
    vision = ConfiguredVision(error=RuntimeError("synthetic failure"))
    failed = asyncio.run(load(tmp_path, vision))
    assert failed.status == "unknown" and failed.error == "vision_failed"
    assert not list(tmp_path.rglob("*.json"))
    vision.error = None
    success = asyncio.run(load(tmp_path, vision))
    path = tmp_path / "analysis" / (success.analysis_ref.rsplit(":", 1)[1] + ".json")
    path.write_text("corrupted", encoding="utf-8")
    retry = asyncio.run(load(tmp_path, vision))
    assert retry.status == "described" and len(vision.calls) == 3
    assert retry.analysis_ref is None
    assert path.read_text(encoding="utf-8") == "corrupted"


def test_same_bytes_single_flight_while_acquisition_still_verifies_each_source(tmp_path):
    calls = []
    vision = ConfiguredVision()
    original = vision.complete_chat_result
    def slow(*args, **kwargs):
        time.sleep(.05)
        return original(*args, **kwargs)
    vision.complete_chat_result = slow
    async def api(*args, **kwargs):
        calls.append(1)
        return {"base64": base64.b64encode(picture()).decode()}
    async def execute():
        return await asyncio.gather(*(load(tmp_path, vision, api=api) for _ in range(6)))
    results = asyncio.run(execute())
    assert len(calls) == 6 and len(vision.calls) == 1
    assert len({result.analysis_ref for result in results}) == 1
    assert all(result.status == "described" for result in results)
    assert len({id(result) for result in results}) == 6


def test_acquisition_decode_two_and_vision_one_bounds(tmp_path, monkeypatch):
    lock = threading.Lock()
    active = {"acquire": 0, "decode": 0, "vision": 0}
    peaks = dict(active)
    def enter(name):
        with lock:
            active[name] += 1
            peaks[name] = max(peaks[name], active[name])
    def leave(name):
        with lock:
            active[name] -= 1
    original_inspect = images._inspect
    def inspect(*args):
        enter("decode")
        try:
            time.sleep(.03)
            return original_inspect(*args)
        finally:
            leave("decode")
    monkeypatch.setattr(images, "_inspect", inspect)
    vision = ConfiguredVision()
    original = vision.complete_chat_result
    def infer(*args, **kwargs):
        enter("vision")
        try:
            time.sleep(.03)
            return original(*args, **kwargs)
        finally:
            leave("vision")
    vision.complete_chat_result = infer
    async def one(index):
        async def api(*args, **kwargs):
            enter("acquire")
            try:
                await asyncio.sleep(.03)
                return {"base64": base64.b64encode(picture(size=(24 + index, 24))).decode()}
            finally:
                leave("acquire")
        return await load(tmp_path, vision, api=api)
    async def execute():
        return await asyncio.gather(*(one(index) for index in range(6)))
    values = asyncio.run(execute())
    assert all(value.status == "described" for value in values)
    assert peaks["acquire"] == 2 and 1 <= peaks["decode"] <= 2 and peaks["vision"] == 1


def test_cancelled_waiter_does_not_cancel_shared_vision(tmp_path):
    started, release = threading.Event(), threading.Event()
    vision = ConfiguredVision()
    original = vision.complete_chat_result
    def infer(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    vision.complete_chat_result = infer
    async def execute():
        first = asyncio.create_task(load(tmp_path, vision))
        await asyncio.to_thread(started.wait, 3)
        second = asyncio.create_task(load(tmp_path, vision))
        await asyncio.sleep(.05)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        return await second
    result = asyncio.run(execute())
    assert result.status == "described" and len(vision.calls) == 1


def test_expired_vision_is_not_cached_and_keeps_slot_until_thread_finishes(tmp_path):
    started, release = threading.Event(), threading.Event()
    vision = ConfiguredVision()
    original = vision.complete_chat_result
    def infer(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    vision.complete_chat_result = infer
    async def execute():
        def forbidden_network(request):
            raise AssertionError("synthetic get_image bytes must not use HTTP")
        # This test expires an already-running model call. Construct the local
        # transport before its one-second budget; host SSL setup is unrelated
        # and can itself take over a second without making a network request.
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_network), trust_env=False) as client:
            first = asyncio.create_task(load(tmp_path, vision, deadline=QQDeadline.after(1), http_client=client))
            assert await asyncio.to_thread(started.wait, 3)
            expired = await first
            assert expired.status == "unknown" and expired.error == "deadline_expired"
            assert media.media_work().vision.locked()
            release.set()
            for _ in range(100):
                if not media.media_work().vision.locked():
                    break
                await asyncio.sleep(.005)
            assert not list(tmp_path.rglob("*.json"))
            vision.complete_chat_result = original
            return await load(tmp_path, vision, http_client=client)
    success = asyncio.run(execute())
    assert success.status == "described" and len(vision.calls) == 2


def test_image_stage_trace_contains_only_stage_names_and_counts(tmp_path):
    trace = QQTurnTrace()
    asyncio.run(load(tmp_path, ConfiguredVision(), trace=trace))
    snapshot = trace.snapshot()
    assert {item["name"] for item in snapshot["stages"]} >= {
        "image.acquire_queue", "image.acquire", "image.decode", "image.store",
        "image.vision_queue", "image.vision", "image.cache_read", "image.cache_write"}
    assert snapshot["counters"]["image_cache_misses"] == 1
    serialized = json.dumps(snapshot)
    assert "synthetic.invalid" not in serialized and "description" not in serialized


def test_same_analysis_preserves_distinct_message_author_and_wire_binding(actor_factory):  # noqa: F811 - pytest fixture
    actor, _, _ = actor_factory()
    actor.settings = actor.settings.model_copy(update={"qq_media_cache_enabled": True, "qq_media_parallel_enabled": True})
    actor.llm = ConfiguredVision()
    async def execute():
        await actor.verify_connection()
        observations = []
        for number, member in [(101, "20001"), (102, "20002")]:
            payload = incoming(number, images=True, member=member)
            await actor.on_event(payload)
            event = normalize_message(payload, self_id=SELF, groups=[MAIN])
            row = actor.store.get(event["event_id"])
            observations.append(await actor.observe(row["event"], QQDeadline.after(10)))
        return observations
    first, second = asyncio.run(execute())
    assert len(actor.llm.calls) == 1
    assert first.record_id != second.record_id and first.member_id != second.member_id
    assert first.attribution_evidence != second.attribution_evidence
    assert first.capture_id != second.capture_id
    shared = set(first.evidence) & set(second.evidence)
    assert len(shared) == 1
    assert first.image_analysis[0].analysis_ref == second.image_analysis[0].analysis_ref
    assert first.image_analysis[0].analysis_ref.startswith("qq-image-analysis:sha256:")
    assert any(ref.startswith("qq-direct-image:sha256:") for ref in shared)


def test_rotating_rkey_duplicate_keeps_original_event_and_both_raw_evidence(actor_factory):  # noqa: F811 - pytest fixture
    actor, _, _ = actor_factory()
    payload = incoming(images=True)
    payload["message"][-1]["data"]["url"] = "https://multimedia.nt.qq.com/download?fileid=ABC&rkey=old"
    newer = deepcopy(payload)
    newer["message"][-1]["data"]["url"] = "https://multimedia.nt.qq.com/download?fileid=ABC&rkey=new"
    async def execute():
        await actor.on_event(payload)
        event = normalize_message(payload, self_id=SELF, groups=[MAIN])
        before = actor.store.get(event["event_id"])
        await actor.on_event(newer, historical=True)
        return before, actor.store.get(event["event_id"])
    before, after = asyncio.run(execute())
    assert before == after
    assert actor.state.get("invalid_event_count", 0) == 0
    assert len(list(actor.settings.evidence_dir.glob("*.json"))) == 2


@pytest.mark.parametrize("change", ["file", "host", "path", "parameter", "metadata", "missing_file", "http", "credentials"])
def test_rkey_equivalence_does_not_weaken_image_identity(change):
    first = [{"file": "same.png", "url": "https://multimedia.nt.qq.com/download?fileid=ABC&rkey=old", "subType": 0}]
    second = deepcopy(first)
    second[0]["url"] = second[0]["url"].replace("rkey=old", "rkey=new")
    assert equivalent_image_segments(first, second)
    if change == "file":
        second[0]["file"] = "different.png"
    elif change == "metadata":
        second[0]["subType"] = 1
    elif change == "missing_file":
        first[0].pop("file")
        second[0].pop("file")
    else:
        substitutions = {"host": ("multimedia.nt.qq.com", "example.com"),
            "path": ("download", "another"), "parameter": ("fileid=ABC", "fileid=DEF"),
            "http": ("https:", "http:"), "credentials": ("https://", "https://user:secret@")}
        old, new = substitutions[change]
        second[0]["url"] = second[0]["url"].replace(old, new)
    assert not equivalent_image_segments(first, second)
