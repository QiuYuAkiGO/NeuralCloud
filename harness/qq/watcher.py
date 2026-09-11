"""Authorized QQ desktop watcher: capture, attribute, remember, decide and send once.

QQ content can influence a conversational reply, never host operations or scope.
The private configuration is the only source of group and sending authorization.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from collections import deque
import hashlib
import math
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from harness.qq.decision import decide_qq_reply, get_qq_delivery, record_qq_delivery
from harness.qq.memory import QQID, QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput
from harness.qq.session import QQSessionSettings
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace, classify_error


def runtime_source_hash() -> str:
    root = Path(__file__).absolute().parents[2]
    files = sorted((root / "harness/qq").glob("*.py")) + [
        root / "harness/llm/client.py", root / "harness/embodiment/providers/mistyai.py"]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return "qq-unattended:" + digest.hexdigest()


def now() -> datetime:
    return datetime.now(timezone.utc)


def error_code(exc: Exception) -> str:
    from harness.qq.desktop import DesktopError
    from harness.qq.grounding import GroundingError
    return str(exc) if isinstance(exc, (DesktopError, GroundingError)) and re.fullmatch(r"[a-z_]{1,100}", str(exc)) else type(exc).__name__


class WatcherSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_config_path: Path
    state_db: Path
    status_file: Path
    control_file: Path
    body_url: str = "http://127.0.0.1:8788"
    body_token_file: Path
    agent_url: str = "http://127.0.0.1:8766"
    agent_token_file: Path
    expected_instance_id: str = "misty-qq-test-01"
    expected_revision: int = Field(default=2, ge=1)
    window_id: Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]+$")] = "0x200002e"
    group_id: QQID
    self_member_id: QQID
    vision_model: str = "deepseek-v4-flash-vision-exp"
    poll_seconds: float = Field(default=15, ge=5, le=300)
    cooldown_seconds: float = Field(default=60, ge=30, le=3600)
    automatic_send: bool = False
    accessibility_enabled: bool = False
    identity_cache_enabled: bool = False
    settle_seconds: float = Field(default=0.65, ge=0.1, le=3)
    observation_interval_seconds: float = Field(default=1, ge=0.25, le=2)
    turn_deadline_seconds: float = Field(default=60, ge=1, le=60)
    trace_file: Path | None = None
    initial_baseline_observation: Path | None = None
    recover_attribution_event_id: Annotated[str, StringConstraints(pattern=r"^qq-watch-event:[0-9a-f]{32}$")] | None = None

    @model_validator(mode="after")
    def local_configuration(self):
        for name in ("session_config_path", "state_db", "status_file", "control_file", "body_token_file", "agent_token_file"):
            if not getattr(self, name).is_absolute():
                raise ValueError("Watcher state and token paths must be absolute")
        for value in (self.body_url, self.agent_url):
            parts = urlsplit(value)
            if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1"} or parts.username or parts.password:
                raise ValueError("Desktop endpoints must use authenticated loopback HTTP")
        paths = [self.state_db, self.status_file, self.control_file]
        if len(set(paths)) != len(paths) or any(p == self.session_config_path for p in paths):
            raise ValueError("Watcher outputs and configuration must use distinct files")
        if self.trace_file is not None and (not self.trace_file.is_absolute() or self.trace_file in paths or self.trace_file == self.session_config_path):
            raise ValueError("Trace output must be a separate absolute private path")
        if self.initial_baseline_observation is not None and not self.initial_baseline_observation.is_absolute():
            raise ValueError("Initial baseline observation path must be absolute")
        return self


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(5):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


@contextmanager
def worker_lock(path: Path):
    """An OS-held lock survives file reuse and is released if the worker dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass
class PendingTurn:
    event: object
    member_id: str
    source_id: str
    frame: object
    verification: object
    deadline: QQDeadline
    trace: QQTurnTrace
    decision: object = None
    reservation_id: str | None = None
    retry_at: datetime | None = None
    recheck_request: str | None = None
    context_digest: str | None = None


