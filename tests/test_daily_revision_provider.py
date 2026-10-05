"""Provider contracts protect durable memory from incomplete or untrusted output."""
import json
from datetime import UTC, datetime

import httpx
import pytest

from harness.memory.revision.config import DailyRevisionConfig, load_config
from harness.memory.revision.provider import DailyRevisionProvider, RevisionProviderError
from harness.memory.revision.models import MemoryRevision
from harness.memory.revision.queue import DailyRevisionQueue


def source(source_id="source-1", text="用户说喜欢摄影"):
    return {"source_id": source_id, "text": text, "source_hash": "a" * 64,
            "author_principal": "person:1", "evidence_keys": ["message:1"],
            "self_generated": False, "relation_target": "person:1"}


def setup(tmp_path, **updates):
    key = tmp_path / "api.key"
    key.write_text("TEST-PRIVATE-KEY", encoding="utf-8")
    key.chmod(0o600)
    config = DailyRevisionConfig(api_key_file=key, enabled=True, max_attempts=1, **updates)
    queue = DailyRevisionQueue(tmp_path / "jobs.sqlite3")
    records = [source()]
    run = queue.enqueue(owner_id="misty", preset_id="misty", day="2026-09-01", partition="local",
                        sources=records, basis={})
    return config, queue, records, run["run_id"]


def response(value, *, finish="stop", **message):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {
        "content": json.dumps(value, ensure_ascii=False), "reasoning_content": "PRIVATE-REASONING", **message}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150}})


def answer(request, *, rejected=False):
    payload = json.loads(request.content)
    user = json.loads(payload["messages"][1]["content"])
    name = payload["response_format"]["json_schema"]["name"]
    if name.endswith("CleanedBatch"):
        return response({"cleaned": [{"source_ids": [row["source_id"]], "disposition": "keep",
            "summary": row["text"], "evidence_kind": "speaker_claim", "reason": ""}
            for row in user["sources"]]})
    if name.endswith("Conclusions"):
        return response({"memories": [], "impressions": [], "relationships": [], "depends_on_run_ids": []})
    return response({"approved": not rejected, "reasons": ["unsupported"] if rejected else [],
                     "reviewed_source_ids": [row["source_id"] for row in user["sources"]]})


