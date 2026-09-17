"""Stage reserves share one original deadline; slow calls are not retried."""
import httpx
import pytest

from harness.llm.client import ChatResult
from harness.qq.decision import _QQScopedLLM
from harness.qq.policy import QQLatencyOptions
from harness.qq.telemetry import QQDeadline, QQTurnTrace


def test_gate_body_reserves_use_original_monotonic_deadline():
    clock = [100.0]
    timeouts = []

    class Client:
        def complete_chat_result(self, messages, **kwargs):
            timeouts.append(kwargs["timeout_seconds"])
            clock[0] += 4
            return ChatResult(text="ok")

    llm = _QQScopedLLM(Client(), deadline=QQDeadline.after(30, clock=lambda: clock[0]),
        timeout=60, trace=QQTurnTrace(), policy=QQLatencyOptions(qq_stage_budgets_enabled=True))
    llm.complete_chat_result([])
    clock[0] += 8  # Web/other work also consumes the same turn.
    llm.stage_name = "persona"
    llm.complete_chat_result([])
    assert timeouts == [15, 15]  # Body gets remaining 18 minus delivery 3, not a fresh 60.


def test_exhausted_gate_reserve_starts_no_call():
    class Client:
        def complete_chat_result(self, *args, **kwargs):
            pytest.fail("exhausted downstream reserve started a model")

    llm = _QQScopedLLM(Client(), deadline=QQDeadline.after(12), timeout=60, trace=QQTurnTrace(),
                       policy=QQLatencyOptions(qq_stage_budgets_enabled=True))
    with pytest.raises(TimeoutError, match="stage budget"):
        llm.complete_chat_result([])


@pytest.mark.parametrize("spent,retries", [(0.5, 1), (2.1, 0), (15.0, 0)])
def test_only_fast_transient_failure_with_useful_budget_can_retry(monkeypatch, spent, retries):
    clock, calls = [0.0], []

    class Client:
        def complete_chat_result(self, messages, **kwargs):
            calls.append(kwargs["timeout_seconds"])
            if len(calls) == 1:
                clock[0] += spent
                raise httpx.ReadTimeout("synthetic")
            return ChatResult(text="ok")

    monkeypatch.setattr("harness.qq.decision.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    llm = _QQScopedLLM(Client(), deadline=QQDeadline.after(60, clock=lambda: clock[0]), timeout=60,
        trace=QQTurnTrace(), policy=QQLatencyOptions(qq_stage_budgets_enabled=True))
    if retries:
        assert llm.complete_chat_result([]).text == "ok"
    else:
        with pytest.raises(httpx.ReadTimeout):
            llm.complete_chat_result([])
    assert len(calls) == 1 + retries
    if retries:
        assert calls[1] < calls[0]
