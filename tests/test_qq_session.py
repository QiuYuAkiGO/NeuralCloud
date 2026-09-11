from datetime import datetime, timezone
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from harness.qq.memory import QQVerifiedProfileInput, QQVisualMemoryStore
from harness.qq.session import QQSessionSettings, create_qq_session_app


TOKEN = "synthetic-token-for-qq-session-tests-only-1234567890"
NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)


def profile(group="10001", member="20001"):
    return QQVerifiedProfileInput(
        group_id=group, member_id=member, display_name="虚构成员",
        display_name_source="qq_profile", verification="qq_ui_verified",
        capture_id=f"test-profile-{group}-{member}", observed_at=NOW,
        group_evidence=["test-evidence://group"], member_evidence=["test-evidence://profile"],
    )


def observation(person):
    return {
        "group_id": person.group_id, "member_id": person.member_id,
        "profile_record_id": person.record_id, "capture_id": "test-message-1",
        "capture_item_id": "bubble-1", "observed_at": NOW.isoformat(),
        "text": "虚构的测试发言。", "evidence": ["test-evidence://bubble"],
        "author_attribution": "qq_ui_verified", "attribution_evidence": ["test-evidence://binding"],
    }


@pytest.fixture
def setup(tmp_path):
    db = tmp_path / "memory.sqlite3"
    store = QQVisualMemoryStore(db)
    person = store.record_profile(profile()).profile
    token = tmp_path / "session.token"
    token.write_text(TOKEN)
    settings = QQSessionSettings(memory_db=db, token_file=token, allowed_group_ids=["10001"], self_member_id="30001")
    calls = []

    def gate(**kwargs):
        calls.append(kwargs)
        context = QQVisualMemoryStore(kwargs["memory_db"]).group_context(group_id=kwargs["group_id"])
        # The actual gate must see the observation AFTER its durable write.
        count = sum(len(person.observations) for person in context.members)
        return SimpleNamespace(model_dump=lambda **_: {"action": "wait", "reply": None, "observation_count": count})

    return settings, store, person, calls, gate


def client(settings, gate):
    return TestClient(create_qq_session_app(settings, decide=gate), headers={"Authorization": "Bearer " + TOKEN})


def test_all_routes_require_authentication(setup):
    settings, _, person, calls, gate = setup
    c = TestClient(create_qq_session_app(settings, decide=gate))
    assert c.get("/v1/status").status_code == 401
    assert c.get("/v1/groups/10001/memory").status_code == 401
    assert c.post("/v1/observations", json=observation(person)).status_code == 401
    assert c.post("/v1/groups/10001/decisions", json={}).status_code == 401
    assert not calls


def test_new_observation_is_durable_before_gate_and_duplicate_does_not_repeat(setup):
    settings, store, person, calls, gate = setup
    c = client(settings, gate)
    result = c.post("/v1/observations", json=observation(person))
    assert result.status_code == 200
    assert result.json()["decision_status"] == "completed"
    assert result.json()["decision"] == {"action": "wait", "reply": None, "observation_count": 1}
    assert result.json()["automatic_send"] is False
    assert len(calls) == 1 and calls[0]["self_member_id"] == "30001"
    assert len(QQVisualMemoryStore(store.path).reply_context(group_id="10001", member_id="20001").observations) == 1
    assert c.post("/v1/observations", json=observation(person)).json()["decision_status"] == "duplicate"
    assert len(calls) == 1


def test_gate_failure_keeps_memory_and_suppresses_error_body(setup):
    settings, store, person, _, _ = setup

    def fail(**_):
        raise RuntimeError("provider error with confidential source content")

    c = client(settings, fail)
    result = c.post("/v1/observations", json=observation(person))
    assert result.status_code == 200
    assert result.json()["decision_status"] == "failed"
    assert "confidential" not in result.text
    assert store.reply_context(group_id="10001", member_id="20001").observations


def test_nonallowlisted_groups_cannot_read_write_or_decide(setup):
    settings, store, _, calls, gate = setup
    other = store.record_profile(profile(group="10002")).profile
    c = client(settings, gate)
    assert c.get("/v1/groups/10002/memory").status_code == 403
    assert c.post("/v1/profiles", json=profile(group="10002").model_dump(mode="json")).status_code == 403
    assert c.post("/v1/observations", json=observation(other)).status_code == 403
    assert c.post("/v1/groups/10002/decisions", json={}).status_code == 403
    assert not calls


def test_disabled_decision_switch_never_calls_model(setup):
    settings, _, person, calls, gate = setup
    c = client(settings.model_copy(update={"decision_enabled": False}), gate)
    assert c.post("/v1/observations", json=observation(person)).json()["decision_status"] == "disabled"
    assert c.post("/v1/groups/10001/decisions", json={}).status_code == 409
    assert not calls


def test_reopened_service_reads_multiple_people_and_truthful_status(setup):
    settings, store, person, _, gate = setup
    c = client(settings, gate)
    c.post("/v1/observations", json=observation(person)).raise_for_status()
    other = store.record_profile(profile(member="20002")).profile
    incoming = observation(other)
    incoming["capture_item_id"] = "bubble-2"
    c.post("/v1/observations", json=incoming).raise_for_status()
    del c
    c = client(settings, gate)
    result = c.get("/v1/groups/10001/memory").json()
    assert set(result["participants"]) == {"20001", "20002"}
    status = c.get("/v1/status").json()
    assert status["autonomous_decision_enabled"] is True
    assert status["new_verified_observations_trigger_decision"] is True
    assert status["automatic_send"] is False and status["automatic_screen_capture"] is False
    assert status["standard_runtime_started_by_this_service"] is False
    assert status["group_member_counts"] == {"10001": 2}


