"""One owner, durable candidates, and independent Linux readback; no live models."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from harness.board.config import BoardSettings
from harness.cognitive.board import BoardDecision
from harness.embodiment.board_contracts import BoardEntry, BoardReceipt, BoardScope
from harness.llm.client import ChatResult, MockLLM
from harness.memory.shared import SharedLocalEvent, SharedReadScope
from tests.test_basic_persona_continuity import _command, _host


@pytest.fixture(autouse=True)
def setup_board(tmp_path, monkeypatch):
    monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
    token = tmp_path / "body.token"
    token.write_text("synthetic-token")
    config = tmp_path / "board.json"
    config.write_text(json.dumps({"token_file": str(token)}), encoding="utf-8")
    monkeypatch.setenv("NC_BOARD_CONFIG", str(config))


def prepare_command(number=1, **updates):
    return _command("PrepareBoardEntry", number, {
        "entry_id": f"board-entry:{number}", "source_event_id": f"evt_chat_{number}",
        "body_instance_id": "misty-linux-board-01", "board_id": "misty-thoughts",
        "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=50)).isoformat(),
        "trigger": "user_request", "request_text": "写下你现在愿意留下的一点想法。",
        "homeostatic_summary": {"reported_state": "平静，有一点好奇"}, **updates})


def response(action="write", text="我觉得不急着给每件事起名字也挺好的。"):
    return json.dumps({"action": action, "title": "" if action == "wait" else "小念头",
                       "text": "" if action == "wait" else text}, ensure_ascii=False)


def receipt_for(decision):
    return BoardReceipt(scope=BoardScope(body_instance_id=decision.body_instance_id, board_id=decision.board_id),
        entry_id=decision.entry_id, decision_id=decision.decision_id,
        text_sha256=hashlib.sha256(decision.text.encode()).hexdigest(), created_at=datetime.now(timezone.utc),
        evidence_ref="board-evidence:test", sequence=1)


class BodyReader:
    def __init__(self, entry):
        self.entry = entry
        self.calls = []
        self.closed = 0

    async def evidence(self, request):
        self.calls.append(request)
        return self.entry

    async def close(self):
        self.closed += 1


def use_llm(host, script):
    original = host.chat.llm
    llm = MockLLM(chat_script=script)
    host.chat.llm = llm
    return llm, original


def test_prepare_uses_owner_complete_persona_and_shared_memory_without_delivery(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, [response()])
        try:
            host.shared.store.record_local(SharedLocalEvent(event_id="private-one", session_id="local-main",
                author_principal="local:main", role="user", text="SOURCE_ONLY_雨后的照片", occurred_at=datetime.now(timezone.utc),
                evidence_refs=("test:source",)))
            before = host.chat.pipeline.session_store.snapshot()
            persona = host.chat.memory.get_persona()
            command = prepare_command()
            result = await host.prepare_board_entry(command)
            assert result.status == "applied", result.error
            decision = BoardDecision.model_validate(result.result["decision"])
            assert decision.action == "write" and result.result["timing"]["model_calls"] == 1
            shown_persona = llm.chat_calls[-1][0]["content"].split("\n", 1)[1].split("\n\n", 1)[0]
            assert json.loads(shown_persona) == persona.model_dump(mode="json")
            assert "SOURCE_ONLY_" in llm.chat_calls[-1][1]["content"]
            assert "SOURCE_ONLY_" not in llm.chat_calls[-1][0]["content"]
            assert decision.source_event_ids[0] == command.event_id
            assert host.chat.pipeline.session_store.snapshot() == before
            assert not host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",))).records
            replay = await host.prepare_board_entry(command)
            assert replay.status == "idempotent_replay" and replay.result == result.result
            assert len(llm.chat_calls) == 1 and host.chat.memory.get_persona() == persona
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


def test_restart_commit_verifies_readback_and_is_idempotent(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        _, original = use_llm(host, [response()])
        result = await host.prepare_board_entry(prepare_command())
        assert result.status == "applied", result.error
        decision = BoardDecision.model_validate(result.result["decision"])
        receipt = receipt_for(decision)
        body = BodyReader(BoardEntry(receipt=receipt, text=decision.text, title=decision.title,
            source_event_ids=decision.source_event_ids))
        monkeypatch.setattr(BoardSettings, "client", lambda self: body)
        host.chat.llm = original
        await host.close()
        restored, _ = _host(tmp_path, monkeypatch, ["unused"])
        try:
            command = _command("CommitBoardEntry", 2, {"entry_id": decision.entry_id,
                "decision_id": decision.decision_id, "receipt": receipt.model_dump(mode="json")})
            result = await restored.commit_board_entry(command)
            assert result.status == "applied", result.error
            assert result.result["memory_created"] is True and len(body.calls) == 1 and body.closed == 1
            assert result.result["receipt"] == receipt.model_dump(mode="json")
            assert (await restored.commit_board_entry(command)).status == "idempotent_replay"
            assert len(body.calls) == 1
            records = restored.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",))).records
            assert len(records) == 1 and records[0].text == decision.text
            assert not restored.shared.store.query(SharedReadScope(audience="qq:group:1044946257")).records
            assert not any(restored.chat.pipeline.session_store.snapshot().values())
        finally:
            await restored.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["receipt", "text", "title", "sources", "scope"])
def test_forged_or_inconsistent_delivery_never_becomes_memory(failure, tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        _, original = use_llm(host, [response()])
        try:
            result = await host.prepare_board_entry(prepare_command())
            decision = BoardDecision.model_validate(result.result["decision"])
            receipt = receipt_for(decision)
            entry = BoardEntry(receipt=receipt, text=decision.text, title=decision.title,
                source_event_ids=decision.source_event_ids)
            if failure == "receipt":
                receipt = receipt.model_copy(update={"text_sha256": "0" * 64})
            elif failure == "scope":
                receipt = receipt.model_copy(update={"scope": receipt.scope.model_copy(update={"board_id": "another"})})
            else:
                key = "source_event_ids" if failure == "sources" else failure
                entry = entry.model_copy(update={key: ("evt_forged",) if failure == "sources" else "不同内容"})
            body = BodyReader(entry)
            monkeypatch.setattr(BoardSettings, "client", lambda self: body)
            command = _command("CommitBoardEntry", 2, {"entry_id": decision.entry_id,
                "decision_id": decision.decision_id, "receipt": receipt.model_dump(mode="json")})
            result = await host.commit_board_entry(command)
            assert result.status == "failed"
            assert not host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",))).records
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("raw,expected", [(response("wait"), "applied"), ("not-json", "failed"),
    (json.dumps({"action": "wait", "text": "旧草稿"}), "failed"),
    (json.dumps({"action": "write", "text": ""}), "failed")])
def test_wait_is_distinct_from_model_error(raw, expected, tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, [raw])
        try:
            command = prepare_command()
            first = await host.prepare_board_entry(command)
            assert first.status == expected
            replay = await host.prepare_board_entry(command)
            assert replay.status == ("idempotent_replay" if expected == "applied" else "failed")
            assert len(llm.chat_calls) == (1 if expected == "applied" else 2)
            if expected == "applied":
                assert first.result["decision"]["action"] == "wait"
                assert not first.result["decision"]["text"]
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("update", [{"body_instance_id": "wrong-body"}, {"source_event_id": "evt_wrong"},
    {"model_timeout_seconds": 26}, {"base_url": "http://untrusted.invalid"},
    {"deadline_at": (datetime.now(timezone.utc)+timedelta(seconds=600)).isoformat()}])
def test_untrusted_scope_deadline_or_client_overrides_rejected_before_model(update, tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, [response()])
        try:
            result = await host.prepare_board_entry(prepare_command(**update))
            assert result.status == "failed" and llm.chat_calls == []
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


def test_board_uses_same_lock_and_bounded_owner_call(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, [response("wait")])
        kwargs_seen = []
        complete = llm.complete_chat_result
        def bounded(messages, **kwargs):
            kwargs_seen.append(kwargs)
            return complete(messages, **kwargs)
        monkeypatch.setattr(llm, "complete_chat_result", bounded)
        try:
            await host.adapter._lock.acquire()
            task = asyncio.create_task(host.prepare_board_entry(prepare_command(trigger="homeostasis",
                model_timeout_seconds=8)))
            await asyncio.sleep(0)
            assert not task.done() and not llm.chat_calls
            host.adapter._lock.release()
            result = await task
            assert result.status == "applied", result.error
            assert len(kwargs_seen) == 1
            assert 0 < kwargs_seen[0]["timeout_seconds"] <= 8 and kwargs_seen[0]["max_tokens"] == 2048
            assert kwargs_seen[0]["response_format"] == {"type": "json_object"}
        finally:
            if host.adapter._lock.locked():
                host.adapter._lock.release()
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


def test_model_timeout_is_durable_failure_not_natural_wait(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, [])
        calls = []
        def timeout(*args, **kwargs):
            calls.append(kwargs)
            raise TimeoutError("private_url_or_prompt_must_not_be_reported")
        monkeypatch.setattr(llm, "complete_chat_result", timeout)
        try:
            command = prepare_command()
            result = await host.prepare_board_entry(command)
            assert result.status == "failed" and "decision" not in result.result
            assert result.result["diagnostic"]["stage"] == "model_call"
            assert result.result["diagnostic"]["model_attempts"][0]["status"] == "request_failed"
            assert result.error == "board_shared_prepare_failed:model_call:TimeoutError"
            replay = await host.prepare_board_entry(command)
            assert replay.status == "failed" and replay.idempotency_hit and len(calls) == 1
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


def test_commit_after_body_write_recovers_prior_memory_projection(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        _, original = use_llm(host, [response()])
        try:
            result = await host.prepare_board_entry(prepare_command())
            decision = BoardDecision.model_validate(result.result["decision"])
            receipt = receipt_for(decision)
            entry = BoardEntry(receipt=receipt, text=decision.text, title=decision.title,
                source_event_ids=decision.source_event_ids)
            body = BodyReader(entry)
            monkeypatch.setattr(BoardSettings, "client", lambda self: body)
            # A crash after SQLite commit but before domain-ledger persistence
            # leaves this state; exact source import must repair idempotently.
            host.shared.commit_board_entry(entry)
            result = await host.commit_board_entry(_command("CommitBoardEntry", 2,
                {"entry_id": decision.entry_id, "decision_id": decision.decision_id,
                 "receipt": receipt.model_dump(mode="json")}))
            assert result.status == "applied" and result.result["memory_created"] is False
            assert len(body.calls) == 1
            assert host.shared.store.query(SharedReadScope(audience="local:main")).visible_count == 1
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["write", "wait"])
def test_fenced_model_json_uses_existing_parser_without_another_model_call(tmp_path, monkeypatch, action):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, ["```json\n" + response(action) + "\n```"])
        try:
            result = await host.prepare_board_entry(prepare_command())
            assert result.status == "applied", result.error
            assert result.result["decision"]["action"] == action
            assert len(llm.chat_calls) == 1
            assert not host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",))).records
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("raw,diagnostic", [
    ('```json\n{"action":"write","text":"PRIVATE_MODEL_TEXT","PRIVATE_SECRET_KEY":"SECRET_VALUE"}\n```',
     "model_schema:ValidationError:extra_forbidden"),
    ('```json\n{"action":"unknown","text":"PRIVATE_MODEL_TEXT"}\n```',
     "model_schema:ValidationError:literal_error"),
    ('```json\n{"text":"PRIVATE_MODEL_TEXT"}\n```', "model_schema:ValidationError:missing"),
    ("PRIVATE_MODEL_TEXT SECRET_VALUE", "model_json:ValueError"),
])
def test_strict_schema_and_safe_diagnostics_never_return_model_content_legacy_mode(legacy_json_mode, raw, diagnostic, tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, [raw])
        try:
            command = prepare_command()
            result = await host.prepare_board_entry(command)
            assert result.status == "failed" and "decision" not in result.result
            assert len(result.result["diagnostic"]["model_attempts"]) == 2
            assert result.error == "board_shared_prepare_failed:" + diagnostic
            replay = await host.prepare_board_entry(command)
            assert replay.status == "failed" and replay.error == result.error
            assert len(llm.chat_calls) == 2
            persisted = (tmp_path / "cognitive.json").read_text(encoding="utf-8")
            for secret in ("PRIVATE_MODEL_TEXT", "PRIVATE_SECRET_KEY", "SECRET_VALUE"):
                assert secret not in result.model_dump_json() and secret not in persisted
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("first,action", [
    (ChatResult(text="PRIVATE_MALFORMED"), "write"),
    (ChatResult(text="", content_type="null"), "wait"),
    (ChatResult(text=response("write"), finish_reason="length"), "wait"),
    (ChatResult(text='{"action":"wait","text":"invalid draft"}'), "write"),
])
def test_one_format_repair_preserves_choice_and_one_durable_candidate(first, action, tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, [response(action)])
        completed = llm.complete_chat_result
        calls = []

        def complete(messages, **kwargs):
            calls.append((messages, kwargs))
            return first if len(calls) == 1 else completed(messages, **kwargs)

        monkeypatch.setattr(llm, "complete_chat_result", complete)
        try:
            command = prepare_command(trigger="homeostasis")
            result = await host.prepare_board_entry(command)
            assert result.status == "applied", result.error
            assert result.result["decision"]["action"] == action
            assert result.result["timing"]["model_calls"] == len(calls) == 2
            # Windows monotonic readings may share one coarse clock tick;
            # the controlled-clock test below proves actual budget subtraction.
            assert 0 < calls[1][1]["timeout_seconds"] <= calls[0][1]["timeout_seconds"] <= 12
            assert all(call[1]["response_format"] == {"type": "json_object"} for call in calls)
            assert "PRIVATE_MALFORMED" not in json.dumps(calls[1][0])
            assert (await host.prepare_board_entry(command)).status == "idempotent_replay"
            assert len(calls) == 2
            assert not host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",))).records
            assert "PRIVATE_MALFORMED" not in (tmp_path / "cognitive.json").read_text(encoding="utf-8")
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("initial,stage", [
    (ChatResult(text=response(), has_refusal=True), "model_refusal"),
    (ChatResult(text=response(), finish_reason="content_filter"), "model_refusal"),
    (ChatResult(text=response(), has_tool_calls=True), "model_tool_calls"),
    (ChatResult(text=response(), finish_reason="tool_calls"), "model_tool_calls"),
    (ChatResult(text="", content_type="array"), "model_content"),
    (ChatResult(text=response(), finish_reason="unknown"), "model_finish"),
])
def test_non_format_failures_do_not_retry_or_create_candidate(initial, stage, tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, [])
        calls = []

        def complete(messages, **kwargs):
            calls.append(kwargs)
            return initial

        monkeypatch.setattr(llm, "complete_chat_result", complete)
        try:
            command = prepare_command()
            result = await host.prepare_board_entry(command)
            assert result.status == "failed" and result.error == f"board_shared_prepare_failed:{stage}:ValueError"
            assert result.result is None or "decision" not in result.result
            assert (await host.prepare_board_entry(command)).status == "failed"
            assert len(calls) == 1
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("first_seconds,second_seconds,expected_calls,expected_status", [
    (8, 3, 2, "applied"),
    (10.1, 0, 1, "failed"),
    (10, 1, 2, "applied"),
    (8, 4.1, 2, "failed"),
])
def test_all_attempts_share_original_twelve_second_budget(first_seconds, second_seconds,
                                                         expected_calls, expected_status, tmp_path, monkeypatch):
    async def run():
        from harness.cognitive import board

        clock = SimpleNamespace(value=100.0)
        monkeypatch.setattr(board, "time", SimpleNamespace(monotonic=lambda: clock.value))
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, [])
        calls = []

        def complete(messages, **kwargs):
            calls.append(kwargs)
            clock.value += first_seconds if len(calls) == 1 else second_seconds
            return ChatResult(text="not json" if len(calls) == 1 else response("wait"))

        monkeypatch.setattr(llm, "complete_chat_result", complete)
        try:
            result = await host.prepare_board_entry(prepare_command(trigger="homeostasis"))
            assert result.status == expected_status, result.error
            assert len(calls) == expected_calls
            assert calls[0]["timeout_seconds"] == 12
            if len(calls) == 2:
                assert calls[1]["timeout_seconds"] == 12 - first_seconds
            if expected_status == "applied":
                assert result.result["decision"]["action"] == "wait"
            assert not host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",))).records
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


def test_outer_timeout_discards_late_sync_worker_without_repair(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, [])
        entered, release = threading.Event(), threading.Event()
        calls = []

        def complete(messages, **kwargs):
            calls.append(kwargs)
            entered.set()
            release.wait(timeout=2)
            return ChatResult(text=response())

        monkeypatch.setattr(llm, "complete_chat_result", complete)
        try:
            command = prepare_command(trigger="homeostasis", model_timeout_seconds=0.05)
            task = asyncio.create_task(host.prepare_board_entry(command))
            assert await asyncio.to_thread(entered.wait, 1)
            result = await asyncio.wait_for(task, timeout=0.5)
            assert result.status == "failed" and "TimeoutError" in result.error
            assert not release.is_set()
            release.set()
            await asyncio.sleep(0.01)
            replay = await host.prepare_board_entry(command)
            assert replay.status == "failed" and len(calls) == 1
            assert not host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",))).records
        finally:
            release.set()
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


def test_board_model_output_limit_is_trusted_config_only(tmp_path, monkeypatch):
    async def run():
        import os
        from pathlib import Path

        path = Path(os.environ["NC_BOARD_CONFIG"])
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["model_max_tokens"] = 3072
        path.write_text(json.dumps(settings), encoding="utf-8")
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        llm, original = use_llm(host, [response("wait")])
        try:
            result = await host.prepare_board_entry(prepare_command())
            assert result.status == "applied" and llm.chat_call_options[0]["max_tokens"] == 3072
            rejected = await host.prepare_board_entry(prepare_command(number=2, model_max_tokens=8192))
            assert rejected.status == "failed" and len(llm.chat_calls) == 1
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())
