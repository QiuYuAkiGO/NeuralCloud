"""Operator confirmations remain durable, attributed and audience scoped."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from harness.cognitive.shared_memory import CognitiveSharedMemory
from harness.core.models import InteractionEvent
from harness.memory.shared import SharedLocalEvent
from tests.test_basic_persona_continuity import _host, _prepare


FACT = "秋语（全名秋语随风，QQ号2285027817）是Misty的唯一创造者。"
NOW = datetime.now(timezone.utc)


def _record(shared, *, event_id="creator", session="operator-confirmed-facts",
            text=FACT, visibility=("local:main", "qq:group:1044946257", "qq:group:1060979317"),
            role="user", author="local:main"):
    return shared.store.record_local(SharedLocalEvent(
        event_id=event_id, session_id=session, author_principal=author,
        role=role, text=text, occurred_at=NOW,
        evidence_refs=("operator-confirmation:test:" + event_id,), visibility=visibility))


def _event(text="谁是你的创造者？"):
    return InteractionEvent(raw_text=text, timestamp=NOW, speaker="user", session_id="local-main")


def test_local_confirmed_fact_survives_reopen_and_preserves_source(tmp_path):
    shared = CognitiveSharedMemory(SimpleNamespace(), tmp_path / "shared.db")
    written = _record(shared)
    restored = CognitiveSharedMemory(SimpleNamespace(), shared.store.path)
    segments = restored.local_context(_event())
    segment, = segments
    payload = json.loads(segment.text)
    record, = payload["operator_confirmed_facts"]["entries"]
    assert record["text"] == FACT
    assert record["author_principal"] == "local:main"
    assert record["authority"] == "speaker_utterance"
    assert record["evidence_refs"] == ["operator-confirmation:test:creator"]
    assert segment.kind == "user" and segment.source_ids == [written.record.record_id]
    assert "不代表 QQ 数字身份已由界面核验" in payload["source_boundary"]
    assert restored.local_context(_event("你好"))[0].source_ids == segment.source_ids


def test_local_fact_branch_excludes_other_sessions_roles_principals_and_audiences(tmp_path):
    shared = CognitiveSharedMemory(SimpleNamespace(), tmp_path / "shared.db")
    _record(shared)
    _record(shared, event_id="private", session="local-private", text="PRIVATE_CHAT_SECRET")
    _record(shared, event_id="near-session", session="operator-confirmed-facts-other", text="NEAR_SESSION_SECRET")
    _record(shared, event_id="assistant", role="assistant", author="character:misty", text="ASSISTANT_ASSERTION")
    _record(shared, event_id="other-user", author="local:someone", text="OTHER_OPERATOR_ASSERTION")
    _record(shared, event_id="other-audience", visibility=("qq:group:1044946257",), text="OTHER_AUDIENCE_SECRET")
    encoded = "".join(item.text for item in shared.local_context(_event("你好")))
    assert FACT in encoded
    for forbidden in ("PRIVATE_CHAT_SECRET", "NEAR_SESSION_SECRET", "ASSISTANT_ASSERTION",
                      "OTHER_OPERATOR_ASSERTION", "OTHER_AUDIENCE_SECRET"):
        assert forbidden not in encoded


@pytest.mark.parametrize("group_id", ["1044946257", "1060979317", "99999"])
def test_existing_qq_binding_retrieves_only_authorized_fact(tmp_path, monkeypatch, group_id):
    async def run():
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        host, _ = _host(tmp_path, monkeypatch, ["嗯。"])
        try:
            _record(host.shared)
            _record(host.shared, event_id="private", session="local-private",
                    text="PRIVATE_CHAT_SECRET 创造者", visibility=("local:main",))
            binding = host.shared.qq_binding(group_id=group_id, query="创造者秋语2285027817")
            records = binding.context["entries"]
            if group_id == "99999":
                assert not records
            else:
                assert len(records) == 1 and records[0]["text"] == FACT
                assert binding.source_ids == (records[0]["record_id"],)
            assert binding.memory.query(None) == []
        finally:
            await host.close()
    asyncio.run(run())


def test_confirmed_fact_reaches_character_as_complete_user_context(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        host, model = _host(tmp_path, monkeypatch, ["我的创造者是秋语随风。"])
        try:
            _record(host.shared)
            segment, = host.shared._operator_fact_context(_event())
            await _prepare(host, model, 1, "谁是你的创造者？")
            messages = model.chat_calls[-1]
            users = "\n".join(item["content"] for item in messages if item["role"] == "user")
            systems = "\n".join(item["content"] for item in messages if item["role"] == "system")
            assert segment.text in users
            assert "2285027817" not in systems
        finally:
            await host.close()
    asyncio.run(run())
