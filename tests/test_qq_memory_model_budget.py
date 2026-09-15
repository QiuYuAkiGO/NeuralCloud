"""QQ-only reasoning exhaustion recovery stays bounded by the caller deadline."""

from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from harness.config import Settings
from harness.llm.client import OpenAICompatClient
from harness.memory.qq_assimilation import (
    QQAssimilationQueue, QQMemoryProposer, QQNonThinkingProposalInvalid, QQReasoningBudgetExhausted,
)
from harness.memory.shared import SharedMemoryStore
from tests.test_qq_assimilation import make_job, proposal


@pytest.fixture
def setup(tmp_path):
    store = SharedMemoryStore(tmp_path / "shared.sqlite3")
    queue = QQAssimilationQueue(store.path, owner_id="misty", preset_id="active", self_id="999",
                                allowed_group_ids=("100", "101"))
    return store, queue


def _body(text, *, finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8191,
                      "total_tokens": 8201, "completion_tokens_details": {"reasoning_tokens": 8191}}}


def _client(monkeypatch, responses, *, base="https://api.deepseek.com", model="deepseek-v4-flash", on_request=None):
    original_client = httpx.Client
    calls = []

    def handle(request):
        calls.append({"payload": json.loads(request.content), "timeout": request.extensions["timeout"]})
        if on_request:
            on_request(len(calls))
        assert len(calls) <= len(responses), "model calls exceeded the bounded recovery policy"
        return httpx.Response(200, json=responses[len(calls) - 1])

    monkeypatch.setattr("harness.llm.client.httpx.Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs))
    client = OpenAICompatClient(Settings(llm_base_url=base, llm_model=model))
    return client, calls


@pytest.mark.parametrize("partial_text", ["", '{"episodes":[{"summary":"cut'])
def test_length_recovers_once_without_changing_regular_client_or_deadline(setup, monkeypatch, partial_text):
    job = make_job(setup)
    clock = [100.0]
    monkeypatch.setattr("harness.llm.deadline.time.monotonic", lambda: clock[0])
    client, calls = _client(monkeypatch, [_body(partial_text, finish="length"), _body(proposal(job).model_dump_json())],
                            on_request=lambda count: clock.__setitem__(0, 135.0) if count == 1 else None)
    settings_before = client.settings.model_dump()
    try:
        result = QQMemoryProposer(client, timeout_seconds=45, reasoning_options={"reasoning_effort": "low"}).propose(job)
        assert result == proposal(job)
        assert len(calls) == 2
        assert "thinking" not in calls[0]["payload"]
        assert calls[1]["payload"]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in calls[1]["payload"]
        assert calls[0]["payload"]["messages"] == calls[1]["payload"]["messages"]
        assert 0 < calls[1]["timeout"]["read"] <= 10.1
        assert client._extra_payload == {}
        assert client.settings.model_dump() == settings_before
    finally:
        client.close()


@pytest.mark.parametrize("base,model", [
    ("https://example.test", "deepseek-v4-flash"),
    ("https://api.deepseek.com", "unverified-model"),
    ("http://api.deepseek.com", "deepseek-v4-flash"),
])
def test_unknown_provider_or_model_keeps_typed_failure_and_pending_source(setup, monkeypatch, base, model):
    _, queue = setup
    job = make_job(setup)
    client, calls = _client(monkeypatch, [_body("", finish="length")], base=base, model=model)
    try:
        with pytest.raises(QQReasoningBudgetExhausted) as error:
            QQMemoryProposer(client).propose(job)
        queue.fail(job.job_id, error.value.error_code)
        pending = queue.get_job(job.job_id)
        assert pending.status == "retry"
        assert pending.error_code == "reasoning_budget_exhausted"
        assert pending.sources == job.sources and pending.proposal is None
        assert len(calls) == 1
        assert "thinking" not in calls[0]["payload"]
    finally:
        client.close()


def test_recovery_failure_does_not_recover_again(setup, monkeypatch):
    job = make_job(setup)
    client, calls = _client(monkeypatch, [_body("", finish="length"), _body("", finish="length")])
    try:
        with pytest.raises(QQReasoningBudgetExhausted, match="reasoning_budget_exhausted"):
            QQMemoryProposer(client).propose(job)
        assert len(calls) == 2
    finally:
        client.close()


def test_restart_recovers_known_budget_failure_directly_with_unchanged_sources(setup, monkeypatch):
    _, queue = setup
    job = make_job(setup)
    valid = proposal(job).model_dump_json()

    def timeout_first_fallback(count):
        if count == 2:
            raise httpx.ReadTimeout("simulated remaining deadline exhausted")

    client, calls = _client(monkeypatch, [_body("", finish="length"), None, _body(valid)],
                            on_request=timeout_first_fallback)
    try:
        with pytest.raises(QQReasoningBudgetExhausted) as error:
            QQMemoryProposer(client, reasoning_options={"reasoning_effort": "low"}).propose(job)
        queue.fail(job.job_id, error.value.error_code)
        # Reconstruct both the durable queue and proposer as after a restart.
        recovered_queue = QQAssimilationQueue(queue.path, owner_id="misty", preset_id="active", self_id="999",
                                              allowed_group_ids=("100", "101"))
        retry = recovered_queue.next_job(now=datetime.now(timezone.utc) + timedelta(minutes=10))
        assert retry.error_code == "reasoning_budget_exhausted"
        result = QQMemoryProposer(client, reasoning_options={"reasoning_effort": "low"}).propose(retry)
        assert result == proposal(job)
        assert len(calls) == 3
        assert calls[2]["payload"]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in calls[2]["payload"]
        assert calls[2]["payload"]["messages"] == calls[0]["payload"]["messages"]
        assert retry.sources == job.sources and retry.source_digest == job.source_digest
    finally:
        client.close()


def test_valid_completion_never_changes_thinking_mode(setup, monkeypatch):
    job = make_job(setup)
    client, calls = _client(monkeypatch, [_body(proposal(job).model_dump_json())])
    try:
        assert QQMemoryProposer(client).propose(job) == proposal(job)
        assert len(calls) == 1 and "thinking" not in calls[0]["payload"]
    finally:
        client.close()


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_known_recovery_repairs_once_without_thinking_and_preserves_mode_on_failure(setup, monkeypatch, repair_succeeds):
    _, queue = setup
    job = make_job(setup).model_copy(update={"error_code": "reasoning_budget_exhausted"})
    invalid = proposal(job).model_dump(mode="json")
    invalid["episodes"][0]["source_ids"] = ["forged-source"]
    valid = proposal(job).model_dump_json()
    responses = [_body(json.dumps(invalid)), _body(valid if repair_succeeds else json.dumps(invalid)), _body(valid)]
    clock = [100.0]
    monkeypatch.setattr("harness.llm.deadline.time.monotonic", lambda: clock[0])
    client, calls = _client(monkeypatch, responses,
                            on_request=lambda count: clock.__setitem__(0, clock[0] + 10))
    try:
        if repair_succeeds:
            assert QQMemoryProposer(client).propose(job) == proposal(job)
        else:
            with pytest.raises(QQNonThinkingProposalInvalid, match="partition every frozen source") as error:
                QQMemoryProposer(client).propose(job)
            assert error.value.error_code == "non_thinking_proposal_invalid"
            assert len(calls) == 2
            queue.fail(job.job_id, error.value.error_code)
            retry = queue.get_job(job.job_id)
            assert QQMemoryProposer(client).propose(retry) == proposal(job)
        assert len(calls) == (2 if repair_succeeds else 3)
        assert all(call["payload"]["thinking"] == {"type": "disabled"} for call in calls)
        assert all("reasoning_effort" not in call["payload"] for call in calls)
        payloads = [json.loads(call["payload"]["messages"][1]["content"]) for call in calls]
        assert all(payload["frozen_sources"] == payloads[0]["frozen_sources"] for payload in payloads)
        assert "rejected_proposal" in payloads[1] and "validation_feedback" in payloads[1]
        assert 0 < calls[1]["timeout"]["read"] <= 35.1
    finally:
        client.close()


def test_outer_model_deadline_is_not_restarted_by_proposer(setup, monkeypatch):
    from harness.llm.deadline import model_deadline

    job = make_job(setup)
    client, calls = _client(monkeypatch, [_body(proposal(job).model_dump_json())])
    try:
        with model_deadline(datetime.now(timezone.utc) + timedelta(seconds=2)):
            QQMemoryProposer(client, timeout_seconds=45).propose(job)
        assert 0 < calls[0]["timeout"]["read"] <= 2
    finally:
        client.close()
