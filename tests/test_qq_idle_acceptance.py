"""Offline acceptance samples: real temporary SQLite, no live runtime."""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3

import pytest

from harness.qq import acceptance as module
from harness.qq.idle import QQIdleIntent
from tests.test_qq_acceptance import (
    setup as setup, add_turn, start, complete_samples, full_dataset, change_record,
    replace_trace, health, NOW, MONO, GROUP, SELF,
)
from tests.test_qq_segment_acceptance import segmented_sources


def internalize(trace, event, sources):
    first = module._date(trace["first_seen_at"])
    intent = QQIdleIntent(intent_id="qq-idle:" + event["event_id"], run_id=event["run_id"],
        group_id=GROUP, self_member_id=SELF, created_at=first, deadline_at=first+timedelta(seconds=60),
        episode_signature="synthetic-episode", homeostasis_revision=3,
        homeostatic_interoception=dict(profile_ref="synthetic@1", snapshot_revision=3,
            salience="noticeable", episode_signature="synthetic-episode"),
        context_signature="synthetic-creation-frame", window_id="window:synthetic",
        body_instance_id="body:synthetic", environment_revision=1)
    event.update(origin="homeostasis", idle_intent=intent.model_dump(mode="json"),
                 item=None, item_index=None, screen_id=None, source_record_id=None)
    decision = sources["decisions"][trace["decision_id"]]
    decision.update(initiated_by="homeostasis", idle_intent_id=intent.intent_id, self_member_id=SELF,
        trigger_member_id=None, new_observation_record_ids=[], seen_observation_record_ids=[],
        addressing="ambient", addressing_source_record_ids=[], addressing_evidence_refs=[])
    trace["counters"].pop("cold_path", None)
    trace["counters"]["homeostatic_idle"] = 1
    for send in sources["sends"].values():
        if send["event_id"] != event["event_id"]:
            continue
        send.update(run_id=event["run_id"], deadline_at=intent.deadline_at.isoformat())
        send["preclick_proof"]["source_record_ids"] = []
        for attempt in sources["attempts"].get(send["reservation_id"], []):
            attempt["proof"] = deepcopy(send["preclick_proof"])
        for child in sources.get("segments", {}).values():
            if child["parent_id"] == send["reservation_id"]:
                child["proof"]["source_record_ids"] = []
                for attempt in sources["attempts"].get(child["child_id"], []):
                    attempt["proof"] = deepcopy(child["proof"])
    for collection in ("receipts", "segment_receipts"):
        for receipt in sources.get(collection, {}).values():
            if receipt["decision_id"] == decision["decision_id"]:
                receipt["source_record_ids"] = []
    return intent


def add_idle(config, index=200, *, wait=False, latency=45):
    trace = add_turn(config, index, kind="natural_wait" if wait else "warm_speak", latency=latency)
    sources = module._sources(config)
    event = sources["events"][trace["event_id"]]
    internalize(trace, event, sources)
    change_record(config.ledger_db, "qq_watch_events", "event_id", event)
    frozen = dict(sources["decisions"][trace["decision_id"]])
    frozen.pop("_stored_status", None)
    change_record(config.memory_db, "qq_reply_decisions", "decision_id", frozen)
    for send in sources["sends"].values():
        if send["event_id"] == event["event_id"]:
            change_record(config.ledger_db, "qq_watch_sends", "reservation_id", send)
            for attempt in sources["attempts"][send["reservation_id"]]:
                change_record(config.ledger_db, "qq_watch_delivery_attempts", "attempt_id", attempt)
    for receipt in sources["receipts"].values():
        if receipt["decision_id"] == trace["decision_id"]:
            change_record(config.memory_db, "qq_reply_deliveries", "receipt_id", receipt)
    replace_trace(config, trace)
    return trace


def test_idle_does_not_fill_message_budgets_or_change_message_p95(setup):
    start(setup)
    full_dataset(setup)
    add_idle(setup, 200, latency=45)
    add_idle(setup, 201, wait=True, latency=48)
    complete_samples(setup)
    report = module.report_run(setup.run_dir)
    assert report["status"] == "PASS"
    assert report["counts"] == {"warm_speak": 40, "cold_speak": 5, "natural_wait": 10}
    assert report["p95_seconds"] == {"warm_speak": 20, "cold_speak": 20}
    assert report["idle"]["counts"] == {"speak": 1, "natural_wait": 1}
    assert report["idle"]["latency_seconds"]["speak"]["p95"] == 45
    assert report["idle"]["latency_seconds"]["natural_wait"]["max"] == 48
    assert report["idle"]["included_in_message_targets"] is False
    assert {t["kind"] for t in report["turns"] if t.get("origin") == "homeostasis"} == {"idle_speak", "idle_natural_wait"}


