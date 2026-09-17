import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from harness.qq.policy import QQLatencyOptions
from harness.qq.web_plan import QQWebPlan
from harness.runtime import qq_web
from harness.runtime.models import utc_now
from tests.test_web_runtime import rig


def _case(monkeypatch, *, steps=None, seconds=60):
    monkeypatch.setattr(qq_web, "verified_qq_source", lambda request: {"digest": "a" * 64,
        "text": "请读 https://example.com/article", "source": {}})
    event = SimpleNamespace(event_id="qq-turn:test")
    request = SimpleNamespace(deadline_at=utc_now() + timedelta(seconds=seconds))
    policy = QQLatencyOptions(qq_web_policy="after_gate")
    prepared = {"prepare_id": "qq-prepare:test", "snapshot_hash": "b" * 64,
        "policy": policy.model_dump(), "policy_version": policy.version,
        "web_plan": {"steps": steps or [{"step_id": "s", "action": "search", "query": "更新"}]}}
    return event, request, policy, prepared


def test_search_read_and_replay_are_durable_without_selector(tmp_path, monkeypatch):
    async def run():
        sup, calls = rig(tmp_path)
        try:
            event, request, policy, prepared = _case(monkeypatch, steps=[
                {"step_id": "s", "action": "search", "query": "更新"},
                {"step_id": "r", "action": "read", "search_results": [{"step_id": "s", "result_indices": [0]}]},
            ])
            caps = await qq_web.web_capabilities(sup, event, request, policy=policy)
            assert not calls and caps["registered_sources"]
            first = await qq_web.execute_qq_web(sup, event, request, prepared, capabilities=caps)
            assert len(calls) == 2 and first["evidence_refs"]
            assert any(source["content"] == "BODY_EVIDENCE_TEXT" for source in first["sources"])
            assert first == await qq_web.execute_qq_web(sup, event, request, prepared, capabilities=caps)
            assert len(calls) == 2
        finally:
            await sup.web.close()
    asyncio.run(run())


def test_registered_user_link_works_without_query_field(tmp_path, monkeypatch):
    async def run():
        sup, calls = rig(tmp_path)
        try:
            event, request, policy, prepared = _case(monkeypatch)
            caps = await qq_web.web_capabilities(sup, event, request, policy=policy)
            prepared["web_plan"] = {"steps": [{"step_id": "r", "action": "read",
                "source_refs": [caps["registered_sources"][0]["source_ref"]]}]}
            context = await qq_web.execute_qq_web(sup, event, request, prepared, capabilities=caps)
            assert len(calls) == 1 and calls[0].url.path == "/extract" and context["evidence_refs"]
        finally:
            await sup.web.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "empty", "partial", "quota", "revoked", "budget"])
def test_optional_web_failures_preserve_real_status(tmp_path, monkeypatch, failure):
    async def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private content must not leak")
        return httpx.Response(200, json={"results": []})

    async def run():
        sup, calls = rig(tmp_path, quota=1, handler=handler)
        try:
            event, request, policy, prepared = _case(monkeypatch, seconds=14 if failure == "budget" else 60)
            caps = await qq_web.web_capabilities(sup, event, request, policy=policy)
            if failure == "quota":
                sup.web.calls.reserve("consumed", "previous", "web.search.v1", "q", sup.web._day(), False, 1, 1)
            if failure == "revoked":
                sup.manifest = sup.manifest.model_copy(update={"web_access": sup.manifest.web_access.model_copy(update={"enabled": False})})
            if failure == "partial":
                from harness.embodiment.web_contracts import WebResult
                async def partial(*args, **kwargs):
                    return WebResult(status="partial")
                sup.web.execute = partial
            result = await qq_web.execute_qq_web(sup, event, request, prepared, capabilities=caps)
            assert result["status"] in {"partial", "unavailable"} and result["failures"]
            assert not result["evidence_refs"]
            assert "private content" not in str(result)
            if failure in {"quota", "revoked", "budget"}:
                assert not calls
        finally:
            await sup.web.close()
    asyncio.run(run())


@pytest.mark.parametrize("tamper", ["scope", "source", "policy", "snapshot", "trigger"])
def test_forged_scope_or_snapshot_fails_closed(tmp_path, monkeypatch, tamper):
    async def run():
        sup, calls = rig(tmp_path)
        try:
            event, request, policy, prepared = _case(monkeypatch)
            caps = await qq_web.web_capabilities(sup, event, request, policy=policy)
            if tamper == "scope":
                caps["scope"] = "qq:foreign"
            elif tamper == "source":
                prepared["web_plan"] = {"steps": [{"step_id": "r", "action": "read", "source_refs": ["web-source:foreign"]}]}
            elif tamper == "policy":
                prepared["policy_version"] = "bad"
            elif tamper == "snapshot":
                await qq_web.execute_qq_web(sup, event, request, prepared, capabilities=caps)
                prepared["snapshot_hash"] = "different"
            elif tamper == "trigger":
                monkeypatch.setattr(qq_web, "verified_qq_source", lambda request: {"digest": "changed"})
            before = len(calls)
            with pytest.raises(qq_web.QQWebBindingError):
                await qq_web.execute_qq_web(sup, event, request, prepared, capabilities=caps)
            assert len(calls) == before
        finally:
            await sup.web.close()
    asyncio.run(run())


@pytest.mark.parametrize("status", ["started", "uncertain", "failed"])
def test_unknown_external_effect_is_never_reissued(tmp_path, monkeypatch, status):
    async def run():
        sup, calls = rig(tmp_path)
        try:
            event, request, policy, prepared = _case(monkeypatch)
            caps = await qq_web.web_capabilities(sup, event, request, policy=policy)
            ident = QQWebPlan.model_validate(prepared["web_plan"]).execution_id(prepared["prepare_id"], "s")
            sup.web.calls.reserve(ident, caps["scope"], "web.search.v1", "unknown", sup.web._day(), False, 100, 20)
            if status != "started":
                sup.web.calls.finish(ident, error="web_timeout", uncertain=status == "uncertain")
            result = await qq_web.execute_qq_web(sup, event, request, prepared, capabilities=caps)
            assert result["failures"] == [{"code": "web_result_uncertain"}] and not calls
        finally:
            await sup.web.close()
    asyncio.run(run())


@pytest.mark.parametrize("steps", [
    [{"step_id": "r", "action": "read", "source_refs": ["https://invented.com"]}],
    [{"step_id": "r", "action": "read", "search_results": [{"step_id": "later", "result_indices": [0]}]}],
    [{"step_id": str(i), "action": "search", "query": "q"} for i in range(3)],
    [{"step_id": "s", "action": "search", "query": "q"}] * 2,
])
def test_model_intent_cannot_grant_extra_authority(steps):
    with pytest.raises(ValueError):
        QQWebPlan(steps=steps)
