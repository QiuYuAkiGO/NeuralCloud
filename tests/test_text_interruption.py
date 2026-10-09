"""Finite waiting and durable reply ownership, using synthetic messages only."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from harness.interruption import InterruptionDecision, InterruptionOptions, InterruptionRequest, TextInterruption
from harness.cognitive.interruption import InterruptionAssessor


def result(action="continue", evidence="b"):
    return {"action": action, "reason_code": {"continue": "uncertain", "wait": "explicit_wait", "regenerate": "correction"}[action],
            "evidence_ids": [evidence]}


def engine(tmp_path, judge, **options):
    return TextInterruption(tmp_path / "turns.db", {"mode": "active", "coalesce_seconds": 0, **options}, judge)


@pytest.mark.parametrize("action", ["continue", "regenerate", "wait"])
def test_decision_and_same_scope_only(tmp_path, action):
    async def run():
        requests = []
        async def judge(request):
            requests.append(request)
            return result(action)
        turns = engine(tmp_path, judge)
        turns.begin("text:a", "a", "old request", time.time() + 60)
        turns.receive("text:other", "outside", "bob", "unrelated")
        assert await turns.before_send("a") == "continue"
        turns.receive("text:a", "b", "bob", "correction")
        assert await turns.before_send("a", "candidate") == action
        assert len(requests) == 1 and requests[0].messages == [{"id": "b", "author": "bob", "text": "correction"}]
        if action == "wait":
            assert turns.absorbed("b")
        await turns.close()
    asyncio.run(run())


def test_regeneration_keeps_prefix_quota_deadline_and_survives_restart(tmp_path):
    async def run():
        async def judge(request):
            return {**result("regenerate"), "evidence_ids": [m["id"] for m in request.messages]}
        turns = engine(tmp_path, judge)
        deadline = time.time() + 40
        turns.begin("text:a", "a", "original", deadline)
        turns.delivered("a", ["already visible"])
        turns.receive("text:a", "b", "author", "change")
        turns.receive("text:a", "c", "author", "extra detail")
        assert await turns.before_send("a") == "regenerate"
        assert turns.absorbed("c")
        await turns.close()
        turns = engine(tmp_path, judge)
        row = turns.begin("text:a", "b", "change", time.time() + 60)
        assert (row["deadline"], row["checks"], row["regenerations"]) == (deadline, 1, 1)
        assert turns.continuation("b")["already_delivered"] == ["already visible"]
        assert turns.continuation("b")["original_request"] == "original"
        assert await turns.before_send("a") == "regenerate"
        turns.receive("text:a", "d", "author", "another correction")
        assert await turns.before_send("b") == "continue"
        row = turns.store.get("b")
        assert row["loose"] and row["checks"] == 2 and row["regenerations"] == 1
        await turns.close()
    asyncio.run(run())


def test_flood_releases_all_segments_and_late_judge_cannot_revoke(tmp_path):
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()
        async def judge(request):
            started.set()
            await finish.wait()
            return result("wait")
        turns = engine(tmp_path, judge)
        turns.begin("text:a", "a", "original", time.time() + 60)
        turns.receive("text:a", "b", "author", "more")
        await started.wait()
        for i in range(40):
            turns.receive("text:a", str(i), "other", "busy")
        assert await turns.before_send("a", "segment 1") == "continue"
        turns.delivered("a", ["segment 1"])
        finish.set()
        await asyncio.gather(*turns.tasks.values())
        for i in range(40, 80):
            turns.receive("text:a", str(i), "other", "busy")
        assert await turns.before_send("a", "segment 2") == "continue"
        row = turns.store.get("a")
        assert row["loose"] and row["checks"] == 1 and row["status"] == "active"
        assert len(turns.store.inputs("text:a")) == 20
        with turns.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM interruption_inputs").fetchone()[0] == 82
        await turns.close()
    asyncio.run(run())


def test_valid_wait_not_reopened_by_flood(tmp_path):
    async def run():
        async def judge(request):
            return result("wait")
        turns = engine(tmp_path, judge)
        turns.begin("text:a", "a", "original", time.time() + 60)
        turns.receive("text:a", "b", "author", "please wait")
        assert await turns.before_send("a") == "wait"
        for i in range(8):
            turns.receive("text:a", str(i), "other", "busy")
        assert await turns.before_send("a") == "wait"
        assert not turns.store.get("a")["loose"]
        await turns.close()
    asyncio.run(run())


@pytest.mark.parametrize("response", [None, {}, result("regenerate", "invented"), {**result(), "extra": True}])
def test_invalid_judgment_fails_open_without_consuming_new_message(tmp_path, response):
    async def run():
        async def judge(request):
            return response
        turns = engine(tmp_path, judge)
        turns.begin("text:a", "a", "original", time.time() + 60)
        turns.receive("text:a", "b", "author", "more")
        assert await turns.before_send("a") == "continue"
        assert turns.store.get("a")["loose"]
        assert not turns.absorbed("b")
        await turns.close()
    asyncio.run(run())


def test_hold_timeout_and_restart_never_renew_budget(tmp_path):
    async def run():
        now = [100.]
        async def judge(request):
            await asyncio.Event().wait()
        turns = TextInterruption(tmp_path / "turns.db", {"mode": "active"}, judge, clock=lambda: now[0])
        turns.begin("s", "a", "original", 160)
        turns.receive("s", "b", "author", "more")
        await turns.close()
        now[0] = 103
        turns = TextInterruption(tmp_path / "turns.db", {"mode": "active"}, judge, clock=lambda: now[0])
        turns.begin("s", "a", "original", 999)
        assert await turns.before_send("a") == "continue"
        row = turns.store.get("a")
        assert row["loose"] and row["hold_used"] == 2 and row["deadline"] == 160
        now[0] = 161
        assert await turns.before_send("a") == "expired"
        await turns.close()
    asyncio.run(run())


def test_wall_time_hold_limit_even_when_judge_never_returns(tmp_path):
    async def run():
        async def judge(request):
            await asyncio.Event().wait()
        turns = engine(tmp_path, judge, max_hold_seconds=.1)
        turns.begin("s", "a", "original", time.time() + 60)
        start = time.monotonic()
        turns.receive("s", "b", "author", "more")
        assert await turns.before_send("a") == "continue"
        assert time.monotonic() - start < .8
        assert turns.store.get("a")["hold_used"] <= .1
        await turns.close()
    asyncio.run(run())


def test_stale_results_exhaust_budget_without_rechecking_forever(tmp_path):
    async def run():
        started, complete = asyncio.Queue(), asyncio.Queue()
        async def judge(request):
            await started.put(request)
            await complete.get()
            return result("regenerate", request.messages[-1]["id"])
        turns = engine(tmp_path, judge, burst_count=20)
        turns.begin("s", "a", "original", time.time() + 60)
        turns.receive("s", "b", "author", "more")
        await started.get()
        turns.receive("s", "c", "author", "newer")
        await complete.put(True)
        await started.get()
        turns.receive("s", "d", "author", "newest")
        await complete.put(True)
        assert await turns.before_send("a") == "continue"
        row = turns.store.get("a")
        assert 1 <= row["stale_results"] <= 2 and row["checks"] == 2 and row["loose"]
        await turns.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_nonactive_modes_preserve_delivery(tmp_path, mode):
    async def run():
        async def judge(request):
            return result("wait")
        turns = engine(tmp_path, judge, mode=mode)
        turns.begin("s", "a", "original", time.time() + 60)
        turns.receive("s", "b", "author", "wait")
        assert await turns.before_send("a") == "continue"
        await asyncio.gather(*turns.tasks.values())
        assert not turns.managed("a") and not turns.absorbed("b")
        if mode == "shadow":
            assert turns.store.get("a")["checks"] == 1
        await turns.close()
    asyncio.run(run())


def request():
    return InterruptionRequest(root_id="a", scope="text:test", generation=0, input_sequence=2,
        original_text="original", messages=[{"id": "b", "author": "user", "text": "change"}], timeout_seconds=1)


@pytest.mark.parametrize("raw", ["not json", '{"action":"continue","action":"wait"}',
    json.dumps(result("regenerate", "unknown")), json.dumps({**result(), "evidence_ids": [] , "x": 1})])
def test_assessor_rejects_invalid_structured_output_legacy_mode(legacy_json_mode, raw):
    class LLM:
        def complete_chat_result(self, messages, **kwargs):
            assert kwargs["max_tokens"] == 256
            return SimpleNamespace(text=raw)
    async def run():
        with pytest.raises(ValueError):
            await InterruptionAssessor(LLM()).assess(request())
    asyncio.run(run())


@pytest.mark.parametrize("value", [result("wait") | {"reason_code": "uncertain"}, result("regenerate") | {"evidence_ids": []}])
def test_action_needs_relevant_reason_and_evidence(value):
    with pytest.raises(ValueError):
        InterruptionDecision.model_validate(value)


def test_configuration_has_finite_ceilings():
    assert InterruptionOptions().mode == "off"
    for invalid in ({"max_hold_seconds": float("nan")}, {"max_judgments": 3}, {"max_regenerations": 2}):
        with pytest.raises(ValueError):
            InterruptionOptions(**invalid)


def test_message_during_replacement_is_rechecked_with_remaining_budget(tmp_path):
    async def run():
        async def judge(request):
            return result("regenerate" if request.generation == 0 else "wait", request.messages[-1]["id"])
        turns = engine(tmp_path, judge)
        turns.begin("s", "a", "original", time.time() + 60, author="owner")
        turns.receive("s", "b", "owner", "correction")
        assert await turns.before_send("a") == "regenerate"
        turns.receive("s", "c", "owner", "please wait")
        turns.begin("s", "b", "correction", time.time() + 120)
        assert await turns.before_send("b") == "wait"
        assert turns.store.get("b")["checks"] == 2
        await turns.close()
    asyncio.run(run())


def test_judge_saturation_and_timeout_preserve_physical_concurrency_limit():
    import threading
    release, entered = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = [0]
    class LLM:
        def complete_chat_result(self, messages, **kwargs):
            with lock:
                calls[0] += 1
                if calls[0] == 2:
                    entered.set()
            release.wait(timeout=2)
            return SimpleNamespace(text=json.dumps(result()))
    async def run():
        assessor = InterruptionAssessor(LLM())
        payload = request().model_copy(update={"timeout_seconds": .1})
        tasks = [asyncio.create_task(assessor.assess(payload)) for _ in range(8)]
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            with pytest.raises(RuntimeError, match="queue_full"):
                await assessor.assess(payload)
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(item, TimeoutError) for item in outcomes)
            assert len(assessor.jobs) == 8 and calls[0] == 2
        finally:
            release.set()
            await asyncio.gather(*assessor.jobs, return_exceptions=True)
        assert calls[0] == 2  # Expired queued work never reaches the provider.
    asyncio.run(run())


def test_judge_rpc_does_not_retry_or_extend_transport_deadline():
    import httpx
    from harness.runtime.rpc import HttpCognitiveClient
    calls = []
    def handle(request):
        calls.append(request)
        raise httpx.ReadError("synthetic connection ended", request=request)
    async def run():
        async with httpx.AsyncClient(base_url="http://synthetic", transport=httpx.MockTransport(handle)) as transport:
            client = HttpCognitiveClient("http://synthetic", service_token="synthetic", client=transport)
            with pytest.raises(httpx.ReadError):
                await client.assess_text_interruption(request())
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("action", ["regenerate", "wait"])
def test_only_cited_related_messages_are_absorbed(tmp_path, action):
    async def run():
        async def judge(request):
            return result(action, "related")
        turns = engine(tmp_path, judge)
        turns.begin("group", "a", "original", time.time() + 60)
        turns.receive("group", "unrelated", "someone", "separate question")
        turns.receive("group", "related", "owner", "update this reply")
        assert await turns.before_send("a") == action
        assert turns.store.get("unrelated") is None and not turns.absorbed("unrelated")
        if action == "regenerate":
            assert turns.store.get("related")["current_event"] == "related"
            assert turns.continuation("related")["new_messages"] == [{"id": "related", "author": "owner", "text": "update this reply"}]
        await turns.close()
    asyncio.run(run())
