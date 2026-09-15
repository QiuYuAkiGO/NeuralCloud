"""Cognitive owner/RPC/ledger integration for capability selection and evidence."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json

import httpx
import pytest

from harness.cognitive import board
from harness.contracts.decisions import ActionOutcomeEvidence, DecisionEvidence, DeliveryEvidence, PreparedResponse
from harness.runtime.adapters import _PreparedEntry
from harness.runtime.rpc import HttpCognitiveClient, create_domain_service_app
from infra.trace.request_spans import current_trace_context
from tests.test_basic_persona_continuity import _command, _host
from tests.test_capability_cognition import FakeLLM, command, desktop_offer, moment


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
    monkeypatch.setenv("NC_ISOLATED_DEVELOPMENT", "1")
    monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)


def desktop_choice(item):
    return {"kind": "act", "offer_id": item.offer_id, "arguments": {"app_id": "scratchpad"},
        "success_criteria": [{"kind": "window", "condition": "present", "app_id": "scratchpad"}]}


async def prepared_desktop(owner):
    item = desktop_offer()
    llm = FakeLLM(desktop_choice(item))
    owner.chat.llm = llm
    request = command([item])
    result = await owner.prepare_capability_call(request)
    assert result.status == "applied", result.error
    return request, result, PreparedResponse.model_validate(result.result["prepared_response"]), llm


def commit_command(prepared, **updates):
    evidence = ActionOutcomeEvidence(prepare_id=prepared.prepare_id, goal_id=prepared.decision.goal_id,
        observed_at=moment(), success=True, summary="provider prose is not an objective memory",
        evidence_refs=["provider:independent-observation-1"], external_effect_ref="provider:receipt-1")
    evidence = evidence.model_copy(update=updates)
    return _command("CommitInteraction", 4, {
        "prepare_id": prepared.prepare_id, "evidence": evidence.model_dump(mode="json")})


def test_host_prepares_same_owner_desktop_without_memory_or_chat_and_replays(tmp_path, monkeypatch):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        before = owner.chat.pipeline.session_store.snapshot()
        try:
            request, first, prepared, llm = await prepared_desktop(owner)
            assert prepared.prepare_id in owner.adapter._prepared
            assert owner.adapter._prepared[prepared.prepare_id].capability
            assert owner.chat.pipeline.session_store.snapshot() == before
            assert owner.chat.memory.stores.episodic.list_all() == []
            assert prepared.expires_at <= request.deadline
            second = await owner.prepare_capability_call(request)
            assert second.status == "idempotent_replay" and second.result == first.result
            assert len(llm.calls) == 1
            assert llm.calls[0][2]["correlation_id"] == request.correlation_id
        finally:
            owner.chat.llm = original
            await owner.close()
        restored, _ = _host(tmp_path, monkeypatch, [])
        try:
            assert restored.adapter._prepared[prepared.prepare_id].capability
            assert (await restored.prepare_capability_call(request)).result == first.result
        finally:
            await restored.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["wait", "defer", "no_action"])
def test_wait_has_no_prepared_action_and_cannot_commit_chat(tmp_path, monkeypatch, kind):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        owner.chat.llm = FakeLLM({"kind": kind, "reason": "PRIVATE_DECISION_REASON"})
        try:
            result = await owner.prepare_capability_call(command())
            assert result.status == "applied"
            assert "prepared_response" not in result.result
            assert owner.adapter._prepared == {}
            assert "PRIVATE_DECISION_REASON" not in (tmp_path / "cognitive.json").read_text(encoding="utf-8")
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())


def test_failed_selection_is_durable_safe_and_never_regenerated(tmp_path, monkeypatch):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        owner.chat.llm = llm = FakeLLM("PRIVATE_BAD_MODEL_OUTPUT")
        request = command()
        try:
            result = await owner.prepare_capability_call(request)
            assert result.status == "failed" and result.error == "capability_invalid_json"
            assert (await owner.prepare_capability_call(request)).idempotency_hit
            assert len(llm.calls) == 1 and not owner.adapter._prepared
            assert "PRIVATE_BAD_MODEL_OUTPUT" not in (tmp_path / "cognitive.json").read_text(encoding="utf-8")
        finally:
            owner.chat.llm = original
            await owner.close()
        restored, _ = _host(tmp_path, monkeypatch, [])
        try:
            replay = await restored.prepare_capability_call(request)
            assert replay.status == "failed" and replay.idempotency_hit
        finally:
            await restored.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("success", [True, False])
def test_verified_desktop_commit_records_objective_memory_only_and_restart_replay(tmp_path, monkeypatch, success):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        try:
            _, _, prepared, _ = await prepared_desktop(owner)
            session_revision = owner.adapter._component_revisions["session"]
            snapshot_before = owner.chat.memory.get_state()
            commit = commit_command(prepared, success=success)
            result = await owner.commit_interaction(commit)
            assert result.status == "applied", result.error
            assert not any(owner.chat.pipeline.session_store.snapshot().values())
            assert owner.adapter._component_revisions["session"] == session_revision
            records = owner.chat.memory.stores.episodic.list_all()
            assert len(records) == 1
            record = records[0]
            assert record.event_type == "capability_action_outcome"
            assert record.character_action == "app.open.v1"
            assert record.outcome == ("verified_success" if success else "verified_failure")
            assert record.persona_conditioned_insight is None
            assert record.evidence == ["provider:independent-observation-1"]
            assert "provider prose" not in record.objective_fact
            assert owner.chat.memory.get_state().relationship == snapshot_before.relationship
            assert (await owner.commit_interaction(commit)).status == "idempotent_replay"
            assert len(owner.chat.memory.stores.episodic.list_all()) == 1
        finally:
            owner.chat.llm = original
            await owner.close()
        restored, _ = _host(tmp_path, monkeypatch, [])
        try:
            assert len(restored.chat.memory.stores.episodic.list_all()) == 1
            assert (await restored.commit_interaction(commit)).status == "idempotent_replay"
            assert not any(restored.chat.pipeline.session_store.snapshot().values())
        finally:
            await restored.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["goal", "missing_refs", "early", "delivery"])
def test_capability_commit_rejects_unbound_or_unsubstantiated_success(tmp_path, monkeypatch, failure):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        try:
            _, _, prepared, _ = await prepared_desktop(owner)
            kwargs = {"goal_id": "invented-goal"} if failure == "goal" else (
                {"evidence_refs": []} if failure == "missing_refs" else (
                    {"observed_at": prepared.prepared_at - timedelta(seconds=1)} if failure == "early" else {}))
            commit = commit_command(prepared, **kwargs)
            if failure == "delivery":
                evidence = DeliveryEvidence(prepare_id=prepared.prepare_id, delivered_at=moment(),
                    channel="chat", success=True, delivered_segments=["forged incoming/outgoing text"])
                commit = commit.model_copy(update={"payload": {"prepare_id": prepared.prepare_id,
                    "evidence": evidence.model_dump(mode="json")}})
            result = await owner.commit_interaction(commit)
            assert result.status == "rejected"
            assert result.error == "capability_commit_requires_bound_action_evidence"
            assert owner.chat.memory.stores.episodic.list_all() == []
            assert not any(owner.chat.pipeline.session_store.snapshot().values())
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["cancelled", "defer", "no_action"])
def test_no_effect_end_commits_without_memory_or_session_revision(tmp_path, monkeypatch, ending):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        try:
            _, _, prepared, _ = await prepared_desktop(owner)
            commit = commit_command(prepared, success=False, evidence_refs=[])
            if ending != "cancelled":
                evidence = DecisionEvidence(prepare_id=prepared.prepare_id, recorded_at=moment(),
                    decision_kind=ending, reason="provider unavailable")
                commit = commit.model_copy(update={"payload": {"prepare_id": prepared.prepare_id,
                    "evidence": evidence.model_dump(mode="json")}})
            before = owner.adapter._component_revisions.copy()
            result = await owner.commit_interaction(commit)
            assert result.status == "applied", result.error
            assert owner.chat.memory.stores.episodic.list_all() == []
            assert owner.adapter._component_revisions == before
            assert not any(owner.chat.pipeline.session_store.snapshot().values())
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())


def test_previous_prepared_state_defaults_to_legacy(tmp_path, monkeypatch):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        try:
            _, _, prepared, _ = await prepared_desktop(owner)
            legacy_payload = owner.adapter._prepared[prepared.prepare_id].model_dump(mode="json", exclude={"capability"})
            assert _PreparedEntry.model_validate(legacy_payload).capability is False
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())


def test_authenticated_rpc_exposes_existing_owner_capability_handler(tmp_path, monkeypatch):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        owner.chat.llm = FakeLLM({"kind": "wait"})
        app = create_domain_service_app(owner, service_token="test-token", service_name="cognitive")
        request = command()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                    headers={"Authorization": "Bearer test-token"}) as client:
                cognitive = HttpCognitiveClient("http://test", service_token="test-token", client=client)
                result = await cognitive.prepare_capability_call(request)
                assert result.status == "applied" and result.result["decision"]["choice"]["kind"] == "wait"
                denied = await client.post("/v1/commands/PrepareCapabilityCall", json=request.model_dump(mode="json"),
                    headers={"Authorization": "Bearer wrong-token"})
                assert denied.status_code == 401
                assert len(owner.chat.llm.calls) == 1
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())


def test_board_failure_includes_safe_diagnostic_and_command_trace(tmp_path, monkeypatch):
    trace = []

    async def failing_prepare(host, command, *, diagnostic):
        trace.append(current_trace_context())
        diagnostic.stage = "model_truncated"
        diagnostic.model_attempts.append({"attempt": 1, "finish_reason": "length", "content_characters": 20})
        raise ValueError("PRIVATE_MODEL_OUTPUT")

    monkeypatch.setattr(board, "prepare", failing_prepare)

    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        request = _command("PrepareBoardEntry", 1, {})
        try:
            result = await owner.prepare_board_entry(request)
            assert result.status == "failed"
            assert result.result["diagnostic"]["stage"] == "model_truncated"
            assert result.result["diagnostic"]["model_attempts"][0]["finish_reason"] == "length"
            assert trace[0]["event_id"] == request.event_id and trace[0]["command_id"] == request.command_id
            assert "PRIVATE_MODEL_OUTPUT" not in result.model_dump_json()
            assert (await owner.prepare_board_entry(request)).result == result.result
            assert len(trace) == 1
            persisted = json.loads((tmp_path / "cognitive.json").read_text(encoding="utf-8"))
            assert persisted["ledger"]["PrepareBoardEntry\0PrepareBoardEntry:1"]["result"] == result.result
        finally:
            await owner.close()
    asyncio.run(scenario())
