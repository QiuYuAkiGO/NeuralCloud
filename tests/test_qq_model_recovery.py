"""Transient model outages retry within one turn; GUI sends are never retried."""
from types import SimpleNamespace

import httpx
import pytest

from harness.qq import decision
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace


def status_error(code, headers=None):
    response = httpx.Response(code, headers=headers, request=httpx.Request("POST", "https://model.invalid/chat"))
    return httpx.HTTPStatusError("synthetic HTTP failure", request=response.request, response=response)


def scoped(monkeypatch, outcomes, *, seconds=10, work_seconds=0.5):
    now, calls = [100.0], []
    monkeypatch.setattr(decision.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    def request(*args, **kwargs):
        calls.append(kwargs["timeout_seconds"])
        now[0] += work_seconds
        result = outcomes[len(calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    trace = QQTurnTrace(clock=lambda: now[0])
    client = decision._QQScopedLLM(SimpleNamespace(complete_json=request),
        deadline=QQDeadline.after(seconds, clock=lambda: now[0]), trace=trace, timeout=20)
    return client, calls, trace


@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), httpx.ReadError("disconnected"),
    httpx.ReadTimeout("timeout"), httpx.RemoteProtocolError("peer closed"),
    status_error(503), status_error(429, {"Retry-After": "1"})])
def test_transient_failure_recovers_once_using_remaining_turn_budget(monkeypatch, error):
    client, calls, trace = scoped(monkeypatch, [error, {"action": "wait"}])
    assert client.complete_json(system="fixed", user="synthetic") == {"action": "wait"}
    assert len(calls) == 2 and 0 < calls[1] < calls[0] == 10
    assert client.failed_error_type is None
    snapshot = trace.snapshot()
    assert snapshot["counters"] == {"model_calls": 2, "gate_model_calls": 2,
                                    "failed_model_calls": 1, "model_retries": 1}
    assert [stage["outcome"] for stage in snapshot["stages"] if stage["name"] == "gate.model_call"] == ["error", "ok"]
    assert any(stage["name"] == "gate.retry_backoff" and stage["duration_ms"] >= 250 for stage in snapshot["stages"])


@pytest.mark.parametrize("error", [ValueError("invalid JSON"), status_error(400), status_error(401),
    status_error(403), status_error(429, {"Retry-After": "30"}),
    status_error(503, {"Retry-After": "inf"})])
def test_permanent_errors_and_long_server_backoffs_do_not_retry(monkeypatch, error):
    client, calls, trace = scoped(monkeypatch, [error])
    with pytest.raises(type(error)):
        client.complete_json(system="fixed", user="synthetic")
    assert len(calls) == 1 and trace.snapshot()["counters"].get("model_retries", 0) == 0


def test_two_failures_remain_error_without_a_third_call(monkeypatch):
    client, calls, trace = scoped(monkeypatch, [httpx.ConnectError("one"), httpx.ConnectError("two")])
    with pytest.raises(httpx.ConnectError):
        client.complete_json(system="fixed", user="synthetic")
    assert len(calls) == 2 and client.failed_error_type == "model_http"
    assert trace.snapshot()["counters"]["failed_model_calls"] == 2


def test_insufficient_original_deadline_does_not_schedule_retry(monkeypatch):
    client, calls, trace = scoped(monkeypatch, [httpx.ConnectError("offline")], seconds=0.6)
    with pytest.raises(httpx.ConnectError):
        client.complete_json(system="fixed", user="synthetic")
    assert calls == [0.6] or calls == pytest.approx([0.6])
    assert trace.snapshot()["counters"].get("model_retries", 0) == 0


def test_late_success_after_reconnect_is_expired_not_a_reply(monkeypatch):
    client, calls, trace = scoped(monkeypatch, [httpx.ConnectError("offline"), {"action": "speak"}],
                                  seconds=1.2)
    with pytest.raises(QQDeadlineExpired):
        client.complete_json(system="fixed", user="synthetic")
    assert len(calls) == 2 and trace.snapshot()["counters"]["late_results"] == 1
    assert client.failed_error_type == "deadline_expired"


def test_later_success_cannot_hide_an_earlier_exhausted_helper_failure(monkeypatch):
    client, calls, trace = scoped(monkeypatch, [httpx.ConnectError("one"), httpx.ConnectError("two"),
                                               {"action": "speak"}])
    with pytest.raises(httpx.ConnectError):
        client.complete_json(system="fixed", user="synthetic")
    assert client.complete_json(system="fixed", user="next synthetic stage") == {"action": "speak"}
    assert len(calls) == 3 and client.failed_error_type == "model_http"
    assert trace.snapshot()["counters"]["failed_model_calls"] == 2
