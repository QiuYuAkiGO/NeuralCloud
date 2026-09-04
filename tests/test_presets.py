from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gui.presets import (
    DEFAULT_PRESET_ID,
    PresetLibrary,
    hydrate_memory,
    load_builtin,
    snapshot_memory,
)
from harness.core.inference import SessionTurn
from harness.core.models import RetrievalQuery
from harness.memory.service import build_inmemory_service
from harness.pipeline.session import SessionStore

TZ = ZoneInfo("Asia/Shanghai")


def test_vaelith_builtin_loads() -> None:
    preset = load_builtin("vaelith")
    assert preset.preset_id == "vaelith"
    assert preset.persona.identity.name == "Vaelith"
    assert "优雅" in preset.persona.identity.core_traits
    assert len(preset.episodic) == 9
    assert preset.transcript == []
    assert preset.relationships[0].last_major_event == "evt_09014"


def test_misty_digitalbody_is_the_default_builtin() -> None:
    preset = load_builtin(DEFAULT_PRESET_ID)
    assert DEFAULT_PRESET_ID == "misty"
    assert preset.preset_id == "misty"
    assert preset.name == "小雾 · MistyDigitalBody v0.2.0"
    assert preset.persona.identity.name == "小雾"
    assert preset.persona.identity.core_traits == ["温柔", "慢热", "容易害羞", "情绪细腻"]
    assert preset.persona.identity.profile is not None
    assert preset.persona.identity.profile.age == 19
    assert preset.persona.identity.profile.birthday == "5月20日"
    assert preset.persona.identity.profile.height_cm == 157
    assert preset.persona.identity.profile.hobbies == ["听歌", "摄影", "收集蝴蝶结"]
    assert preset.persona.identity.profile.source_refs
    assert preset.relationships == []
    assert preset.episodic == []
    assert preset.semantic == []
    assert preset.transcript == []


def test_snapshot_hydrate_roundtrip() -> None:
    factory = load_builtin("vaelith")
    memory = build_inmemory_service()
    hydrate_memory(memory, factory)
    turns = [
        SessionTurn(role="user", text="早上好。", speaker="用户"),
        SessionTurn(role="character", text="嗯。", speaker="Vaelith"),
    ]
    snap = snapshot_memory(
        memory,
        preset_id="vaelith",
        session_id=factory.session_id,
        transcript=turns,
        user_name="阿宁",
        builtin_id="vaelith",
    )
    restored = build_inmemory_service()
    hydrate_memory(restored, snap)
    persona = restored.get_persona()
    assert persona is not None
    assert persona.identity.name == "Vaelith"
    assert len(restored.stores.episodic.list_all()) == 9
    rel = restored.stores.relationship.get("user")
    assert rel is not None
    assert rel.closeness == pytest.approx(0.68)
    assert snap.user_name == "阿宁"
    assert [turn.text for turn in snap.transcript] == ["早上好。", "嗯。"]


def test_hydrate_backfills_missing_embeddings() -> None:
    factory = load_builtin("vaelith-daily")
    assert all(event.embedding is None for event in factory.episodic)
    assert all(fact.embedding is None for fact in factory.semantic)
    memory = build_inmemory_service()
    hydrate_memory(memory, factory)
    events = memory.stores.episodic.list_all()
    facts = memory.stores.semantic.list_all()
    assert len(events) == len(factory.episodic)
    assert len(facts) == len(factory.semantic)
    assert all(event.embedding is not None for event in events)
    assert all(fact.embedding is not None for fact in facts)


def test_hydrate_preserves_existing_embeddings() -> None:
    factory = load_builtin("vaelith-daily")
    sentinel = [0.01] * 1024
    seeded = factory.episodic[0].model_copy(update={"embedding": sentinel})
    preset = factory.model_copy(update={"episodic": [seeded, *factory.episodic[1:]]})
    memory = build_inmemory_service()
    hydrate_memory(memory, preset)
    kept = {event.event_id: event for event in memory.stores.episodic.list_all()}
    assert kept[seeded.event_id].embedding == sentinel
    assert all(event.embedding is not None for event in kept.values())


