"""Synthetic operator UI, source fences, and bounded maintenance regressions."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from harness.cognitive.qq_memory import wait_foreground_idle
from tests.test_basic_persona_continuity import _host
from tests.test_production_memory import NOW, belief, experience


def test_operator_snapshot_is_bounded_current_and_revocation_aware(tmp_path):
    from harness.memory.production import ProductionMemoryStore
    store = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    store.ingest([experience(), experience("counter", text="反证"),
                  experience("restricted", audience="qq:group:101").model_copy(update={
                      "scope": experience().scope.model_copy(update={"visibility": ["qq:group:101"]})})])
    store.revise(belief(counter_ids=("counter",)), event_id="belief")
    store.revise(belief("restricted-belief", source="restricted"), event_id="restricted-belief")
    store.revise(belief("future", valid_until=NOW + timedelta(days=1)), event_id="future")
    data = store.operator_snapshot(limit=1, now=NOW)
    assert len(data["entries"]) == 1
    assert data["entries"][0]["record_id"] != "restricted-belief"
    assert data["authority"] == "operator_only" and data["limit"] == 1
    store.retract_source("e1", event_id="withdraw", reason="撤销")
    assert store.operator_snapshot(now=NOW)["entries"] == []


def test_production_snapshot_api_is_operator_authenticated_without_legacy_scan(tmp_path, monkeypatch):
    async def scenario():
        from harness.cognitive import service
        host, _ = _host(tmp_path, monkeypatch, ["嗯"])
        host.chat.settings.production_memory_enabled = True
        store = host.shared.production_store
        store.ingest([experience()])
        store.revise(belief(), event_id="initial")
        current = {"description": "平静地听取", "scope": "local:main", "revision": 2,
                   "source_refs": ["e1"], "observed_at": NOW.isoformat()}
        host.adapter.production_affect.update({"history:local:main:old": {"description": "旧版本"}, "local:main": current})
        host.adapter.production_meter = SimpleNamespace(spool=SimpleNamespace(status=lambda: {"pending": 2, "in_flight": 1}))
        def fail(*args, **kwargs):
            raise AssertionError("legacy full archive scan")
        monkeypatch.setattr(host.shared.store, "query", fail)
        monkeypatch.setattr(host.qq_memory, "status", fail)
        monkeypatch.setattr(service, "CognitiveServiceHost", lambda **kwargs: host)
        credential = "operator-ui-synthetic-credential-0123456789"
        monkeypatch.setenv("NC_SERVICE_TOKEN", credential)
        app = service.create_app()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/v1/ui/snapshot")).status_code == 401
                response = await client.get("/v1/ui/snapshot", headers={"Authorization": "Bearer " + credential})
                assert response.status_code == 200, response.text
                data = response.json()
            assert "relationship" not in data and "psych" not in data
            assert data["production_memory"]["entries"][0]["statement"] == "愿意分享摄影经验"
            assert data["production_cognitive"]["affect"] == [current]
            assert data["production_cognitive"]["usage_spool"] == {"pending": 2, "in_flight": 1}
        finally:
            await host.close()
    asyncio.run(scenario())


def test_private_authoring_reads_and_commits_share_production_fence(tmp_path, monkeypatch):
    from harness.cognitive.prompts import chronological_memory
    from tests.test_board_memory import board_entry
    async def scenario():
        host, _ = _host(tmp_path, monkeypatch, ["嗯"])
        host.chat.settings.production_memory_enabled = True
        try:
            write = host.shared.commit_board_entry(board_entry())
            contexts = [host.shared.board_context(), host.shared.diary_context()]
            for context in contexts:
                assert context["schema_version"] == "production-memory-v1"
                assert context == chronological_memory(context)
                assert host.shared.validate_production_context(context, audience="local:main")
                assert write.record.record_id in {entry["record_id"] for entry in context["entries"]}
            host.shared.production_store.retract_source(write.record.record_id, event_id="remove-board", reason="撤回")
            for context in contexts:
                assert not host.shared.validate_production_context(context, audience="local:main")
            assert host.shared.board_context() == host.shared.diary_context() == {}
        finally:
            await host.close()
    asyncio.run(scenario())


def test_production_maintenance_wait_has_deadline_without_cancelling_chat():
    async def scenario():
        release = asyncio.Event()
        command = asyncio.create_task(release.wait())
        host = SimpleNamespace(adapter=SimpleNamespace(command_tasks={command}),
                               shared=SimpleNamespace(production_enabled=True))
        try:
            assert await asyncio.wait_for(wait_foreground_idle(host, asyncio.Event(), maximum_wait_seconds=.01), .3)
            assert not command.done()
            stopping = asyncio.Event()
            stopping.set()
            assert not await wait_foreground_idle(host, stopping, maximum_wait_seconds=0)
        finally:
            release.set()
            await command
    asyncio.run(scenario())


def test_production_ui_renders_diagnostics_and_escapes_source_text():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    script = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const names = ['esc', 'renderProductionCards', 'renderProductionMemory'];
eval(names.map(name => {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf('\nfunction ', start + 1);
  return source.slice(start, end);
}).join('\n'));
const attack = '<img src=x onerror=alert(1)>';
console.log(JSON.stringify(renderProductionCards({body_available:true, body_age_seconds:2,
 body_identity:{body_instance_id:'body-test',episode_id:'episode-test'},resting:true,exhausted:false,
 budget:{used:3,limit:12,remaining:9},usage_pending:1,tasks:2,queued:4,last_reason:'working',
 goals:[{description:attack,status:'active'}]}, {affect:[{description:attack,scope:'local:main',revision:3,source_refs:['e1']}]}) +
 renderProductionMemory({entries:[{subject_id:'qq:member:200',statement:attack,revision:2,
 kind:'self_report',authority:'legacy_interpreted_self_report',legacy_confidence:0.5,
 evidence:[{stance:'counter',text:attack,record_id:'e1',source_refs:['raw:1']}]}]},
 {processing:true,queue:{retry:2}}, {usage_spool:{pending:1,in_flight:2}})));
"""
    static = Path(__file__).resolve().parents[1] / "gui" / "static"
    result = subprocess.run([node, "-e", script, str(static / "app.js")], capture_output=True,
                            text=True, encoding="utf-8", timeout=10, check=True)
    html = json.loads(result.stdout)
    for expected in ("具身状态", "body-test", "episode-test", "3 / 12", "当前情绪解释", "r3", "e1",
                     "人物认识与证据", "操作者只读", "旧自述解释（未核验为原话）", "历史算法值 0.5（非概率）",
                     "反证", "学习中", "收据", "&lt;img"):
        assert expected in html
    assert "<img" not in html
    for name in ("app.js", "ops.js"):
        subprocess.run([node, "--check", str(static / name)], capture_output=True, check=True, timeout=10)
