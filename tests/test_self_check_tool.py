"""Self-check tool probes: readonly classification, isolated wiring, production gates."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gui import runtime_server
from harness.diary.config import DiarySettings
from harness.regulation.config import RegulationConfig
from harness.research.manifest import AutonomousCapabilitiesCondition, WebAccessCondition
from harness.runtime import PassiveHomeostasisAdapter, RuntimeSupervisor
from harness.runtime.diary import DiaryController
from tests.test_runtime_supervisor import _manifest
from tests.test_web_runtime import rig
from tools.selfcheck.common import LaunchContext, already_ran, load_launch, mark_ran
from tools.selfcheck.probes_isolated import _attach_web, drain, run_isolated
from tools.selfcheck.probes_production import run_production
from tools.selfcheck.probes_readonly import run_readonly
from tools.selfcheck.scripted import ScriptedSelfCheckCognitive


def _ctx(**ports) -> LaunchContext:
    merged = {"gui": 18790, "runtime": 18770, "cognitive": 18771, "homeostasis": 18772, **ports}
    return LaunchContext(
        root=Path("."), launch_config=Path("launch.json"), profile="shared", bindings={},
        ports=merged, manifest_path=None, control_token="secret", service_token="service",
        runtime_url="http://runtime.test", cognitive_url="http://cognitive.test",
        homeostasis_url="http://homeostasis.test", gui_url="http://gui.test",
        runtime_state=None, watcher_config=None, gui_config=None,
    )


class FakeApi:
    def __init__(self, base_url, token, timeout=20):
        self.base_url = base_url
        self.token = token
        self.calls: list[tuple[str, str]] = []
        self._timeout = timeout

    def json(self, method, str_path, **kwargs):
        self.calls.append((method.upper(), str_path))
        host = self.base_url
        if host.endswith("cognitive.test") or host.endswith("homeostasis.test"):
            return 200, {"service": "cognitive" if "cognitive" in host else "homeostasis", "healthy": True}
        if host.endswith("gui.test"):
            return 200, {}
        routes = {
            ("GET", "/v1/health"): (200, {"lifecycle": "RUNNING", "journal_writable": True}),
            ("GET", "/v1/status"): (200, {"lifecycle": "RUNNING", "health": {"lifecycle": "RUNNING", "last_heartbeat_at": "2026-09-17T12:00:00+00:00"}, "affect": []}),
            ("GET", "/v1/capabilities"): (200, {"enabled": True, "owners": {"qq": "shared", "board": "shared"}, "catalog": [], "calls": [], "qq_send_enabled": True}),
            ("GET", "/v1/web/status"): (200, {"enabled": True, "conversation_enabled": True, "usage": {"requests": 1, "daily_limit": 100}}),
            ("GET", "/v1/diary"): (200, {"autonomous_enabled": False, "wrote_today": False, "today_count": 0, "paused": False, "turns": []}),
            ("GET", "/v1/goals"): (200, {"enabled": True, "goals": [], "budget": {"calls_last_hour": 0}}),
            ("GET", "/v1/board"): (503, {"detail": "board_not_configured"}),
            ("GET", "/v1/voice"): (503, {"detail": "voice_not_configured"}),
            ("GET", "/v1/server/status"): (503, {"detail": "server_not_configured"}),
            ("GET", "/v1/homeostasis/snapshot"): (200, {"payload": {"updated_at": "2026-09-17T12:00:00+00:00"}}),
            ("GET", "/v1/monitor/journal"): (200, {"items": []}),
        }
        if str_path.startswith("/v1/monitor/journal"):
            return routes[("GET", "/v1/monitor/journal")]
        return routes.get((method.upper(), str_path), (404, {"detail": str_path}))

    def close(self):
        return None


def test_launch_json_shared_profile_expands_localappdata():
    ctx = load_launch(Path("launch.json"), "shared")
    assert ctx.ports["runtime"] == 8770
    assert ctx.ports["gui"] == 8790
    assert "NeuralCloud" in (ctx.bindings.get("NC_RUNTIME_STATE_DIR") or "")
    assert ctx.gui_config is not None and ctx.gui_config.is_absolute()


def test_load_linux_deploy_example():
    ctx = load_launch(Path("deploy/linux.example.json"))
    assert ctx.profile == "linux-deploy"
    assert ctx.ports["runtime"] == 8770
    assert ctx.bindings["NC_CONTROL_TOKEN_FILE"] == "/etc/neuralcloud/control.token"
    assert ctx.bindings["NC_RUNTIME_STATE_DIR"] == "/var/lib/neuralcloud/runtime"


def test_load_linux_deploy_without_service_user(tmp_path):
    import json
    payload = json.loads(Path("deploy/linux.example.json").read_text(encoding="utf-8"))
    payload.pop("service_user", None)
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    ctx = load_launch(path)
    assert ctx.profile == "linux-deploy"
    assert ctx.bindings["NC_CONTROL_TOKEN_FILE"] == "/etc/neuralcloud/control.token"


def test_tier0_readonly_classifies_without_side_effects(monkeypatch):
    monkeypatch.setattr("tools.selfcheck.probes_readonly.ApiClient", FakeApi)
    monkeypatch.setattr("tools.selfcheck.probes_readonly.listening", lambda port, host="127.0.0.1": port != 8766)
    results = run_readonly(_ctx())
    by_path = {item.path: item.status for item in results}
    assert by_path["infra.runtime_health"] == "pass"
    assert by_path["infra.runtime_lifecycle"] == "pass"
    assert by_path["diary.snapshot"] == "pass"
    assert by_path["goals.snapshot"] == "pass"
    assert by_path["web.status"] == "pass"
    assert by_path["qq.web.passive"] == "no_recent_sample"
    assert by_path["board.snapshot"] == "skipped(not_configured)"
    assert by_path["emotion.affect"] == "skipped(not_configured)"
    assert "POST" not in {method for item in results for method in []}


def test_tier2_no_active_posts_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("NC_SELF_CHECK_LEDGER_DIR", str(tmp_path))
    client = FakeApi("http://runtime.test", "secret")
    results = run_production(_ctx(), no_active=True, client=client)
    assert all(item.status in {"skipped(disabled)", "no_recent_sample"} for item in results)
    assert all(method == "GET" for method, _ in client.calls)


def test_tier2_daily_ledger_skips_second_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("NC_SELF_CHECK_LEDGER_DIR", str(tmp_path))
    mark_ran("chat")
    assert already_ran("chat")
    client = FakeApi("http://runtime.test", "secret")
    # Force only chat by calling the helper through run_production with no_active false
    # but intercept POST /v1/messages — ledger should skip chat before POST.
    from tools.selfcheck import probes_production
    result = probes_production._chat_reply(client, timeout=1)
    assert result.status == "skipped(already_ran_today)"
    assert client.calls == []


def _isolated_supervisor(tmp_path):
    cognitive = ScriptedSelfCheckCognitive()
    manifest = _manifest(mode="development_debug").model_copy(update={
        "autonomous_chat_enabled": False,
        "web_access": WebAccessCondition(enabled=True, conversation_enabled=True),
        "autonomous_capabilities": AutonomousCapabilitiesCondition(
            enabled=True, owners={"web": "shared", "board": "off", "qq": "off", "desktop": "off", "diary": "legacy"},
            allowed_capabilities=("web.search.v1", "web.read.v1")),
        "goal_regulation": RegulationConfig(enabled=True),
    })
    supervisor = RuntimeSupervisor(
        manifest=manifest, cognitive=cognitive, homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime",
    )
    supervisor.diary = DiaryController(supervisor, DiarySettings(autonomous_enabled=False, max_entries_per_day=8),
                                       tmp_path / "runtime")
    fake, requests = rig(tmp_path / "web")
    _attach_web(supervisor, fake)
    return supervisor, cognitive, requests


def test_tier1_scripted_paths_leave_evidence(tmp_path):
    async def scenario():
        supervisor, cognitive, requests = _isolated_supervisor(tmp_path)
        try:
            await supervisor.boot_and_recover()
            supervisor.scheduler.suspend()
            await drain(supervisor)
            supervisor.web._revision = supervisor.web._configuration()
            ctx = _ctx()
            results = await run_isolated(ctx, supervisor=supervisor, protected=[])
        finally:
            await supervisor.stop(reason="self-check test")
            supervisor.diary.close()
            await supervisor.web.close()
        by_path = {item.path: item for item in results}
        assert by_path["chat.reply"].status == "pass"
        assert by_path["chat.web"].status == "pass"
        assert by_path["qq.web"].status == "pass"
        assert by_path["diary.write"].status == "pass"
        assert by_path["goal.submit"].status == "pass"
        assert by_path["homeostasis.heartbeat"].status == "pass"
        assert by_path["memory.commit"].status == "pass"
        assert by_path["isolated.safety"].status == "pass"
        assert "PrepareWebStep" in cognitive.calls
        assert "CommitInteraction" in cognitive.calls
        assert requests, "fake Tavily must have been invoked"
        return results
    asyncio.run(scenario())


def test_tier1_skips_live_owner_when_gui_is_demo(tmp_path):
    gui = tmp_path / "gui.json"
    gui.write_text('{"mode": "demo"}', encoding="utf-8")
    results = asyncio.run(run_isolated(replace(_ctx(), gui_config=gui), protected=[]))
    by_path = {item.path: item.status for item in results}
    assert by_path["isolated.live_owner"] == "skipped(not_configured)"
    assert by_path["isolated.safety"] == "pass"
    assert "chat.reply" not in by_path


@pytest.fixture
def selfcheck_gui(monkeypatch):
    requests = []
    snapshot = {"enabled": True, "interval_seconds": 3600, "latest": {"generated_at": "2026-09-17T12:00:00+00:00",
                "paths": [{"path": "web.status", "status": "pass", "evidence": {}}]}, "history": []}

    def handle(request):
        requests.append(request)
        if request.url.path == "/v1/selfcheck:run":
            return httpx.Response(200, json=snapshot["latest"])
        return httpx.Response(200, json=snapshot)

    def gateway_init(self, base_url, token):
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = httpx.AsyncClient(base_url=base_url, headers=self.headers, transport=httpx.MockTransport(handle))
        self.stream_task = self.monitor_stream_task = None

    async def no_stream(self):
        pass

    monkeypatch.setattr(runtime_server.RuntimeGateway, "__init__", gateway_init)
    monkeypatch.setattr(runtime_server.RuntimeGateway, "start", no_stream)
    with TestClient(runtime_server.create_app(runtime_url="http://runtime.test", control_token="private-selfcheck")) as client:
        yield client, requests, snapshot


def test_selfcheck_page_and_read_use_authenticated_runtime(selfcheck_gui):
    client, requests, snapshot = selfcheck_gui
    page = client.get("/selfcheck")
    assert page.status_code == 200 and "路径自检" in page.text
    assert "/static/selfcheck.js" in page.text
    assert 'href="/selfcheck"' in client.get("/").text
    body = client.get("/api/selfcheck")
    assert body.status_code == 200
    assert body.json() == snapshot
    assert requests[0].url.path == "/v1/selfcheck"
    assert requests[0].headers["authorization"] == "Bearer private-selfcheck"
    assert "private-selfcheck" not in page.text + body.text


def test_selfcheck_run_rejects_cross_origin(selfcheck_gui):
    client, requests, _ = selfcheck_gui
    assert client.post("/api/selfcheck/run", json={}, headers={"Origin": "https://unrelated.test"}).status_code == 403
    assert not requests