def test_only_idle_samples_never_pass_and_item_none_never_crashes_complete_report(setup):
    start(setup)
    for index in range(10):
        add_idle(setup, index, wait=index%2 == 0)
    complete_samples(setup)
    report = module.report_run(setup.run_dir, write=True)
    assert report["status"] == "INSUFFICIENT_SAMPLES" and report["reasons"] == []
    assert all(value == 0 for value in report["counts"].values())
    assert report["idle"]["counts"] == {"speak": 5, "natural_wait": 5}
    assert module.report_run(setup.run_dir) == report


@pytest.mark.parametrize("fault", ["missing_marker", "boolean_marker", "fake_message_marker", "wrong_intent",
    "source", "fake_item", "other_group", "other_run", "revision", "origin", "trigger", "new_ids", "direct", "cold_idle"])
def test_mixed_or_forged_origin_never_bypasses_author_checks(setup, fault):
    start(setup)
    trace = add_idle(setup, wait=True)
    sources = module._sources(setup)
    event, decision = sources["events"][trace["event_id"]], sources["decisions"][trace["decision_id"]]
    if fault == "missing_marker":
        trace["counters"].pop("homeostatic_idle")
    elif fault == "boolean_marker":
        trace["counters"]["homeostatic_idle"] = True
    elif fault == "fake_message_marker":
        event["origin"] = "message"
    elif fault == "wrong_intent":
        decision["idle_intent_id"] = "other-intent"
    elif fault == "source":
        event["source_record_id"] = "fabricated-source"
    elif fault == "fake_item":
        event["item"] = {"direction": "incoming", "text": "fabricated"}
    elif fault == "other_group":
        event["idle_intent"]["group_id"] = "98765432"
    elif fault == "other_run":
        event["idle_intent"]["run_id"] = "other-run"
    elif fault == "revision":
        event["idle_intent"]["homeostasis_revision"] = 4
    elif fault == "origin":
        decision["initiated_by"] = "message"
    elif fault == "trigger":
        decision["trigger_member_id"] = "123123123"
    elif fault == "new_ids":
        decision["new_observation_record_ids"] = ["fake-source"]
    elif fault == "direct":
        decision["addressing"] = "direct"
    else:
        trace["counters"]["cold_path"] = 1
    replace_trace(setup, trace)
    decision.pop("_stored_status", None)
    change_record(setup.ledger_db, "qq_watch_events", "event_id", event)
    change_record(setup.memory_db, "qq_reply_decisions", "decision_id", decision)
    complete_samples(setup)
    report = module.report_run(setup.run_dir)
    assert report["status"] == "FAIL"
    assert sum(report["counts"].values()) == sum(report["idle"]["counts"].values()) == 0


@pytest.mark.parametrize("fault", ["source", "proof_source", "window", "body", "revision", "self", "deadline", "late_click"])
def test_idle_single_receipt_still_requires_exact_body_and_empty_addressed_sources(setup, fault):
    trace = add_idle(setup)
    sources = module._sources(setup)
    event = sources["events"][trace["event_id"]]
    send, receipt = next(iter(sources["sends"].values())), next(iter(sources["receipts"].values()))
    proof = send["preclick_proof"]
    if fault == "source":
        receipt["source_record_ids"] = ["fake"]
    elif fault == "proof_source":
        proof["source_record_ids"] = ["fake"]
    elif fault in {"window", "body", "revision"}:
        proof[{"window":"window_id", "body":"body_instance_id", "revision":"environment_revision"}[fault]] = "other" if fault != "revision" else 9
    elif fault == "self":
        send["self_member_id"] = "11111111"
    elif fault == "deadline":
        send["deadline_at"] = (module._date(send["deadline_at"])+timedelta(seconds=60)).isoformat()
    else:
        send["click_started_at"] = send["deadline_at"]
    with pytest.raises(module.AcceptanceError):
        module._link_delivery(trace, event, sources)


