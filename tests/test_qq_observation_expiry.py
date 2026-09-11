"""Expired sensor windows are exclusions, never newly authorized arrivals."""
import json

import pytest

from harness.qq import accessibility
from harness.qq.telemetry import QQTurnTrace
from tests.test_qq_watcher import setup as setup
from tests.test_qq_watcher_unattended import events, scenario as scenario


@pytest.fixture
def parsed_scenario(scenario, monkeypatch):
    # The real parser has its own scoped-tree tests. This injected sensor uses
    # ordered synthetic lines and supplies the same content-only DTO.
    monkeypatch.setattr(accessibility, "chat_observation_items", lambda tree: tuple(
        dict(direction="incoming", kind="text", text=text, display_name="虚构测试甲", author_key="虚构测试甲")
        for text in tree["lines"]))
    return scenario


def observations(state):
    return [json.loads(line) for line in state.watcher._trace_file.with_suffix(".observation.jsonl").read_text().splitlines()]


def expiry_rows(state):
    return [row for row in observations(state) if row["counters"].get("expired_observations")]


def test_expired_prefix_preserves_fresh_origin_and_excludes_old_visible_text(parsed_scenario):
    s = parsed_scenario
    old = s.append("fictional expired message")
    fresh = s.append("fictional fresh message", after=61)
    s.watcher.cycle()
    assert s.watcher._observer_sequence == fresh.sequence
    assert len(s.calls) == 1
    assert s.calls[0]["trace"].first_seen_mono == fresh.first_seen_mono
    assert s.calls[0]["deadline"].deadline_mono == fresh.first_seen_mono + 60
    records = events(s)
    assert [(e["item"]["text"], e["state"]) for e in records] == [
        ("fictional expired message", "expired"), ("fictional fresh message", "decided_wait")]
    expired = expiry_rows(s)
    assert len(expired) == 1 and expired[0]["observation_sequence"] == old.sequence
    assert expired[0]["event_id"] is None
    assert expired[0]["stages"][-1]["outcome"] == "expired"
    assert not any(name.startswith("turn_") for name in expired[0]["counters"])
    assert not s.desktop.sends


def test_only_expired_is_not_reaudited_or_reactivated_and_later_arrival_runs(parsed_scenario):
    s = parsed_scenario
    old = s.append("fictional expired")
    s.clock.advance(61)
    s.watcher.cycle()
    captures = s.desktop.captures
    assert not s.calls and s.watcher._observer_sequence == old.sequence
    s.watcher.cycle()
    assert s.desktop.captures == captures and len(expiry_rows(s)) == 1
    fresh = s.append("fictional later")
    s.watcher.cycle()
    assert len(s.calls) == 1 and s.calls[0]["trace"].first_seen_mono == fresh.first_seen_mono
    assert events(s)[0]["state"] == "expired"


def test_preparation_failure_commits_only_expired_prefix(parsed_scenario):
    s = parsed_scenario
    old = s.append("fictional expired")
    fresh = s.append("fictional fresh", after=61)
    s.desktop.fail_verification = 1
    with pytest.raises(RuntimeError):
        s.watcher.cycle()
    assert s.watcher._observer_sequence == old.sequence
    assert s.watcher._expired_observation.sequence == old.sequence
    failed = observations(s)[-1]
    assert failed["observation_sequence"] == fresh.sequence
    s.clock.advance(2)
    s.watcher.cycle()
    assert len(expiry_rows(s)) == 1
    assert s.calls[0]["trace"].first_seen_mono == fresh.first_seen_mono
    assert s.watcher.status["preparation"]["observation_sequence"] == fresh.sequence


def test_audit_write_failure_never_retires_cursor_or_touches_gui(parsed_scenario, monkeypatch):
    s = parsed_scenario
    s.append("fictional expired")
    s.clock.advance(61)
    captures = s.desktop.captures
    original = QQTurnTrace.flush

    def failing(self, path, **kwargs):
        if self.snapshot()["counters"].get("expired_observations"):
            assert kwargs == {"durable": True}
            raise OSError("synthetic audit disk failure")
        return original(self, path, **kwargs)

    monkeypatch.setattr(QQTurnTrace, "flush", failing)
    with pytest.raises(OSError):
        s.watcher.cycle()
    assert s.watcher._observer_sequence == 0
    assert s.watcher._expired_observation is None
    assert s.desktop.captures == captures and not s.calls


