"""QQ feedback queue tests using only isolated local engine/SQLite state."""

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from harness.qq import homeostatic
from harness.homeostasis.models import build_interoceptive_summary


START = datetime(2026, 9, 12, tzinfo=UTC)
PROFILE = Path("presets/homeostasis/qq-social-drive-v1.json")


class Clock:
    def __init__(self):
        self.wall = START
        self.mono = 100.0

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
    clock = Clock()
    options = dict(state_dir=tmp_path, profile_path=PROFILE, group_id="10001", self_member_id="20001",
                   run_id="run-one", run_async=asyncio.run, clock=lambda: clock.wall, monotonic=lambda: clock.mono,
                   development_mode=True)
    driver = homeostatic.QQHomeostaticDriver(**options)
    yield driver, clock, options
    driver.close()


def enqueue(driver, clock, identity="message-1", kind="verified_message", **extra):
    arguments = dict(fact_id=identity, kind=kind, occurred_at=clock.wall, evidence_refs=[f"fixture://{identity}"])
    if kind == "verified_interaction":
        arguments["related_attempt_id"] = "actual-send-receipt"
    return driver.enqueue_fact(**{**arguments, **extra})


def rows(driver):
    with closing(driver._connect()) as db:
        return {row["fact_id"]: dict(row) for row in db.execute("SELECT * FROM facts")}


def factor_values(driver):
    return {key: factor.value for key, factor in driver.engine.snapshot.dimensions.items()}


def set_marked(driver, *, high=False):
    engine = driver.engine
    factors = dict(engine.snapshot.dimensions)
    for name, value in {"social_connection": 0.94 if high else 0.22, "novelty": 0.94 if high else 0.16}.items():
        factors[name] = factors[name].model_copy(update={"value": value})
    affect = engine._derive_affect(factors)
    summary = build_interoceptive_summary(
        engine.profile, revision=engine.snapshot.revision, factors=factors, affect=affect,
        emotion=engine.snapshot.emotion, candidates=engine._rank_policies(factors), watchdog=[],
    )
    assert summary.salience == "marked"
    engine.snapshot = engine.snapshot.model_copy(update={"dimensions": factors, "interoceptive_summary": summary})


def test_driver_uses_actual_summary_fields_and_monotonic_quiet_interval(case):
    driver, clock, _ = case
    assert not driver.eligible(quiet_seconds=30)
    set_marked(driver)
    assert not driver.eligible(quiet_seconds=30)
    clock.wall += timedelta(hours=4)
    assert not driver.eligible(quiet_seconds=30)
    clock.mono += 30
    assert driver.eligible(quiet_seconds=30)
    set_marked(driver, high=True)
    assert not driver.eligible(quiet_seconds=30)


def test_shared_opportunity_uses_noticeable_readiness_without_legacy_quiet(case):
    driver, clock, _ = case
    set_marked(driver)
    snapshot = driver.engine.snapshot
    driver.engine.snapshot = snapshot.model_copy(update={"interoceptive_summary":
        snapshot.interoceptive_summary.model_copy(update={"salience": "noticeable"})})
    assert not driver.eligible(quiet_seconds=120)
    assert driver.eligible(quiet_seconds=120, shared_opportunity=True)
    enqueue(driver, clock)
    assert not driver.eligible(quiet_seconds=120, shared_opportunity=True)


def test_tick_runs_at_five_second_cadence_not_wall_time_and_allow_heartbeat_is_respected(case):
    driver, clock, _ = case
    driver.tick()
    assert driver.engine.snapshot.revision == 1
    clock.wall += timedelta(hours=8)
    for _ in range(20):
        driver.tick()
    assert driver.engine.snapshot.revision == 1
    clock.mono += 4.9
    driver.tick()
    assert driver.engine.snapshot.revision == 1
    clock.mono += 0.1
    driver.tick()
    assert driver.engine.snapshot.revision == 2
    clock.advance(5)
    driver.tick(allow_heartbeat=False)
    assert driver.engine.snapshot.revision == 2
    assert driver.status()["tick_seconds"] == 5.0


