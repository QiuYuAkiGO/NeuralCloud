from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from harness.cognitive.shared_memory import CognitiveSharedMemory
from harness.core.models import InteractionEvent
from harness.embodiment.qq_contracts import QQReadResult, QQScope, QQVisibleMessage


@pytest.fixture
def views(tmp_path, monkeypatch):
    monkeypatch.delenv("NC_QQ_WATCHER_CONFIG", raising=False)
    config_path = tmp_path / "watcher.json"
    config = {"group_id": "10001", "self_member_id": "20001", "automatic_send": True,
        "expected_instance_id": "body-test", "expected_revision": 2, "window_id": "0x123",
        "state_db": str(tmp_path / "watcher.sqlite3"),
        "group_roster": [{"group_id": "10001", "label": "Home", "send": True},
                         {"group_id": "10002", "label": "Other group"}]}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    database = Path(config["state_db"]).with_suffix(".roster.sqlite3")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE qq_group_reads(group_id TEXT PRIMARY KEY, result_json TEXT, "
                           "read_count INTEGER, last_attempt_at TEXT, last_error TEXT)")
        connection.commit()
    digest = "b" * 64
    scope = QQScope(body_instance_id="body-test", environment_revision=2, window_id="0x123",
                    group_id="10001", self_member_id="20001")

    def write(group_id, texts, *, changes=None):
        result = QQReadResult(scope=scope.model_copy(update={"group_id": group_id}),
            capture_id=f"capture-{group_id}", observed_at=datetime.now(timezone.utc) - timedelta(seconds=20),
            context_signature="a" * 64, evidence_refs=(f"evidence://sha256/{digest}",),
            messages=tuple(QQVisibleMessage(capture_item_id=f"item-{index}", direction="incoming",
                kind="text", text=text, display_name="Unverified display name") for index, text in enumerate(texts)))
        if changes:
            result = result.model_copy(update=changes)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("INSERT OR REPLACE INTO qq_group_reads VALUES(?,?,1,?,NULL)",
                               (group_id, result.model_dump_json(), result.observed_at.isoformat()))
            connection.commit()
        return result

    shared = CognitiveSharedMemory(SimpleNamespace(), tmp_path / "shared.sqlite3",
        qq_settings=SimpleNamespace(allowed_group_ids=["10001", "10002"], self_member_id="20001",
                                    watcher_status_file=tmp_path / "watcher.status.json"))
    monkeypatch.setattr(shared, "sync_qq", lambda: None)
    return shared, write, config, config_path, database


def local_context(shared):
    return shared.local_context(InteractionEvent(raw_text="Recent group topics?",
        timestamp=datetime.now(timezone.utc), speaker="user", session_id="local-main"))


def test_private_local_context_preserves_separate_group_sources(views):
    shared, write, _, _, database = views
    write("10001", ["HOME_SOURCE"])
    write("10002", ["OTHER_SOURCE"])
    write("99999", ["UNAUTHORIZED_SOURCE"])
    before = database.read_bytes()
    segments = local_context(shared)
    assert len(segments) == 1 and segments[0].kind == "user"
    payload = json.loads(segments[0].text)["qq_group_views"]
    assert not payload["current_live_observation"]
    assert payload["author_identity"] == "可见消息显示名未核验为数字成员身份"
    records = {entry["group_id"]: entry for entry in payload["snapshots"]}
    assert records["10001"]["messages"][0]["text"] == "HOME_SOURCE"
    assert records["10002"]["messages"][0]["text"] == "OTHER_SOURCE"
    assert "UNAUTHORIZED_SOURCE" not in segments[0].text
    assert all(entry["observed_at"] and entry["evidence_refs"] for entry in records.values())
    assert all(source.startswith("qq-group-view:") for source in segments[0].source_ids)
    assert database.read_bytes() == before


@pytest.mark.parametrize("field,value", [
    ("body_instance_id", "different-body"), ("environment_revision", 3),
    ("window_id", "0x999"), ("self_member_id", "99999"), ("group_id", "10002"),
])
def test_mismatched_snapshot_scope_is_never_imported(views, field, value):
    shared, write, _, _, _ = views
    original = write("10001", ["MISMATCHED_SOURCE"])
    write("10001", ["MISMATCHED_SOURCE"], changes={"scope": original.scope.model_copy(update={field: value})})
    assert local_context(shared) == []


