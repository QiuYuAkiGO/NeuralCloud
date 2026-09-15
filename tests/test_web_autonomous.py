"""Autonomous web observations cross real cognition, authority and HTTP adapters."""

import asyncio
from datetime import timedelta
import json
import time

from harness.core.inference import TokenUsage
from harness.llm.client import ChatResult
from harness.runtime.models import RuntimeEventType, utc_now
from harness.runtime.web import READ, SEARCH, WebCallStore
from tests.test_autonomous_capabilities import rig as autonomous_rig
from tests.test_web_runtime import rig as web_rig


class WebOwnerModel:
    def __init__(self):
        self.chat_calls = []
        self.inputs = []

    def complete_chat_result(self, messages, **kwargs):
        self.chat_calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        self.inputs.append(payload)
        offers = {item["capability_id"]: item for item in payload["available_offers"]}
        if len(self.inputs) == 1:
            choice = {"kind": "observe", "offer_id": offers[SEARCH]["offer_id"],
                      "arguments": {"query": "find public evidence"}}
        else:
            sources = offers[READ]["observation"]["sources"]
            assert sources[0]["snippet"] == "Returned snippet"
            choice = {"kind": "observe", "offer_id": offers[READ]["offer_id"],
                      "arguments": {"source_refs": [sources[0]["source_ref"]]}}
        return ChatResult(text=json.dumps(choice), usage=TokenUsage(prompt_tokens=100, completion_tokens=30, total_tokens=130))


async def next_opportunity(supervisor):
    for row in supervisor.capabilities.calls.all():
        row["created_at"] = (utc_now() - timedelta(seconds=supervisor.capabilities.settings.opportunity_seconds + 1)).isoformat()
        supervisor.capabilities.calls.put(row)
    supervisor.capabilities.last_activity = time.monotonic() - 61
    await supervisor.capabilities.heartbeat()
    event = await supervisor.queue.get(timeout=0)
    assert event is not None, supervisor.capabilities.last_error
    assert event.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY
    return event


def test_autonomous_search_then_read_returns_evidence_without_homeostatic_reward_or_replay(tmp_path, monkeypatch):
    async def run():
        async with autonomous_rig(tmp_path, monkeypatch, [{"kind": "no_action"}]) as values:
            supervisor, host, engine, _, initial_event, board_requests, _ = values
            assert (await supervisor.dispatch(initial_event)).status == "completed"
            fake, requests = web_rig(tmp_path / "web-state")
            supervisor.manifest = supervisor.manifest.model_copy(update={
                "web_access": fake.manifest.web_access,
                "autonomous_capabilities": fake.manifest.autonomous_capabilities})
            supervisor.web = fake.web
            supervisor.web.supervisor = supervisor
            model = WebOwnerModel()
            host.chat.llm = model
            baseline = engine.snapshot.model_dump(mode="json")
            session = host.chat.pipeline.session_store.snapshot()
            try:
                first = await next_opportunity(supervisor)
                outcome = await supervisor.dispatch(first)
                assert outcome.status == "completed", outcome.detail
                first_row = supervisor.capabilities.calls.get(first.payload["opportunity_id"])
                assert first_row["status"] == "completed", first_row
                assert first_row["channel_result"]["effect"] == "external_information_only"
                first_observation = first_row["channel_result"]["observation"]
                assert first_observation["sources"][0]["snippet"] == "Returned snippet"
                assert first_row["evidence_refs"] and first_observation["sources"][0]["origin"] == "search"
                assert requests[0].url.path == "/search"

                second = await next_opportunity(supervisor)
                outcome = await supervisor.dispatch(second)
                assert outcome.status == "completed", outcome.detail
                second_row = supervisor.capabilities.calls.get(second.payload["opportunity_id"])
                assert second_row["status"] == "completed", second_row
                observation = second_row["channel_result"]["observation"]
                assert observation["sources"][0]["content"] == "BODY_EVIDENCE_TEXT"
                assert observation["sources"][0]["origin"] == "read"
                assert requests[1].url.path == "/extract" and len(requests) == 2
                assert (await supervisor.web.status())["usage"]["autonomous_requests"] == 2

                await supervisor.capabilities.dispatch(first, 1001)
                await supervisor.capabilities.dispatch(second, 1002)
                await supervisor.capabilities.reconcile()
                assert len(requests) == 2 and len(model.chat_calls) == 2
                assert engine.snapshot.model_dump(mode="json") == baseline
                assert host.chat.pipeline.session_store.snapshot() == session
                assert not any(record.data.get("envelope", {}).get("command_type") == "ApplyOutcome"
                               for record in supervisor.journal.iter_records())
                assert ("POST", "/v1/board/append") not in board_requests
                assert "Returned snippet" in model.chat_calls[1][-1]["content"]
                assert "Returned snippet" not in model.chat_calls[1][0]["content"]
            finally:
                await supervisor.web.close()
    asyncio.run(run())


