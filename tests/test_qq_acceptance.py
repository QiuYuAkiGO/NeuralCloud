"""Synthetic acceptance evidence only: no live databases, models or desktops."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from harness.qq import acceptance as module
from harness.qq.acceptance import (
    AcceptanceConfig, AcceptanceError, DURATION_SECONDS, initialize_run, main,
    report_run, sample_run,
)


NOW = datetime(2026, 9, 11, 8, tzinfo=timezone.utc)
MONO = 10000
GROUP = "12345678"
SELF = "99887766"
PRIVATE = "PRIVATE_MEMBER_AND_MESSAGE_CONTENT"


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    actual_utc = module._utc
    monkeypatch.setattr(module, "_utc", lambda value=None: actual_utc(value or NOW))
    ledger, memory = tmp_path / "ledger.sqlite3", tmp_path / "memory.sqlite3"
    with sqlite3.connect(ledger) as db:
        db.executescript("""
            CREATE TABLE qq_watch_state(singleton,group_id);
            CREATE TABLE qq_watch_events(event_id,group_id,state,record_json);
            CREATE TABLE qq_watch_sends(reservation_id,event_id,decision_id,group_id,status,record_json);
            CREATE TABLE qq_watch_delivery_attempts(attempt_id,reservation_id,record_json);
            CREATE TABLE qq_operator_delivery_repairs(repair_id,state,updated_at,evidence_json);
        """)
        db.execute("INSERT INTO qq_watch_state VALUES(1,?)", (GROUP,))
    with sqlite3.connect(memory) as db:
        db.executescript("""
            CREATE TABLE qq_reply_decisions(decision_id,group_id,status,record_json);
            CREATE TABLE qq_reply_deliveries(receipt_id,group_id,decision_id,record_json);
        """)
    config = AcceptanceConfig(run_dir=tmp_path / "formal-run", watcher_status_path=tmp_path / "status.json",
                              trace_file=tmp_path / "trace.jsonl", ledger_db=ledger, memory_db=memory,
                              worker_identity={"pid": 1234, "started_at": NOW}, build_hash="synthetic-build-0123456")
    health(config, NOW)
    return config


def health(config, moment, **changes):
    value = {"pid": 1234, "started_at": NOW.isoformat(), "group_id": GROUP, "build_hash": config.build_hash,
             "running": True, "automatic_screen_capture": True, "automatic_send": True,
             "heartbeat_at": moment.isoformat(), "last_error_type": None,
             "last_capture_at": (NOW - timedelta(days=1)).isoformat(),
             "observer": {"running": True, "polls": 5, "changes": 2, "last_polled_at": moment.isoformat(),
                          "gap_seconds": 0, "gap_started_at": None, "overflows": 0, "last_error_type": None,
                          "gap_sequence": 0, "completed_gap_count": 0, "total_gap_seconds": 0,
                          "max_gap_seconds": 0, "recent_gaps": []}}
    value.update(changes)
    write(config.watcher_status_path, value)


def start(config):
    return initialize_run(config, now=NOW, monotonic=MONO)


def complete_samples(config, *, elapsed=DURATION_SECONDS):
    # Virtual-time continuity; no sleeping and no shortening the formal policy.
    path = config.run_dir / "samples.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        for offset in range(15, elapsed + 1, 15):
            stream.write(json.dumps({"sampled_at": (NOW + timedelta(seconds=offset)).isoformat(),
                                     "elapsed_seconds": offset, "metrics": {"occlusion_gap_seconds": 0},
                                     "reasons": []}) + "\n")


def add_turn(config, index, *, kind="warm_speak", latency=20, first_offset=None):
    offset = 100 + index * 100 if first_offset is None else first_offset
    first = NOW + timedelta(seconds=offset)
    event_id, decision_id, send_id, receipt_id, trace_id = [f"synthetic-{name}:{index}" for name in ("event", "decision", "send", "receipt", "trace")]
    source_id = f"source:{index}"
    wait = kind == "natural_wait"
    event = {"event_id": event_id, "group_id": GROUP, "run_id": "current-run",
             "state": "decided_wait" if wait else "sent", "first_seen_at": first.isoformat(),
             "observed_at": first.isoformat(), "source_record_id": source_id, "decision_id": decision_id,
             "item": {"direction": "incoming", "kind": "text", "text": PRIVATE, "author_key": PRIVATE}}
    decision = {"decision_id": decision_id, "group_id": GROUP, "action": "wait" if wait else "speak",
                "decision_source": "model", "outcome": "natural_wait" if wait else "ready",
                "reply": None if wait else PRIVATE, "source_record_ids": [source_id], "error_type": None}
    trace = {"schema_version": "qq-turn-trace-v1", "trace_id": trace_id, "event_id": event_id,
             "decision_id": decision_id, "first_seen_at": first.isoformat(), "first_seen_mono": MONO + offset,
             "recorded_at": (first + timedelta(seconds=latency)).isoformat(), "elapsed_ms": (latency + 1) * 1000,
             "stages": [{"name": "turn_completed", "outcome": "ok", "end_offset_ms": latency * 1000}] if wait else [], "counters": {"turn_natural_wait" if wait else "turn_sent": 1,
                                        "cold_path": int(kind == "cold_speak")}}
    with sqlite3.connect(config.ledger_db) as db:
        db.execute("INSERT INTO qq_watch_events VALUES(?,?,?,?)", (event_id, GROUP, event["state"], json.dumps(event)))
    with sqlite3.connect(config.memory_db) as db:
        db.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?)", (decision_id, GROUP, "completed", json.dumps(decision)))
    if not wait:
        reply_hash = hashlib.sha256(PRIVATE.encode()).hexdigest()
        proof = {"group_id": GROUP, "self_member_id": SELF, "reply_sha256": reply_hash,
                 "frame_evidence_ref": "private-evidence-reference", "context_signature": "digest",
                 "verified_at": (first + timedelta(seconds=2)).isoformat(), "before_outgoing_count": 0,
                 "window_id": "window:synthetic", "body_instance_id": "body:synthetic",
                 "environment_revision": 1, "title_pixels_sha256": "a" * 64}
        send = {"reservation_id": send_id, "event_id": event_id, "decision_id": decision_id,
                "group_id": GROUP, "self_member_id": SELF, "status": "sent", "attempt_count": 1,
                "preclick_proof": proof, "reply_sha256": reply_hash,
                "created_at": (first + timedelta(seconds=1)).isoformat(), "evidence_refs": ["bubble-evidence"],
                "click_timing_required": True, "click_started_at": (first + timedelta(seconds=2)).isoformat(),
                "click_acknowledged_at": (first + timedelta(seconds=2.1)).isoformat()}
        receipt = {"receipt_id": receipt_id, "group_id": GROUP, "self_member_id": SELF,
                   "decision_id": decision_id, "delivered_text": PRIVATE, "evidence_refs": ["bubble-evidence"],
                   "verification": "qq_ui_verified", "origin": "decision", "source_record_ids": [source_id],
                   "delivered_at": (first + timedelta(seconds=latency - 0.5)).isoformat()}
        attempt = {"attempt_id": f"attempt:{index}", "reservation_id": send_id, "phase": "sent", "proof": proof}
        with sqlite3.connect(config.ledger_db) as db:
            db.execute("INSERT INTO qq_watch_sends VALUES(?,?,?,?,?,?)", (send_id, event_id, decision_id, GROUP, "sent", json.dumps(send)))
            db.execute("INSERT INTO qq_watch_delivery_attempts VALUES(?,?,?)", (attempt["attempt_id"], send_id, json.dumps(attempt)))
        with sqlite3.connect(config.memory_db) as db:
            db.execute("INSERT INTO qq_reply_deliveries VALUES(?,?,?,?)", (receipt_id, GROUP, decision_id, json.dumps(receipt)))
        trace["stages"] = [{"name": "receipt_persisted", "outcome": "ok", "end_offset_ms": latency * 1000}]
    with config.trace_file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(trace) + "\n")
    return trace


def replace_trace(config, trace):
    # A subsequent flush snapshot replaces rather than increments the same turn.
    with config.trace_file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(trace) + "\n")


def change_record(path, table, identifier, value):
    with sqlite3.connect(path) as db:
        record = json.loads(db.execute(f"SELECT record_json FROM {table} WHERE {identifier}=?", (value[identifier],)).fetchone()[0])
        record.update(value)
        db.execute(f"UPDATE {table} SET record_json=? WHERE {identifier}=?", (json.dumps(record), value[identifier]))


def full_dataset(config):
    for index in range(55):
        add_turn(config, index, kind="warm_speak" if index < 40 else "cold_speak" if index < 45 else "natural_wait")


def test_full_eight_hour_run_requires_all_three_natural_sample_budgets(setup):
    start(setup)
    full_dataset(setup)
    complete_samples(setup)
    result = report_run(setup.run_dir, write=True)
    assert result["status"] == "PASS" and result["fully_automatic_e2e_pass"]
    assert result["counts"] == {"warm_speak": 40, "cold_speak": 5, "natural_wait": 10}
    assert result["p95_seconds"] == {"warm_speak": 20, "cold_speak": 20}
    text = json.dumps(result)
    for secret in (PRIVATE, GROUP, SELF, "private-evidence-reference", str(setup.memory_db)):
        assert secret not in text
    assert all(item["event_id"].startswith("id:") for item in result["turns"])


def test_duration_cannot_be_shortened_and_enough_samples_early_is_still_running(setup):
    start(setup)
    full_dataset(setup)
    complete_samples(setup, elapsed=6000)
    result = report_run(setup.run_dir)
    assert result["status"] == "RUNNING" and not result["fully_automatic_e2e_pass"]
    with pytest.raises(ValueError):
        AcceptanceConfig.model_validate(setup.model_dump() | {"duration_seconds": 1})


@pytest.mark.parametrize("kind,count", [("warm_speak", 39), ("cold_speak", 4), ("natural_wait", 9)])
def test_insufficient_samples_are_not_pass(setup, kind, count):
    start(setup)
    for index in range(count):
        add_turn(setup, index, kind=kind)
    complete_samples(setup)
    assert report_run(setup.run_dir)["status"] == "INSUFFICIENT_SAMPLES"


def test_quiet_group_poll_heartbeat_not_old_durable_capture_proves_liveness(setup):
    start(setup)
    health(setup, NOW + timedelta(seconds=15))
    sample = sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    assert sample["reasons"] == []


@pytest.mark.parametrize("change,reason", [
    ({"pid": 9999}, "worker_identity_changed"),
    ({"running": False}, "worker_flag_disabled"),
    ({"automatic_send": False}, "worker_flag_disabled"),
    ({"automatic_screen_capture": False}, "worker_flag_disabled"),
    ({"last_error_type": "PRIVATE_EXCEPTION"}, "worker_error"),
    ({"heartbeat_at": (NOW - timedelta(seconds=50)).isoformat()}, "heartbeat_gap"),
    ({"human_intervention": True}, "human_intervention"),
])
def test_status_degradation_persists_failure_after_recovery(setup, change, reason):
    start(setup)
    health(setup, NOW + timedelta(seconds=15), **change)
    sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    health(setup, NOW + timedelta(seconds=30))
    sample_run(setup.run_dir, now=NOW + timedelta(seconds=30), monotonic=MONO + 30)
    result = report_run(setup.run_dir)
    assert result["status"] == "FAIL" and reason in result["reasons"]
    assert "PRIVATE_EXCEPTION" not in json.dumps(result)


def test_observer_staleness_and_occlusion_gap_are_separate(setup):
    start(setup)
    health(setup, NOW + timedelta(seconds=15))
    value = json.loads(setup.watcher_status_path.read_text())
    value["observer"].update(last_polled_at=NOW.isoformat(), gap_seconds=7)
    write(setup.watcher_status_path, value)
    sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    report = report_run(setup.run_dir)
    assert "observation_gap" in report["reasons"] and report["max_occlusion_gap_seconds"] == 7


def test_visible_chat_gap_cannot_pass_merely_because_polling_is_alive(setup):
    start(setup)
    health(setup, NOW + timedelta(seconds=15))
    value = json.loads(setup.watcher_status_path.read_text())
    value["observer"]["gap_seconds"] = 12
    write(setup.watcher_status_path, value)
    sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    assert "occlusion_observation_gap" in report_run(setup.run_dir)["reasons"]


def gap_record(sequence, start, duration):
    return {"sequence": sequence, "started_at": (NOW + timedelta(seconds=start)).isoformat(),
            "finished_at": (NOW + timedelta(seconds=start + duration)).isoformat(),
            "duration_seconds": duration, "reason": "chat_occluded"}


def closed_gaps(config, moment, gaps, *, count=None, total=None, maximum=None):
    health(config, moment)
    value = json.loads(config.watcher_status_path.read_text())
    sequence = count if count is not None else (gaps[-1]["sequence"] if gaps else 0)
    value["observer"].update(gap_sequence=sequence, completed_gap_count=sequence,
        total_gap_seconds=sum(gap["duration_seconds"] for gap in gaps) if total is None else total,
        max_gap_seconds=max((gap["duration_seconds"] for gap in gaps), default=0) if maximum is None else maximum,
        recent_gaps=gaps)
    write(config.watcher_status_path, value)


def test_gap_that_opens_and_closes_between_monitor_samples_cannot_pass(setup):
    start(setup)
    full_dataset(setup)
    closed_gaps(setup, NOW + timedelta(seconds=15), [gap_record(1, 1, 11)])
    sample = sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    assert "completed_observation_gap" in sample["reasons"]
    assert sample["metrics"]["closed_gaps"][0]["duration_seconds"] == 11
    complete_samples(setup)
    result = report_run(setup.run_dir)
    assert result["status"] == "FAIL" and result["max_occlusion_gap_seconds"] == 11


def test_preexisting_maximum_does_not_hide_a_smaller_new_overlong_gap(setup):
    old = gap_record(1, -40, 20)
    closed_gaps(setup, NOW, [old])
    manifest = start(setup)
    assert manifest["observer_gap_baseline"]["max_seconds"] == 20
    assert report_run(setup.run_dir)["status"] == "RUNNING"
    closed_gaps(setup, NOW + timedelta(seconds=15), [old, gap_record(2, 1, 11)])
    sample = sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    assert "completed_observation_gap" in sample["reasons"]
    assert [gap["sequence"] for gap in sample["metrics"]["closed_gaps"]] == [2]


def test_gap_cursor_advances_without_recounting_history(setup):
    start(setup)
    first = gap_record(1, 1, 2)
    closed_gaps(setup, NOW + timedelta(seconds=15), [first])
    one = sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    closed_gaps(setup, NOW + timedelta(seconds=30), [first, gap_record(2, 20, 3)])
    two = sample_run(setup.run_dir, now=NOW + timedelta(seconds=30), monotonic=MONO + 30)
    assert not one["reasons"] and not two["reasons"]
    assert [gap["sequence"] for gap in one["metrics"]["closed_gaps"]] == [1]
    assert [gap["sequence"] for gap in two["metrics"]["closed_gaps"]] == [2]


def test_gap_ring_overflow_between_samples_is_explicit_failure(setup):
    start(setup)
    gaps = [gap_record(index, index * .2, .1) for index in range(3, 35)]
    closed_gaps(setup, NOW + timedelta(seconds=15), gaps, count=34, total=3.4)
    sample = sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    assert "observer_gap_history_overflow" in sample["reasons"]


@pytest.mark.parametrize("state", ["waiting_for_observer", "observer_unavailable"])
def test_short_observation_gap_can_temporarily_disable_flags_then_recover(setup, state):
    start(setup)
    health(setup, NOW + timedelta(seconds=15), state=state, automatic_send=False,
           automatic_screen_capture=False, last_error_type="request_timeout")
    value = json.loads(setup.watcher_status_path.read_text())
    value["observer"].update(gap_started_at=(NOW + timedelta(seconds=13)).isoformat(),
                             gap_seconds=2, max_gap_seconds=2, last_error_type="request_timeout")
    write(setup.watcher_status_path, value)
    sample = sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    assert sample["reasons"] == []
    closed_gaps(setup, NOW + timedelta(seconds=30), [gap_record(1, 13, 3)])
    recovered = sample_run(setup.run_dir, now=NOW + timedelta(seconds=30), monotonic=MONO + 30)
    assert recovered["reasons"] == []
    assert report_run(setup.run_dir)["status"] == "RUNNING"


@pytest.mark.parametrize("change,reason", [
    ({"state": "paused"}, "worker_flag_disabled"),
    ({"running": False}, "worker_flag_disabled"),
    ({"pid": 99999}, "worker_identity_changed"),
    ({"human_intervention": True}, "human_intervention"),
])
def test_short_gap_never_excuses_pause_shutdown_restart_or_manual_intervention(setup, change, reason):
    start(setup)
    health(setup, NOW + timedelta(seconds=15), state="waiting_for_observer", automatic_send=False,
           automatic_screen_capture=False)
    value = json.loads(setup.watcher_status_path.read_text())
    value.update(change)
    value["observer"].update(gap_started_at=(NOW + timedelta(seconds=14)).isoformat(), gap_seconds=1,
                             max_gap_seconds=1, evidence_in_progress=True)
    write(setup.watcher_status_path, value)
    sample = sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    assert reason in sample["reasons"]


def test_missing_gap_accounting_cannot_start_formal_acceptance(setup):
    value = json.loads(setup.watcher_status_path.read_text())
    del value["observer"]["recent_gaps"]
    write(setup.watcher_status_path, value)
    with pytest.raises(AcceptanceError, match="observer_gap_evidence_invalid"):
        start(setup)


@pytest.mark.parametrize("cohort", [None, True, 2])
def test_sent_sample_requires_explicit_integer_warm_or_cold_cohort(setup, cohort):
    start(setup)
    trace = add_turn(setup, 1)
    if cohort is None:
        del trace["counters"]["cold_path"]
    else:
        trace["counters"]["cold_path"] = cohort
    setup.trace_file.write_text(json.dumps(trace) + "\n", encoding="utf-8")
    report = report_run(setup.run_dir)
    assert report["status"] == "FAIL" and report["counts"]["warm_speak"] == report["counts"]["cold_speak"] == 0


def test_new_untraced_incoming_event_cannot_disappear_from_completed_run(setup):
    start(setup)
    add_turn(setup, 1)
    setup.trace_file.write_text("", encoding="utf-8")
    with sqlite3.connect(setup.ledger_db) as db:
        event = json.loads(db.execute("SELECT record_json FROM qq_watch_events").fetchone()[0])
        event["state"] = "observed"
        event["source_record_id"] = event["decision_id"] = None
        db.execute("UPDATE qq_watch_events SET state='observed',record_json=?", (json.dumps(event),))
        db.execute("DELETE FROM qq_watch_sends")
    complete_samples(setup)
    assert "event_without_terminal_trace" in report_run(setup.run_dir)["reasons"]


@pytest.mark.parametrize("change", [{"decision_id": "other-decision"}, {"source_record_id": "other-source"}])
def test_wait_must_belong_to_this_event_source_and_decision(setup, change):
    start(setup)
    trace = add_turn(setup, 1, kind="natural_wait")
    change_record(setup.ledger_db, "qq_watch_events", "event_id", change | {"event_id": trace["event_id"]})
    result = report_run(setup.run_dir)
    assert "wait_not_natural" in result["reasons"] and result["counts"]["natural_wait"] == 0


@pytest.mark.parametrize("mode", ["added", "changed"])
def test_new_or_changed_manual_repair_fails_but_old_known_repair_does_not(setup, mode):
    with sqlite3.connect(setup.ledger_db) as db:
        db.execute("INSERT INTO qq_operator_delivery_repairs VALUES('old','sent','then','PRIVATE')")
    start(setup)
    assert report_run(setup.run_dir)["status"] == "RUNNING"
    with sqlite3.connect(setup.ledger_db) as db:
        if mode == "added":
            db.execute("INSERT INTO qq_operator_delivery_repairs VALUES('new','sent','now','PRIVATE')")
        else:
            db.execute("UPDATE qq_operator_delivery_repairs SET state='unknown'")
    assert "manual_repair_changed" in report_run(setup.run_dir)["reasons"]


def test_baseline_delivery_does_not_count_after_run_start(setup):
    add_turn(setup, 1, first_offset=-100)
    start(setup)
    complete_samples(setup)
    result = report_run(setup.run_dir)
    assert result["status"] == "INSUFFICIENT_SAMPLES"
    assert sum(result["counts"].values()) == 0


def test_repeat_trace_flush_uses_last_snapshot_once(setup):
    start(setup)
    trace = add_turn(setup, 1)
    trace["elapsed_ms"] += 100
    replace_trace(setup, trace)
    assert report_run(setup.run_dir)["counts"]["warm_speak"] == 1


@pytest.mark.parametrize("change", ["first_seen", "counter", "time"])
def test_regressed_or_reassigned_trace_is_rejected(setup, change):
    start(setup)
    trace = add_turn(setup, 1)
    if change == "first_seen":
        trace["first_seen_mono"] += 1
    elif change == "counter":
        trace["counters"]["turn_sent"] = 0
    else:
        trace["elapsed_ms"] -= 1
    replace_trace(setup, trace)
    assert "trace_identity_or_time_regressed" in report_run(setup.run_dir)["reasons"]


def test_prior_slow_receipt_stage_cannot_be_rewritten_in_later_snapshot(setup):
    start(setup)
    trace = add_turn(setup, 1, latency=45)
    trace["stages"][0]["end_offset_ms"] = 20 * 1000
    replace_trace(setup, trace)
    assert "trace_identity_or_time_regressed" in report_run(setup.run_dir)["reasons"]


@pytest.mark.parametrize("terminal", ["turn_batched", "turn_guard_wait", "turn_rate_limited", "turn_superseded"])
def test_explicit_non_sample_terminal_is_not_a_failed_or_natural_wait(setup, terminal):
    start(setup)
    trace = add_turn(setup, 1)
    trace["counters"] = {terminal: 1}
    setup.trace_file.write_text(json.dumps(trace) + "\n", encoding="utf-8")
    result = report_run(setup.run_dir)
    assert result["pending_turns"] == 0 and sum(result["counts"].values()) == 0
    assert result["turns"][0]["kind"] == "excluded"


def test_receipt_completed_after_eight_hour_window_cannot_count(setup):
    start(setup)
    add_turn(setup, 1, first_offset=DURATION_SECONDS - 5, latency=20)
    complete_samples(setup)
    result = report_run(setup.run_dir)
    assert result["status"] == "FAIL" and "receipt_completed_outside_run_window" in result["reasons"]


def test_wait_completed_after_window_or_deadline_is_not_a_natural_sample(setup):
    start(setup)
    add_turn(setup, 1, kind="natural_wait", first_offset=DURATION_SECONDS - 5, latency=20)
    add_turn(setup, 2, kind="natural_wait", latency=65)
    complete_samples(setup)
    result = report_run(setup.run_dir)
    assert "wait_completed_outside_run_window" in result["reasons"]
    assert "wait_completion_timing_invalid" in result["reasons"]
    assert result["counts"]["natural_wait"] == 0


def test_persisted_final_report_does_not_absorb_next_run_or_later_activity(setup):
    start(setup)
    full_dataset(setup)
    complete_samples(setup)
    final = report_run(setup.run_dir, write=True)
    add_turn(setup, 999, first_offset=DURATION_SECONDS + 100)
    with sqlite3.connect(setup.ledger_db) as db:
        db.execute("INSERT INTO qq_operator_delivery_repairs VALUES('later','sent','later','PRIVATE')")
    assert report_run(setup.run_dir, write=True) == final


def test_automatic_receipt_binding_and_read_only_reconciliation_count_exact_delivery(setup):
    start(setup)
    add_turn(setup, 1)
    change_record(setup.ledger_db, "qq_watch_sends", "reservation_id", {"reservation_id": "synthetic-send:1", "reconciled_at": NOW.isoformat()})
    result = report_run(setup.run_dir)
    assert result["status"] == "RUNNING" and result["counts"]["warm_speak"] == 1


def test_bounded_same_worker_reconciliation_can_temporarily_disable_send(setup):
    start(setup)
    add_turn(setup, 1)
    with sqlite3.connect(setup.ledger_db) as db:
        send = json.loads(db.execute("SELECT record_json FROM qq_watch_sends").fetchone()[0])
        sent = dict(send)
        send.update(status="uncertain", updated_at=(NOW + timedelta(seconds=10)).isoformat())
        db.execute("UPDATE qq_watch_sends SET status='uncertain',record_json=?", (json.dumps(send),))
    health(setup, NOW + timedelta(seconds=15), state="reconciling_delivery", automatic_send=False, last_error_type="bubble_pending")
    sample = sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    assert sample["reasons"] == [] and sample["metrics"]["reconciling_sends"] == 1
    with sqlite3.connect(setup.ledger_db) as db:
        db.execute("UPDATE qq_watch_sends SET status='sent',record_json=?", (json.dumps(sent),))
    assert report_run(setup.run_dir)["counts"]["warm_speak"] == 1


def test_unresolved_reconciliation_after_30_seconds_invalidates_run(setup):
    start(setup)
    add_turn(setup, 1)
    with sqlite3.connect(setup.ledger_db) as db:
        send = json.loads(db.execute("SELECT record_json FROM qq_watch_sends").fetchone()[0])
        send.update(status="uncertain", updated_at=(NOW - timedelta(seconds=20)).isoformat())
        db.execute("UPDATE qq_watch_sends SET status='uncertain',record_json=?", (json.dumps(send),))
    health(setup, NOW + timedelta(seconds=15), state="reconciling_delivery", automatic_send=False)
    sample = sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    assert "unresolved_uncertain_send" in sample["reasons"]


@pytest.mark.parametrize("mode", ["missing_receipt_stage", "cache", "timeout", "invalid_wait", "wrong_first_seen", "wrong_mono"])
def test_invalid_or_failed_model_work_is_never_natural_wait(setup, mode):
    start(setup)
    trace = add_turn(setup, 1, kind="natural_wait" if mode == "invalid_wait" else "warm_speak")
    if mode == "missing_receipt_stage":
        trace["stages"] = []
    elif mode == "cache":
        trace["counters"]["decision_cache_hits"] = 1
    elif mode == "timeout":
        trace["counters"] = {"turn_failed": 1}
    elif mode == "invalid_wait":
        change_record(setup.memory_db, "qq_reply_decisions", "decision_id", {"decision_id": trace["decision_id"], "outcome": "model_error", "error_type": "model_timeout"})
    elif mode == "wrong_first_seen":
        trace["first_seen_at"] = NOW.isoformat()
    else:
        trace["first_seen_mono"] += 99
    setup.trace_file.write_text(json.dumps(trace) + "\n", encoding="utf-8")
    result = report_run(setup.run_dir)
    assert result["status"] == "FAIL"
    assert sum(result["counts"].values()) == 0


@pytest.mark.parametrize("table,identity,change", [
    ("qq_reply_deliveries", "receipt_id", {"delivered_text": "DIFFERENT_PRIVATE_TEXT"}),
    ("qq_reply_deliveries", "receipt_id", {"origin": "external_verification"}),
    ("qq_reply_deliveries", "receipt_id", {"self_member_id": "11111"}),
    ("qq_reply_deliveries", "receipt_id", {"source_record_ids": ["unrelated"]}),
    ("qq_reply_deliveries", "receipt_id", {"evidence_refs": ["unrelated"]}),
    ("qq_watch_sends", "reservation_id", {"preclick_proof": None}),
    ("qq_watch_sends", "reservation_id", {"attempt_count": 4}),
])
def test_exact_delivery_chain_is_required(setup, table, identity, change):
    start(setup)
    add_turn(setup, 1)
    database = setup.ledger_db if table.startswith("qq_watch") else setup.memory_db
    value = "synthetic-send:1" if identity == "reservation_id" else "synthetic-receipt:1"
    change_record(database, table, identity, change | {identity: value})
    result = report_run(setup.run_dir)
    assert result["status"] == "FAIL" and result["counts"]["warm_speak"] == 0


def test_original_not_sent_remains_not_sent_even_with_external_repair_receipt(setup):
    start(setup)
    add_turn(setup, 1)
    with sqlite3.connect(setup.ledger_db) as db:
        record = json.loads(db.execute("SELECT record_json FROM qq_watch_sends").fetchone()[0])
        record["status"] = "not_sent"
        db.execute("UPDATE qq_watch_sends SET status='not_sent',record_json=?", (json.dumps(record),))
    result = report_run(setup.run_dir)
    assert result["counts"]["warm_speak"] == 0 and result["status"] == "FAIL"


def test_slow_samples_remain_in_percentile_and_fail_threshold(setup):
    start(setup)
    for index in range(40):
        add_turn(setup, index, latency=31 if index >= 37 else 20)
    result = report_run(setup.run_dir)
    assert result["counts"]["warm_speak"] == 40 and result["p95_seconds"]["warm_speak"] == 31
    assert "warm_p95_over_30_seconds" in result["reasons"]


def test_missing_terminal_and_skipped_sampling_cannot_pass(setup):
    start(setup)
    trace = add_turn(setup, 1)
    trace["counters"] = {}
    setup.trace_file.write_text(json.dumps(trace) + "\n", encoding="utf-8")
    health(setup, NOW + timedelta(seconds=DURATION_SECONDS))
    sample_run(setup.run_dir, now=NOW + timedelta(seconds=DURATION_SECONDS), monotonic=MONO + DURATION_SECONDS)
    reasons = report_run(setup.run_dir)["reasons"]
    assert "monitor_sampling_gap" in reasons and "unterminated_turn" in reasons


def test_source_files_are_read_only_and_missing_db_is_not_created(setup):
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (setup.ledger_db, setup.memory_db, setup.watcher_status_path)}
    start(setup)
    report_run(setup.run_dir, write=True)
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in hashes.items())
    missing = setup.model_copy(update={"memory_db": setup.memory_db.with_name("absent.sqlite3")})
    with pytest.raises(AcceptanceError):
        initialize_run(missing, now=NOW, monotonic=MONO)
    assert not missing.memory_db.exists()


def test_existing_run_cannot_be_restarted_or_monitor_replaced(setup):
    start(setup)
    with pytest.raises(AcceptanceError):
        start(setup)
    manifest = json.loads((setup.run_dir / "manifest.json").read_text())
    manifest["monitor_pid"] = -1
    write(setup.run_dir / "manifest.json", manifest)
    with pytest.raises(AcceptanceError, match="monitor_process_changed"):
        sample_run(setup.run_dir, now=NOW, monotonic=MONO)


def test_cli_status_exports_only_redacted_result_and_does_not_write_report(setup, capsys):
    start(setup)
    assert main(["status", "--run-dir", str(setup.run_dir)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "RUNNING"
    assert not (setup.run_dir / "report.json").exists()


def test_cli_invalid_config_hides_paths_and_exception_content(tmp_path, capsys):
    bad = tmp_path / "config.json"
    write(bad, {"PRIVATE_API_KEY": PRIVATE})
    assert main(["start", "--config", str(bad)]) == 2
    assert PRIVATE not in capsys.readouterr().out


def test_relative_private_paths_are_rejected(setup):
    with pytest.raises(ValueError):
        AcceptanceConfig.model_validate(setup.model_dump() | {"ledger_db": Path("relative.sqlite3")})
