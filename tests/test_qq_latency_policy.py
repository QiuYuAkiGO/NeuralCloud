import json

import pytest

from gui.persist import (apply_loaded_tuning, load_gui_settings, persist_gui_settings)
from harness.config import Settings
from harness.qq.policy import QQLatencyOptions, effective_configuration
from harness.qq.telemetry import QQTurnTrace, measure_stage, qq_trace_scope


def test_persisted_policy_overrides_environment_and_survives_rebuild(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, qq_web_policy="off", validator_enabled=False)
    path = tmp_path / "settings.json"
    persist_gui_settings(mode="demo", settings=settings, path=path)
    monkeypatch.setenv("NC_QQ_WEB_POLICY", "legacy")
    reloaded = Settings(_env_file=None)
    assert effective_configuration(reloaded)["values"]["qq_web_policy"]["source"] == "environment"
    apply_loaded_tuning(reloaded, load_gui_settings(path))
    assert reloaded.qq_web_policy == "off"
    assert effective_configuration(reloaded)["values"]["qq_web_policy"]["source"] == "persisted"
    assert load_gui_settings(path).validator_enabled is False


def test_old_config_does_not_override_policy_environment(tmp_path, monkeypatch):
    path = tmp_path / "old.json"
    path.write_text('{"validator_enabled":false}', encoding="utf-8")
    monkeypatch.setenv("NC_QQ_WEB_POLICY", "off")
    settings = Settings(_env_file=None)
    apply_loaded_tuning(settings, load_gui_settings(path))
    assert settings.qq_web_policy == "off"


def test_configuration_excludes_secrets_and_freezes_per_turn():
    settings = Settings(_env_file=None, llm_api_key="private-secret",
                        llm_base_url="https://user:secret@example.com/?credential=abc")
    trace = QQTurnTrace()
    first = trace.bind_configuration(settings)
    settings.qq_web_policy = "off"
    assert trace.snapshot()["effective_configuration"] == first
    assert "private-secret" not in json.dumps(first)
    assert "example.com" not in json.dumps(first)
    with pytest.raises(ValueError, match="frozen configuration"):
        trace.bind_configuration(settings)


@pytest.mark.parametrize(("remaining", "gate", "web", "body"), [
    (60, 15, 10, 57), (30, 15, 10, 27), (18, 5, 5, 15),
    (15, 2, 2, 12), (14, 1, 0, 11), (3, 0, 0, 0), (0, 0, 0, 0),
])
def test_budgets_share_original_deadline(remaining, gate, web, body):
    policy = QQLatencyOptions()
    assert [policy.stage_budget(stage, remaining) for stage in ("gate", "web", "body")] == [gate, web, body]


def test_measure_stage_without_active_trace_is_noop():
    with measure_stage("input.projection"):
        pass
    trace = QQTurnTrace()
    with qq_trace_scope(trace), measure_stage("input.projection"):
        pass
    assert trace.snapshot()["stages"][0]["name"] == "input.projection"


def test_copied_settings_do_not_mutate_prior_provenance():
    original = Settings(_env_file=None)
    copy = original.model_copy(update={"qq_web_policy": "off"})
    copy.mark_configuration_source(["qq_web_policy"], "runtime")
    assert effective_configuration(original)["values"]["qq_web_policy"]["source"] == "default"
    assert effective_configuration(copy)["values"]["qq_web_policy"]["source"] == "runtime"


