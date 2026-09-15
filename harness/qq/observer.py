"""Independent, read-only QQ accessibility polling while a turn is processed.

The first readable frame is a baseline. Observations are sensor hints, never
member verification or authority to click, type or send. No state DB is opened.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from harness.qq.accessibility import AccessibilityUnavailable, ChatChangeSignature, chat_change_signature


MAX_TREE_BYTES = 4 * 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CODES = {"invalid_response", "response_too_large", "identity_mismatch", "scope_mismatch",
          "stale_response", "future_response", "invalid_timing", "request_timeout", "http_error",
          "client_closed", "invalid_signature", "accessibility_unavailable", "observer_error", "close_timeout"}
_CODES.update({"nonce_mismatch", "clock_skew", "clock_drift", "timestamp_replay"})
_CODES.update({"evidence_capture_failed", "evidence_metadata_invalid"})

# Only parser-owned literals may become diagnostics. Unknown exceptions and
# arbitrary AX strings remain the generic accessibility_unavailable category.
_SIGNATURE_ERROR_CODES = frozenset({
    "a11y_active_ancestor_missing", "a11y_ancestry_invalid", "a11y_avatar_geometry",
    "a11y_avatar_unbound", "a11y_capture_time_unbound", "a11y_chat_content_unavailable",
    "a11y_content_not_actionable", "a11y_control_ambiguous", "a11y_dialog_blocks_chat",
    "a11y_direction_unbound", "a11y_draft_state_conflict", "a11y_draft_subtree_unreadable",
    "a11y_draft_text_conflict", "a11y_evidence_binding_invalid", "a11y_frame_scope_mismatch",
    "a11y_input_controls_unreadable", "a11y_label_unreadable", "a11y_message_text_missing",
    "a11y_node_binding_invalid", "a11y_nodes_invalid", "a11y_overlay_ambiguous",
    "a11y_profile_geometry_unbound", "a11y_profile_numeric_id_unreadable",
    "a11y_projection_unavailable", "a11y_protected_ancestor", "a11y_scope_missing",
    "a11y_scope_unbound", "a11y_text_unreadable", "a11y_title_geometry", "a11y_top_binding_invalid",
})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _finite(value, *, minimum=0.0, maximum=float("inf")) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("A finite value within the observer bounds is required.")
    return float(value)


def _aware(value) -> datetime:
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise ObserverReadError("invalid_timing") from None


class ObserverReadError(RuntimeError):
    def __init__(self, code: str):
        if code not in _CODES:
            raise ValueError("Observer errors must use fixed categories.")
        self.code = code
        super().__init__(code)


class QQScopedAccessibilityClient:
    """One authenticated loopback HTTP connection pool for scoped reads only."""

    def __init__(
        self, *, agent_url: str, token_file: Path | str, body_instance_id: str,
        environment_revision: int, window_id: str, app_id: str = "org.mistyai.QQ",
        window_rect: tuple[int, int, int, int] = (0, 27, 1280, 693),
        timeout_seconds: float = 1.5, maximum_age_seconds: float = 3,
        future_tolerance_seconds: float = 1, client: httpx.Client | None = None,
        causal_freshness: bool = False,
        read_lock=None,
        skip_obscured_content: bool = False,
        clock: Callable[[], float] = time.perf_counter, wall_clock: Callable[[], datetime] = _utcnow,
    ):
        parsed = urlsplit(agent_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("The QQ accessibility endpoint must be an explicit loopback HTTP origin.")
        if (not isinstance(body_instance_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", body_instance_id)
                or isinstance(environment_revision, bool) or not isinstance(environment_revision, int) or environment_revision < 1
                or not isinstance(window_id, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", window_id)
                or not isinstance(app_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", app_id)):
            raise ValueError("A concrete QQ instance, revision, window and application binding is required.")
        if (not isinstance(window_rect, (tuple, list)) or len(window_rect) != 4
                or any(isinstance(n, bool) or not isinstance(n, int) for n in window_rect)
                or min(window_rect[:2]) < 0 or min(window_rect[2:]) <= 0
                or max(window_rect) > 16384):
            raise ValueError("A positive bounded absolute window rectangle is required.")
        token_path = Path(token_file)
        if not token_path.is_absolute() or not token_path.is_file() or token_path.is_symlink() or token_path.stat().st_size > 4096:
            raise ValueError("An existing private absolute token file is required.")
        token = token_path.read_text(encoding="utf-8").strip()
        if not 16 <= len(token) <= 2048 or any(character.isspace() for character in token):
            raise ValueError("The private service token is invalid.")
        self.timeout_seconds = _finite(timeout_seconds, minimum=0.05, maximum=5)
        self.maximum_age_seconds = _finite(maximum_age_seconds, minimum=0.05, maximum=30)
        self.future_tolerance_seconds = _finite(future_tolerance_seconds, maximum=5)
        if type(causal_freshness) is not bool:
            raise ValueError("Causal freshness must be explicitly enabled or disabled.")
        if type(skip_obscured_content) is not bool:
            raise ValueError("Obscured-content skipping must be explicitly enabled or disabled.")
        self.skip_obscured_content = skip_obscured_content
        if read_lock is not None and (not callable(getattr(read_lock, "acquire", None))
                                      or not callable(getattr(read_lock, "release", None))):
            raise ValueError("The optional scoped-read lock must support acquire and release.")
        self._read_lock = read_lock
        self.causal_freshness = causal_freshness
        self._last_causal_sample: tuple[datetime, float] | None = None
        self._clock_step_detected = False
        self._clock_step_count = 0
        self._progress_error_ms: float | None = None
        self._intra_request_wall_step = False
        self._wall_duration_error_ms: float | None = None
        self._remote_local_duration_delta_ms: float | None = None
        self._read_timing_basis = "legacy_wall_clock"
        self.clock, self.wall_clock = clock, wall_clock
        self._endpoint = agent_url.rstrip("/") + "/agent/v1/accessibility:read"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._identity = {"body_instance_id": body_instance_id, "environment_revision": environment_revision}
        self._scope = {"window_id": window_id, "app_id": app_id,
                       "window_rect": dict(zip(("x", "y", "width", "height"), window_rect, strict=True)),
                       "maximum_duration_ms": 1000}
        if skip_obscured_content:
            self._scope["skip_obscured_content"] = True
        self._client = client or httpx.Client(timeout=self.timeout_seconds, follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        self._previous_digest: str | None = None
        self._closed = False
        self._last_timing: dict | None = None

    @property
    def last_timing(self) -> dict | None:
        """Paired transport clock diagnostics, never a freshness calibration."""
        return dict(self._last_timing) if self._last_timing is not None else None

    def _record_timing(self, tree, *, started_at: datetime, started_mono: float) -> None:
        finished_at = _aware(self.wall_clock())
        duration_ms = max(0.0, self.clock() - started_mono) * 1000
        timing = {"local_started_at": started_at.isoformat(), "local_finished_at": finished_at.isoformat(),
                  "request_duration_ms": round(duration_ms, 3), "clock_offset_ms": None,
                  "clock_uncertainty_ms": None}
        if self.causal_freshness:
            timing.update(clock_step_detected=self._clock_step_detected,
                          clock_step_count=self._clock_step_count, progress_error_ms=self._progress_error_ms,
                          intra_request_wall_step=self._intra_request_wall_step,
                          wall_duration_error_ms=self._wall_duration_error_ms,
                          remote_local_duration_delta_ms=self._remote_local_duration_delta_ms,
                          freshness_timing_basis=self._read_timing_basis)
        try:
            remote_started, remote_finished = _aware(tree["started_at"]), _aware(tree["finished_at"])
            local_interval = (finished_at - started_at).total_seconds()
            remote_interval = (remote_finished - remote_started).total_seconds()
            if local_interval >= remote_interval >= 0:
                # The remote processing interval lies inside the request/response
                # interval. Its midpoint gives an estimate; remaining transit
                # time is uncertainty, not permission to relax timestamp checks.
                lower = (remote_finished - finished_at).total_seconds()
                upper = (remote_started - started_at).total_seconds()
                timing["clock_offset_ms"] = round((lower + upper) * 500, 3)
                timing["clock_uncertainty_ms"] = round((upper - lower) * 500, 3)
        except (ObserverReadError, KeyError, TypeError, OverflowError):
            pass
        self._last_timing = timing

    def _validate(self, tree: dict, *, request_started_at: datetime,
                  request_started_mono: float, request_nonce: str | None) -> None:
        if not isinstance(tree, dict) or tree.get("schema_version") != "a11y-tree-v1" or tree.get("truncated") is not False:
            raise ObserverReadError("invalid_response")
        if (tree.get("body_instance_id") != self._identity["body_instance_id"]
                or type(tree.get("environment_revision")) is not int
                or tree["environment_revision"] != self._identity["environment_revision"]):
            raise ObserverReadError("identity_mismatch")
        scope = tree.get("scope")
        bounds = scope.get("window_rect") if isinstance(scope, dict) else None
        if (not isinstance(scope, dict) or scope.get("bound") is not True or scope.get("active") is not True
                or not isinstance(bounds, dict) or set(bounds) != {"x", "y", "width", "height"}
                or any(type(number) is not int for number in bounds.values())
                or any(scope.get(name) != self._scope[name] for name in ("window_id", "app_id", "window_rect"))):
            raise ObserverReadError("scope_mismatch")
        if scope.get("skip_obscured_content", False) is not self.skip_obscured_content:
            raise ObserverReadError("scope_mismatch")
        if (not isinstance(tree.get("nodes"), list) or len(tree["nodes"]) > 1500
                or not isinstance(tree.get("semantic_digest"), str) or not _DIGEST.fullmatch(tree["semantic_digest"])
                or type(tree.get("changed")) is not bool):
            raise ObserverReadError("invalid_response")
        started, finished, current = _aware(tree.get("started_at")), _aware(tree.get("finished_at")), _aware(self.wall_clock())
        duration = tree.get("duration_ms")
        if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration)
                or not 0 <= duration <= 1000):
            raise ObserverReadError("invalid_timing")
        has_monotonic_timing = "read_timing" in tree
        if not has_monotonic_timing and (finished < started or (finished - started).total_seconds() > 1.1):
            raise ObserverReadError("invalid_timing")
        if request_nonce is None:
            if has_monotonic_timing:
                raise ObserverReadError("invalid_timing")
            if max((started - current).total_seconds(), (finished - current).total_seconds()) > self.future_tolerance_seconds:
                raise ObserverReadError("future_response")
            if ((current - finished).total_seconds() > self.maximum_age_seconds
                    or (request_started_at - finished).total_seconds() > self.future_tolerance_seconds):
                raise ObserverReadError("stale_response")
            return
        returned_nonce = tree.get("request_nonce")
        if (not isinstance(returned_nonce, str) or not _DIGEST.fullmatch(returned_nonce)
                or not secrets.compare_digest(returned_nonce, request_nonce)):
            raise ObserverReadError("nonce_mismatch")
        received_mono = self.clock()
        elapsed = received_mono - request_started_mono
        remote_elapsed = (finished - started).total_seconds()
        if has_monotonic_timing:
            envelope = tree["read_timing"]
            if (not isinstance(envelope, dict) or envelope.get("schema_version") != "a11y-read-timing-v1"
                    or envelope.get("basis") != "agent_monotonic"):
                raise ObserverReadError("invalid_timing")
            bound_nonce = envelope.get("request_nonce")
            if (not isinstance(bound_nonce, str) or not _DIGEST.fullmatch(bound_nonce)
                    or not secrets.compare_digest(bound_nonce, request_nonce)):
                raise ObserverReadError("nonce_mismatch")
            start_offset = envelope.get("read_started_offset_ms")
            end_offset = envelope.get("read_finished_offset_ms")
            if (any(type(value) not in (int, float) or not math.isfinite(value)
                    for value in (start_offset, end_offset, elapsed))
                    or start_offset != 0):
                raise ObserverReadError("invalid_timing")
            # Windows QPC and the Agent's Linux monotonic clock do not share a
            # duration scale. Bound each independently; their difference is a
            # diagnostic, never a clock calibration or extra deadline allowance.
            budget_seconds = min(self.timeout_seconds, 1.5)
            self._remote_local_duration_delta_ms = round(end_offset - elapsed * 1000, 6)
            if (not 0 <= end_offset <= budget_seconds * 1000
                    or not 0 <= elapsed <= budget_seconds
                    or duration > end_offset):
                raise ObserverReadError("invalid_timing")
            self._read_timing_basis = "agent_monotonic"
            self._wall_duration_error_ms = round((remote_elapsed - duration / 1000) * 1000, 3)
            self._intra_request_wall_step = abs(self._wall_duration_error_ms) > 100
        elif (elapsed < 0 or elapsed > self.timeout_seconds or duration / 1000 > elapsed + .1
                or abs(remote_elapsed - duration / 1000) > .1):
            raise ObserverReadError("invalid_timing")
        # Nonce + the trusted endpoint's fresh read proves causal request
        # membership. It does not authorize an unlimited clock difference.
        lower = (finished - current).total_seconds()
        upper = (started - request_started_at).total_seconds()
        if max(abs(lower), abs(upper)) > 5:
            raise ObserverReadError("clock_skew")
        if self._last_causal_sample is not None:
            prior_remote, prior_mono = self._last_causal_sample
            remote_progress = (finished - prior_remote).total_seconds()
            local_progress = received_mono - prior_mono
            if local_progress < 0:
                raise ObserverReadError("invalid_timing")
            # A fresh nonce is echoed only after this trusted endpoint's fresh
            # capture. Cross-request wall-clock correction is diagnostic: it
            # cannot invalidate that causal binding or relax the ±5s bound.
            progress_error = remote_progress - local_progress
            self._progress_error_ms = round(progress_error * 1000, 3)
            allowance = min(5.0, .25 + .15 * max(0, local_progress))
            self._clock_step_detected = remote_progress <= 0 or abs(progress_error) > allowance
            if self._clock_step_detected:
                self._clock_step_count += 1
        self._last_causal_sample = (finished, received_mono)

    def read(self, previous_digest: str | None = None) -> dict:
        if self._closed:
            raise ObserverReadError("client_closed")
        digest = self._previous_digest if previous_digest is None else previous_digest
        if digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            raise ValueError("The previous accessibility digest must be SHA-256.")
        scope = dict(self._scope)
        if digest is not None:
            scope["previous_digest"] = digest
        started_at, started_mono = _aware(self.wall_clock()), self.clock()
        self._clock_step_detected = False
        self._progress_error_ms = None
        self._intra_request_wall_step = False
        self._wall_duration_error_ms = None
        self._remote_local_duration_delta_ms = None
        self._read_timing_basis = "legacy_wall_clock"
        request_nonce = secrets.token_hex(32) if self.causal_freshness else None
        payload = {**self._identity, "scope": scope}
        if request_nonce is not None:
            payload["request_nonce"] = request_nonce
        tree = None
        lock_acquired = False
        try:
            if self._read_lock is not None:
                remaining = self.timeout_seconds - (self.clock() - started_mono)
                # Leave the endpoint's unchanged 1000ms scope budget plus a
                # transport margin. Do not abandon a just-started remote read
                # merely because contention consumed its HTTP time allowance.
                if remaining < 1.1 or not self._read_lock.acquire(timeout=min(.4, max(0, remaining - 1.1))):
                    raise ObserverReadError("request_timeout")
                lock_acquired = True
            remaining = self.timeout_seconds - (self.clock() - started_mono)
            if remaining <= 0 or (self._read_lock is not None and remaining < 1.1):
                raise ObserverReadError("request_timeout")
            chunks, size = [], 0
            with self._client.stream("POST", self._endpoint, json=payload,
                                     headers=self._headers, timeout=remaining,
                                     follow_redirects=False) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_TREE_BYTES:
                        raise ObserverReadError("response_too_large")
                    if self.clock() - started_mono > self.timeout_seconds:
                        raise ObserverReadError("request_timeout")
                    chunks.append(chunk)
            tree = json.loads(b"".join(chunks))
            if self.clock() - started_mono > self.timeout_seconds:
                raise ObserverReadError("request_timeout")
            self._validate(tree, request_started_at=started_at, request_started_mono=started_mono,
                           request_nonce=request_nonce)
        except ObserverReadError:
            raise
        except httpx.TimeoutException:
            raise ObserverReadError("request_timeout") from None
        except httpx.HTTPError:
            raise ObserverReadError("http_error") from None
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ObserverReadError("invalid_response") from None
        finally:
            try:
                self._record_timing(tree, started_at=started_at, started_mono=started_mono)
            finally:
                if lock_acquired:
                    self._read_lock.release()
        self._previous_digest = tree["semantic_digest"]
        return tree

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._owns_client:
                self._client.close()


@dataclass(frozen=True)
class QQObserverEvidence:
    """A subsequent read-only capture, not proof it depicts the earlier AX tree."""

    capture_id: str
    captured_at: datetime
    evidence_ref: str
    content_digest: str
    a11y_evidence_ref: str | None = None
    a11y_digest: str | None = None

    def __post_init__(self):
        if (not isinstance(self.capture_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", self.capture_id)):
            raise ObserverReadError("evidence_metadata_invalid")
        for ref in (self.evidence_ref, self.a11y_evidence_ref):
            if ref is not None and (not isinstance(ref, str) or not re.fullmatch(r"evidence://sha256/[0-9a-f]{64}", ref)):
                raise ObserverReadError("evidence_metadata_invalid")
        if self.evidence_ref is None:
            raise ObserverReadError("evidence_metadata_invalid")
        for digest in (self.content_digest, self.a11y_digest):
            if digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
                raise ObserverReadError("evidence_metadata_invalid")
        if self.content_digest is None:
            raise ObserverReadError("evidence_metadata_invalid")
        object.__setattr__(self, "captured_at", _aware(self.captured_at))

    def as_dict(self) -> dict:
        return {"capture_id": self.capture_id, "captured_at": self.captured_at.isoformat(),
                "evidence_ref": self.evidence_ref, "content_digest": self.content_digest,
                "a11y_evidence_ref": self.a11y_evidence_ref, "a11y_digest": self.a11y_digest}


@dataclass(frozen=True)
class QQObservedChange:
    sequence: int
    digest: str
    first_seen_at: datetime
    first_seen_mono: float
    polled_at: datetime
    polled_mono: float
    tree: dict = field(repr=False)
    is_baseline: bool = False
    after_gap: bool = False
    evidence: QQObserverEvidence | None = None
    evidence_started_at: datetime | None = None
    evidence_finished_at: datetime | None = None
    evidence_started_mono: float | None = None
    evidence_finished_mono: float | None = None


@dataclass(frozen=True)
class QQObserverChanges:
    observations: tuple[QQObservedChange, ...]
    oldest_sequence: int
    latest_sequence: int
    overflowed: bool
    missed_changes: int
    gap: bool


@dataclass(frozen=True)
class QQObserverGap:
    sequence: int
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    reason: str

    def as_dict(self) -> dict:
        return {"sequence": self.sequence, "started_at": self.started_at.isoformat(),
                "finished_at": self.finished_at.isoformat(), "duration_seconds": self.duration_seconds,
                "reason": self.reason}


class ScopedQQObserver:
    def __init__(
        self, *, read_fn: Callable[[], dict],
        signature_fn: Callable[[dict], ChatChangeSignature | str | None] = chat_change_signature,
        evidence_fn: Callable[[], QQObserverEvidence | dict] | None = None,
        gap_fn: Callable[[QQObserverGap], None] | None = None,
        poll_seconds: float = 1, max_changes: int = 128, max_buffer_bytes: int = 16 * 1024 * 1024,
        close_fn: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], datetime] = _utcnow,
    ):
        self.poll_seconds = _finite(poll_seconds, minimum=0.01, maximum=60)
        if isinstance(max_changes, bool) or not isinstance(max_changes, int) or not 1 <= max_changes <= 128:
            raise ValueError("The change buffer must contain at most 128 observations.")
        if isinstance(max_buffer_bytes, bool) or not isinstance(max_buffer_bytes, int) or not MAX_TREE_BYTES <= max_buffer_bytes <= 64 * 1024 * 1024:
            raise ValueError("The observer byte budget must be between 4 and 64 MiB.")
        if not callable(read_fn) or not callable(signature_fn):
            raise ValueError("Read and signature callbacks are required.")
        if evidence_fn is not None and not callable(evidence_fn):
            raise ValueError("The optional read-only evidence callback must be callable.")
        if gap_fn is not None and not callable(gap_fn):
            raise ValueError("The optional content-free gap sink must be callable.")
        self._read_fn, self._signature_fn, self._close_fn = read_fn, signature_fn, close_fn
        self._evidence_fn = evidence_fn
        self._gap_fn = gap_fn
        self.clock, self.wall_clock = clock, wall_clock
        self.max_changes, self.max_buffer_bytes = max_changes, max_buffer_bytes
        self._condition = threading.Condition(threading.RLock())
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._close_called = False
        self._buffer: deque[tuple[QQObservedChange, int]] = deque()
        self._buffer_bytes = 0
        self._latest: QQObservedChange | None = None
        self._sequence = 0
        self._polls = self._changes = self._overflows = 0
        self._last_polled_at: datetime | None = None
        self._last_change_at: datetime | None = None
        self._last_error_type: str | None = None
        self._signature_error_type: str | None = None
        self._gap_started_at: datetime | None = None
        self._gap_started_mono: float | None = None
        self._gap_reason: str | None = None
        self._gap_sequence = self._gap_sink_failures = 0
        self._total_gap_seconds = self._max_gap_seconds = 0.0
        self._recent_gaps: deque[QQObserverGap] = deque(maxlen=32)
        self._pending_evidence: tuple[str, datetime, float] | None = None
        self._evidence_in_progress = False
        self._evidence_attempts = self._evidence_captures = self._evidence_failures = 0
        self._last_evidence_duration_ms = self._max_evidence_duration_ms = 0.0
        self._last_poll_duration_ms = self._max_poll_duration_ms = 0.0

    @staticmethod
    def _copy(observation: QQObservedChange) -> QQObservedChange:
        return replace(observation, tree=deepcopy(observation.tree))

    @staticmethod
    def _digest(value) -> str | None:
        if isinstance(value, ChatChangeSignature):
            if not value.chat_visible:
                return None
            value = value.chat_digest
        if value is not None and (not isinstance(value, str) or not _DIGEST.fullmatch(value)):
            raise ObserverReadError("invalid_signature")
        return value

    def _mark_gap(self, polled_at: datetime, polled_mono: float, error_type: str | None, *, reason: str | None = None):
        if self._gap_started_at is None:
            self._gap_started_at, self._gap_started_mono = polled_at, polled_mono
            self._gap_reason = error_type or reason or "chat_occluded"
        elif error_type:
            self._gap_reason = error_type
        self._last_error_type = error_type

    def _close_gap(self, finished_at: datetime, finished_mono: float) -> QQObserverGap | None:
        if self._gap_started_at is None:
            return None
        duration = round(max(0, finished_mono - self._gap_started_mono), 6)
        self._gap_sequence += 1
        gap = QQObserverGap(self._gap_sequence, self._gap_started_at, finished_at, duration,
                            self._gap_reason or "chat_occluded")
        self._recent_gaps.append(gap)
        self._total_gap_seconds += duration
        self._max_gap_seconds = max(self._max_gap_seconds, duration)
        self._gap_started_at = self._gap_started_mono = self._gap_reason = None
        return gap

    def _publish_gap(self, gap: QQObserverGap | None) -> None:
        if gap is not None and self._gap_fn is not None:
            try:
                self._gap_fn(gap)
            except Exception:
                # Ring records remain available even when the optional sink
                # fails. Expose a sticky count without leaking exception text.
                with self._condition:
                    self._gap_sink_failures += 1

    def poll_once(self) -> QQObservedChange | None:
        """An explicit read-only poll, useful before start or in deterministic tests."""
        if self._closed or self._stop.is_set():
            return None
        if not self._poll_lock.acquire(blocking=False):
            raise RuntimeError("An observer read is already in progress.")
        poll_started_mono = self.clock()
        signature_error_type = None
        try:
            try:
                raw = self._read_fn()
                if not isinstance(raw, dict):
                    raise ObserverReadError("invalid_response")
                encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if len(encoded) > MAX_TREE_BYTES:
                    raise ObserverReadError("response_too_large")
                tree = json.loads(encoded)
                try:
                    signature = self._signature_fn(tree)
                except AccessibilityUnavailable as exc:
                    reason = exc.args[0] if len(exc.args) == 1 else None
                    signature_error_type = (reason if type(reason) is str and reason in _SIGNATURE_ERROR_CODES
                                            else "accessibility_unavailable")
                    raise
                digest = self._digest(signature)
                error_type = None
            except Exception as exc:
                digest, tree, encoded = None, None, b""
                if isinstance(exc, ObserverReadError):
                    error_type = exc.code
                elif isinstance(exc, AccessibilityUnavailable):
                    error_type = "accessibility_unavailable"
                elif isinstance(exc, httpx.TimeoutException):
                    error_type = "request_timeout"
                else:
                    error_type = "observer_error"
            polled_at, polled_mono = _aware(self.wall_clock()), _finite(self.clock())
            evidence = None
            evidence_started_at = evidence_finished_at = None
            evidence_started_mono = evidence_finished_mono = None
            first_seen_at, first_seen_mono = polled_at, polled_mono
            with self._condition:
                self._polls += 1
                self._last_polled_at = polled_at
                self._signature_error_type = signature_error_type
                if self._stop.is_set():
                    return None
                if digest is None:
                    self._mark_gap(polled_at, polled_mono, error_type)
                    self._condition.notify_all()
                    return None
                after_gap = self._gap_started_at is not None
                capture_needed = (self._evidence_fn is not None and self._latest is not None
                                  and digest != self._latest.digest)
                if capture_needed:
                    if self._pending_evidence is not None and self._pending_evidence[0] == digest:
                        _, first_seen_at, first_seen_mono = self._pending_evidence
                    else:
                        self._pending_evidence = (digest, first_seen_at, first_seen_mono)
                    self._evidence_in_progress = True
                    self._evidence_attempts += 1
                    # A known change is not yet ready to publish. Hide the old
                    # latest snapshot while this independent capture is pending.
                    self._mark_gap(first_seen_at, first_seen_mono, None, reason="evidence_pending")
                    self._condition.notify_all()
            if capture_needed:
                evidence_started_at, evidence_started_mono = _aware(self.wall_clock()), self.clock()
                try:
                    value = self._evidence_fn()
                    if isinstance(value, QQObserverEvidence):
                        evidence = value
                    elif isinstance(value, dict):
                        evidence = QQObserverEvidence(**value)
                    else:
                        raise ObserverReadError("evidence_metadata_invalid")
                    evidence_size = len(json.dumps(evidence.as_dict(), separators=(",", ":")).encode())
                    if evidence_size > 4096 or len(encoded) + evidence_size > self.max_buffer_bytes:
                        raise ObserverReadError("evidence_metadata_invalid")
                except Exception as exc:
                    evidence_error = ("evidence_metadata_invalid" if isinstance(exc, (ObserverReadError, TypeError))
                                      else "evidence_capture_failed")
                    with self._condition:
                        self._evidence_failures += 1
                        self._mark_gap(first_seen_at, first_seen_mono, evidence_error)
                        self._condition.notify_all()
                    return None
                finally:
                    evidence_finished_at, evidence_finished_mono = _aware(self.wall_clock()), self.clock()
                    with self._condition:
                        elapsed = max(0, evidence_finished_mono - evidence_started_mono) * 1000
                        self._last_evidence_duration_ms = round(elapsed, 3)
                        self._max_evidence_duration_ms = max(self._max_evidence_duration_ms, self._last_evidence_duration_ms)
                        self._evidence_in_progress = False
                with self._condition:
                    self._evidence_captures += 1
            with self._condition:
                if self._stop.is_set():
                    return None
                self._pending_evidence = None
                completed_gap = self._close_gap(_aware(self.wall_clock()), self.clock())
                self._last_error_type = None
                if self._latest is None:
                    self._latest = QQObservedChange(0, digest, polled_at, polled_mono, polled_at,
                                                    polled_mono, tree, is_baseline=True, after_gap=after_gap)
                elif digest == self._latest.digest:
                    # Refresh the tree without renewing the original arrival or
                    # sequence. Input drafts/roster/animation are signature-owned.
                    self._latest = replace(self._latest, tree=tree, polled_at=polled_at, polled_mono=polled_mono)
                else:
                    self._sequence += 1
                    self._changes += 1
                    self._last_change_at = first_seen_at
                    self._latest = QQObservedChange(self._sequence, digest, first_seen_at, first_seen_mono,
                        polled_at, polled_mono, tree, after_gap=after_gap, evidence=evidence,
                        evidence_started_at=evidence_started_at, evidence_finished_at=evidence_finished_at,
                        evidence_started_mono=evidence_started_mono, evidence_finished_mono=evidence_finished_mono)
                    item_size = len(encoded) + (evidence_size if capture_needed else 0)
                    while self._buffer and (len(self._buffer) >= self.max_changes
                                           or self._buffer_bytes + item_size > self.max_buffer_bytes):
                        _, size = self._buffer.popleft()
                        self._buffer_bytes -= size
                        self._overflows += 1
                    self._buffer.append((self._latest, item_size))
                    self._buffer_bytes += item_size
                self._condition.notify_all()
                result = self._copy(self._latest)
            self._publish_gap(completed_gap)
            return result
        finally:
            with self._condition:
                self._last_poll_duration_ms = round(max(0, self.clock() - poll_started_mono) * 1000, 3)
                self._max_poll_duration_ms = max(self._max_poll_duration_ms, self._last_poll_duration_ms)
            self._poll_lock.release()

    def latest(self) -> QQObservedChange | None:
        with self._condition:
            if self._closed or self._stop.is_set() or self._gap_started_at is not None or self._latest is None:
                return None
            return self._copy(self._latest)

    def changes_after(self, sequence: int) -> QQObserverChanges:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= self._sequence:
            raise ValueError("The change cursor must belong to the current observer sequence.")
        with self._condition:
            oldest = self._buffer[0][0].sequence if self._buffer else self._sequence + 1
            missed = max(0, oldest - sequence - 1)
            return QQObserverChanges(tuple(self._copy(item) for item, _ in self._buffer if item.sequence > sequence),
                                     oldest, self._sequence, missed > 0, missed, self._gap_started_at is not None)

    def wait_for_change(self, after_sequence: int, timeout: float = 1) -> QQObserverChanges:
        timeout = _finite(timeout, maximum=1)
        # Validate before waiting, including cursors from a different run.
        self.changes_after(after_sequence)
        with self._condition:
            self._condition.wait_for(lambda: self._sequence > after_sequence or self._stop.is_set(), timeout=timeout)
            return self.changes_after(after_sequence)

    def status(self) -> dict:
        with self._condition:
            running = self._thread is not None and self._thread.is_alive()
            gap_seconds = 0.0 if self._gap_started_mono is None else max(0.0, self.clock() - self._gap_started_mono)
            result = {"running": running, "polls": self._polls, "changes": self._changes,
                    "last_polled_at": self._last_polled_at.isoformat() if self._last_polled_at else None,
                    "last_change_at": self._last_change_at.isoformat() if self._last_change_at else None,
                    "last_error_type": self._last_error_type,
                    "signature_error_type": self._signature_error_type,
                    "gap_started_at": self._gap_started_at.isoformat() if self._gap_started_at else None,
                    "gap_seconds": round(gap_seconds, 3), "overflows": self._overflows,
                    "gap_sequence": self._gap_sequence, "completed_gap_count": self._gap_sequence,
                    "total_gap_seconds": round(self._total_gap_seconds, 6),
                    "max_gap_seconds": round(max(self._max_gap_seconds, gap_seconds), 6),
                    "recent_gaps": [gap.as_dict() for gap in self._recent_gaps],
                    "gap_sink_failures": self._gap_sink_failures,
                    "evidence_enabled": self._evidence_fn is not None,
                    "evidence_in_progress": self._evidence_in_progress,
                    "pending_digest": self._pending_evidence[0] if self._pending_evidence is not None else None,
                    "evidence_attempts": self._evidence_attempts, "evidence_captures": self._evidence_captures,
                    "evidence_failures": self._evidence_failures,
                    "last_evidence_duration_ms": self._last_evidence_duration_ms,
                    "max_evidence_duration_ms": self._max_evidence_duration_ms,
                    "last_poll_duration_ms": self._last_poll_duration_ms,
                    "max_poll_duration_ms": self._max_poll_duration_ms}
            reader = getattr(self._read_fn, "__self__", None)
            if isinstance(reader, QQScopedAccessibilityClient):
                result["read_timing"] = reader.last_timing
                result["read_freshness"] = "nonce_bound" if reader.causal_freshness else "strict_wall_clock"
            return result

    def _run(self):
        try:
            while not self._stop.is_set():
                started = self.clock()
                self.poll_once()
                self._stop.wait(max(0.0, self.poll_seconds - (self.clock() - started)))
        except Exception:
            # Unexpected local callback/runtime failures must not leave a stale
            # snapshot looking healthy after the sensing thread has terminated.
            with self._condition:
                self._last_error_type = "observer_error"
                self._stop.set()
        finally:
            with self._condition:
                self._condition.notify_all()

    def start(self) -> ScopedQQObserver:
        with self._condition:
            if self._closed or self._stop.is_set():
                raise RuntimeError("A closed observer cannot be restarted.")
            if self._thread is not None and self._thread.is_alive():
                return self
            self._thread = threading.Thread(target=self._run, name="qq-scoped-observer", daemon=True)
            self._thread.start()
            return self

    def close(self, timeout: float = 3) -> bool:
        timeout = _finite(timeout, maximum=5)
        self._closed = True
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        if (self._thread is not None and self._thread.is_alive()) or self._poll_lock.locked():
            with self._condition:
                self._last_error_type = "close_timeout"
            return False
        if self._close_fn is not None and not self._close_called:
            self._close_fn()
            self._close_called = True
        return True