def test_two_controllers_do_not_repeat_a_concurrently_reserved_identity(tmp_path, monkeypatch):
    """Both callers see an absent row before catalog refresh; reservation wins once."""
    async def run():
        first, first_requests = web_rig(tmp_path)
        second, second_requests = web_rig(tmp_path)
        offer = next(item for item in await first.web.catalog("shared", autonomous=False) if item.capability_id == SEARCH)
        reached = 0
        both_ready = asyncio.Event()

        def barrier(original):
            async def catalog(*args, **kwargs):
                nonlocal reached
                result = await original(*args, **kwargs)
                reached += 1
                if reached == 2:
                    both_ready.set()
                await both_ready.wait()
                return result
            return catalog

        monkeypatch.setattr(first.web, "catalog", barrier(first.web.catalog))
        monkeypatch.setattr(second.web, "catalog", barrier(second.web.catalog))
        try:
            results = await asyncio.gather(*(
                controller.execute("same-identity", offer, {"query": "public evidence"},
                                   deadline=utc_now() + timedelta(seconds=10))
                for controller in (first.web, second.web)), return_exceptions=True)
            assert len(first_requests) + len(second_requests) == 1, results
            assert sum(not isinstance(result, Exception) for result in results) >= 1
        finally:
            await first.web.close()
            await second.web.close()
    asyncio.run(run())


def test_opening_a_second_store_preserves_another_live_started_reservation(tmp_path):
    path = tmp_path / "web-calls.sqlite3"
    first = WebCallStore(path)
    first.reserve("in-flight", "shared", SEARCH, "fingerprint", "2026-09-12", True, 100, 20)
    second = WebCallStore(path)
    try:
        assert first.call("in-flight")["status"] == "started"
        assert second.call("in-flight")["status"] == "started"
        assert second.usage("2026-09-12") == (1, 1)
    finally:
        second.close()
        first.close()


def test_replaying_old_search_does_not_replace_later_read_evidence(tmp_path):
    async def run():
        supervisor, requests = web_rig(tmp_path)
        web = supervisor.web
        try:
            search_offer = next(item for item in await web.catalog("shared", autonomous=False) if item.capability_id == SEARCH)
            found = await web.execute("old-search", search_offer, {"query": "public evidence"},
                                      deadline=utc_now() + timedelta(seconds=10))
            read_offer = next(item for item in await web.catalog("shared", autonomous=False) if item.capability_id == READ)
            await web.execute("later-read", read_offer, {"source_refs": [found.sources[0].source_ref]},
                              deadline=utc_now() + timedelta(seconds=10))
            baseline = web.context("shared")
            assert baseline["sources"][0]["content"] == "BODY_EVIDENCE_TEXT"
            await web.execute("old-search", search_offer, {"query": "public evidence"},
                              deadline=utc_now() + timedelta(seconds=10))
            assert web.context("shared") == baseline
            assert len(requests) == 2
        finally:
            await web.close()
    asyncio.run(run())
