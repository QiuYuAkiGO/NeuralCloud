"""Exposure belongs to a reserved transmission attempt, not prompt preparation."""
import httpx
import pytest

from harness.config import Settings
from harness.llm.client import MockLLM, OpenAICompatClient, RoutingLLMClient
from harness.production.config import ProductionConfig
from harness.production.metering import mark_production_dispatch, production_attempt, production_call_scope
from harness.production.store import ProductionBudgetExceeded, ProductionStore


CONTEXT = {"schema_version": "production-context-v1", "configuration": {}, "body": {}}


def client(handler, observed):
    result = OpenAICompatClient(Settings(_env_file=None, llm_model="fixture-model",
        llm_base_url="https://model.invalid/v1", llm_api_key="fixture", call_log_enabled=False),
        request_observer=(lambda event: None) if observed else None)
    result._client.close()
    result._client = httpx.Client(transport=httpx.MockTransport(handler))
    return result


def response():
    return {"choices": [{"finish_reason": "stop", "message": {"content": "fixture reply"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}


@pytest.mark.parametrize("observed", [False, True])
def test_budget_rejection_has_no_exposure_or_post(tmp_path, observed):
    meter = ProductionStore(tmp_path / "meter.sqlite3", ProductionConfig(autonomous_attempts_per_hour=1))
    meter.reserve_attempt("already-used", purpose="other", autonomous=True)
    events = []
    llm = client(lambda request: events.append("post") or httpx.Response(200, json=response()), observed)
    try:
        with production_call_scope(CONTEXT, command_id="denied", autonomous=True, meter=meter,
                                   on_dispatch=lambda: events.append("exposure")):
            with pytest.raises(ProductionBudgetExceeded):
                llm.complete_chat_result([{"role": "user", "content": "fixture"}])
        assert events == []
        assert meter.pending_usage() == []
    finally:
        llm.close()


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("failure", [None, "transport", "status", "shape", "json"])
def test_actual_attempt_keeps_exposure_on_success_or_failure(tmp_path, observed, failure):
    meter = ProductionStore(tmp_path / "meter.sqlite3")
    events = []

    def handler(request):
        assert events == ["exposure"]
        events.append("post")
        if failure == "transport":
            raise httpx.ReadTimeout("lost response", request=request)
        if failure == "json":
            return httpx.Response(200, text="not JSON")
        body = response()
        if failure == "shape":
            body.pop("choices")
        return httpx.Response(503 if failure == "status" else 200, json=body)

    llm = client(handler, observed)
    try:
        with production_call_scope(CONTEXT, command_id="attempt", meter=meter,
                                   on_dispatch=lambda: events.append("exposure")):
            if failure:
                with pytest.raises((httpx.HTTPError, ValueError, KeyError)):
                    llm.complete_chat_result([{"role": "user", "content": "fixture"}])
            else:
                assert llm.complete_chat_result([{"role": "user", "content": "fixture"}]).text == "fixture reply"
        assert events == ["exposure", "post"]
        usage = meter.pending_usage()
        assert len(usage) == 1
        if failure not in {"transport", "json"}:
            assert usage[0]["usage"]["raw_usage"] == response()["usage"]
    finally:
        llm.close()


def test_router_forwards_dispatch_awareness_and_does_not_duplicate_exposure(tmp_path):
    meter = ProductionStore(tmp_path / "meter.sqlite3")
    exposures = []
    main = client(lambda request: httpx.Response(200, json=response()), False)
    router = RoutingLLMClient(main, MockLLM())
    assert router.production_dispatch_aware is True
    assert RoutingLLMClient(MockLLM(), MockLLM()).production_dispatch_aware is False
    try:
        with production_call_scope(CONTEXT, command_id="routed", meter=meter,
                                   on_dispatch=lambda: exposures.append("sent")):
            router.complete_chat_result([{"role": "user", "content": "fixture"}])
        assert exposures == ["sent"]
    finally:
        router.close()


def test_each_reserved_attempt_has_one_dispatch_hook_and_preparation_has_none(tmp_path):
    meter = ProductionStore(tmp_path / "meter.sqlite3")
    exposures = []
    with production_call_scope(CONTEXT, command_id="retry", meter=meter,
                               on_dispatch=lambda: exposures.append("attempt")):
        assert exposures == []
        for _ in range(2):
            with production_attempt(model="fixture") as evidence:
                assert len(exposures) == _
                mark_production_dispatch(evidence)
                mark_production_dispatch(evidence)
        assert exposures == ["attempt", "attempt"]
    assert len(meter.pending_usage()) == 2


def test_nested_scope_does_not_leak_dispatch_hook(tmp_path):
    meter = ProductionStore(tmp_path / "meter.sqlite3")
    exposures = []
    with production_call_scope(CONTEXT, command_id="outer", meter=meter,
                               on_dispatch=lambda: exposures.append("outer")):
        with production_call_scope(CONTEXT, command_id="inner", meter=meter):
            with production_attempt(model="fixture") as evidence:
                mark_production_dispatch(evidence)
        assert exposures == []
        with production_attempt(model="fixture") as evidence:
            mark_production_dispatch(evidence)
    assert exposures == ["outer"]
