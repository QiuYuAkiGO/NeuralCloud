"""Identity-cache wiring uses persisted scoped profiles, never fabricated recaptures."""

import json
from types import SimpleNamespace as NS

import pytest

from harness.qq import desktop as desktop_module
from harness.qq.memory import QQVerifiedProfileInput, QQVisualMemoryStore
from harness.qq.watcher import WatcherSettings
from tests.test_qq_watcher import setup as setup  # noqa: F401 - scenario dependency
from tests.test_qq_watcher_unattended import events, scenario as scenario, traces


def enable(state):
    state.watcher.settings = state.watcher.settings.model_copy(update={"identity_cache_enabled": True})


def test_hot_identity_reuses_old_profile_with_current_observation_evidence(scenario, monkeypatch):
    state = scenario
    enable(state)
    original = state.watcher.store.reply_context(group_id="10001", member_id="20001").profiles[0]
    saved = original.model_dump_json()
    base = state.desktop.attribute

    def attribute(message, frame):
        result = base(message, frame)
        result.profile_record_id = original.record_id
        result.attribution_method = "qq_verified_identity_cache_v1"
        result.capture_id = original.capture_id
        result.observed_at = original.observed_at
        result.profile_evidence_refs = original.member_evidence
        result.evidence_refs = [frame.evidence_ref, "test-evidence://current-cache-binding"]
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("A hot identity must not rewrite or rebind the original profile")

    state.desktop.attribute = attribute
    state.desktop.remember_verified_identity = forbidden
    monkeypatch.setattr(state.watcher.store, "record_profile", forbidden)
    state.append("synthetic new message from a known member")
    state.watcher.cycle()
    context = state.watcher.store.reply_context(group_id="10001", member_id="20001")
    assert len(context.profiles) == len(context.observations) == 1
    assert context.profiles[0].model_dump_json() == saved
    observed = context.observations[0]
    assert observed.profile_record_id == original.record_id
    assert observed.capture_id == state.desktop.current.capture_id != original.capture_id
    assert observed.observed_at == state.desktop.current.captured_at > original.observed_at
    assert observed.attribution_evidence == [
        state.desktop.current.evidence_ref,
        "test-evidence://current-cache-binding",
    ]
    assert observed.extraction_method == "qq_verified_identity_cache_v1"
    assert observed.message_id is None
    assert traces(state)[-1]["counters"]["cold_path"] == 0
    assert len(state.calls) == 1 and not state.receipts


@pytest.mark.parametrize("invalid_scope", ["other_group", "other_member", "missing_profile"])
def test_hot_identity_cannot_reference_an_unbound_persisted_profile(scenario, monkeypatch, invalid_scope):
    state = scenario
    enable(state)
    profile_id = "qq-profile:missing"
    if invalid_scope != "missing_profile":
        original = state.watcher.store.reply_context(group_id="10001", member_id="20001").profiles[0]
        profile_id = state.watcher.store.record_profile(
            QQVerifiedProfileInput(
                **{key: value for key, value in original.model_dump().items() if key not in {"record_id", "stored_at"}}
            ).model_copy(
                update={
                    "group_id": "10002" if invalid_scope == "other_group" else "10001",
                    "member_id": "20002" if invalid_scope == "other_member" else "20001",
                }
            )
        ).profile.record_id
    base = state.desktop.attribute

    def attribute(message, frame):
        result = base(message, frame)
        result.profile_record_id = profile_id
        result.attribution_method = "qq_verified_identity_cache_v1"
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("An invalid hot profile must not trigger a fabricated profile write")

    state.desktop.attribute = attribute
    state.desktop.remember_verified_identity = forbidden
    monkeypatch.setattr(state.watcher.store, "record_profile", forbidden)
    state.append("synthetic message with an invalid cached binding")
    state.watcher.cycle()
    assert not state.watcher.store.reply_context(group_id="10001", member_id="20001").observations
    assert not state.calls and not state.receipts and state.desktop.clicks == 0
    assert events(state)[0]["source_record_id"] is None
    assert state.watcher.status["last_error_type"] == "ValueError"


@pytest.mark.parametrize("member_id,expected_cold", [("20001", 0), ("20002", 1)])
def test_card_fallback_persists_before_binding_cache_and_keeps_memory_based_cold_path(
    scenario, member_id, expected_cold
):
    state = scenario
    enable(state)
    base = state.desktop.attribute
    remembered = []

    def attribute(message, frame):
        result = base(message, frame)
        result.member_id = member_id
        result.profile_record_id = None
        result.attribution_method = "qq_visual_avatar_profile_v1"
        return result

    def remember(message, frame, attributed, profile_record_id):
        persisted = QQVisualMemoryStore(state.watcher.session.memory_db).reply_context(
            group_id="10001", member_id=member_id
        )
        assert profile_record_id in {profile.record_id for profile in persisted.profiles}
        assert persisted.observations[0].profile_record_id == profile_record_id
        assert persisted.observations[0].capture_id == frame.capture_id
        assert persisted.observations[0].text == message.text
        remembered.append((attributed, profile_record_id))

    state.desktop.attribute, state.desktop.remember_verified_identity = attribute, remember
    state.append("synthetic card-verified message")
    state.watcher.cycle()
    assert len(remembered) == 1
    context = state.watcher.store.reply_context(group_id="10001", member_id=member_id)
    assert context.observations[0].profile_record_id == remembered[0][1]
    assert context.observations[0].extraction_method == "qq_visual_avatar_profile_v1"
    assert traces(state)[-1]["counters"]["cold_path"] == expected_cold
    assert len(state.calls) == 1 and not state.receipts


@pytest.mark.parametrize("enabled,expected_checks", [(False, 1), (True, 2)])
def test_self_identity_preparation_refreshes_aliases_each_cycle_only_when_enabled(scenario, enabled, expected_checks):
    state = scenario
    state.watcher.settings = state.watcher.settings.model_copy(update={"identity_cache_enabled": enabled})
    verifications = []

    def verify_self():
        verifications.append(len(verifications) + 1)
        state.desktop.verified_self_aliases = [f"synthetic-verified-self-{len(verifications)}"]

    state.desktop.verify_self = verify_self
    for index in range(2):
        state.append(f"synthetic preparation {index}")
        state.watcher.cycle()
    assert len(verifications) == expected_checks
    assert [call["verified_self_aliases"] for call in state.calls] == [
        ["synthetic-verified-self-1"],
        [f"synthetic-verified-self-{expected_checks}"],
    ]
    assert state.watcher._self_aliases == state.desktop.verified_self_aliases


@pytest.mark.parametrize("enabled", [False, True])
def test_config_defaults_off_and_make_desktop_passes_flag(scenario, monkeypatch, enabled):
    state = scenario
    legacy = json.loads(state.watcher.settings.model_dump_json())
    legacy.pop("identity_cache_enabled")
    assert WatcherSettings.model_validate(legacy).identity_cache_enabled is False
    state.watcher.settings = state.watcher.settings.model_copy(update={"identity_cache_enabled": enabled})
    captured = []
    monkeypatch.setattr(desktop_module, "DesktopConfig", lambda **kwargs: NS(**kwargs))
    monkeypatch.setattr(desktop_module, "DesktopBackend", lambda config, **kwargs: captured.append(config) or NS())
    state.watcher.make_desktop()
    assert captured[0].identity_cache_enabled is enabled
