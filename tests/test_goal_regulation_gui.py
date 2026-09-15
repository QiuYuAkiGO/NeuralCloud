"""Goal facade authorization and safe rendering; no live Runtime or body actions."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gui import runtime_server


@pytest.fixture
def goal_gui(monkeypatch):
    requests = []
    snapshot = {
        "enabled": True,
        "standing_goal": {"id": "psychological-balance", "statement": "保持可持续的参与", "status": "observing"},
        "goals": [], "active_episode": None, "episodes": [], "evaluations": [],
        "budget": {"calls_last_hour": 0, "maximum_calls_per_hour": 4}, "next_review_at": None,
    }

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=snapshot)
        if request.url.path.endswith("/goal:missing/pause"):
            return httpx.Response(404, json={"detail": "goal_not_found"})
        return httpx.Response(200, json={"id": "goal:fixture", "status": "queued"})

    def gateway_init(self, base_url, token):
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = httpx.AsyncClient(base_url=base_url, headers=self.headers, transport=httpx.MockTransport(handle))
        self.stream_task = self.monitor_stream_task = None

    async def no_stream(self):
        pass

    monkeypatch.setattr(runtime_server.RuntimeGateway, "__init__", gateway_init)
    monkeypatch.setattr(runtime_server.RuntimeGateway, "start", no_stream)
    with TestClient(runtime_server.create_app(runtime_url="http://runtime.test", control_token="private-fixture-token")) as client:
        yield client, requests, snapshot


def test_goal_page_and_read_use_existing_authenticated_runtime(goal_gui):
    client, requests, snapshot = goal_gui
    page = client.get("/goals")
    assert page.status_code == 200 and "目标与调节" in page.text
    assert "/static/goals.js" in page.text and "/static/goals.css" in page.text
    response = client.get("/api/goals")
    assert response.json() == snapshot
    assert len(requests) == 1 and requests[0].url.path == "/v1/goals"
    assert requests[0].headers["authorization"] == "Bearer private-fixture-token"
    assert "private-fixture-token" not in page.text + response.text
    assert client.get("/static/goals.js").status_code == 200
    assert 'href="/goals"' in client.get("/").text


def test_goal_create_preserves_client_idempotency_and_criteria(goal_gui):
    client, requests, _ = goal_gui
    payload = {"statement": "留下一个想法", "success_criteria": ["有独立回读证据"], "request_id": "request:one"}
    for _ in range(2):
        response = client.post("/api/goals", json=payload, headers={"Origin": "http://testserver"})
        assert response.status_code == 200
    assert len(requests) == 2
    assert all(request.url.path == "/v1/goals" and json.loads(request.content) == payload for request in requests)


@pytest.mark.parametrize("operation", ["pause", "resume", "cancel"])
def test_goal_operations_proxy_encoded_identity_and_request_id(goal_gui, operation):
    client, requests, _ = goal_gui
    response = client.post(f"/api/goals/goal%3Afixture/{operation}", json={"request_id": "retry:one"})
    assert response.status_code == 200
    assert requests[-1].url.path == f"/v1/goals/goal:fixture/{operation}"
    assert json.loads(requests[-1].content) == {"request_id": "retry:one"}


def test_goal_mutations_reject_cross_origin_and_unknown_operations(goal_gui):
    client, requests, _ = goal_gui
    for path in ("/api/goals", "/api/goals/goal:fixture/pause"):
        for headers in ({"Origin": "https://unrelated.test"}, {"Sec-Fetch-Site": "cross-site"}):
            assert client.post(path, json={}, headers=headers).status_code == 403
    assert client.post("/api/goals/goal:fixture/delete", json={}).status_code == 404
    assert client.post("/api/goals/.hidden/pause", json={}).status_code == 422
    assert requests == []


def test_goal_facade_preserves_runtime_failure(goal_gui):
    client, requests, _ = goal_gui
    response = client.post("/api/goals/goal:missing/pause", json={"request_id": "missing"})
    assert response.status_code == 404 and response.json()["detail"] == "goal_not_found"
    assert len(requests) == 1


def test_goal_rendering_preserves_evidence_boundaries_and_never_interprets_html():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    source = Path(runtime_server.STATIC_DIR) / "goals.js"
    script = r"""
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ''; this.disabled = false; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
  set innerHTML(_) { throw new Error('Untrusted HTML renderer used'); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ''; this.children = [...nodes]; }
  addEventListener() {}
}
const panels = new Map();
global.document = {createElement: tag => new Element(tag), getElementById: id => {
  if (!panels.has(id)) panels.set(id, new Element('panel'));
  return panels.get(id);
}};
const view = require(process.argv[1]);
const unsafe = '<img src=x onerror=alert(1)>';
const snapshot = {
  enabled: true,
  standing_goal: {id:'psychological-balance',status:'observing',statement:'保持参与'},
  goals:[{id:'goal:test',statement:unsafe,status:'paused',success_criteria:[unsafe],blocked_reason:'等待重新观察'}],
  active_episode:{id:'episode:1',statement:'整理想法',status:'waiting',target_dimensions:['social_connection'],
    steps:[{id:'step:1',statement:unsafe,status:'completed',evidence_refs:['javascript:alert(1)']}],
    results:[{status:'committed',call_id:'call:1',evidence_refs:[unsafe]}],next_review_at:'2026-09-12T08:00:00Z'},
  episodes:[],
  evaluations:[{id:'review:1',episode_id:'episode:1',at:'2026-09-12T07:00:00Z',outcome:'self_report_only',
    before:{social_connection:.3},after:{social_connection:.31},
    sources:{fact:['fact:original'],self_report:['appraisal:one'],drift:true},evidence_refs:[unsafe]}],
  budget:{calls_last_hour:0,maximum_calls_per_hour:4},next_review_at:null,
};
view.render(snapshot, {busy:false,offline:false});
const text = [...panels.values()].map(panel => panel.textContent).join('\n');
function flatten(node) { return [node, ...node.children.flatMap(flatten)]; }
const all = [...panels.values()].flatMap(flatten);
const safeTags = all.every(item => !['img','script','iframe','a'].includes(item.tag));
const controlEnabled = !panels.get('create').disabled;
const labels = panel => flatten(panel).filter(item => item.tag === 'button').map(item => item.textContent);
const standingControls = labels(panels.get('standing-goal'));
const userControls = labels(panels.get('goals'));
const pausedStandingControls = labels(view.goalCard({...snapshot.standing_goal,status:'paused'}, {enabled:true}));
const schedule = panels.get('schedule').textContent;
view.render({...snapshot,enabled:false}, {});
const disabled = panels.get('create').disabled;
view.render({enabled:true,goals:[],episodes:[],evaluations:[],budget:{}}, {offline:true});
console.log(JSON.stringify({text,safeTags,controlEnabled,standingControls,userControls,pausedStandingControls,schedule,disabled,offlineDisabled:panels.get('create').disabled}));
"""
    result = subprocess.run(
        [node, "-e", script, str(source)], capture_output=True, text=True, encoding="utf-8", timeout=10, check=True
    )
    rendered = json.loads(result.stdout)
    assert rendered["safeTags"] and rendered["controlEnabled"] and rendered["disabled"] and rendered["offlineDisabled"]
    assert rendered["standingControls"] == ["暂停目标"]
    assert rendered["pausedStandingControls"] == ["恢复目标"]
    assert rendered["userControls"] == ["恢复目标", "取消目标"]
    assert "最近一小时目标评估" in rendered["schedule"]
    assert "每小时目标评估上限" in rendered["schedule"]
    assert "此预算统计目标评估；各渠道另有既有的执行期限与冷却。" in rendered["schedule"]
    for text in ("<img src=x onerror=alert(1)>", "javascript:alert(1)", "事实证据", "自我评价", "时间漂移", "fact:original", "appraisal:one", "0.300 → 0.310", "仅有自评", "等待重新观察"):
        assert text in rendered["text"]