def test_independent_three_stage_content_only_and_durable_cache(tmp_path):
    config, queue, records, run_id = setup(tmp_path)
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        assert request.url.host == "api.moonshot.cn"
        assert request.headers["Authorization"] == "Bearer TEST-PRIVATE-KEY"
        return answer(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = DailyRevisionProvider(config, queue, client)
        result = provider.propose(run_id, records, {})
        # Restart the provider and queue; the same run makes no new paid calls.
        again = DailyRevisionProvider(config, DailyRevisionQueue(queue.path), client).propose(run_id, records, {})
    assert result == again
    assert len(requests) == 3
    for payload in requests:
        assert payload["model"] == "kimi-k3"
        assert payload["reasoning_effort"] == "high"
        assert payload["response_format"]["json_schema"]["strict"]
        assert not {"temperature", "thinking", "keep_alive", "options"} & set(payload)
    details = json.dumps(queue.get(run_id), ensure_ascii=False)
    assert "PRIVATE-REASONING" not in details and "TEST-PRIVATE-KEY" not in details
    assert queue.status()["usage"][0]["charged_tokens"] == 450
    assert queue.status()["usage"][0]["day"] != "2026-09-01"


def test_deepseek_responses_preserves_three_stage_cache_and_usage(tmp_path):
    config, queue, records, run_id = setup(tmp_path, base_url="https://api.deepseek.com", model="deepseek-flash", reasoning_effort="none")
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert str(request.url) == "https://api.deepseek.com/responses"
        assert payload["reasoning"] == {"effort": "none"}
        old = {"messages": payload["input"], "response_format": {"json_schema": {"name": payload["text"]["format"]["name"]}}}
        result = answer(httpx.Request("POST", "https://synthetic.invalid", json=old)).json()
        return httpx.Response(200, json={"status": "completed", "usage": {"input_tokens": 50, "output_tokens": 100, "total_tokens": 150},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": result["choices"][0]["message"]["content"]}]}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = DailyRevisionProvider(config, queue, client)
        first = provider.propose(run_id, records, {})
        assert provider.propose(run_id, records, {}) == first
    assert len(requests) == 3
    assert queue.status()["usage"][0]["charged_tokens"] == 450


@pytest.mark.parametrize("bad", ["length", "null", "fence", "tool", "schema"])
def test_incomplete_or_invalid_response_cannot_become_a_proposal(tmp_path, bad):
    config, queue, records, run_id = setup(tmp_path)
    def handler(request):
        if bad == "length":
            return response({"cleaned": []}, finish="length")
        if bad == "null":
            return response({}, content=None)
        if bad == "fence":
            return response({}, content='```json\n{"cleaned": []}\n```')
        if bad == "tool":
            return response({"cleaned": []}, tool_calls=[{"id": "call-1"}])
        return response({"cleaned": [], "unexpected": True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RevisionProviderError, match="response_invalid"):
            DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    assert queue.stage(run_id, "proposal") is None


def test_cleaning_must_account_for_every_source(tmp_path):
    config, queue, records, run_id = setup(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda request: response({"cleaned": []}))) as client:
        with pytest.raises(RevisionProviderError, match="cleaning_coverage_invalid"):
            DailyRevisionProvider(config, queue, client).propose(run_id, records, {})


def test_rejected_independent_review_is_durable_and_blocks_proposal(tmp_path):
    config, queue, records, run_id = setup(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda request: answer(request, rejected=True))) as client:
        with pytest.raises(RevisionProviderError, match="review_rejected"):
            DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    assert queue.stage(run_id, "review_rejection")["reasons"] == ["unsupported"]
    assert queue.stage(run_id, "proposal") is None


def test_manual_retry_after_review_rejection_resynthesizes_but_reuses_cleaning(tmp_path):
    config, queue, records, run_id = setup(tmp_path)
    rejected, names = True, []
    def handler(request):
        names.append(json.loads(request.content)["response_format"]["json_schema"]["name"])
        return answer(request, rejected=rejected)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = DailyRevisionProvider(config, queue, client)
        with pytest.raises(RevisionProviderError, match="review_rejected"):
            provider.propose(run_id, records, {})
        rejected = False
        provider.propose(run_id, records, {})
    assert sum(name.endswith("CleanedBatch") for name in names) == 1
    assert sum(name.endswith("Conclusions") for name in names) == 2
    assert sum(name.endswith("Review") for name in names) == 2
    assert any(stage.startswith("invalid:") for stage in queue.get(run_id)["stages"])


@pytest.mark.parametrize("status", [401, 402, 403])
def test_access_errors_are_blocked_without_retry(tmp_path, status):
    config, queue, records, run_id = setup(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status,
            json={"error": "PRIVATE-KEY-PROVIDER-BODY"}))) as client:
        with pytest.raises(RevisionProviderError) as caught:
            DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    assert caught.value.blocked and not caught.value.retryable
    assert "PRIVATE" not in str(caught.value)
    assert queue.status()["usage"][0]["unconfirmed_tokens"] > 0


def test_retry_after_is_preserved_for_later_scheduler_attempt(tmp_path):
    config, queue, records, run_id = setup(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429,
            headers={"Retry-After": "120"}, json={"error": "slow down"}))) as client:
        with pytest.raises(RevisionProviderError) as caught:
            DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    assert caught.value.retry_after_seconds == 120
    assert caught.value.retryable


