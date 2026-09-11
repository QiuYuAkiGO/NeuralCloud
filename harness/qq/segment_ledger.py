"""Child delivery state transitions for WatchLedger's frozen reply plans.

No GUI or model code. The watcher must revalidate current context before each
click; only the first child inherits the original stream revision/cooldown.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
from uuid import uuid4


def _api():
    # Deferred to avoid a cycle while watch_ledger defines the public DTOs.
    from harness.qq import watch_ledger
    return watch_ledger


def _references(value, maximum=32):
    api = _api()
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise api.WatchLedgerError("segment evidence limit exceeded")
    return sorted(set(api._identifier(ref) for ref in value))


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


class SegmentLedgerMixin:
    @staticmethod
    def _save_segment(connection, segment):
        # Revalidate model_copy updates before persisting any transition.
        segment = _api().SendSegment.model_validate(segment.model_dump())
        connection.execute("UPDATE qq_watch_send_segments SET status=?,record_json=? WHERE child_id=?",
                           (segment.status, segment.model_dump_json(), segment.child_id))
        return segment

    def _segment(self, connection, child_id):
        api = _api()
        row = connection.execute("SELECT * FROM qq_watch_send_segments WHERE child_id=? AND group_id=?",
                                 (api._identifier(child_id), self.group_id)).fetchone()
        if row is None:
            raise api.WatchLedgerError("unknown reply segment in this group")
        segment = api.SendSegment.model_validate_json(row["record_json"])
        if (segment.child_id != row["child_id"] or segment.parent_id != row["parent_id"]
                or segment.group_id != row["group_id"] or segment.segment_index != row["segment_index"]
                or segment.status != row["status"]):
            raise api.WatchLedgerError("segment record disagrees with its stored scope")
        return segment

    def _segments(self, connection, parent):
        api = _api()
        rows = connection.execute("SELECT child_id FROM qq_watch_send_segments WHERE parent_id=? ORDER BY segment_index",
                                  (parent.reservation_id,)).fetchall()
        segments = [self._segment(connection, row[0]) for row in rows]
        if not parent.segment_count and not segments:
            return []
        if (len(segments) != parent.segment_count or not 1 <= len(segments) <= 3
                or [s.segment_index for s in segments] != list(range(len(segments)))
                or any(s.parent_id != parent.reservation_id or s.decision_id != parent.decision_id
                       or s.event_id != parent.event_id or s.run_id != parent.run_id
                       or s.segment_count != parent.segment_count or s.deadline_at != parent.deadline_at
                       or s.plan_sha256 != parent.reply_sha256
                       or s.child_id != "qq-watch-segment:" + hashlib.sha256(
                           f"{parent.reservation_id}:{s.segment_index}".encode()).hexdigest() for s in segments)
                or hashlib.sha256("\n\n".join(s.text for s in segments).encode()).hexdigest() != parent.reply_sha256):
            raise api.WatchLedgerError("the stored segment plan disagrees with its frozen parent")
        return segments

    def initialize_segments(self, parent_id, texts: list[str]):
        api = _api()
        if (not isinstance(texts, list) or not 1 <= len(texts) <= 3
                or any(not isinstance(t, str) or not t.strip() or len(t) > 4000
                       or any(char in t for char in "\r\n\x85\u2028\u2029") for t in texts)
                or len("\n\n".join(texts)) > 8000):
            raise api.WatchLedgerError("a reply requires one to three bounded single-line segments")
        plan_hash = hashlib.sha256("\n\n".join(texts).encode()).hexdigest()
        with self._transaction() as (connection, _state):
            parent = self._reservation(connection, parent_id)
            if plan_hash != parent.reply_sha256:
                raise api.WatchLedgerError("segments do not match the reserved original reply")
            existing = self._segments(connection, parent)
            if existing:
                if [s.text for s in existing] != texts:
                    raise api.WatchLedgerError("a frozen segment plan cannot be replaced")
                return existing
            if (parent.run_id != self.run_id or parent.status != "reserved" or parent.attempt_count
                    or parent.click_started_at is not None or parent.preclick_proof is not None
                    or parent.deadline_at is None or parent.self_member_id is None):
                raise api.WatchLedgerError("only an untouched current reservation can freeze segments")
            created = []
            for index, text in enumerate(texts):
                segment = api.SendSegment(
                    child_id="qq-watch-segment:" + hashlib.sha256(f"{parent_id}:{index}".encode()).hexdigest(),
                    parent_id=parent_id, group_id=self.group_id, run_id=parent.run_id,
                    event_id=parent.event_id, decision_id=parent.decision_id,
                    segment_index=index, segment_count=len(texts), text=text,
                    reply_sha256=hashlib.sha256(text.encode()).hexdigest(), plan_sha256=plan_hash,
                    created_at=parent.created_at, updated_at=parent.created_at, deadline_at=parent.deadline_at,
                )
                connection.execute("INSERT INTO qq_watch_send_segments VALUES(?,?,?,?,?,?)",
                    (segment.child_id, parent_id, self.group_id, index, segment.status, segment.model_dump_json()))
                created.append(segment)
            self._save_send(connection, parent.model_copy(update={"segment_count": len(texts)}))
            return created

    def segments(self, parent_id):
        with closing(self._connect()) as connection:
            return self._segments(connection, self._reservation(connection, parent_id))

    def get_segment(self, child_id):
        with closing(self._connect()) as connection:
            segment = self._segment(connection, child_id)
            self._segments(connection, self._reservation(connection, segment.parent_id))
            return segment

    def segment_attempts(self, child_id):
        with closing(self._connect()) as connection:
            self._segment(connection, child_id)
            return [_api().DeliveryAttempt.model_validate_json(row[0]) for row in connection.execute(
                "SELECT record_json FROM qq_watch_delivery_attempts WHERE reservation_id=? ORDER BY attempt_number",
                (child_id,))]

    def _segment_ready(self, connection, state, segment, moment):
        api = _api()
        parent = self._reservation(connection, segment.parent_id)
        siblings = self._segments(connection, parent)
        if (parent.run_id != self.run_id or segment.run_id != self.run_id
                or parent.status not in {"reserved", "retry_wait", "click_started"}
                or segment.status not in {"reserved", "retry_wait"} or segment.click_started_at is not None):
            raise api.WatchLedgerError("only an unclicked current-run next segment may proceed")
        if (moment >= segment.deadline_at or moment < parent.created_at - timedelta(seconds=2)
                or segment.next_retry_at is not None and moment < segment.next_retry_at):
            raise api.WatchLedgerError("segment deadline expired or retry is not due")
        if (any(s.status != "sent" or not s.receipt_id for s in siblings[:segment.segment_index])
                or any(s.status in {"click_started", "uncertain", "sent"} for s in siblings[segment.segment_index + 1:])):
            raise api.WatchLedgerError("only the next segment after a confirmed contiguous prefix may proceed")
        if connection.execute("SELECT 1 FROM qq_watch_sends WHERE reservation_id!=? AND status IN ('reserved','retry_wait','click_started','uncertain') LIMIT 1",
                              (parent.reservation_id,)).fetchone():
            raise api.WatchLedgerError("another unresolved send blocks this segment")
        event = self._event(connection, parent.event_id)
        if event.source_record_id is None:
            raise api.WatchLedgerError("the original source must remain bound")
        if segment.segment_index == 0:
            if event.stream_revision != state["revision"]:
                raise api.WatchLedgerError("the visible stream changed before the first segment")
            allowed_at = self._allowed_at(state, parent.cooldown_seconds)
            if allowed_at is not None and moment < allowed_at:
                raise api.WatchLedgerError("the logical reply cooldown is still active")
        return parent, event, siblings

    def start_segment_attempt(self, child_id, *, now=None):
        api = _api()
        moment = api._now(now)
        with self._transaction() as (connection, state):
            segment = self._segment(connection, child_id)
            self._segment_ready(connection, state, segment, moment)
            if segment.attempt_count >= 3:
                raise api.WatchLedgerError("at most two pre-click retries are permitted per segment")
            if connection.execute("SELECT 1 FROM qq_watch_delivery_attempts WHERE reservation_id=? AND phase IN ('prechecking','click_started','uncertain')",
                                  (child_id,)).fetchone():
                raise api.WatchLedgerError("a segment attempt is already active")
            attempt = api.DeliveryAttempt(attempt_id=f"qq-watch-attempt:{uuid4().hex}", reservation_id=child_id,
                attempt_number=segment.attempt_count + 1, phase="prechecking", started_at=moment, updated_at=moment)
            connection.execute("INSERT INTO qq_watch_delivery_attempts VALUES(?,?,?,?,?)",
                (attempt.attempt_id, child_id, attempt.attempt_number, attempt.phase, attempt.model_dump_json()))
            return self._save_segment(connection, segment.model_copy(update={
                "status": "reserved", "attempt_count": attempt.attempt_number, "updated_at": moment, "next_retry_at": None}))

    def mark_segment_click_started(self, child_id, *, proof, evidence_refs=None, now=None):
        api = _api()
        moment, refs = api._now(now), _references(evidence_refs)
        proof = api.PreClickProof.model_validate(proof)
        _references(proof.evidence_refs, 64)
        with self._transaction() as (connection, state):
            segment = self._segment(connection, child_id)
            parent, event, siblings = self._segment_ready(connection, state, segment, moment)
            if (proof.reservation_id != child_id or proof.group_id != self.group_id
                    or proof.self_member_id != parent.self_member_id or proof.reply_sha256 != segment.reply_sha256
                    or not -2 <= (moment - proof.verified_at).total_seconds() <= 15
                    or proof.source_record_ids != [event.source_record_id]
                    or not proof.window_id or not proof.body_instance_id or proof.environment_revision is None
                    or proof.title_pixels_sha256 is None or proof.before_outgoing_count is None
                    or proof.frame_evidence_ref not in proof.evidence_refs):
                raise api.WatchLedgerError("segment proof must bind its exact source, text, window and sender")
            if segment.segment_index:
                first = siblings[0].proof
                if first is None or any(getattr(first, key) != getattr(proof, key)
                                        for key in ("window_id", "body_instance_id", "environment_revision")):
                    raise api.WatchLedgerError("the verified GUI scope changed between segments")
            row = connection.execute("SELECT record_json FROM qq_watch_delivery_attempts WHERE reservation_id=? AND phase='prechecking'",
                                     (child_id,)).fetchone()
            if row is None:
                raise api.WatchLedgerError("start_segment_attempt must finish before the click boundary")
            attempt = api.DeliveryAttempt.model_validate_json(row[0]).model_copy(update={
                "phase": "click_started", "proof": proof, "updated_at": moment, "evidence_refs": refs})
            connection.execute("UPDATE qq_watch_delivery_attempts SET phase=?,record_json=? WHERE attempt_id=?",
                               (attempt.phase, attempt.model_dump_json(), attempt.attempt_id))
            segment = self._save_segment(connection, segment.model_copy(update={
                "status": "click_started", "proof": proof, "click_started_at": moment, "updated_at": moment,
                "evidence_refs": refs, "next_retry_at": None}))
            parent = parent.model_copy(update={"click_started_at": parent.click_started_at or moment,
                                              "click_timing_required": True})
            self._set_send(connection, parent, "click_started", moment)
            return segment

    def _mark_segment_bound(self, child_id, *, bound_at, observed, evidence_ref=None, evidence_refs=None, now=None):
        api = _api()
        if not isinstance(bound_at, datetime):
            raise api.WatchLedgerError("an explicit local segment click-bound timestamp is required")
        moment, bound, refs = api._now(now), api._now(bound_at), _references(evidence_refs)
        if observed:
            evidence_ref = api._identifier(evidence_ref)
        with self._transaction() as (connection, state):
            segment = self._segment(connection, child_id)
            parent = self._reservation(connection, segment.parent_id)
            if (segment.click_started_at is None or bound < segment.click_started_at
                    or bound > moment + timedelta(seconds=2)):
                raise api.WatchLedgerError("the local segment click upper bound is invalid")
            if segment.click_acknowledged_at is not None or segment.click_observed_at is not None:
                return segment
            if segment.run_id != self.run_id or segment.status not in {"click_started", "uncertain"}:
                raise api.WatchLedgerError("only this run can acquire a segment click upper bound")
            updates = ({"click_observed_at": bound, "click_observed_evidence_ref": evidence_ref} if observed
                       else {"click_acknowledged_at": bound})
            updates["postclick_evidence_refs"] = _references(list(set(segment.postclick_evidence_refs) | set(refs)), 128)
            segment = self._save_segment(connection, segment.model_copy(update=updates))
            # This is the actual activity time of the latest physical segment.
            # Receipts and repeat calls never extend it.
            self._record_actual_activity(connection, state, bound, parent.cooldown_seconds)
            self._save_send(connection, parent.model_copy(update={
                "click_acknowledged_at": None if observed else bound,
                "click_observed_at": bound if observed else None,
                "click_observed_evidence_ref": evidence_ref if observed else None}))
            return segment

    def mark_segment_click_acknowledged(self, child_id, *, acknowledged_at, evidence_refs=None, now=None):
        return self._mark_segment_bound(child_id, bound_at=acknowledged_at, observed=False,
                                        evidence_refs=evidence_refs, now=now)

    def mark_segment_click_observed(self, child_id, *, observed_at, evidence_ref, evidence_refs=None, now=None):
        return self._mark_segment_bound(child_id, bound_at=observed_at, observed=True, evidence_ref=evidence_ref,
                                        evidence_refs=evidence_refs, now=now)

    def _cancel_segments(self, connection, parent, moment, reason, refs):
        api = _api()
        if parent.status == "sent":
            raise api.WatchLedgerError("a completed logical reply cannot be cancelled")
        for segment in self._segments(connection, parent):
            if segment.status in {"reserved", "retry_wait"}:
                evidence = _references(list(set(segment.precheck_evidence_refs) | set(refs)), 96)
                self._save_segment(connection, segment.model_copy(update={
                    "status": "cancelled", "reason": reason, "updated_at": moment, "next_retry_at": None,
                    "precheck_evidence_refs": evidence}))
                self._finish_attempt(connection, segment.child_id, "cancelled", moment, reason, evidence_refs=refs)
        segments = self._segments(connection, parent)
        if any(s.status in {"click_started", "uncertain"} for s in segments):
            status = "uncertain"
        elif any(s.status == "sent" for s in segments):
            status = "partial"
        else:
            status = "expired" if "expired" in reason else "superseded" if "supersed" in reason else "not_sent"
        return self._set_send(connection, parent, status, moment, reason)

    def cancel_remaining_segments(self, parent_id, *, reason="partial_superseded", evidence_refs=None, now=None):
        api = _api()
        moment, refs, reason = api._now(now), _references(evidence_refs), api._identifier(reason)
        with self._transaction() as (connection, _state):
            parent = self._reservation(connection, parent_id)
            if not parent.segment_count:
                raise api.WatchLedgerError("the reservation has no segment plan")
            return self._cancel_segments(connection, parent, moment, reason, refs)

    def defer_segment_preclick(self, child_id, *, reason, retryable, evidence_refs=None, now=None):
        api = _api()
        moment, refs, reason = api._now(now), _references(evidence_refs), api._identifier(reason)
        if type(retryable) is not bool:
            raise api.WatchLedgerError("segment retry policy must be explicit")
        with self._transaction() as (connection, state):
            segment = self._segment(connection, child_id)
            parent = self._reservation(connection, segment.parent_id)
            if (segment.run_id != self.run_id or segment.status not in {"reserved", "retry_wait"}
                    or segment.click_started_at is not None):
                raise api.WatchLedgerError("a possibly clicked segment cannot be retried")
            evidence = _references(list(set(segment.precheck_evidence_refs) | set(refs)), 96)
            segment = self._save_segment(connection, segment.model_copy(update={"precheck_evidence_refs": evidence}))
            self._finish_attempt(connection, child_id, "precheck_failed", moment, reason, evidence_refs=refs)
            event = self._event(connection, parent.event_id)
            stop_reason = ("partial_expired" if moment >= segment.deadline_at else
                           "partial_superseded" if segment.segment_index == 0 and event.stream_revision != state["revision"] else
                           reason if not retryable or segment.attempt_count >= 3 else None)
            if stop_reason is not None:
                self._cancel_segments(connection, parent, moment, stop_reason, refs)
                return self._segment(connection, child_id)
            return self._save_segment(connection, segment.model_copy(update={
                "status": "retry_wait", "updated_at": moment, "reason": reason,
                "next_retry_at": moment + timedelta(seconds=2)}))

    def mark_segment_uncertain(self, child_id, *, reason, evidence_refs=None, cancel_remaining=True, now=None):
        api = _api()
        moment, refs, reason = api._now(now), _references(evidence_refs), api._identifier(reason)
        if type(cancel_remaining) is not bool:
            raise api.WatchLedgerError("segment cancellation policy must be explicit")
        with self._transaction() as (connection, _state):
            segment = self._segment(connection, child_id)
            if (segment.run_id != self.run_id or segment.status not in {"click_started", "uncertain"}
                    or segment.click_started_at is None):
                raise api.WatchLedgerError("only a possibly clicked segment can become uncertain")
            evidence = _references(list(set(segment.postclick_evidence_refs) | set(refs)), 128)
            segment = self._save_segment(connection, segment.model_copy(update={
                "status": "uncertain", "updated_at": moment, "reason": reason, "postclick_evidence_refs": evidence}))
            self._finish_attempt(connection, child_id, "uncertain", moment, reason, evidence_refs=refs)
            parent = self._reservation(connection, segment.parent_id)
            if cancel_remaining:
                self._cancel_segments(connection, parent, moment, reason, [])
            else:
                # Only same-run read-only reconciliation may release this
                # guard; no next-child attempt is allowed while uncertain.
                self._set_send(connection, parent, "uncertain", moment, reason)
            return segment

    def reconcile_segment_sent(self, child_id, *, receipt, now=None):
        from harness.qq.decision import QQSegmentDeliveryReceipt
        api = _api()
        receipt = QQSegmentDeliveryReceipt.model_validate(receipt)
        moment = api._now(now)
        fingerprint = _fingerprint(receipt.model_dump(mode="json"))
        with self._transaction() as (connection, _state):
            segment = self._segment(connection, child_id)
            parent = self._reservation(connection, segment.parent_id)
            siblings = self._segments(connection, parent)
            event = self._event(connection, parent.event_id, current_run=False)
            bound = segment.click_acknowledged_at or segment.click_observed_at
            if (segment.status not in {"click_started", "uncertain", "sent"} or segment.proof is None or bound is None
                    or receipt.group_id != self.group_id or receipt.self_member_id != parent.self_member_id
                    or receipt.decision_id != parent.decision_id or receipt.segment_index != segment.segment_index
                    or receipt.segment_count != segment.segment_count or receipt.delivered_text != segment.text
                    or receipt.source_record_ids != [event.source_record_id]
                    or receipt.delivered_at < bound or receipt.delivered_at > moment + timedelta(seconds=2)
                    or not set(segment.proof.evidence_refs) <= set(receipt.evidence_refs)
                    or segment.click_observed_evidence_ref is not None
                    and segment.click_observed_evidence_ref not in receipt.evidence_refs
                    or any(s.status != "sent" for s in siblings[:segment.segment_index])):
                raise api.WatchLedgerError("segment receipt does not match the original proof, source, time and text")
            if segment.receipt_id is not None:
                if segment.receipt_id != receipt.receipt_id or segment.receipt_fingerprint != fingerprint:
                    raise api.WatchLedgerError("a segment receipt cannot be replaced")
                return segment
            segment = self._save_segment(connection, segment.model_copy(update={
                "status": "sent", "receipt_id": receipt.receipt_id, "receipt_fingerprint": fingerprint,
                "delivered_at": receipt.delivered_at, "updated_at": receipt.delivered_at,
                "evidence_refs": sorted(set(receipt.evidence_refs)), "reason": "matched_verified_segment_receipt"}))
            self._finish_attempt(connection, child_id, "sent", moment, "matched_verified_segment_receipt")
            siblings = self._segments(connection, parent)
            if any(s.status == "cancelled" for s in siblings) and not any(s.status in {"click_started", "uncertain"} for s in siblings):
                self._set_send(connection, parent, "partial", moment, parent.reason or "partial_cancelled")
            elif parent.run_id == self.run_id and parent.status == "uncertain" and not any(
                    s.status in {"click_started", "uncertain"} for s in siblings):
                self._set_send(connection, parent, "click_started", moment, "segment_readonly_reconciled")
            return segment

    def finish_segmented_send(self, parent_id, *, aggregate_receipt, now=None):
        from harness.qq.decision import QQDeliveryReceipt
        api = _api()
        try:
            receipt = QQDeliveryReceipt.model_validate(aggregate_receipt)
        except ValueError:
            raise api.WatchLedgerError("a valid complete aggregate receipt is required") from None
        moment = api._now(now)
        with self._transaction() as (connection, _state):
            parent = self._reservation(connection, parent_id)
            segments = self._segments(connection, parent)
            event = self._event(connection, parent.event_id, current_run=False)
            if (not segments or any(s.status != "sent" or not s.receipt_id for s in segments)
                    or receipt.group_id != self.group_id or receipt.self_member_id != parent.self_member_id
                    or receipt.decision_id != parent.decision_id or receipt.origin != "decision"
                    or receipt.delivered_segments != [s.text for s in segments]
                    or receipt.segment_receipt_ids != [s.receipt_id for s in segments]
                    or receipt.delivered_text != "\n\n".join(s.text for s in segments)
                    or receipt.source_record_ids != [event.source_record_id]
                    or receipt.delivered_at < max(s.delivered_at for s in segments)
                    or receipt.delivered_at > moment + timedelta(seconds=2)
                    or set(receipt.evidence_refs) != {ref for s in segments for ref in s.evidence_refs}):
                raise api.WatchLedgerError("the aggregate requires every exact ordered segment receipt")
            if parent.receipt_id is not None:
                if (parent.receipt_id != receipt.receipt_id or parent.evidence_refs != sorted(set(receipt.evidence_refs))
                        or parent.updated_at != receipt.delivered_at):
                    raise api.WatchLedgerError("the logical reply receipt cannot be replaced")
                return parent
            parent = parent.model_copy(update={
                "status": "sent", "receipt_id": receipt.receipt_id, "updated_at": receipt.delivered_at,
                "reconciled_at": moment, "reason": "all_segments_verified", "next_retry_at": None,
                "evidence_refs": sorted(set(receipt.evidence_refs))})
            self._save_send(connection, parent)
            self._save_event(connection, event.model_copy(update={"state": "sent", "reason": "all_segments_verified"}))
            return parent

    def _quarantine_segments(self, connection, parent, moment):
        for segment in self._segments(connection, parent):
            if segment.status in {"reserved", "retry_wait", "click_started"}:
                unknown = segment.status == "click_started"
                updated = self._save_segment(connection, segment.model_copy(update={
                    "status": "uncertain" if unknown else "cancelled", "updated_at": moment,
                    "reason": "process_restarted_no_segment_replay", "next_retry_at": None}))
                self._finish_attempt(connection, segment.child_id, "uncertain" if unknown else "cancelled",
                                     moment, updated.reason)
        segments = self._segments(connection, parent)
        if any(s.status in {"click_started", "uncertain"} for s in segments) or all(s.status == "sent" for s in segments):
            return "uncertain"
        return "partial" if any(s.status == "sent" for s in segments) else "not_sent"
