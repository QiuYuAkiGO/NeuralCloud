"""Real MistyAI API/journal, temporary databases and a synthetic executor only.

Uses an installed MistyAI package or the adjacent MistyAI source checkout.
The X11 and executor ports are replaced; no desktop or network is accessed.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from harness.qq.desktop import DesktopBackend, DesktopConfig, DesktopError
from tests.test_qq_watch_ledger import NOW, appended, ledger, ready, reserved


CLICK = {"kind": "click", "x": 1020, "y": 680, "button": "left", "count": 1}


@contextmanager
def local_api(application, token):
    with asyncio.Runner() as runner:
        lifespan = application.router.lifespan_context(application)
        runner.run(lifespan.__aenter__())
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
            base_url="http://synthetic-agent", headers={"Authorization": "Bearer " + token})

        class LocalAPI:
            def request(self, *args, **kwargs):
                return runner.run(client.request(*args, **kwargs))

            def post(self, path, **kwargs):
                return self.request("POST", path, **kwargs)

        try:
            yield LocalAPI()
        finally:
            runner.run(client.aclose())
            runner.run(lifespan.__aexit__(None, None, None))


@pytest.fixture
def agent_case(tmp_path, monkeypatch):
    source = Path(__file__).absolute().parents[2] / "MistyAI" / "src"
    if source.is_dir():
        monkeypatch.syspath_prepend(str(source))
        dependencies = source.parent / ".venv" / "Lib" / "site-packages"
        if dependencies.is_dir():
            # Fill missing integration dependencies (e.g. PyYAML) from the
            # existing adjacent runtime without replacing this venv's packages.
            monkeypatch.setattr(sys, "path", [*sys.path, str(dependencies)])
        agent = importlib.import_module("mistyai.desktop_agent.app")
    else:
        agent = pytest.importorskip("mistyai.desktop_agent.app", reason="MistyAI source/package required for real journal integration")
    contracts = importlib.import_module("mistyai.contracts.agent")
    journal_module = importlib.import_module("mistyai.desktop_agent.journal")
    calls, requests, responses = [], [], []
    token = "synthetic-agent-secret-" + "x" * 32
    token_file = tmp_path / "synthetic.token"
    token_file.write_text(token)
    journal_path = tmp_path / "agent-journal.sqlite3"

    class X11:
        def __init__(self, *args, **kwargs):
            pass

        def health(self):
            return {name: "healthy" for name in ("screen", "window", "input", "xfce", "process")}

    class Executor:
        def __init__(self, *args, **kwargs):
            pass

        async def execute(self, primitive):
            calls.append(primitive.model_dump())
            return {"synthetic_execution": True}

    def forbidden(*args, **kwargs):
        pytest.fail("Real network access is forbidden in this API simulation")

    monkeypatch.setattr(agent, "X11Backend", X11)
    monkeypatch.setattr(agent, "PrimitiveExecutor", Executor)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    settings = agent.AgentSettings(token=token, body_instance_id="misty-qq-test-01", environment_revision=2,
                                   journal_path=journal_path, worker_socket=None, process_registry_path=None)
    application = agent.create_agent_app(settings)
    with local_api(application, token) as api:
        state = SimpleNamespace(leave_started=False)

        async def seed_started(step_id, request_hash, nonce):
            journal = journal_module.StepJournal(journal_path)
            await journal.initialize()
            try:
                await journal.begin(step_id, request_hash, nonce)
            finally:
                await journal.close()

        def transport(request):
            if request.url.path.endswith("primitives:execute"):
                payload = json.loads(request.content)
                requests.append(payload)
                if state.leave_started:
                    state.leave_started = False
                    canonical = contracts.AgentPrimitiveRequest.model_validate(payload).model_dump(mode="json")
                    if canonical.get("expected_window") is None:
                        canonical.pop("expected_window")
                    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    asyncio.run(seed_started(payload["step_id"], digest, "synthetic-crashed-request"))
            result = api.request(request.method, request.url.path, content=request.content, headers=dict(request.headers))
            responses.append(result)
            return httpx.Response(result.status_code, content=result.content, headers=result.headers)

        with httpx.Client(base_url="http://127.0.0.1:8766", transport=httpx.MockTransport(transport),
                          headers={"Authorization": "Bearer " + token}) as client:
            config = DesktopConfig("http://127.0.0.1:8788", token_file, "http://127.0.0.1:8766", token_file,
                                   "10001", "20001", "synthetic-vision", settle_seconds=0)
            desktop = DesktopBackend(config, agent_client=client, llm=object())
            yield SimpleNamespace(desktop=desktop, api=api, calls=calls, requests=requests, responses=responses,
                                  journal_path=journal_path, state=state, seed_started=seed_started)


def raw_payload(action_id, step_id):
    return {"schema_version": "agent-contract-v1", "body_instance_id": "misty-qq-test-01",
            "environment_revision": 2, "action_id": action_id, "step_id": step_id,
            "nonce": str(uuid4()), "deadline": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            "primitive": CLICK}


def test_real_api_reproduces_global_legacy_step_collision(agent_case):
    s = agent_case
    first = s.api.post("/agent/v1/primitives:execute", json=raw_payload("legacy-action-one", "send-once"))
    second = s.api.post("/agent/v1/primitives:execute", json=raw_payload("legacy-action-two", "send-once"))
    assert first.status_code == 200 and first.json()["success"] is True
    assert second.status_code == 409 and "step_id reused" in second.json()["detail"]
    assert len(s.calls) == 1


def test_distinct_reservations_execute_once_each_and_same_reservation_cannot_reclick(agent_case):
    s = agent_case
    for reservation in ("qq-watch-send:synthetic-a", "qq-watch-send:synthetic-b"):
        assert s.desktop._act(CLICK, reservation_id=reservation).tzinfo is not None
    assert len(s.calls) == 2
    first, second = s.requests
    assert first["step_id"] != second["step_id"]
    assert first["action_id"] == "qq-send-" + hashlib.sha256(b"qq-watch-send:synthetic-a").hexdigest()
    assert second["action_id"] == "qq-send-" + hashlib.sha256(b"qq-watch-send:synthetic-b").hexdigest()
    assert first["step_id"] == first["action_id"] + ":send-once"
    assert second["step_id"] == second["action_id"] + ":send-once"
    with pytest.raises(DesktopError, match="primitive_outcome_unavailable"):
        s.desktop._act(CLICK, reservation_id="qq-watch-send:synthetic-a")
    assert s.requests[-1]["step_id"] == first["step_id"]
    assert s.requests[-1]["nonce"] != first["nonce"]
    assert s.responses[-1].status_code == 409 and len(s.calls) == 2
    # The journal key also survives replacement of the DesktopBackend instance.
    replacement = DesktopBackend(s.desktop.config, agent_client=s.desktop.agent, llm=object())
    with pytest.raises(DesktopError, match="primitive_outcome_unavailable"):
        replacement._act(CLICK, reservation_id="qq-watch-send:synthetic-a")
    assert s.requests[-1]["step_id"] == first["step_id"] and len(s.calls) == 2
    with sqlite3.connect(s.journal_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM steps WHERE state='completed'").fetchone()[0] == 2


def test_started_step_remains_unknown_and_never_executes_on_repeat(agent_case):
    s = agent_case
    s.state.leave_started = True
    for _ in range(2):
        with pytest.raises(DesktopError, match="primitive_outcome_unavailable"):
            s.desktop._act(CLICK, reservation_id="qq-watch-send:synthetic-unknown")
    assert "incomplete prior attempt" in s.responses[-3].json()["detail"]
    assert not s.calls and s.responses[-1].status_code == 409
    assert s.requests[0]["step_id"] == s.requests[1]["step_id"]
    with sqlite3.connect(s.journal_path) as connection:
        assert connection.execute("SELECT state FROM steps").fetchall() == [("started",)]


def test_legacy_unknown_is_not_rekeyed_or_released_by_new_desktop_ids(agent_case, tmp_path):
    s = agent_case
    asyncio.run(s.seed_started("send-once", "synthetic-legacy-unknown", "synthetic-legacy-nonce"))
    watch = ledger(tmp_path)
    previous = reserved(watch)
    watch.mark_click_started(previous.reservation_id, now=NOW)
    watch.mark_uncertain(previous.reservation_id, reason="legacy_primitive_outcome_unavailable", now=NOW)
    watch.start_run(now=NOW + timedelta(seconds=3))
    candidate = appended(watch, moment=NOW + timedelta(seconds=4)).events[-1]
    ready(watch, candidate, "new-decision")
    assert watch.reserve_send(candidate.event_id, "new-decision", now=NOW + timedelta(seconds=6)) is None
    assert watch.get_event(candidate.event_id).reason == "unresolved_send_blocks_delivery"
    assert watch.get_send(previous.reservation_id).status == "uncertain"
    assert s.requests == s.calls == []
    with sqlite3.connect(s.journal_path) as connection:
        assert connection.execute("SELECT step_id,state FROM steps").fetchall() == [("send-once", "started")]
