from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from harness.memory_encoder.config import EncoderConfig
from harness.memory_encoder.contracts import ChatRequest, MemoryBundle, MemoryQuery, MemoryRecord, ReadContext
from harness.memory_encoder.data import prepare_data, read_data
from harness.memory_encoder.packing import pack
from harness.memory_encoder.service import create_app
from harness.memory_encoder.sources import HyMemorySource, NeuralCloudSource, open_neuralcloud

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


class CharacterTokenizer:
    cls_token_id, sep_token_id, pad_token_id = 1, 2, 0

    def encode(self, text, **kwargs):
        return [ord(char) % 61 + 3 for char in text]

    def __call__(self, text, **kwargs):
        return {"input_ids": self.encode(text), "offset_mapping": [(i, i + 1) for i in range(len(text))]}


def bundle(content="小林说自己喜欢摄影；朋友猜测他喜欢游泳，但未证实。"):
    return MemoryBundle(provider="neuralcloud", audience="qq:group:100", read_at=NOW,
        records=(MemoryRecord(record_id="r1", revision="1", content=content),))


def test_json_roundtrip_and_request_cannot_supply_permissions():
    assert MemoryBundle.model_validate_json(bundle().model_dump_json()) == bundle()
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"messages": [{"role": "user", "content": "你好"}],
            "memory": {"provider": "neuralcloud", "query": "回忆", "audience": "local:main"}})


def test_packing_preserves_whole_record_and_omission_is_explicit():
    config = EncoderConfig()
    packed = pack(bundle(), CharacterTokenizer(), config)
    assert packed.input_ids and max(map(len, packed.input_ids)) <= 128
    assert len(packed.manifest.chunks) == len(packed.input_ids)
    reconstructed = "".join(c.text for c in packed.manifest.chunks)
    assert json.loads(reconstructed) == bundle().records[0].model_dump(mode="json")
    tight = pack(bundle(), CharacterTokenizer(), config.model_copy(update={"max_chunks": 1}))
    assert not tight.input_ids
    assert tight.manifest.omitted[0]["reason"] == "whole_record_budget"


def test_expired_records_do_not_enter_encoder():
    expired = bundle().records[0].model_copy(update={"valid_until": NOW})
    result = pack(bundle().model_copy(update={"records": (expired,)}), CharacterTokenizer(), EncoderConfig())
    assert not result.input_ids
    assert result.manifest.omitted[0]["reason"] == "outside_validity"


def test_real_neuralcloud_read_only_scope_and_retraction(tmp_path):
    from harness.core.models import MemoryScope
    from harness.memory.production import Experience, ProductionMemoryStore
    path = tmp_path / "memory.sqlite3"
    store = ProductionMemoryStore(path)
    def source(identifier, audience, text):
        return Experience(record_id=identifier, author_id="speaker:a", text=text,
            occurred_at=NOW, scope=MemoryScope(owner_id="misty", channel="qq_group",
                conversation_id=audience, visibility=[audience]), independent_key=identifier, source_refs=(identifier,))
    store.ingest([source("public", "qq:group:100", "摄影"), source("private", "qq:group:101", "私密内容")])
    adapter = NeuralCloudSource(open_neuralcloud(path, owner_id="misty", preset_id="misty"))
    context = ReadContext(audience="qq:group:100", principal="test", now=NOW)
    result = adapter.read(MemoryQuery(provider="neuralcloud", query="摄影"), context)
    assert [r.record_id for r in result.records] == ["public"]
    assert result.records[0].subject_ids == ()  # utterance author != subject
    assert adapter.validate(result, context)
    store.delete_source("public", event_id="delete:test")
    assert not adapter.validate(result, context)
    assert not adapter.read(MemoryQuery(provider="neuralcloud", query="摄影"), context).records


def test_hy_scope_versions_time_and_subject_are_not_invented():
    rows = [{"node_id": "r1", "content": "摄影", "user_id": "partition-a", "agent_id": "misty",
             "valid_until": "2026-10-20T00:00:00+00:00", "is_latest": True, "status": "active"},
            {"node_id": "r2", "content": "秘密", "user_id": "partition-b", "agent_id": "misty"}]
    calls = []
    def transport(request):
        calls.append(request)
        return rows if request["op"] == "search" else [rows[0]]
    source = HyMemorySource(transport, timezone="Asia/Shanghai")
    context = ReadContext(audience="a", principal="p", now=NOW,
                          hy_user_ids=("partition-a",), hy_agent_ids=("misty",))
    result = source.read(MemoryQuery(provider="hy_memory", query="兴趣"), context)
    assert [r.record_id for r in result.records] == ["r1"]
    assert result.records[0].subject_ids == ()
    assert source.validate(result, context)
    rows[0]["content"] = "更正为游泳"
    assert not source.validate(result, context)
    with pytest.raises(ValueError):
        source.read(MemoryQuery(provider="hy_memory", query="兴趣"), context.model_copy(update={"hy_user_ids": ()}))


