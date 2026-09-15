"""Bounded GUI segment coordination; never reconstruct an unobserved receipt.

One frozen decision owns all child sends. This mixin shares the watcher's sole
GUI lane and original deadline; restart recovery only joins durable receipts.
"""
from __future__ import annotations

from contextlib import closing
import sqlite3

from harness.embodiment.providers.qq import QQCommunicationError
from harness.qq.accessibility import chat_change_signature
from harness.qq.decision import (
    get_qq_delivery,
    get_qq_segment_deliveries,
    record_qq_delivery,
    record_qq_segment_delivery,
)
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired
from harness.qq.watch_ledger import address_sources_for_event


def _runtime():
    # Keep the coordinator's injected wall/monotonic clocks authoritative.
    from harness.qq import watcher
    return watcher


class SegmentedDeliveryMixin:
    def _refresh_segment_delivery_status(self):
        """Durable group totals distinguish real bubbles from complete replies."""
        candidates = []
        with closing(sqlite3.connect(self.ledger.path.as_uri() + "?mode=ro", uri=True)) as db:
            segments = db.execute("""SELECT COUNT(*) FROM qq_watch_send_segments
                WHERE group_id=? AND status='sent'""", (self.settings.group_id,)).fetchone()[0]
            partial = db.execute("""SELECT COUNT(*) FROM qq_watch_sends
                WHERE group_id=? AND status='partial'""", (self.settings.group_id,)).fetchone()[0]
            db.execute("ATTACH DATABASE ? AS receipts", (self.session.memory_db.as_uri() + "?mode=ro",))
            for ledger_table, receipt_table, segmented in (
                ("qq_watch_sends", "qq_reply_deliveries", False),
                ("qq_watch_send_segments", "qq_reply_segment_deliveries", True),
            ):
                if db.execute("SELECT 1 FROM receipts.sqlite_master WHERE type='table' AND name=?",
                              (receipt_table,)).fetchone() is None:
                    continue
                # Table names are fixed above. Cancellation/recovery updated_at
                # values never establish when a real QQ bubble was delivered.
                row = db.execute(f"""SELECT r.receipt_id,r.decision_id FROM {ledger_table} AS s
                    JOIN receipts.{receipt_table} AS r ON r.group_id=s.group_id
                      AND r.decision_id=json_extract(s.record_json,'$.decision_id')
                      AND r.receipt_id=json_extract(s.record_json,'$.receipt_id')
                    WHERE s.group_id=? AND s.status='sent'
                      AND json_extract(r.record_json,'$.self_member_id')=?
                    ORDER BY julianday(json_extract(r.record_json,'$.delivered_at')) DESC LIMIT 1""",
                    (self.settings.group_id, self.settings.self_member_id)).fetchone()
                if row is not None:
                    candidates.append((*row, segmented))
        delivered = []
        for receipt_id, decision_id, segmented in candidates:
            options = dict(memory_db=self.session.memory_db, group_id=self.settings.group_id, decision_id=decision_id)
            receipts = get_qq_segment_deliveries(**options) if segmented else [get_qq_delivery(**options)]
            delivered.extend(item.delivered_at for item in receipts
                             if item is not None and item.receipt_id == receipt_id
                             and item.self_member_id == self.settings.self_member_id)
        self.set_status(verified_segments_sent=segments, partial_deliveries=partial,
                        last_delivery_at=max(delivered).isoformat() if delivered else None)

    def _segment_receipts(self, turn):
        return get_qq_segment_deliveries(memory_db=self.session.memory_db,
            group_id=self.settings.group_id, decision_id=turn.decision.decision_id)

    def _retain_segment_timing(self, turn, segment, *, result=None, acknowledged_at=None):
        runtime = _runtime()
        current = self.ledger.segments(turn.reservation_id)[segment.segment_index]
        if current.click_acknowledged_at is not None or current.click_observed_at is not None:
            return
        acknowledged_at = acknowledged_at or getattr(result, "click_acknowledged_at", None)
        observed_at = getattr(result, "click_observed_at", None)
        if acknowledged_at is not None:
            self.ledger.mark_segment_click_acknowledged(segment.child_id,
                acknowledged_at=acknowledged_at, now=runtime.now())
            turn.trace.record(f"segment_{segment.segment_index}_click_acknowledged")
        elif observed_at is not None:
            self.ledger.mark_segment_click_observed(segment.child_id, observed_at=observed_at,
                evidence_ref=getattr(result, "click_observed_evidence_ref", None), now=runtime.now())
            turn.trace.record(f"segment_{segment.segment_index}_click_observed")
        else:
            raise RuntimeError("segment_click_timing_unavailable")

    def _persist_segment(self, turn, segment, result):
        if self._qq_was_persisted(turn, segment.segment_index):
            return next(item for item in self._segment_receipts(turn) if item.segment_index == segment.segment_index)
        self._retain_segment_timing(turn, segment, result=result)
        with turn.trace.stage(f"segment_{segment.segment_index}_receipt_storage"):
            receipt = record_qq_segment_delivery(memory_db=self.session.memory_db,
                group_id=self.settings.group_id, self_member_id=self.settings.self_member_id,
                decision_id=turn.decision.decision_id, segment_index=segment.segment_index,
                segment_count=segment.segment_count, delivered_text=segment.text,
                evidence_refs=result.evidence_refs, source_record_ids=address_sources_for_event(turn.event),
                delivered_at=result.delivered_at, verification="qq_ui_verified")
            self.ledger.reconcile_segment_sent(segment.child_id, receipt=receipt, now=_runtime().now())
        turn.trace.record(f"segment_{segment.segment_index}_receipt_persisted")
        turn.trace.add_counter("segments_sent")
        self._refresh_segment_delivery_status()
        self.set_status(last_delivery_at=receipt.delivered_at.isoformat())
        turn.active_segment_text = None
        self._homeostatic_feedback(fact_id=receipt.receipt_id, kind="send_attempt",
            occurred_at=receipt.delivered_at, evidence_refs=receipt.evidence_refs)
        return receipt

    def _finish_segmented_delivery(self, turn):
        receipts = self._segment_receipts(turn)
        with turn.trace.stage("receipt_storage"):
            receipt = record_qq_delivery(memory_db=self.session.memory_db, group_id=self.settings.group_id,
                self_member_id=self.settings.self_member_id, decision_id=turn.decision.decision_id,
                delivered_text="\n\n".join(turn.segment_texts),
                evidence_refs=sorted({ref for item in receipts for ref in item.evidence_refs}),
                source_record_ids=sorted({ref for item in receipts for ref in item.source_record_ids}),
                delivered_at=max(item.delivered_at for item in receipts), verification="qq_ui_verified")
            self.ledger.finish_segmented_send(turn.reservation_id, aggregate_receipt=receipt, now=_runtime().now())
        turn.trace.record("receipt_persisted")
        self.set_status(last_delivery_at=receipt.delivered_at.isoformat(), deliveries=self._delivery_count(turn),
            state="watching", automatic_send=self.settings.automatic_send, last_error_type=None)
        self._finish_trace(turn.trace, "sent")
        self._pending_turn = None
        self._replans = 0

    def _terminate_segments(self, turn, reason, *, uncertain=False):
        """Keep the sent prefix and never classify partial delivery as excluded."""
        runtime = _runtime()
        segments = self.ledger.segments(turn.reservation_id)
        prefix = sum(item.status == "sent" for item in segments)
        reason = ("partial_" + reason) if prefix and not reason.startswith("partial_") else reason
        self.ledger.cancel_remaining_segments(turn.reservation_id, reason=reason, now=runtime.now())
        self._refresh_segment_delivery_status()
        if prefix:
            turn.trace.add_counter(reason)
        cleaned = True if uncertain else self._cleanup_owned_draft(turn)
        if uncertain:
            self.set_status(state="delivery_uncertain", automatic_send=False, last_error_type=reason)
            try:
                runtime.atomic_json(self.settings.control_file, {"paused": True, "reason": "delivery_uncertain"})
            except Exception:
                self._heartbeat_failed = True
        elif cleaned:
            self.set_status(state="watching", last_error_type=reason)
        self._finish_trace(turn.trace, "failed")
        self._pending_turn = None
        self._carry_first_seen = None
        self._replans = 0
        return cleaned

    def _advance_segment(self, turn, result, text):
        # Desktop verifies only the exact new own bubble, scope, epoch, fresh
        # paired AX and a uniquely retained suffix before returning this frame.
        frame, verification = self.desktop.advance_segment_context(turn.frame, result, text)
        signature = chat_change_signature(frame.a11y_tree)
        if not signature.chat_visible or not signature.chat_digest:
            raise RuntimeError("segment_context_signature_unavailable")
        turn.confirmed_context_digests.add(signature.chat_digest)
        turn.frame, turn.verification = frame, verification
        turn.context_digest = signature.chat_digest

    def _reconcile_segment(self, turn, segment):
        runtime = _runtime()
        current = self.ledger.segments(turn.reservation_id)[segment.segment_index]
        if current.proof is None or not hasattr(self.desktop, "reconcile_delivery"):
            return None
        # A late read may establish what already happened. It never renews the
        # original deadline checked before the next child click.
        deadline = QQDeadline.after(30, clock=turn.deadline.clock)
        for pause in (0, 1, 3):
            stop_at = turn.deadline.clock() + pause
            while turn.deadline.clock() < stop_at:
                self.check_control()
                deadline.check()
                runtime.time.sleep(min(.2, stop_at - turn.deadline.clock()))
            self.check_control()
            deadline.check()
            with self._desktop_turn(deadline, turn.trace), turn.trace.stage(f"segment_{segment.segment_index}_reconciliation"):
                result = self._qq_action(turn, segment=segment, reconcile=True,
                    persist=lambda result: self._persist_segment(turn, segment, result),
                    execute=lambda: self.desktop.reconcile_delivery(segment.text, reservation_id=segment.child_id,
                    proof=current.proof.model_dump(mode="json")))
            if result.status == "sent":
                return result
            self.ledger.mark_segment_uncertain(segment.child_id, reason=result.reason_code,
                evidence_refs=getattr(result, "evidence_refs", []), cancel_remaining=False, now=runtime.now())
        return None

    def _attempt_segmented_delivery(self, turn):
        runtime = _runtime()
        if not self.ledger.segments(turn.reservation_id):
            self.ledger.initialize_segments(turn.reservation_id, texts=list(turn.segment_texts))
        while self._pending_turn is turn:
            segments = self.ledger.segments(turn.reservation_id)
            if all(item.status == "sent" for item in segments):
                self._finish_segmented_delivery(turn)
                return
            segment = next(item for item in segments if item.status != "sent")
            clicked = False
            result = None
            turn.active_segment_text = segment.text
            try:
                self.check_control()
                turn.deadline.check()
                if self._newer_visible_context():
                    self._terminate_segments(turn, "superseded")
                    return
                attempt = self.ledger.start_segment_attempt(segment.child_id, now=runtime.now())
                if attempt is None:
                    self._terminate_segments(turn, "precheck_failed")
                    return
                if getattr(self.desktop, "supports_click_acknowledgement", False) is not True:
                    self._terminate_segments(turn, "click_timing_unsupported")
                    return

                def before_click():
                    nonlocal clicked
                    self.check_control()
                    turn.deadline.check()
                    self._check_homeostatic_turn(turn)
                    version = (getattr(self.desktop, "_identity_epoch", None), self._identity_cache_gap_generation)
                    newer = self._newer_visible_context()
                    if self.settings.identity_cache_enabled and version != (
                            getattr(self.desktop, "_identity_epoch", None), self._identity_cache_gap_generation):
                        from harness.qq.desktop import PreClickIdentityInvalidated
                        raise PreClickIdentityInvalidated()
                    if newer:
                        raise RuntimeError("newer_context_before_click")
                    proof = getattr(self.desktop, "last_preclick_proof", None)
                    if proof is None:
                        raise RuntimeError("preclick_proof_missing")
                    self.ledger.mark_segment_click_started(segment.child_id,
                        proof=dict(proof) | {"source_record_ids": address_sources_for_event(turn.event)}, now=runtime.now())
                    clicked = True
                    turn.trace.record(f"segment_{segment.segment_index}_send_click_started")

                def after_click(acknowledged_at):
                    self._retain_segment_timing(turn, segment, acknowledged_at=acknowledged_at)

                self.set_status(state="sending")
                with self._desktop_turn(turn.deadline, turn.trace), turn.trace.stage(f"segment_{segment.segment_index}_delivery"):
                    result = self._qq_action(turn, segment=segment,
                        persist=lambda result: self._persist_segment(turn, segment, result),
                        execute=lambda: self.desktop.deliver(segment.text, reservation_id=segment.child_id,
                        expected_content_digest=turn.frame.content_digest, expected_context_frame=turn.frame,
                        # Every child starts with current group evidence, even
                        # its first attempt. The original context/deadline remain.
                        group_verification=None, before_click=before_click, after_click=after_click))
                if clicked and result.status != "sent":
                    self.ledger.mark_segment_uncertain(segment.child_id, reason=result.reason_code,
                        evidence_refs=getattr(result, "evidence_refs", []), cancel_remaining=False, now=runtime.now())
                    self.set_status(state="reconciling_delivery", automatic_send=False, last_error_type=result.reason_code)
                    result = self._reconcile_segment(turn, segment)
                if clicked and result is not None and result.status == "sent":
                    self._persist_segment(turn, segment, result)
                    if segment.segment_index + 1 == segment.segment_count:
                        self._finish_segmented_delivery(turn)
                        return
                    try:
                        self._advance_segment(turn, result, segment.text)
                    except Exception:
                        self._terminate_segments(turn, "superseded")
                        return
                    turn.retry_at = None
                    continue
                if clicked:
                    self._terminate_segments(turn, "delivery_uncertain", uncertain=True)
                    return
                if self.should_stop():
                    self._terminate_segments(turn, "paused")
                    return
                if self._newer_visible_context():
                    self._terminate_segments(turn, "superseded")
                    return
                retryable = (result.status == "not_sent" and getattr(result, "phase", "precheck") == "precheck"
                             and getattr(result, "retryable", False) and not turn.deadline.expired)
                reason = "turn_deadline_expired" if turn.deadline.expired else result.reason_code
                current = self.ledger.defer_segment_preclick(segment.child_id, reason=reason,
                    retryable=retryable, evidence_refs=getattr(result, "evidence_refs", []), now=runtime.now())
                if current.status == "retry_wait":
                    turn.retry_at = current.next_retry_at
                    turn.trace.add_counter("precheck_retries")
                    turn.trace.add_counter(f"segment_{segment.segment_index}_precheck_retries")
                    turn.trace.flush(self._trace_file)
                    self.set_status(state="retrying_precheck", last_error_type=reason)
                    return
                self._terminate_segments(turn, "expired" if turn.deadline.expired else "precheck_failed")
                return
            except Exception as exc:
                if clicked:
                    # If a storage acknowledgement was lost, joining an exact
                    # already durable receipt never authorizes another click.
                    durable = None
                    try:
                        durable = next((item for item in self._segment_receipts(turn)
                                        if item.segment_index == segment.segment_index), None)
                        if durable is not None:
                            self.ledger.reconcile_segment_sent(segment.child_id, receipt=durable, now=runtime.now())
                            if segment.segment_index + 1 == segment.segment_count:
                                self._finish_segmented_delivery(turn)
                                return
                            self._terminate_segments(turn, "storage_interrupted")
                            return
                    except Exception:
                        pass
                    current = self.ledger.segments(turn.reservation_id)[segment.segment_index]
                    # A confirmed receipt never becomes an unknown click merely
                    # because aggregate storage/telemetry failed afterwards.
                    if current.status != "sent":
                        self.ledger.mark_segment_uncertain(segment.child_id, reason=type(exc).__name__, now=runtime.now())
                    self._terminate_segments(turn, "delivery_uncertain", uncertain=True)
                else:
                    reason = ("paused" if isinstance(exc, InterruptedError) else
                              "expired" if isinstance(exc, QQDeadlineExpired) else
                              "superseded" if isinstance(exc, QQCommunicationError) and exc.code == "chat_context_changed" else
                              exc.code if isinstance(exc, QQCommunicationError) else "precheck_failed")
                    self._terminate_segments(turn, reason)
                if isinstance(exc, InterruptedError):
                    raise
                return

    def _recover_segment_receipts(self, reservation):
        """Startup may join persisted evidence, never send remaining text."""
        runtime = _runtime()
        segments = self.ledger.segments(reservation.reservation_id)
        receipts = get_qq_segment_deliveries(memory_db=self.session.memory_db,
            group_id=self.settings.group_id, decision_id=reservation.decision_id)
        for receipt in receipts:
            self.ledger.reconcile_segment_sent(segments[receipt.segment_index].child_id,
                receipt=receipt, now=runtime.now())
        if len(receipts) == reservation.segment_count:
            aggregate = get_qq_delivery(memory_db=self.session.memory_db, group_id=self.settings.group_id,
                                       decision_id=reservation.decision_id)
            if aggregate is None:
                aggregate = record_qq_delivery(memory_db=self.session.memory_db,
                    group_id=self.settings.group_id, self_member_id=reservation.self_member_id,
                    decision_id=reservation.decision_id, delivered_text="\n\n".join(item.text for item in segments),
                    evidence_refs=sorted({ref for item in receipts for ref in item.evidence_refs}),
                    source_record_ids=sorted({ref for item in receipts for ref in item.source_record_ids}),
                    delivered_at=max(item.delivered_at for item in receipts), verification="qq_ui_verified")
            self.ledger.finish_segmented_send(reservation.reservation_id, aggregate_receipt=aggregate, now=runtime.now())
        else:
            self.ledger.cancel_remaining_segments(reservation.reservation_id,
                reason="process_restarted_no_segment_replay", now=runtime.now())
        self._refresh_segment_delivery_status()
