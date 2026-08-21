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
    generate = next(event for event in observer.events if event["stage"] == "generate")
    usage = generate["data"]["usage"]
    assert usage["calls"] == 1
    assert usage["total_tokens"] > 0
    assert usage["prompt_tokens"] > 0
    assert usage["revision_calls"] == 0
    assert generate["meta"]["total_tokens"] == usage["total_tokens"]
    assert final.usage.total_tokens == usage["total_tokens"]
    assert final.usage.calls == 1
    assert final.latency_ms >= 0.0
    final_event = next(event for event in observer.events if event["stage"] == "final")
    assert final_event["data"]["latency_ms"] == pytest.approx(final.latency_ms)
    assert final_event["meta"]["total_latency_ms"] == pytest.approx(final.latency_ms)


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
    generates = [event for event in observer.events if event["stage"] == "generate"]
    assert len(generates) >= 2
    per_call = [event["data"]["usage"]["total_tokens"] for event in generates]
    assert all(n > 0 for n in per_call)
    assert generates[0]["data"]["usage"]["revision_calls"] == 0
    assert generates[1]["data"]["usage"]["revision_calls"] == 1
    assert final.usage.calls == len(generates)
    assert final.usage.total_tokens == sum(per_call)
    assert final.usage.revision_calls >= 1
    assert final.usage.revision_tokens == sum(
        event["data"]["usage"]["revision_tokens"] for event in generates
    )
    final_event = next(event for event in observer.events if event["stage"] == "final")
    assert final_event["data"]["usage"]["total_tokens"] == final.usage.total_tokens
    for event in observer.events:
        _jsonable(event["data"])


@pytest.fixture
def api_client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from gui.server import create_app

    with TestClient(create_app(config_path=tmp_path / "gui_settings.json")) as client:
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
    assert body["speakers"]["user_name"] == "用户"
    assert body["speakers"]["character_name"] == "Vaelith"
    assert body["counts"]["episodic"] >= 1
    assert body["llm"]["aux"]["active"] is False
    assert body["llm"]["embedding"]["backend"] == "mock"
    assert 'id="embed-badge"' in home.text
    assert "debug-dialog" in home.text
    assert "调试" in home.text
    assert 'id="user-name"' in home.text
    assert 'id="btn-presets"' in home.text
    assert 'id="chat-tabs"' in home.text
    assert body["presets"]["active_id"] == "vaelith"
    css = api_client.get("/static/styles.css")
    assert css.status_code == 200
    js = api_client.get("/static/app.js")
    assert js.status_code == 200
    assert "slider-row" in js.text
    assert "重新生成解读" in js.text
    assert "saveSpeakers" in js.text
    assert "presetAction" in js.text
    assert "chat-tabs" in js.text
    assert "Validator 打回重生成" in js.text
    assert "hideProtocolMarkers" in js.text
    assert "replySegmentsOf" in js.text
    assert "formatLatencyMs" in js.text
    assert "合计耗时" in js.text
    assert 'id="aux-badge"' in home.text
    assert 'id="cfg-aux-url"' in home.text
    assert 'value="ollama"' in home.text
    assert 'id="cfg-model-tabs"' in home.text
    assert "aux_base_url" in js.text
    assert "OLLAMA_BASE_URL" in js.text
    assert "refreshEndpointModels" in js.text


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
        assert final["data"]["usage"]["calls"] >= 1
        assert final["data"]["usage"]["total_tokens"] > 0
        assert final["data"]["latency_ms"] >= 0
        assert final["meta"]["total_latency_ms"] == pytest.approx(final["data"]["latency_ms"])
        generate = next(event for event in events if event.get("stage") == "generate")
        assert generate["data"]["usage"]["total_tokens"] > 0
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
        assert final["data"]["usage"]["calls"] >= 2
        assert final["data"]["usage"]["revision_tokens"] > 0
        assert final["data"]["usage"]["total_tokens"] >= final["data"]["usage"]["revision_tokens"]
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
    assert len(configured.json()["session"]["turns"]) >= 2

    reset = api_client.post("/api/reset")
    assert reset.status_code == 200
    after = api_client.get("/api/status?session_id=sess_0881").json()
    assert after["session"]["turns"] == []
    assert after["persona"]["identity"]["name"] == "Vaelith"
    assert after["counts"]["episodic"] >= 1


