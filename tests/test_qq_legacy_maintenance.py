"""Explicit legacy rejection maintenance against synthetic private databases."""

from contextlib import closing
from datetime import timedelta
import hashlib
import json
import sqlite3

import pytest

from harness.qq import acceptance, maintenance
from harness.qq.watch_ledger import LegacyStepConflictRejection, WatchLedgerError
from harness.qq.watcher import worker_lock
from tests.test_qq_watch_ledger import GROUP, NOW, SELF, click_proof, ledger, reserved


BUILD = "qq-unattended:" + "a" * 64
REFS = ["evidence://sha256/" + "b" * 64]


def uncertain(directory):
    watch = ledger(directory)
    send = reserved(watch)
    proof = click_proof(reservation_id=send.reservation_id, body_instance_id="synthetic-body", environment_revision=2)
    watch.mark_click_started(send.reservation_id, proof=proof, require_click_timing=True, now=NOW)
    watch.mark_uncertain(send.reservation_id, reason="primitive_execution_unknown", now=NOW)
    return watch, send.reservation_id


def proof_for(reservation_id):
    return {
        "schema_version": "qq-legacy-step-conflict-rejection-v1",
        "kind": "legacy_step_conflict_rejection",
        "reservation_id": reservation_id,
        "attempted_action_id": "qq-send-" + hashlib.sha256(reservation_id.encode()).hexdigest(),
        "attempted_step_id": "send-once",
        "queried_at": NOW,
        "journal": {
            "schema_version": "agent-step-journal-v1",
            "body_instance_id": "synthetic-body",
            "environment_revision": 2,
            "step_id": "send-once",
            "state": "completed",
            "request_hash": "c" * 64,
            "started_at": NOW - timedelta(hours=1),
            "finished_at": NOW - timedelta(minutes=30),
            "result": {"action_id": "old-unrelated-action", "step_id": "send-once", "success": True},
        },
    }


def maintain(watch, identifier, **kwargs):
    kwargs.setdefault("proof", proof_for(identifier))
    return watch.maintain_legacy_step_conflict(
        identifier, legacy_build_hash=BUILD, legacy_evidence_refs=REFS, now=NOW, **kwargs
    )


def records(watch):
    with closing(sqlite3.connect(watch.path)) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "qq_watch_state",
                "qq_watch_runs",
                "qq_watch_screens",
                "qq_watch_events",
                "qq_watch_sends",
                "qq_watch_delivery_attempts",
            )
        }


def test_explicit_rejection_preserves_original_uncertain_audit_attempt_and_cooldown(tmp_path):
    watch, identifier = uncertain(tmp_path)
    before = records(watch)
    ended = maintain(watch, identifier)
    assert ended.status == "not_sent" and ended.reason == "legacy_step_conflict_rejection"
    assert watch.get_event(ended.event_id).state == "failed"
    assert not watch.has_uncertain_send() and not watch.unsettled_sends()
    after = records(watch)
    for table in ("qq_watch_state", "qq_watch_runs", "qq_watch_screens", "qq_watch_delivery_attempts"):
        assert after[table] == before[table]
    with closing(sqlite3.connect(watch.path)) as connection:
        row = connection.execute("SELECT state,evidence_json FROM qq_operator_delivery_repairs").fetchone()
    audit = json.loads(row[1])
    assert row[0] == "legacy_step_conflict_not_sent"
    assert audit["kind"] == "legacy_step_conflict_rejection" and audit["automatic_recovery"] is False
    assert audit["previous_reservation_json"] == before["qq_watch_sends"][0][-1]
    assert json.loads(audit["previous_reservation_json"])["status"] == "uncertain"
    assert audit["previous_event_json"] == before["qq_watch_events"][0][-1]
    assert audit["previous_attempt_json"] == [row[-1] for row in before["qq_watch_delivery_attempts"]]
    assert audit["delivery_receipt_created"] is False
    with pytest.raises(WatchLedgerError):
        watch.mark_click_started(identifier, now=NOW)
    with pytest.raises(WatchLedgerError):
        watch.start_send_attempt(identifier, now=NOW)


