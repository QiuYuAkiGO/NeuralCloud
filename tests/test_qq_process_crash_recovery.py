"""Real child-process crashes and recovery; only synthetic temporary SQLite.

The writer exits via os._exit after committed state, without Python cleanup.
Every recovery invocation is a new interpreter, not an object reconstruction
in the pytest process. No QQ, desktop backend, model or network is involved.
"""
import atexit
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).absolute().parents[1]
NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
GROUP, MEMBER, SELF = "10001", "20001", "30001"
TEXT = "synthetic prepared reply"
CRASH_CODE = 73


def _forbid_network(*args, **kwargs):
    raise AssertionError("No network is allowed in this process-boundary test")


def _write_manifest(path, value):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())


def _writer(directory, case):
    from harness.qq import decision
    from harness.qq.memory import QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput
    from harness.qq.watch_ledger import VisibleItem, WatchLedger

    memory_path = directory / "memory.sqlite3"
    store = QQVisualMemoryStore(memory_path)
    profile = store.record_profile(QQVerifiedProfileInput(
        group_id=GROUP, member_id=MEMBER, display_name="synthetic person", display_name_source="qq_profile",
        verification="qq_ui_verified", capture_id="synthetic-profile", observed_at=NOW,
        group_evidence=["test://group"], member_evidence=["test://member"],
    )).profile
    source = store.record_observation(QQVisualObservationInput(
        group_id=GROUP, member_id=MEMBER, profile_record_id=profile.record_id,
        capture_id="synthetic-chat", capture_item_id="synthetic-item", observed_at=NOW,
        text="synthetic incoming question", evidence=["test://source"],
        author_attribution="qq_ui_verified", attribution_evidence=["test://member"],
    )).observation
    # The model is outside this persistence test. Seed a validated synthetic
    # completed decision, then use the real public receipt-writing API below.
    choice = decision.QQReplyDecision(
        decision_id="qq-decision:synthetic-process-crash", input_hash="synthetic-process-hash",
        created_at=NOW, completed_at=NOW, action="speak", reason="synthetic", reply=TEXT,
        decision_source="model", outcome="ready", group_id=GROUP, self_member_id=SELF,
        trigger_member_id=MEMBER, participants=[MEMBER], source_record_ids=[source.record_id],
        new_observation_record_ids=[source.record_id], evidence_refs=["test://source"], delivery_refs=[],
        model="synthetic", persona_version="synthetic", preset_id="synthetic",
    )
    with closing(decision._connect(memory_path)) as connection, connection:
        connection.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)",
            (choice.decision_id, choice.input_hash, GROUP, "completed", choice.model_dump_json()))

    watch = WatchLedger(directory / "watch.sqlite3", group_id=GROUP)
    watch.start_run(now=NOW)
    anchor = VisibleItem(direction="incoming", kind="text", text="synthetic anchor", author_key="synthetic person")
    incoming = anchor.model_copy(update={"text": "synthetic incoming question"})
    watch.observe(frame_signature="synthetic-baseline", items=[anchor], observed_at=NOW)
    event = watch.observe(frame_signature="synthetic-question", items=[anchor, incoming],
                           observed_at=NOW + timedelta(seconds=1)).events[-1]
    watch.bind_event(event.event_id, source.record_id)
    watch.attach_decision(event.event_id, choice.decision_id, "speak", new_decision=True)
    reservation = watch.reserve_send(event.event_id, choice.decision_id, self_member_id=SELF,
        reply_sha256=hashlib.sha256(TEXT.encode()).hexdigest(), now=NOW + timedelta(seconds=1))
    watch.start_send_attempt(reservation.reservation_id, now=NOW + timedelta(seconds=2))
    proof = dict(reservation_id=reservation.reservation_id, group_id=GROUP, self_member_id=SELF,
        reply_sha256=reservation.reply_sha256, frame_evidence_ref="test://preclick",
        context_signature="synthetic-current-context", verified_at=NOW + timedelta(seconds=2),
        before_outgoing_count=0, source_record_ids=[source.record_id])
    watch.mark_click_started(reservation.reservation_id, proof=proof,
                             require_click_timing=True, now=NOW + timedelta(seconds=2))
    receipt = None
    if case == "receipt":
        watch.mark_click_acknowledged(reservation.reservation_id,
            acknowledged_at=NOW + timedelta(seconds=3), now=NOW + timedelta(seconds=3))
        receipt = decision.record_qq_delivery(memory_db=memory_path, group_id=GROUP, self_member_id=SELF,
            delivered_text=TEXT, evidence_refs=["test://synthetic-new-bubble"], verification="qq_ui_verified",
            decision_id=choice.decision_id, source_record_ids=[source.record_id], delivered_at=NOW + timedelta(seconds=4))
    _write_manifest(directory / "writer.json", {
        "pid": os.getpid(), "run_id": watch.run_id, "reservation_id": reservation.reservation_id,
        "decision_id": choice.decision_id, "event_id": event.event_id,
        "receipt_id": receipt.receipt_id if receipt else None,
        "precrash": watch.get_send(reservation.reservation_id).model_dump(mode="json"),
    })
    atexit.register(lambda: (directory / "unexpected-cleanup.txt").write_text("atexit ran"))
    os._exit(CRASH_CODE)