def test_unbound_observation_fails_before_any_decision(setup):
    settings, _, person, calls, gate = setup
    incoming = observation(person)
    incoming["member_id"] = "20002"
    response = client(settings, gate).post("/v1/observations", json=incoming)
    assert response.status_code == 422 and not calls


def test_invalid_or_relative_state_paths_do_not_start(setup):
    settings, _, _, _, gate = setup
    with pytest.raises(ValueError, match="absolute"):
        create_qq_session_app(settings.model_copy(update={"memory_db": settings.memory_db.relative_to(settings.memory_db.parent)}), decide=gate)
    settings.token_file.write_text("short")
    with pytest.raises(ValueError, match="token"):
        create_qq_session_app(settings, decide=gate)


def test_invalid_search_and_extra_control_fields_are_rejected(setup):
    settings, _, _, calls, gate = setup
    c = client(settings, gate)
    assert c.get("/v1/groups/10001/memory", params={"query": "!!!"}).status_code == 422
    assert c.post("/v1/groups/10001/decisions", json={"automatic_send": True}).status_code == 422
    assert c.post("/v1/groups/10001/decisions", json={"query": " "}).status_code == 422
    assert not calls


def test_status_preserves_configured_path_when_windows_resolves_a_package_alias(setup):
    settings, _, _, _, gate = setup

    class RedirectedPath(type(settings.memory_db)):
        def resolve(self, *args, **kwargs):
            return self.parent / "packaged-app-alias" / self.name

    configured = RedirectedPath(settings.memory_db)
    c = client(settings.model_copy(update={"memory_db": configured}), gate)
    response = c.get("/v1/status")
    assert response.status_code == 200
    assert response.json()["memory_db"] == str(configured)


def test_watcher_status_requires_fresh_scoped_heartbeat(setup):
    settings, _, _, _, gate = setup
    path = settings.memory_db.parent / "watcher.status.json"
    configured = settings.model_copy(update={"watcher_status_file": path})
    c = client(configured, gate)
    assert not c.get("/v1/status").json()["automatic_send"]
    record = {"heartbeat_at": datetime.now(timezone.utc).isoformat(), "group_id": "10001",
              "self_member_id": "30001", "running": True, "automatic_send": True,
              "automatic_screen_capture": True, "state": "watching"}
    path.write_text(json.dumps(record))
    assert c.get("/v1/status").json()["automatic_send"]
    for changed in ({"heartbeat_at": NOW.isoformat()}, {"group_id": "99999"}, {"self_member_id": "99999"}, {"running": False}):
        path.write_text(json.dumps(record | changed))
        assert not c.get("/v1/status").json()["automatic_send"]


def test_watcher_status_exposes_bounded_observer_latency_and_turn_metadata(setup):
    settings, _, _, _, gate = setup
    path = settings.memory_db.parent / "watcher.status.json"
    moment = datetime.now(timezone.utc).isoformat()
    record = {"heartbeat_at": moment, "group_id": "10001", "self_member_id": "30001", "running": True,
              "automatic_send": True, "automatic_screen_capture": True,
              "observer": {"running": True, "polls": 99, "changes": 3, "last_polled_at": moment,
                           "last_change_at": moment, "last_error_type": None, "gap_started_at": None,
                           "gap_seconds": 0, "overflows": 0, "tree": "PRIVATE_SOURCE_TEXT"},
              "latency_ms": {"samples": 5, "p50": 1000, "p95": 5000, "max": 5500, "reply": "PRIVATE_REPLY"},
              "backlog": 1, "current_stage": "attribution", "last_turn_outcome": "sent",
              "last_trace_id": "qq-trace:" + "a" * 32}
    path.write_text(json.dumps(record))
    response = client(settings.model_copy(update={"watcher_status_file": path}), gate).get("/v1/status")
    result = response.json()["watcher"]
    assert result["observer"]["polls"] == 99 and result["observer"]["last_polled_at"] == moment
    assert result["latency_ms"] == {"samples": 5, "p50": 1000, "p95": 5000, "max": 5500}
    assert result["backlog"] == 1 and result["current_stage"] == "attribution"
    assert result["last_trace_id"] == record["last_trace_id"]
    assert "PRIVATE" not in response.text


def test_watcher_measurements_do_not_forward_unbounded_or_non_numeric_values(setup):
    settings, _, _, _, gate = setup
    path = settings.memory_db.parent / "watcher.status.json"
    path.write_text(json.dumps({"heartbeat_at": datetime.now(timezone.utc).isoformat(), "group_id": "10001",
        "self_member_id": "30001", "running": True, "observer": {"running": "true", "polls": True,
        "gap_seconds": float("inf"), "last_error_type": "PRIVATE exception text", "last_polled_at": "private"},
        "latency_ms": {"samples": 0.5, "max": -1}, "backlog": True,
        "current_stage": "PRIVATE prompt", "last_trace_id": "PRIVATE reply"}))
    response = client(settings.model_copy(update={"watcher_status_file": path}), gate).get("/v1/status")
    result = response.json()["watcher"]
    assert result["latency_ms"] == {} and "backlog" not in result
    assert "polls" not in result["observer"] and "current_stage" not in result
    assert "PRIVATE" not in response.text