def test_hy_read_bridge_disables_search_deletions_and_access_writes(monkeypatch):
    import os
    from types import SimpleNamespace
    from harness.memory_encoder.hy_worker import harden_client, read_only_environment
    for key in ("MEMORY_SEARCH_DEDUP_ENABLED", "MEMORY_RECALL_ACCESS_TRACKING", "MEMORY_PIPELINE_TRACE_ENABLED"):
        monkeypatch.setenv(key, "true")
    read_only_environment()
    assert os.environ["MEMORY_SEARCH_DEDUP_ENABLED"] == "false"
    assert os.environ["MEMORY_RECALL_ACCESS_TRACKING"] == "false"
    client = SimpleNamespace(_config=SimpleNamespace(
        recall=SimpleNamespace(access_tracking_enabled=True), history=SimpleNamespace(record_searches=True)))
    harden_client(client)
    assert not client._config.recall.access_tracking_enabled
    assert not client._config.history.record_searches


def test_service_authentication_and_post_generation_revision_fence():
    class Source:
        valid = True
        def read(self, query, context):
            assert context.audience == "qq:group:100"
            return bundle()
        def validate(self, memory, context):
            return self.valid
    source = Source()
    def engine(messages, memory, maximum):
        source.valid = False
        return {"answer": "must not escape stale fence"}
    client = TestClient(create_app(engine, {"neuralcloud": source}, {
        "secret": ReadContext(audience="qq:group:100", principal="test", now=NOW)}))
    request = {"messages": [{"role": "user", "content": "摄影"}],
               "memory": {"provider": "neuralcloud", "query": "摄影"}}
    assert client.post("/v1/memory/chat", json=request).status_code == 401
    response = client.post("/v1/memory/chat", json=request, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 409
    assert "must not escape" not in response.text


def test_datasets_are_reproducible_disjoint_and_counterfactual(tmp_path):
    first = prepare_data(tmp_path / "a", {"train": 30, "validation": 30, "test": 30})
    second = prepare_data(tmp_path / "b", {"train": 30, "validation": 30, "test": 30})
    assert first == second
    groups = [read_data(tmp_path / "a" / f"{name}.jsonl") for name in ("train", "validation", "test")]
    for key in ("story_id", "template_id"):
        sets = [{r[key] for r in group} for group in groups]
        assert not (sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2])
    pair = [row for row in groups[2] if row["pair_id"]][:2]
    assert len(pair) == 2 and pair[0]["messages"] == pair[1]["messages"]
    assert pair[0]["answer"] != pair[1]["answer"]
    events = read_data(tmp_path / "a" / "timeline.jsonl")
    assert len(events) == 10000
    assert datetime.fromisoformat(events[-1]["at"]) - datetime.fromisoformat(events[0]["at"]) == timedelta(days=90)


def test_pair_metric_does_not_count_only_one_answer_as_a_success():
    from harness.memory_encoder.experiments import metrics
    first = {"id": "a", "kind": "counterfactual", "pair_id": "p", "variant": 0, "correct": True}
    second = {**first, "id": "b", "variant": 1, "correct": False}
    assert metrics([first])["counterfactual_pairs"] == 0
    assert metrics([first, second])["counterfactual_pairs"] == 0
    assert metrics([first, {**second, "correct": True}])["counterfactual_pairs"] == 1


def test_release_rejects_reports_for_another_adapter(tmp_path):
    from harness.memory_encoder.checkpoints import write_json
    from harness.memory_encoder.validation import accept_release
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    (checkpoint / "adapter.safetensors").write_bytes(b"fixture")
    write_json(checkpoint / "manifest.json", {"training_status": "trained", "config_hash": "cfg",
        "training": {"arm": "native", "dataset": {"id": "test"}}})
    paths = []
    for name in ("profile", "evaluation", "sources", "replay"):
        path = tmp_path / (name + ".json")
        write_json(path, {"passed": True, "quality_passed": True, "config_hash": "cfg",
            "adapter_sha256": "wrong", "dataset": {"id": "test"}, "events": 10000,
            "span_days": 90, "restarts": 2, "unauthorized_records": 0})
        paths.append(path)
    assert accept_release(checkpoint, *paths)["accepted"] is False
