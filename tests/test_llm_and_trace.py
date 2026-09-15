from __future__ import annotations

from pathlib import Path

from harness.config import SCHEMA_VERSION, Settings
from harness.llm.client import MockLLM, parse_json_object, usage_from_api_payload
from harness.llm.embeddings import (
    GuardedEmbedding,
    MockEmbedding,
    build_embedding_client,
    embedding_endpoint_candidates,
)
from infra.trace.recorder import JsonlTracer


def test_mock_embedding_is_deterministic_and_similar() -> None:
    emb = MockEmbedding(dim=64)
    a = emb.embed_one("你以后不用管我了")
    b = emb.embed_one("你昨天不是说不会再管我了吗？")
    c = emb.embed_one("今天天气晴朗适合散步")
    assert a == emb.embed_one("你以后不用管我了")
    assert len(a) == 64

    def cos(x, y) -> float:
        import numpy as np

        va, vb = np.asarray(x), np.asarray(y)
        return float(va @ vb)

    assert cos(a, b) > cos(a, c)


def test_parse_json_object_from_fence() -> None:
    raw = parse_json_object("```json\n{\"ops\": []}\n```")
    assert raw == {"ops": []}


def test_mock_llm_routes() -> None:
    llm = MockLLM(routes={"hello": {"ok": True}})
    assert llm.complete_json(system="s", user="say hello")["ok"] is True


def test_mock_llm_complete_chat_revision_overrides_violation() -> None:
    llm = MockLLM()
    first = llm.complete_chat([{"role": "user", "content": "你昨天不是说不会再管我了吗？"}])
    assert "对不起" in first
    revised = llm.complete_chat(
        [
            {"role": "user", "content": "你昨天不是说不会再管我了吗？"},
            {"role": "system", "content": "Revision instruction: 保持嘴硬"},
        ]
    )
    assert "对不起" not in revised
    assert "不想管" in revised


def test_usage_from_api_payload_prefers_provider_counts() -> None:
    messages = [{"role": "user", "content": "hello"}]
    usage = usage_from_api_payload(
        {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
         "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 4},
        messages,
        "hi",
    )
    assert usage.source == "api"
    assert usage.prompt_tokens == 12
    assert usage.completion_tokens == 4
    assert usage.total_tokens == 16
    assert usage.cache_hit_tokens == 8
    assert usage.cache_miss_tokens == 4


def test_usage_from_api_payload_reads_openai_cached_tokens() -> None:
    usage = usage_from_api_payload(
        {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22,
         "prompt_tokens_details": {"cached_tokens": 16}},
        [{"role": "user", "content": "hello"}],
        "hi",
    )
    assert usage.cache_hit_tokens == 16
    assert usage.cache_miss_tokens == 4


def test_token_usage_add_and_for_call_keep_cache_counts() -> None:
    from harness.core.inference import TokenUsage

    first = TokenUsage(prompt_tokens=10, completion_tokens=2, cache_hit_tokens=8, cache_miss_tokens=2,
                       source="api").for_call()
    second = TokenUsage(prompt_tokens=20, completion_tokens=3, cache_hit_tokens=18, cache_miss_tokens=2,
                        source="api").for_call()
    total = first.add(second)
    assert total.cache_hit_tokens == 26 and total.cache_miss_tokens == 4
    assert total.calls == 2 and total.prompt_tokens == 30


def test_usage_from_api_payload_falls_back_to_estimate() -> None:
    messages = [{"role": "user", "content": "hello world"}]
    usage = usage_from_api_payload({}, messages, "hi")
    assert usage.source == "estimate"
    assert usage.total_tokens == usage.prompt_tokens + usage.completion_tokens
    assert usage.total_tokens > 0


def test_mock_complete_chat_result_includes_usage() -> None:
    llm = MockLLM()
    result = llm.complete_chat_result([{"role": "user", "content": "早上好。"}])
    assert result.text
    assert result.usage.prompt_tokens > 0
    assert result.usage.completion_tokens > 0
    assert result.usage.total_tokens == result.usage.prompt_tokens + result.usage.completion_tokens


def test_jsonl_tracer_stamps_schema(tmp_path: Path) -> None:
    path = tmp_path / "memory.jsonl"
    tracer = JsonlTracer(path)
    tracer.record("episodic_write", {"event_id": "evt_1"})
    line = path.read_text(encoding="utf-8").strip()
    assert SCHEMA_VERSION in line
    assert "episodic_write" in line


def test_guarded_embedding_degrades_to_none() -> None:
    class Boom:
        dim = 8

        def embed(self, texts):
            raise ConnectionError("down")

        def embed_one(self, text):
            return self.embed([text])[0]

    guarded = GuardedEmbedding(Boom())
    assert guarded.embed(["a", "b"]) == [None, None]
    assert guarded.embed_one("a") is None


def test_embedding_candidates_follow_local_aux_not_cloud_chat() -> None:
    settings = Settings(
        llm_base_url="https://api.deepseek.com",
        llm_api_key="sk-cloud",
        aux_llm_base_url="http://127.0.0.1:11434/v1",
        aux_llm_api_key="not-needed",
        embedding_base_url="http://127.0.0.1:8080/v1",
        embedding_model="bge-m3",
    )
    urls = [row[0] for row in embedding_endpoint_candidates(settings)]
    assert urls[0] == "http://127.0.0.1:8080/v1"
    assert "http://127.0.0.1:11434/v1" in urls
    assert all("deepseek" not in url for url in urls)


def test_build_embedding_client_falls_back_to_mock_when_probe_fails(monkeypatch) -> None:
    monkeypatch.setattr("harness.llm.client.probe_endpoint", lambda *a, **k: False)
    client = build_embedding_client(Settings(llm_timeout=1.0))
    assert isinstance(client, MockEmbedding)


def test_build_embedding_client_wraps_first_reachable_endpoint(monkeypatch) -> None:
    class FakeEmbed:
        def __init__(self, settings=None) -> None:
            self.settings = settings
            self.dim = 1024

        def embed(self, texts):
            return [[0.0] * 1024 for _ in texts]

        def embed_one(self, text):
            return [0.0] * 1024

        def close(self) -> None:
            return None

    monkeypatch.setattr("harness.llm.client.probe_endpoint", lambda *a, **k: True)
    monkeypatch.setattr("harness.llm.embeddings.OpenAICompatEmbedding", FakeEmbed)
    client = build_embedding_client(Settings(embedding_model="bge-m3"))
    assert isinstance(client, GuardedEmbedding)
    assert isinstance(client.inner, FakeEmbed)
    assert client.inner.settings.embedding_model == "bge-m3"
