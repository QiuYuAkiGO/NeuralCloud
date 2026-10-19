"""A late model result cannot create beliefs after control authority changes."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from harness.cognitive.production import appraise_qq, validate_runtime_fence
from harness.production.metering import production_call_scope
from tests.test_production_cognition import ChoiceModel, adapter_at, command, context


@pytest.mark.parametrize("change", ["paused", "resumed", "restarted", "offline"])
def test_late_local_model_cannot_write_affect_or_memory(tmp_path, monkeypatch, change):
    async def run():
        started, release = threading.Event(), threading.Event()
        model = ChoiceModel(started=started, release=release)
        original = model.complete_chat_result
        def complete(*args, **kwargs):
            result = original(*args, **kwargs)
            value = json.loads(result.text)
            value["memory_updates"] = []
            return SimpleNamespace(finish_reason="stop", content_type="text", has_tool_calls=False,
                                   has_refusal=False, text=json.dumps(value))
        model.complete_chat_result = complete
        adapter = adapter_at(tmp_path, model)
        writes = []
        monkeypatch.setattr("harness.cognitive.production._commit_memory", lambda *args, **kwargs: writes.append(kwargs))
        live = context()
        def current(**kwargs):
            if live.get("offline"):
                raise ConnectionError("unavailable")
            return live
        adapter.production_meter.context = current
        task = asyncio.create_task(adapter.prepare_response(command()))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            if change == "paused":
                live["runtime_lifecycle"] = "PAUSED"
            elif change == "resumed":
                live["epoch"] = 1
            elif change == "restarted":
                live["authorized_action_context"] = {"runtime_instance_id": "new-runtime"}
            else:
                live["offline"] = True
            release.set()
            result = await task
            assert result.status == "failed"
            assert not writes and not adapter.production_affect and not adapter._prepared
        finally:
            release.set()
            await adapter.close()
    asyncio.run(run())


def test_restricted_body_does_not_block_model_rest_recovery(tmp_path):
    async def run():
        adapter = adapter_at(tmp_path, ChoiceModel("rest_start"))
        request = command()
        request.payload["production_context"]["restricted_dialogue"] = True
        request.payload["production_context"]["body"]["snapshot"].update(exhausted=True, resting=False)
        adapter.production_meter.context = lambda **kwargs: {**context(), "restricted_dialogue": True}
        try:
            result = await adapter.prepare_response(request)
            assert result.status == "applied", result.error
            assert result.result.decision.policy_id == "production:rest:start"
        finally:
            await adapter.close()
    asyncio.run(run())


def _qq_memory(tmp_path):
    from harness.core.models import MemoryScope
    from harness.memory.production import Experience, ProductionMemoryStore
    from datetime import datetime, timezone
    memory = ProductionMemoryStore(tmp_path / "qq-production.sqlite3")
    memory.ingest([Experience(record_id="source", author_id="qq:member:20002", text="hello",
        occurred_at=datetime.now(timezone.utc), independent_key="qq-original", source_refs=("qq-original",),
        scope=MemoryScope(owner_id="misty", channel="qq_group", conversation_id="qq:group:10001",
                          visibility=["local:main", "qq:group:10001"]))])
    memory.ingest_qq = lambda value, **kwargs: "source"
    return memory


def test_qq_appraisal_checks_pause_before_derived_writes(tmp_path, monkeypatch):
    async def run():
        started, release = threading.Event(), threading.Event()
        class Appraisal:
            def complete_chat_result(self, messages, **kwargs):
                started.set()
                assert release.wait(3)
                return SimpleNamespace(finish_reason="stop", content_type="text", has_tool_calls=False,
                    has_refusal=False, text=json.dumps({"memory_updates": [],
                    "affect": {"description": "late", "valence": 0, "activation": .2, "source_refs": ["source"]}}))
        adapter = adapter_at(tmp_path, Appraisal())
        live = context()
        adapter.production_meter.context = lambda **kwargs: live
        writes = []
        monkeypatch.setattr("harness.cognitive.production._commit_memory", lambda *args, **kwargs: writes.append(kwargs))
        host = SimpleNamespace(adapter=adapter,
            chat=SimpleNamespace(memory=adapter.pipeline.memory),
            shared=SimpleNamespace(production_store=_qq_memory(tmp_path),
                apply_production_updates=lambda *args, **kwargs: writes.append(args)))
        owner = SimpleNamespace(preset=SimpleNamespace(persona=adapter.pipeline.memory.get_persona()), llm=adapter.pipeline.llm, derived_memory={})
        request = SimpleNamespace(group_id="10001", idle_intent=None, trigger_member_id="20002")
        task = asyncio.create_task(appraise_qq(host, command(), request, {"source": {}}, owner))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            live["runtime_lifecycle"] = "PAUSED"
            release.set()
            with pytest.raises(ValueError, match="runtime_fenced"):
                await task
            assert not writes and not adapter.production_affect
        finally:
            release.set()
            await adapter.close()
    asyncio.run(run())


def test_missing_control_identity_is_not_a_valid_fence():
    with pytest.raises(ValueError, match="fence_missing"):
        validate_runtime_fence(SimpleNamespace(), {"epoch": 0})


def test_invalid_affect_evidence_does_not_apply_memory_updates_first(tmp_path, monkeypatch):
    async def run():
        class Model:
            def complete_chat_result(self, messages, **kwargs):
                return SimpleNamespace(finish_reason="stop", content_type="text", has_tool_calls=False,
                    has_refusal=False, text=json.dumps({"kind": "wait", "reason": "fixture",
                    "memory_updates": [],
                    "affect": {"description": "unverified", "valence": 0, "activation": .1,
                               "source_refs": ["unavailable-source"]}}))
        adapter = adapter_at(tmp_path, Model())
        writes = []
        monkeypatch.setattr("harness.cognitive.production._commit_memory", lambda *args, **kwargs: writes.append(kwargs))
        try:
            result = await adapter.prepare_response(command())
            assert result.status == "failed"
            assert not writes and not adapter.production_affect
        finally:
            await adapter.close()
    asyncio.run(run())


@pytest.mark.parametrize("used_id", ["memory-1", "unavailable"])
def test_explicit_memory_use_and_verified_goal_outcomes(tmp_path, used_id):
    async def run():
        seen = []
        class Model:
            def complete_chat_result(self, messages, **kwargs):
                shown = json.loads(messages[-1]["content"])
                assert {"outcome-1", "tool-proof"} <= set(shown["source_refs"])
                return SimpleNamespace(finish_reason="stop", content_type="text", has_tool_calls=False,
                    has_refusal=False, text=json.dumps({"kind": "wait", "reason": "done", "used_memory_ids": [used_id],
                    "goal_update": {"goal_id": "goal-1", "status": "completed", "description": "checked result",
                        "success_criteria": ["observed"], "evidence_refs": ["outcome-1", "tool-proof"], "reason": "verified"}}))
        adapter = adapter_at(tmp_path, Model())
        segment = {"source_ids": ["memory-1"], "text": "a recalled memory"}
        adapter._context_provider = lambda event: [SimpleNamespace(source_ids=["memory-1"], model_dump=lambda **kwargs: segment)]
        adapter.production_record_use = lambda ids, **kwargs: seen.append((ids, kwargs))
        request = command()
        request.payload["production_context"]["goals"] = [{"id": "goal-1", "outcomes": ["outcome-1"],
                                                             "results": [{"evidence_refs": ["tool-proof"]}]}]
        try:
            result = await adapter.prepare_response(request)
            assert result.status == ("applied" if used_id == "memory-1" else "failed"), result.error
            uses = [(ids, details) for ids, details in seen if details["kind"] == "decision_use"]
            assert len(uses) == (1 if used_id == "memory-1" else 0)
            if uses:
                assert uses[0] == (["memory-1"], {"event_id": "decision-use:evt-1", "kind": "decision_use"})
        finally:
            await adapter.close()
    asyncio.run(run())


def test_qq_appraisal_can_cite_verified_body_without_treating_it_as_memory(tmp_path):
    async def run():
        class Model:
            def complete_chat_result(self, messages, **kwargs):
                return SimpleNamespace(finish_reason="stop", content_type="text", has_tool_calls=False,
                    has_refusal=False, text=json.dumps({"affect": {"description": "body interpretation", "valence": 0,
                    "activation": .1, "source_refs": ["body-proof"]}}))
        adapter = adapter_at(tmp_path, Model())
        verified = []
        memory = _qq_memory(tmp_path)
        validate = memory.validate_references
        memory.validate_references = lambda ids, **kwargs: verified.append(ids) or validate(ids, **kwargs)
        host = SimpleNamespace(adapter=adapter, chat=SimpleNamespace(memory=adapter.pipeline.memory),
            shared=SimpleNamespace(production_store=memory))
        owner = SimpleNamespace(preset=SimpleNamespace(persona=adapter.pipeline.memory.get_persona()), llm=adapter.pipeline.llm, derived_memory={})
        request = command()
        request.payload["production_context"]["body"]["snapshot"]["evidence_refs"] = [{"ref": "body-proof"}]
        try:
            await appraise_qq(host, request, SimpleNamespace(group_id="10001", idle_intent=None, trigger_member_id="20002"),
                             {"source": {}}, owner)
            assert verified == [["source"]]
            assert adapter.production_affect["qq:group:10001"]["source_refs"] == ["body-proof"]
        finally:
            await adapter.close()
    asyncio.run(run())


def test_qq_resume_cannot_reuse_gate_from_before_pause(monkeypatch):
    from harness.qq import prepared_turn
    from harness.qq.turn_checkpoints import QQTurnStateError, binding_identity
    binding = {"event_id": "event", "transport": "test", "group_id": "group", "self_member_id": "self",
               "runtime_instance_id": "runtime", "manifest_revision": 1,
               "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()}
    from harness.config import Settings
    settings = Settings(_env_file=None)
    from harness.qq import decision
    monkeypatch.setattr(decision,
                        "_load_configuration", lambda *args: (settings, None))
    row = {"snapshot_hash": "hash", "snapshot": {"production_context": context(),
           "expression_binding": prepared_turn._expression_binding(settings)}}
    monkeypatch.setattr(prepared_turn, "QQTurnCheckpointStore", lambda path: SimpleNamespace(get=lambda value: row))
    with production_call_scope({**context(), "epoch": 1}, command_id="resume", meter=object()):
        with pytest.raises(QQTurnStateError, match="qq_turn_cancelled"):
            prepared_turn.resume_qq_response(memory_db="unused", prepare_id=binding_identity(binding),
                                             snapshot_hash="hash", turn_binding=binding)


@pytest.mark.parametrize("channel", ["board", "diary"])
def test_production_channels_ignore_legacy_emotion_and_commit_delivered_facts_after_pause(tmp_path, monkeypatch, channel):
    from harness.board.config import BoardSettings
    from harness.cognitive.board import BoardDecision
    from harness.embodiment.board_contracts import BoardEntry
    from harness.llm.client import MockLLM
    from tests.test_basic_persona_continuity import _command, _host
    from tests.test_board_cognitive import BodyReader, prepare_command, receipt_for, response

    async def run():
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        token = tmp_path / "body.token"
        token.write_text("synthetic-token")
        config = tmp_path / "board.json"
        config.write_text(json.dumps({"token_file": str(token)}))
        monkeypatch.setenv("NC_BOARD_CONFIG", str(config))
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        previous = host.chat.llm
        host.chat.settings.emotion_mode = "on"
        host.chat.llm = MockLLM(chat_script=[response() if channel == "board" else '{"action":"write","text":"a diary fact"}'])
        live = context()
        host.adapter.production_meter = SimpleNamespace(context=lambda **kwargs: live)
        now = datetime.now(timezone.utc)
        request = prepare_command() if channel == "board" else _command("PrepareDiaryEntry", 1, {
            "entry_id": "diary-entry:1", "deadline_at": (now + timedelta(seconds=50)).isoformat(),
            "trigger": "user_request", "source_event_id": "evt_chat_1", "day": now.date().isoformat()})
        request.payload["production_context"] = context()
        try:
            prepared = await (host.prepare_board_entry(request) if channel == "board" else host.prepare_diary_entry(request))
            assert prepared.status == "applied", prepared.error
            decision = prepared.result["decision"]
            live["runtime_lifecycle"] = "PAUSED"
            if channel == "board":
                candidate = BoardDecision.model_validate(decision)
                receipt = receipt_for(candidate)
                reader = BodyReader(BoardEntry(receipt=receipt, title=candidate.title, text=candidate.text,
                                               source_event_ids=candidate.source_event_ids))
                monkeypatch.setattr(BoardSettings, "client", lambda self: reader)
                payload = {"entry_id": candidate.entry_id, "decision_id": candidate.decision_id,
                           "receipt": receipt.model_dump(mode="json")}
            else:
                payload = {"entry_id": decision["entry_id"], "decision_id": decision["decision_id"], "text": decision["text"],
                           "body_hash": hashlib.sha256(decision["text"].encode()).hexdigest(), "written_at": now.isoformat(),
                           "source_event_id": request.event_id}
            payload["production_context"] = context()
            commit = _command("CommitBoardEntry" if channel == "board" else "CommitDiaryEntry", 2, payload)
            result = await (host.commit_board_entry(commit) if channel == "board" else host.commit_diary_entry(commit))
            assert result.status == "applied" and result.result["memory_created"], result.error
        finally:
            host.chat.llm = previous
            await host.close()
    asyncio.run(run())
