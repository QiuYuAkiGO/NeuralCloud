"""Only independently verified, committed board posts enter emotion history."""
import asyncio

import pytest

from harness.board.config import BoardSettings
from harness.cognitive.board import BoardDecision
from harness.embodiment.board_contracts import BoardEntry
from tests.test_basic_persona_continuity import _command, _host
from tests.test_board_cognitive import (
    BodyReader, prepare_command, receipt_for, response, setup_board as setup_board, use_llm,
)


async def prepared(host):
    result = await host.prepare_board_entry(prepare_command())
    assert result.status == "applied", result.error
    candidate = BoardDecision.model_validate(result.result["decision"])
    receipt = receipt_for(candidate)
    entry = BoardEntry(receipt=receipt, text=candidate.text, title=candidate.title,
                       source_event_ids=candidate.source_event_ids)
    command = _command("CommitBoardEntry", 2, {"entry_id": candidate.entry_id,
        "decision_id": candidate.decision_id, "receipt": receipt.model_dump(mode="json")})
    return entry, command


@pytest.mark.parametrize("mode", ["on", "shadow", "off"])
def test_verified_board_entry_reaches_same_owner_window_once(mode, tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        _, original = use_llm(host, [response()])
        try:
            entry, command = await prepared(host)
            service = host.adapter.emotion_service
            service.settings = service.settings.model_copy(update={"emotion_mode": mode})
            body = BodyReader(entry)
            monkeypatch.setattr(BoardSettings, "client", lambda self: body)
            assert not service.snapshot()["windows"]  # Prepared text was never delivered.
            result = await host.commit_board_entry(command)
            assert result.status == "applied", result.error
            state = service.snapshot()
            if mode == "off":
                assert state["windows"] == {}
            else:
                window = state["windows"]["board:misty-thoughts"]
                assert len(window) == 1
                message = window[0]
                assert message["source_id"] == entry.receipt.entry_id
                assert message["scope"] == "board:" + entry.receipt.scope.board_id
                assert message["role"] == "character" and message["speaker"] == "misty"
                assert message["timestamp"] == entry.receipt.model_dump(mode="json")["created_at"]
                assert message["text"] == entry.title + "\n" + entry.text
                assert not state["global_affect"] and not state["reservations"]
                assert all(source["processed"] for source in state["sources"].values())
            assert (await host.commit_board_entry(command)).status == "idempotent_replay"
            assert service.snapshot() == state and len(body.calls) == 1
            # A new command after the SQLite commit rechecks the real body but
            # repairs the same source id without another window entry.
            retried = await host.commit_board_entry(_command("CommitBoardEntry", 3, command.payload))
            assert retried.status == "applied", retried.error
            assert not retried.result["memory_created"] and service.snapshot() == state
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["receipt_scope", "body_text", "memory_commit"])
def test_unverified_or_uncommitted_board_text_never_enters_emotion_window(failure, tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        _, original = use_llm(host, [response()])
        try:
            entry, command = await prepared(host)
            service = host.adapter.emotion_service
            service.settings = service.settings.model_copy(update={"emotion_mode": "on"})
            if failure == "receipt_scope":
                payload = dict(command.payload)
                receipt = entry.receipt.model_copy(update={"scope": entry.receipt.scope.model_copy(update={"board_id": "wrong"})})
                payload["receipt"] = receipt.model_dump(mode="json")
                command = command.model_copy(update={"payload": payload, "request_hash": None})
            elif failure == "body_text":
                entry = entry.model_copy(update={"text": "并未核验的候选文案"})
            else:
                def failed_commit(_entry):
                    raise RuntimeError("synthetic memory failure")
                monkeypatch.setattr(host.shared, "commit_board_entry", failed_commit)
            monkeypatch.setattr(BoardSettings, "client", lambda self: BodyReader(entry))
            result = await host.commit_board_entry(command)
            assert result.status == "failed"
            assert not service.snapshot()["windows"] and not service.snapshot()["sources"]
        finally:
            host.chat.llm = original
            await host.close()
    asyncio.run(run())
