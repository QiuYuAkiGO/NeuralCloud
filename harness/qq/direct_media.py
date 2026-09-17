"""Bounded media work and immutable, content-addressed successful analyses."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import hashlib
import heapq
from itertools import count as sequence
import json
import os
from pathlib import Path
import tempfile
import weakref
from uuid import uuid4


_PROCESS_MODEL_EPOCH = uuid4().hex


def stage(trace, name):
    return trace.stage(name) if trace is not None else nullcontext()


def count(trace, name):
    if trace is not None:
        trace.add_counter(name)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(value):
    return hashlib.sha256(encode(value)).hexdigest()


def analysis_key(llm, image_digest, *, prompt, processing):
    settings = getattr(llm, "settings", None)
    model = getattr(settings, "llm_model", None)
    endpoint = getattr(settings, "llm_base_url", None)
    if not isinstance(model, str) or not model or not isinstance(endpoint, str) or not endpoint:
        # An injected client without a stable model identity cannot populate a
        # persistent cache that a different implementation might later trust.
        return None
    version = getattr(settings, "model_version", None)
    if not isinstance(version, str) or version.strip().lower() in {"", "mock", "unknown", "latest", "live", "ollama", "demo"}:
        # Artifacts remain durable evidence, but an unversioned model's cache
        # cannot survive this process. Rotating credentials are never retained.
        version = "process:" + _PROCESS_MODEL_EPOCH
    try:
        return fingerprint({"schema": "qq-image-analysis-v1", "image_sha256": image_digest,
            "model": model, "endpoint": endpoint.rstrip("/"),
            "model_version": version, "prompt": prompt,
            "extra_payload": getattr(llm, "_extra_payload", {}),
            "processing": processing, "temperature": 0.0, "max_tokens": 3072,
            "response_format": {"type": "json_object"}})
    except (TypeError, ValueError):
        return None


def _read(path):
    if path.is_symlink() or path.stat().st_size > 128_000:
        raise ValueError("media_artifact_invalid")
    return path.read_bytes()


def load_analysis(cache_dir, key, image_digest):
    if key is None:
        return None
    try:
        index = json.loads(_read(Path(cache_dir) / "analysis-index" / (key + ".json")))
        digest = index["artifact_sha256"]
        if (index.get("key") != key or not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            return None
        raw = _read(Path(cache_dir) / "analysis" / (digest + ".json"))
        record = json.loads(raw)
        if (hashlib.sha256(raw).hexdigest() != digest or record.get("key") != key
                or record.get("schema") != "qq-image-analysis-v1"
                or record.get("image_sha256") != image_digest):
            return None
        return record["observation"] | {"analysis_ref": "qq-image-analysis:sha256:" + digest}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _publish(path, data):
    """An atomic first-writer publication; existing artifacts are never rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".media-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink():
                raise OSError("media_artifact_invalid") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_analysis(cache_dir, key, image_digest, observation):
    if key is None or observation.get("status") != "described" or observation.get("error") is not None:
        return None
    record = {"schema": "qq-image-analysis-v1", "key": key, "image_sha256": image_digest,
              "observation": {k: v for k, v in observation.items() if k != "analysis_ref"}}
    raw = encode(record)
    digest = hashlib.sha256(raw).hexdigest()
    _publish(Path(cache_dir) / "analysis" / (digest + ".json"), raw)
    _publish(Path(cache_dir) / "analysis-index" / (key + ".json"),
             encode({"key": key, "artifact_sha256": digest}))
    return load_analysis(cache_dir, key, image_digest)


class PrioritySemaphore:
    """FIFO within priority; already running I/O/model calls are not preempted."""
    def __init__(self, value):
        self._value = value
        self._waiters = []
        self._sequence = sequence()

    def locked(self):
        return self._value == 0

    async def acquire(self, priority=0):
        if self._value:
            self._value -= 1
            return
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (priority, next(self._sequence), future))
        try:
            await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                self.release()
            raise

    def release(self):
        while self._waiters:
            _, _, future = heapq.heappop(self._waiters)
            if not future.done():
                future.set_result(None)
                return
        self._value += 1


class MediaWork:
    def __init__(self):
        self.acquisition = PrioritySemaphore(2)
        self.vision = PrioritySemaphore(1)
        self.flights = {}
        self.tasks = set()
        self.closing = False
        self.closed = False

    @staticmethod
    def _consume(task):
        if not task.cancelled():
            task.exception()

    async def limited(self, semaphore, work, deadline, trace, queue_stage, *, priority=0):
        async def run():
            with stage(trace, queue_stage):
                await semaphore.acquire(priority)
            try:
                if self.closing:
                    raise asyncio.CancelledError
                deadline.check()
                return await work()
            finally:
                semaphore.release()
        # A cancelled waiter must not release a slot while its to_thread model
        # call is still running, nor cancel a second subscriber's shared work.
        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(self._consume)
        return await asyncio.wait_for(asyncio.shield(task), deadline.check())

    async def close(self):
        # Do not close a shared HTTP client under an active to_thread request.
        # Existing calls retain their original bounded timeouts; queued calls
        # exit without starting, then the actor may close the client safely.
        self.closing = True
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        await asyncio.gather(*tuple(self.flights.values()), return_exceptions=True)
        self.closed = True

    async def single_flight(self, key, work, deadline, trace):
        task = self.flights.get(key)
        if task is None:
            task = asyncio.create_task(work())
            self.flights[key] = task
            def complete(done):
                if self.flights.get(key) is done:
                    self.flights.pop(key, None)
                self._consume(done)
            task.add_done_callback(complete)
        else:
            count(trace, "image_single_flight_hits")
        return await asyncio.wait_for(asyncio.shield(task), deadline.check())


_LOOPS = weakref.WeakKeyDictionary()


def media_work():
    loop = asyncio.get_running_loop()
    if loop not in _LOOPS or _LOOPS[loop].closed:
        _LOOPS[loop] = MediaWork()
    return _LOOPS[loop]


async def close_media_work():
    work = _LOOPS.get(asyncio.get_running_loop())
    if work is not None:
        await work.close()
