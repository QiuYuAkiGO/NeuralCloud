from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from harness.qq.desktop import DesktopError
from tests import test_qq_desktop as fixtures

backend = fixtures.backend


def remember_ax(desktop, name="current", *, age=.1, moment=None):
    desktop.config = replace(desktop.config, accessibility_enabled=True)
    item = fixtures.accessibility_frame(desktop, name, moment=moment or datetime.now(timezone.utc))
    reading = desktop.inspect(item)
    desktop._remember_capture(item, time.monotonic() - age)
    return item, reading


@pytest.mark.parametrize("fault", [None, "stale", "unvalidated", "generation", "window", "disabled", "not_latest"])
def test_only_latest_unmutated_recent_ax_frame_reused(backend, monkeypatch, fault):
    desktop, _calls = backend
    item, _ = remember_ax(desktop, age=1.01 if fault == "stale" else .1)
    fallback = fixtures.frame(desktop, "fallback")
    captured = []
    monkeypatch.setattr(desktop, "capture", lambda: captured.append(True) or fallback)
    if fault == "unvalidated":
        desktop._a11y_frames.clear()
    elif fault == "generation":
        desktop._gui_generation += 1
    elif fault == "window":
        desktop._latest_capture = replace(item, active_window_id="0x999")
    elif fault == "disabled":
        desktop.config = replace(desktop.config, accessibility_enabled=False)
    elif fault == "not_latest":
        desktop._remember_capture(fixtures.frame(desktop, "new-vlm-frame"), time.monotonic())
    assert desktop._current_ax_frame_or_capture() is (item if fault is None else fallback)
    assert captured == ([] if fault is None else [True])


@pytest.mark.parametrize("primitive", [
    {"kind": "move_pointer", "x": 640, "y": 45},
    {"kind": "press_keys", "keys": ["escape"]},
    {"kind": "type_text", "text": "synthetic"},
])
def test_every_action_invalidates_and_sends_current_window_binding(backend, monkeypatch, primitive):
    desktop, _ = backend
    remember_ax(desktop)
    payloads = []

    def transport(request):
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"body_instance_id": desktop.config.body_instance_id,
                "environment_revision": desktop.config.environment_revision,
                "status": "healthy", "takeover_active": False})
        value = json.loads(request.content)
        payloads.append(value)
        assert desktop._latest_capture is None
        return httpx.Response(200, json={"success": True, "action_id": value["action_id"], "step_id": value["step_id"]})

    client = httpx.Client(base_url="http://127.0.0.1:8766", transport=httpx.MockTransport(transport))
    monkeypatch.setattr(desktop, "agent", client)
    try:
        desktop._act(primitive)
    finally:
        client.close()
    assert payloads[0]["expected_window"] == {"window_id": desktop.config.window_id,
        "app_id": desktop.config.app_id, "rect": {"x": 0, "y": 27, "width": 1280, "height": 693}}
    assert desktop._latest_capture is None and desktop._latest_capture_started_mono is None


def test_failed_action_also_invalidates_reuse(backend, monkeypatch):
    desktop, _ = backend
    remember_ax(desktop)
    monkeypatch.setattr(desktop, "_agent_health", lambda: (_ for _ in ()).throw(DesktopError("unavailable")))
    with pytest.raises(DesktopError):
        desktop._act({"kind": "move_pointer", "x": 640, "y": 45})
    assert desktop._latest_capture is None


def test_monotonic_capture_age_ignores_remote_clock_and_bounds_group_ttl(backend):
    desktop, _ = backend
    item, _ = remember_ax(desktop, moment=datetime.now(timezone.utc) + timedelta(seconds=3))
    assert desktop._frame_is_recent(item) and desktop._frame_is_recent(item, 60)
    assert not desktop._frame_is_recent(replace(item))  # Imported/copied frame cannot borrow timing.
    desktop._capture_times[item.capture_id] = (item, time.monotonic() - 61)
    assert not desktop._frame_is_recent(item, 60)
    assert desktop._frame_is_recent(item)
    desktop._capture_times[item.capture_id] = (item, time.monotonic() - 121)
    assert not desktop._frame_is_recent(item)


def test_capture_timing_cache_is_bounded(backend):
    desktop, _ = backend
    original = fixtures.frame(desktop, "base")
    for index in range(129):
        desktop._remember_capture(replace(original, capture_id=str(index)), time.monotonic())
    assert len(desktop._capture_times) == 128 and "0" not in desktop._capture_times