def test_segmented_idle_links_every_bubble_but_counts_as_one_idle_sample(setup, monkeypatch):
    start(setup)
    trace, event, sources = segmented_sources(setup)
    internalize(trace, event, sources)
    replace_trace(setup, trace)
    assert module._link_delivery(trace, event, sources)[2] == 27
    monkeypatch.setattr(module, "_sources", lambda _: sources)
    complete_samples(setup)
    report = module.report_run(setup.run_dir)
    assert report["reasons"] == []
    assert report["counts"]["warm_speak"] == 0 and report["idle"]["counts"]["speak"] == 1
    assert report["turns"][0]["segment_count"] == 3
    sources["segments"]["child:1"]["proof"]["source_record_ids"] = ["fake"]
    with pytest.raises(module.AcceptanceError):
        module._link_delivery(trace, event, sources)


@pytest.mark.parametrize("failure", ["model_error", "expired", "uncertain", "paused", "gap"])
def test_idle_does_not_disable_global_failures(setup, failure):
    start(setup)
    trace = add_idle(setup, wait=failure in {"model_error", "expired"})
    if failure in {"model_error", "expired"}:
        sources = module._sources(setup)
        event = sources["events"][trace["event_id"]]
        event["state"] = "failed" if failure == "model_error" else "expired"
        with sqlite3.connect(setup.ledger_db) as db:
            db.execute("UPDATE qq_watch_events SET state=?,record_json=? WHERE event_id=?", (event["state"], json.dumps(event), event["event_id"]))
        trace["counters"].pop("turn_natural_wait")
        trace["counters"]["turn_failed"] = 1
        replace_trace(setup, trace)
    elif failure == "uncertain":
        sources = module._sources(setup)
        send = next(iter(sources["sends"].values()))
        send.update(status="uncertain", updated_at=NOW.isoformat())
        with sqlite3.connect(setup.ledger_db) as db:
            db.execute("UPDATE qq_watch_sends SET status=?,record_json=?", ("uncertain", json.dumps(send)))
    else:
        health(setup, NOW+timedelta(seconds=15), **({"running":False,"automatic_send":False} if failure == "paused" else {}))
        if failure == "gap":
            status = json.loads(setup.watcher_status_path.read_text())
            status["observer"]["gap_seconds"] = 12
            setup.watcher_status_path.write_text(json.dumps(status))
        module.sample_run(setup.run_dir, now=NOW+timedelta(seconds=15), monotonic=MONO+15)
    complete_samples(setup)
    report = module.report_run(setup.run_dir)
    assert report["status"] == "FAIL"
    expected = {"model_error":"turn_failed", "expired":"event_failed_or_expired", "uncertain":"uncertain_send",
                "paused":"worker_flag_disabled", "gap":"occlusion_observation_gap"}[failure]
    assert expected in report["reasons"]


def test_malformed_message_item_is_reported_not_dereferenced(setup):
    start(setup)
    trace = add_turn(setup, 0, kind="natural_wait")
    change_record(setup.ledger_db, "qq_watch_events", "event_id", {"event_id":trace["event_id"], "item":None})
    complete_samples(setup)
    assert "event_item_invalid" in module.report_run(setup.run_dir)["reasons"]


def test_eventless_terminal_preparation_is_reported_without_crashing(setup):
    start(setup)
    trace = add_idle(setup, wait=True)
    trace["event_id"] = None
    trace["counters"].pop("turn_natural_wait")
    trace["counters"]["turn_failed"] = 1
    replace_trace(setup, trace)
    complete_samples(setup)
    report = module.report_run(setup.run_dir)
    assert report["status"] == "FAIL" and "event_first_seen_mismatch" in report["reasons"]
    with pytest.raises(module.AcceptanceError, match="event_first_seen_mismatch"):
        module._turn_origin(trace, None)


def test_idle_wait_obeys_shorter_original_deadline(setup):
    start(setup)
    trace = add_idle(setup, wait=True, latency=20)
    event = module._sources(setup)["events"][trace["event_id"]]
    event["idle_intent"]["deadline_at"] = (module._date(trace["first_seen_at"])+timedelta(seconds=10)).isoformat()
    change_record(setup.ledger_db, "qq_watch_events", "event_id", event)
    complete_samples(setup)
    report = module.report_run(setup.run_dir)
    assert report["status"] == "FAIL" and "idle_wait_deadline_expired" in report["reasons"]
    assert report["idle"]["counts"]["natural_wait"] == 0