def test_conversation_rollout_uses_exact_account_scope_and_freezes_policy():
    candidate = QQLatencyOptions(qq_web_policy="after_gate", qq_stage_budgets_enabled=True,
        cognitive_incremental_persistence_enabled=True, qq_latency_conversations=["29999/10001"])
    selected = candidate.for_conversation(group_id="10001", self_member_id="29999")
    excluded = candidate.for_conversation(group_id="10001", self_member_id="38888")
    assert selected.qq_web_policy == "after_gate" and selected.qq_context_projection == "compact"
    assert excluded.qq_web_policy == "legacy" and excluded.qq_body_pipeline == "legacy"
    assert excluded.qq_context_projection == "legacy" and not excluded.qq_stage_budgets_enabled
    assert excluded.cognitive_incremental_persistence_enabled
    assert selected.qq_history_selection == excluded.qq_history_selection == "bounded"
    changed = candidate.model_copy(update={"qq_latency_conversations": []})
    assert changed.for_conversation(group_id="10001", self_member_id="29999").qq_web_policy == "legacy"
    assert selected.qq_web_policy == "after_gate"
    assert selected.version != excluded.version


@pytest.mark.parametrize("history_mode", ["legacy", "shadow", "bounded"])
@pytest.mark.parametrize("group_id", ["712342021", "private:29999:2285027817"])
def test_history_selection_is_independent_of_other_conversation_rollouts(history_mode, group_id):
    policy = QQLatencyOptions(qq_history_selection=history_mode, qq_latency_conversations=[])
    effective = policy.for_conversation(group_id=group_id, self_member_id="29999")
    assert effective.qq_history_selection == history_mode
    assert effective.qq_context_projection == effective.qq_body_pipeline == effective.qq_web_policy == "legacy"


def test_old_persisted_policy_without_history_selection_uses_bounded(tmp_path, monkeypatch):
    monkeypatch.delenv("NC_QQ_HISTORY_SELECTION", raising=False)
    path = tmp_path / "old-policy.json"
    path.write_text(json.dumps({"qq_latency": {"qq_latency_conversations": []}}), encoding="utf-8")
    settings = Settings(_env_file=None)
    apply_loaded_tuning(settings, load_gui_settings(path))
    effective = QQLatencyOptions.from_settings(settings).for_conversation(
        group_id="712342021", self_member_id="29999")
    assert effective.qq_history_selection == "bounded"


@pytest.mark.parametrize("scopes", [["29999/10001", "29999/10001"], ["10001"], ["29999/private:38888:20001"]])
def test_conversation_rollout_rejects_ambiguous_or_wrong_account_scopes(scopes):
    with pytest.raises(ValueError):
        QQLatencyOptions(qq_latency_conversations=scopes)


def test_conversation_rollout_round_trips_in_persisted_settings(tmp_path):
    settings = Settings(_env_file=None, qq_web_policy="after_gate",
        qq_latency_conversations=["29999/10001", "29999/private:29999:20001"])
    path = tmp_path / "rollout.json"
    persist_gui_settings(mode="demo", settings=settings, path=path)
    loaded = Settings(_env_file=None)
    apply_loaded_tuning(loaded, load_gui_settings(path))
    assert QQLatencyOptions.from_settings(loaded) == QQLatencyOptions.from_settings(settings)


def test_gate_timeout_cannot_be_rolled_out_without_compact_input():
    with pytest.raises(ValueError, match="compact input"):
        QQLatencyOptions(qq_context_projection="legacy", qq_stage_budgets_enabled=True)


def test_derived_rollout_configuration_survives_remote_trace_validation():
    from harness.qq.policy import settings_with_policy
    from harness.qq.shared import _import_remote_trace
    settings = Settings(_env_file=None, qq_web_policy="after_gate", qq_latency_conversations=[])
    policy = QQLatencyOptions.from_settings(settings).for_conversation(group_id="10001", self_member_id="29999")
    trace = QQTurnTrace(event_id="qq-watch-event:" + "a" * 32)
    remote = QQTurnTrace(event_id=trace.event_id)
    remote.trace_id = trace.trace_id
    remote.bind_configuration(settings_with_policy(settings, policy))
    _import_remote_trace(trace, remote.snapshot(), started=trace.first_seen_mono, ended=trace.clock() + 1)
    snapshot = trace.snapshot()["effective_configuration"]
    assert snapshot["values"]["qq_latency_conversations"]["value"] == []
    assert snapshot["values"]["qq_web_policy"] == {"value": "legacy", "source": "frozen_request"}
