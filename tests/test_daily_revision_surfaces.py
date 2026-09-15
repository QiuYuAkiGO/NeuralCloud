"""Authenticated maintenance controls, UI rendering and deployment boundaries."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from gui import runtime_server
from harness.deploy import runner
from harness.deploy.config import DeploymentConfig
from harness.deploy.snapshot import discover_shared_sources, build_sources_manifest, rewrite_config_paths
from harness.runtime.control import ControlPlane, create_control_app


@pytest.fixture
def revision_gui(monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"accepted": True, "config": {"api_key_configured": True}})
    def initialize(self, base_url, token):
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = httpx.AsyncClient(base_url=base_url, headers=self.headers, transport=httpx.MockTransport(handle))
        self.stream_task = self.monitor_stream_task = None
    async def start(self):
        pass
    monkeypatch.setattr(runtime_server.RuntimeGateway, "__init__", initialize)
    monkeypatch.setattr(runtime_server.RuntimeGateway, "start", start)
    with TestClient(runtime_server.create_app(runtime_url="http://runtime.test", control_token="private-control-fixture")) as client:
        yield client, requests


def test_gui_page_and_authenticated_maintenance_facade(revision_gui):
    client, requests = revision_gui
    page = client.get("/daily-revision")
    assert page.status_code == 200 and "每日修订" in page.text
    assert 'href="/daily-revision"' in client.get("/").text
    assert client.get("/api/daily-revision/status").status_code == 200
    assert client.get("/api/daily-revision/diff", params={"run_id": "revision-fixture"}).status_code == 200
    assert client.post("/api/daily-revision/run", json={"day": "2026-09-13"}, headers={"Origin": "http://testserver"}).status_code == 200
    assert all(row.headers["authorization"] == "Bearer private-control-fixture" for row in requests)
    assert [json.loads(row.content)["operation"] for row in requests] == ["daily_revision.status", "daily_revision.diff", "daily_revision.run"]
    assert json.loads(requests[1].content)["payload"] == {"run_id": "revision-fixture"}
    assert "private-control-fixture" not in page.text


@pytest.mark.parametrize("headers", [{"Origin": "https://other.example"}, {"Sec-Fetch-Site": "cross-site"}])
def test_daily_revision_mutation_rejects_cross_site(revision_gui, headers):
    client, requests = revision_gui
    assert client.post("/api/daily-revision/rollback", json={"run_id": "revision-fixture"}, headers=headers).status_code == 403
    assert not requests


def test_study_runtime_allows_only_named_memory_maintenance_operations():
    calls, rejected = [], []
    async def control(operation, payload):
        calls.append(operation)
        return {"accepted": True}
    supervisor = SimpleNamespace(manifest=SimpleNamespace(control_mode="study_runtime"),
        add_listener=lambda callback: None, _append_journal=lambda *args, **kwargs: rejected.append(kwargs),
        cognitive=SimpleNamespace(control=control))
    with TestClient(create_control_app(ControlPlane(supervisor, token="private-control-fixture"))) as client:
        endpoint = "/v1/cognitive/control"
        assert client.post(endpoint, json={"operation": "daily_revision.run"}).status_code == 401
        headers = {"Authorization": "Bearer private-control-fixture"}
        for operation in ("status", "history", "diff", "configure", "run", "dry_run", "retry", "rollback"):
            assert client.post(endpoint, json={"operation": "daily_revision." + operation}, headers=headers).status_code == 200
        for operation in ("debug.relationship", "daily_revision.arbitrary", "presets.restore"):
            assert client.post(endpoint, json={"operation": operation}, headers=headers).status_code == 403
    assert len(calls) == 8 and len(rejected) == 3


def test_daily_model_configuration_only_reaches_cognitive(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "read_token", lambda path: "a" * 48 if "control" in str(path) else "b" * 48)
    configured = "/etc/neuralcloud/daily-revision.json"
    cfg = DeploymentConfig(tmp_path / "deploy.json", bindings={"NC_DAILY_REVISION_CONFIG": configured})
    for service in cfg.services:
        values = runner.service_environment(cfg, service, release=tmp_path)
        assert values.get("NC_DAILY_REVISION_CONFIG") == (configured if service == "cognitive" else None)


def test_backup_includes_daily_queue_and_rewrites_credential_reference(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "daily-revision.json"
    key = tmp_path / "revision.key"
    queue = tmp_path / "daily.sqlite3"
    payload = {"api_key_file": str(key), "queue_path": str(queue)}
    config.write_text(json.dumps(payload), encoding="utf-8")
    sources = discover_shared_sources(repo, {"NC_DAILY_REVISION_CONFIG": str(config)})
    paths = {Path(row.path) for row in sources}
    assert {config, key, queue}.issubset(paths)
    manifest = build_sources_manifest(sources)
    rewritten = rewrite_config_paths(payload, manifest["path_rewrites"])
    assert rewritten["api_key_file"].startswith("/etc/neuralcloud/secrets/")
    assert rewritten["queue_path"].startswith("/var/lib/neuralcloud/daily-revision/")


def test_daily_ui_renders_untrusted_conclusions_and_real_relation_changes_as_text():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable")
    source = Path(runtime_server.STATIC_DIR) / "daily-revision.js"
    script = r"""
const assert = require('assert');
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.textContent=''; }
  append(...children) { this.children.push(...children); }
  addEventListener() {}
}
global.document={createElement:tag=>new Element(tag), getElementById:()=>null};
const view=require(process.argv[1]);
const text='<img src=x onerror=alert(1)>';
const result=view.result({status:'applied',run_id:'fixture',proposal:{impressions:[{statement:text,evidence_ids:['source-1']}],memories:[],relationships:[]},evidence:[{source_id:'source-1',text}],diff:{relationships:{before:[{target:'user',trust:.98}],after:[{target:'user',trust:1}]}}},()=>{});
const all=[];function walk(item){all.push(item.textContent);item.children.forEach(walk);}walk(result);
assert(all.includes(text));assert(all.includes('0.980 → 1.000'));assert(all.includes('撤销本次修订'));
console.log('ok');
"""
    result = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    assert "innerHTML" not in source.read_text(encoding="utf-8")
