from __future__ import annotations

import json

import pytest

from gui.observer import RecordingObserver
from harness.config import Settings
from harness.llm.client import REVISED_IN_CHARACTER_REPLY
from harness.pipeline.pipeline import InferencePipeline
from tests.factories import NOW, seed_running_example


def _jsonable(payload) -> None:
    json.dumps(payload, ensure_ascii=False)


def test_observer_fast_path_serializable(service) -> None:
    observer = RecordingObserver()
    pipeline = InferencePipeline(
        service,
        observer=observer,
        settings=Settings(
            complexity_threshold=0.5,
            validator_sample_rate=0.0,
            record_interaction=False,
        ),
    )
    final = pipeline.respond("早上好。", now=NOW, session_id="sess_hi")
    kinds = [event["stage"] for event in observer.events]
    assert kinds[0] == "ingest"
    assert "parse" in kinds
    assert "state_build" in kinds
    assert "gate" in kinds
    assert "interpret" not in kinds
    assert "compile" in kinds
    assert "generate" in kinds
    assert "validate" in kinds
    assert kinds[-1] == "final"
    assert final.path == "fast"
    for event in observer.events:
        _jsonable(event["data"])
        _jsonable(event["meta"])
    gate = next(event for event in observer.events if event["stage"] == "gate")
    assert "request" in gate["data"]
    assert "complexity" in gate["data"]
    assert gate["data"]["request"]["path"] == "fast"


def test_observer_deep_path_revision_loop(service) -> None:
    seed_running_example(service)
    observer = RecordingObserver()
    pipeline = InferencePipeline(
        service,
        observer=observer,
        settings=Settings(
            complexity_threshold=0.5,
            validator_sample_rate=1.0,
            max_revisions=2,
            record_interaction=False,
        ),
    )
    final = pipeline.respond("你昨天不是说不会再管我了吗？", now=NOW, session_id="sess_0881")
    kinds = [event["stage"] for event in observer.events]
    assert "ingest" in kinds
    assert "interpret" in kinds
    assert kinds.count("generate") >= 2
    assert kinds.count("validate") >= 2
    assert kinds[-1] == "final"
    assert final.path == "deep"
    assert final.text == REVISED_IN_CHARACTER_REPLY
    interpret = next(event for event in observer.events if event["stage"] == "interpret")
    assert interpret["data"]["constraint"] == "avoid_explicit_psychological_explanation"
    for event in observer.events:
        _jsonable(event["data"])


@pytest.fixture
def api_client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from gui.server import create_app

    with TestClient(create_app()) as client:
        yield client


def _drain_run(ws, run_id: str) -> list[dict]:
    events: list[dict] = []
    for _ in range(50):
        message = ws.receive_json()
        if message.get("type") == "ping":
            continue
        if message.get("run_id") not in {run_id, None}:
            continue
        events.append(message)
        if message.get("type") == "state" and message.get("run_id") == run_id:
            break
    return events


def test_api_status_and_index(api_client) -> None:
    home = api_client.get("/")
    assert home.status_code == 200
    assert "NeuralCloud" in home.text
    status = api_client.get("/api/status")
    assert status.status_code == 200
    body = status.json()
    assert body["mode"] == "demo"
    assert body["persona"]["identity"]["name"] == "Vaelith"
    assert body["persona"]["identity"]["locked"] is True
    assert body["counts"]["episodic"] >= 1
    assert "debug-dialog" in home.text
    assert "调试" in home.text
    css = api_client.get("/static/styles.css")
    assert css.status_code == 200
    js = api_client.get("/static/app.js")
    assert js.status_code == 200
    assert "slider-row" in js.text
    assert "重新生成解读" in js.text


def test_api_chat_websocket_fast_path(api_client) -> None:
    with api_client.websocket_connect("/ws") as ws:
        response = api_client.post("/api/chat", json={"session_id": "sess_0881", "text": "早上好。"})
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        assert run_id.startswith("run_")
        events = _drain_run(ws, run_id)
        stages = [event["stage"] for event in events if event.get("type") in {"stage", "final"}]
        assert stages[0] == "ingest"
        assert "parse" in stages
        assert "final" in stages
        assert "interpret" not in stages
        final = next(event for event in events if event["type"] == "final")
        assert final["data"]["path"] == "fast"
        assert any(event["type"] == "state" for event in events)