def test_exact_same_maintenance_is_idempotent_after_restart_without_refreshing_evidence(tmp_path):
    watch, identifier = uncertain(tmp_path)
    ended = maintain(watch, identifier)
    watch.start_run(now=NOW + timedelta(hours=1))
    state = records(watch)
    assert (
        watch.maintain_legacy_step_conflict(
            identifier,
            proof=proof_for(identifier),
            legacy_build_hash=BUILD,
            legacy_evidence_refs=REFS,
            now=NOW + timedelta(hours=1),
        )
        == ended
    )
    assert records(watch) == state
    with closing(sqlite3.connect(watch.path)) as connection:
        assert connection.execute("SELECT count(*) FROM qq_operator_delivery_repairs").fetchone()[0] == 1
    changed = proof_for(identifier)
    changed["journal"]["request_hash"] = "d" * 64
    with pytest.raises(WatchLedgerError, match="cannot be replaced"):
        maintain(watch, identifier, proof=changed)


@pytest.mark.parametrize(
    "fault",
    [
        "same_action",
        "wrong_reservation",
        "wrong_action",
        "new_step",
        "unknown",
        "started",
        "missing_hash",
        "missing_result",
        "wrong_instance",
        "wrong_revision",
        "too_recent",
        "reversed_clock",
        "stale_query",
        "future_query",
        "wrong_result_step",
    ],
)
def test_incomplete_or_unbound_proof_never_releases_uncertainty(tmp_path, fault):
    watch, identifier = uncertain(tmp_path)
    proof = proof_for(identifier)
    journal = proof["journal"]
    if fault == "same_action":
        journal["result"]["action_id"] = proof["attempted_action_id"]
    elif fault == "wrong_reservation":
        proof["reservation_id"] = "qq-watch-send:" + "f" * 32
    elif fault == "wrong_action":
        proof["attempted_action_id"] = "qq-send-" + "f" * 64
    elif fault == "new_step":
        proof["attempted_step_id"] = proof["attempted_action_id"] + ":send-once"
    elif fault in {"unknown", "started"}:
        journal["state"] = fault
    elif fault == "missing_hash":
        journal["request_hash"] = None
    elif fault == "missing_result":
        journal["result"] = None
    elif fault == "wrong_instance":
        journal["body_instance_id"] = "another-body"
    elif fault == "wrong_revision":
        journal["environment_revision"] = 3
    elif fault == "too_recent":
        journal["finished_at"] = NOW - timedelta(seconds=9)
    elif fault == "reversed_clock":
        journal["started_at"] = NOW
    elif fault == "stale_query":
        proof["queried_at"] = NOW - timedelta(seconds=31)
    elif fault == "future_query":
        proof["queried_at"] = NOW + timedelta(seconds=3)
    else:
        journal["result"]["step_id"] = "another-step"
    before = records(watch)
    with pytest.raises(ValueError):
        maintain(watch, identifier, proof=proof)
    assert records(watch) == before and watch.has_uncertain_send()


@pytest.mark.parametrize(
    "field,value",
    [
        ("click_acknowledged_at", NOW.isoformat()),
        ("click_observed_at", NOW.isoformat()),
        ("click_observed_evidence_ref", "test://verified-bubble"),
        ("receipt_id", "existing-receipt"),
        ("preclick_proof", None),
        ("status", "sent"),
    ],
)
def test_any_positive_delivery_or_missing_preclick_binding_forbids_rejection(tmp_path, field, value):
    watch, identifier = uncertain(tmp_path)
    with sqlite3.connect(watch.path) as connection:
        stored = json.loads(connection.execute("SELECT record_json FROM qq_watch_sends").fetchone()[0])
        stored[field] = value
        connection.execute("UPDATE qq_watch_sends SET status=?,record_json=?", (stored["status"], json.dumps(stored)))
    before = records(watch)
    with pytest.raises(WatchLedgerError):
        maintain(watch, identifier)
    assert records(watch) == before


def test_prior_operator_delivery_repair_is_not_overwritten(tmp_path):
    watch, identifier = uncertain(tmp_path)
    with sqlite3.connect(watch.path) as connection:
        connection.execute(
            "CREATE TABLE qq_operator_delivery_repairs(repair_id,original_reservation_id,state,updated_at,evidence_json)"
        )
        connection.execute(
            "INSERT INTO qq_operator_delivery_repairs VALUES(?,?,?,?,?)",
            ("older-repair", identifier, "sent", NOW.isoformat(), "[]"),
        )
    with pytest.raises(WatchLedgerError, match="prior operator repair"):
        maintain(watch, identifier)
    assert watch.has_uncertain_send()


