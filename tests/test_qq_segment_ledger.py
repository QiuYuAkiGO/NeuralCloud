"""Synthetic temporary SQLite exercises the real segment and receipt protocols."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
from types import SimpleNamespace as NS

import pytest

from harness.qq import decision as choices
from harness.qq.memory import QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput
from harness.qq.watch_ledger import PreClickProof, VisibleItem, WatchLedger, WatchLedgerError


NOW = datetime(2026, 9, 11, 1, tzinfo=timezone.utc)
GROUP, MEMBER, SELF = "10001", "20001", "30001"


def at(seconds):
    return NOW + timedelta(seconds=seconds)


def setup(tmp_path, texts=None, *, priority="ambient"):
    texts = texts or ["合成第一段", "合成第二段", "合成第三段"]
    memory = tmp_path / "synthetic-memory.sqlite3"
    store = QQVisualMemoryStore(memory)
    profile = store.record_profile(QQVerifiedProfileInput(
        group_id=GROUP, member_id=MEMBER, display_name="虚构人物", display_name_source="qq_profile",
        verification="qq_ui_verified", capture_id="fictional-card", observed_at=NOW,
        group_evidence=["test://group"], member_evidence=["test://member"],
    )).profile
    source = store.record_observation(QQVisualObservationInput(
        group_id=GROUP, member_id=MEMBER, profile_record_id=profile.record_id, capture_id="fictional-chat",
        capture_item_id="fictional-bubble", observed_at=NOW, text="合成问题", evidence=["test://chat"],
        author_attribution="qq_ui_verified", attribution_evidence=["test://member"],
    )).observation
    decision = choices.QQReplyDecision(
        decision_id="qq-decision:fictional-segments", input_hash="fictional-input", created_at=NOW,
        completed_at=NOW, action="speak", reason="synthetic", reply="\n\n".join(texts), reply_segments=texts,
        decision_source="model", outcome="ready", group_id=GROUP, self_member_id=SELF,
        trigger_member_id=MEMBER, participants=[MEMBER], source_record_ids=[source.record_id],
        new_observation_record_ids=[source.record_id], evidence_refs=["test://chat"], delivery_refs=[],
        model="synthetic", persona_version="synthetic", preset_id="synthetic",
    )
    with closing(choices._connect(memory)) as connection, connection:
        connection.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)",
            (decision.decision_id, decision.input_hash, GROUP, "completed", decision.model_dump_json()))
    watch = WatchLedger(tmp_path / "synthetic-watch.sqlite3", group_id=GROUP)
    watch.start_run(now=NOW)
    anchor = VisibleItem(direction="incoming", text="合成基线", author_key="虚构人物")
    question = anchor.model_copy(update={"text": "合成问题"})
    watch.observe(frame_signature="baseline", items=[anchor], observed_at=NOW)
    event = watch.observe(frame_signature="incoming", items=[anchor, question], observed_at=at(1)).events[0]
    watch.bind_event(event.event_id, source.record_id)
    watch.attach_decision(event.event_id, decision.decision_id, "speak", new_decision=True)
    parent = watch.reserve_send(event.event_id, decision.decision_id, self_member_id=SELF,
        reply_sha256=hashlib.sha256(decision.reply.encode()).hexdigest(), priority=priority, now=at(2))
    segments = watch.initialize_segments(parent.reservation_id, texts)
    return NS(watch=watch, memory=memory, source=source, parent=parent, segments=segments,
              event=event, decision=decision, texts=texts, anchor=anchor, question=question)


def proof(scenario, index, seconds):
    segment = scenario.segments[index]
    return PreClickProof(reservation_id=segment.child_id, group_id=GROUP, self_member_id=SELF,
        reply_sha256=segment.reply_sha256, frame_evidence_ref=f"test://preclick-{index}",
        context_signature=f"synthetic-context-{index}", verified_at=at(seconds), before_outgoing_count=index,
        source_record_ids=[scenario.source.record_id], evidence_refs=[f"test://preclick-{index}"],
        window_id="0x100", body_instance_id="fictional-body", environment_revision=2, title_pixels_sha256="a"*64)


def click(scenario, index, seconds=3, *, observed=False):
    watch, segment = scenario.watch, scenario.segments[index]
    watch.start_segment_attempt(segment.child_id, now=at(seconds))
    watch.mark_segment_click_started(segment.child_id, proof=proof(scenario, index, seconds), now=at(seconds))
    if observed:
        watch.mark_segment_click_observed(segment.child_id, observed_at=at(seconds+.2),
            evidence_ref=f"test://after-{index}", now=at(seconds+.2))
    else:
        watch.mark_segment_click_acknowledged(segment.child_id, acknowledged_at=at(seconds+.2), now=at(seconds+.2))


def receipt(scenario, index, seconds=3):
    return choices.record_qq_segment_delivery(
        memory_db=scenario.memory, group_id=GROUP, self_member_id=SELF, decision_id=scenario.decision.decision_id,
        segment_index=index, segment_count=len(scenario.texts), delivered_text=scenario.texts[index],
        evidence_refs=[f"test://preclick-{index}", f"test://after-{index}"], verification="qq_ui_verified",
        source_record_ids=[scenario.source.record_id], delivered_at=at(seconds+.6),
    )


def complete(scenario, index, seconds=3, *, observed=False):
    click(scenario, index, seconds, observed=observed)
    value = receipt(scenario, index, seconds)
    scenario.watch.reconcile_segment_sent(scenario.segments[index].child_id, receipt=value, now=at(seconds+.7))
    return value


def aggregate(scenario, seconds=9):
    refs = [ref for segment in scenario.watch.segments(scenario.parent.reservation_id) for ref in segment.evidence_refs]
    return choices.record_qq_delivery(memory_db=scenario.memory, group_id=GROUP, self_member_id=SELF,
        decision_id=scenario.decision.decision_id, delivered_text=scenario.decision.reply,
        evidence_refs=refs, verification="qq_ui_verified", source_record_ids=[scenario.source.record_id],
        delivered_at=at(seconds))


@pytest.mark.parametrize("count", [1, 2, 3])
def test_complete_frozen_plan_has_independent_once_proofs_and_only_one_final_receipt(tmp_path, count):
    s = setup(tmp_path, [f"合成段{index}" for index in range(count)])
    child_receipts = []
    for index in range(count):
        child_receipts.append(complete(s, index, 3+index*2, observed=index == 1))
        assert s.watch.get_event(s.event.event_id).state != "sent"
        assert choices.get_qq_delivery(memory_db=s.memory, group_id=GROUP, decision_id=s.decision.decision_id) is None
        with pytest.raises(WatchLedgerError):
            s.watch.start_segment_attempt(s.segments[index].child_id, now=at(4+index*2))
    final = aggregate(s)
    done = s.watch.finish_segmented_send(s.parent.reservation_id, aggregate_receipt=final, now=at(9))
    assert done.status == "sent" and s.watch.get_event(s.event.event_id).state == "sent"
    assert done.receipt_id == final.receipt_id
    assert final.segment_receipt_ids == [r.receipt_id for r in child_receipts]
    assert s.watch.finish_segmented_send(s.parent.reservation_id, aggregate_receipt=final, now=at(30)) == done
    assert all(len(s.watch.segment_attempts(c.child_id)) == 1 for c in s.segments)
    assert len({c.child_id for c in s.segments}) == count
    with sqlite3.connect(s.watch.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM qq_watch_sends").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM qq_watch_events").fetchone()[0] == 1


def test_plan_ids_and_order_are_stable_and_no_duplicate_text_aliasing(tmp_path):
    s = setup(tmp_path, ["重复合成", "重复合成"])
    assert s.watch.initialize_segments(s.parent.reservation_id, list(s.texts)) == s.segments
    assert s.segments[0].reply_sha256 == s.segments[1].reply_sha256
    assert s.segments[0].child_id != s.segments[1].child_id
    rebuilt = WatchLedger(s.watch.path, group_id=GROUP)
    assert rebuilt.segments(s.parent.reservation_id) == s.segments
    with pytest.raises(WatchLedgerError):
        s.watch.initialize_segments(s.parent.reservation_id, ["改写合成"])


@pytest.mark.parametrize("texts", [[], ["a"]*4, [""], [" \t"], ["a\nb"], ["a\u2028b"], ["x"*4001], ["x"*4000]*3, "text", [None]])
def test_invalid_plans_do_not_replace_existing_plan(tmp_path, texts):
    s = setup(tmp_path)
    with pytest.raises(WatchLedgerError):
        s.watch.initialize_segments(s.parent.reservation_id, texts)
    assert s.watch.segments(s.parent.reservation_id) == s.segments


def test_no_skip_or_parallel_child_and_legacy_parent_cannot_click_or_finish(tmp_path):
    s = setup(tmp_path)
    with pytest.raises(WatchLedgerError):
        s.watch.start_segment_attempt(s.segments[1].child_id, now=at(3))
    with pytest.raises(WatchLedgerError):
        s.watch.start_send_attempt(s.parent.reservation_id, now=at(3))
    with pytest.raises(WatchLedgerError):
        s.watch.mark_click_started(s.parent.reservation_id, proof=proof(s, 0, 3), now=at(3))
    click(s, 0)
    with pytest.raises(WatchLedgerError):
        s.watch.start_segment_attempt(s.segments[1].child_id, now=at(4))
    with pytest.raises(WatchLedgerError):
        s.watch.mark_segment_click_started(s.segments[0].child_id, proof=proof(s, 0, 4), now=at(4))


@pytest.mark.parametrize("field,value", [
    ("reservation_id", "other-child"), ("group_id", "10002"), ("self_member_id", "30002"),
    ("reply_sha256", "f"*64), ("source_record_ids", []), ("window_id", None),
    ("body_instance_id", None), ("environment_revision", None), ("title_pixels_sha256", None),
    ("before_outgoing_count", None), ("evidence_refs", []), ("verified_at", at(-30)),
])
def test_click_proof_must_bind_the_exact_child_text_source_and_gui_scope(tmp_path, field, value):
    s = setup(tmp_path)
    s.watch.start_segment_attempt(s.segments[0].child_id, now=at(3))
    bad = proof(s, 0, 3).model_copy(update={field: value})
    with pytest.raises(WatchLedgerError):
        s.watch.mark_segment_click_started(s.segments[0].child_id, proof=bad, now=at(3))
    assert s.watch.get_segment(s.segments[0].child_id).click_started_at is None
    assert s.watch.segment_attempts(s.segments[0].child_id)[0].phase == "prechecking"


def test_later_segment_cannot_switch_window_body_or_revision(tmp_path):
    s = setup(tmp_path)
    complete(s, 0)
    s.watch.start_segment_attempt(s.segments[1].child_id, now=at(5))
    for field, value in [("window_id", "0x200"), ("body_instance_id", "other-body"), ("environment_revision", 3)]:
        with pytest.raises(WatchLedgerError):
            s.watch.mark_segment_click_started(s.segments[1].child_id,
                proof=proof(s, 1, 5).model_copy(update={field: value}), now=at(5))


def test_same_plan_avoids_ambient_delay_but_every_ack_advances_external_cooldown(tmp_path):
    s = setup(tmp_path)
    complete(s, 0, 3)
    assert s.watch.next_allowed_at(priority="ambient") == at(63.2)
    complete(s, 1, 5)
    assert s.watch.next_allowed_at(priority="ambient") == at(65.2)
    assert s.watch.next_allowed_at(priority="direct") == at(15.2)
    original = s.watch.get_segment(s.segments[1].child_id)
    assert s.watch.mark_segment_click_acknowledged(s.segments[1].child_id, acknowledged_at=at(40), now=at(40)) == original
    assert s.watch.next_allowed_at(priority="ambient") == at(65.2)


@pytest.mark.parametrize("change", ["stream", "cooldown", "deadline"])
def test_first_click_rechecks_parent_stream_cooldown_and_original_deadline(tmp_path, change):
    s = setup(tmp_path)
    s.watch.start_segment_attempt(s.segments[0].child_id, now=at(3))
    if change == "stream":
        s.watch.observe(frame_signature="changed", items=[s.anchor, s.question,
            VisibleItem(direction="incoming", text="另一个合成问题")], observed_at=at(4))
    elif change == "cooldown":
        with sqlite3.connect(s.watch.path) as connection:
            connection.execute("UPDATE qq_watch_state SET last_actual_activity_at=?", (at(2).isoformat(),))
    moment = 61 if change == "deadline" else 4
    with pytest.raises(WatchLedgerError):
        s.watch.mark_segment_click_started(s.segments[0].child_id, proof=proof(s, 0, moment), now=at(moment))
    assert s.watch.get_segment(s.segments[0].child_id).click_started_at is None


def test_preclick_retries_are_bounded_and_keep_all_failure_frames(tmp_path):
    s = setup(tmp_path)
    child = s.segments[0].child_id
    for attempt in range(3):
        seconds = 3+attempt*3
        current = s.watch.start_segment_attempt(child, now=at(seconds))
        assert current.attempt_count == attempt+1
        current = s.watch.defer_segment_preclick(child, reason="synthetic_precheck", retryable=True,
            evidence_refs=[f"test://failed-{attempt}"], now=at(seconds))
        if attempt < 2:
            assert current.status == "retry_wait"
            with pytest.raises(WatchLedgerError):
                s.watch.start_segment_attempt(child, now=at(seconds+1))
    assert current.status == "cancelled" and s.watch.get_reservation(s.parent.reservation_id).status == "not_sent"
    assert current.deadline_at == s.parent.deadline_at
    assert current.precheck_evidence_refs == [f"test://failed-{i}" for i in range(3)]
    assert [a.evidence_refs for a in s.watch.segment_attempts(child)] == [[f"test://failed-{i}"] for i in range(3)]
    with pytest.raises(WatchLedgerError):
        s.watch.start_segment_attempt(child, now=at(15))


@pytest.mark.parametrize("reason", ["partial_superseded", "partial_expired", "partial_paused"])
def test_confirmed_prefix_is_preserved_and_remaining_are_explicitly_cancelled(tmp_path, reason):
    s = setup(tmp_path)
    complete(s, 0)
    prefix = s.watch.get_segment(s.segments[0].child_id)
    parent = s.watch.cancel_remaining_segments(s.parent.reservation_id, reason=reason, now=at(5))
    assert parent.status == "partial" and parent.reason == reason
    assert s.watch.get_event(s.event.event_id).state == "partial"
    assert s.watch.get_segment(prefix.child_id) == prefix
    assert [c.status for c in s.watch.segments(parent.reservation_id)] == ["sent", "cancelled", "cancelled"]
    assert choices.get_qq_delivery(memory_db=s.memory, group_id=GROUP, decision_id=s.decision.decision_id) is None
    with pytest.raises(WatchLedgerError):
        s.watch.start_segment_attempt(s.segments[1].child_id, now=at(6))


def test_unknown_click_blocks_next_child_and_all_new_logical_replies(tmp_path):
    s = setup(tmp_path)
    click(s, 0)
    s.watch.mark_segment_uncertain(s.segments[0].child_id, reason="synthetic_missing_bubble", now=at(4))
    assert s.watch.has_uncertain_send()
    assert s.watch.get_segment(s.segments[1].child_id).status == "cancelled"
    with pytest.raises(WatchLedgerError):
        s.watch.start_segment_attempt(s.segments[1].child_id, now=at(5))
    later = s.watch.observe(frame_signature="new-question", items=[s.anchor, s.question,
        VisibleItem(direction="incoming", text="新的合成问题")], observed_at=at(5)).events[0]
    s.watch.bind_event(later.event_id, "fictional-other-source")
    s.watch.attach_decision(later.event_id, "fictional-other-decision", "speak", new_decision=True)
    assert s.watch.reserve_send(later.event_id, "fictional-other-decision", self_member_id=SELF,
        reply_sha256="a"*64, now=at(6)) is None
    value = receipt(s, 0)
    s.watch.reconcile_segment_sent(s.segments[0].child_id, receipt=value, now=at(6))
    assert s.watch.get_reservation(s.parent.reservation_id).status == "partial"
    assert not s.watch.has_uncertain_send()


def test_optional_same_process_readonly_reconciliation_allows_only_original_deadline_continuation(tmp_path):
    s = setup(tmp_path)
    click(s, 0)
    s.watch.mark_segment_uncertain(s.segments[0].child_id, reason="pending_readonly", cancel_remaining=False, now=at(4))
    with pytest.raises(WatchLedgerError):
        s.watch.start_segment_attempt(s.segments[1].child_id, now=at(4))
    s.watch.reconcile_segment_sent(s.segments[0].child_id, receipt=receipt(s, 0), now=at(5))
    complete(s, 1, 6)
    with pytest.raises(WatchLedgerError):
        s.watch.start_segment_attempt(s.segments[2].child_id, now=at(61))
    assert all(c.deadline_at == s.parent.deadline_at for c in s.watch.segments(s.parent.reservation_id))


@pytest.mark.parametrize("bound", [None, "now", NOW.replace(tzinfo=None), at(2), at(100)])
def test_no_missing_or_fake_local_ack_time(tmp_path, bound):
    s = setup(tmp_path)
    child = s.segments[0].child_id
    s.watch.start_segment_attempt(child, now=at(3))
    s.watch.mark_segment_click_started(child, proof=proof(s, 0, 3), now=at(3))
    with pytest.raises(WatchLedgerError):
        s.watch.mark_segment_click_acknowledged(child, acknowledged_at=bound, now=at(4))
    assert s.watch.get_segment(child).click_acknowledged_at is None
    with pytest.raises(WatchLedgerError):
        s.watch.reconcile_segment_sent(child, receipt=receipt(s, 0), now=at(4))


@pytest.mark.parametrize("field,value", [
    ("decision_id", "wrong-decision"), ("segment_index", 1), ("segment_count", 2),
    ("group_id", "10002"), ("self_member_id", "30002"), ("delivered_text", "错误合成正文"),
    ("source_record_ids", []), ("evidence_refs", ["test://unrelated"]), ("delivered_at", at(2)),
])
def test_segment_receipt_cannot_rebind_scope_text_proof_or_time(tmp_path, field, value):
    s = setup(tmp_path)
    click(s, 0)
    value = receipt(s, 0).model_copy(update={field: value})
    with pytest.raises(WatchLedgerError):
        s.watch.reconcile_segment_sent(s.segments[0].child_id, receipt=value, now=at(4))
    assert s.watch.get_segment(s.segments[0].child_id).status == "click_started"


def test_segment_receipt_stored_before_process_loss_is_linked_without_click_or_time_refresh(tmp_path):
    s = setup(tmp_path)
    click(s, 0)
    value = receipt(s, 0)
    original = s.watch.get_segment(s.segments[0].child_id)
    restarted = WatchLedger(s.watch.path, group_id=GROUP)
    restarted.start_run(now=at(5))
    assert restarted.get_segment(original.child_id).status == "uncertain"
    assert restarted.get_segment(s.segments[1].child_id).status == "cancelled"
    linked = restarted.reconcile_segment_sent(original.child_id, receipt=value, now=at(6))
    assert linked.status == "sent" and linked.click_acknowledged_at == original.click_acknowledged_at
    assert restarted.get_reservation(s.parent.reservation_id).status == "partial"
    assert restarted.reconcile_segment_sent(original.child_id, receipt=value, now=at(40)) == linked
    assert restarted.next_allowed_at() == at(63.2)
    with pytest.raises(WatchLedgerError):
        restarted.start_segment_attempt(s.segments[1].child_id, now=at(7))
    with pytest.raises(WatchLedgerError):
        s.watch.start_segment_attempt(s.segments[1].child_id, now=at(7))


def test_restart_keeps_confirmed_prefix_partial_and_aggregate_can_recover_when_all_sent(tmp_path):
    s = setup(tmp_path, ["合成唯一段"])
    complete(s, 0)
    final = aggregate(s)
    restart = WatchLedger(s.watch.path, group_id=GROUP)
    restart.start_run(now=at(10))
    assert restart.get_reservation(s.parent.reservation_id).status == "uncertain"
    done = restart.finish_segmented_send(s.parent.reservation_id, aggregate_receipt=final, now=at(11))
    assert done.status == "sent" and restart.next_allowed_at() == at(63.2)


@pytest.mark.parametrize("field,value", [("segment_receipt_ids", []), ("source_record_ids", []),
                                        ("evidence_refs", ["test://wrong"]), ("group_id", "10002")])
def test_aggregate_cannot_mark_partial_or_mismatched_plan_complete(tmp_path, field, value):
    s = setup(tmp_path, ["合成段一", "合成段二"])
    complete(s, 0)
    # A structurally plausible aggregate cannot bypass the missing child.
    forged = choices.QQDeliveryReceipt(receipt_id="fictional-aggregate", group_id=GROUP, self_member_id=SELF,
        decision_id=s.decision.decision_id, delivered_text=s.decision.reply, evidence_refs=["test://wrong"],
        verification="qq_ui_verified", source_record_ids=[s.source.record_id], delivered_at=at(9), origin="decision")
    with pytest.raises(WatchLedgerError):
        s.watch.finish_segmented_send(s.parent.reservation_id, aggregate_receipt=forged, now=at(9))
    complete(s, 1, 5)
    final = aggregate(s).model_copy(update={field: value})
    with pytest.raises(WatchLedgerError):
        s.watch.finish_segmented_send(s.parent.reservation_id, aggregate_receipt=final, now=at(9))
    assert s.watch.get_reservation(s.parent.reservation_id).status != "sent"
