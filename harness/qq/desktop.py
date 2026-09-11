"""Dedicated QQ desktop actions grounded in verified, recent screenshots.

The caller owns the durable send reservation. Once before_click is invoked, any
uncertain result is terminal for this attempt: no automatic retry or second click.
"""
from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict, deque
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import threading
import time
from typing import TYPE_CHECKING, Callable, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from harness.embodiment.providers.mistyai import MistyAIClient
from harness.embodiment.providers.mistyai_dto import NativeObservation
from harness.llm.client import OpenAICompatClient
from harness.qq.grounding import GroundedMessage, GroundedScreen, GroundingError, Rect, inspect_qq_frame
from harness.qq.identities import IdentityScope, SessionIdentityCache, VerifiedIdentity, VisualHint
from harness.qq.vision import _png_dimensions, load_vision_settings

if TYPE_CHECKING:
    from harness.qq.decision import QQVerifiedSelfAlias


class DesktopError(RuntimeError):
    """Reason codes only: no credentials, observed text, or HTTP response bodies."""


class PreClickIdentityInvalidated(DesktopError):
    """Callback rejected stale identity before committing any click transition."""

    def __init__(self):
        super().__init__("identity_cache_invalidated_before_click")


@dataclass(frozen=True)
class DesktopConfig:
    body_url: str
    body_token_file: Path
    agent_url: str
    agent_token_file: Path
    expected_group_id: str
    self_member_id: str
    vision_model: str
    model_config_path: Path | None = None
    body_instance_id: str = "misty-qq-test-01"
    environment_revision: int = 2
    window_id: str = "0x200002e"
    app_id: str = "org.mistyai.QQ"
    width: int = 1280
    height: int = 720
    window_rect: tuple[int, int, int, int] = (0, 27, 1280, 693)
    title_roi: tuple[int, int, int, int] = (0, 67, 1000, 118)
    chat_roi: tuple[int, int, int, int] = (0, 119, 1095, 509)
    input_roi: tuple[int, int, int, int] = (0, 545, 1095, 720)
    send_roi: tuple[int, int, int, int] = (700, 650, 1095, 720)
    maximum_frame_age_seconds: float = 120
    settle_seconds: float = 0.65
    request_timeout_seconds: float = 15
    self_display_name_hint: str = "Misty"
    accessibility_enabled: bool = False
    identity_cache_enabled: bool = False

    def __post_init__(self):
        if type(self.identity_cache_enabled) is not bool:
            raise ValueError("identity cache must be explicitly boolean")
        for value in (self.expected_group_id, self.self_member_id):
            if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
                raise ValueError("numeric QQ identity required")
        for url in (self.body_url, self.agent_url):
            parsed = urlsplit(url)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.query or parsed.fragment:
                raise ValueError("dedicated loopback endpoint required")
        if (self.width, self.height) != (1280, 720):
            raise ValueError("this desktop layout requires 1280x720")
        for region in (self.title_roi, self.chat_roi, self.input_roi, self.send_roi):
            left, top, right, bottom = region
            if not (0 <= left < right <= self.width and 0 <= top < bottom <= self.height):
                raise ValueError("invalid fixed desktop region")
        if not self.vision_model.strip() or not 0 <= self.settle_seconds <= 3 or not 0 < self.request_timeout_seconds <= 60:
            raise ValueError("invalid desktop settings")
        if not self.self_display_name_hint.strip() or len(self.self_display_name_hint) > 256:
            raise ValueError("invalid local member name hint")


@dataclass(frozen=True)
class Frame:
    capture_id: str
    captured_at: datetime
    evidence_ref: str
    png: bytes = field(repr=False)
    width: int = 1280
    height: int = 720
    window_id: str = ""
    window_rect: tuple[int, int, int, int] = (0, 27, 1280, 693)
    active_window_id: str = ""
    content_digest: str = ""
    a11y_tree: dict | None = field(default=None, repr=False)
    a11y_evidence_ref: str | None = None


@dataclass(frozen=True)
class GroupVerification:
    group_id: str
    display_name: str
    evidence_refs: list[str]
    chat_frame: Frame
    chat_screen: GroundedScreen


@dataclass(frozen=True)
class AttributedMessage:
    member_id: str
    display_name: str
    capture_id: str
    observed_at: datetime
    profile_evidence_refs: list[str]
    evidence_refs: list[str]
    text: str | None = None
    capture_item_id: str = ""
    profile_record_id: str | None = None
    attribution_method: str = "qq_visual_avatar_profile_v1"


@dataclass(frozen=True)
class DeliveryResult:
    status: Literal["sent", "unknown", "not_sent"]
    evidence_refs: list[str]
    reason_code: str
    delivered_at: datetime | None = None
    phase: Literal["precheck", "click_started", "postcheck"] = "precheck"
    retryable: bool = False
    click_acknowledged_at: datetime | None = None
    click_observed_at: datetime | None = None
    click_observed_evidence_ref: str | None = None


def _pixels(png: bytes, regions: tuple[tuple[int, int, int, int], ...], *,
            masks: tuple[tuple[int, int, int, int], ...] = ()) -> str:
    # Pillow is used only for digest crops, never to edit a model-visible frame.
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        image.load()
        hasher = hashlib.sha256()
        for region in regions:
            hasher.update(repr(region).encode("ascii"))
            crop = image.crop(region).convert("RGB")
            for mask in masks:
                left, top = max(region[0], mask[0]), max(region[1], mask[1])
                right, bottom = min(region[2], mask[2]), min(region[3], mask[3])
                if left < right and top < bottom:
                    crop.paste((0, 0, 0), (left - region[0], top - region[1], right - region[0], bottom - region[1]))
            hasher.update(crop.tobytes())
        return hasher.hexdigest()


def _title(value: str | None) -> str:
    return re.sub(r"\s*[（(]\d+[）)]\s*$", "", value or "").strip()


