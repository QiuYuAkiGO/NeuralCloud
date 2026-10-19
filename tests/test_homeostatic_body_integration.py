"""Natural homeostatic drift through real Runtime/cognition/body persistence.

Only model text and elapsed heartbeat source times are scripted. The board HTTP
app runs in process against an isolated SQLite file; no live QQ or model is used.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import httpx
import pytest

from harness.board.config import BoardSettings
from harness.board.service import create_app
from harness.board.store import BoardStore
from harness.embodiment.capabilities import CapabilityRequirement
from harness.embodiment.providers.board import BOARD_CAPABILITIES, BoardClient, BoardProvider
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.llm.client import MockLLM
from harness.memory.shared import SharedReadScope
from harness.research.manifest import EmbodimentCondition, load_experiment_manifest
from harness.runtime.board import BoardController
from harness.runtime.models import RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle, utc_now
from harness.runtime.supervisor import RuntimeSupervisor
from tests.test_basic_persona_continuity import _host


@pytest.mark.parametrize("model_action", ["write", "wait", "malformed", "repair_write", "repair_wait"])
def test_natural_drift_heartbeat_reaches_body_with_durable_evidence(tmp_path, monkeypatch, model_action):
    async def run():
        token = "synthetic-isolated-homeostatic-body-token"
        token_path = tmp_path / "body.token"
        token_path.write_text(token)
        settings = BoardSettings(token_file=token_path)
        config_path = tmp_path / "board.json"
        config_path.write_text(settings.model_dump_json(), encoding="utf-8")
        monkeypatch.setenv("NC_BOARD_CONFIG", str(config_path))
        monkeypatch.delenv("NC_QQ_SESSION_CONFIG", raising=False)
        monkeypatch.delenv("NC_SHARED_MEMORY_DB", raising=False)
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        host, _ = _host(tmp_path / "owner", monkeypatch, ["unused"])
        original_llm = host.chat.llm
        action = model_action.removeprefix("repair_")
        text = "安静了一会儿，我想给这块小小的留言板留下一句话。"
        answer = ("This is not valid JSON." if model_action == "malformed" else
            json.dumps({"action": action, "title": "", "text": text if action == "write" else ""}, ensure_ascii=False))
        llm = MockLLM(chat_script=(["This is not valid JSON.", answer] if model_action.startswith("repair_") else [answer, answer] if model_action == "malformed" else [answer]))
        host.chat.llm = llm

        # Replay historical elapsed time, keeping real wall-clock command and
        # body deadlines valid. No factors, summaries or thresholds are edited.
        started_at = utc_now() - timedelta(hours=8)
        profile = load_profile("presets/homeostasis/qq-social-drive-v1.json")
        engine = HomeostasisEngine(profile, state_dir=tmp_path / "homeostasis", now=started_at, clock=utc_now)
        manifest = load_experiment_manifest(Path("presets/runtime/shared-misty-v1.json"))
        supervisor = RuntimeSupervisor(manifest=manifest, cognitive=host, homeostasis=engine,
            state_dir=tmp_path / "runtime")
        store = BoardStore(tmp_path / "body" / "board.sqlite3", settings.scope)
        app = create_app(store=store, token=token)
        requests = []

        @app.middleware("http")
        async def record_body_request(request, call_next):
            requests.append((request.method, request.url.path))
            return await call_next(request)

        monkeypatch.setattr(BoardSettings, "client", lambda self: BoardClient(self.base_url, token,
            transport=httpx.ASGITransport(app=app)))
        provider = BoardProvider(EmbodimentCondition(mode="grounded", body_instance_id=settings.body_instance_id,
            capability_requirements=[CapabilityRequirement(capability=name, required=True) for name in BOARD_CAPABILITIES]),
            settings.client(), settings.scope)
        controller = BoardController(supervisor, settings, tmp_path / "runtime", provider=provider)
        supervisor.board = controller
        try:
            async with app.router.lifespan_context(app):
                await supervisor.boot_and_recover()
                assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
                assert supervisor._response_homeostatic_interoception()["salience"] == "nominal"
                initial_social = engine.snapshot.dimensions["social_connection"].value
                controller.next_opportunity = 0
                controller.last_activity = time.monotonic() - settings.quiet_seconds - 1

                for step in range(1, 241):
                    event = await supervisor.submit_event(event_type=RuntimeEventType.HEARTBEAT,
                        priority=RuntimeEventPriority.HEARTBEAT, source="isolated-elapsed-time-replay",
                        source_time=started_at + timedelta(seconds=step * profile.max_heartbeat_step_seconds))
                    assert (await supervisor.queue.get()).event_id == event.event_id
                    result = await supervisor.dispatch(event)
                    assert result.status == "completed", result.detail
                    assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
                    summary = supervisor._response_homeostatic_interoception()
                    if summary["salience"] == "nominal":
                        assert supervisor.queue.empty()
                        assert not llm.chat_calls
                        assert not controller.turns.all()
                    else:
                        break
                else:
                    pytest.fail("Natural idle drift never produced a noticeable body opportunity")

                assert step > 1
                assert summary["salience"] in {"noticeable", "marked"}
                assert engine.snapshot.dimensions["social_connection"].value < initial_social
                assert summary["snapshot_revision"] == engine.snapshot.revision == step
                assert supervisor.queue.qsize() == 1
                reflection = await supervisor.queue.get()
                assert reflection.event_type == RuntimeEventType.BOARD_REFLECTION
                assert reflection.source == "board_opportunity"
                assert controller.turns.get(reflection.payload["entry_id"])["trigger"] == "homeostasis"

                result = await supervisor.dispatch(reflection)
                row = controller.turns.get(reflection.payload["entry_id"])
                expected = {"write": "committed", "wait": "wait", "malformed": "error"}[action]
                assert row["status"] == expected, result.detail
                assert result.status == ("failed" if model_action == "malformed" else "completed")
                assert len(llm.chat_calls) == (2 if model_action == "malformed" or model_action.startswith("repair_") else 1)
                prompt = json.loads(llm.chat_calls[0][1]["content"])
                assert prompt["trigger"] == "homeostasis"
                assert prompt["homeostatic_summary"] == summary
                assert engine.snapshot.revision == step  # Reflection is not another heartbeat/reward.
                assert ("POST", "/v1/board/read") in requests
                assert requests.count(("POST", "/v1/board/append")) == (action == "write")

                # Independent read-only connection proves body persistence, not
                # merely a provider return flag or a runtime journal status.
                with closing(sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)) as readback:
                    entries = readback.execute("SELECT entry_id,text,text_sha256,source_event_ids_json,evidence_ref FROM board_entries").fetchall()
                assert len(entries) == (action == "write")
                memories = host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",)))
                assert len(memories.records) == len(entries)
                if entries:
                    entry = entries[0]
                    assert entry[:3] == (row["entry_id"], text, hashlib.sha256(text.encode()).hexdigest())
                    assert reflection.event_id in json.loads(entry[3])
                    assert entry[4].startswith("board-evidence://sha256/")
                    assert ("POST", "/v1/board/evidence") in requests

                intents = [record.data["envelope"]["command_type"] for record in supervisor.journal.iter_records()
                    if record.record_type == "domain_command_intent" and record.event_id == reflection.event_id]
                assert intents == (["PrepareBoardEntry", "CommitBoardEntry"] if action == "write" else ["PrepareBoardEntry"])
                if model_action == "malformed":
                    errors = [record.data["result"].get("error") for record in supervisor.journal.iter_records()
                        if record.record_type == "domain_command_result" and record.event_id == reflection.event_id]
                    assert errors == ["board_shared_prepare_failed:model_json:JSONOutputError"]
                host.chat.llm = original_llm
                await supervisor.stop()
                await controller.close()
        finally:
            if supervisor.lifecycle != RuntimeLifecycle.STOPPED:
                host.chat.llm = original_llm
                await supervisor.stop()
                await controller.close()

    asyncio.run(run())