class QQWatcher:
    def __init__(self, settings: WatcherSettings, *, desktop=None, decide=decide_qq_reply):
        self.settings = settings
        self.session = QQSessionSettings.model_validate_json(settings.session_config_path.read_text(encoding="utf-8"))
        if settings.group_id not in self.session.allowed_group_ids or settings.self_member_id != self.session.self_member_id:
            raise ValueError("Watcher scope must match the persistent QQ session")
        if not self.session.decision_enabled:
            raise ValueError("Autonomous speech decisions must be enabled")
        if not self.session.memory_db.is_file():
            raise ValueError("Existing persistent QQ memory is required")
        self.store = QQVisualMemoryStore(self.session.memory_db)
        from harness.qq.watch_ledger import WatchLedger
        self.first_deployment = not settings.state_db.exists()
        self.ledger = WatchLedger(settings.state_db, group_id=settings.group_id)
        self.ledger.start_run()
        if settings.recover_attribution_event_id:
            self.ledger.recover_unbound_attribution_event(settings.recover_attribution_event_id)
        self.desktop = desktop
        self.decide = decide
        self.last_digest = None
        self.last_items = []
        self.observer = None
        self._evidence_desktop = None
        self._observation_lock = threading.RLock()
        self._observer_sequence = 0
        self._observer_baselined = False
        self._observer_gap_recovery_attempts = 0
        self._observer_gap_resume_mono = 0.0
        self._attribution_retry = False
        self._identity_prepared = False
        self._identity_cache_invalidated_gaps = deque(maxlen=64)
        self._identity_cache_gap_generation = 0
        self._pending_turn: PendingTurn | None = None
        self._carry_first_seen = None
        self._expired_observation = None
        self._replans = 0
        self._traces = {}
        self._latencies = deque(maxlen=200)
        self._self_aliases = []
        self._trace_file = settings.trace_file or settings.status_file.with_name("watcher-trace.jsonl")
        self._active_preparation = None
        self._preparation_phase = "idle"
        self._guard = threading.RLock()
        self._heartbeat_failed = False
        self.status = {
            "service": "qq-desktop-watcher-v1", "pid": os.getpid(), "started_at": now().isoformat(),
            "build_hash": runtime_source_hash(),
            "running": True, "automatic_screen_capture": False, "automatic_send": False,
            "group_id": settings.group_id, "self_member_id": settings.self_member_id,
            "state": "starting", "last_error_type": None, "last_capture_at": None,
            "last_decision_action": None, "last_delivery_at": None, "captures": 0, "decisions": 0, "deliveries": 0,
            "config_path": None, "state_db": str(settings.state_db),
        }

    def set_status(self, **updates):
        with self._guard:
            self.status.update(updates)

    def heartbeat(self):
        with self._guard:
            if self.observer is not None:
                self.status["observer"] = self.observer.status()
            if self._active_preparation is not None:
                self.status["preparation"] = self._preparation_summary(self._active_preparation, outcome="running")
            value = self.status | {"heartbeat_at": now().isoformat()}
        atomic_json(self.settings.status_file, value)

    def should_stop(self) -> bool:
        if self._heartbeat_failed:
            return True
        try:
            if not self.settings.control_file.is_file():
                return False
            if self.settings.control_file.stat().st_size > 65536:
                return True
            return json.loads(self.settings.control_file.read_text(encoding="utf-8")).get("paused") is not False
        except (OSError, ValueError, AttributeError):
            return True

    def check_control(self):
        if self.should_stop():
            raise InterruptedError("Watcher pause requested")

    def make_desktop(self):
        from harness.qq.desktop import DesktopBackend, DesktopConfig
        self.desktop = DesktopBackend(DesktopConfig(
            body_url=self.settings.body_url, body_token_file=self.settings.body_token_file,
            agent_url=self.settings.agent_url, agent_token_file=self.settings.agent_token_file,
            expected_group_id=self.settings.group_id, self_member_id=self.settings.self_member_id,
            vision_model=self.settings.vision_model, model_config_path=self.session.model_config_path,
            body_instance_id=self.settings.expected_instance_id, environment_revision=self.settings.expected_revision,
            window_id=self.settings.window_id, accessibility_enabled=self.settings.accessibility_enabled,
            identity_cache_enabled=self.settings.identity_cache_enabled,
            settle_seconds=self.settings.settle_seconds,
        ), should_stop=self.should_stop, observation_lock=self._observation_lock)

    def _desktop_turn(self, deadline=None, trace=None):
        return self.desktop.turn(deadline=deadline, trace=trace) if hasattr(self.desktop, "turn") else nullcontext()

    def _preparation_summary(self, trace, *, outcome, error=None):
        value = trace.snapshot()
        return {"trace_id": trace.trace_id, "phase": self._preparation_phase, "outcome": outcome,
                "first_seen_at": value["first_seen_at"], "first_seen_mono": value["first_seen_mono"],
                "observation_sequence": value.get("observation_sequence"), "elapsed_ms": value["elapsed_ms"],
                "counters": value["counters"], "last_error_type": error}

    @contextmanager
    def _observation_preparation(self, *, first_seen_at, first_seen_mono, phase="capture", observation_sequence=None):
        trace = QQTurnTrace(first_seen_at=first_seen_at, first_seen_mono=first_seen_mono,
                            observation_sequence=observation_sequence)
        trace.add_counter("preparation_attempts")
        with self._guard:
            self._active_preparation, self._preparation_phase = trace, phase
            self.set_status(preparation=self._preparation_summary(trace, outcome="running"))
        error, outcome = None, "ok"
        try:
            yield trace
        except BaseException as exc:
            error = error_code(exc)
            outcome = "expired" if isinstance(exc, QQDeadlineExpired) else "error"
            trace.record("observation_completed", outcome=outcome, error_type=classify_error(exc))
            raise
        else:
            trace.record("observation_completed")
        finally:
            with self._guard:
                self.set_status(preparation=self._preparation_summary(trace, outcome=outcome, error=error))
                self._active_preparation = None
            try:
                trace.flush(self._trace_file.with_suffix(".observation.jsonl"))
            except (OSError, ValueError):
                self.set_status(preparation_trace_error="preparation_trace_unavailable")
                if error is None:
                    raise

    def _audit_observation_expiry(self, *, first_seen_at, first_seen_mono, observation_sequence=None,
                                  counter="expired_observations", outcome="expired"):
        trace = QQTurnTrace(first_seen_at=first_seen_at, first_seen_mono=first_seen_mono,
                            observation_sequence=observation_sequence)
        trace.add_counter(counter)
        trace.record("observation_completed", outcome=outcome,
                     error_type="deadline_expired" if outcome == "expired" else "internal_error")
        # Retirement must not outrun its audit. No GUI/model/candidate is created.
        trace.flush(self._trace_file.with_suffix(".observation.jsonl"), durable=True)
        self.set_status(last_observation_outcome=outcome, last_observation_trace_id=trace.trace_id)

    def _retire_expired_observations(self, arrivals):
        while arrivals and time.monotonic() - arrivals[0].first_seen_mono >= self.settings.turn_deadline_seconds:
            expired = arrivals[0]
            self._audit_observation_expiry(first_seen_at=expired.first_seen_at,
                first_seen_mono=expired.first_seen_mono, observation_sequence=expired.sequence)
            self._expired_observation = expired
            self._observer_sequence = expired.sequence
            arrivals.pop(0)
        if self._carry_first_seen is not None:
            at, mono = self._carry_first_seen
            if time.monotonic() - mono >= self.settings.turn_deadline_seconds:
                self._audit_observation_expiry(first_seen_at=at, first_seen_mono=mono,
                                               counter="expired_carry_origins")
                self._carry_first_seen = None
        # Deferred identity lookups retain their original candidate clocks.
        for event_id, trace in list(self._traces.items()):
            if any(name.startswith("turn_") for name in trace.snapshot()["counters"]):
                continue
            if time.monotonic() - trace.first_seen_mono < self.settings.turn_deadline_seconds:
                continue
            event = self.ledger.get_event(event_id)
            if event is not None and event.state == "observed":
                self.ledger.finish_event(event_id, outcome="expired", reason="attribution_deadline_expired")
                self._finish_trace(trace, "failed")
        return arrivals

    def _exclude_expired_window(self, *, items, frame_signature, first_seen_at, first_seen_mono, observation_sequence):
        """Use old AX content only to exclude arrivals, never to authorize them."""
        from harness.qq.accessibility import AccessibilityUnavailable, chat_observation_items
        from harness.qq.watch_ledger import VisibleItem, plan_visible_transition
        expired = self._expired_observation
        if expired is None:
            return False
        try:
            old_items = [VisibleItem(direction=item["direction"],
                kind="text" if item["kind"] == "text" else "sticker",
                text=item["text"] if item["kind"] == "text" and item["text"] else "[non-text]",
                author_key=item["author_key"]) for item in chat_observation_items(expired.tree)]
        except AccessibilityUnavailable:
            old_items = None
        safe = False
        if old_items is not None:
            signature = "observer-expired:" + expired.digest
            batch = self.ledger.observe(frame_signature=signature, items=old_items,
                observed_at=now(), first_seen_at=expired.first_seen_at)
            pending = self.ledger.pending_attribution(items=old_items, frame_signature=signature,
                                                      now=now(), max_age_seconds=86400)
            for event in {e.event_id: e for e in [*batch.events, *pending]}.values():
                if event.state == "observed":
                    self.ledger.finish_event(event.event_id, outcome="expired", reason="expired_observer_window")
            plan = plan_visible_transition(old_items, items, frame_signature=frame_signature,
                                            previous_frame_signature=signature)
            safe = plan.status in {"new", "unchanged"}
        if not safe:
            # No ordered anchor proves which visible text is newer. Record
            # the loss explicitly, then retain this window without sending.
            self._audit_observation_expiry(first_seen_at=first_seen_at, first_seen_mono=first_seen_mono,
                observation_sequence=observation_sequence, counter="unresolved_observation_baselines", outcome="error")
            batch = self.ledger.observe(frame_signature=frame_signature, items=items,
                observed_at=now(), first_seen_at=expired.first_seen_at)
            pending = self.ledger.pending_attribution(items=items, frame_signature=frame_signature,
                                                      now=now(), max_age_seconds=86400)
            for event in {e.event_id: e for e in [*batch.events, *pending]}.values():
                if event.state == "observed":
                    self.ledger.finish_event(event.event_id, outcome="expired", reason="unresolved_expired_window")
        self._expired_observation = None
        return not safe

    def _cleanup_owned_draft(self, turn) -> bool:
        """Bound UI cleanup independently; never renew a candidate's send deadline."""
        text = getattr(turn.decision, "reply", None)
        cleanup = getattr(self.desktop, "discard_owned_draft", None)
        if not text or (hasattr(self.desktop, "_owned_draft") and self.desktop._owned_draft is None):
            return True
        if cleanup is None and not hasattr(self.desktop, "_owned_draft"):
            # Injected read-only/test adapters do not acknowledge draft ownership.
            return True
        cleanup_deadline = QQDeadline.after(15, clock=time.monotonic)
        previous_state, previous_send = self.status["state"], self.status["automatic_send"]
        self.set_status(state="cleaning_owned_draft", automatic_send=False)
        try:
            with self._desktop_turn(cleanup_deadline, turn.trace), turn.trace.stage("owned_draft_cleanup"):
                cleanup_deadline.check()
                if cleanup is None or cleanup(text) is not True:
                    raise RuntimeError("owned_draft_cleanup_not_verified")
                cleanup_deadline.check()
            self.set_status(state=previous_state, automatic_send=previous_send)
            return True
        except Exception:
            turn.trace.add_counter("owned_draft_cleanup_failed")
            self.set_status(state="draft_cleanup_required", automatic_screen_capture=False,
                            automatic_send=False, last_error_type="owned_draft_cleanup_required")
            try:
                atomic_json(self.settings.control_file, {"paused": True, "reason": "owned_draft_cleanup_required"})
            except Exception:
                # The existing fail-stop path still blocks actions if pause persistence fails.
                self._heartbeat_failed = True
            return False

    def _finish_trace(self, trace, outcome):
        trace.add_counter("turn_" + outcome)
        trace.record("turn_completed", outcome="ok" if outcome in {"sent", "natural_wait", "rate_limited", "superseded"} else "error")
        trace.flush(self._trace_file)
        value = trace.snapshot()
        self.set_status(last_turn_outcome=outcome, last_trace_id=trace.trace_id, backlog=0)
        if outcome == "sent":
            receipt = next(stage for stage in reversed(value["stages"]) if stage["name"] == "receipt_persisted")
            self._latencies.append(receipt["end_offset_ms"])
            values = sorted(self._latencies)
            self.set_status(latency_ms={"samples": len(values), "p50": values[len(values)//2],
                                      "p95": values[math.ceil(len(values)*.95)-1], "max": values[-1]})

    def _new_trace(self, event, first_seen_at, first_seen_mono=None):
        prior = self._traces.get(event.event_id)
        if prior is not None:
            return prior
        age = max(0, (now() - first_seen_at).total_seconds())
        trace = QQTurnTrace(event_id=event.event_id, first_seen_at=first_seen_at,
                            first_seen_mono=first_seen_mono if first_seen_mono is not None else max(0, time.monotonic() - age))
        self._traces[event.event_id] = trace
        while len(self._traces) > 128:
            self._traces.pop(next(iter(self._traces)))
        return trace

    def _invalidate_identity_after_observer_gap(self, observer_status):
        if not self.settings.identity_cache_enabled or not isinstance(observer_status, dict):
            return

        def gap_key(started_at, sequence, duration):
            if (not isinstance(started_at, str) or not 1 <= len(started_at) <= 80
                    or type(sequence) is not int or sequence < 1
                    or type(duration) not in (int, float) or not math.isfinite(duration)
                    or duration < 10):
                return None
            try:
                started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                if started.utcoffset() is None:
                    return None
                return started.astimezone(timezone.utc).isoformat(), sequence
            except (ValueError, OverflowError):
                return None

        candidates = []
        recent = observer_status.get("recent_gaps")
        if isinstance(recent, list):
            for gap in recent[-64:]:
                if isinstance(gap, dict):
                    candidates.append(gap_key(gap.get("started_at"), gap.get("sequence"),
                                              gap.get("duration_seconds")))
        # The observer assigns a sequence when closing a gap. While it is open,
        # gap_sequence is the completed count, so +1 identifies that same gap.
        completed_sequence = observer_status.get("gap_sequence")
        if type(completed_sequence) is int and completed_sequence >= 0:
            candidates.append(gap_key(observer_status.get("gap_started_at"), completed_sequence + 1,
                                      observer_status.get("gap_seconds")))
        for key in candidates:
            if key is None or key in self._identity_cache_invalidated_gaps:
                continue
            self.desktop.invalidate_identity_cache()
            self._identity_prepared = False
            self._self_aliases = []
            self._identity_cache_invalidated_gaps.append(key)
            self._identity_cache_gap_generation = getattr(self, "_identity_cache_gap_generation", 0) + 1

    def _newer_visible_context(self):
        if self.observer is None:
            return False
        observer_status = self.observer.status()
        self._invalidate_identity_after_observer_gap(observer_status)
        pending_digest = observer_status.get("pending_digest")
        if (pending_digest is not None and self._pending_turn is not None
                and pending_digest != self._pending_turn.context_digest):
            return True
        batch = self.observer.changes_after(self._observer_sequence)
        if batch.overflowed:
            self.set_status(observation_overflow=True, observation_missed_changes=batch.missed_changes)
            return True
        if not batch.observations:
            return False
        turn = self._pending_turn
        if turn is not None and turn.context_digest == batch.observations[-1].digest:
            self._observer_sequence = batch.latest_sequence
            return False
        return True

    def _supersede_pending(self):
        turn = self._pending_turn
        if turn is None:
            return True
        cleaned = self._cleanup_owned_draft(turn)
        if turn.reservation_id:
            self.ledger.supersede_send(turn.reservation_id, now=now())
        else:
            self.ledger.finish_event(turn.event.event_id, outcome="superseded", reason="newer_context")
        if cleaned and self._replans < 1:
            self._carry_first_seen = (turn.trace.first_seen_at, turn.trace.first_seen_mono)
            self._replans += 1
        else:
            self._carry_first_seen = None
            self._replans = 0
        self._finish_trace(turn.trace, "superseded" if cleaned else "failed")
        self._pending_turn = None
        return cleaned

    def _retain_click_timing(self, turn, *, acknowledged_at=None, observed_at=None, evidence_ref=None):
        reservation = self.ledger.get_send(turn.reservation_id)
        if not reservation.click_timing_required:
            return
        if reservation.click_acknowledged_at is None and reservation.click_observed_at is None:
            if acknowledged_at is not None:
                reservation = self.ledger.mark_click_acknowledged(turn.reservation_id,
                    acknowledged_at=acknowledged_at, now=now())
            elif observed_at is not None:
                reservation = self.ledger.mark_click_observed(turn.reservation_id,
                    observed_at=observed_at, evidence_ref=evidence_ref, now=now())
            else:
                raise RuntimeError("click_timing_unavailable")
        label = "send_click_acknowledged" if reservation.click_acknowledged_at is not None else "send_click_observed"
        if not any(stage["name"] == label for stage in turn.trace.snapshot()["stages"]):
            turn.trace.record(label)

    def _persist_delivery(self, turn, result, *, reconcile=False):
        self._retain_click_timing(turn, acknowledged_at=getattr(result, "click_acknowledged_at", None),
            observed_at=getattr(result, "click_observed_at", None),
            evidence_ref=getattr(result, "click_observed_evidence_ref", None))
        with turn.trace.stage("receipt_storage"):
            receipt = record_qq_delivery(memory_db=self.session.memory_db, group_id=self.settings.group_id,
                self_member_id=self.settings.self_member_id, delivered_text=turn.decision.reply,
                evidence_refs=result.evidence_refs, verification="qq_ui_verified",
                decision_id=turn.decision.decision_id, source_record_ids=[turn.source_id], delivered_at=result.delivered_at)
        if not any(stage["name"] == "receipt_persisted" and stage["outcome"] == "ok"
                   for stage in turn.trace.snapshot()["stages"]):
            turn.trace.record("receipt_persisted")
        if receipt is not None:
            self.ledger.reconcile_sent(turn.reservation_id, receipt=receipt, now=now())
        else:
            # Compatibility for injected test collectors; production always returns a receipt.
            self.ledger.mark_sent(turn.reservation_id, evidence_refs=result.evidence_refs, now=result.delivered_at)
        self.set_status(last_delivery_at=result.delivered_at.isoformat(), deliveries=self.status["deliveries"]+1,
                        state="watching", last_error_type=None, automatic_send=self.settings.automatic_send)
        self._finish_trace(turn.trace, "sent")
        self._pending_turn = None
        self._replans = 0

    def _recover_existing_receipts(self):
        for reservation in self.ledger.unsettled_sends():
            receipt = get_qq_delivery(memory_db=self.session.memory_db, group_id=self.settings.group_id,
                                      decision_id=reservation.decision_id)
            if receipt is not None:
                self.ledger.reconcile_sent(reservation.reservation_id, receipt=receipt, now=now())

    def _attempt_delivery(self, turn):
        attempt = self.ledger.start_send_attempt(turn.reservation_id, now=now())
        if attempt is None:
            self._cleanup_owned_draft(turn)
            self._finish_trace(turn.trace, "failed")
            self._pending_turn = None
            return
        clicked = False
        # The actual backend always opts in; legacy injected collectors retain
        # their existing delivery signature.
        click_timing_required = getattr(self.desktop, "supports_click_acknowledgement", False) is True

        def before_click():
            nonlocal clicked
            self.check_control()
            turn.deadline.check()
            identity_version = (getattr(self.desktop, "_identity_epoch", None),
                                self._identity_cache_gap_generation)
            newer_context = self._newer_visible_context()
            if (self.settings.identity_cache_enabled
                    and identity_version != (getattr(self.desktop, "_identity_epoch", None),
                                             self._identity_cache_gap_generation)):
                from harness.qq.desktop import PreClickIdentityInvalidated
                raise PreClickIdentityInvalidated()
            if newer_context:
                raise RuntimeError("newer_context_before_click")
            proof = getattr(self.desktop, "last_preclick_proof", None)
            if proof is None:
                raise RuntimeError("preclick_proof_missing")
            proof = dict(proof) | {"source_record_ids": [turn.source_id]}
            self.ledger.mark_click_started(turn.reservation_id, proof=proof, now=now(),
                                           require_click_timing=click_timing_required)
            clicked = True
            turn.trace.record("send_click_started")

        def after_click(acknowledged_at):
            self._retain_click_timing(turn, acknowledged_at=acknowledged_at)

        self.set_status(state="sending")
        try:
            if attempt.attempt_number > 1:
                with self._desktop_turn(turn.deadline, turn.trace), turn.trace.stage("retry_relocation"):
                    turn.verification = self.desktop.verify_group()
            with self._desktop_turn(turn.deadline, turn.trace), turn.trace.stage("delivery"):
                click_options = {"after_click": after_click} if click_timing_required else {}
                result = self.desktop.deliver(turn.decision.reply, reservation_id=turn.reservation_id,
                    expected_content_digest=turn.frame.content_digest, expected_context_frame=turn.frame,
                    group_verification=turn.verification, before_click=before_click, **click_options)
            if result.status == "sent" and clicked:
                self._persist_delivery(turn, result)
                return
            if not clicked and result.status == "not_sent":
                if turn.deadline.expired:
                    reason, retryable = "turn_deadline_expired", False
                else:
                    reason = result.reason_code
                    retryable = getattr(result, "phase", "precheck") == "precheck" and getattr(result, "retryable", False)
                reserved = self.ledger.defer_preclick(turn.reservation_id, reason=reason, retryable=retryable,
                    evidence_refs=getattr(result, "evidence_refs", []), now=now())
                self.set_status(state="retrying_precheck" if reserved.status == "retry_wait" else "watching", last_error_type=reason)
                if reserved.status != "retry_wait":
                    self._cleanup_owned_draft(turn)
                    self._finish_trace(turn.trace, "failed")
                    self._pending_turn = None
                else:
                    turn.retry_at = reserved.next_retry_at
                    turn.trace.add_counter("precheck_retries")
                    turn.trace.flush(self._trace_file)
                return
            if not clicked:
                # This callback is the sole durable click boundary. If it never
                # completed, no GUI click can have been issued by DesktopBackend.
                self.ledger.defer_preclick(turn.reservation_id, reason="callback_not_committed", retryable=False,
                    evidence_refs=getattr(result, "evidence_refs", []), now=now())
                if self._newer_visible_context():
                    self._carry_first_seen = (turn.trace.first_seen_at, turn.trace.first_seen_mono) if self._replans < 1 else None
                    self._replans += 1
                self._cleanup_owned_draft(turn)
                self._finish_trace(turn.trace, "failed")
                self._pending_turn = None
                return
            self.ledger.record_postclick_evidence(turn.reservation_id, evidence_refs=getattr(result, "evidence_refs", []))
            self.ledger.mark_uncertain(turn.reservation_id, reason=result.reason_code, now=now())
            self.set_status(state="reconciling_delivery", automatic_send=False, last_error_type=result.reason_code)
            proof = self.ledger.get_send(turn.reservation_id).preclick_proof
            # Only this process retains continuous UI ownership. A restart may
            # use an existing receipt, but cannot infer one from matching text.
            if proof is not None and hasattr(self.desktop, "reconcile_delivery"):
                reconciliation = QQDeadline.after(30)
                for pause in (0, 1, 3):
                    stop_at = time.monotonic() + pause
                    while time.monotonic() < stop_at:
                        self.check_control()
                        time.sleep(min(.2, max(0, stop_at-time.monotonic())))
                    with self._desktop_turn(reconciliation, turn.trace), turn.trace.stage("reconciliation"):
                        result = self.desktop.reconcile_delivery(turn.decision.reply,
                            reservation_id=turn.reservation_id, proof=proof.model_dump(mode="json"))
                    self.ledger.record_postclick_evidence(turn.reservation_id, evidence_refs=getattr(result, "evidence_refs", []))
                    if result.status == "sent":
                        self._persist_delivery(turn, result, reconcile=True)
                        return
            self.set_status(state="delivery_uncertain", automatic_send=False)
            self._finish_trace(turn.trace, "failed")
            self._pending_turn = None
        except InterruptedError:
            if clicked:
                self.ledger.mark_uncertain(turn.reservation_id, reason="pause_after_click", now=now())
            else:
                self.ledger.defer_preclick(turn.reservation_id, reason="pause_before_click", retryable=False, now=now())
                self._cleanup_owned_draft(turn)
            self._finish_trace(turn.trace, "failed")
            self._pending_turn = None
            raise
        except Exception as exc:
            if clicked:
                current = self.ledger.get_send(turn.reservation_id)
                if current.status != "sent":
                    self.ledger.mark_uncertain(turn.reservation_id, reason=type(exc).__name__, now=now())
                self.set_status(automatic_send=False, state="delivery_uncertain", last_error_type=type(exc).__name__)
                receipt = get_qq_delivery(memory_db=self.session.memory_db, group_id=self.settings.group_id,
                                          decision_id=turn.decision.decision_id)
                if receipt is not None:
                    self.ledger.reconcile_sent(turn.reservation_id, receipt=receipt, now=now())
                    if not any(stage["name"] == "receipt_persisted" and stage["outcome"] == "ok"
                               for stage in turn.trace.snapshot()["stages"]):
                        turn.trace.record("receipt_persisted")
                    turn.trace.add_counter("receipt_recoveries")
                    if not turn.trace.snapshot()["counters"].get("turn_sent"):
                        self._finish_trace(turn.trace, "sent")
                    else:
                        # The terminal counter may already exist if only the
                        # first flush failed after recording the durable receipt.
                        turn.trace.flush(self._trace_file)
                        self.set_status(last_turn_outcome="sent", last_trace_id=turn.trace.trace_id, backlog=0)
                    self.set_status(last_delivery_at=receipt.delivered_at.isoformat(), deliveries=self.status["deliveries"]+1,
                                    state="watching", automatic_send=self.settings.automatic_send, last_error_type=None)
                    self._pending_turn = None
                    return
                self.ledger.mark_uncertain(turn.reservation_id, reason=type(exc).__name__, now=now())
                self.set_status(automatic_send=False, state="delivery_uncertain", last_error_type=type(exc).__name__)
            else:
                self.ledger.defer_preclick(turn.reservation_id, reason=type(exc).__name__, retryable=False, now=now())
                if not self._cleanup_owned_draft(turn):
                    self._finish_trace(turn.trace, "failed")
                    self._pending_turn = None
                    raise InterruptedError("owned_draft_cleanup_required") from exc
            self._finish_trace(turn.trace, "failed")
            self._pending_turn = None
            raise

    def _decide_turn(self, turn):
        self.check_control()
        turn.deadline.check()
        self.set_status(state="deciding")
        with turn.trace.stage("decision"):
            decision = self.decide(memory_db=self.session.memory_db, group_id=self.settings.group_id,
                trigger_member_id=turn.member_id, self_member_id=self.settings.self_member_id,
                config_path=self.session.model_config_path, preset_path=self.session.preset_path,
                deadline=turn.deadline, trace=turn.trace, verified_self_aliases=self._self_aliases,
                request=turn.recheck_request)
        self.check_control()
        turn.decision = decision
        turn.trace.bind_decision(decision.decision_id)
        self.set_status(last_decision_action=decision.action, decisions=self.status["decisions"]+1)
        outcome = getattr(decision, "outcome", "cached_legacy")
        if outcome in {"model_error", "model_invalid", "expired", "pending"} or turn.deadline.expired:
            self.ledger.finish_event(turn.event.event_id, outcome="expired" if turn.deadline.expired or outcome == "expired" else "failed", reason=outcome)
            self._finish_trace(turn.trace, "failed")
            self._pending_turn = None
            return
        if self._newer_visible_context():
            self._supersede_pending()
            return
        self.ledger.attach_decision(turn.event.event_id, decision_id=decision.decision_id, action=decision.action,
                                    new_decision=decision.new_decision, replayed=decision.replayed)
        if decision.action != "speak" or not decision.new_decision or decision.replayed or outcome == "cached_legacy":
            self._finish_trace(turn.trace, "natural_wait" if outcome == "natural_wait" else "guard_wait")
            self._pending_turn = None
            self.set_status(state="watching")
            return
        if not self.settings.automatic_send or self.ledger.has_uncertain_send():
            self._finish_trace(turn.trace, "send_disabled")
            self._pending_turn = None
            return
        priority = getattr(decision, "addressing", "ambient")
        allowed_at = self.ledger.next_allowed_at(priority=priority)
        if allowed_at is not None and allowed_at > now():
            self.ledger.finish_event(turn.event.event_id, outcome="rate_limited", reason="cadence_wait")
            turn.retry_at = allowed_at
            turn.trace.add_counter("rate_limited")
            turn.trace.flush(self._trace_file)
            self.set_status(state="rate_limited", rate_limit_until=allowed_at.isoformat())
            return
        reservation = self.ledger.reserve_send(turn.event.event_id, decision_id=decision.decision_id,
            priority=priority, reply_sha256=hashlib.sha256(decision.reply.encode("utf-8")).hexdigest(),
            self_member_id=self.settings.self_member_id, now=now())
        if reservation is None:
            self._finish_trace(turn.trace, "rate_limited")
            self._pending_turn = None
            return
        turn.reservation_id = reservation.reservation_id
        self._attempt_delivery(turn)

    def _expire_pending(self) -> bool:
        turn = self._pending_turn
        if turn is None or not turn.deadline.expired:
            return True
        cleaned = self._cleanup_owned_draft(turn)
        if turn.reservation_id:
            self.ledger.start_send_attempt(turn.reservation_id, now=now())
        else:
            self.ledger.finish_event(turn.event.event_id, outcome="expired", reason="turn_deadline_expired")
        self._finish_trace(turn.trace, "failed")
        self._pending_turn = None
        return cleaned

    def _recover_observer_gap(self, observer_status) -> None:
        """Recover a known card, never an unknown dialog or pending evidence read."""
        eligible = (not self._observer_baselined or self._pending_turn is not None or self._attribution_retry
                    or self.status.get("last_turn_outcome") == "failed")
        if (not eligible or observer_status.get("running") is not True
                or observer_status.get("last_error_type") is not None
                or observer_status.get("gap_started_at") is None
                or observer_status.get("evidence_in_progress") is True
                or observer_status.get("pending_digest") is not None
                or time.monotonic() < self._observer_gap_resume_mono):
            return
        if self._observer_gap_recovery_attempts >= 2:
            self.set_status(state="observer_recovery_exhausted", last_error_type="observer_gap_recovery_exhausted")
            return
        self.check_control()
        turn = self._pending_turn
        budget = min(15.0, turn.deadline.remaining()) if turn is not None else 15.0
        if budget <= 0:
            return
        deadline = QQDeadline.after(budget, clock=time.monotonic)
        trace = turn.trace if turn is not None else None
        self._observer_gap_recovery_attempts += 1
        self.set_status(state="recovering_observer_gap", automatic_send=False,
                        observer_gap_recovery_attempts=self._observer_gap_recovery_attempts)
        try:
            preparation_context = (nullcontext(trace) if trace is not None else
                self._observation_preparation(first_seen_at=now(), first_seen_mono=time.monotonic(), phase="gap_recovery"))
            with preparation_context as measured_trace, self._desktop_turn(deadline, measured_trace), measured_trace.stage("observer_gap_recovery"):
                if trace is None:
                    measured_trace.add_counter("observer_gap_recovery_attempts")
                deadline.check()
                frame = self.desktop.capture()
                self.set_status(last_capture_at=frame.captured_at.isoformat(), captures=self.status["captures"] + 1)
                screen = self.desktop.inspect(frame)
                deadline.check()
                profile = getattr(screen, "opened_profile", None)
                if screen.scene == "group_chat" and not screen.blocked and profile is None:
                    self.set_status(state="waiting_for_observer", last_error_type=None)
                    return  # The sensor, not another group-card click, must publish chat.
                kind = {"group_profile": "group", "member_profile": "member"}.get(screen.scene)
                if (screen.blocked or kind is None or profile is None or profile.kind != kind
                        or profile.uncertainties or getattr(screen, "uncertainties", [])):
                    self.set_status(state="observer_gap_unrecognized", last_error_type="observer_gap_unrecognized")
                    return
                self.check_control()
                deadline.check()
                verification = self.desktop.verify_group(frame=frame)
                deadline.check()
                if verification.group_id != self.settings.group_id:
                    raise RuntimeError("observer_recovery_group_mismatch")
                self.set_status(state="waiting_for_observer", last_error_type=None)
        except InterruptedError:
            raise
        except Exception as exc:
            self.set_status(state="observer_gap_recovery_failed", last_error_type=error_code(exc))
        finally:
            # An actor-created card transition must settle before another attempt.
            self._observer_gap_resume_mono = time.monotonic() + 3.0
            if trace is not None:
                trace.add_counter("observer_gap_recovery_attempts")
                trace.flush(self._trace_file)

    def cycle(self):
        from harness.qq.watch_ledger import VisibleItem
        self.check_control()
        # Sensor gaps must not keep an old candidate alive indefinitely.
        if not self._expire_pending():
            return
        snapshot = None
        if self.observer is not None:
            snapshot = self.observer.latest()
            observer_status = self.observer.status()
            self._invalidate_identity_after_observer_gap(observer_status)
            self.set_status(observer=observer_status)
            if snapshot is None:
                observer_error = observer_status.get("last_error_type")
                diagnostic = observer_status.get("signature_error_type") or observer_error
                self.set_status(state="observer_unavailable" if observer_error else "waiting_for_observer",
                                last_error_type=diagnostic, automatic_screen_capture=False, automatic_send=False)
                self._recover_observer_gap(observer_status)
                self._expire_pending()
                return
            if observer_status.get("last_error_type"):
                self.set_status(state="observer_unavailable", automatic_screen_capture=False, automatic_send=False,
                                last_error_type=observer_status.get("signature_error_type") or observer_status["last_error_type"])
                return
            if self.status["state"] == "observer_unavailable":
                self.set_status(last_error_type=None)
            if time.monotonic() < self._observer_gap_resume_mono:
                self.set_status(state="waiting_for_observer", automatic_screen_capture=False, automatic_send=False)
                return
            self._observer_gap_recovery_attempts = 0
            self.set_status(observer_gap_recovery_attempts=0)
        if self._pending_turn is not None:
            turn = self._pending_turn
            if self._newer_visible_context():
                if not self._supersede_pending():
                    return
            elif turn.retry_at is not None and turn.retry_at > now():
                return
            elif turn.reservation_id:
                self._attempt_delivery(turn)
                return
            else:
                event = self.ledger.reopen_rate_limited(turn.event.event_id, now=now())
                if event is None:
                    self._finish_trace(turn.trace, "rate_limited")
                    self._pending_turn = None
                    return
                # A new request, not a cached draft; retain the original first-seen time.
                cold = turn.trace.snapshot()["counters"].get("cold_path", 0)
                self._finish_trace(turn.trace, "rate_limited")
                turn.trace = QQTurnTrace(event_id=event.event_id, first_seen_at=turn.trace.first_seen_at,
                                         first_seen_mono=turn.trace.first_seen_mono)
                turn.trace.add_counter("cold_path", cold)
                turn.event = event
                turn.retry_at = None
                turn.recheck_request = "发送冷却已经结束。请依据当前已核验记录重新决定是否仍有必要回应，不沿用此前草稿。"
                self._decide_turn(turn)
                return
        changes = self.observer.changes_after(self._observer_sequence) if self.observer is not None else None
        arrivals = self._retire_expired_observations(list(changes.observations) if changes is not None else [])
        if (snapshot is not None and self._observer_baselined
                and snapshot.sequence <= self._observer_sequence and not self._attribution_retry
                and self._expired_observation is None):
            self.set_status(state="watching", automatic_screen_capture=True,
                            automatic_send=self.settings.automatic_send and not self.ledger.has_uncertain_send())
            return
        if changes is not None and changes.overflowed:
            self.set_status(observation_overflow=True, observation_missed_changes=changes.missed_changes)
        pipeline_started = time.monotonic()
        first_arrival = arrivals[0] if arrivals else snapshot if snapshot is not None and snapshot.sequence > self._observer_sequence else None
        # A baseline has no candidate TTL. Its old sensor timestamp must not
        # permanently expire initialization retries. Actual changes retain it.
        first_seen_at = first_arrival.first_seen_at if first_arrival is not None else now()
        first_seen_mono = first_arrival.first_seen_mono if first_arrival is not None else pipeline_started
        if self.last_digest is not None:
            known_pending = self.ledger.pending_attribution(items=self.last_items, frame_signature=self.last_digest,
                                                           now=now(), max_age_seconds=60)
            for pending_event in known_pending:
                pending_at = pending_event.first_seen_at or pending_event.observed_at
                prior_trace = self._traces.get(pending_event.event_id)
                pending_mono = (prior_trace.first_seen_mono if prior_trace is not None
                                else max(0, pipeline_started - max(0, (now() - pending_at).total_seconds())))
                first_seen_at, first_seen_mono = min(first_seen_at, pending_at), min(first_seen_mono, pending_mono)
        if self._carry_first_seen is not None:
            carry_at, carry_mono = self._carry_first_seen
            first_seen_at = min(first_seen_at, carry_at)
            first_seen_mono = min(first_seen_mono, carry_mono)
        accepted_sequence = snapshot.sequence if snapshot is not None else self._observer_sequence
        observation_deadline = QQDeadline(first_seen_mono + self.settings.turn_deadline_seconds, clock=time.monotonic)
        observation_sequence = first_arrival.sequence if first_arrival is not None else None
        with self._observation_preparation(first_seen_at=first_seen_at, first_seen_mono=first_seen_mono,
                                           observation_sequence=observation_sequence) as preparation:
            with self._desktop_turn(observation_deadline, preparation):
                self.set_status(state="capturing")
                with preparation.stage("initial_capture"):
                    observation_deadline.check()
                    frame = self.desktop.capture()
                    observation_deadline.check()
                self.set_status(last_capture_at=frame.captured_at.isoformat(), captures=self.status["captures"]+1,
                                automatic_screen_capture=True)
                if self.observer is None and frame.content_digest == self.last_digest:
                    blocked = self.ledger.has_uncertain_send()
                    pending = self.ledger.pending_attribution(items=self.last_items, frame_signature=frame.content_digest,
                                                              now=now(), max_age_seconds=60)
                    if not pending:
                        self.set_status(state="watching_send_blocked" if blocked else "watching",
                                        automatic_send=self.settings.automatic_send and not blocked)
                        return
                self._preparation_phase = "group_verification"
                self.set_status(state="verifying_group")
                with preparation.stage("group_verification"):
                    observation_deadline.check()
                    verification = self.desktop.verify_group(frame=frame) if self.settings.accessibility_enabled else self.desktop.verify_group()
                    observation_deadline.check()
                if (self.settings.identity_cache_enabled or not self._identity_prepared) and hasattr(self.desktop, "verify_self"):
                    self._preparation_phase = "self_verification"
                    with preparation.stage("initial_self_verification"):
                        self.desktop.verify_self()
                        observation_deadline.check()
                    self._self_aliases = list(getattr(self.desktop, "verified_self_aliases", []))
                    self._identity_prepared = True
        self._attribution_retry = False
        self._carry_first_seen = None
        frame, screen = verification.chat_frame, verification.chat_screen
        context_digest = None
        if getattr(frame, "a11y_tree", None) is not None:
            from harness.qq.accessibility import chat_change_signature
            signature = chat_change_signature(frame.a11y_tree)
            context_digest = signature.chat_digest if signature.chat_visible else None
            latest = self.observer.latest() if self.observer is not None else None
            if latest is not None and latest.digest == context_digest:
                accepted_sequence = latest.sequence
        self.check_control()
        items = [VisibleItem(direction=m.direction, kind="text" if m.kind == "text" else "sticker",
                    text=m.text if m.kind == "text" and m.text else "[non-text]", author_key=m.display_name)
                 for m in screen.messages if m.direction in {"incoming", "outgoing"}]
        messages = [m for m in screen.messages if m.direction in {"incoming", "outgoing"}]
        # Ledger scheduling uses the collector clock. Remote screenshot and
        # accessibility acquisition timestamps remain in their own evidence.
        discarded = self._exclude_expired_window(items=items, frame_signature=frame.content_digest,
            first_seen_at=first_seen_at, first_seen_mono=first_seen_mono, observation_sequence=observation_sequence)
        batch = self.ledger.observe(frame_signature=frame.content_digest, items=items,
                                    observed_at=now(), first_seen_at=first_seen_at)
        self.last_digest, self.last_items = frame.content_digest, items
        if snapshot is not None:
            self._observer_sequence = accepted_sequence
            self._observer_baselined = True
        pending = self.ledger.pending_attribution(items=items, frame_signature=frame.content_digest,
                                                  now=now(), max_age_seconds=60)
        events = {event.event_id: event for event in [*batch.events, *pending]}
        allowed = self.settings.automatic_send and not self.ledger.has_uncertain_send()
        self.set_status(automatic_send=allowed, state="watching", last_transition=batch.plan.status,
                        last_error_type=None, backlog=len(events))
        if discarded:
            self.set_status(last_error_type="expired_window_unresolved", last_observation_outcome="error")
            return
        if not events:
            return
        bound = []
        self.set_status(state="attributing_members")
        for event in events.values():
            self.check_control()
            message = messages[event.item_index]
            if message.direction != "incoming" or message.kind != "text" or not message.text:
                self.ledger.mark_ignored(event.event_id, reason="non_text_or_self")
                continue
            first_seen = event.first_seen_at or event.observed_at
            trace = self._new_trace(event, first_seen, first_seen_mono if event.event_id in {e.event_id for e in batch.events} else None)
            trace.merge_preparation(preparation)
            deadline = QQDeadline(trace.first_seen_mono + self.settings.turn_deadline_seconds)
            try:
                deadline.check()
                with self._desktop_turn(deadline, trace), trace.stage("attribution"):
                    attributed = self.desktop.attribute(message, frame)
                if attributed.member_id == self.settings.self_member_id:
                    self.ledger.mark_ignored(event.event_id, reason="self_message")
                    continue
                known = bool(self.store.reply_context(group_id=self.settings.group_id, member_id=attributed.member_id, limit=1).profiles)
                if "cold_path" not in trace.snapshot()["counters"]:
                    trace.add_counter("cold_path", int(not known))
                with trace.stage("memory_storage"):
                    profile_id = getattr(attributed, "profile_record_id", None)
                    fresh_profile = profile_id is None
                    if fresh_profile:
                        profile = self.store.record_profile(QQVerifiedProfileInput(
                            group_id=self.settings.group_id, member_id=attributed.member_id, display_name=attributed.display_name,
                            display_name_source="qq_profile", verification="qq_ui_verified", capture_id=attributed.capture_id,
                            observed_at=attributed.observed_at, group_evidence=verification.evidence_refs,
                            member_evidence=attributed.profile_evidence_refs)).profile
                        profile_id = profile.record_id
                    observation = self.store.record_observation(QQVisualObservationInput(
                        group_id=self.settings.group_id, member_id=attributed.member_id, profile_record_id=profile_id,
                        capture_id=frame.capture_id, capture_item_id=event.event_id, observed_at=frame.captured_at,
                        text=message.text, evidence=[frame.evidence_ref], author_attribution="qq_ui_verified",
                        attribution_evidence=attributed.evidence_refs,
                        extraction_method=getattr(attributed, "attribution_method", "qq_visual_avatar_profile_v1"))).observation
                    if fresh_profile and self.settings.identity_cache_enabled and hasattr(self.desktop, "remember_verified_identity"):
                        self.desktop.remember_verified_identity(message, frame, attributed, profile_id)
                self.ledger.bind_event(event.event_id, source_record_id=observation.record_id)
                bound.append(PendingTurn(event, attributed.member_id, observation.record_id, frame, verification, deadline, trace,
                                         context_digest=context_digest))
            except InterruptedError:
                raise
            except Exception as exc:
                if deadline.expired or event.attribution_attempts >= 2:
                    self.ledger.finish_event(event.event_id, outcome="expired" if deadline.expired else "failed", reason="attribution_"+error_code(exc))
                    self._finish_trace(trace, "failed")
                else:
                    self.ledger.defer_attribution(event.event_id, reason="attribution_"+error_code(exc), now=now())
                    trace.add_counter("attribution_retries")
                    trace.flush(self._trace_file)
                    # Retry the same still-visible candidate without awaiting another message.
                    self._attribution_retry = True
                self.set_status(last_error_type=error_code(exc))
        if not bound:
            self.set_status(state="watching")
            return
        for earlier in bound[:-1]:
            self.ledger.mark_ignored(earlier.event.event_id, reason="considered_in_batch")
            self._finish_trace(earlier.trace, "batched")
        turn = bound[-1]
        self._pending_turn = turn
        self._decide_turn(turn)

    def run(self, *, once=False, config_path: Path | None = None):
        self.set_status(config_path=str(config_path) if config_path else None)
        stop_heartbeat = threading.Event()

        def beat():
            while not stop_heartbeat.is_set():
                try:
                    self.heartbeat()
                except OSError:
                    self._heartbeat_failed = True
                    self.set_status(automatic_send=False, state="heartbeat_failed", last_error_type="OSError")
                    return
                stop_heartbeat.wait(5)

        self.heartbeat()
        thread = threading.Thread(target=beat, daemon=True, name="qq-watcher-heartbeat")
        thread.start()
        try:
            self._recover_existing_receipts()
            if self.desktop is None:
                self.make_desktop()
            if self.settings.accessibility_enabled:
                from harness.qq.observer import QQScopedAccessibilityClient, ScopedQQObserver
                from harness.qq.accessibility import chat_change_signature
                from harness.qq.desktop import DesktopBackend
                self._evidence_desktop = DesktopBackend(self.desktop.config, should_stop=self.should_stop,
                                                       observation_lock=self._observation_lock)

                def capture_change_evidence():
                    with self._evidence_desktop.turn(deadline=QQDeadline.after(3)):
                        frame = self._evidence_desktop.capture()
                    return {"capture_id": frame.capture_id, "captured_at": frame.captured_at,
                            "evidence_ref": frame.evidence_ref, "content_digest": frame.content_digest,
                            "a11y_evidence_ref": frame.a11y_evidence_ref,
                            "a11y_digest": frame.a11y_tree.get("semantic_digest") if frame.a11y_tree else None}

                def retain_observer_gap(gap):
                    path = self._trace_file.with_suffix(".gaps.jsonl")
                    if path.is_symlink():
                        raise ValueError("observer_gap_path_invalid")
                    payload = {"schema_version": "qq-observer-gap-v1", **gap.as_dict()}
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())

                reader = QQScopedAccessibilityClient(agent_url=self.settings.agent_url,
                    token_file=self.settings.agent_token_file, body_instance_id=self.settings.expected_instance_id,
                    environment_revision=self.settings.expected_revision, window_id=self.settings.window_id,
                    app_id="org.mistyai.QQ", window_rect=(0,27,1280,693), causal_freshness=True,
                    read_lock=self._observation_lock, skip_obscured_content=True)
                self.observer = ScopedQQObserver(read_fn=reader.read, signature_fn=chat_change_signature,
                    poll_seconds=self.settings.observation_interval_seconds, close_fn=reader.close,
                    evidence_fn=capture_change_evidence, gap_fn=retain_observer_gap)
                self.observer.start()
            if self.first_deployment and self.settings.initial_baseline_observation is not None:
                # A first deployment may use a real screenshot captured before
                # the operator's live test. This is read-only; its coordinates
                # never drive a click. Every subsequent process starts afresh.
                from harness.qq.watch_ledger import VisibleItem
                self.check_control()
                self.set_status(state="reading_initial_baseline")
                baseline = self.desktop.load_baseline(self.settings.initial_baseline_observation)
                reading = self.desktop.inspect(baseline)
                if reading.scene != "group_chat" or reading.blocked:
                    raise ValueError("Initial screenshot is not an unobstructed group chat")
                items = [VisibleItem(direction=m.direction, kind="text" if m.kind == "text" else "sticker",
                                     text=m.text if m.kind == "text" and m.text else "[non-text]",
                                     author_key=m.display_name) for m in reading.messages if m.direction in {"incoming", "outgoing"}]
                self.ledger.observe(frame_signature=baseline.content_digest, items=items, observed_at=baseline.captured_at)
                self.last_digest = baseline.content_digest
            while not self.should_stop():
                try:
                    self.cycle()
                except InterruptedError:
                    break
                except Exception as exc:
                    self.set_status(state="retrying_capture", automatic_screen_capture=False, automatic_send=False,
                                    last_error_type=error_code(exc))
                if once:
                    break
                interval = self.settings.observation_interval_seconds if self.observer is not None else self.settings.poll_seconds
                deadline = time.monotonic() + interval
                while time.monotonic() < deadline and not self.should_stop():
                    time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        finally:
            stop_heartbeat.set()
            thread.join(timeout=6)
            if self.observer is not None:
                self.observer.close()
            if self._evidence_desktop is not None:
                self._evidence_desktop.close()
            if self.desktop is not None:
                self.desktop.close()
            self.set_status(running=False, automatic_screen_capture=False, automatic_send=False,
                            state="paused" if self.should_stop() else "stopped")
            self.heartbeat()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = WatcherSettings.model_validate_json(args.config.read_text(encoding="utf-8"))
        # One desktop, one owner: token-file identity prevents duplicate workers
        # even when launched with different state database paths.
        with worker_lock(settings.agent_token_file.with_suffix(".watcher.lock")):
            watcher = QQWatcher(settings)
            watcher.run(once=args.once, config_path=args.config.absolute())
        return 0
    except Exception as exc:
        print(f"QQ watcher stopped ({type(exc).__name__}); private configuration or desktop needs inspection.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