def test_hydrated_seeds_give_semantic_signal_for_named_entity() -> None:
    memory = build_inmemory_service()
    hydrate_memory(memory, load_builtin("vaelith"))
    results = memory.query(
        RetrievalQuery(
            text="之前和鸽子少女吵架了吗？",
            now=datetime(2026, 8, 22, 1, 17, tzinfo=TZ),
            session_id="sess_0881",
            entities=["user", "character", "鸽子少女"],
        )
    )
    by_ref = {item.ref: item for item in results}
    assert by_ref["evt_09018"].score_breakdown.semantic > 0
    assert by_ref["evt_09014"].score_breakdown.semantic > 0
    assert results[0].ref in {"evt_09018", "evt_09014"}


def test_hydrate_skips_failed_vectors() -> None:
    class NoneEmbed:
        dim = 1024

        def embed(self, texts):
            return [None] * len(texts)

        def embed_one(self, text):
            return None

    factory = load_builtin("vaelith")
    memory = build_inmemory_service()
    memory.embeddings = NoneEmbed()
    hydrate_memory(memory, factory)
    assert all(event.embedding is None for event in memory.stores.episodic.list_all())


def test_session_store_replace_truncates_window() -> None:
    store = SessionStore(window=2)
    store.replace(
        "sess_x",
        [
            SessionTurn(role="user", text="1"),
            SessionTurn(role="character", text="2"),
            SessionTurn(role="user", text="3"),
        ],
    )
    assert [turn.text for turn in store.recent("sess_x")] == ["2", "3"]


def test_library_write_through_and_restore(tmp_path) -> None:
    library = PresetLibrary(working_dir=tmp_path / "presets")
    loaded = library.load("vaelith")
    identity = loaded.persona.identity.model_copy(update={"core_traits": ["傲慢", "debug"]})
    persona = loaded.persona.model_copy(update={"identity": identity})
    patched = loaded.model_copy(update={"persona": persona})
    library.save(patched)
    again = library.load("vaelith")
    assert again.persona.identity.core_traits == ["傲慢", "debug"]
    restored = library.restore_factory("vaelith")
    assert "debug" not in restored.persona.identity.core_traits
    assert restored.preset_id == "vaelith"
    assert restored.transcript == []


def test_library_create_from_copies_source_state(tmp_path) -> None:
    library = PresetLibrary(working_dir=tmp_path / "presets")
    source = library.load("vaelith")
    rel = source.relationships[0].model_copy(update={"closeness": 0.91, "trust": 0.2})
    identity = source.persona.identity.model_copy(update={"core_traits": ["傲慢", "cloned"]})
    persona = source.persona.model_copy(update={"identity": identity})
    source = source.model_copy(
        update={
            "persona": persona,
            "relationships": [rel],
            "transcript": [SessionTurn(role="user", text="旧聊天")],
        }
    )
    library.save(source)
    created = library.create_from("vaelith")
    factory = load_builtin("vaelith")
    assert created.preset_id != "vaelith"
    assert created.builtin_id == "vaelith"
    assert created.transcript == []
    assert created.persona.identity.core_traits == ["傲慢", "cloned"]
    assert created.relationships[0].closeness == pytest.approx(0.91)
    assert created.relationships[0].trust == pytest.approx(0.2)
    assert created.psych == source.psych
    assert created.world == source.world
    assert len(created.episodic) == len(source.episodic)
    assert created.relationships[0].closeness != pytest.approx(factory.relationships[0].closeness)
    assert not created.is_protected()
    library.delete(created.preset_id)
    with pytest.raises(ValueError, match="builtin"):
        library.delete("vaelith")


@pytest.fixture
def api_client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from gui.server import create_app

    with TestClient(create_app(config_path=tmp_path / "gui_settings.json")) as client:
        yield client


