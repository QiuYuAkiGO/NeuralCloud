from __future__ import annotations

from pathlib import Path

from harness.config import SCHEMA_VERSION
from harness.llm.client import MockLLM, parse_json_object
from harness.llm.embeddings import MockEmbedding
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


def test_jsonl_tracer_stamps_schema(tmp_path: Path) -> None:
    path = tmp_path / "memory.jsonl"
    tracer = JsonlTracer(path)
    tracer.record("episodic_write", {"event_id": "evt_1"})
    line = path.read_text(encoding="utf-8").strip()
    assert SCHEMA_VERSION in line
    assert "episodic_write" in line