def test_api_speakers_rename_and_chat(api_client) -> None:
    patched = api_client.post("/api/speakers", json={"user_name": "阿宁", "character_name": "薇莉丝"})
    assert patched.status_code == 200
    body = patched.json()
    assert body["speakers"]["user_name"] == "阿宁"
    assert body["speakers"]["character_name"] == "薇莉丝"
    assert body["persona"]["identity"]["name"] == "薇莉丝"

    api_client.post("/api/chat", json={"session_id": "sess_0881", "text": "早上好。"})
    turns = api_client.get("/api/status?session_id=sess_0881").json()["session"]["turns"]
    assert turns[0]["role"] == "user"
    assert turns[0]["speaker"] == "阿宁"
    character_turns = [turn for turn in turns if turn["role"] == "character"]
    assert character_turns
    assert character_turns[0]["speaker"] == "薇莉丝"

    reset = api_client.post("/api/reset")
    assert reset.status_code == 200
    after = reset.json()
    assert after["session"]["turns"] == []
    assert after["speakers"]["user_name"] == "用户"
    assert after["speakers"]["character_name"] == "Vaelith"
    assert after["persona"]["identity"]["name"] == "Vaelith"


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
    assert "aux" in body["llm"]
    assert set(body["llm"]["aux"]) >= {"base_url", "model", "reachable", "preloaded", "active"}
    assert set(body["llm"]["embedding"]) >= {"backend", "reachable", "base_url", "model"}


def test_api_config_live_falls_back_to_mock_embedding(api_client, monkeypatch) -> None:
    monkeypatch.setattr("harness.llm.client.probe_endpoint", lambda *a, **k: False)
    response = api_client.post(
        "/api/config",
        json={"mode": "live", "base_url": "http://127.0.0.1:9", "model": "qwen"},
    )
    assert response.status_code == 200
    embedding = response.json()["llm"]["embedding"]
    assert embedding["backend"] == "mock"
    assert embedding["reachable"] is False


def test_api_config_live_injects_guarded_embedding(api_client, monkeypatch) -> None:
    from harness.llm.embeddings import GuardedEmbedding

    class FakeEmbed:
        dim = 1024

        def __init__(self, settings=None) -> None:
            self.settings = settings

        def embed(self, texts):
            return [[0.01] * 1024 for _ in texts]

        def embed_one(self, text):
            return [0.01] * 1024

        def close(self) -> None:
            return None

    monkeypatch.setattr("harness.llm.client.probe_endpoint", lambda *a, **k: True)
    monkeypatch.setattr("harness.llm.embeddings.OpenAICompatEmbedding", FakeEmbed)
    response = api_client.post(
        "/api/config",
        json={"mode": "live", "base_url": "http://127.0.0.1:9", "model": "qwen"},
    )
    assert response.status_code == 200
    embedding = response.json()["llm"]["embedding"]
    assert embedding["backend"] == "openai"
    assert embedding["reachable"] is True
    service = api_client.app.state.service
    assert isinstance(service.memory.embeddings, GuardedEmbedding)
    assert isinstance(service.memory.embeddings.inner, FakeEmbed)


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


def test_gui_settings_file_roundtrip(tmp_path) -> None:
    from gui.persist import GuiPersistedSettings, load_gui_settings, save_gui_settings

    path = tmp_path / "nested" / "gui_settings.json"
    save_gui_settings(
        GuiPersistedSettings(
            mode="live",
            base_url="http://127.0.0.1:9999/v1",
            model="persist-me",
            api_key="sk-persist",
            aux_base_url="http://127.0.0.1:11434/v1",
            aux_model="qwen3:4b-instruct",
            aux_api_key="sk-aux",
        ),
        path,
    )
    loaded = load_gui_settings(path)
    assert loaded.mode == "live"
    assert loaded.base_url == "http://127.0.0.1:9999/v1"
    assert loaded.model == "persist-me"
    assert loaded.api_key == "sk-persist"
    assert loaded.aux_base_url == "http://127.0.0.1:11434/v1"
    assert loaded.aux_model == "qwen3:4b-instruct"
    assert loaded.aux_api_key == "sk-aux"
    assert loaded.user_name == "用户"
    assert loaded.character_name == ""
    assert load_gui_settings(tmp_path / "missing.json").mode == "demo"


def test_gui_settings_corrupt_file_falls_back(tmp_path) -> None:
    from gui.persist import load_gui_settings

    path = tmp_path / "gui_settings.json"
    path.write_text("{not-json", encoding="utf-8")
    loaded = load_gui_settings(path)
    assert loaded.mode == "demo"
    assert loaded.api_key is None