class DesktopBackend:
    supports_click_acknowledgement = True

    def __init__(self, config: DesktopConfig, *, should_stop: Callable[[], bool] | None = None,
                 agent_client: httpx.Client | None = None, llm: OpenAICompatClient | None = None,
                 observation_lock=None):
        self.config = config
        self.should_stop = should_stop or (lambda: False)
        self._lock = threading.RLock()
        self._observation_lock = observation_lock
        self._screens: dict[str, GroundedScreen] = {}
        self._pixel_screens: OrderedDict[str, GroundedScreen] = OrderedDict()
        self._group: GroupVerification | None = None
        self._identity_epoch = uuid4().hex
        self._group_identity_anchor: tuple[str, str, tuple[str, ...]] | None = None
        self._self_identity: tuple[tuple[str, str], AttributedMessage] | None = None
        self._member_identities = None
        self._pending_identity_bindings: OrderedDict[str, tuple] = OrderedDict()
        self._attempted_reservations: set[str] = set()
        self._click_acknowledgements: dict[str, datetime] = {}
        self._click_observations: dict[str, tuple[datetime, str]] = {}
        self._a11y_frames: set[str] = set()
        self._latest_capture: Frame | None = None
        # Diagnostic evidence survives reuse-cache invalidation. Never used
        # for freshness, coordinates, identity checks or action authorization.
        self._last_verified_capture: Frame | None = None
        self._latest_capture_started_mono: float | None = None
        self._gui_generation = 0
        self._latest_capture_generation = -1
        self._capture_times: OrderedDict[str, tuple[Frame, float]] = OrderedDict()
        self._body_runner = None
        self._body_client = None
        self.deadline = None
        self.trace = None
        self.last_preclick_proof: dict | None = None
        self.verified_self_aliases: list[QQVerifiedSelfAlias] = []
        self._owned_draft: tuple[str, str, str] | None = None
        self._body_token = self._read_token(config.body_token_file)
        agent_token = self._read_token(config.agent_token_file)
        self._owns_agent = agent_client is None
        self.agent = agent_client or httpx.Client(base_url=config.agent_url, timeout=config.request_timeout_seconds,
                                                   headers={"Authorization": "Bearer " + agent_token}, trust_env=False)
        self._owns_llm = llm is None
        if llm is None:
            settings = load_vision_settings(config.model_config_path, model=config.vision_model)
            # This recognizer only transcribes visible UI geometry. Keep this
            # provider-specific budget local; character and decision models use
            # their own unchanged clients and settings.
            payload = ({"reasoning_effort": "low", "response_format": {"type": "json_object"}}
                       if urlsplit(settings.llm_base_url).hostname == "api.deepseek.com"
                       and settings.llm_model.startswith("deepseek") else {})
            self.llm = OpenAICompatClient(settings, extra_payload=payload)
        else:
            self.llm = llm

    @staticmethod
    def _read_token(path: Path) -> str:
        try:
            path = Path(path)
            if path.stat().st_size > 16384:
                raise ValueError("token size")
            token = path.read_text(encoding="utf-8").strip()
            if len(token) < 32 or len(token) > 4096 or any(char.isspace() for char in token):
                raise ValueError("token shape")
            return token
        except Exception:
            raise DesktopError("private_token_unavailable") from None

    def close(self):
        if self._body_runner is not None:
            try:
                if self._body_client is not None:
                    self._body_runner.run(self._body_client.close())
            finally:
                self._body_runner.close()
                self._body_runner = self._body_client = None
        if self._owns_agent:
            self.agent.close()
        if self._owns_llm:
            self.llm.close()

    def _check_stop(self):
        if self.should_stop():
            raise InterruptedError("qq_watcher_paused")
        if self.deadline is not None:
            self.deadline.check()

    def _timeout(self) -> float:
        return min(self.config.request_timeout_seconds, self.deadline.check()) if self.deadline else self.config.request_timeout_seconds

    def _stage(self, name: str):
        return self.trace.stage(name) if self.trace is not None else nullcontext()

    def _count(self, name: str):
        if self.trace is not None:
            self.trace.add_counter(name)

    def _vision_client(self):
        if self.deadline is None:
            return self.llm
        backend = self

        class BoundedVision:
            def complete_chat_result(self, *args, **kwargs):
                backend._check_stop()
                kwargs["timeout_seconds"] = backend._timeout()
                response = backend.llm.complete_chat_result(*args, **kwargs)
                backend._check_stop()
                return response

        return BoundedVision()

    @contextmanager
    def turn(self, *, deadline=None, trace=None):
        previous = self.deadline, self.trace
        self.deadline, self.trace = deadline, trace
        try:
            yield self
        finally:
            self.deadline, self.trace = previous

    def accessibility_scope(self) -> dict:
        x, y, width, height = self.config.window_rect
        return {"window_id": self.config.window_id, "app_id": self.config.app_id,
                "window_rect": {"x": x, "y": y, "width": width, "height": height},
                "maximum_duration_ms": 1000}

    def _agent_health(self):
        self._check_stop()
        try:
            response = self.agent.get("/agent/v1/health", timeout=self._timeout())
            response.raise_for_status()
            health = response.json()
            if (health.get("body_instance_id") != self.config.body_instance_id
                    or health.get("environment_revision") != self.config.environment_revision
                    or health.get("status") != "healthy" or health.get("takeover_active") is not False):
                if self.config.identity_cache_enabled:
                    self.invalidate_identity_cache()
                raise ValueError("agent identity or state")
        except Exception:
            raise DesktopError("dedicated_agent_unavailable") from None

    @contextmanager
    def _observation_gate(self):
        """Keep short AX reads/captures and mutations from racing each other.

        Never hold this gate while interpreting evidence or calling a model.
        The event deadline includes queueing, and sensing has its own budget.
        """
        self._check_stop()
        gate = self._observation_lock
        if gate is None:
            yield
            return
        with self._stage("observation_gate_wait"):
            acquired = gate.acquire(timeout=min(1.5, self._timeout()))
        if not acquired:
            raise DesktopError("observation_gate_busy")
        try:
            self._check_stop()
            yield
        finally:
            gate.release()

    def _act(self, primitive: dict, *, reservation_id: str | None = None):
        with self._observation_gate():
            return self._act_unlocked(primitive, reservation_id=reservation_id)

    def _act_unlocked(self, primitive: dict, *, reservation_id: str | None = None):
        # Even an unsuccessful action request ends the reusable capture phase.
        # Pointer movement can alter hover state, so it is a mutation too.
        self._gui_generation += 1
        self._latest_capture = None
        self._latest_capture_started_mono = None
        self._agent_health()
        self._check_stop()
        action_id = "qq-send-" + hashlib.sha256(reservation_id.encode()).hexdigest() if reservation_id else str(uuid4())
        # Agent journal keys step_id globally, not within action_id.
        step_id = action_id + ":send-once" if reservation_id else str(uuid4())
        payload = {"schema_version": "agent-contract-v1", "body_instance_id": self.config.body_instance_id,
                   "environment_revision": self.config.environment_revision, "action_id": action_id,
                   "step_id": step_id, "nonce": str(uuid4()),
                   "deadline": (datetime.now(timezone.utc) + timedelta(seconds=self._timeout())).isoformat(),
                   "primitive": primitive}
        if self.config.accessibility_enabled:
            x, y, width, height = self.config.window_rect
            payload["expected_window"] = {"window_id": self.config.window_id,
                "app_id": self.config.app_id,
                "rect": {"x": x, "y": y, "width": width, "height": height}}
        try:
            response = self.agent.post("/agent/v1/primitives:execute", json=payload, timeout=self._timeout())
            acknowledged_at = datetime.now(timezone.utc)
            response.raise_for_status()
            result = response.json()
            if result.get("success") is not True or result.get("action_id") != action_id or result.get("step_id") != step_id:
                raise ValueError("primitive failed")
        except Exception:
            raise DesktopError("primitive_outcome_unavailable") from None
        return acknowledged_at

    def content_digest(self, frame: Frame) -> str:
        if (frame.width, frame.height) != (self.config.width, self.config.height):
            raise DesktopError("desktop_geometry_changed")
        return _pixels(frame.png, (self.config.title_roi, self.config.chat_roi))

    def frame_evidence_refs(self, frame: Frame) -> list[str]:
        refs = [frame.evidence_ref]
        if frame.capture_id in self._a11y_frames and frame.a11y_evidence_ref:
            refs.append(frame.a11y_evidence_ref)
        return list(dict.fromkeys(refs))

    def capture(self) -> Frame:
        with self._observation_gate():
            return self._capture_unlocked()

    def _capture_unlocked(self) -> Frame:
        self._check_stop()
        self._latest_capture = None
        self._latest_capture_started_mono = None
        if self._body_runner is None:
            self._body_runner = asyncio.Runner()
            self._body_client = MistyAIClient(self.config.body_url, token=self._body_token,
                                            timeout_seconds=self.config.request_timeout_seconds,
                                            evidence_byte_cache_bytes=16 * 1024 * 1024)

        async def collect():
            async with asyncio.timeout(self._timeout()):
                scope = self.accessibility_scope() if self.config.accessibility_enabled else None
                started_mono = time.monotonic()
                observation = await self._body_client.observe(a11y_scope=scope)
                png = await self._body_client.get_evidence(observation.frame.sha256, expected_media_type="image/png")
                tree = None
                if scope is not None and observation.a11y_ref:
                    raw = await self._body_client.get_evidence(observation.a11y_ref.removeprefix("evidence://sha256/"),
                                                              expected_media_type="application/vnd.mistyai.a11y+json")
                    tree = json.loads(raw)
                return observation, png, tree, started_mono

        try:
            with self._stage("body_capture"):
                self._count("body_capture")
                observation, png, tree, started_mono = self._body_runner.run(collect())
                if self.config.accessibility_enabled and tree is None:
                    # A just-opened card can move while the scoped snapshot is
                    # captured. Reacquire the entire evidence pair once; never
                    # attach later nodes to the earlier screenshot.
                    self._count("accessibility_pair_retry")
                    self._check_stop()
                    time.sleep(.15)
                    self._count("body_capture")
                    observation, png, tree, started_mono = self._body_runner.run(collect())
                self._check_stop()
                frame = self.frame_from_observation(observation, png, a11y_tree=tree)
                self._remember_capture(frame, started_mono)
                return frame
        except DesktopError as exc:
            if self.config.identity_cache_enabled and str(exc) in {"dedicated_window_not_active", "desktop_geometry_changed"}:
                self.invalidate_identity_cache()
            raise
        except InterruptedError:
            raise
        except Exception:
            self._check_stop()
            raise DesktopError("verified_frame_unavailable") from None

    def _remember_capture(self, frame: Frame, started_mono: float) -> None:
        self._last_verified_capture = frame
        self._latest_capture = frame
        self._latest_capture_started_mono = started_mono
        self._latest_capture_generation = self._gui_generation
        self._capture_times[frame.capture_id] = (frame, started_mono)
        self._capture_times.move_to_end(frame.capture_id)
        while len(self._capture_times) > 128:
            self._capture_times.popitem(last=False)

    def _frame_is_recent(self, frame: Frame, maximum_seconds: float | None = None) -> bool:
        maximum = self.config.maximum_frame_age_seconds if maximum_seconds is None else maximum_seconds
        known = self._capture_times.get(frame.capture_id)
        if known is not None and known[0] is frame:
            # Agent/VM wall clocks may differ. Only a capture obtained by this
            # backend has a trusted local request-start time; imports never do.
            return 0 <= time.monotonic() - known[1] <= maximum
        return -2 <= (datetime.now(timezone.utc) - frame.captured_at).total_seconds() <= maximum

    def _current_ax_frame_or_capture(self) -> Frame:
        """Reuse only this backend's unchanged, locally timed AX capture phase.

        Reuse never authorizes a stale or model-only frame. Every subsequent
        AX GUI action carries a server-checked current window precondition.
        Final send proof and post-send evidence call capture directly.
        """
        self._check_stop()
        frame = self._latest_capture
        started = self._latest_capture_started_mono
        if (self.config.accessibility_enabled and frame is not None and started is not None
                and self._latest_capture_generation == self._gui_generation
                and frame.capture_id in self._a11y_frames and frame.a11y_tree is not None
                and frame.a11y_evidence_ref is not None
                and frame.window_id == self.config.window_id
                and frame.active_window_id == self.config.window_id
                and frame.window_rect == self.config.window_rect
                and (frame.width, frame.height) == (self.config.width, self.config.height)
                and 0 <= time.monotonic() - started <= 1):
            reading = self._screens.get(frame.capture_id)
            if (reading is not None and not reading.blocked and reading.frame_id == frame.capture_id
                    and reading.evidence_ref == frame.evidence_ref):
                self._count("fresh_frame_reuse")
                return frame
        return self.capture()

    def frame_from_observation(self, observation: NativeObservation | dict, png: bytes, *, a11y_tree: dict | None = None) -> Frame:
        """Rebuild evidence-bound pixels; this does not authorize old coordinates."""
        observation = NativeObservation.model_validate(observation)
        digest = hashlib.sha256(png).hexdigest()
        if digest != observation.frame.sha256 or observation.frame.evidence_ref != "evidence://sha256/" + digest:
            raise DesktopError("frame_digest_mismatch")
        if observation.frame.media_type != "image/png":
            raise DesktopError("frame_media_type_mismatch")
        active = observation.active_window
        window = next((w for w in observation.windows if w.window_id == self.config.window_id), None)
        if (observation.body_instance_id != self.config.body_instance_id
                or observation.environment_revision != self.config.environment_revision
                or window is None or not window.visible or window.app_id != self.config.app_id
                or active is None or active.app_id != self.config.app_id):
            raise DesktopError("dedicated_window_not_active")
        rect = (window.rect.x, window.rect.y, window.rect.width, window.rect.height)
        dimensions = _png_dimensions(png)
        if (dimensions != (self.config.width, self.config.height) or rect != self.config.window_rect
                or dimensions != (observation.frame.width, observation.frame.height)
                or dimensions != (observation.display.width, observation.display.height)):
            raise DesktopError("desktop_geometry_changed")
        frame = Frame(observation.observation_id, observation.captured_at, observation.frame.evidence_ref,
                      png, *dimensions, window.window_id, rect, active.window_id)
        return Frame(**{**frame.__dict__, "content_digest": self.content_digest(frame),
                        "a11y_tree": a11y_tree, "a11y_evidence_ref": observation.a11y_ref if a11y_tree is not None else None})

    def load_baseline(self, path: Path | str) -> Frame:
        """Read a prior Body JSON and same-name PNG for the first ledger baseline.

        Only pure inspect/ledger work may use this old frame. _fresh rejects
        expired geometry before any click, including frames loaded here.
        """
        try:
            path = Path(path)
            image_path = path.with_suffix(".png")
            if path.stat().st_size > 1024 * 1024 or image_path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("baseline too large")
            observation = NativeObservation.model_validate_json(path.read_text(encoding="utf-8"))
            return self.frame_from_observation(observation, image_path.read_bytes())
        except DesktopError:
            raise
        except Exception:
            raise DesktopError("baseline_evidence_unavailable") from None

    def inspect(self, frame: Frame) -> GroundedScreen:
        self._check_stop()
        if frame.capture_id not in self._screens and self.config.accessibility_enabled and frame.a11y_tree is not None:
            from harness.qq.accessibility import AccessibilityUnavailable, project_qq_accessibility
            try:
                with self._stage("accessibility_projection"):
                    self._count("accessibility_projection")
                    projected = project_qq_accessibility(frame.a11y_tree, frame_id=frame.capture_id,
                        evidence_ref=frame.evidence_ref, a11y_evidence_ref=frame.a11y_evidence_ref,
                        width=frame.width, height=frame.height, window_id=frame.window_id,
                        window_rect=frame.window_rect, app_id=self.config.app_id, frame_captured_at=frame.captured_at,
                        expected_frame_sha256=hashlib.sha256(frame.png).hexdigest())
                    self._screens[frame.capture_id] = self._complete_chat_messages(projected.screen)
                    self._a11y_frames.add(frame.capture_id)
                    while len(self._screens) > 20:
                        removed = next(iter(self._screens))
                        self._screens.pop(removed)
                        self._a11y_frames.discard(removed)
            except AccessibilityUnavailable as exc:
                self._count("accessibility_fallback")
                code = str(exc)
                if re.fullmatch(r"a11y_[a-z0-9_]{1,45}", code):
                    self._count(code)
        if frame.capture_id not in self._screens:
            x, y, width, height = frame.window_rect
            signature = _pixels(frame.png, ((x, y, x + width, y + height),))
            cached = self._pixel_screens.get(signature)
            if cached is None:
                initially_fresh = self._frame_is_recent(frame)
                for attempt in range(3):
                    self._check_stop()
                    # Historical baseline/probe reads remain supported. A live
                    # frame that expires during retries stops; actions always
                    # independently enforce their existing freshness checks.
                    if initially_fresh and not self._frame_is_recent(frame):
                        raise DesktopError("grounding_expired")
                    try:
                        self._count("vision_calls")
                        with self._stage("vision"):
                            screen = inspect_qq_frame(self._vision_client(), frame.png, frame_id=frame.capture_id, evidence_ref=frame.evidence_ref)
                        break
                    except GroundingError:
                        if attempt == 2:
                            raise
                self._check_stop()
                screen = self._complete_chat_messages(screen)
                self._pixel_screens[signature] = screen
                while len(self._pixel_screens) > 8:
                    self._pixel_screens.popitem(last=False)
            else:
                self._pixel_screens.move_to_end(signature)
                screen = cached.model_copy(update={"frame_id": frame.capture_id, "evidence_ref": frame.evidence_ref})
            self._check_stop()
            self._screens[frame.capture_id] = screen
            while len(self._screens) > 20:
                removed = next(iter(self._screens))
                self._screens.pop(removed)
                self._a11y_frames.discard(removed)
        return self._screens[frame.capture_id]

    def _complete_chat_messages(self, screen: GroundedScreen) -> GroundedScreen:
        """Expose only fully grounded viewport messages, not partial history."""
        if screen.scene != "group_chat":
            return screen
        chat = self.config.chat_roi
        messages = [message for message in screen.messages if all(
            rect is not None and chat[0] <= rect.x < rect.x + rect.width <= chat[2]
            and chat[1] <= rect.y < rect.y + rect.height <= chat[3]
            for rect in (message.bubble, message.avatar))]
        return screen.model_copy(update={"messages": messages})

    def _require_chat(self, frame: Frame, screen: GroundedScreen):
        if (screen.frame_id != frame.capture_id or screen.evidence_ref != frame.evidence_ref
                or (screen.width, screen.height) != (frame.width, frame.height)
                or screen.blocked or screen.scene != "group_chat" or screen.opened_profile is not None
                or frame.active_window_id != self.config.window_id
                or screen.group_title is None or not _title(screen.group_title.display_name)
                or screen.group_title.rect is None):
            if self.config.identity_cache_enabled:
                self.invalidate_identity_cache()
            raise DesktopError("unambiguous_group_chat_required")

    def invalidate_identity_cache(self):
        """End weak UI continuity without deleting persistent person memory."""
        self._identity_epoch = uuid4().hex
        self._group_identity_anchor = None
        self._self_identity = None
        self._member_identities = None
        self._pending_identity_bindings.clear()
        self.verified_self_aliases.clear()
        self._count("identity_cache_invalidations")

    def _identity_frame(self, frame: Frame, screen: GroundedScreen) -> bool:
        return bool(self.config.identity_cache_enabled and frame.capture_id in self._a11y_frames
                    and frame.a11y_tree is not None and frame.a11y_evidence_ref
                    and self._frame_is_recent(frame, 1) and screen.scene == "group_chat"
                    and not screen.blocked and not screen.uncertainties and screen.group_title
                    and screen.opened_profile is None and frame.window_id == self.config.window_id
                    and frame.active_window_id == self.config.window_id
                    and frame.window_rect == self.config.window_rect)

    def _group_identity_matches(self, frame: Frame, screen: GroundedScreen) -> bool:
        anchor = self._group_identity_anchor
        return bool(anchor and self._identity_frame(frame, screen)
                    and _title(screen.group_title.display_name) == anchor[0]
                    and _pixels(frame.png, (self.config.title_roi,)) == anchor[1])

    @staticmethod
    def _portrait_digest(frame: Frame, rect: Rect) -> str | None:
        # A location-independent, exact inner portrait fingerprint. Changes
        # cause a miss; neither nicknames nor tree indices become QQ IDs.
        if rect.width < 16 or rect.height < 16:
            return None
        region = rect.bounds
        if not (0 <= region[0] < region[2] <= frame.width and 0 <= region[1] < region[3] <= frame.height):
            return None
        from PIL import Image
        with Image.open(io.BytesIO(frame.png)) as image:
            crop = image.convert("RGB").crop(region)
            # QQ paints circular portraits over a selectable row background.
            # Hash the inner circle, excluding its antialiased outer edge.
            radius = min(crop.size) / 2 - max(2, min(crop.size) // 10)
            cx, cy = (crop.width - 1) / 2, (crop.height - 1) / 2
            pixels = crop.load()
            rgb = bytes(channel for y in range(crop.height) for x in range(crop.width)
                        if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2 for channel in pixels[x, y])
            return hashlib.sha256(repr(crop.size).encode("ascii") + rgb).hexdigest()

    def _self_visual_hint(self, frame: Frame, screen: GroundedScreen) -> tuple[str, str] | None:
        if not self._group_identity_matches(frame, screen):
            return None
        pattern = re.compile(r"^" + re.escape(self.config.self_display_name_hint.strip()) + r"(?:\s*[（(].*[）)])?$", re.IGNORECASE)
        candidates = [member for member in screen.visible_members if member.display_name and member.row
                      and not member.uncertainties and pattern.fullmatch(member.display_name.strip())]
        if len(candidates) != 1:
            return None
        member = candidates[0]
        row = member.row
        if not self._center_in(row, (1095, 298, 1280, 720)):
            return None
        if member.avatar is not None:
            avatar = member.avatar
        elif row.height == 32 and row.width >= 40:
            # This fixed QQ layout exposes the row but omits its 20px image
            # child. The verified row's icon is inset 10px and centered.
            avatar = Rect(x=row.x + 10, y=row.y + 6, width=20, height=20)
        else:
            return None
        digest = self._portrait_digest(frame, avatar)
        return (member.display_name, digest) if digest else None

    def _identity_scope(self) -> IdentityScope:
        return IdentityScope(self._identity_epoch, self.config.body_instance_id,
            self.config.environment_revision, self.config.window_id,
            self.config.expected_group_id, self.config.self_member_id)

    def _member_visual_hints(self, message: GroundedMessage, frame: Frame,
                             screen: GroundedScreen) -> tuple[VisualHint, tuple[VisualHint, ...]] | None:
        if (frame.capture_id not in self._a11y_frames or not frame.a11y_evidence_ref
                or message not in screen.messages or message.direction != "incoming"
                or not message.display_name or message.uncertainties or message.avatar is None):
            return None
        roster_matches = [member for member in screen.visible_members if member.display_name == message.display_name]
        if any(member.uncertainties or member.row is None for member in roster_matches):
            return None
        if len({member.row.bounds for member in roster_matches}) > 1:
            # Separate roster rows are distinct visible people, even when
            # their names and portraits happen to be identical.
            return None
        hints = []
        for candidate in screen.messages:
            if candidate.direction != "incoming" or candidate.display_name != message.display_name:
                continue
            if candidate.uncertainties or candidate.avatar is None or candidate.bubble is None:
                return None
            avatar, bubble = candidate.avatar, candidate.bubble
            chat = self.config.chat_roi
            if not all(chat[0] <= rect.x < rect.x + rect.width <= chat[2]
                       and chat[1] <= rect.y < rect.y + rect.height <= chat[3] for rect in (avatar, bubble)):
                return None
            digest = self._portrait_digest(frame, avatar)
            if digest is None:
                return None
            try:
                hints.append(VisualHint(candidate.display_name, digest))
            except ValueError:
                return None
        digest = self._portrait_digest(frame, message.avatar)
        return (VisualHint(message.display_name, digest), tuple(hints)) if digest and hints else None

    def remember_verified_identity(self, message: GroundedMessage, frame: Frame,
                                   attributed: AttributedMessage, profile_record_id: str) -> bool:
        """Bind only the exact successful card result after its profile is durable."""
        with self._lock:
            if not self.config.identity_cache_enabled:
                return False
            pending = self._pending_identity_bindings.pop(attributed.capture_id, None)
            if pending is None:
                return False
            scope, hint, visible, result, source, source_message = pending
            if (result is not attributed or source is not frame or source_message != message
                    or scope != self._identity_scope() or attributed.profile_record_id is not None):
                return False
            identity = VerifiedIdentity(attributed.member_id, profile_record_id, attributed.capture_id,
                attributed.observed_at, attributed.display_name, tuple(attributed.profile_evidence_refs),
                tuple(self.frame_evidence_refs(frame)), tuple(attributed.evidence_refs))
            if self._member_identities is None:
                self._member_identities = SessionIdentityCache()
            stored = self._member_identities.record(scope, hint, identity, visible)
            self._count("member_identity_cache_recorded" if stored else "member_identity_cache_rejected")
            return stored

    @staticmethod
    def _center_in(rect: Rect, region: tuple[int, int, int, int]) -> bool:
        x, y = rect.center
        return region[0] <= x < region[2] and region[1] <= y < region[3]

    def _refine_sticker_box(self, frame: Frame, predicted: Rect) -> tuple[int, int, int, int]:
        """Find a dense foreground component, never a freely expanded mask."""
        from PIL import Image

        original = predicted.bounds
        if predicted.width > 512 or predicted.height > 384:
            return original
        chat = self.config.chat_roi
        sample = (max(chat[0], original[0] - 10), max(chat[1], original[1] - 10),
                  min(chat[2], original[2] + 10), min(chat[3], original[3] + 10))
        search = (max(chat[0], original[0] - 8), max(chat[1], original[1] - 8),
                  min(chat[2], original[2] + 8), min(chat[3], original[3] + 8))
        with Image.open(io.BytesIO(frame.png)) as image:
            pixels = image.convert("RGB").load()
            ring = [pixels[x, y] for y in range(sample[1], sample[3]) for x in range(sample[0], sample[2])
                    if not (search[0] <= x < search[2] and search[1] <= y < search[3])]
            if not ring:
                return original
            background, count = Counter(ring).most_common(1)[0]
            if count < len(ring) * 0.8:
                return original
            center = predicted.center
            if pixels[center] == background:
                return original
            component, queue = {center}, deque([center])
            while queue:
                x, y = queue.popleft()
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                    point = (x + dx, y + dy)
                    if (search[0] <= point[0] < search[2] and search[1] <= point[1] < search[3]
                            and point not in component and pixels[point] != background):
                        component.add(point)
                        queue.append(point)
            box = (min(x for x, _ in component), min(y for _, y in component),
                   max(x for x, _ in component) + 1, max(y for _, y in component) + 1)
            area = (box[2] - box[0]) * (box[3] - box[1])
            if (len(component) < max(64, predicted.width * predicted.height / 4) or len(component) < area / 2
                    or any(abs(a - b) > 8 for a, b in zip(box, original))
                    or box[0] <= search[0] or box[1] <= search[1] or box[2] >= search[2] or box[3] >= search[3]):
                return original
            return box

    def _sticker_regions(self, frame: Frame, screen: GroundedScreen) -> tuple[tuple[int, int, int, int], ...]:
        regions = []
        chat = self.config.chat_roi
        for message in screen.messages:
            if message.kind != "sticker" or message.uncertainties or message.direction == "unknown" or message.bubble is None:
                continue
            box = self._refine_sticker_box(frame, message.bubble)
            if not (chat[0] <= box[0] < box[2] <= chat[2] and chat[1] <= box[1] < box[3] <= chat[3]):
                return ()
            # Avatar estimates can include wide padding. Their centers, and
            # entire known non-sticker bubbles, must stay outside the mask.
            if any(other.avatar and box[0] <= other.avatar.center[0] < box[2]
                   and box[1] <= other.avatar.center[1] < box[3] for other in screen.messages):
                return ()
            protected = [other.bubble for other in screen.messages if other.kind != "sticker"]
            for rect in protected:
                if rect and box[0] < rect.x + rect.width and rect.x < box[2] and box[1] < rect.y + rect.height and rect.y < box[3]:
                    return ()
            regions.append(box)
        if len(regions) > 8 or sum((r[2] - r[0]) * (r[3] - r[1]) for r in regions) > (chat[2] - chat[0]) * (chat[3] - chat[1]) / 3:
            return ()
        return tuple(regions)

    def _grounded_text_messages(self, screen: GroundedScreen) -> list[GroundedMessage] | None:
        messages = [message for message in screen.messages if message.kind != "sticker"]
        if not messages:
            return None
        chat = self.config.chat_roi
        for message in messages:
            if (message.kind != "text" or message.direction == "unknown" or not message.text or not message.text.strip()
                    or message.uncertainties or message.avatar is None or message.bubble is None):
                return None
            avatar, bubble = message.avatar, message.bubble
            for rect in (avatar, bubble):
                if not (chat[0] <= rect.x < rect.x + rect.width <= chat[2]
                        and chat[1] <= rect.y < rect.y + rect.height <= chat[3]
                        and rect.width >= 4 and rect.height >= 4):
                    return None
            if avatar.width > 128 or avatar.height > 128:
                return None
            if not (avatar.y < bubble.y + bubble.height and bubble.y < avatar.y + avatar.height):
                return None
            if not (avatar.x + avatar.width <= bubble.x + 20 if message.direction == "incoming"
                    else avatar.x >= bubble.x + bubble.width - 20):
                return None
        return messages

    def _text_contexts_match(self, expected: Frame, actual: Frame, before: GroundedScreen, after: GroundedScreen) -> bool:
        old = self._grounded_text_messages(before)
        new = self._grounded_text_messages(after)
        if old is None or new is None:
            return False
        if [(m.kind, m.direction, m.text) for m in old] != [(m.kind, m.direction, m.text) for m in new]:
            return False
        if _title(before.group_title.display_name) != _title(after.group_title.display_name):
            return False
        regions = (self.config.title_roi,) + tuple(rect.bounds for message in old for rect in (message.bubble, message.avatar))
        # These exact pixels and a fresh transcription prove the ordered text
        # conversation is unchanged. Sticker artwork and unrelated background
        # pixels are deliberately irrelevant to this narrower proof.
        return _pixels(expected.png, regions) == _pixels(actual.png, regions)

    def _contexts_match(self, expected: Frame, actual: Frame, *, expected_screen: GroundedScreen | None = None,
                        actual_screen: GroundedScreen | None = None) -> bool:
        if (expected.window_rect != actual.window_rect or expected.window_id != actual.window_id
                or expected.active_window_id != actual.active_window_id
                or (expected.width, expected.height) != (actual.width, actual.height)):
            return False
        if expected_screen is not None:
            self._require_chat(expected, expected_screen)
        if actual_screen is not None:
            self._require_chat(actual, actual_screen)
        if self.config.accessibility_enabled and expected.a11y_tree is not None and actual.a11y_tree is not None:
            before = expected_screen or self.inspect(expected)
            self._require_chat(expected, before)
            after = actual_screen or self.inspect(actual)
            self._require_chat(actual, after)
            if expected.capture_id in self._a11y_frames and actual.capture_id in self._a11y_frames:
                def content(reading):
                    return [(
                        m.direction, m.kind, m.display_name, m.text, tuple(m.uncertainties),
                        m.avatar.bounds if m.avatar else None, m.bubble.bounds if m.bubble else None,
                    ) for m in reading.messages]
                return (_title(before.group_title.display_name) == _title(after.group_title.display_name)
                        and content(before) == content(after))
        if expected.content_digest == actual.content_digest:
            return True
        before = expected_screen or self.inspect(expected)
        self._require_chat(expected, before)
        masks = self._sticker_regions(expected, before)
        regions = (self.config.title_roi, self.config.chat_roi)
        sticker_pixels_match = bool(masks) and _pixels(expected.png, regions, masks=masks) == _pixels(actual.png, regions, masks=masks)
        if not sticker_pixels_match and self._grounded_text_messages(before) is None:
            return False
        # Never reuse a masked inference result. Changed pixels get a new
        # full-image reading, so a sticker replaced by text, moved messages, or
        # changed attribution cannot inherit the previous frame's exemption.
        after = actual_screen or self.inspect(actual)
        self._require_chat(actual, after)
        fresh_masks = self._sticker_regions(actual, after) if sticker_pixels_match else ()
        # The unchanged pixels already prove the static title, text and avatars
        # have not changed. Re-OCR of those pixels adds no authority. Each old
        # animated region must instead remain uniquely recognized as a sticker
        # with exactly the same independently localized foreground bounds.
        if sticker_pixels_match and all(fresh_masks.count(mask) == 1 for mask in masks):
            return True
        return self._text_contexts_match(expected, actual, before, after)

    def _fresh(self, frame: Frame, screen: GroundedScreen, *, anchor: Rect | None = None,
               force_capture: bool = False) -> Frame:
        self._check_stop()
        if (screen.frame_id != frame.capture_id or screen.evidence_ref != frame.evidence_ref
                or (screen.width, screen.height) != (frame.width, frame.height)):
            raise DesktopError("frame_binding_mismatch")
        if not self._frame_is_recent(frame):
            raise DesktopError("grounding_expired")
        fresh = self.capture() if force_capture else self._current_ax_frame_or_capture()
        if fresh.window_rect != frame.window_rect or fresh.active_window_id != frame.active_window_id:
            raise DesktopError("window_changed_after_grounding")
        regions = []
        if screen.scene == "group_chat":
            if not self._contexts_match(frame, fresh, expected_screen=screen):
                raise DesktopError("chat_changed_after_grounding")
        elif screen.opened_profile:
            regions.append(screen.opened_profile.card.bounds)
        else:
            raise DesktopError("unsupported_interaction_scene")
        if anchor:
            regions.append(anchor.bounds)
        if regions and _pixels(frame.png, tuple(regions)) != _pixels(fresh.png, tuple(regions)):
            raise DesktopError("target_changed_after_grounding")
        return fresh

    def _click(self, frame: Frame, screen: GroundedScreen, anchor: Rect):
        self._fresh(frame, screen, anchor=anchor)
        self._act({"kind": "click", "x": anchor.center[0], "y": anchor.center[1], "button": "left", "count": 1})
        time.sleep(self.config.settle_seconds)

    def _click_group_title(self, frame: Frame, screen: GroundedScreen):
        # Opening the group card is independent of moving chat content. This
        # click grants no group authority; verify_group still checks the actual
        # numeric group ID in the resulting fresh profile before returning.
        self._check_stop()
        self._require_chat(frame, screen)
        anchor = screen.group_title.rect
        if not self._center_in(anchor, self.config.title_roi):
            raise DesktopError("group_title_anchor_outside_region")
        if not self._frame_is_recent(frame):
            raise DesktopError("grounding_expired")
        fresh = self._current_ax_frame_or_capture()
        if (fresh.window_rect != frame.window_rect or fresh.window_id != frame.window_id
                or fresh.active_window_id != frame.active_window_id
                or (fresh.width, fresh.height) != (frame.width, frame.height)):
            raise DesktopError("window_changed_after_grounding")
        regions = (self.config.title_roi, anchor.bounds)
        if _pixels(frame.png, regions) != _pixels(fresh.png, regions):
            raise DesktopError("group_title_changed_after_grounding")
        if not self._frame_is_recent(fresh):
            raise DesktopError("grounding_expired")
        # QQ exposes one wide title span; its member-count end opens the
        # profile while the name portion may only select text. Stay inside the
        # live, pixel-checked AT-SPI button, then verify the numeric card.
        x = anchor.x + anchor.width - min(12, anchor.width // 2) if frame.capture_id in self._a11y_frames else anchor.center[0]
        self._act({"kind": "click", "x": x, "y": anchor.center[1], "button": "left", "count": 1})
        time.sleep(self.config.settle_seconds)

    def _fresh_profile_for_escape(self, frame: Frame, screen: GroundedScreen) -> Frame:
        """Escape has no coordinate target; recheck the actual profile identity."""
        self._check_stop()
        if screen.frame_id != frame.capture_id or screen.evidence_ref != frame.evidence_ref:
            raise DesktopError("frame_binding_mismatch")
        if not self._frame_is_recent(frame):
            raise DesktopError("grounding_expired")
        before = screen.opened_profile
        if (screen.blocked or screen.scene not in {"group_profile", "member_profile"} or before is None
                or before.kind != ("group" if screen.scene == "group_profile" else "member")
                or before.uncertainties
                or (before.group_id if before.kind == "group" else before.member_id) is None):
            raise DesktopError("profile_cannot_be_dismissed_safely")
        fresh = self._current_ax_frame_or_capture()
        if (fresh.window_rect != frame.window_rect or fresh.window_id != frame.window_id
                or fresh.active_window_id != frame.active_window_id
                or (fresh.width, fresh.height) != (frame.width, frame.height)):
            raise DesktopError("window_changed_after_grounding")
        reading = self.inspect(fresh)
        after = reading.opened_profile
        if (reading.frame_id != fresh.capture_id or reading.evidence_ref != fresh.evidence_ref
                or (reading.width, reading.height) != (fresh.width, fresh.height)
                or reading.blocked or reading.scene != screen.scene or after is None
                or after.kind != before.kind or after.member_id != before.member_id or after.group_id != before.group_id
                or after.uncertainties):
            raise DesktopError("profile_changed_before_escape")
        if not self._frame_is_recent(fresh):
            raise DesktopError("grounding_expired")
        return fresh

    def _dismiss_group_via_blank_bar(self, frame: Frame, screen: GroundedScreen) -> bool:
        """Close a verified QQ popover using freshly proven empty window chrome.

        QQ can leave a group card open after Escape while its editor has focus.
        This path never applies to a member card, unknown dialog or vision-only
        reading. The main window, numeric group and blank target are all checked.
        """
        card = screen.opened_profile
        if (not self.config.accessibility_enabled or frame.capture_id not in self._a11y_frames
                or screen.scene != "group_profile" or screen.blocked or screen.uncertainties
                or screen.group_title is None
                or card is None or card.kind != "group" or card.group_id != self.config.expected_group_id
                or card.uncertainties):
            return False
        fresh = self._fresh_profile_for_escape(frame, screen)
        if (fresh.capture_id not in self._a11y_frames or fresh.active_window_id != self.config.window_id
                or fresh.window_id != self.config.window_id):
            return False
        reading = self._screens[fresh.capture_id]
        current = reading.opened_profile
        wx, wy, width, _ = fresh.window_rect
        x, y = wx + width // 2, wy + 18
        patch = (x - 10, y - 7, x + 10, y + 7)
        if (fresh.window_rect != self.config.window_rect or current is None
                or current.kind != "group" or current.group_id != card.group_id or current.uncertainties
                or reading.group_title is None
                or _title(reading.group_title.display_name) != _title(screen.group_title.display_name)
                or current.card is None or y + 7 >= self.config.title_roi[1]
                or not (0 <= patch[0] < patch[2] <= fresh.width and 0 <= patch[1] < patch[3] <= fresh.height)
                or current.card.x <= x < current.card.x + current.card.width
                and current.card.y <= y < current.card.y + current.card.height):
            return False
        interactive = {"push button", "button", "link", "menu item", "check box", "toggle button", "entry", "text"}
        for node in fresh.a11y_tree["nodes"]:
            if node.get("role") not in interactive:
                continue
            rect = node.get("rect") or {}
            if all(type(rect.get(key)) is int for key in ("x", "y", "width", "height")):
                if (rect["x"] <= x < rect["x"] + rect["width"]
                        and rect["y"] <= y < rect["y"] + rect["height"]):
                    return False
        from PIL import Image
        with Image.open(io.BytesIO(fresh.png)) as pixels:
            colors = pixels.convert("RGB").crop(patch).getcolors(maxcolors=280)
        if not colors or max(count for count, _ in colors) < 280 * .98:
            return False
        self._check_stop()
        self._act({"kind": "click", "x": x, "y": y, "button": "left", "count": 1})
        self._count("group_card_blank_dismiss")
        return True

    def _dismiss(self, frame: Frame, screen: GroundedScreen) -> tuple[Frame, GroundedScreen]:
        if screen.blocked or screen.opened_profile is None:
            raise DesktopError("profile_cannot_be_dismissed_safely")
        blank_dismissed = self._dismiss_group_via_blank_bar(frame, screen)
        if blank_dismissed:
            pass  # The pointer is already in the verified empty title bar.
        elif screen.opened_profile.close:
            self._click(frame, screen, screen.opened_profile.close)
        else:
            # Escape is restricted to a freshly verified profile scene; it is
            # never used to dismiss an unknown dialog or login verification.
            self._fresh_profile_for_escape(frame, screen)
            self._act({"kind": "press_keys", "keys": ["escape"]})
            time.sleep(self.config.settle_seconds)
        # Title/avatar hover paints would otherwise change the chat digest when
        # the next action moves the pointer. Park within the fixed QQ title bar,
        # outside title/chat/input regions, without clicking or activating it.
        if not blank_dismissed:
            self._act({"kind": "move_pointer", "x": 640, "y": 45})
        time.sleep(self.config.settle_seconds)
        chat = self.capture()
        reading = self.inspect(chat)
        self._require_chat(chat, reading)
        if blank_dismissed and _title(reading.group_title.display_name) != _title(screen.group_title.display_name):
            raise DesktopError("group_changed_after_profile")
        return chat, reading

    def _capture_profile_transition(self, chat_title: str) -> tuple[Frame, GroundedScreen]:
        """Wait for one already-clicked card without ever repeating its click.

        Only an unchanged chat scene permits two further whole evidence reads.
        Any card (including a wrong identity), login or dialog returns at once
        to the caller's existing strict profile/number checks.
        """
        for attempt in range(3):
            self._check_stop()
            frame = self.capture()
            screen = self.inspect(frame)
            self._check_stop()
            if screen.scene != "group_chat" or screen.blocked or screen.opened_profile is not None:
                return frame, screen
            self._require_chat(frame, screen)
            if _title(screen.group_title.display_name) != chat_title:
                raise DesktopError("profile_transition_chat_changed")
            if attempt == 2:
                return frame, screen
            self._count("profile_transition_retries")
            time.sleep(min(.15, self._timeout()))
        raise AssertionError("unreachable profile transition")

    def verify_group(self, frame: Frame | None = None) -> GroupVerification:
        with self._lock:
            if frame is None:
                frame = self._current_ax_frame_or_capture() if self.config.identity_cache_enabled else self.capture()
            elif not self._frame_is_recent(frame):
                raise DesktopError("grounding_expired")
            screen = self.inspect(frame)
            if (not screen.blocked and screen.scene in {"group_profile", "member_profile"}
                    and screen.opened_profile is not None):
                # Resume a paused card workflow by closing only a positively
                # grounded profile, then repeat the full numerical group check.
                # The dismissed card grants no identity or send authority.
                frame, screen = self._dismiss(frame, screen)
            self._require_chat(frame, screen)
            if self.config.identity_cache_enabled and not self._frame_is_recent(frame, 1):
                frame = self.capture()
                screen = self.inspect(frame)
                self._require_chat(frame, screen)
            if self._group_identity_matches(frame, screen):
                title, _pixels_digest, original_refs = self._group_identity_anchor
                self._group = GroupVerification(self.config.expected_group_id, title,
                    list(dict.fromkeys([*original_refs, *self.frame_evidence_refs(frame)])), frame, screen)
                self._count("group_identity_cache_hit")
                return self._group
            if self.config.identity_cache_enabled and self._group_identity_anchor is not None:
                self.invalidate_identity_cache()
            anchor = screen.group_title.rect
            if not self._center_in(anchor, self.config.title_roi):
                raise DesktopError("group_title_anchor_outside_region")
            title = _title(screen.group_title.display_name)
            self._click_group_title(frame, screen)
            profile_frame, profile_screen = self._capture_profile_transition(title)
            profile = profile_screen.opened_profile
            if (profile_screen.blocked or profile_screen.scene != "group_profile" or profile is None
                    or profile.kind != "group" or profile.group_id != self.config.expected_group_id
                    or profile.uncertainties or _title(profile.display_name) != title):
                raise DesktopError("group_profile_not_verified")
            chat, reading = self._dismiss(profile_frame, profile_screen)
            if _title(reading.group_title.display_name) != title:
                raise DesktopError("group_changed_after_profile")
            self._group = GroupVerification(self.config.expected_group_id, title,
                                            list(dict.fromkeys(self.frame_evidence_refs(profile_frame)
                                                               + self.frame_evidence_refs(chat))), chat, reading)
            if self.config.identity_cache_enabled and chat.capture_id in self._a11y_frames:
                self._group_identity_anchor = (title, _pixels(chat.png, (self.config.title_roi,)),
                                               tuple(self._group.evidence_refs))
                self._count("group_identity_full_verification")
            return self._group

    def attribute(self, message: GroundedMessage, frame: Frame) -> AttributedMessage:
        with self._lock:
            screen = self.inspect(frame)
            self._require_chat(frame, screen)
            if self._group is None or _title(screen.group_title.display_name) != self._group.display_name:
                raise DesktopError("group_identity_required")
            if message not in screen.messages or message.avatar is None or message.bubble is None or message.direction == "unknown":
                raise DesktopError("message_avatar_not_grounded")
            if message.uncertainties or not self._center_in(message.avatar, self.config.chat_roi):
                raise DesktopError("message_attribution_uncertain")
            avatar, bubble = message.avatar, message.bubble
            row_overlap = avatar.y < bubble.y + bubble.height and bubble.y < avatar.y + avatar.height
            correct_side = (avatar.x + avatar.width <= bubble.x + 20 if message.direction == "incoming"
                            else avatar.x >= bubble.x + bubble.width - 20)
            if not row_overlap or not correct_side or not self._center_in(bubble, self.config.chat_roi):
                raise DesktopError("avatar_bubble_relationship_uncertain")
            # A batch can take longer than one model-call TTL. Renew its geometry
            # only with current evidence proving the same title/chat and exact
            # avatar/bubble pixels; never extend a changed screenshot's lifetime.
            original_frame = frame
            fresh = self._current_ax_frame_or_capture() if self.config.identity_cache_enabled else self.capture()
            if (not self._contexts_match(frame, fresh, expected_screen=screen) or fresh.window_rect != frame.window_rect
                    or fresh.active_window_id != self.config.window_id
                    or _pixels(frame.png, (avatar.bounds, bubble.bounds)) != _pixels(fresh.png, (avatar.bounds, bubble.bounds))):
                raise DesktopError("message_changed_before_attribution")
            frame = fresh
            screen = self.inspect(frame) if self.config.identity_cache_enabled else screen.model_copy(
                update={"frame_id": frame.capture_id, "evidence_ref": frame.evidence_ref})
            hint_data = self._member_visual_hints(message, frame, screen) if self._group_identity_matches(frame, screen) else None
            scope = self._identity_scope()
            if hint_data is not None and self._member_identities is not None:
                hint, visible = hint_data
                cached = self._member_identities.resolve(scope, hint, visible)
                if cached is not None:
                    refs = list(dict.fromkeys([*cached.evidence_refs,
                                               *self.frame_evidence_refs(original_frame), *self.frame_evidence_refs(frame)]))
                    self._count("member_identity_cache_hit")
                    return AttributedMessage(cached.member_id, cached.display_name, cached.capture_id,
                        cached.observed_at, list(cached.profile_evidence_refs), refs, message.text,
                        f"message:{screen.messages.index(message)}", cached.profile_record_id,
                        "qq_session_cached_identity_v1")
            self._click(frame, screen, message.avatar)
            profile_frame, profile_screen = self._capture_profile_transition(self._group.display_name)
            profile = profile_screen.opened_profile
            if (profile_screen.blocked or profile_screen.scene != "member_profile" or profile is None
                    or profile.kind != "member" or profile.member_id is None or not profile.display_name
                    or profile.uncertainties):
                raise DesktopError("member_profile_not_verified")
            chat, reading = self._dismiss(profile_frame, profile_screen)
            if _title(reading.group_title.display_name) != self._group.display_name:
                raise DesktopError("group_changed_after_attribution")
            attributed = AttributedMessage(profile.member_id, profile.display_name, profile_frame.capture_id,
                                     profile_frame.captured_at, self.frame_evidence_refs(profile_frame),
                                     list(dict.fromkeys(ref for evidence_frame in (original_frame, frame, profile_frame, chat)
                                                        for ref in self.frame_evidence_refs(evidence_frame))),
                                     message.text, f"message:{screen.messages.index(message)}")
            if hint_data is not None and scope == self._identity_scope() and original_frame.capture_id in self._a11y_frames:
                hint, visible = hint_data
                self._pending_identity_bindings[attributed.capture_id] = (
                    scope, hint, visible, attributed, original_frame, message)
                while len(self._pending_identity_bindings) > 256:
                    self._pending_identity_bindings.popitem(last=False)
                self._count("member_identity_full_verification")
            return attributed

    def verify_self(self) -> AttributedMessage:
        with self._lock:
            frame = self._current_ax_frame_or_capture() if self.config.identity_cache_enabled else self.capture()
            screen = self.inspect(frame)
            self._require_chat(frame, screen)
            hint = self._self_visual_hint(frame, screen)
            if hint is not None and self._self_identity is not None and hint == self._self_identity[0]:
                cached = self._self_identity[1]
                self._count("self_identity_cache_hit")
                return replace(cached, evidence_refs=list(dict.fromkeys([
                    *cached.profile_evidence_refs, *self.frame_evidence_refs(frame)])),
                    attribution_method="qq_session_cached_self_v1")
            if self.config.identity_cache_enabled:
                self._self_identity = None
            own = [message for message in screen.messages if message.direction == "outgoing" and message.avatar and message.bubble]
            use_roster = hint is not None or not own
            profile = self._verify_self_from_roster(frame, screen) if use_roster else self.attribute(own[-1], frame)
            if profile.member_id != self.config.self_member_id:
                raise DesktopError("logged_in_account_mismatch")
            if not use_roster:
                linked_alias = own[-1].display_name
            else:
                pattern = re.compile(r"^" + re.escape(self.config.self_display_name_hint.strip()) + r"(?:\s*[（(].*[）)])?$", re.IGNORECASE)
                names = [member.display_name for member in screen.visible_members
                         if member.display_name and pattern.fullmatch(member.display_name.strip())]
                linked_alias = names[0] if len(names) == 1 else None
            self._record_self_aliases(profile, frame, linked_alias)
            if self.config.identity_cache_enabled and hint is not None:
                self._self_identity = (hint, profile)
                self._count("self_identity_full_verification")
            return profile

    def _record_self_aliases(self, profile: AttributedMessage, frame: Frame, linked_alias: str | None) -> None:
        from harness.qq.decision import QQVerifiedSelfAlias

        reading = self._screens.get(profile.capture_id)
        card = reading.opened_profile if reading is not None else None
        if (self._group is None or card is None or reading.blocked or card.uncertainties
                or card.kind != "member" or card.member_id != self.config.self_member_id
                or profile.member_id != self.config.self_member_id or card.verified_self_indicator == "none"):
            return
        refs = list(dict.fromkeys(profile.profile_evidence_refs + self.frame_evidence_refs(frame)))[:16]
        for value in (card.display_name, linked_alias):
            if not value or not value.strip() or len(value.strip()) > 128:
                continue
            alias = QQVerifiedSelfAlias(group_id=self.config.expected_group_id,
                                        self_member_id=self.config.self_member_id, alias=value.strip(), evidence_refs=refs)
            self.verified_self_aliases = [old for old in self.verified_self_aliases if old.alias != alias.alias] + [alias]
        self.verified_self_aliases = self.verified_self_aliases[-16:]

    def _verify_self_from_roster(self, frame: Frame, screen: GroundedScreen) -> AttributedMessage:
        self._require_chat(frame, screen)
        if self._group is None or _title(screen.group_title.display_name) != self._group.display_name:
            raise DesktopError("group_identity_required")
        # The hint selects one visible candidate locally. It is never supplied
        # to vision and grants no identity authority, including after a match.
        pattern = re.compile(r"^" + re.escape(self.config.self_display_name_hint.strip()) + r"(?:\s*[（(].*[）)])?$", re.IGNORECASE)
        candidates = [member for member in screen.visible_members
                      if member.display_name and pattern.fullmatch(member.display_name.strip())]
        if len(candidates) != 1 or candidates[0].uncertainties or candidates[0].row is None:
            raise DesktopError("unique_visible_self_candidate_required")
        member = candidates[0]
        row = member.row
        # A model's row rectangle may include a few padding pixels across the
        # sidebar divider. The click target must stay inside the known member
        # list and the named row must be small, fresh and internally coherent.
        if not (self._center_in(row, (1095, 298, 1280, 720)) and row.width <= 256 and row.height <= 64):
            raise DesktopError("member_roster_anchor_outside_region")
        anchor = member.avatar or row
        if not self._center_in(anchor, (1095, 298, 1280, 720)):
            raise DesktopError("member_roster_anchor_outside_region")
        if not (row.x <= anchor.x < anchor.x + anchor.width <= row.x + row.width
                and row.y <= anchor.y < anchor.y + anchor.height <= row.y + row.height):
            raise DesktopError("member_roster_avatar_relationship_uncertain")
        # The entire named row must still have exactly the grounded pixels;
        # sidebar rows are intentionally absent from the chat content digest.
        fresh = self._fresh(frame, screen, anchor=row)
        self._act({"kind": "click", "x": anchor.center[0], "y": anchor.center[1], "button": "left", "count": 1})
        time.sleep(self.config.settle_seconds)
        profile_frame, profile_screen = self._capture_profile_transition(self._group.display_name)
        card = profile_screen.opened_profile
        if (profile_screen.blocked or profile_screen.scene != "member_profile" or card is None
                or card.kind != "member" or card.member_id != self.config.self_member_id
                or not card.display_name or card.uncertainties):
            raise DesktopError("self_roster_profile_not_verified")
        # Any account can open the target member's card. Require a genuine
        # own-profile UI marker as well as the number before treating it as the
        # currently signed-in account; edit-remark controls never qualify.
        if card.verified_self_indicator == "none":
            raise DesktopError("self_profile_marker_required")
        chat, reading = self._dismiss(profile_frame, profile_screen)
        if _title(reading.group_title.display_name) != self._group.display_name:
            raise DesktopError("group_changed_after_self_profile")
        return AttributedMessage(card.member_id, card.display_name, profile_frame.capture_id, profile_frame.captured_at,
                                 self.frame_evidence_refs(profile_frame),
                                 list(dict.fromkeys(ref for evidence_frame in (frame, fresh, profile_frame, chat)
                                                    for ref in self.frame_evidence_refs(evidence_frame))),
                                 capture_item_id="roster:self")

    def deliver(self, text: str, *, reservation_id: str, expected_content_digest: str,
                before_click: Callable[[], None], group_verification: GroupVerification | None = None,
                expected_context_frame: Frame | None = None,
                after_click: Callable[[datetime], None] | None = None) -> DeliveryResult:
        with self._lock:
            self.last_preclick_proof = None
            callback_attempted = False
            acknowledged_at = None
            refs: list[str] = []
            entry_capture = self._last_verified_capture

            def failure_evidence() -> list[str]:
                latest = self._last_verified_capture
                attempt_frame = latest if latest is not entry_capture else None
                return list(dict.fromkeys([*refs, *(self.frame_evidence_refs(attempt_frame) if attempt_frame is not None else [])]))

            try:
                if (not text.strip() or len(text) > 4000 or "\n" in text or "\r" in text
                        or any(ord(char) < 32 for char in text) or not reservation_id.strip()
                        or not re.fullmatch(r"[0-9a-f]{64}", expected_content_digest)):
                    raise DesktopError("invalid_delivery_request")
                if expected_context_frame is not None and expected_context_frame.content_digest != expected_content_digest:
                    raise DesktopError("trigger_frame_digest_mismatch")

                def context_matches(frame: Frame, screen: GroundedScreen | None = None) -> bool:
                    if expected_context_frame is None:
                        return frame.content_digest == expected_content_digest
                    return self._contexts_match(expected_context_frame, frame, actual_screen=screen)

                if reservation_id in self._attempted_reservations:
                    return DeliveryResult("unknown", [], "reservation_already_attempted")
                self._check_stop()
                group = group_verification
                if group is not None:
                    # Reuse only this backend's exact incoming verification.
                    # The post-staging numeric group check still runs below.
                    if (group is not self._group or group.group_id != self.config.expected_group_id
                            or not self._frame_is_recent(group.chat_frame, 60)):
                        raise DesktopError("incoming_group_verification_expired")
                else:
                    group = self.verify_group()
                refs.extend(group.evidence_refs)
                if not context_matches(group.chat_frame, group.chat_screen):
                    raise DesktopError("trigger_context_changed")
                owner = self.verify_self()
                owner_identity_epoch = self._identity_epoch
                refs.extend(owner.profile_evidence_refs)
                frame = self._current_ax_frame_or_capture()
                screen = self.inspect(frame)
                self._require_chat(frame, screen)
                if not context_matches(frame, screen):
                    raise DesktopError("trigger_context_changed")
                if screen.input_box is None or not self._center_in(screen.input_box, self.config.input_roi):
                    raise DesktopError("input_anchor_unavailable")
                if screen.visible_draft not in ("", text):
                    raise DesktopError("existing_or_unreadable_draft")
                if screen.visible_draft != text:
                    self._click(frame, screen, screen.input_box)
                    self._act({"kind": "type_text", "text": text})
                    # A pre-existing draft never grants ownership. Record only
                    # an acknowledged insertion made by this backend process.
                    self._owned_draft = (text, self.config.expected_group_id, self.config.window_id)
                    time.sleep(self.config.settle_seconds)
                # Revalidate group continuity (or its numeric card on a miss),
                # then read the draft and button again.
                staged_group = self.verify_group()
                if self.config.identity_cache_enabled and self._identity_epoch != owner_identity_epoch:
                    raise PreClickIdentityInvalidated()
                refs.extend(staged_group.evidence_refs)
                frame, screen = staged_group.chat_frame, staged_group.chat_screen
                if not context_matches(frame, screen) or screen.visible_draft != text:
                    raise DesktopError("staged_text_or_context_mismatch")
                if screen.send_button is None or not self._center_in(screen.send_button, self.config.send_roi):
                    raise DesktopError("send_anchor_unavailable")
                if screen.input_box is None:
                    raise DesktopError("input_anchor_unavailable")
                baseline = Counter(message.text for message in screen.messages if message.direction == "outgoing" and message.kind == "text")
                fresh = self._fresh(frame, screen, anchor=screen.send_button, force_capture=True)
                draft_ax_verified = False
                if self.config.identity_cache_enabled and fresh.capture_id in self._a11y_frames:
                    fresh_reading = self.inspect(fresh)
                    if (not self._identity_frame(fresh, fresh_reading) or fresh_reading.visible_draft != text
                            or fresh_reading.input_box != screen.input_box):
                        raise DesktopError("fresh_draft_not_verified")
                    draft_ax_verified = frame.capture_id in self._a11y_frames
                # The flashing caret changes pixels without changing text.
                # Only complete, paired AX from this final capture can replace
                # the exact pixel test; vision-only evidence cannot do so.
                if (not draft_ax_verified and
                        _pixels(frame.png, (screen.input_box.bounds,)) != _pixels(fresh.png, (screen.input_box.bounds,))):
                    raise DesktopError("draft_pixels_changed_before_send")
                if draft_ax_verified:
                    self._count("final_draft_ax_verified")
                refs.extend(self.frame_evidence_refs(fresh))
                self._agent_health()
                self._check_stop()
                if self.config.identity_cache_enabled and self._identity_epoch != owner_identity_epoch:
                    raise PreClickIdentityInvalidated()
                self.last_preclick_proof = {
                    "reservation_id": reservation_id,
                    "group_id": self.config.expected_group_id, "self_member_id": owner.member_id,
                    "reply_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "frame_evidence_ref": fresh.evidence_ref, "context_signature": fresh.content_digest,
                    "verified_at": datetime.now(timezone.utc).isoformat(), "before_outgoing_count": baseline[text],
                    "window_id": fresh.window_id, "body_instance_id": self.config.body_instance_id,
                    "environment_revision": self.config.environment_revision,
                    "title_pixels_sha256": _pixels(fresh.png, (self.config.title_roi,)),
                    "evidence_refs": list(dict.fromkeys(refs)),
                }
                callback_attempted = True
                self._attempted_reservations.add(reservation_id)
                try:
                    before_click()  # Durable reservation transition must finish first.
                except PreClickIdentityInvalidated:
                    # This typed rejection is allowed only before the callback
                    # commits. No GUI click or remote request has happened.
                    self._attempted_reservations.discard(reservation_id)
                    callback_attempted = False
                    raise
                self._check_stop()
                acknowledged_at = self._act({"kind": "click", "x": screen.send_button.center[0], "y": screen.send_button.center[1],
                                            "button": "left", "count": 1}, reservation_id=reservation_id)
                self._click_acknowledgements[reservation_id] = acknowledged_at
                if after_click is not None:
                    after_click(acknowledged_at)
                time.sleep(self.config.settle_seconds)
                after = self.capture()
                reading = self.inspect(after)
                self._require_chat(after, reading)
                if _title(reading.group_title.display_name) != staged_group.display_name or reading.visible_draft != "":
                    raise DesktopError("post_send_state_not_verified")
                outgoing = Counter(message.text for message in reading.messages if message.direction == "outgoing"
                                   and message.kind == "text" and message.bubble is not None and not message.uncertainties)
                if outgoing[text] <= baseline[text]:
                    raise DesktopError("new_outgoing_bubble_not_verified")
                refs.extend(self.frame_evidence_refs(after))
                self._owned_draft = None
                return DeliveryResult("sent", list(dict.fromkeys(refs)), "fresh_outgoing_bubble_verified", datetime.now(timezone.utc),
                                      phase="postcheck", click_acknowledged_at=acknowledged_at)
            except InterruptedError:
                refs = failure_evidence()
                return DeliveryResult("unknown" if callback_attempted else "not_sent", refs, "pause_requested", phase="click_started" if callback_attempted else "precheck",
                                      click_acknowledged_at=acknowledged_at)
            except DesktopError as exc:
                refs = failure_evidence()
                return DeliveryResult("unknown" if callback_attempted else "not_sent", refs, str(exc),
                                      phase="click_started" if callback_attempted else "precheck",
                                      retryable=not callback_attempted and str(exc) in {
                                          "verified_frame_unavailable", "grounding_expired", "input_anchor_unavailable",
                                          "send_anchor_unavailable", "target_changed_after_grounding",
                                          "group_title_changed_after_grounding", "incoming_group_verification_expired",
                                          "observation_gate_busy",
                                          "identity_cache_invalidated_before_click",
                                      }, click_acknowledged_at=acknowledged_at)
            except Exception:
                refs = failure_evidence()
                return DeliveryResult("unknown" if callback_attempted else "not_sent", refs, "desktop_operation_unavailable",
                                      phase="click_started" if callback_attempted else "precheck", click_acknowledged_at=acknowledged_at)

    def discard_owned_draft(self, text: str) -> bool:
        """Clear only this process's acknowledged insertion, never another draft."""
        with self._lock:
            ownership = (text, self.config.expected_group_id, self.config.window_id)
            if not text or self._owned_draft != ownership:
                return False
            try:
                self._check_stop()
                group = self.verify_group()
                frame, reading = group.chat_frame, group.chat_screen
                self._require_chat(frame, reading)
                if (group.group_id != self.config.expected_group_id or frame.window_id != self.config.window_id
                        or not self._frame_is_recent(frame)):
                    return False
                if reading.visible_draft == "":
                    self._owned_draft = None
                    return True
                if (reading.visible_draft != text or reading.input_box is None
                        or not self._center_in(reading.input_box, self.config.input_roi)):
                    return False
                self._click(frame, reading, reading.input_box)
                self._act({"kind": "press_keys", "keys": ["ctrl", "a"]})
                self._act({"kind": "press_keys", "keys": ["backspace"]})
                time.sleep(self.config.settle_seconds)
                after = self.capture()
                result = self.inspect(after)
                self._require_chat(after, result)
                if result.visible_draft != "" or _title(result.group_title.display_name) != group.display_name:
                    return False
                self._owned_draft = None
                return True
            except Exception:
                # A pause or uncertain cleanup keeps ownership for a later
                # explicitly bounded read. No cleanup or send action is retried.
                return False


    def reconcile_delivery(self, text: str, *, reservation_id: str, proof: dict) -> DeliveryResult:
        """Read-only confirmation for a click attempted by this process only."""
        with self._lock:
            refs = list(proof.get("evidence_refs", []))
            try:
                if (reservation_id not in self._attempted_reservations
                        or proof.get("reservation_id") != reservation_id
                        or proof.get("group_id") != self.config.expected_group_id
                        or proof.get("self_member_id") != self.config.self_member_id
                        or proof.get("body_instance_id") != self.config.body_instance_id
                        or proof.get("environment_revision") != self.config.environment_revision
                        or proof.get("window_id") != self.config.window_id
                        or proof.get("reply_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()
                        or proof.get("before_outgoing_count") is None):
                    raise DesktopError("reconciliation_continuity_unavailable")
                self._check_stop()
                frame = self.capture()
                capture_completed_at = datetime.now(timezone.utc)
                # Keep the read-only result even when the draft/bubble check
                # fails; failure evidence does not authorize another click.
                refs.extend(self.frame_evidence_refs(frame))
                reading = self.inspect(frame)
                self._require_chat(frame, reading)
                if (_pixels(frame.png, (self.config.title_roi,)) != proof.get("title_pixels_sha256")
                        or reading.visible_draft != ""):
                    raise DesktopError("post_send_state_not_verified")
                count = sum(m.direction == "outgoing" and m.kind == "text" and m.text == text
                            and m.bubble is not None and m.avatar is not None and not m.uncertainties
                            for m in reading.messages)
                if count <= proof["before_outgoing_count"]:
                    raise DesktopError("new_outgoing_bubble_not_verified")
                self._owned_draft = None
                acknowledged_at = self._click_acknowledgements.get(reservation_id)
                observed_at = observed_ref = None
                if acknowledged_at is None:
                    observed_at, observed_ref = self._click_observations.setdefault(
                        reservation_id, (capture_completed_at, frame.evidence_ref))
                    refs.append(observed_ref)
                return DeliveryResult("sent", list(dict.fromkeys(refs)), "read_only_outgoing_bubble_verified",
                                      datetime.now(timezone.utc), phase="postcheck", click_acknowledged_at=acknowledged_at,
                                      click_observed_at=observed_at, click_observed_evidence_ref=observed_ref)
            except InterruptedError:
                return DeliveryResult("unknown", refs, "pause_requested", phase="postcheck")
            except DesktopError as exc:
                return DeliveryResult("unknown", refs, str(exc), phase="postcheck")
            except Exception:
                return DeliveryResult("unknown", refs, "reconciliation_unavailable", phase="postcheck")