@pytest.mark.parametrize("damage", ["malformed_digest", "empty_refs", "unbound_ref", "json"])
def test_damaged_contract_or_unbound_evidence_reference_does_not_become_context(views, damage):
    shared, write, _, _, database = views
    write("10001", ["NOT_VERIFIED"])
    if damage == "malformed_digest":
        write("10001", ["NOT_VERIFIED"], changes={"evidence_refs": ("evidence://sha256/corrupt",)})
    elif damage == "empty_refs":
        write("10001", ["NOT_VERIFIED"], changes={"evidence_refs": ()})
    elif damage == "unbound_ref":
        write("10001", ["NOT_VERIFIED"], changes={"evidence_refs": ("arbitrary evidence",)})
    else:
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE qq_group_reads SET result_json=?", ('{"messages":"broken"}',))
            connection.commit()
    assert local_context(shared) == []


def test_budget_selects_whole_messages_without_truncating_json_or_text(views):
    shared, write, _, _, _ = views
    original = [f"{index}:" + "完整中文消息" * 180 for index in range(8)]
    write("10001", original)
    segment, = local_context(shared)
    assert len(segment.text.encode("utf-8")) <= 8192
    entry, = json.loads(segment.text)["qq_group_views"]["snapshots"]
    selected = [message["text"] for message in entry["messages"]]
    assert selected and selected == original[-len(selected):]
    assert entry["omitted_message_count"] == len(original) - len(selected)


def test_observation_disabled_and_session_scope_changes_hide_retained_views(views):
    shared, write, config, path, _ = views
    write("10002", ["OTHER_SOURCE"])
    config["group_roster"][1]["observe"] = False
    path.write_text(json.dumps(config), encoding="utf-8")
    assert local_context(shared) == []
    config["group_roster"][1]["observe"] = True
    path.write_text(json.dumps(config), encoding="utf-8")
    shared.qq_settings.allowed_group_ids = ["10001"]
    assert local_context(shared) == []


def test_legacy_config_does_not_open_snapshot_database(views, monkeypatch):
    shared, _, config, path, _ = views
    config.pop("group_roster")
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr("harness.cognitive.shared_memory.sqlite3.connect",
                        lambda *args, **kwargs: pytest.fail("legacy config must not open roster database"))
    assert shared._group_view_context() == []


def test_environment_config_is_authoritative_and_missing_files_are_compatible(views, monkeypatch):
    shared, write, _, path, database = views
    write("10001", ["HOME_SOURCE"])
    shared.qq_settings.watcher_status_file = None
    monkeypatch.setenv("NC_QQ_WATCHER_CONFIG", str(path))
    assert local_context(shared)
    monkeypatch.setenv("NC_QQ_WATCHER_CONFIG", str(path.with_name("missing.json")))
    assert local_context(shared) == []
    monkeypatch.setenv("NC_QQ_WATCHER_CONFIG", str(path))
    database.unlink()
    assert local_context(shared) == []


def test_qq_binding_never_reads_group_views(views, monkeypatch):
    from gui.presets import load_builtin

    shared, write, _, _, _ = views
    write("10002", ["OTHER_SOURCE"])
    copyable = SimpleNamespace()
    copyable.model_copy = lambda **kwargs: copyable
    preset = load_builtin("misty")
    shared.host = SimpleNamespace(chat=SimpleNamespace(
        library=SimpleNamespace(load=lambda _: copyable), active_preset_id="test", settings=copyable,
        _active_builtin_id=preset.builtin_id, _active_preset_name=preset.name,
        active_session_id=preset.session_id, user_name=preset.user_name,
        llm=object(), memory=SimpleNamespace(get_persona=lambda: preset.persona)))
    copyable.qq_memory_recall_max_items = 8
    copyable.qq_memory_recall_max_characters = 1200
    monkeypatch.setattr(shared, "sync_local", lambda: None)
    monkeypatch.setattr(shared, "_group_view_context", lambda: pytest.fail("QQ reply cannot read other group views"))
    binding = shared.qq_binding(group_id="10001")
    assert not binding.context["entries"]