@pytest.mark.parametrize("dimension,edge", [("energy_reserve", 0), ("cognitive_load", 1)])
def test_existing_marked_need_cannot_override_energy_or_load_limit(case, dimension, edge):
    driver, clock, _ = case
    set_marked(driver)
    clock.advance(1800)
    assert driver.eligible(quiet_seconds=1800)
    factors = dict(driver.engine.snapshot.dimensions)
    original = factors[dimension]
    factors[dimension] = original.model_copy(update={"value": original.target_band[edge]})
    driver.engine.snapshot = driver.engine.snapshot.model_copy(update={"dimensions": factors})
    assert driver.summary.salience == "marked"
    assert not driver.eligible(quiet_seconds=1800)
    factors[dimension] = original
    driver.engine.snapshot = driver.engine.snapshot.model_copy(update={"dimensions": factors})
    assert driver.eligible(quiet_seconds=1800)


def test_expired_prefix_is_retired_and_does_not_block_fresh_fact_or_heartbeat(case):
    driver, clock, _ = case
    enqueue(driver, clock, "expired")
    clock.advance(301)
    enqueue(driver, clock, "fresh")
    before = factor_values(driver)
    driver.tick()
    recorded = rows(driver)
    assert recorded["expired"]["applied"] == 2 and recorded["expired"]["terminal_reason"] == "fact_expired"
    assert recorded["fresh"]["applied"] == 1
    assert driver.engine.snapshot.revision == 2  # One actual fact and heartbeat.
    assert factor_values(driver)["novelty"] > before["novelty"]
    assert driver.status()["feedback_pending"] == 0
    assert driver.status()["feedback_retirement_reasons"] == {"fact_expired": 1}
    assert driver.status()["last_error_type"] is None


def test_domain_commit_is_recovered_after_restart_without_reapplying_old_fact(case):
    driver, clock, options = case
    enqueue(driver, clock)
    raw = json.loads(rows(driver)["message-1"]["record_json"])
    driver._apply("ApplyEvent", {"event": {"event_type": "qq_fact", "source_time": raw["occurred_at"],
                    "payload": {"qq_fact": raw}}}, key="message-1")
    assert rows(driver)["message-1"]["applied"] == 0
    before, revision = factor_values(driver), driver.engine.snapshot.revision
    driver.close()
    clock.advance(600)
    restarted = homeostatic.QQHomeostaticDriver(**{**options, "run_id": "run-two"})
    try:
        restarted.tick(allow_heartbeat=False)
        assert factor_values(restarted) == before and restarted.engine.snapshot.revision == revision
        assert rows(restarted)["message-1"]["terminal_reason"] == "domain_recovered"
        assert restarted.status()["feedback_applied"] == 1
    finally:
        restarted.close()


def test_unapplied_old_fact_after_restart_is_retired_not_replayed(case):
    driver, clock, options = case
    enqueue(driver, clock)
    before = factor_values(driver)
    driver.close()
    clock.advance(600)
    restarted = homeostatic.QQHomeostaticDriver(**{**options, "run_id": "run-two"})
    try:
        restarted.tick(allow_heartbeat=False)
        assert factor_values(restarted) == before and restarted.engine.snapshot.revision == 0
        assert rows(restarted)["message-1"]["terminal_reason"] == "fact_expired"
    finally:
        restarted.close()


def test_duplicate_queue_fact_preserves_original_record_and_quiet_time(case):
    driver, clock, _ = case
    assert enqueue(driver, clock)
    old = rows(driver)["message-1"]["record_json"]
    original_time = driver._last_activity_mono
    clock.advance(10)
    assert enqueue(driver, clock, occurred_at=START) is False
    assert rows(driver)["message-1"]["record_json"] == old
    assert driver._last_activity_mono == original_time
    with pytest.raises(ValueError, match="fact_conflict"):
        enqueue(driver, clock, occurred_at=START, evidence_refs=["fixture://changed"])


def test_legacy_timestamp_format_is_semantically_duplicate(case):
    driver, clock, _ = case
    enqueue(driver, clock)
    record = json.loads(rows(driver)["message-1"]["record_json"])
    record["occurred_at"] = START.isoformat()
    old = json.dumps(record)
    with closing(driver._connect()) as db, db:
        db.execute("UPDATE facts SET record_json=?", (old,))
    assert enqueue(driver, clock) is False
    assert rows(driver)["message-1"]["record_json"] == old


@pytest.mark.parametrize("corruption", ["invalid", "foreign_group", "wrong_id"])
def test_corrupt_or_cross_scope_pending_rows_retire_without_domain_effect(case, corruption):
    driver, clock, _ = case
    enqueue(driver, clock)
    raw = json.loads(rows(driver)["message-1"]["record_json"])
    if corruption == "invalid":
        raw["text"] = "unsupported content field"
    else:
        raw["group_id" if corruption == "foreign_group" else "fact_id"] = "99999"
    with closing(driver._connect()) as db, db:
        db.execute("UPDATE facts SET record_json=?", (json.dumps(raw),))
    before = factor_values(driver)
    driver.tick(allow_heartbeat=False)
    assert rows(driver)["message-1"]["applied"] == 2
    assert factor_values(driver) == before and driver.engine.snapshot.revision == 0