def test_ambiguous_or_unparseable_exclusion_is_explicit_no_send_baseline(scenario):
    s = scenario  # Real parser rejects this synthetic, unbound tree.
    s.append("fictional expired")
    fresh = s.append("fictional cannot establish continuity", after=61)
    s.watcher.cycle()
    assert not s.calls and not s.desktop.sends
    assert s.watcher._observer_sequence == fresh.sequence
    assert s.watcher.status["last_error_type"] == "expired_window_unresolved"
    failures = [r for r in observations(s) if r["counters"].get("unresolved_observation_baselines")]
    assert failures[0]["stages"][-1]["outcome"] == "error"
    assert failures[0]["observation_sequence"] == fresh.sequence
    s.watcher.cycle()
    assert not s.calls and len(failures) == 1


def test_expired_carry_cannot_poison_fresh_candidate(parsed_scenario):
    s = parsed_scenario
    s.watcher._carry_first_seen = (s.clock.wall(), s.clock.mono())
    fresh = s.append("fictional fresh", after=61)
    s.watcher.cycle()
    assert len(s.calls) == 1
    assert s.calls[0]["trace"].first_seen_mono == fresh.first_seen_mono
    assert s.watcher._carry_first_seen is None
    carry = [r for r in observations(s) if r["counters"].get("expired_carry_origins")]
    assert len(carry) == 1 and carry[0]["stages"][-1]["outcome"] == "expired"


def test_attribution_retry_survives_failed_preparation_without_renewal(parsed_scenario):
    s = parsed_scenario
    arrival = s.append("fictional identity retry")
    s.desktop.fail_attribution = 1
    s.watcher.cycle()
    assert s.watcher._attribution_retry
    s.desktop.fail_verification = 1
    with pytest.raises(RuntimeError):
        s.watcher.cycle()
    assert s.watcher._attribution_retry and not s.calls
    s.clock.advance(2)
    s.watcher.cycle()
    assert len(s.calls) == 1
    assert s.calls[0]["trace"].first_seen_mono == arrival.first_seen_mono


def test_expired_attribution_retry_is_failed_then_new_arrival_remains_fresh(parsed_scenario):
    s = parsed_scenario
    s.append("fictional identity failure")
    s.desktop.fail_attribution = 1
    s.watcher.cycle()
    fresh = s.append("fictional new message", after=61)
    s.watcher.cycle()
    assert events(s)[0]["state"] == "expired"
    assert len(s.calls) == 1 and s.calls[0]["trace"].first_seen_mono == fresh.first_seen_mono
    old_trace = next(iter(s.watcher._traces.values())).snapshot()
    assert old_trace["counters"]["turn_failed"] == 1
    assert "turn_natural_wait" not in old_trace["counters"]


@pytest.mark.parametrize("sequence", [True, -1, 1.5, "1"])
def test_observation_sequence_is_bounded_identity_type(sequence):
    with pytest.raises(ValueError):
        QQTurnTrace(observation_sequence=sequence)


def test_multiple_expired_prefixes_leave_only_fresh_cursor_uncommitted(parsed_scenario):
    s = parsed_scenario
    first = s.append("fictional first")
    second = s.append("fictional second")
    fresh = s.append("fictional fresh", after=60)
    s.desktop.fail_verification = 1
    with pytest.raises(RuntimeError):
        s.watcher.cycle()
    assert [r["observation_sequence"] for r in expiry_rows(s)] == [first.sequence, second.sequence]
    assert s.watcher._observer_sequence == second.sequence
    assert s.watcher._expired_observation.sequence == second.sequence
    s.watcher.cycle()
    assert len(s.calls) == 1 and s.calls[0]["trace"].first_seen_mono == fresh.first_seen_mono
    assert [e["state"] for e in events(s)] == ["expired", "expired", "decided_wait"]


def test_valid_but_unmatched_expired_window_baselines_without_new_work(parsed_scenario):
    s = parsed_scenario
    s.append("fictional expired")
    s.clock.advance(61)
    s.desktop.lines = ["fictional unrelated anchor", "fictional not safely dateable"]
    fresh = s.observer.poll_once()
    s.watcher.cycle()
    assert not s.calls and not s.desktop.sends
    assert s.watcher._observer_sequence == fresh.sequence
    assert s.watcher.status["last_error_type"] == "expired_window_unresolved"
    assert any(r["counters"].get("unresolved_observation_baselines") for r in observations(s))
    later = s.append("fictional subsequent provable arrival")
    s.watcher.cycle()
    assert len(s.calls) == 1 and s.calls[0]["trace"].first_seen_mono == later.first_seen_mono
