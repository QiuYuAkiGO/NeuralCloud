"""Normal QQ workers use the single owner; local cognition is explicit dev."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from harness.qq import draft, watcher as module
from harness.qq.decision import QQReplyDecision
from harness.qq.session import create_qq_session_app
from harness.qq.shared import QQSharedCognitiveClient, SharedQQUnavailable
from harness.runtime.ownership import DevelopmentModeRequired
from tests import test_qq_session as session_fixtures
from tests import test_qq_watcher as watcher_fixtures
from tests.test_qq_session import TOKEN, observation


session_setup = session_fixtures.setup
watcher_setup = watcher_fixtures.setup


def normal(settings, monkeypatch):
    monkeypatch.setenv("NC_CONTROL_MODE", "study_runtime")
    return settings.model_copy(update={"development_mode": False, "shared_control_token_file": settings.body_token_file})


def remote_transport(settings, monkeypatch, *, fail=False):
    original_client = httpx.Client
    calls = []

    def response(request):
        assert request.url.path == "/v1/qq/decisions"
        assert request.headers["Authorization"] == "Bearer " + settings.shared_control_token_file.read_text().strip()
        payload = json.loads(request.content)
        calls.append(payload)
        if fail:
            return httpx.Response(503, json={"detail": "unavailable"})
        trace = payload["trace"]
        value = QQReplyDecision(
            decision_id="qq-decision:shared-test", input_hash="test", created_at=trace["first_seen_at"],
            action="wait", reason="synthetic natural wait", decision_source="model", outcome="natural_wait",
            group_id=payload["group_id"], self_member_id=payload["self_member_id"], participants=[],
            source_record_ids=[], new_observation_record_ids=[], evidence_refs=[], delivery_refs=[],
            model="shared-mock", persona_version="shared", preset_id="shared", trace_id=trace["trace_id"],
        )
        return httpx.Response(200, json={"decision": value.model_dump(mode="json"), "trace": {
            "trace_id": trace["trace_id"], "elapsed_ms": 0, "stages": [], "counters": {"gate_model_calls": 1}}})

    monkeypatch.setattr("harness.qq.shared.httpx.Client", lambda **kwargs: original_client(transport=httpx.MockTransport(response), **kwargs))
    monkeypatch.setattr(module, "decide_qq_reply", lambda **kwargs: pytest.fail("normal mode invoked local cognition"))
    return calls


def test_normal_watcher_routes_actual_turn_to_authenticated_shared_client(watcher_setup, monkeypatch):
    settings, desktop, _, _, receipts = watcher_setup
    settings = normal(settings, monkeypatch)
    calls = remote_transport(settings, monkeypatch)
    watch = module.QQWatcher(settings, desktop=desktop)
    try:
        assert isinstance(watch.decide, QQSharedCognitiveClient)
        assert watch.status["cognitive_owner"] == "shared_runtime"
        watch.cycle()
        watch.cycle()
        assert len(calls) == 1
        assert calls[0]["group_id"] == settings.group_id
        assert calls[0]["self_member_id"] == settings.self_member_id
        assert calls[0]["trace"]["event_id"].startswith("qq-watch-event:")
        assert "memory_db" not in calls[0] and "config_path" not in calls[0]
        assert watch.status["last_decision_action"] == "wait"
        assert desktop.sends == 0 and not receipts
    finally:
        watch._shared_cognition.close()


def test_shared_service_failure_does_not_fall_back_or_become_natural_wait(watcher_setup, monkeypatch):
    settings, desktop, _, _, receipts = watcher_setup
    settings = normal(settings, monkeypatch)
    calls = remote_transport(settings, monkeypatch, fail=True)
    watch = module.QQWatcher(settings, desktop=desktop)
    try:
        watch.cycle()
        with pytest.raises(SharedQQUnavailable):
            watch.cycle()
        assert len(calls) == 1 and not receipts and desktop.sends == 0
        assert watch.status["decisions"] == 0
        assert watch.status["last_decision_action"] is None
    finally:
        watch._shared_cognition.close()


@pytest.mark.parametrize("token_case", ["none", "missing", "empty", "relative"])
def test_normal_missing_shared_credentials_fails_before_run_ledger_creation(watcher_setup, monkeypatch, tmp_path, token_case):
    settings, desktop, *_ = watcher_setup
    settings = normal(settings, monkeypatch)
    token = {"none": None, "missing": tmp_path / "missing", "empty": tmp_path / "empty", "relative": Path("relative")}[token_case]
    if token_case == "empty":
        token.write_text("")
    settings = settings.model_copy(update={"shared_control_token_file": token})
    with pytest.raises((ValueError, OSError)):
        module.QQWatcher(settings, desktop=desktop)
    assert not settings.state_db.exists()


def test_callback_cannot_bypass_normal_mode(watcher_setup, monkeypatch):
    settings, desktop, decide, *_ = watcher_setup
    settings = normal(settings, monkeypatch)
    with pytest.raises(ValueError, match="requires_development"):
        module.QQWatcher(settings, desktop=desktop, decide=decide)
    assert not settings.state_db.exists()


def test_development_setting_without_development_authority_is_denied(watcher_setup, monkeypatch):
    settings, desktop, decide, *_ = watcher_setup
    monkeypatch.setenv("NC_CONTROL_MODE", "study_runtime")
    with pytest.raises(DevelopmentModeRequired):
        module.QQWatcher(settings, desktop=desktop, decide=decide)
    assert not settings.state_db.exists()


def test_default_missing_config_fields_mean_normal_not_legacy_fallback(watcher_setup, monkeypatch):
    settings, desktop, *_ = watcher_setup
    monkeypatch.setenv("NC_CONTROL_MODE", "study_runtime")
    raw = settings.model_dump(mode="json", exclude={"development_mode", "shared_control_token_file", "shared_runtime_url"})
    parsed = module.WatcherSettings.model_validate(raw)
    assert not parsed.development_mode
    assert parsed.shared_runtime_url == "http://127.0.0.1:8770"
    with pytest.raises(ValueError, match="token_required"):
        module.QQWatcher(parsed, desktop=desktop)


def test_normal_session_persists_new_observation_but_never_invokes_gate(session_setup, monkeypatch):
    settings, store, person, calls, _ = session_setup
    monkeypatch.setenv("NC_CONTROL_MODE", "study_runtime")
    settings = settings.model_copy(update={"development_mode": False})
    monkeypatch.setattr("harness.qq.decision.decide_qq_reply", lambda **kwargs: pytest.fail("session created independent cognition"))
    with TestClient(create_qq_session_app(settings), headers={"Authorization": "Bearer " + TOKEN}) as client:
        observed = client.post("/v1/observations", json=observation(person))
        assert observed.status_code == 200
        assert observed.json()["decision_status"] == "shared_runtime_owned"
        assert observed.json()["decision"] is None
        assert not calls and len(store.reply_context(group_id=person.group_id, member_id=person.member_id).observations) == 1
        assert client.post("/v1/groups/10001/decisions", json={}).status_code == 409
        assert client.get("/v1/groups/10001/memory").status_code == 200
        status = client.get("/v1/status").json()
        assert status["cognitive_owner"] == "shared_runtime"
        assert status["configured_decision_enabled"] is True
        assert status["development_mode"] is False
        assert status["new_verified_observations_trigger_decision"] is False
        assert status["autonomous_decision_enabled"] is False  # No active shared watcher proof.


def test_normal_session_rejects_callback_injection(session_setup):
    settings, _, _, _, gate = session_setup
    with pytest.raises(ValueError, match="requires_development"):
        create_qq_session_app(settings.model_copy(update={"development_mode": False}), decide=gate)


def test_development_session_also_requires_development_authority(session_setup, monkeypatch):
    settings, _, _, _, gate = session_setup
    monkeypatch.setenv("NC_CONTROL_MODE", "study_runtime")
    with pytest.raises(DevelopmentModeRequired):
        create_qq_session_app(settings, decide=gate)


def test_embodied_normal_driver_gets_same_runtime_owner_parameters(watcher_setup, monkeypatch, tmp_path):
    settings, desktop, *_ = watcher_setup
    settings = normal(settings, monkeypatch).model_copy(update={"qq_body_enabled": True, "homeostatic_idle_enabled": True,
        "homeostasis_state_dir": tmp_path / "feedback", "homeostasis_profile_path": None})
    remote_transport(settings, monkeypatch)
    calls = []

    class Driver:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def close(self):
            pass

    monkeypatch.setattr("harness.qq.homeostatic.QQHomeostaticDriver", Driver)
    watch = module.QQWatcher(settings, desktop=desktop)
    try:
        assert len(calls) == 1
        assert calls[0]["development_mode"] is False
        assert calls[0]["shared_runtime_url"] == settings.shared_runtime_url
        assert calls[0]["shared_control_token_file"] == settings.shared_control_token_file
        assert calls[0]["run_id"] == watch.ledger.run_id
    finally:
        watch._close_embodied()
        watch._shared_cognition.close()


@pytest.mark.parametrize("flag,mode", [(False, "development_debug"), (True, "study_runtime")])
def test_standalone_draft_requires_both_explicit_flag_and_development_authority(tmp_path, monkeypatch, flag, mode):
    monkeypatch.setenv("NC_CONTROL_MODE", mode)
    monkeypatch.setattr(draft, "prepare_qq_draft", lambda **kwargs: pytest.fail("unauthorized standalone model"))
    output = tmp_path / "draft.json"
    args = ["--memory-db", str(tmp_path / "missing.db"), "--group-id", "100", "--member-id", "200", "--output", str(output)]
    if flag:
        args.append("--development-mode")
    assert draft.main(args) == 2 and not output.exists()


@pytest.mark.parametrize("relative", ["harness/runtime/qq_turns.py", "harness/runtime/qq_homeostasis.py",
                                      "harness/cognitive/shared_memory.py", "harness/memory/shared.py"])
def test_runtime_source_hash_covers_shared_owner_implementation(monkeypatch, relative):
    before = module.runtime_source_hash()
    original = Path.read_bytes

    def read_bytes(path):
        content = original(path)
        return content + b"\n# synthetic build change" if path.as_posix().endswith(relative) else content

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert module.runtime_source_hash() != before