def test_api_chat_websocket_deep_path(api_client) -> None:
    with api_client.websocket_connect("/ws") as ws:
        response = api_client.post(
            "/api/chat",
            json={"session_id": "sess_0881", "text": "你昨天不是说不会再管我了吗？"},
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        events = _drain_run(ws, run_id)
        stages = [event["stage"] for event in events if event.get("type") in {"stage", "final"}]
        assert "interpret" in stages
        assert stages.count("generate") >= 2
        final = next(event for event in events if event["type"] == "final")
        assert final["data"]["path"] == "deep"
        assert "对不起" not in final["data"]["text"]
        for event in events:
            if "data" in event:
                _jsonable(event["data"])


def test_api_config_and_reset(api_client) -> None:
    api_client.post("/api/chat", json={"session_id": "sess_0881", "text": "早上好。"})
    before = api_client.get("/api/status?session_id=sess_0881").json()
    assert len(before["session"]["turns"]) >= 2

    configured = api_client.post(
        "/api/config",
        json={"mode": "demo", "base_url": "http://127.0.0.1:8080/v1", "model": "qwen"},
    )
    assert configured.status_code == 200
    assert configured.json()["mode"] == "demo"
    assert configured.json()["llm"]["model"] == "qwen"

    reset = api_client.post("/api/reset")
    assert reset.status_code == 200
    after = api_client.get("/api/status?session_id=sess_0881").json()
    assert after["session"]["turns"] == []
    assert after["persona"]["identity"]["name"] == "Vaelith"
    assert after["counts"]["episodic"] >= 1


def test_api_config_live_does_not_call_llm_during_seed(api_client) -> None:
    response = api_client.post(
        "/api/config",
        json={"mode": "live", "base_url": "http://127.0.0.1:9", "model": "qwen"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "live"
    assert body["persona"]["identity"]["name"] == "Vaelith"
    assert body["counts"]["episodic"] >= 1
    assert body["llm"]["reachable"] is False


def test_api_debug_state_and_edits(api_client) -> None:
    state = api_client.get("/api/debug/state")
    assert state.status_code == 200
    body = state.json()
    assert body["persona"]["identity"]["name"] == "Vaelith"
    assert len(body["episodic"]) >= 1
    assert "embedding" not in body["episodic"][0]
    assert body["tuning"]["retrieval_top_k"] >= 1

    rel = api_client.post("/api/debug/relationship", json={"closeness": 0.91, "trust": 0.2})
    assert rel.status_code == 200
    assert rel.json()["relationship"]["closeness"] == pytest.approx(0.91)
    assert rel.json()["relationship"]["trust"] == pytest.approx(0.2)

    persona = api_client.post("/api/debug/persona", json={"core_traits": ["傲慢", "debug"]})
    assert persona.status_code == 200
    assert persona.json()["persona"]["identity"]["core_traits"] == ["傲慢", "debug"]
    assert persona.json()["persona"]["identity"]["locked"] is True

    event_id = body["episodic"][0]["event_id"]
    epi = api_client.post("/api/debug/episodic", json={"event_id": event_id, "importance": 0.42})
    assert epi.status_code == 200
    edited = next(item for item in epi.json()["episodic"] if item["event_id"] == event_id)
    assert edited["importance"] == pytest.approx(0.42)

    missing = api_client.post("/api/debug/episodic", json={"event_id": "evt_nope", "importance": 0.1})
    assert missing.status_code == 404

    regen = api_client.post(
        "/api/debug/episodic",
        json={
            "event_id": event_id,
            "objective_fact": "用户提到上周收养的猫生病了",
            "regenerate_insight": True,
        },
    )
    assert regen.status_code == 200
    regenerated = next(item for item in regen.json()["episodic"] if item["event_id"] == event_id)
    assert regenerated["persona_conditioned_insight"]

    created = api_client.post(
        "/api/debug/episodic",
        json={
            "summary": "用户提到上周收养的猫生病了",
            "regenerate_insight": True,
            "curate": True,
            "importance": 0.66,
            "character_action": "询问细节",
        },
    )
    assert created.status_code == 200
    new_events = [item for item in created.json()["episodic"] if item["event_id"] != event_id]
    assert new_events
    newest = new_events[0]
    assert newest["summary"] == "用户提到上周收养的猫生病了"
    assert newest["persona_conditioned_insight"]
    assert newest["character_action"] == "询问细节"
    assert newest["importance"] == pytest.approx(0.66)
    assert created.json()["candidates"]

    blank = api_client.post("/api/debug/episodic", json={"regenerate_insight": True})
    assert blank.status_code == 400

    psych = api_client.post("/api/debug/psych", json={"short": {"mood": "calm", "ttl_seconds": 1800}})
    assert psych.status_code == 200
    assert psych.json()["psych"]["short"]["mood"] == "calm"
    assert "set_at" in psych.json()["psych"]["short"]

    bad_weights = api_client.post(
        "/api/debug/tuning",
        json={
            "retrieval_weights": {
                "semantic": 1,
                "entity": 1,
                "recency": 1,
                "importance": 1,
                "relationship": 1,
                "persona": 1,
            }
        },
    )
    assert bad_weights.status_code == 400

    tuned = api_client.post(
        "/api/debug/tuning",
        json={
            "retrieval_weights": {
                "semantic": 1,
                "entity": 1,
                "recency": 1,
                "importance": 1,
                "relationship": 1,
                "persona": 1,
            },
            "normalize": True,
            "retrieval_top_k": 3,
        },
    )
    assert tuned.status_code == 200
    assert tuned.json()["tuning"]["weight_sum"] == pytest.approx(1.0)
    assert tuned.json()["tuning"]["retrieval_top_k"] == 3

    snapshot = api_client.get("/api/status").json()
    assert snapshot["relationship"]["closeness"] == pytest.approx(0.91)
    assert snapshot["persona"]["identity"]["core_traits"] == ["傲慢", "debug"]

    reset = api_client.post("/api/reset")
    assert reset.status_code == 200
    after = api_client.get("/api/debug/state").json()
    assert "debug" not in after["persona"]["identity"]["core_traits"]
    assert after["relationship"]["closeness"] != pytest.approx(0.91)
