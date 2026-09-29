"""Exercise final prompt grants and actual local/QQ production commit paths."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from harness.cognitive.production import ProductionChoice, _ask, _commit_memory, appraise_qq
from harness.config import Settings
from harness.llm.client import ChatResult, OpenAICompatClient
from harness.memory.evidence_v2 import EvidenceV2Store
from harness.memory.production import ProductionMemoryStore
from harness.production.store import ProductionStore
from tests.test_evidence_manifest import source
from tests.test_production_cognition import adapter_at, command, context


def proposal(view, *, subject="local:main", identifier="preference"):
    return {"group_id": identifier, "claims": [{"subject_id": subject,
        "topic": "饮品", "statement": "模型认为这是当前饮品偏好", "kind": "self_report",
        "report_mode": "direct", "facet": "preference", "anchors": [{
            "unit_id": view["unit_id"], "start": view["start"], "end": view["end"],
            "quote": view["text"], "role": "supports", "rationale": "当前作者原话"}]}]}


class EvidenceModel:
    def __init__(self, *, bad=False, during=None):
        self.bad, self.during, self.calls, self.payload = bad, during, 0, None

    def complete_chat_result(self, messages, **kwargs):
        self.calls += 1
        self.payload = json.loads(messages[-1]["content"])
        view = self.payload["evidence_views"][0]
        groups = [proposal(view)]
        if self.bad:
            groups.append(proposal(view, subject="qq:member:someone-else", identifier="wrong-author"))
        if self.during:
            self.during()
        return ChatResult(finish_reason="stop", content_type="text", text=json.dumps({
            "kind": "reply", "reason": "回应本人的自述", "text": "我记下了你的原话。",
            "memory_updates": groups, "used_memory_ids": [view["canonical_source_id"]]}, ensure_ascii=False))


def attach_memory(adapter, tmp_path):
    memory = ProductionMemoryStore(tmp_path / "evidence.sqlite3")
    adapter.production_evidence_store = memory
    def ingest(event):
        memory.ingest([source(event.event_id, author="local:main", text=event.raw_text)])
        return event.event_id
    adapter.production_ingest = ingest
    adapter.production_record_use = lambda ids, *, event_id, kind: memory.record_use(
        event_id, ids, audience="local:main", kind=kind)
    return memory


@pytest.mark.parametrize("bad", [False, True])
def test_local_preference_survives_independent_rejection_and_replay(tmp_path, bad):
    async def run():
        model = EvidenceModel(bad=bad)
        adapter = adapter_at(tmp_path, model)
        memory = attach_memory(adapter, tmp_path)
        request = command(text="现在我喜欢喝茶。")
        try:
            result = await adapter.prepare_response(request)
            assert result.status == ("failed" if bad else "applied"), result.error
            entries = memory.recall(audience="local:main")["entries"]
            claims = [row for row in entries if row["kind"] == "person"]
            assert len(claims) == 1
            claim = claims[0]
            assert request.payload["text"] in claim["current_impression"]
            assert claim["model_interpretation"] not in claim["current_impression"]
            assert claim["semantic_status"] == "model_interpretation"
            assert claim["quote_verified"] and claim["confidence"] is None
            assert len(memory.history(claim["record_id"])) == 1
            tasks = EvidenceV2Store(memory).pending_tasks(audience="local:main")
            assert len(tasks) == int(bad)
            if bad:
                assert not adapter._prepared
                assert tasks[0]["reason"] == "evidence_subject_not_authorized"
                assert memory.operator_snapshot()["evidence_tasks"][0]["task_id"] == tasks[0]["task_id"]
            again = await adapter.prepare_response(request)
            assert again.status == ("failed" if bad else "idempotent_replay") and model.calls == 1
            assert len(memory.history(claim["record_id"])) == 1
            with closing(memory._connect()) as db:
                uses = db.execute("SELECT kind FROM pm_receipts WHERE kind IN ('context_exposure','decision_use')").fetchall()
                assert sorted(row[0] for row in uses) == ["context_exposure", "decision_use"]
        finally:
            await adapter.close()
    asyncio.run(run())


def test_source_withdrawn_during_model_call_blocks_memory_and_reply(tmp_path):
    async def run():
        model = EvidenceModel()
        adapter = adapter_at(tmp_path, model)
        memory = attach_memory(adapter, tmp_path)
        model.during = lambda: memory.retract_source("evt-1", event_id="withdraw-during-call", reason="撤回")
        try:
            result = await adapter.prepare_response(command(text="现在我喜欢喝茶。"))
            assert result.status == "failed" and not adapter._prepared
            assert not memory.recall(audience="local:main").get("entries")
            assert not memory.operator_snapshot()["entries"]
        finally:
            await adapter.close()
    asyncio.run(run())


def test_qq_appraisal_binds_authenticated_author_and_group_audience(tmp_path):
    async def run():
        class QQModel(EvidenceModel):
            def complete_chat_result(self, messages, **kwargs):
                self.payload = json.loads(messages[-1]["content"])
                return ChatResult(finish_reason="stop", content_type="text", text=json.dumps({
                    "memory_updates": [proposal(self.payload["evidence_views"][0], subject="qq:member:20002")]}))
        model = QQModel()
        adapter = adapter_at(tmp_path, model)
        memory = ProductionMemoryStore(tmp_path / "qq.sqlite3")
        memory.ingest([source("qq-source", author="qq:member:20002", audience="qq:group:10001")])
        memory.ingest_qq = lambda *args, **kwargs: "qq-source"
        host = SimpleNamespace(adapter=adapter, chat=SimpleNamespace(memory=adapter.pipeline.memory),
            shared=SimpleNamespace(production_store=memory))
        owner = SimpleNamespace(preset=SimpleNamespace(persona=adapter.pipeline.memory.get_persona()), llm=model, derived_memory={})
        try:
            await appraise_qq(host, command(), SimpleNamespace(group_id="10001", trigger_member_id="20002", idle_intent=None),
                             {"source": {}}, owner)
            assert any(row["kind"] == "person" for row in memory.recall(audience="qq:group:10001")["entries"])
            assert not memory.recall(audience="qq:group:other").get("entries")
        finally:
            await adapter.close()
    asyncio.run(run())


def test_actual_http_usage_survives_invalid_independent_group(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "http-memory.sqlite3")
    memory.ingest([source("raw", author="local:main")])
    meter = ProductionStore(tmp_path / "http-meter.sqlite3")
    client = OpenAICompatClient(Settings(llm_model="fixture", llm_base_url="https://example.test/v1", call_log_enabled=False))
    model = EvidenceModel(bad=True)
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        reply = model.complete_chat_result(body["messages"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": reply.text}}],
            "usage": {"prompt_tokens": 111, "completion_tokens": 222, "total_tokens": 333}})
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        appraisal = _ask(client, {"current_source_id": "raw", "source_refs": ["raw"]}, ProductionChoice,
            context=context(), command_id="http-test", autonomous=False, meter=meter,
            evidence_store=memory, audience="local:main", subjects=("local:main",))
        with pytest.raises(ValueError, match="proposals_rejected"):
            _commit_memory(appraisal, memory, event_id="http-test", audience="local:main", now=datetime.now(timezone.utc))
        assert len(requests) == 1 and len(meter.pending_usage()) == 1
        assert "333" in json.dumps(meter.pending_usage())
        assert len(EvidenceV2Store(memory).pending_tasks(audience="local:main")) == 1
        assert len([row for row in memory.recall(audience="local:main")["entries"] if row["kind"] == "person"]) == 1
    finally:
        client.close()


@pytest.mark.parametrize("extra", [{"_manifest_id": "forged"}, {"memory_updates": [{"kind": "belief", "statement": "old unsafe DTO"}]}])
def test_model_cannot_supply_trusted_manifest_or_legacy_update_shape(extra):
    with pytest.raises(ValidationError):
        ProductionChoice.model_validate({"kind": "wait", "reason": "fixture", **extra})


def test_final_prompt_pruning_removes_raw_write_grant(tmp_path, monkeypatch):
    import harness.cognitive.production as cognitive
    memory = ProductionMemoryStore(tmp_path / "pruned.sqlite3")
    memory.ingest([source("raw", author="local:main"), source("hidden", author="local:main", text="秘密原话")])
    original = cognitive.bounded_payload
    def pruning(payload):
        value = original(payload)
        if "evidence_views" in value:
            value["evidence_views"] = value["evidence_views"][:1]
            value["memories"] = []
            value["source_refs"] = ["raw"]
        return value
    monkeypatch.setattr(cognitive, "bounded_payload", pruning)
    model = EvidenceModel()
    appraisal = _ask(model, {"current_source_id": "raw", "source_refs": ["raw", "hidden"],
        "memories": [{"source_ids": ["hidden"], "text": "not a raw quote"}]}, ProductionChoice,
        context=context(), command_id="pruned", autonomous=False, evidence_store=memory,
        audience="local:main", subjects=("local:main",), meter=ProductionStore(tmp_path / "prune-meter.sqlite3"))
    assert appraisal._shown_memory_refs == {"raw"}
    assert "hidden" not in appraisal._shown_refs
    from harness.memory.evidence_manifest import EvidenceManifestStore
    manifest = EvidenceManifestStore(memory).get(appraisal._manifest_id, audience="local:main")
    assert [view["canonical_source_id"] for view in manifest["views"]] == ["raw"]


def test_bad_group_schema_does_not_discard_good_correction(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "schema.sqlite3")
    memory.ingest([source("raw", author="local:main")])
    class Model(EvidenceModel):
        def complete_chat_result(self, messages, **kwargs):
            value = json.loads(super().complete_chat_result(messages, **kwargs).text)
            value["memory_updates"].append({"group_id": "bad-shape", "claims": [{"old_field": "invalid"}]})
            return ChatResult(finish_reason="stop", content_type="text", text=json.dumps(value))
    result = _ask(Model(), {"current_source_id": "raw"}, ProductionChoice, context=context(), command_id="schema",
        autonomous=False, evidence_store=memory, audience="local:main", meter=ProductionStore(tmp_path / "schema-meter.sqlite3"))
    with pytest.raises(ValueError, match="proposals_rejected"):
        _commit_memory(result, memory, event_id="schema", audience="local:main", now=datetime.now(timezone.utc))
    assert len([row for row in memory.recall(audience="local:main")["entries"] if row["kind"] == "person"]) == 1
    assert EvidenceV2Store(memory).pending_tasks(audience="local:main")[0]["reason"] == "evidence_group_schema_invalid"


@pytest.mark.parametrize("effect", ["affect", "goal_update"])
@pytest.mark.parametrize("long_source", [False, True])
def test_withdrawn_source_cannot_support_non_memory_effects(tmp_path, effect, long_source):
    async def run():
        class Model:
            def complete_chat_result(self, messages, **kwargs):
                memory.retract_source("evt-1", event_id="withdraw", reason="用户撤回")
                value = {"kind": "wait", "reason": "fixture"}
                if effect == "affect":
                    value[effect] = {"description": "情绪解释", "valence": 0, "activation": .1, "source_refs": ["evt-1"]}
                else:
                    value[effect] = {"goal_id": "new", "status": "active", "description": "探索", "success_criteria": ["完成"],
                        "evidence_refs": ["evt-1"], "reason": "来源已撤回"}
                return ChatResult(finish_reason="stop", content_type="text", text=json.dumps(value))
        adapter = adapter_at(tmp_path, Model())
        memory = attach_memory(adapter, tmp_path)
        try:
            result = await adapter.prepare_response(command(text="现在我喜欢喝茶。" * (2000 if long_source else 1)))
            assert result.status == "failed" and not adapter._prepared and not adapter.production_affect
        finally:
            await adapter.close()
    asyncio.run(run())