def test_future_fact_retires_and_does_not_create_activity(case):
    driver, clock, _ = case
    previous = driver._last_activity_mono
    enqueue(driver, clock, occurred_at=clock.wall + timedelta(seconds=6))
    driver.tick(allow_heartbeat=False)
    assert rows(driver)["message-1"]["terminal_reason"] == "fact_future"
    assert driver._last_activity_mono == previous


def test_domain_fact_conflict_retires_queue_without_rewriting_previous_fact(case):
    driver, clock, _ = case
    enqueue(driver, clock)
    raw = json.loads(rows(driver)["message-1"]["record_json"])
    driver._apply("ApplyEvent", {"event": {"source_time": raw["occurred_at"], "payload": {"qq_fact": raw}}}, key="message-1")
    before = factor_values(driver)
    raw["kind"] = "verified_novelty"
    with closing(driver._connect()) as db, db:
        db.execute("UPDATE facts SET record_json=?", (json.dumps(raw),))
    driver.tick(allow_heartbeat=False)
    assert rows(driver)["message-1"]["terminal_reason"] == "fact_conflict"
    assert factor_values(driver) == before and driver.engine.snapshot.revision == 1


def test_temporary_failure_keeps_pending_and_blocks_eligibility_until_success(case, monkeypatch):
    driver, clock, _ = case
    enqueue(driver, clock)
    apply = driver.engine.apply_event
    async def unavailable(command):
        raise OSError("synthetic unavailable")
    monkeypatch.setattr(driver.engine, "apply_event", unavailable)
    with pytest.raises(OSError):
        driver.tick(allow_heartbeat=False)
    assert rows(driver)["message-1"]["applied"] == 0 and driver.last_error == "OSError"
    set_marked(driver)
    assert not driver.eligible(quiet_seconds=0)
    monkeypatch.setattr(driver.engine, "apply_event", apply)
    clock.advance(5)
    driver.tick(allow_heartbeat=False)
    assert driver.last_error is None and rows(driver)["message-1"]["applied"] == 1


def test_send_attempt_and_no_response_do_not_reward_connection(case):
    driver, clock, _ = case
    before = factor_values(driver)
    enqueue(driver, clock, "send", "send_attempt")
    enqueue(driver, clock, "silence", "no_response")
    driver.tick(allow_heartbeat=False)
    assert factor_values(driver) == before and driver.status()["feedback_applied"] == 2


def test_pending_feedback_blocks_idle_until_current_facts_are_accounted_for(case):
    driver, clock, _ = case
    set_marked(driver)
    assert driver.eligible(quiet_seconds=0)
    enqueue(driver, clock)
    assert not driver.eligible(quiet_seconds=0)
    driver.tick(allow_heartbeat=False)
    assert driver.status()["feedback_pending"] == 0


@pytest.mark.parametrize("key,value", [("group_id", "99999"), ("self_member_id", "99999")])
def test_wrong_scope_is_rejected_before_domain_is_constructed(case, monkeypatch, key, value):
    _, _, options = case
    def forbidden(*args, **kwargs):
        pytest.fail("domain must not be opened before scope rejection")
    monkeypatch.setattr(homeostatic, "HomeostasisEngine", forbidden)
    with pytest.raises(ValueError, match="scope_mismatch"):
        homeostatic.QQHomeostaticDriver(**{**options, key: value})


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), 301])
def test_invalid_tick_budget_is_rejected(case, interval):
    _, _, options = case
    with pytest.raises(ValueError, match="invalid_tick"):
        homeostatic.QQHomeostaticDriver(**{**options, "tick_seconds": interval})


def test_invalid_fact_fields_fail_before_enqueue_and_old_history_is_skipped(case):
    driver, clock, _ = case
    with pytest.raises(ValueError):
        enqueue(driver, clock, "bad", "verified_interaction", related_attempt_id=None)
    with pytest.raises(ValueError):
        enqueue(driver, clock, "bad", occurred_at=START.replace(tzinfo=None))
    with pytest.raises(ValueError):
        enqueue(driver, clock, "bad", evidence_refs=[])
    assert enqueue(driver, clock, "history", occurred_at=START - timedelta(seconds=1)) is False
    assert rows(driver) == {}
