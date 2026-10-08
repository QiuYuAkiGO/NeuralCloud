"""Runtime delivery and UI authorization with concurrent synthetic ingress."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from harness.interruption import InterruptionOptions
from harness.runtime import RuntimeSupervisor, PassiveHomeostasisAdapter
from tests.test_runtime_supervisor import _manifest, _pipeline_adapter
from tests.test_text_interruption import result
from tests.test_text_interruption import request as judge_request


@pytest.mark.parametrize("action", ["continue", "regenerate", "wait"])
def test_runtime_interleaving_and_authorized_ui_final(tmp_path, action):
    _, _, cognitive = _pipeline_adapter(tmp_path)
    manifest = _manifest().model_copy(update={"text_interruption": InterruptionOptions(mode="active", coalesce_seconds=0)})
    supervisor = RuntimeSupervisor(manifest=manifest, cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
    async def run():
        published, commands = [], []
        first_entered, release = asyncio.Event(), asyncio.Event()
        original = cognitive.prepare_response
        async def prepare(command):
            commands.append(command)
            if len(commands) == 1:
                first_entered.set()
                await release.wait()
            return await original(command)
        cognitive.prepare_response = prepare
        async def judge(request):
            return result(action, request.messages[-1]["id"])
        cognitive.assess_text_interruption = judge
        async def listener(topic, payload):
            if topic == "cognitive.ui":
                published.append(payload)
        supervisor.add_listener(listener)
        await supervisor.boot_and_recover()
        await supervisor.submit_user_message("original request", session_id="s")
        first = await supervisor.queue.get(timeout=0)
        task = asyncio.create_task(supervisor.dispatch(first))
        await asyncio.wait_for(first_entered.wait(), 3)
        await supervisor.submit_user_message("correction", session_id="s")
        following = await supervisor.queue.get(timeout=0)
        # New user ingress doesn't have to wait for generation/owner locks.
        release.set()
        await task
        first_finals = [msg for msg in published if msg["type"] == "final"]
        assert len(first_finals) == int(action == "continue")
        await supervisor.dispatch(following)
        finals = [msg for msg in published if msg["type"] == "final"]
        assert len(finals) == {"continue": 2, "regenerate": 1, "wait": 0}[action]
        assert all(msg["data"]["authorized_delivery"] for msg in finals)
        assert len(supervisor.delivery_backend.deliveries) == len(finals)
        if action == "regenerate":
            assert commands[-1].payload["text_interruption_context"]["original_request"] == "original request"
            assert commands[-1].deadline == commands[0].deadline
        elif action == "wait":
            assert len(commands) == 1
            assert any(msg["type"] == "reply_disposition" and msg["data"]["action"] == "coalesced" for msg in published)
        assert supervisor.text_turns.suppress_preview({"type": "final", "run_id": first.correlation_id, "data": {"text": "candidate"}})
        assert supervisor.text_turns.suppress_preview({"type": "stage", "stage": "reply_segment", "run_id": first.correlation_id})
        assert not supervisor.text_turns.suppress_preview({"type": "final", "run_id": "unrelated-call"})
        await supervisor.stop(reason="test done")
    asyncio.run(run())


def test_pause_fence_survives_resume_and_supersedes_loose_mode(tmp_path):
    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(manifest=_manifest().model_copy(update={
        "text_interruption": InterruptionOptions(mode="active")}), cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
    async def run():
        import time
        await supervisor.boot_and_recover()
        engine = supervisor.text_turns.engine
        engine.begin("text:s", "old", "old request", time.time() + 60)
        for i in range(5):
            engine.receive("text:s", str(i), "user", "busy")
        assert await engine.before_send("old") == "continue"
        await supervisor.pause(reason="user pause")
        await supervisor.resume(reason="user resume")
        assert await engine.before_send("old") == "fenced"
        await supervisor.stop(reason="done")
    asyncio.run(run())


def test_judge_runs_outside_main_lock_and_is_rejected_during_maintenance(tmp_path):
    from harness.llm.client import MockLLM
    _, pipeline, cognitive = _pipeline_adapter(tmp_path)
    model = MockLLM(chat_script=[json.dumps(result())])
    pipeline.llm = model
    async def run():
        async with cognitive._lock:
            value = await asyncio.wait_for(cognitive.assess_text_interruption(judge_request()), 1)
        assert value["action"] == "continue"
        assert model.chat_call_options[-1]["reasoning_effort"] == "none"
        assert model.chat_call_options[-1]["thinking"] == {"type": "disabled"}
        assert await cognitive.pause_maintenance(timeout=1)
        with pytest.raises(RuntimeError, match="owner_paused"):
            await cognitive.assess_text_interruption(judge_request())
        assert len(model.chat_calls) == 1
        await cognitive.close()
    asyncio.run(run())


def test_ingress_holds_output_before_async_acceptance_listener(tmp_path):
    import time
    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(manifest=_manifest().model_copy(update={
        "text_interruption": InterruptionOptions(mode="active", coalesce_seconds=0)}), cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
    async def run():
        await supervisor.boot_and_recover()
        entered, release = asyncio.Event(), asyncio.Event()
        async def listener(topic, payload):
            if topic == "event.accepted" and payload["payload"].get("text") == "new message":
                entered.set()
                await release.wait()
        supervisor.add_listener(listener)
        async def judge(request):
            return result("wait", request.messages[-1]["id"])
        cognitive.assess_text_interruption = judge
        engine = supervisor.text_turns.engine
        engine.begin("text:chat:s", "old", "original", time.time() + 60)
        incoming = asyncio.create_task(supervisor.submit_user_message("new message", session_id="s"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not incoming.done()
            assert await engine.before_send("old") == "wait"
        finally:
            release.set()
            await incoming
            await supervisor.stop(reason="done")
    asyncio.run(run())


def test_gui_request_correlation_exists_before_websocket_response(monkeypatch):
    from gui import runtime_server
    captured = []
    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True, "event_id": "synthetic"})
    original = runtime_server.RuntimeGateway.__init__
    def initialize(self, base_url, token):
        original(self, base_url, token)
        self.client = httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handle))
    async def start(self):
        pass
    monkeypatch.setattr(runtime_server.RuntimeGateway, "__init__", initialize)
    monkeypatch.setattr(runtime_server.RuntimeGateway, "start", start)
    with TestClient(runtime_server.create_app(runtime_url="http://runtime.test", control_token="synthetic")) as client:
        response = client.post("/api/chat", json={"text": "hello", "correlation_id": "run_0123456789abcdef"})
        assert response.status_code == 200 and response.json()["run_id"] == "run_0123456789abcdef"
        assert captured[0]["correlation_id"] == "run_0123456789abcdef"
        assert client.post("/api/chat", json={"text": "hello", "correlation_id": "invalid"}).status_code == 400


def test_independent_gui_also_keeps_browser_reply_identity(tmp_path):
    from gui.server import create_app
    with TestClient(create_app(development=True, control_mode="development_debug", config_path=tmp_path / "gui.json")) as client:
        response = client.post("/api/chat", json={"text": "早上好。", "correlation_id": "run_0123456789abcdef"})
        assert response.status_code == 200 and response.json()["run_id"] == "run_0123456789abcdef"


def test_gui_overlapping_replies_do_not_erase_other_users_and_dedupe(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for UI behavioral check")
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const state = {speakers: {}, messages: [
 {role:'user', text:'first', run_id:'one'}, {role:'character', pending:true, run_id:'one'},
 {role:'user', text:'second', run_id:'two'}, {role:'character', pending:true, run_id:'two'}]};
const context = vm.createContext({state, renderTranscript(){}, hideProtocolMarkers:text=>text, $:()=>({})});
vm.runInContext(source.slice(source.indexOf('const finalizedReplies'), source.indexOf('function applyStageEvent')), context);
context.applyReplySegment({run_id:'one', data:{text:'partial'}});
context.commitCharacterSegments(['reply 1'], {response_id:'r1'}, 'one');
assert.equal(state.messages.filter(m=>m.role==='user').length, 2);
assert.equal(state.messages.find(m=>m.run_id==='two' && m.pending).pending, true);
context.commitCharacterSegments(['reply 1'], {response_id:'r1'}, 'one');
assert.equal(state.messages.filter(m=>m.text==='reply 1').length, 1);
context.applyReplyDisposition({run_id:'two',data:{prepare_id:'r2',action:'regenerate'}});
context.commitCharacterSegments(['obsolete'], {response_id:'r2'}, 'two');
assert.equal(state.messages.some(m=>m.text==='obsolete'), false);
assert.equal(state.messages.some(m=>m.pending || m.draft_segment), false);
assert.equal(state.messages.filter(m=>m.role==='user').length, 2);
'''
    subprocess.run([node, "-e", script, str(Path("gui/static/app.js").resolve())], check=True, capture_output=True, text=True, timeout=10)
