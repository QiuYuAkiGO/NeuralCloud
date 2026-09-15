"""Real temporary SQLite; no desktop, model, or private records."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3

import pytest
from pydantic import ValidationError

from harness.qq.decision import QQDeliveryReceipt, QQSegmentDeliveryReceipt
from harness.qq.idle import QQIdleIntent, validated_idle_intent
from harness.qq.watch_ledger import PreClickProof, VisibleItem, WatchEvent, WatchLedger, WatchLedgerError, address_sources_for_event


NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)
SCOPE = dict(self_member_id="29999", body_instance_id="synthetic-body", environment_revision=2,
             window_id="synthetic-window", context_signature="synthetic-frame")


def idle(run_id="synthetic-run", *, moment=NOW, **updates):
    return QQIdleIntent(**(dict(intent_id="qq-idle:synthetic", run_id=run_id, group_id="10001",
        created_at=moment, deadline_at=moment + timedelta(seconds=60), episode_signature="synthetic-episode",
        homeostasis_revision=3, homeostatic_interoception=dict(profile_ref="synthetic@1", snapshot_revision=3,
            salience="noticeable", episode_signature="synthetic-episode"), **SCOPE) | updates))


def setup_watch(tmp_path, *, baseline=True):
    watch = WatchLedger(tmp_path / "synthetic.sqlite3", group_id="10001")
    watch.start_run(now=NOW)
    if baseline:
        watch.observe(frame_signature="synthetic-frame", items=[VisibleItem(direction="incoming", text="synthetic anchor")], observed_at=NOW)
    return watch


def register(watch, intent=None, *, moment=NOW, **options):
    return watch.register_idle_intent(intent or idle(watch.run_id, moment=moment), now=moment, **(SCOPE | options))


def reserve(watch, event, *, text="synthetic reply", moment=NOW):
    watch.attach_decision(event.event_id, "synthetic-decision", "speak", new_decision=True)
    return watch.reserve_send(event.event_id, "synthetic-decision", self_member_id=SCOPE["self_member_id"],
        reply_sha256=hashlib.sha256(text.encode()).hexdigest(), now=moment)


def proof(reservation, *, moment=NOW, **updates):
    return PreClickProof(**(dict(reservation_id=reservation.reservation_id, group_id="10001",
        self_member_id=SCOPE["self_member_id"], reply_sha256=reservation.reply_sha256,
        verified_at=moment, frame_evidence_ref="test://fresh", evidence_refs=["test://fresh"],
        context_signature="fresh-frame-after-input", window_id=SCOPE["window_id"],
        body_instance_id=SCOPE["body_instance_id"], environment_revision=2,
        title_pixels_sha256="a" * 64, before_outgoing_count=0) | updates))


def test_idle_is_bound_without_fabricated_message_and_cannot_be_attributed(tmp_path):
    watch = setup_watch(tmp_path)
    event = register(watch)
    assert event.origin == "homeostasis" and event.state == "bound"
    assert event.item is event.item_index is event.screen_id is event.source_record_id is None
    assert event.first_seen_at == NOW and address_sources_for_event(event) == []
    with pytest.raises(WatchLedgerError):
        watch.bind_event(event.event_id, "fabricated-source")
    with pytest.raises(WatchLedgerError):
        watch.defer_attribution(event.event_id, "synthetic-error")
    assert watch.get_event(event.event_id) == event


@pytest.mark.parametrize("mutation", [dict(item_index=0), dict(screen_id="fake"), dict(source_record_id="fake"),
    dict(origin="message"), dict(idle_intent=None), dict(first_seen_at=NOW-timedelta(seconds=1))])
def test_event_origin_cannot_be_forged(tmp_path, mutation):
    event = register(setup_watch(tmp_path))
    with pytest.raises(ValidationError):
        WatchEvent.model_validate(event.model_dump() | mutation)


@pytest.mark.parametrize("update", [dict(deadline_at=NOW), dict(deadline_at=NOW+timedelta(seconds=61)),
    dict(homeostasis_revision=4), dict(episode_signature="other"), dict(environment_revision=True),
    dict(intent_id=" "), dict(group_id="not-qq"), dict(created_at=NOW.replace(tzinfo=None))])
def test_full_intent_validation(update):
    with pytest.raises(ValidationError):
        idle(**update)


def test_model_copy_cannot_bypass_nested_summary_validation():
    original = idle()
    forged = original.model_copy(update={"homeostatic_interoception":
        original.homeostatic_interoception.model_copy(update={"salience": "forged"})})
    with pytest.raises(ValidationError):
        validated_idle_intent(forged)


@pytest.mark.parametrize("update", [dict(self_member_id="28888"), dict(body_instance_id="other"),
    dict(environment_revision=3), dict(window_id="other"), dict(context_signature="other")])
def test_registration_scope_is_exact(tmp_path, update):
    watch = setup_watch(tmp_path)
    with pytest.raises(WatchLedgerError):
        register(watch, **update)


def test_requires_baseline_and_original_unexpired_lifetime(tmp_path):
    watch = setup_watch(tmp_path, baseline=False)
    with pytest.raises(WatchLedgerError, match="baseline"):
        register(watch)
    watch.observe(frame_signature="baseline", items=[], observed_at=NOW)
    for instant in (NOW+timedelta(seconds=60), NOW-timedelta(seconds=3)):
        with pytest.raises(WatchLedgerError):
            register(watch, idle(watch.run_id), moment=instant)


@pytest.mark.parametrize("outcome", ["wait", "failed"])
def test_opportunity_consumed_on_wait_or_failure_and_same_episode_can_return_after_cooldown(tmp_path, outcome):
    watch = setup_watch(tmp_path)
    event = register(watch)
    if outcome == "wait":
        watch.attach_decision(event.event_id, "synthetic-wait", "wait", new_decision=True)
    else:
        watch.finish_event(event.event_id, outcome="failed", reason="synthetic-error")
    assert register(watch) is None
    for episode in ("synthetic-episode", "other-episode"):
        next_intent = idle(watch.run_id, moment=NOW+timedelta(seconds=20), intent_id="new-intent",
            episode_signature=episode, homeostatic_interoception=dict(profile_ref="synthetic@1",
                snapshot_revision=3, salience="noticeable", episode_signature=episode))
        assert register(watch, next_intent, moment=next_intent.created_at) is None
    later = NOW+timedelta(seconds=1800)
    accepted = register(watch, idle(watch.run_id, moment=later, intent_id="later-same-episode"), moment=later)
    assert accepted is not None and accepted.idle_intent.episode_signature == event.idle_intent.episode_signature


def test_duplicate_ids_are_immutable_and_concurrent_opportunities_are_atomic(tmp_path):
    watch = setup_watch(tmp_path)
    intents = [idle(watch.run_id, intent_id=f"candidate-{i}") for i in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        events = list(pool.map(lambda value: register(watch, value), intents))
    accepted = next(event for event in events if event)
    assert sum(event is not None for event in events) == 1
    with pytest.raises(WatchLedgerError, match="rebound"):
        register(watch, accepted.idle_intent.model_copy(update={"context_signature": "other"}), context_signature="other")
    with sqlite3.connect(watch.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM qq_watch_idle_opportunities").fetchone()[0] == 1


def test_restart_preserves_cooldown_and_abandons_old_idle_without_replay(tmp_path):
    watch = setup_watch(tmp_path)
    old = register(watch)
    rebuilt = WatchLedger(watch.path, group_id="10001")
    rebuilt.start_run(now=NOW+timedelta(seconds=2))
    assert rebuilt.get_event(old.event_id).state == "ignored"
    rebuilt.observe(frame_signature="restart-baseline", items=[], observed_at=NOW+timedelta(seconds=2))
    next_intent = idle(rebuilt.run_id, moment=NOW+timedelta(seconds=3), intent_id="new-run-intent")
    assert register(rebuilt, next_intent, moment=next_intent.created_at) is None
    with pytest.raises(WatchLedgerError):
        rebuilt.attach_decision(old.event_id, "old-decision", "speak", new_decision=True)


def test_advisory_next_opportunity_is_readonly_and_survives_restart(tmp_path):
    watch = setup_watch(tmp_path)
    assert watch.next_idle_opportunity_at() is None
    event = register(watch)
    watch.attach_decision(event.event_id, "wait", "wait", new_decision=True)
    before = watch.path.read_bytes()
    assert watch.next_idle_opportunity_at() == NOW+timedelta(seconds=1800)
    assert watch.path.read_bytes() == before
    rebuilt = WatchLedger(watch.path, group_id="10001")
    assert rebuilt.next_idle_opportunity_at(minimum_interval_seconds=300) == NOW+timedelta(seconds=300)
    rebuilt.start_run(now=NOW+timedelta(seconds=10))
    assert rebuilt.next_idle_opportunity_at() == NOW+timedelta(seconds=1800)
    for value in (-1, float("nan"), float("inf"), True, "300"):
        with pytest.raises(WatchLedgerError):
            rebuilt.next_idle_opportunity_at(minimum_interval_seconds=value)


@pytest.mark.parametrize("priority,self_id", [("direct", "29999"), ("ambient", "28888")])
def test_idle_cannot_gain_direct_priority_or_other_sender(tmp_path, priority, self_id):
    watch = setup_watch(tmp_path)
    event = register(watch)
    watch.attach_decision(event.event_id, "decision", "speak", new_decision=True)
    with pytest.raises(WatchLedgerError):
        watch.reserve_send(event.event_id, "decision", priority=priority, self_member_id=self_id, reply_sha256="a"*64, now=NOW)


def test_shorter_original_deadline_and_new_message_preempt_idle(tmp_path):
    watch = setup_watch(tmp_path)
    event = register(watch, idle(watch.run_id, deadline_at=NOW+timedelta(seconds=10)))
    parent = reserve(watch, event)
    assert parent.deadline_at == NOW+timedelta(seconds=10)
    watch.observe(frame_signature="new", items=[VisibleItem(direction="incoming", text="synthetic anchor"),
        VisibleItem(direction="incoming", text="synthetic new")], observed_at=NOW+timedelta(seconds=2))
    with pytest.raises(WatchLedgerError, match="stream"):
        watch.mark_click_started(parent.reservation_id, proof=proof(parent), now=NOW+timedelta(seconds=2))
    assert watch.get_send(parent.reservation_id).click_started_at is None


@pytest.mark.parametrize("mutation", [dict(source_record_ids=["fake-source"]), dict(window_id="other"),
    dict(body_instance_id="other"), dict(environment_revision=3)])
def test_idle_proof_rejects_sources_or_scope_change(tmp_path, mutation):
    watch = setup_watch(tmp_path)
    parent = reserve(watch, register(watch))
    with pytest.raises(WatchLedgerError):
        watch.mark_click_started(parent.reservation_id, proof=proof(parent, **mutation), now=NOW)


def test_single_delivery_has_empty_addressed_sources_and_once_receipt(tmp_path):
    watch = setup_watch(tmp_path)
    parent = reserve(watch, register(watch))
    watch.mark_click_started(parent.reservation_id, proof=proof(parent), now=NOW, require_click_timing=True)
    watch.mark_click_acknowledged(parent.reservation_id, acknowledged_at=NOW+timedelta(seconds=1), now=NOW+timedelta(seconds=1))
    receipt = QQDeliveryReceipt(receipt_id="synthetic-receipt", decision_id=parent.decision_id,
        group_id="10001", self_member_id="29999", delivered_text="synthetic reply", evidence_refs=["test://after"],
        source_record_ids=[], delivered_at=NOW+timedelta(seconds=2), verification="qq_ui_verified", origin="decision")
    sent = watch.reconcile_sent(parent.reservation_id, receipt=receipt, now=NOW+timedelta(seconds=2))
    assert sent.status == "sent"
    assert watch.reconcile_sent(parent.reservation_id, receipt=receipt, now=NOW+timedelta(seconds=5)) == sent
    with pytest.raises(WatchLedgerError):
        watch.reconcile_sent(parent.reservation_id, receipt=receipt.model_copy(update={"source_record_ids": ["fake"]}), now=NOW+timedelta(seconds=5))


def test_segmented_idle_retains_once_proofs_and_empty_addressed_sources(tmp_path):
    watch = setup_watch(tmp_path)
    texts = ["synthetic first", "synthetic second"]
    parent = reserve(watch, register(watch), text="\n\n".join(texts))
    children = watch.initialize_segments(parent.reservation_id, texts)
    receipts = []
    for index, child in enumerate(children):
        instant = NOW+timedelta(seconds=index*3)
        watch.start_segment_attempt(child.child_id, now=instant)
        watch.mark_segment_click_started(child.child_id, proof=proof(child, moment=instant), now=instant)
        watch.mark_segment_click_acknowledged(child.child_id, acknowledged_at=instant+timedelta(seconds=1), now=instant+timedelta(seconds=1))
        receipt = QQSegmentDeliveryReceipt(receipt_id=f"segment-receipt-{index}", decision_id=parent.decision_id,
            segment_index=index, segment_count=2, group_id="10001", self_member_id="29999", delivered_text=child.text,
            evidence_refs=["test://fresh", f"test://after-{index}"], source_record_ids=[],
            delivered_at=instant+timedelta(seconds=2), verification="qq_ui_verified")
        receipts.append(receipt)
        assert watch.reconcile_segment_sent(child.child_id, receipt=receipt, now=instant+timedelta(seconds=2)).status == "sent"
    final = QQDeliveryReceipt(receipt_id="aggregate", decision_id=parent.decision_id, group_id="10001",
        self_member_id="29999", delivered_text="\n\n".join(texts), delivered_segments=texts,
        segment_receipt_ids=[r.receipt_id for r in receipts], source_record_ids=[],
        evidence_refs=sorted({ref for r in receipts for ref in r.evidence_refs}), origin="decision",
        delivered_at=NOW+timedelta(seconds=6), verification="qq_ui_verified")
    assert watch.finish_segmented_send(parent.reservation_id, aggregate_receipt=final, now=NOW+timedelta(seconds=6)).status == "sent"
