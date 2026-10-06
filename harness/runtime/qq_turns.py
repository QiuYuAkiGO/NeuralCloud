"""QQ cognition through the existing Runtime queue, with no local delivery."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from contextlib import closing

from harness.core.error_notice import ERROR_FALLBACK_SEND_SECONDS
from harness.qq.feedback import _observation, _readonly
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.watch_ledger import WatchEvent
from harness.runtime.models import RuntimeEvent, RuntimeEventPriority, RuntimeEventType


def _source(config, watcher_path: Path | None, request: QQSharedPrepareRequest, *, completed_replay=False):
    if request.transport == "onebot":
        from harness.qq.direct_source import validate_direct_source

        return validate_direct_source(config, request, completed_replay=completed_replay)
    if (request.group_id not in config.allowed_group_ids or request.self_member_id != config.self_member_id
            or watcher_path is None):
        raise ValueError("qq_turn_scope_unavailable")
    if (not watcher_path.is_absolute() or watcher_path.is_symlink() or not watcher_path.is_file()
            or watcher_path.stat().st_size > 65536):
        raise ValueError("qq_turn_watcher_configuration_invalid")
    settings = json.loads(watcher_path.read_text(encoding="utf-8"))
    if (settings.get("group_id"), settings.get("self_member_id")) != (request.group_id, request.self_member_id):
        raise ValueError("qq_turn_watcher_scope_invalid")
    ledger_path = Path(settings["state_db"])
    if not ledger_path.is_absolute() or ledger_path.is_symlink() or not ledger_path.is_file():
        raise ValueError("qq_turn_ledger_unavailable")
    with closing(_readonly(ledger_path)) as ledger:
        row = ledger.execute("SELECT run_id,group_id,state,record_json FROM qq_watch_events WHERE event_id=? AND group_id=?",
                             (request.trace.event_id, request.group_id)).fetchone()
        state = ledger.execute("SELECT group_id,run_id,revision,baseline_required FROM qq_watch_state WHERE singleton=1").fetchone()
        has_send = ledger.execute("SELECT 1 FROM qq_watch_sends WHERE event_id=? LIMIT 1", (request.trace.event_id,)).fetchone()
    if row is None:
        raise ValueError("qq_turn_source_missing")
    event = WatchEvent.model_validate_json(row["record_json"])
    if (state is None or (event.run_id,event.group_id,event.state) != (row["run_id"],row["group_id"],row["state"])
            or (event.run_id,event.group_id) != (state["run_id"],state["group_id"])):
        raise ValueError("qq_turn_current_run_invalid")
    if not completed_replay and (event.state != "bound" or event.decision_id is not None
            or has_send is not None or state["baseline_required"] or event.stream_revision != state["revision"]):
        raise ValueError("qq_turn_source_not_pending")
    if (event.event_id != request.trace.event_id or event.group_id != request.group_id
            or event.first_seen_at != request.trace.first_seen_at
            or request.deadline_at > event.first_seen_at + timedelta(seconds=60)
            or request.deadline_at <= event.first_seen_at):
        raise ValueError("qq_turn_source_binding_invalid")
    if event.origin == "homeostasis":
        if (request.idle_intent is None or event.idle_intent != request.idle_intent
                or request.request is not None or request.trigger_member_id is not None):
            raise ValueError("qq_turn_idle_source_invalid")
    else:
        if (request.idle_intent is not None or event.source_record_id is None or event.item.direction != "incoming"
                or event.item.kind != "text"):
            raise ValueError("qq_turn_message_source_invalid")
        with closing(_readonly(config.memory_db)) as memory:
            observation = _observation(memory, request.group_id, event.source_record_id)
        if (observation is None or observation.member_id != request.trigger_member_id
                or observation.member_id == config.self_member_id or observation.capture_item_id != event.event_id
                or observation.text != event.item.text):
            raise ValueError("qq_turn_author_binding_invalid")


class QQTurnIngress:
    def __init__(self, control, config, *, watcher_path=None):
        self.control, self.config = control, config
        configured = watcher_path or os.environ.get("NC_QQ_WATCHER_CONFIG")
        self.watcher_path = Path(configured) if configured else (
            config.watcher_status_file.parent / "watcher.json" if config and config.watcher_status_file else None)
        self._lock = asyncio.Lock()
        self._waiters = {}
        self._groups = {}
        control.supervisor.add_listener(self._completed)
        control.supervisor.qq_turn_ingress = self

    def close_group(self, group_id):
        for event_id, group in list(self._groups.items()):
            if group == group_id:
                self._groups.pop(event_id, None)
                for waiter in self._waiters.pop(event_id, ()):
                    if not waiter.done():
                        waiter.set_exception(ValueError("attention_closed"))

    async def _completed(self, topic, value):
        if topic == "event.completed":
            self._groups.pop(value.get("event_id"), None)
            for waiter in self._waiters.pop(value.get("event_id"), ()):
                if not waiter.done():
                    waiter.set_result(value)

    def _remove_waiter(self, event_id, waiter):
        """A disconnected/timed-out subscriber cannot remove another caller."""
        subscribers = self._waiters.get(event_id)
        if subscribers is not None:
            subscribers.discard(waiter)
            if not subscribers:
                self._waiters.pop(event_id, None)
                self._groups.pop(event_id, None)
        if not waiter.done():
            waiter.cancel()

    def _historical(self, event_id):
        indexed = getattr(self.control.supervisor, "qq_turn_history", None)
        if callable(indexed):
            return indexed(event_id)
        # Small injected supervisors retain their fixture/embedding protocol.
        # Production Runtime supplies the sequence-only journal index above.
        accepted, completed = None, None
        for record in self.control.supervisor.journal.iter_records(reverse=True):
            if record.event_id != event_id:
                continue
            if record.record_type == "processing_completed" and completed is None:
                completed = record.data
            if record.record_type == "event_accepted":
                accepted = RuntimeEvent.model_validate(record.data["event"])
                break
        return accepted, completed

    async def prepare(self, request: QQSharedPrepareRequest):
        now = datetime.now(timezone.utc)
        remaining = (request.deadline_at - now).total_seconds()
        if remaining <= 0 or request.trace.first_seen_at > now + timedelta(seconds=5):
            raise TimeoutError("qq_turn_expired")
        # Admission still expires at the original deadline. Only a waiter for
        # an already admitted request may receive a verified error notice late.
        loop = asyncio.get_running_loop()
        original_deadline = loop.time() + remaining
        grace = ERROR_FALLBACK_SEND_SECONDS if remaining >= 1 else 0
        waiter = None
        event_id = None
        try:
            async with asyncio.timeout_at(original_deadline):
                waiter, event_id, replay = await self._admit(request)
                if replay is not None:
                    return self._result_in_window(replay, request, allow_grace=False,
                                                  original_deadline=original_deadline, grace=0)
            async with asyncio.timeout_at(original_deadline + grace):
                value = await asyncio.shield(waiter)
                return self._result_in_window(value, request, allow_grace=True,
                                              original_deadline=original_deadline, grace=grace)
        finally:
            if waiter is not None:
                self._remove_waiter(event_id, waiter)

    async def _admit(self, request: QQSharedPrepareRequest):
        """Validate the unchanged source and queue it within the original TTL."""
        waiter = None
        event_id = None
        try:
            key = f"{request.group_id}:{request.self_member_id}:{request.trace.event_id}:{request.trace.trace_id}"
            event_id = "qq-turn:" + hashlib.sha256(key.encode()).hexdigest()
            payload = request.model_dump(mode="json")
            async with self._lock:
                prior, completed = self._historical(event_id)
                if prior is not None:
                    # Desktop requests persisted before transport was added
                    # retain the same semantic identity under the default.
                    prior_payload = QQSharedPrepareRequest.model_validate(prior.payload).model_dump(mode="json")
                    if prior.event_type != RuntimeEventType.QQ_TURN or prior_payload != payload:
                        raise ValueError("qq_turn_request_conflict")
                    if completed is not None:
                        await asyncio.to_thread(_source, self.config, self.watcher_path, request, completed_replay=True)
                        return None, event_id, completed
                await asyncio.to_thread(_source, self.config, self.watcher_path, request)
                # Retries subscribe to the same event independently: their
                # admission time, transport cancellation and grace may differ.
                waiter = asyncio.get_running_loop().create_future()
                self._waiters.setdefault(event_id, set()).add(waiter)
                self._groups[event_id] = request.group_id
                if request.transport == "onebot":
                    from harness.qq.attention import require_attention
                    require_attention(request.group_id)
                if prior is None:
                    event = await self.control.supervisor.submit_event(event_type=RuntimeEventType.QQ_TURN,
                        priority=RuntimeEventPriority.USER, source="qq_body", event_id=event_id,
                        correlation_id=request.trace.trace_id, idempotency_key=event_id,
                        source_time=request.trace.first_seen_at, payload=payload)
                    if event is None:
                        raise ValueError("qq_turn_not_queued")
            return waiter, event_id, None
        except BaseException:
            if waiter is not None:
                self._remove_waiter(event_id, waiter)
            raise

    def _result_in_window(self, value, request, *, allow_grace, original_deadline, grace):
        detail = self._result(value)
        now = datetime.now(timezone.utc)
        monotonic_now = asyncio.get_running_loop().time()
        if now < request.deadline_at and monotonic_now < original_deadline:
            return detail
        if (not allow_grace or grace <= 0 or monotonic_now >= original_deadline + grace
                or now >= request.deadline_at + timedelta(seconds=grace)):
            raise TimeoutError("qq_turn_expired")
        # Do not trust an outcome string to extend any ordinary reply. Reuse
        # the reply contract and scope/trace bindings enforced by Runtime.
        from harness.qq.decision import QQReplyDecision

        decision = QQReplyDecision.model_validate(detail["decision"])
        trace = detail["trace"]
        if (decision.outcome != "error_fallback" or decision.replayed or not decision.new_decision):
            raise TimeoutError("qq_turn_expired")
        if ((decision.group_id, decision.self_member_id, decision.trace_id) !=
                (request.group_id, request.self_member_id, request.trace.trace_id)
                or not isinstance(trace, dict) or trace.get("trace_id") != request.trace.trace_id
                or trace.get("event_id") != request.trace.event_id):
            raise ValueError("qq_turn_result_binding_invalid")
        return detail

    @staticmethod
    def _result(value):
        if value.get("status") != "completed" or value.get("decision_kind") != "qq_prepare":
            from harness.qq.attention import ATTENTION_DENIALS
            code = (value.get("detail") or {}).get("error_code")
            if code in ATTENTION_DENIALS:
                raise ValueError(code)
            raise ValueError("qq_turn_not_completed")
        detail = value.get("detail")
        if not isinstance(detail, dict) or set(detail) != {"decision", "trace"}:
            raise ValueError("qq_turn_result_invalid")
        return detail


def add_qq_turn_route(router, control, config):
    from fastapi import HTTPException

    ingress = QQTurnIngress(control, config) if config else None

    @router.post("/v1/qq/decisions")
    async def prepare(payload: dict):
        if ingress is None:
            raise HTTPException(status_code=503, detail="qq_shared_runtime_not_configured")
        try:
            request = QQSharedPrepareRequest.model_validate(payload)
        except ValueError:
            raise HTTPException(status_code=422, detail="qq_shared_request_invalid") from None
        try:
            return await ingress.prepare(request)
        except TimeoutError:
            raise HTTPException(status_code=408, detail="qq_turn_expired") from None
        except (ValueError, OSError, KeyError) as exc:
            if str(exc) in {"attention_closed", "attention_policy_changed", "attention_backlog", "attention_deferred"}:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            raise HTTPException(status_code=409, detail="qq_turn_source_or_state_invalid") from None