def test_api_config_persists_across_restart(tmp_path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from gui.server import create_app

    path = tmp_path / "gui_settings.json"
    with TestClient(create_app(config_path=path)) as client:
        response = client.post(
            "/api/config",
            json={
                "mode": "live",
                "base_url": "http://127.0.0.1:9/v1",
                "model": "persist-me",
                "api_key": "sk-persist",
            },
        )
        assert response.status_code == 200
        assert path.is_file()
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["mode"] == "live"
        assert raw["api_key"] == "sk-persist"

    with TestClient(create_app(config_path=path)) as client:
        body = client.get("/api/status").json()
        assert body["mode"] == "live"
        assert body["llm"]["base_url"] == "http://127.0.0.1:9/v1"
        assert body["llm"]["model"] == "persist-me"
        assert body["llm"]["api_key_configured"] is True
        assert client.post("/api/reset").json()["mode"] == "live"


def test_api_config_aux_persists_across_restart(tmp_path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from gui.server import create_app

    path = tmp_path / "gui_settings.json"
    with TestClient(create_app(config_path=path)) as client:
        response = client.post(
            "/api/config",
            json={
                "mode": "live",
                "base_url": "http://127.0.0.1:9/v1",
                "model": "persist-me",
                "api_key": "sk-persist",
                "aux_base_url": "http://127.0.0.1:9/aux/v1",
                "aux_model": "qwen3:4b-instruct",
                "aux_api_key": "sk-aux",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["llm"]["aux"]["active"] is True
        assert body["llm"]["aux"]["model"] == "qwen3:4b-instruct"
        assert body["llm"]["aux"]["base_url"] == "http://127.0.0.1:9/aux/v1"
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["aux_base_url"] == "http://127.0.0.1:9/aux/v1"
        assert raw["aux_model"] == "qwen3:4b-instruct"
        assert raw["aux_api_key"] == "sk-aux"

    with TestClient(create_app(config_path=path)) as client:
        body = client.get("/api/status").json()
        assert body["mode"] == "live"
        assert body["llm"]["aux"]["active"] is True
        assert body["llm"]["aux"]["model"] == "qwen3:4b-instruct"
        assert body["llm"]["aux"]["base_url"] == "http://127.0.0.1:9/aux/v1"


def test_api_tuning_persists_across_restart(tmp_path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from gui.server import create_app

    path = tmp_path / "gui_settings.json"
    with TestClient(create_app(config_path=path)) as client:
        response = client.post(
            "/api/debug/tuning",
            json={"retrieval_top_k": 3, "consolidation_threshold": 0.42},
        )
        assert response.status_code == 200
        assert response.json()["tuning"]["retrieval_top_k"] == 3

    with TestClient(create_app(config_path=path)) as client:
        body = client.get("/api/debug/state").json()
        assert body["tuning"]["retrieval_top_k"] == 3
        assert body["tuning"]["consolidation_threshold"] == pytest.approx(0.42)


def test_api_speakers_persist_across_restart(tmp_path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from gui.server import create_app

    path = tmp_path / "gui_settings.json"
    with TestClient(create_app(config_path=path)) as client:
        response = client.post("/api/speakers", json={"user_name": "阿宁", "character_name": "薇莉丝"})
        assert response.status_code == 200
        client.post(
            "/api/debug/tuning",
            json={"retrieval_top_k": 3},
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["user_name"] == "阿宁"
        assert raw["character_name"] == "薇莉丝"
        assert raw["tuning"]["retrieval_top_k"] == 3

    with TestClient(create_app(config_path=path)) as client:
        body = client.get("/api/status").json()
        assert body["speakers"]["user_name"] == "阿宁"
        assert body["speakers"]["character_name"] == "薇莉丝"
        assert body["persona"]["identity"]["name"] == "薇莉丝"
        assert client.get("/api/debug/state").json()["tuning"]["retrieval_top_k"] == 3


def test_api_models_lists_installed_names(api_client, monkeypatch) -> None:
    monkeypatch.setattr(
        "gui.server.list_endpoint_models",
        lambda base_url, api_key, timeout=2.0: ["neuralcloud-main", "qwen3:4b-instruct"],
    )
    response = api_client.get("/api/models", params={"base_url": "http://127.0.0.1:11434/v1"})
    assert response.status_code == 200
    body = response.json()
    assert body["base_url"] == "http://127.0.0.1:11434/v1"
    assert body["models"] == ["neuralcloud-main", "qwen3:4b-instruct"]


def test_api_config_ollama_defaults_base_url(api_client) -> None:
    from gui.persist import DEFAULT_OLLAMA_BASE_URL

    response = api_client.post("/api/config", json={"mode": "ollama"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "ollama"
    assert body["llm"]["mode"] == "ollama"
    assert body["llm"]["base_url"] == DEFAULT_OLLAMA_BASE_URL