@pytest.fixture
def cli_case(tmp_path, monkeypatch):
    root = tmp_path / "NeuralCloud/QQDesktop"
    root.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("harness.qq.watch_ledger._now", lambda value=None: value or NOW)
    watch, identifier = uncertain(root)
    memory, token, session, config, proof = [
        root / name for name in ("memory.db", "agent.token", "session.json", "watcher.json", "proof.json")
    ]
    token.write_text("fictional-secret-do-not-print")
    with sqlite3.connect(memory) as connection:
        connection.execute("CREATE TABLE qq_reply_deliveries(receipt_id,group_id,decision_id,record_json)")
        connection.execute("CREATE TABLE qq_reply_decisions(decision_id,group_id,status,record_json)")
    session.write_text(
        json.dumps(
            {"memory_db": str(memory), "token_file": str(token), "allowed_group_ids": [GROUP], "self_member_id": SELF}
        )
    )
    config.write_text(
        json.dumps(
            {
                "session_config_path": str(session),
                "state_db": str(watch.path),
                "status_file": str(root / "status.json"),
                "control_file": str(root / "control.json"),
                "body_token_file": str(token),
                "agent_token_file": str(token),
                "group_id": GROUP,
                "self_member_id": SELF,
                "expected_instance_id": "synthetic-body",
            }
        )
    )
    proof.write_text(LegacyStepConflictRejection.model_validate(proof_for(identifier)).model_dump_json())
    args = [
        "--config",
        str(config),
        "--reservation-id",
        identifier,
        "--proof-file",
        str(proof),
        "--legacy-build-hash",
        BUILD,
        "--legacy-evidence-ref",
        REFS[0],
    ]
    return watch, identifier, args, token, memory, config


def test_cli_never_constructs_worker_or_calls_network_and_redacts_output(cli_case, monkeypatch, capsys):
    watch, identifier, args, _, _, _ = cli_case
    monkeypatch.setattr("harness.qq.watcher.QQWatcher", lambda *_a, **_k: pytest.fail("worker constructed"))
    monkeypatch.setattr("httpx.Client.send", lambda *_a, **_k: pytest.fail("network called"))
    assert maintenance.main(args) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "not_sent"
    assert all(secret not in output for secret in (identifier, "fictional-secret", GROUP, SELF, BUILD, REFS[0]))
    assert watch.run_id is not None and not watch.has_uncertain_send()


def test_cli_requires_worker_exclusion_and_absence_of_any_decision_receipt(cli_case, capsys):
    watch, identifier, args, token, memory, _ = cli_case
    before = records(watch)
    with worker_lock(token.with_suffix(".watcher.lock")):
        assert maintenance.main(args) == 2
    assert records(watch) == before
    with sqlite3.connect(memory) as connection:
        connection.execute(
            "INSERT INTO qq_reply_deliveries VALUES(?,?,?,?)",
            ("receipt", GROUP, watch.get_send(identifier).decision_id, "{}"),
        )
    assert maintenance.main(args) == 2 and records(watch) == before
    assert "fictional-secret" not in capsys.readouterr().out


def test_operator_maintenance_is_detected_during_formal_run_but_old_audit_can_be_baseline(cli_case):
    watch, identifier, _, _, memory, config_path = cli_case
    settings = json.loads(config_path.read_text())
    config = acceptance.AcceptanceConfig(
        run_dir=config_path.parent / "formal",
        watcher_status_path=config_path.parent / "status.json",
        trace_file=config_path.parent / "trace.jsonl",
        ledger_db=watch.path,
        memory_db=memory,
        worker_identity={"pid": 1234, "started_at": NOW},
        build_hash=BUILD,
    )
    before = acceptance._inventory(acceptance._sources(config))
    maintain(watch, identifier)
    after = acceptance._inventory(acceptance._sources(config))
    assert before["repairs"] == {} and len(after["repairs"]) == 1
    assert before["repairs"] != after["repairs"]  # Existing sample/report rejection branch.
    settings.clear()  # No configuration or repair evidence is printed.
