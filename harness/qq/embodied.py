"""QQ body port and homeostatic opportunities on the existing GUI actor."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import hashlib
import re
import sqlite3
from uuid import uuid4

from harness.embodiment.capabilities import CapabilityRequirement
from harness.embodiment.providers.qq import QQCommunicationError, QQCommunicationProvider, QQ_CAPABILITIES
from harness.embodiment.qq_contracts import (
    QQScope, QQDriverStatus, QQReadRequest, QQReadResult, QQVisibleMessage,
    QQSendRequest, QQFrozenSendPlan, QQVerifiedReceipt, QQDeliveryResult, QQReconcileRequest,
)
from harness.research.manifest import EmbodimentCondition


def _runtime():
    from harness.qq import watcher
    return watcher


class QQEmbodiedMixin:
    def _init_embodied(self):
        self._qq_loop = None
        self._qq_provider = None
        self._qq_read_frame = None
        self._qq_read_screen = None
        self._qq_read_group_id = self.settings.group_id
        self._qq_read_group_evidence = ()
        self._qq_operation = None
        self._qq_persisted = set()
        self._homeostasis = None
        self._homeostasis_ready = False
        self._idle_next_probe = 0.0
        self._feedback_next_scan = 0.0
        self._capability_bridge = None
        self._capability_next_report = 0.0
        if not self.settings.qq_body_enabled:
            return
        # This loop runs synchronously in the watcher's sole GUI actor thread.
        self._qq_loop = asyncio.new_event_loop()
        if getattr(self, "_shared_cognition", None) is not None:
            from harness.qq.capability_bridge import QQCapabilityClient
            self._capability_bridge = QQCapabilityClient(self._shared_cognition, self.ledger)
        if self.settings.homeostatic_idle_enabled:
            from harness.qq.homeostatic import QQHomeostaticDriver
            self._homeostasis = QQHomeostaticDriver(
                state_dir=self.settings.homeostasis_state_dir,
                profile_path=self.settings.homeostasis_profile_path,
                group_id=self.settings.group_id, self_member_id=self.settings.self_member_id,
                run_id=self.ledger.run_id, run_async=self._qq_loop.run_until_complete,
                tick_seconds=self.settings.homeostasis_tick_seconds,
                development_mode=self.settings.development_mode,
                shared_runtime_url=None if self.settings.development_mode else self.settings.shared_runtime_url,
                shared_control_token_file=None if self.settings.development_mode else self.settings.shared_control_token_file)

    def _qq_scope(self, group_id=None):
        return QQScope(body_instance_id=self.settings.expected_instance_id,
            environment_revision=self.settings.expected_revision, window_id=self.settings.window_id,
            group_id=group_id or self.settings.group_id, self_member_id=self.settings.self_member_id)

    def _qq_driver_status(self):
        moment = _runtime().now()
        fresh = self._qq_send_observer_ready()
        frame = self._qq_operation["turn"].frame if self._qq_operation else self._qq_read_frame
        bound = (frame is not None and frame.window_id == self.settings.window_id
                 and frame.active_window_id == self.settings.window_id)
        # A just-completed synchronous identity check may precede the sensor's
        # next poll. Its already-captured, grounded chat is a valid read source;
        # it never substitutes for the independent observer's send gate.
        direct_read = bool(self._qq_operation is None and bound
            and self._qq_read_screen is not None and self._qq_read_screen.scene == "group_chat"
            and not self._qq_read_screen.blocked
            and -2 <= (moment - frame.captured_at).total_seconds() <= 5)
        connected = bool(self._identity_prepared and bound and (fresh or direct_read))
        active_group = self.settings.group_id if self._qq_operation else self._qq_read_group_id
        return QQDriverStatus(observed_at=moment, active_scope=self._qq_scope(active_group) if connected else None,
            context_signature=frame.content_digest if connected else None,
            connected=connected, observer_ready=connected,
            send_ready=connected and fresh and self.automatic_send_enabled
                and active_group == self.settings.group_id and self._roster_visiting is None,
            paused=self.should_stop(), has_uncertain_delivery=self.ledger.has_uncertain_send())

    def _qq_send_observer_ready(self):
        snapshot = self.observer.latest() if self.observer is not None else None
        state = self.observer.status() if self.observer is not None else {}
        return (snapshot is not None and state.get("running") is True
                 and not state.get("last_error_type") and not state.get("gap_started_at")
                 and not state.get("gap_seconds")
                 and 0 <= _runtime().time.monotonic() - snapshot.polled_mono <= 5)

    def _qq_ensure_provider(self):
        if self._qq_provider is None:
            scope = self._qq_scope()
            condition = EmbodimentCondition(mode="grounded", body_instance_id=scope.body_instance_id,
                required_for_runtime=True, capability_requirements=[
                    CapabilityRequirement(capability=name, required=True) for name in QQ_CAPABILITIES])
            self._qq_provider = QQCommunicationProvider(condition=condition,
                allowed_scopes=[self._qq_scope(group.group_id) for group in self._group_roster if group.observe],
                driver_status=self._qq_driver_status, read_executor=self._qq_read_executor,
                send_executor=self._qq_send_executor, reconcile_executor=self._qq_reconcile_executor,
                load_frozen_plan=self._qq_frozen_plan, load_verified_receipt=self._qq_verified_receipt,
                clock=_runtime().now)
            value = self._qq_loop.run_until_complete(self._qq_provider.activate())
        else:
            value = self._qq_loop.run_until_complete(self._qq_provider.status())
        self.set_status(qq_body=value.model_dump(mode="json"))

    def _qq_read_executor(self, request):
        if request.scope.group_id != self._qq_read_group_id:
            raise ValueError("qq_read_group_binding_mismatch")
        frame, screen = self._qq_read_frame, self._qq_read_screen
        refs = list(dict.fromkeys([*self._qq_read_group_evidence, frame.evidence_ref]))
        if getattr(frame, "a11y_evidence_ref", None):
            refs.append(frame.a11y_evidence_ref)
        return QQReadResult(scope=request.scope, capture_id=frame.capture_id,
            observed_at=frame.captured_at, context_signature=frame.content_digest,
            evidence_refs=tuple(dict.fromkeys(refs))[-32:], messages=tuple(QQVisibleMessage(
                capture_item_id=f"{frame.capture_id}:{index}", direction=item.direction,
                kind=item.kind if item.kind in {"text", "sticker", "image"} else "unknown",
                text=item.text, display_name=item.display_name)
                for index, item in enumerate(screen.messages[:200])))

    def _qq_accept_read(self, frame, screen):
        if not self.settings.qq_body_enabled:
            return frame, screen
        # Identity verification can capture and close a card after the caller's
        # earlier group frame. Inspect only a frame already acquired by that
        # same actor; do not capture again or reopen a profile to satisfy a port.
        latest = getattr(self.desktop, "_last_verified_capture", None)
        if (latest is not None and latest.capture_id != frame.capture_id
                and latest.window_id == frame.window_id == self.settings.window_id
                and latest.active_window_id == self.settings.window_id
                and latest.captured_at >= frame.captured_at
                and -2 <= (_runtime().now() - latest.captured_at).total_seconds() <= 5):
            reading = getattr(self.desktop, "_screens", {}).get(latest.capture_id)
            if (reading is not None and reading.frame_id == latest.capture_id
                    and reading.evidence_ref == latest.evidence_ref and reading.scene == "group_chat"
                    and not reading.blocked and reading.opened_profile is None
                    and reading.group_title is not None and screen.group_title is not None
                    and reading.group_title.display_name == screen.group_title.display_name):
                frame, screen = latest, reading
        if (screen.frame_id != frame.capture_id or screen.evidence_ref != frame.evidence_ref
                or screen.scene != "group_chat" or screen.blocked or screen.opened_profile is not None
                or frame.window_id != self.settings.window_id or frame.active_window_id != self.settings.window_id):
            raise ValueError("qq_body_read_scope_mismatch")
        self._qq_read_frame, self._qq_read_screen = frame, screen
        self._qq_read_group_id = self.settings.group_id
        self._qq_read_group_evidence = ()
        self._qq_ensure_provider()
        moment = _runtime().now()
        request = QQReadRequest(scope=self._qq_scope(), requested_at=moment,
                                deadline=moment + timedelta(seconds=5))
        result = self._qq_loop.run_until_complete(self._qq_provider.read(request))
        self._remember_group_read(result)
        return frame, screen

    def _qq_wait_for_observer(self, deadline):
        runtime = _runtime()
        stop = min(deadline.deadline_mono, runtime.time.monotonic() + 3)
        current = self.observer.latest()
        # Captures and the sensor share one gate. A capture may temporarily
        # make the sensor report a gap; require recovery within the original
        # deadline before checking continuity, never accept its stale cache.
        while current is None and runtime.time.monotonic() < stop:
            self.check_control()
            deadline.check()
            runtime.time.sleep(min(.05, stop - runtime.time.monotonic()))
            current = self.observer.latest()
        deadline.check()
        if current is None:
            raise ValueError("shared_qq_observer_unavailable")
        return current

    def _qq_wait_for_send_observer(self, turn):
        """Wait out the actor's sensor gap inside the original send budget.

        A confirmed bubble can precede the observer's next successful read.
        Waiting does not authorize that bubble's remaining segments: a new
        arrival still supersedes the turn, and the provider rechecks all gates.
        """
        runtime = _runtime()
        waiting = False
        while True:
            self.check_control()
            turn.deadline.check()
            if self._newer_visible_context():
                raise QQCommunicationError("chat_context_changed")
            if self._qq_send_observer_ready():
                if waiting:
                    self.set_status(state="sending", last_error_type=None)
                return
            if not waiting:
                waiting = True
                turn.trace.add_counter("send_observer_waits")
                self.set_status(state="waiting_for_send_observer", automatic_send=False,
                                last_error_type="send_observer_not_ready")
            remaining = turn.deadline.deadline_mono - runtime.time.monotonic()
            runtime.time.sleep(min(.05, max(0, remaining)))

    def _qq_accept_group_read(self, verification, deadline):
        self._qq_wait_for_observer(deadline)
        frame, _screen = self._qq_accept_read(verification.chat_frame, verification.chat_screen)
        # The sender accepts only a verification issued by this desktop actor.
        # A copied dataclass cannot authorize the refreshed observation. Let
        # the existing numeric/continuity checks bind it, without extending TTL.
        verification = self.desktop.verify_group(frame=frame)
        self._qq_read_frame = verification.chat_frame
        self._qq_read_screen = verification.chat_screen
        self._qq_wait_for_observer(deadline)
        return verification

    def _qq_bound_plan(self, request):
        from harness.qq.decision import _frozen_decision, effective_reply_segments
        from harness.qq.watch_ledger import address_sources_for_event
        reservation = self.ledger.get_send(request.reservation_id)
        event = self.ledger.get_event(reservation.event_id)
        if (request.scope != self._qq_scope()
                or reservation.decision_id != request.decision_id or reservation.group_id != request.scope.group_id
                or reservation.self_member_id != request.scope.self_member_id
                or reservation.run_id != self.ledger.run_id or event.run_id != reservation.run_id
                or event.group_id != reservation.group_id or event.decision_id != reservation.decision_id
                or reservation.deadline_at is None
                or not 0 < (reservation.deadline_at - event.first_seen_at).total_seconds() <= 60):
            raise ValueError("qq_plan_scope_mismatch")
        with closing(sqlite3.connect(self.session.memory_db.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            decision = _frozen_decision(db, request.scope.group_id, request.decision_id)
        texts = effective_reply_segments(decision)
        if (decision.outcome != "ready" or not decision.new_decision or decision.replayed
                or decision.self_member_id != request.scope.self_member_id
                or hashlib.sha256(decision.reply.encode()).hexdigest() != reservation.reply_sha256
                or "\n\n".join(texts) != decision.reply or len(texts) != request.segment_count
                or texts[request.segment_index] != request.text
                or not set(address_sources_for_event(event)).issubset(decision.source_record_ids)
                or (event.origin == "homeostasis" and (decision.initiated_by != "homeostasis"
                    or decision.idle_intent_id != event.idle_intent.intent_id))
                or (event.origin == "message" and decision.initiated_by != "message")):
            raise ValueError("qq_plan_decision_mismatch")
        segments = self.ledger.segments(reservation.reservation_id)
        if ((segments and [item.text for item in segments] != texts)
                or (not segments and (request.segment_count != 1 or reservation.segment_count))):
            raise ValueError("qq_plan_segment_mismatch")
        return reservation, event, decision, segments

    def _qq_frozen_plan(self, request):
        reservation, event, decision, _segments = self._qq_bound_plan(request)
        from harness.qq.decision import effective_reply_segments
        return QQFrozenSendPlan(scope=self._qq_scope(), decision_id=decision.decision_id,
            reservation_id=reservation.reservation_id, input_hash=decision.input_hash,
            reply_segments=tuple(effective_reply_segments(decision)),
            first_seen_at=event.first_seen_at or event.observed_at, deadline=reservation.deadline_at)

    def _qq_verified_receipt(self, request):
        from harness.qq.decision import get_qq_delivery, get_qq_segment_deliveries
        from harness.qq.watch_ledger import address_sources_for_event
        reservation, event, _decision, segments = self._qq_bound_plan(request)
        if segments:
            receipts = get_qq_segment_deliveries(memory_db=self.session.memory_db,
                group_id=request.scope.group_id, decision_id=request.decision_id)
            receipt = next((item for item in receipts if item.segment_index == request.segment_index), None)
        else:
            receipt = get_qq_delivery(memory_db=self.session.memory_db,
                group_id=request.scope.group_id, decision_id=request.decision_id)
        if receipt is None:
            return None
        child = segments[request.segment_index] if segments else None
        record = child or reservation
        proof = child.proof if child else reservation.preclick_proof
        expected_id = child.child_id if child else reservation.reservation_id
        ack, observed = record.click_acknowledged_at, record.click_observed_at
        click_upper = ack or observed
        if (receipt.self_member_id != reservation.self_member_id or receipt.group_id != reservation.group_id
                or receipt.decision_id != reservation.decision_id or receipt.delivered_text != request.text
                or (child and (receipt.segment_index != child.segment_index or receipt.segment_count != child.segment_count))
                or set(receipt.source_record_ids) != set(address_sources_for_event(event))
                or record.status not in {"click_started", "uncertain", "sent"}
                or proof is None or record.click_started_at is None
                or proof.reservation_id != expected_id or proof.group_id != reservation.group_id
                or proof.self_member_id != reservation.self_member_id
                or proof.reply_sha256 != hashlib.sha256(request.text.encode()).hexdigest()
                or set(proof.source_record_ids) != set(address_sources_for_event(event))
                or proof.window_id != request.scope.window_id or proof.body_instance_id != request.scope.body_instance_id
                or proof.environment_revision != request.scope.environment_revision
                or (record.receipt_id is not None and record.receipt_id != receipt.receipt_id)
                or (ack is None) == (observed is None)
                or not proof.verified_at <= record.click_started_at <= click_upper <= receipt.delivered_at
                or (observed is not None and record.click_observed_evidence_ref not in receipt.evidence_refs)):
            raise ValueError("qq_receipt_scope_mismatch")
        scope = QQScope(body_instance_id=proof.body_instance_id, environment_revision=proof.environment_revision,
                        window_id=proof.window_id, group_id=proof.group_id, self_member_id=proof.self_member_id)
        return QQVerifiedReceipt(scope=scope, decision_id=receipt.decision_id,
            reservation_id=reservation.reservation_id, segment_index=request.segment_index,
            segment_count=request.segment_count, receipt_id=receipt.receipt_id,
            delivered_text=receipt.delivered_text, delivered_at=receipt.delivered_at,
            evidence_refs=tuple(receipt.evidence_refs), verification=receipt.verification)

    def _qq_execute(self, request, *, reconcile=False):
        operation = self._qq_operation
        if operation is None or operation["request"] != request:
            raise ValueError("qq_actor_operation_mismatch")
        reservation, _event, _decision, segments = self._qq_bound_plan(request)
        record = segments[request.segment_index] if segments else reservation
        if not reconcile and (record.status not in {"reserved", "retry_wait"}
                              or record.click_started_at is not None
                              or (segments and any(item.status != "sent" or not item.receipt_id
                                  for item in segments[:request.segment_index]))):
            raise ValueError("qq_actor_intent_not_sendable")
        raw = operation["execute"]()
        operation["raw"] = raw
        if raw.status == "sent":
            operation["persist"](raw)
            receipt = self._qq_verified_receipt(request)
            if receipt is None:
                raise ValueError("qq_actor_receipt_missing")
            self._qq_persisted.add((request.decision_id, request.segment_index))
            return QQDeliveryResult(scope=request.scope, decision_id=request.decision_id,
                reservation_id=request.reservation_id, segment_index=request.segment_index,
                segment_count=request.segment_count, status="sent", reason_code="durable_receipt_verified",
                receipt_id=receipt.receipt_id, delivered_at=receipt.delivered_at,
                evidence_refs=receipt.evidence_refs)
        return QQDeliveryResult(scope=request.scope, decision_id=request.decision_id,
            reservation_id=request.reservation_id, segment_index=request.segment_index,
            segment_count=request.segment_count, status=raw.status, reason_code=raw.reason_code,
            evidence_refs=tuple(raw.evidence_refs))

    def _qq_send_executor(self, request):
        return self._qq_execute(request)

    def _qq_reconcile_executor(self, request):
        return self._qq_execute(request.send_request, reconcile=True)

    def _qq_action(self, turn, *, execute, persist, segment=None, reconcile=False):
        if not self.settings.qq_body_enabled:
            return execute()
        from harness.qq.desktop import DeliveryResult
        reservation = self.ledger.get_send(turn.reservation_id)
        moment = _runtime().now()
        # Reconciliation retains the expired original request and uses its own
        # short read-only budget; it can never renew the send opportunity.
        requested = min(moment, reservation.deadline_at - timedelta(microseconds=1))
        request = QQSendRequest(scope=self._qq_scope(), decision_id=turn.decision.decision_id,
            reservation_id=turn.reservation_id, segment_index=segment.segment_index if segment else 0,
            segment_count=segment.segment_count if segment else 1,
            text=segment.text if segment else turn.decision.reply, requested_at=requested,
            deadline=reservation.deadline_at, context_signature=turn.frame.content_digest)
        operation = {"turn": turn, "request": request, "execute": execute, "persist": persist, "raw": None}
        self._qq_operation = operation
        try:
            self._qq_ensure_provider()
            if reconcile:
                call = self._qq_provider.reconcile(QQReconcileRequest(send_request=request,
                    requested_at=moment, deadline=moment + timedelta(seconds=15)))
                result = self._qq_loop.run_until_complete(call)
            else:
                while True:
                    self._qq_wait_for_send_observer(turn)
                    self._check_homeostatic_turn(turn)
                    try:
                        result = self._qq_loop.run_until_complete(self._qq_provider.send(request))
                        break
                    except QQCommunicationError as exc:
                        # The sensor can enter another gap between the wait
                        # and the provider's guard. Only this pre-executor gate
                        # failure may wait again; an attempted click is never retried.
                        if (exc.code not in {"capability_unavailable", "required_capability_unavailable"}
                                or operation["raw"] is not None or self._qq_send_observer_ready()):
                            raise
            raw = operation["raw"]
            if raw is not None:
                return raw if raw.status == result.status else replace(raw, status=result.status,
                    reason_code=result.reason_code, delivered_at=result.delivered_at)
            return DeliveryResult(status=result.status, reason_code=result.reason_code,
                delivered_at=result.delivered_at, evidence_refs=list(result.evidence_refs))
        finally:
            self._qq_operation = None

    def _qq_was_persisted(self, turn, index=0):
        return (turn.decision.decision_id, index) in self._qq_persisted

    def _check_homeostatic_turn(self, turn):
        intent = getattr(turn.event, "idle_intent", None)
        if intent is None:
            return
        bridge = getattr(self, "_capability_bridge", None)
        authority_known = bridge is not None and self.ledger.capability_owner() is not None
        if authority_known:
            # Legacy work also retains its original generation: a migration
            # away and back cannot revive an already prepared idle turn.
            self._report_capabilities(force=True)
        if intent.opportunity_ref is not None:
            # Refresh central ownership immediately before a send, including
            # after model latency. Loss of the authority connection fails shut.
            if (bridge is None or bridge.owner != "shared" or not bridge.send_enabled
                    or bridge.generation != intent.scheduler_generation or bridge.valid_until is None
                    or _runtime().now() >= bridge.valid_until):
                raise RuntimeError("shared_qq_capability_authority_changed")
            row = next((item for item in self.ledger.capability_claims() if item["opportunity_id"] == intent.opportunity_ref), None)
            if row is None:
                raise RuntimeError("shared_qq_capability_claim_missing")
            bridge.check_claim(row, now=_runtime().now())
            # The shared Runtime keeps heartbeating while the QQ model runs.
            # Refresh factual state before the effect, without renewing its
            # deadline or modifying the immutable intent's source summary.
            if self._homeostasis is not None:
                self._homeostasis.refresh()
        elif authority_known:
            if (bridge.owner != "legacy" or bridge.generation != intent.owner_generation
                    or bridge.valid_until is None or _runtime().now() >= bridge.valid_until):
                raise RuntimeError("legacy_qq_opportunity_owner_changed")
        summary = self._homeostasis.summary if self._homeostasis is not None else None
        revision_valid = summary is not None and (
            summary.snapshot_revision >= intent.homeostasis_revision if intent.opportunity_ref is not None
            else summary.snapshot_revision == intent.homeostasis_revision)
        if (not revision_valid
                or summary.episode_signature != intent.episode_signature
                or _runtime().now() >= intent.deadline_at):
            raise RuntimeError("homeostatic_intent_stale")

    def _homeostatic_feedback(self, *, fact_id, kind, occurred_at, evidence_refs):
        if self._homeostasis is not None:
            try:
                self._homeostasis.enqueue_fact(fact_id=fact_id, kind=kind,
                    occurred_at=occurred_at, evidence_refs=[fact_id] if kind == "send_attempt" else evidence_refs)
            except Exception as exc:
                self._homeostasis_ready = False
                self._feedback_next_scan = 0.0
                self.set_status(homeostasis_feedback_error=type(exc).__name__)

    def _tick_homeostasis(self):
        if self._homeostasis is None or self._pending_turn is not None:
            return
        try:
            if _runtime().time.monotonic() >= self._feedback_next_scan:
                from harness.qq.feedback import recover_recent_feedback
                recover_recent_feedback(self._homeostasis, memory_db=self.session.memory_db,
                    watch_db=self.settings.state_db, clock=_runtime().now)
                self._feedback_next_scan = _runtime().time.monotonic() + 60
            self._homeostasis.tick()
            self._homeostasis_ready = True
            self.set_status(homeostasis_feedback_error=None)
        except Exception as exc:
            self._homeostasis_ready = False
            self.set_status(homeostasis_feedback_error=type(exc).__name__)
        self.set_status(homeostasis=self._homeostasis.status())

    def _maybe_interaction_feedback(self, decision):
        if self._homeostasis is not None:
            from harness.qq.feedback import feedback_for_direct_response
            try:
                feedback_for_direct_response(self._homeostasis, memory_db=self.session.memory_db,
                    decision=decision, clock=_runtime().now)
            except Exception as exc:
                self._homeostasis_ready = False
                self._feedback_next_scan = 0.0
                self.set_status(homeostasis_feedback_error=type(exc).__name__)

    def _report_capabilities(self, *, force=False):
        bridge = getattr(self, "_capability_bridge", None)
        if bridge is None:
            return
        runtime = _runtime()
        if not force and runtime.time.monotonic() < self._capability_next_report:
            return
        from harness.qq.capability_bridge import QQCapabilityReport
        from harness.embodiment.capabilities import QQ_COMMUNICATION_READ, QQ_COMMUNICATION_SEND
        driver = self._qq_driver_status()
        provider = self._qq_provider
        ready = provider is not None and driver.connected and driver.observer_ready
        frame = self._qq_read_frame
        for group in self._group_roster:
            if not group.observe:
                continue
            refs = ([frame.evidence_ref] if frame is not None and self._qq_read_group_id == group.group_id
                    else self._roster_read_refs(group.group_id))
            report = QQCapabilityReport(scope=self._qq_scope(group.group_id), run_id=self.ledger.run_id,
                reported_at=runtime.now(),
                read_ready=bool(ready and provider.manager.supports(QQ_COMMUNICATION_READ)),
                send_ready=bool(group.send and group.group_id == self.settings.group_id
                    and ready and driver.send_ready and provider.manager.supports(QQ_COMMUNICATION_SEND)),
                paused=driver.paused or self._pending_turn is not None or self._roster_visiting is not None,
                uncertain=driver.has_uncertain_delivery, observation_refs=refs)
            bridge.report(report)
        self._capability_next_report = runtime.time.monotonic() + 5
        self.set_status(capability_owner=bridge.owner, capability_generation=bridge.generation)

    def _capability_cycle(self):
        bridge = getattr(self, "_capability_bridge", None)
        if bridge is None:
            return
        try:
            self._report_capabilities()
            self.ledger.expire_unstarted_capability_claims(now=_runtime().now())
            for row in self.ledger.capability_claims():
                if row["local_status"] != "claimed":
                    bridge.acknowledge(row)
            self.set_status(capability_bridge_error=None)
        except Exception as exc:
            # Persisted shared ownership never falls back to a legacy trigger.
            self.set_status(capability_bridge_error=type(exc).__name__)

    def _maybe_shared_capability(self, snapshot):
        bridge = getattr(self, "_capability_bridge", None)
        if bridge is None or bridge.owner != "shared" or self._pending_turn is not None:
            return
        row = None
        try:
            for group in self._group_roster:
                if group.observe:
                    row = bridge.claim(self._qq_scope(group.group_id), self.ledger.run_id, now=_runtime().now())
                    if row is not None:
                        break
            if row is None:
                return
            local = next(item for item in self.ledger.capability_claims() if item["opportunity_id"] == row["opportunity_id"])
            if local["local_status"] != "claimed":
                bridge.acknowledge(local)
                return
            from harness.embodiment.capabilities import QQ_COMMUNICATION_READ, QQ_COMMUNICATION_SEND
            if row["capability_id"] == QQ_COMMUNICATION_SEND:
                if not bridge.send_enabled:
                    raise ValueError("shared_qq_send_disabled")
                if self._homeostasis is not None:
                    self._homeostasis.refresh()
                summary = self._homeostasis.summary if self._homeostasis is not None else None
                if (summary is None
                        or (row.get("source_homeostasis_revision") is not None and row["source_homeostasis_revision"] > summary.snapshot_revision)
                        or (row.get("source_episode_signature") is not None and row["source_episode_signature"] != summary.episode_signature)):
                    raise ValueError("shared_qq_homeostasis_changed")
                self._maybe_homeostatic_idle(snapshot, shared_claim=row)
                # Every declined local opportunity consumes this dispatch.
                self.ledger.finish_capability_claim(row["opportunity_id"], status="rejected")
            elif row["capability_id"] == QQ_COMMUNICATION_READ:
                self._read_shared_capability(row, snapshot)
            else:
                raise ValueError("shared_qq_unknown_capability")
        except Exception as exc:
            if row is not None:
                self.ledger.finish_capability_claim(row["opportunity_id"], status="failed")
            code = str(exc) if isinstance(exc, ValueError) else None
            self.set_status(capability_bridge_error=type(exc).__name__,
                capability_failure_code=code if code and re.fullmatch(r"[a-z_]{1,96}", code) else None,
                capability_failed_opportunity=row["opportunity_id"] if row is not None else None)

    def _read_shared_capability(self, row, snapshot):
        from datetime import datetime
        from harness.qq.telemetry import QQDeadline, QQTurnTrace
        from harness.qq.accessibility import chat_change_signature
        runtime = _runtime()
        limit = datetime.fromisoformat(row["deadline_at"])
        if row["scope"]["group_id"] != self.settings.group_id:
            result = self._read_roster_group(row["scope"]["group_id"], deadline_at=limit)
            self.ledger.finish_capability_claim(row["opportunity_id"], status="completed",
                result=result.model_dump(mode="json"))
            return
        remaining = (limit - runtime.now()).total_seconds()
        deadline = QQDeadline(runtime.time.monotonic() + remaining)
        trace = QQTurnTrace(first_seen_at=runtime.now(), first_seen_mono=runtime.time.monotonic())
        with self._desktop_turn(deadline, trace):
            deadline.check()
            identity_started_at = runtime.now()
            frame = self.desktop.capture()
            verification = self.desktop.verify_group(frame=frame)
            account = self.desktop.verify_self()
            self._identity_prepared = True
            verification = self._qq_accept_group_read(verification, deadline)
            frame = verification.chat_frame
            deadline.check()
            current = self.observer.latest()
            signature = chat_change_signature(frame.a11y_tree)
            if current is None or current.sequence != snapshot.sequence or signature.chat_digest != snapshot.digest:
                raise ValueError("shared_qq_read_superseded")
            self._acknowledge_verified_identity_gaps(verification, account, started_at=identity_started_at)
            request = QQReadRequest(scope=self._qq_scope(), requested_at=runtime.now(), deadline=limit)
            result = self._qq_loop.run_until_complete(self._qq_provider.read(request))
            deadline.check()
            self._remember_group_read(result)
            self.ledger.finish_capability_claim(row["opportunity_id"], status="completed", result=result.model_dump(mode="json"))

    def _maybe_homeostatic_idle(self, snapshot, *, shared_claim=None):
        runtime = _runtime()
        bridge = getattr(self, "_capability_bridge", None)
        if shared_claim is None and bridge is not None and bridge.owner != "legacy":
            return
        if (self._homeostasis is None or not self._homeostasis_ready
                or self._pending_turn is not None or self._attribution_retry
                or not self.automatic_send_enabled or self.ledger.has_uncertain_send()
                or self._expired_observation is not None or self._carry_first_seen is not None
                or (shared_claim is None and runtime.time.monotonic() < self._idle_next_probe)
                or not self._homeostasis.eligible(quiet_seconds=self.settings.homeostatic_quiet_seconds,
                                                  shared_opportunity=shared_claim is not None)):
            return
        if shared_claim is None:
            next_opportunity = self.ledger.next_idle_opportunity_at(
                minimum_interval_seconds=self.settings.homeostatic_opportunity_seconds)
            if next_opportunity is not None and runtime.now() < next_opportunity:
                return
            self._idle_next_probe = runtime.time.monotonic() + 60
        from harness.qq.accessibility import chat_change_signature
        from harness.qq.idle import QQIdleIntent
        from harness.qq.telemetry import QQDeadline, QQTurnTrace
        moment, mono = runtime.now(), runtime.time.monotonic()
        from datetime import datetime
        expires = (datetime.fromisoformat(shared_claim["deadline_at"]) if shared_claim is not None
                   else moment + timedelta(seconds=self.settings.turn_deadline_seconds))
        if expires <= moment:
            return
        deadline = QQDeadline(mono + min(self.settings.turn_deadline_seconds, (expires - moment).total_seconds()))
        trace = QQTurnTrace(first_seen_at=moment, first_seen_mono=mono)
        trace.add_counter("homeostatic_idle")
        try:
            with self._desktop_turn(deadline, trace), trace.stage("idle_body_read"):
                identity_started_at = runtime.now()
                frame = self.desktop.capture()
                verification = self.desktop.verify_group(frame=frame)
                account = self.desktop.verify_self()
                deadline.check()
                self._self_aliases = list(getattr(self.desktop, "verified_self_aliases", []))
                self._identity_prepared = True
                verification = self._qq_accept_group_read(verification, deadline)
                frame = verification.chat_frame
        except Exception:
            self._finish_trace(trace, "failed")
            raise
        signature = chat_change_signature(frame.a11y_tree)
        latest = self.observer.latest()
        if (not signature.chat_visible or latest is None or latest.sequence != snapshot.sequence
                or signature.chat_digest != snapshot.digest):
            self._finish_trace(trace, "superseded")
            return
        self._acknowledge_verified_identity_gaps(verification, account, started_at=identity_started_at)
        summary = self._homeostasis.summary
        intent = QQIdleIntent(intent_id=("qq-idle:" + hashlib.sha256(shared_claim["opportunity_id"].encode()).hexdigest()
                            if shared_claim is not None else f"qq-idle:{uuid4().hex}"), group_id=self.settings.group_id,
            self_member_id=self.settings.self_member_id, run_id=self.ledger.run_id,
            created_at=moment, deadline_at=expires,
            episode_signature=summary.episode_signature, homeostasis_revision=summary.snapshot_revision,
            homeostatic_interoception=summary, context_signature=frame.content_digest,
            body_instance_id=self.settings.expected_instance_id, environment_revision=self.settings.expected_revision,
            window_id=self.settings.window_id,
            opportunity_ref=shared_claim["opportunity_id"] if shared_claim is not None else None,
            selection_ref=shared_claim["selection_ref"] if shared_claim is not None else None,
            scheduler_generation=shared_claim["generation"] if shared_claim is not None else None,
            owner_generation=bridge.generation if bridge is not None else None)
        event = self.ledger.register_idle_intent(intent, self_member_id=self.settings.self_member_id,
            body_instance_id=self.settings.expected_instance_id, environment_revision=self.settings.expected_revision,
            window_id=self.settings.window_id, context_signature=frame.content_digest,
            minimum_interval_seconds=0 if shared_claim is not None else self.settings.homeostatic_opportunity_seconds,
            now=runtime.now())
        if event is None:
            self._finish_trace(trace, "guard_wait")
            return
        trace.event_id = event.event_id
        turn = runtime.PendingTurn(event, None, None, frame, verification, deadline, trace,
            context_digest=signature.chat_digest, homeostatic_interoception=summary)
        self._pending_turn = turn
        self.set_status(last_idle_intent_at=moment.isoformat(), homeostasis=self._homeostasis.status())
        self._decide_turn(turn)

    def _close_embodied(self):
        if self._homeostasis is not None:
            self._homeostasis.close()
        if self._qq_loop is not None:
            self._qq_loop.close()