def test_fresh_reuse_keeps_original_target_pixel_proof(backend, monkeypatch):
    desktop, _ = backend
    old, before = remember_ax(desktop, "old")
    new = replace(old, capture_id="current", png=fixtures.png(change=(20, 80, 30, 90)))
    current = before.model_copy(update={"frame_id": new.capture_id})
    desktop._screens[new.capture_id] = current
    desktop._a11y_frames.add(new.capture_id)
    desktop._remember_capture(new, time.monotonic())
    monkeypatch.setattr(desktop, "capture", lambda: pytest.fail("same phase must reuse"))
    with pytest.raises(DesktopError, match="target_changed|chat_changed"):
        desktop._fresh(old, before, anchor=fixtures.TITLE)


def test_title_duplicate_capture_reused_but_action_invalidates(backend, monkeypatch):
    desktop, actions = backend
    item, reading = remember_ax(desktop)
    monkeypatch.setattr(desktop, "capture", lambda: pytest.fail("duplicate title capture"))
    desktop._click_group_title(item, reading)
    assert len(actions) == 1 and desktop._latest_capture is None


def test_final_send_proof_and_postsend_always_capture_and_use_local_time(backend, monkeypatch):
    desktop, actions = backend
    arguments = fixtures.delivery_setup(desktop, monkeypatch)
    original_fresh = desktop._fresh
    original_reuse = desktop._current_ax_frame_or_capture
    force_flags = []

    def fresh(*args, **kwargs):
        force_flags.append(kwargs.get("force_capture", False))
        return original_fresh(*args, **kwargs)

    monkeypatch.setattr(desktop, "_fresh", fresh)

    def reuse():
        if actions and actions[-1].get("x") == fixtures.SEND.center[0]:
            pytest.fail("post-send evidence must not consult reuse")
        return original_reuse()

    monkeypatch.setattr(desktop, "_current_ax_frame_or_capture", reuse)
    before = fixtures.desktop_module.datetime.now(timezone.utc)
    result = desktop.deliver(**arguments)
    after = fixtures.desktop_module.datetime.now(timezone.utc)
    assert result.status == "sent" and force_flags == [False, True]
    assert before <= datetime.fromisoformat(desktop.last_preclick_proof["verified_at"]) <= after
    assert before <= result.delivered_at <= after
    assert len(actions) == 3


def test_force_capture_bypasses_even_recent_valid_ax_frame(backend, monkeypatch):
    desktop, _ = backend
    item, reading = remember_ax(desktop)
    captures = []
    monkeypatch.setattr(desktop, "capture", lambda: captures.append(True) or item)
    assert desktop._fresh(item, reading, anchor=reading.input_box, force_capture=True) is item
    assert captures == [True]


def test_capture_age_starts_before_http_read_not_when_evidence_returns(backend, monkeypatch):
    import asyncio

    desktop, _ = backend
    item, _ = remember_ax(desktop)
    clock = [100.0]
    monkeypatch.setattr("harness.qq.desktop.time.monotonic", lambda: clock[0])

    class Body:
        async def observe(self, **_kwargs):
            clock[0] += 1.2
            return SimpleNamespace(frame=SimpleNamespace(sha256="0" * 64), a11y_ref=item.a11y_evidence_ref)

        async def get_evidence(self, _ref, *, expected_media_type):
            return item.png if expected_media_type == "image/png" else json.dumps(item.a11y_tree).encode()

        async def close(self):
            pass

    desktop._body_client = Body()
    desktop._body_runner = asyncio.Runner()
    monkeypatch.setattr(desktop, "frame_from_observation", lambda *args, **kwargs: item)
    try:
        assert desktop.capture() is item
    finally:
        desktop.close()
    assert desktop._latest_capture_started_mono == 100.0
    fallback = fixtures.frame(desktop, "fresh-after-slow-request")
    monkeypatch.setattr(desktop, "capture", lambda: fallback)
    assert desktop._current_ax_frame_or_capture() is fallback


def test_profile_escape_reuses_only_same_validated_identity_phase(backend, monkeypatch):
    desktop, _ = backend
    item, _ = remember_ax(desktop)
    profile = {"kind": "member", "display_name": "synthetic", "member_id": "20002", "group_id": None,
               "card": fixtures.Rect(x=300, y=140, width=500, height=360), "close": None, "uncertainties": []}
    reading = fixtures.screen(item, profile=profile, scene="member_profile")
    desktop._screens[item.capture_id] = reading
    monkeypatch.setattr(desktop, "capture", lambda: pytest.fail("duplicate profile capture"))
    assert desktop._fresh_profile_for_escape(item, reading) is item