def _recover(directory, round_number):
    from harness.qq.decision import get_qq_delivery
    from harness.qq.memory import QQVisualMemoryStore
    from harness.qq.watch_ledger import VisibleItem, WatchLedger

    manifest = json.loads((directory / "writer.json").read_text())
    memory_path = directory / "memory.sqlite3"
    watch = WatchLedger(directory / "watch.sqlite3", group_id=GROUP)
    moment = NOW + timedelta(seconds=10 * round_number)
    before = watch.get_send(manifest["reservation_id"])
    watch.start_run(now=moment)
    quarantined = watch.get_send(manifest["reservation_id"])
    receipt = get_qq_delivery(memory_db=memory_path, group_id=GROUP, decision_id=manifest["decision_id"])
    if receipt is not None:
        first = watch.reconcile_sent(manifest["reservation_id"], receipt=receipt, now=moment)
        second = watch.reconcile_sent(manifest["reservation_id"], receipt=receipt, now=moment + timedelta(seconds=1))
        assert first == second
        assert first.receipt_id == manifest["receipt_id"]
        blocked_reason = None
    else:
        assert quarantined.status == "uncertain"
        anchor = VisibleItem(direction="incoming", kind="text", text="synthetic current anchor")
        watch.observe(frame_signature=f"baseline-{round_number}", items=[anchor], observed_at=moment)
        fresh = watch.observe(frame_signature=f"new-{round_number}", items=[anchor, anchor.model_copy(
            update={"text": "synthetic later question"})], observed_at=moment + timedelta(seconds=1)).events[-1]
        watch.bind_event(fresh.event_id, "synthetic-later-source")
        watch.attach_decision(fresh.event_id, f"synthetic-later-decision-{round_number}", "speak", new_decision=True)
        assert watch.reserve_send(fresh.event_id, f"synthetic-later-decision-{round_number}",
            self_member_id=SELF, reply_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
            now=moment + timedelta(seconds=2)) is None
        blocked_reason = watch.get_event(fresh.event_id).reason
        assert blocked_reason == "unresolved_send_blocks_delivery"
    after = watch.get_send(manifest["reservation_id"])
    attempts = watch.delivery_attempts(manifest["reservation_id"])
    with closing(watch._connect()) as connection:
        sends = connection.execute("SELECT COUNT(*) FROM qq_watch_sends").fetchone()[0]
    with closing(sqlite3.connect(memory_path)) as connection:
        receipts = connection.execute("SELECT COUNT(*) FROM qq_reply_deliveries").fetchone()[0]
    context = QQVisualMemoryStore(memory_path).reply_context(group_id=GROUP, member_id=MEMBER)
    memory_count = len(context.observations)
    print(json.dumps({"pid": os.getpid(), "run_id": watch.run_id,
        "before_status": before.status, "restart_status": quarantined.status,
        "after": after.model_dump(mode="json"), "attempt_phases": [a.phase for a in attempts],
        "send_count": sends, "receipt_count": receipts, "memory_count": memory_count,
        "blocked_reason": blocked_reason}), flush=True)


def _child(directory, phase, extra):
    environment = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-u", str(Path(__file__).absolute()), "--child", phase,
                           str(directory), str(extra)], cwd=ROOT, env=environment,
                          capture_output=True, text=True, encoding="utf-8", timeout=25)


@pytest.mark.parametrize("case", ["unknown", "receipt"])
def test_hard_process_exit_quarantines_or_links_only_existing_durable_receipt(tmp_path, case):
    writer = _child(tmp_path, "writer", case)
    assert writer.returncode == CRASH_CODE, writer.stderr
    assert writer.stdout == "" and not (tmp_path / "unexpected-cleanup.txt").exists()
    manifest = json.loads((tmp_path / "writer.json").read_text())
    assert manifest["pid"] != os.getpid()
    assert manifest["precrash"]["status"] == "click_started"
    recovered = []
    for round_number in (1, 2):
        process = _child(tmp_path, "recover", round_number)
        assert process.returncode == 0, process.stderr
        value = json.loads(process.stdout)
        assert value["pid"] != os.getpid()
        assert value["run_id"] != manifest["run_id"]
        assert value["send_count"] == value["memory_count"] == 1
        assert value["after"]["preclick_proof"] == manifest["precrash"]["preclick_proof"]
        assert value["after"]["click_started_at"] == manifest["precrash"]["click_started_at"]
        assert value["after"]["attempt_count"] == 1
        recovered.append(value)
    assert recovered[0]["before_status"] == "click_started"
    assert recovered[0]["restart_status"] == "uncertain"
    assert recovered[0]["run_id"] != recovered[1]["run_id"]
    if case == "unknown":
        for value in recovered:
            assert value["after"]["status"] == "uncertain"
            assert value["attempt_phases"] == ["uncertain"]
            assert value["receipt_count"] == 0 and value["after"]["receipt_id"] is None
            assert value["blocked_reason"] == "unresolved_send_blocks_delivery"
            assert value["after"]["click_acknowledged_at"] is None
    else:
        for value in recovered:
            assert value["after"]["status"] == "sent"
            assert value["attempt_phases"] == ["sent"]
            assert value["receipt_count"] == 1 and value["after"]["receipt_id"] == manifest["receipt_id"]
            assert value["after"]["click_acknowledged_at"] == manifest["precrash"]["click_acknowledged_at"]
        assert recovered[0]["after"] == recovered[1]["after"]


if __name__ == "__main__":
    assert sys.argv[1] == "--child"
    socket.socket.connect = _forbid_network
    socket.socket.connect_ex = _forbid_network
    destination = Path(sys.argv[3])
    if sys.argv[2] == "writer":
        _writer(destination, sys.argv[4])
    else:
        assert sys.argv[2] == "recover"
        _recover(destination, int(sys.argv[4]))