def test_debug_persona_and_chat_write_through_restart(tmp_path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from gui.server import create_app

    path = tmp_path / "gui_settings.json"
    with TestClient(create_app(config_path=path)) as client:
        patched = client.post("/api/debug/persona", json={"core_traits": ["傲慢", "debug"]})
        assert patched.status_code == 200
        chat = client.post("/api/chat", json={"text": "早上好。"})
        assert chat.status_code == 200
        rel = client.post("/api/debug/relationship", json={"closeness": 0.91})
        assert rel.status_code == 200

    working = json.loads((tmp_path / "presets" / f"{DEFAULT_PRESET_ID}.json").read_text(encoding="utf-8"))
    assert working["persona"]["identity"]["core_traits"] == ["傲慢", "debug"]
    assert len(working["transcript"]) >= 2

    with TestClient(create_app(config_path=path)) as client:
        body = client.get("/api/status").json()
        assert body["persona"]["identity"]["core_traits"] == ["傲慢", "debug"]
        assert body["relationship"]["closeness"] == pytest.approx(0.91)
        assert len(body["session"]["turns"]) >= 2
        assert body["presets"]["active_id"] == DEFAULT_PRESET_ID


def test_config_rebuild_keeps_preset(api_client) -> None:
    api_client.post("/api/debug/persona", json={"core_traits": ["傲慢", "debug"]})
    api_client.post("/api/chat", json={"text": "早上好。"})
    configured = api_client.post(
        "/api/config",
        json={"mode": "demo", "base_url": "http://127.0.0.1:8080/v1", "model": "qwen"},
    )
    assert configured.status_code == 200
    body = configured.json()
    assert body["persona"]["identity"]["core_traits"] == ["傲慢", "debug"]
    assert len(body["session"]["turns"]) >= 2


def test_new_preset_copies_source_relationship(api_client) -> None:
    rel = api_client.post("/api/debug/relationship", json={"closeness": 0.91, "trust": 0.2})
    assert rel.status_code == 200
    patched = api_client.post("/api/debug/persona", json={"core_traits": ["傲慢", "cloned"]})
    assert patched.status_code == 200
    created = api_client.post("/api/presets/new", json={"from_id": DEFAULT_PRESET_ID})
    assert created.status_code == 200
    body = created.json()
    assert body["presets"]["active_id"] != DEFAULT_PRESET_ID
    assert body["relationship"]["closeness"] == pytest.approx(0.91)
    assert body["relationship"]["trust"] == pytest.approx(0.2)
    assert body["persona"]["identity"]["core_traits"] == ["傲慢", "cloned"]
    assert body["session"]["turns"] == []


def test_switch_preset_loads_its_transcript(api_client) -> None:
    chat = api_client.post("/api/chat", json={"text": "早上好。"})
    assert chat.status_code == 200
    created = api_client.post("/api/presets/new", json={"from_id": DEFAULT_PRESET_ID})
    assert created.status_code == 200
    body = created.json()
    new_id = body["presets"]["active_id"]
    assert new_id != DEFAULT_PRESET_ID
    assert body["session"]["turns"] == []
    opened = api_client.post("/api/presets/open", json={"preset_id": DEFAULT_PRESET_ID})
    assert opened.status_code == 200
    assert len(opened.json()["session"]["turns"]) >= 2
    applied = api_client.post("/api/presets/apply", json={"preset_id": new_id})
    assert applied.status_code == 200
    assert applied.json()["presets"]["active_id"] == new_id
    assert applied.json()["session"]["turns"] == []
    listed = api_client.get("/api/presets").json()
    ids = [item["preset_id"] for item in listed["items"]]
    assert "vaelith" in ids
    assert DEFAULT_PRESET_ID in ids
    assert new_id in ids
    deleted = api_client.delete(f"/api/presets/{new_id}")
    assert deleted.status_code == 200
    blocked = api_client.delete("/api/presets/vaelith")
    assert blocked.status_code == 400
    blocked_default = api_client.delete(f"/api/presets/{DEFAULT_PRESET_ID}")
    assert blocked_default.status_code == 400