def test_retryable_statuses_have_bounded_accounted_attempts(tmp_path, monkeypatch):
    config, queue, records, run_id = setup(tmp_path)
    config = config.model_copy(update={"max_attempts": 3})
    calls, delays = [], []
    monkeypatch.setattr("harness.memory.revision.provider.time.sleep", delays.append)
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        if len(calls) == 2:
            return httpx.Response(503, json={})
        return answer(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    assert len(calls) == 5 and delays == [3, 2]
    assert queue.status()["usage"][0]["requests"] == 5
    assert queue.status()["usage"][0]["unconfirmed_tokens"] > 0


def test_review_expands_raw_evidence_across_cleaning_chunks(tmp_path):
    config, queue, _, run_id = setup(tmp_path, chunk_input_tokens=512)
    records = [source("one"), source("two"), source("three")]
    run_id = queue.enqueue(owner_id="misty", preset_id="misty", day="2026-09-01", partition="local",
                           sources=records, basis={})["run_id"]
    reviews, clean_groups = [], []
    memory = MemoryRevision(kind="episodic", subject="person:1", statement="用户重复提到摄影",
                            evidence_ids=["one", "two"])
    def handler(request):
        payload = json.loads(request.content)
        user = json.loads(payload["messages"][1]["content"])
        name = payload["response_format"]["json_schema"]["name"]
        if name.endswith("Conclusions"):
            return response({"memories": [memory.model_dump(mode="json")], "impressions": [], "relationships": [],
                             "depends_on_run_ids": []})
        if name.endswith("Review"):
            reviews.append({row["source_id"] for row in user["sources"]})
        else:
            clean_groups.append({row["source_id"] for row in user["sources"]})
        return answer(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    assert clean_groups == [{"one"}, {"two"}, {"three"}]
    assert reviews == [{"one", "two"}, {"three"}]


def test_historical_counterevidence_and_dependencies_reach_independent_review(tmp_path):
    config, queue, records, run_id = setup(tmp_path)
    old = source("historical", "用户以前说不喜欢摄影")
    memory = MemoryRevision(kind="semantic", target_id="fact-1", subject="person:1", statement="摄影偏好出现变化",
        evidence_ids=["historical"], counterevidence_ids=["source-1"], operation="dispute")
    cleaned_ids, reviewed = [], []
    context = {"historical_sources": [old], "active_revision_ids": ["revision-old"],
               "existing": {"semantic/fact-1": {"statement": "不喜欢摄影"}}}
    def handler(request):
        payload = json.loads(request.content)
        user = json.loads(payload["messages"][1]["content"])
        name = payload["response_format"]["json_schema"]["name"]
        if name.endswith("Conclusions"):
            return response({"memories": [memory.model_dump(mode="json")], "impressions": [], "relationships": [],
                             "depends_on_run_ids": ["revision-old"]})
        if name.endswith("Review"):
            reviewed.append(user)
        else:
            cleaned_ids.extend(row["source_id"] for row in user["sources"])
        return answer(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        proposal = DailyRevisionProvider(config, queue, client).propose(run_id, records, context)
    assert proposal.depends_on_run_ids == ["revision-old"]
    assert cleaned_ids == ["source-1"]
    assert len(reviewed) == 1
    assert {row["source_id"] for row in reviewed[0]["sources"]} == {"source-1", "historical"}
    assert reviewed[0]["current_source_ids"] == ["source-1"]
    assert reviewed[0]["context"]["existing"] == context["existing"]


def test_provider_rejects_sources_changed_after_queue_freeze(tmp_path):
    config, queue, records, run_id = setup(tmp_path)
    records[0]["text"] = "changed after freezing"
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("no request allowed"))) as client:
        with pytest.raises(RevisionProviderError, match="frozen_sources_changed"):
            DailyRevisionProvider(config, queue, client).propose(run_id, records, {})


def test_limited_budget_reserves_before_network_call(tmp_path):
    config, queue, records, run_id = setup(tmp_path, daily_token_budget=1)
    def forbidden(request):
        pytest.fail("budget must be reserved before transport")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(RevisionProviderError, match="daily_budget_exceeded"):
            DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    assert queue.status()["usage"] == []


def test_budget_day_uses_current_configured_timezone_not_historical_job_day(tmp_path, monkeypatch):
    config, queue, records, run_id = setup(tmp_path)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 14, 18, tzinfo=UTC).astimezone(tz)
    monkeypatch.setattr("harness.memory.revision.provider.datetime", FixedDatetime)
    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    assert queue.status()["usage"][0]["day"] == "2026-09-15"


def test_source_and_config_changes_cannot_reuse_stage_cache(tmp_path):
    config, queue, records, run_id = setup(tmp_path)
    requests = []
    def handler(request):
        requests.append(request)
        return answer(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
        DailyRevisionProvider(config.model_copy(update={"reasoning_effort": "low"}), queue, client).propose(run_id, records, {})
    assert len(requests) == 6


def test_config_uses_private_reference_and_does_not_echo_bad_contents(tmp_path):
    config, _, _, _ = setup(tmp_path)
    public = config.public()
    assert public["api_key_configured"] is True
    assert "api_key_file" not in public
    assert DailyRevisionConfig().daily_token_budget is None
    assert not load_config().enabled
    with pytest.raises(ValueError, match="api_key_file_required"):
        DailyRevisionConfig(enabled=True)
    target = tmp_path / "config.json"
    target.write_text('{"api_key":"DO-NOT-ECHO"}', encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(target)
    assert "DO-NOT-ECHO" not in str(caught.value)
    with pytest.raises(ValueError, match="requires_https"):
        DailyRevisionConfig(base_url="https://user:password@example.org/v1")
