"""Synthetic pixels and mocked transports only; never touches a desktop."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import sys
import time
from types import SimpleNamespace

import httpx
from PIL import Image, ImageDraw
import pytest

from harness.qq.desktop import AttributedMessage, DesktopBackend, DesktopConfig, DesktopError, Frame, GroupVerification
from harness.qq import desktop as desktop_module
from harness.qq.grounding import GroundedMessage, GroundedScreen, GroundingError, Rect


NOW = datetime.now(timezone.utc)
TITLE = Rect(x=20, y=80, width=180, height=25)
INPUT = Rect(x=10, y=550, width=1070, height=100)
SEND = Rect(x=1020, y=678, width=65, height=28)
TEXT = "合成的测试回复"


def png(*, change=None):
    image = Image.new("RGB", (1280, 720), "white")
    if change:
        ImageDraw.Draw(image).rectangle(change, fill="black")
    target = io.BytesIO()
    image.save(target, format="PNG")
    return target.getvalue()


def frame(backend, name, *, image=None, moment=None):
    moment = desktop_module.datetime.now(timezone.utc) if moment is None else moment
    image = png() if image is None else image
    config = backend.config
    item = Frame(name, moment, "evidence://sha256/" + hashlib.sha256(image).hexdigest(), image,
                 1280, 720, config.window_id, config.window_rect, config.window_id)
    return replace(item, content_digest=backend.content_digest(item))


def message(*, text=TEXT, direction="outgoing", name="虚构成员"):
    return GroundedMessage(direction=direction, kind="text", display_name=name, text=text,
                           avatar=Rect(x=1048 if direction == "outgoing" else 20, y=400, width=40, height=40),
                           bubble=Rect(x=850 if direction == "outgoing" else 80, y=400, width=170, height=40),
                           uncertainties=[])


def screen(item, *, draft="", messages=(), profile=None, scene="group_chat"):
    return GroundedScreen(frame_id=item.capture_id, evidence_ref=item.evidence_ref, width=1280, height=720,
                          scene=scene, blocked=False, group_title={"display_name": "虚构群(3)", "rect": TITLE},
                          input_box=INPUT, send_button=SEND, visible_draft=draft, messages=list(messages),
                          opened_profile=profile, uncertainties=[])


@pytest.fixture
def backend(tmp_path, monkeypatch):
    # Collection can precede this test by minutes. Share one test-local clock
    # with synthetic frames without weakening the production freshness checks.
    origin = datetime.now(timezone.utc)
    clock_monotonic = time.monotonic
    started_mono = clock_monotonic()
    monkeypatch.setattr(sys.modules[__name__], "NOW", origin)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            current = origin + timedelta(seconds=clock_monotonic() - started_mono)
            return current.astimezone(tz) if tz is not None else current.replace(tzinfo=None)

    monkeypatch.setattr(desktop_module, "datetime", Clock)
    token = tmp_path / "synthetic.token"
    token.write_text("synthetic-private-token-" + "x" * 32)
    calls = []

    def transport(request):
        if request.url.path == "/agent/v1/health":
            return httpx.Response(200, json={"body_instance_id": "misty-qq-test-01", "environment_revision": 2,
                                             "status": "healthy", "takeover_active": False})
        assert request.url.path == "/agent/v1/primitives:execute"
        payload = json.loads(request.content)
        calls.append(payload["primitive"])
        return httpx.Response(200, json={"success": True, "action_id": payload["action_id"], "step_id": payload["step_id"]})

    client = httpx.Client(base_url="http://127.0.0.1:8766", transport=httpx.MockTransport(transport))
    config = DesktopConfig("http://127.0.0.1:8788", token, "http://127.0.0.1:8766", token,
                           "10001", "20001", "synthetic-vision", settle_seconds=0)
    value = DesktopBackend(config, agent_client=client, llm=object())
    try:
        yield value, calls
    finally:
        value.close()
        client.close()


def test_digest_ignores_clock_input_and_sidebar_but_tracks_chat_and_title(backend):
    desktop, _ = backend
    original = frame(desktop, "base").content_digest
    for region in [(10, 0, 30, 20), (40, 560, 90, 600), (1120, 180, 1170, 210)]:
        assert frame(desktop, "ignored", image=png(change=region)).content_digest == original
    for region in [(60, 170, 90, 210), (40, 80, 90, 90)]:
        assert frame(desktop, "changed", image=png(change=region)).content_digest != original


@pytest.mark.parametrize("base_url,model,expected", [
    ("https://api.deepseek.com", "deepseek-v4-flash-vision-exp", {"reasoning_effort": "low", "response_format": {"type": "json_object"}}),
    ("https://api.deepseek.com/v1", "deepseek-v4-flash-vision-exp", {"reasoning_effort": "low", "response_format": {"type": "json_object"}}),
    ("https://api.deepseek.com.example.invalid", "deepseek-v4-flash-vision-exp", {}),
    ("http://127.0.0.1:11434/v1", "deepseek-v4-flash-vision-exp", {}),
    ("https://api.deepseek.com", "another-model", {}),
])
def test_low_reasoning_is_scoped_to_official_deepseek_visual_client(backend, monkeypatch, base_url, model, expected):
    desktop, _ = backend
    settings = SimpleNamespace(llm_base_url=base_url, llm_model=model)
    constructed = []
    monkeypatch.setattr("harness.qq.desktop.load_vision_settings", lambda *args, **kwargs: settings)

    def construct(received, *, extra_payload):
        assert received is settings
        constructed.append(extra_payload)
        return object()

    monkeypatch.setattr("harness.qq.desktop.OpenAICompatClient", construct)
    DesktopBackend(desktop.config, agent_client=desktop.agent)
    assert constructed == [expected]
    assert settings.llm_base_url == base_url and settings.llm_model == model


def test_expired_or_changed_pixels_never_click(backend, monkeypatch):
    desktop, calls = backend
    source = frame(desktop, "old", moment=NOW - timedelta(minutes=4))
    with pytest.raises(DesktopError, match="expired"):
        desktop._click(source, screen(source), TITLE)
    source = frame(desktop, "old")
    monkeypatch.setattr(desktop, "capture", lambda: frame(desktop, "fresh", image=png(change=(40, 180, 90, 210))))
    with pytest.raises(DesktopError, match="changed"):
        desktop._click(source, screen(source), TITLE)
    assert not calls


def delivery_setup(desktop, monkeypatch, *, post_missing=False, callback=None, changed=False):
    empty = frame(desktop, "empty")
    fresh_empty = frame(desktop, "fresh_empty")
    staged = frame(desktop, "staged", image=png(change=(40, 560, 90, 580)))
    fresh_staged = frame(desktop, "fresh_staged", image=staged.png)
    after = frame(desktop, "after", image=png(change=(850, 400, 990, 420)))
    readings = {empty.capture_id: screen(empty), staged.capture_id: screen(staged, draft=TEXT),
                after.capture_id: screen(after, messages=() if post_missing else [message()])}
    groups = iter([GroupVerification("10001", "虚构群", [empty.evidence_ref], empty, readings[empty.capture_id]),
                   GroupVerification("10001", "虚构群", [staged.evidence_ref], staged, readings[staged.capture_id])])
    captures = iter([empty, fresh_empty, fresh_staged, after])
    monkeypatch.setattr(desktop, "verify_group", lambda: next(groups))
    monkeypatch.setattr(desktop, "verify_self", lambda: AttributedMessage(
        "20001", "虚构本人", "own", desktop_module.datetime.now(timezone.utc), ["evidence-own"], ["evidence-own"]))
    monkeypatch.setattr(desktop, "capture", lambda: next(captures))
    monkeypatch.setattr(desktop, "inspect", lambda item: readings[item.capture_id])
    return {"text": TEXT, "reservation_id": "synthetic-reservation", "expected_content_digest": "0" * 64 if changed else empty.content_digest,
            "before_click": callback or (lambda: None)}


def test_send_requires_callback_then_one_click_then_new_outgoing_bubble(backend, monkeypatch):
    desktop, calls = backend
    callback_positions = []
    arguments = delivery_setup(desktop, monkeypatch, callback=lambda: callback_positions.append(len(calls)))
    result = desktop.deliver(**arguments)
    assert result.status == "sent" and result.delivered_at is not None
    assert callback_positions == [2]  # Input click and text insertion only.
    assert [call["kind"] for call in calls] == ["click", "type_text", "click"]
    assert calls[-1]["x"] == SEND.center[0] and calls[-1]["count"] == 1
    assert desktop.deliver(**arguments).status == "unknown"
    assert len(calls) == 3


def test_callback_failure_and_failed_postcheck_never_repeat_send(backend, monkeypatch):
    desktop, calls = backend

    def callback():
        raise RuntimeError("synthetic-private-failure")

    result = desktop.deliver(**delivery_setup(desktop, monkeypatch, callback=callback))
    assert result.status == "unknown" and len(calls) == 2
    assert "synthetic-private" not in result.reason_code


def test_missing_post_bubble_is_unknown_after_exactly_one_click(backend, monkeypatch):
    desktop, calls = backend
    result = desktop.deliver(**delivery_setup(desktop, monkeypatch, post_missing=True))
    assert result.status == "unknown" and result.reason_code == "new_outgoing_bubble_not_verified"
    assert len([call for call in calls if call.get("x") == SEND.center[0]]) == 1


def test_changed_trigger_stops_before_staging_or_reservation_transition(backend, monkeypatch):
    desktop, calls = backend
    entered = []
    result = desktop.deliver(**delivery_setup(desktop, monkeypatch, changed=True, callback=lambda: entered.append(True)))
    assert result.status == "not_sent" and result.reason_code == "trigger_context_changed"
    assert not calls and not entered


def test_pause_after_durable_mark_never_clicks_and_stays_unknown(backend, monkeypatch):
    desktop, calls = backend
    paused = False

    def mark():
        nonlocal paused
        paused = True

    monkeypatch.setattr(desktop, "should_stop", lambda: paused)
    result = desktop.deliver(**delivery_setup(desktop, monkeypatch, callback=mark))
    assert result.status == "unknown" and result.reason_code == "pause_requested"
    assert len(calls) == 2


def test_profile_attribution_uses_clicked_card_id_not_display_name(backend, monkeypatch):
    desktop, calls = backend
    source = frame(desktop, "source")
    fresh = frame(desktop, "fresh")
    card = frame(desktop, "card", image=png(change=(300, 140, 800, 500)))
    fresh_card = frame(desktop, "fresh_card", image=card.png)
    returned = frame(desktop, "returned")
    incoming = message(direction="incoming", name="群内昵称")
    original = screen(source, messages=[incoming])
    profile = {"kind": "member", "display_name": "资料卡名称", "member_id": "20002", "group_id": None,
               "card": Rect(x=300, y=140, width=500, height=360), "close": None, "uncertainties": []}
    readings = {source.capture_id: original, card.capture_id: screen(card, profile=profile, scene="member_profile"),
                fresh_card.capture_id: screen(fresh_card, profile=profile, scene="member_profile"),
                returned.capture_id: screen(returned)}
    captures = iter([fresh, fresh, card, fresh_card, returned])
    monkeypatch.setattr(desktop, "capture", lambda: next(captures))
    monkeypatch.setattr(desktop, "inspect", lambda item: readings[item.capture_id])
    desktop._group = GroupVerification("10001", "虚构群", [source.evidence_ref], source, original)
    result = desktop.attribute(incoming, source)
    assert result.member_id == "20002" and result.display_name == "资料卡名称"
    assert result.profile_evidence_refs == [card.evidence_ref]
    assert [call["kind"] for call in calls] == ["click", "press_keys", "move_pointer"]
    assert calls[0]["x"] == incoming.avatar.center[0]
    assert calls[-1] == {"kind": "move_pointer", "x": 640, "y": 45}


def test_wrong_group_profile_fails_closed(backend, monkeypatch):
    desktop, calls = backend
    source, fresh, card = (frame(desktop, name) for name in ["source", "fresh", "card"])
    profile = {"kind": "group", "display_name": "虚构群", "group_id": "99999", "member_id": None,
               "card": Rect(x=300, y=140, width=500, height=360), "close": None, "uncertainties": []}
    readings = {source.capture_id: screen(source), card.capture_id: screen(card, profile=profile, scene="group_profile")}
    captures = iter([source, fresh, card])
    monkeypatch.setattr(desktop, "capture", lambda: next(captures))
    monkeypatch.setattr(desktop, "inspect", lambda item: readings[item.capture_id])
    with pytest.raises(DesktopError, match="group_profile_not_verified"):
        desktop.verify_group()
    assert len(calls) == 1 and calls[0]["x"] == TITLE.center[0]


def test_self_account_mismatch_stops_and_unbound_avatar_is_rejected(backend, monkeypatch):
    desktop, calls = backend
    source = frame(desktop, "source")
    own = message()
    monkeypatch.setattr(desktop, "capture", lambda: source)
    monkeypatch.setattr(desktop, "inspect", lambda item: screen(item, messages=[own]))
    monkeypatch.setattr(desktop, "attribute", lambda *args: AttributedMessage("29999", "他人", "p", NOW, ["p"], ["p"]))
    with pytest.raises(DesktopError, match="logged_in_account_mismatch"):
        desktop.verify_self()
    assert not calls


def test_pixel_cache_rebinds_evidence_ignores_only_panel_and_evicts(backend, monkeypatch):
    desktop, _ = backend
    examined = []

    def inspect_pixels(llm, image, *, frame_id, evidence_ref):
        examined.append(frame_id)
        return screen(frame(desktop, frame_id, image=image))

    monkeypatch.setattr("harness.qq.desktop.inspect_qq_frame", inspect_pixels)
    base = frame(desktop, "base")
    clock = frame(desktop, "clock", image=png(change=(10, 0, 30, 20)))
    first = desktop.inspect(base)
    reused = desktop.inspect(clock)
    assert examined == ["base"]
    assert reused.frame_id == clock.capture_id and reused.evidence_ref == clock.evidence_ref
    assert first.frame_id == base.capture_id and first.evidence_ref == base.evidence_ref
    for number in range(9):
        item = frame(desktop, f"draft-{number}", image=png(change=(40 + number, 560, 90, 580)))
        desktop.inspect(item)
    assert len(examined) == 10 and len(desktop._pixel_screens) == 8
    desktop.inspect(frame(desktop, "evicted-original"))
    assert examined[-1] == "evicted-original"


@pytest.mark.parametrize("kind", ["group", "member"])
def test_resume_known_profile_closes_then_reverifies_actual_group(backend, monkeypatch, kind):
    desktop, calls = backend
    start = frame(desktop, "open-profile", image=png(change=(300, 140, 800, 500)))
    start_fresh = frame(desktop, "open-profile-fresh", image=start.png)
    chat = frame(desktop, "chat")
    chat_fresh = frame(desktop, "chat-fresh")
    group = frame(desktop, "actual-group", image=png(change=(310, 140, 800, 500)))
    group_fresh = frame(desktop, "actual-group-fresh", image=group.png)
    returned = frame(desktop, "returned")
    old_profile = {"kind": kind, "display_name": "不可采信的旧资料卡", "group_id": "99999" if kind == "group" else None,
                   "member_id": "29999" if kind == "member" else None,
                   "card": Rect(x=300, y=140, width=500, height=360), "close": None, "uncertainties": []}
    verified_profile = {**old_profile, "kind": "group", "display_name": "虚构群", "group_id": "10001", "member_id": None}
    readings = {start.capture_id: screen(start, profile=old_profile, scene=f"{kind}_profile"),
                start_fresh.capture_id: screen(start_fresh, profile=old_profile, scene=f"{kind}_profile"),
                chat.capture_id: screen(chat),
                group.capture_id: screen(group, profile=verified_profile, scene="group_profile"),
                group_fresh.capture_id: screen(group_fresh, profile=verified_profile, scene="group_profile"),
                returned.capture_id: screen(returned)}
    captures = iter([start, start_fresh, chat, chat_fresh, group, group_fresh, returned])
    monkeypatch.setattr(desktop, "capture", lambda: next(captures))
    monkeypatch.setattr(desktop, "inspect", lambda item: readings[item.capture_id])
    result = desktop.verify_group()
    assert result.group_id == "10001" and result.chat_frame.capture_id == returned.capture_id
    assert result.evidence_refs == [group.evidence_ref, returned.evidence_ref]
    assert [call["kind"] for call in calls] == ["press_keys", "move_pointer", "click", "press_keys", "move_pointer"]
    assert calls[2]["x"] == TITLE.center[0]
    assert calls[1] == calls[-1] == {"kind": "move_pointer", "x": 640, "y": 45}


@pytest.mark.parametrize("scene,blocked", [("login", False), ("other", False), ("uncertain", False), ("group_chat", True)])
def test_resume_never_dismisses_unknown_or_blocked_screen(backend, monkeypatch, scene, blocked):
    desktop, calls = backend
    source = frame(desktop, "unknown")
    reading = screen(source, scene=scene).model_copy(update={"blocked": blocked})
    monkeypatch.setattr(desktop, "capture", lambda: source)
    monkeypatch.setattr(desktop, "inspect", lambda item: reading)
    with pytest.raises(DesktopError, match="unambiguous_group_chat_required"):
        desktop.verify_group()
    assert not calls


def test_baseline_verifies_pixels_identity_geometry_and_cannot_authorize_old_click(backend, tmp_path):
    desktop, calls = backend
    source = frame(desktop, "baseline", moment=NOW - timedelta(hours=1))
    window = {"window_id": desktop.config.window_id, "title": "QQ", "app_id": desktop.config.app_id,
              "rect": dict(zip(("x", "y", "width", "height"), desktop.config.window_rect)), "visible": True, "focused": True}
    observation = {"schema_version": "body-contract-v1", "observation_id": source.capture_id,
                   "body_instance_id": desktop.config.body_instance_id, "environment_revision": 2,
                   "captured_at": source.captured_at.isoformat(), "active_window": window, "windows": [window],
                   "frame": {"evidence_ref": source.evidence_ref, "sha256": hashlib.sha256(source.png).hexdigest(),
                             "media_type": "image/png", "width": 1280, "height": 720},
                   "display": {"name": ":0", "width": 1280, "height": 720, "scale": 1.0},
                   "fs_events": [], "running_apps": [], "backend_health": {"x11": "healthy"}}
    path = tmp_path / "synthetic-baseline.json"
    path.write_text(json.dumps(observation), encoding="utf-8")
    path.with_suffix(".png").write_bytes(source.png)
    restored = desktop.load_baseline(path)
    assert restored == source
    with pytest.raises(DesktopError, match="grounding_expired"):
        desktop._click(restored, screen(restored), TITLE)
    assert not calls
    path.with_suffix(".png").write_bytes(png(change=(50, 180, 80, 200)))
    with pytest.raises(DesktopError, match="frame_digest_mismatch"):
        desktop.load_baseline(path)
    path.with_suffix(".png").write_bytes(source.png)
    observation["body_instance_id"] = "different-body"
    path.write_text(json.dumps(observation), encoding="utf-8")
    with pytest.raises(DesktopError, match="dedicated_window_not_active"):
        desktop.load_baseline(path)


def roster_setup(desktop, monkeypatch, *, marker="own_space_link", marker_text="查看我的QQ空间", member_id="20001", names=("Misty(合成备注)",), changed=False):
    source = frame(desktop, "roster")
    fresh = frame(desktop, "fresh-roster", image=png(change=(1140, 510, 1190, 520)) if changed else source.png)
    card = frame(desktop, "own-card", image=png(change=(760, 360, 1080, 710)))
    fresh_card = frame(desktop, "fresh-own-card", image=card.png)
    returned = frame(desktop, "returned-chat")
    roster = [{"display_name": name, "row": {"x": 1100, "y": 500 + index * 40, "width": 170, "height": 32},
               "avatar": {"x": 1104, "y": 502 + index * 40, "width": 28, "height": 28}, "uncertainties": []}
              for index, name in enumerate(names)]
    original = screen(source).model_copy(update={"visible_members": []})
    original = GroundedScreen.model_validate({**original.model_dump(), "visible_members": roster})
    profile = {"kind": "member", "display_name": "合成本人", "member_id": member_id, "group_id": None,
               "self_indicator": marker, "self_indicator_text": marker_text,
               "card": Rect(x=760, y=360, width=320, height=350), "close": None, "uncertainties": []}
    readings = {source.capture_id: original, card.capture_id: screen(card, profile=profile, scene="member_profile"),
                fresh_card.capture_id: screen(fresh_card, profile=profile, scene="member_profile"),
                returned.capture_id: screen(returned)}
    captures = iter([source, fresh, card, fresh_card, returned])
    monkeypatch.setattr(desktop, "capture", lambda: next(captures))
    monkeypatch.setattr(desktop, "inspect", lambda item: readings[item.capture_id])
    desktop._group = GroupVerification("10001", "虚构群", [source.evidence_ref], source, original)
    return card


@pytest.mark.parametrize("marker,marker_text", [("own_space_link", "查看我的QQ空间"), ("edit_own_profile", "编辑资料"),
                                               ("self_label", "这是我"), ("none", "查看我的QQ空间")])
def test_self_roster_requires_numeric_id_and_explicit_own_marker(backend, monkeypatch, marker, marker_text):
    desktop, calls = backend
    card = roster_setup(desktop, monkeypatch, marker=marker, marker_text=marker_text)
    own = desktop.verify_self()
    assert own.member_id == "20001" and own.profile_evidence_refs == [card.evidence_ref]
    assert own.capture_item_id == "roster:self"
    assert [item["kind"] for item in calls] == ["click", "press_keys", "move_pointer"]
    assert calls[0]["x"] == 1118


@pytest.mark.parametrize("marker,marker_text,member_id,reason", [
    ("none", None, "20001", "self_profile_marker_required"),
    ("own_space_link", "查看我的QQ空间", "29999", "self_roster_profile_not_verified"),
    ("own_space_link", "查看他的QQ空间", "20001", "self_profile_marker_required"),
    ("own_space_link", None, "20001", "self_profile_marker_required"),
])
def test_self_roster_name_and_number_alone_never_prove_login_identity(backend, monkeypatch, marker, marker_text, member_id, reason):
    desktop, calls = backend
    roster_setup(desktop, monkeypatch, marker=marker, marker_text=marker_text, member_id=member_id)
    with pytest.raises(DesktopError, match=reason):
        desktop.verify_self()
    assert len(calls) == 1 and calls[0]["kind"] == "click"


@pytest.mark.parametrize("names,changed,reason", [
    (("MistyOther",), False, "unique_visible_self_candidate_required"),
    (("Misty(甲)", "Misty(乙)"), False, "unique_visible_self_candidate_required"),
    (("Misty(合成备注)",), True, "target_changed_after_grounding"),
])
def test_self_roster_ambiguous_or_changed_row_never_clicked(backend, monkeypatch, names, changed, reason):
    desktop, calls = backend
    roster_setup(desktop, monkeypatch, names=names, changed=changed)
    with pytest.raises(DesktopError, match=reason):
        desktop.verify_self()
    assert not calls


def sticker(*, kind="sticker", shift=0):
    return GroundedMessage(kind=kind, direction="incoming", display_name="合成成员", text=None if kind == "sticker" else "新文字",
                           avatar=Rect(x=20, y=250, width=40, height=40),
                           bubble=Rect(x=80 + shift, y=250 + shift, width=100, height=100), uncertainties=[])


@pytest.mark.parametrize("change,kind,shift,expected", [
    ((90, 270, 100, 280), "sticker", 0, True),
    ((90, 270, 100, 280), "sticker", 4, False),
    ((90, 270, 100, 280), "sticker", 5, False),
    ((79, 270, 79, 270), "sticker", 0, False),
    ((90, 270, 100, 280), "text", 0, False),
    ((20, 250, 20, 250), "sticker", 0, False),
])
def test_animation_context_requires_same_messages_and_exact_pixels_outside_original_mask(backend, change, kind, shift, expected):
    desktop, _ = backend
    original = frame(desktop, "animation-before")
    actual = frame(desktop, "animation-after", image=png(change=change))
    assert original.content_digest != actual.content_digest
    result = desktop._contexts_match(original, actual, expected_screen=screen(original, messages=[sticker()]),
                                     actual_screen=screen(actual, messages=[sticker(kind=kind, shift=shift)]))
    assert result is expected


def test_animation_freshness_reinspects_changed_image_and_keeps_anchor_exact(backend, monkeypatch):
    desktop, calls = backend
    source = frame(desktop, "before")
    fresh = frame(desktop, "fresh", image=png(change=(90, 270, 100, 280)))
    reading = screen(source, messages=[sticker()])
    examined = []
    monkeypatch.setattr(desktop, "capture", lambda: fresh)

    def inspect(item):
        examined.append(item.capture_id)
        return screen(item, messages=[sticker()])

    monkeypatch.setattr(desktop, "inspect", inspect)
    desktop._click(source, reading, TITLE)
    assert examined == [fresh.capture_id] and len(calls) == 1
    with pytest.raises(DesktopError, match="target_changed_after_grounding"):
        desktop._click(source, reading, sticker().bubble)
    assert len(calls) == 1


def test_animation_comparison_uses_unchanged_pixels_instead_of_repeated_static_ocr(backend):
    desktop, _ = backend
    source = frame(desktop, "before")
    fresh = frame(desktop, "fresh", image=png(change=(90, 270, 100, 280)))
    before = screen(source, messages=[sticker(), message(text="原正文")])
    for update in ({"text": "替换正文"}, {"display_name": "不同昵称"}):
        changed = message(text="原正文").model_copy(update=update)
        assert desktop._contexts_match(source, fresh, expected_screen=before,
                                       actual_screen=screen(fresh, messages=[sticker(), changed]))


def test_delivery_binds_optional_context_frame_before_any_gui_action(backend):
    desktop, calls = backend
    original = frame(desktop, "original")
    result = desktop.deliver(TEXT, reservation_id="mismatched-frame", expected_content_digest="0" * 64,
                             expected_context_frame=original, before_click=lambda: pytest.fail("must not mark a send"))
    assert result.status == "not_sent" and result.reason_code == "trigger_frame_digest_mismatch"
    assert not calls


def test_delivery_can_keep_animated_trigger_context_through_staging_and_one_send(backend, monkeypatch):
    desktop, calls = backend
    trigger = frame(desktop, "trigger")
    current = frame(desktop, "current", image=png(change=(90, 270, 100, 280)))
    staged = frame(desktop, "staged", image=current.png)
    after = frame(desktop, "after", image=png(change=(850, 400, 990, 420)))
    readings = {trigger.capture_id: screen(trigger, messages=[sticker()]),
                current.capture_id: screen(current, messages=[sticker()]),
                staged.capture_id: screen(staged, draft=TEXT, messages=[sticker()]),
                after.capture_id: screen(after, messages=[sticker(), message()])}
    groups = iter([GroupVerification("10001", "虚构群", [], current, readings[current.capture_id]),
                   GroupVerification("10001", "虚构群", [], staged, readings[staged.capture_id])])
    captures = iter([current, current, staged, after])
    monkeypatch.setattr(desktop, "verify_group", lambda: next(groups))
    monkeypatch.setattr(desktop, "verify_self", lambda: AttributedMessage("20001", "合成本人", "own", NOW, [], []))
    monkeypatch.setattr(desktop, "capture", lambda: next(captures))
    monkeypatch.setattr(desktop, "inspect", lambda item: readings[item.capture_id])
    entered = []
    result = desktop.deliver(TEXT, reservation_id="animated-trigger", expected_content_digest=trigger.content_digest,
                             expected_context_frame=trigger, before_click=lambda: entered.append(len(calls)))
    assert result.status == "sent" and entered == [2]
    assert [item["kind"] for item in calls] == ["click", "type_text", "click"]


def localized_sticker_frame(desktop, name, *, animated=False, outside=False):
    image = Image.new("RGB", (1280, 720), (246, 246, 246))
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((64, 343, 213, 492), fill=(30, 40, 50))
    if animated:
        drawing.rectangle((64, 343, 110, 400), fill=(200, 80, 60))
    if outside:
        drawing.point((300, 235), fill=(0, 0, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return frame(desktop, name, image=output.getvalue())


def localized_sticker(*, shift=0, direction="incoming", avatar_inside=False, kind="sticker"):
    return GroundedMessage(kind=kind, direction=direction, text=None if kind == "sticker" else "覆盖文字",
                           display_name="合成贴图成员", bubble=Rect(x=64 + shift, y=343 + shift, width=150, height=150),
                           avatar=Rect(x=80 if avatar_inside else 10, y=353, width=70, height=40), uncertainties=[])


def test_pixel_component_refines_two_pixel_hint_errors_to_identical_actual_bounds(backend):
    desktop, _ = backend
    source = localized_sticker_frame(desktop, "before")
    fresh = localized_sticker_frame(desktop, "after", animated=True)
    old_reading = screen(source, messages=[localized_sticker(shift=2)])
    new_reading = screen(fresh, messages=[localized_sticker(shift=-3)])
    # Avatar's estimated right edge overlaps the sticker, but its actual center
    # is outside. Only the nearby pixel component, not a padding expansion, masks.
    assert desktop._sticker_regions(source, old_reading) == ((64, 343, 214, 493),)
    assert desktop._sticker_regions(fresh, new_reading) == ((64, 343, 214, 493),)
    assert desktop._contexts_match(source, fresh, expected_screen=old_reading, actual_screen=new_reading)


@pytest.mark.parametrize("fault", ["outside_pixel", "text_replacement", "avatar_center", "text_overlap", "uncertain_sticker"])
def test_refined_animation_mask_preserves_text_and_avatar_protection(backend, fault):
    desktop, _ = backend
    source = localized_sticker_frame(desktop, "before")
    fresh = localized_sticker_frame(desktop, "after", animated=True, outside=fault == "outside_pixel")
    item = localized_sticker(shift=-2, avatar_inside=fault == "avatar_center", kind="text" if fault == "text_replacement" else "sticker")
    if fault == "uncertain_sticker":
        item = item.model_copy(update={"uncertainties": ["synthetic uncertain shape"]})
    messages = [item]
    if fault == "text_overlap":
        messages.append(localized_sticker(kind="text"))
    assert not desktop._contexts_match(source, fresh, expected_screen=screen(source, messages=[localized_sticker(shift=2)]),
                                       actual_screen=screen(fresh, messages=messages))


def test_component_refinement_never_expands_arbitrary_uniform_background_or_far_hint(backend):
    desktop, _ = backend
    empty = frame(desktop, "empty")
    predicted = localized_sticker(shift=2).bubble
    assert desktop._refine_sticker_box(empty, predicted) == predicted.bounds
    source = localized_sticker_frame(desktop, "actual")
    far = localized_sticker(shift=12).bubble
    assert desktop._refine_sticker_box(source, far) == far.bounds


@pytest.mark.parametrize("polluted_rows,refines", [(1, True), (2, False)])
def test_dominant_background_allows_neighboring_foreground_but_rejects_mixed_ring(backend, polluted_rows, refines):
    desktop, _ = backend
    predicted = localized_sticker(shift=2).bubble
    source = localized_sticker_frame(desktop, "source")
    # For this hint the sampled ring has 1344 pixels. A nearby 170-pixel
    # nickname stroke contaminates 12.6%; two rows contaminate over 25%.
    with Image.open(io.BytesIO(source.png)) as image:
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((56, 335, 225, 334 + polluted_rows), fill=(10, 10, 10))
        output = io.BytesIO()
        image.save(output, format="PNG")
    contaminated = frame(desktop, "neighboring-name", image=output.getvalue())
    expected = (64, 343, 214, 493) if refines else predicted.bounds
    assert desktop._refine_sticker_box(contaminated, predicted) == expected


@pytest.mark.parametrize("fault", [None, "member_id", "scene", "blocked", "window", "expired"])
def test_escape_rechecks_profile_identity_instead_of_coarse_card_pixels(backend, monkeypatch, fault):
    desktop, calls = backend
    old = frame(desktop, "card", moment=NOW - timedelta(minutes=3) if fault == "expired" else NOW)
    fresh = frame(desktop, "fresh-card", image=png(change=(300, 350, 301, 450)))
    if fault == "window":
        fresh = replace(fresh, active_window_id="different-window")
    returned = frame(desktop, "chat")
    card = {"kind": "member", "display_name": "原先模型昵称", "member_id": "20002", "group_id": None,
            "card": Rect(x=300, y=140, width=500, height=360), "close": None, "uncertainties": []}
    updated_card = {**card, "display_name": "重复OCR差异", "member_id": "29999" if fault == "member_id" else "20002"}
    reading = screen(old, profile=card, scene="member_profile")
    fresh_reading = (screen(fresh, scene="login") if fault == "scene"
                     else screen(fresh, profile=updated_card, scene="member_profile"))
    if fault == "blocked":
        fresh_reading = fresh_reading.model_copy(update={"blocked": True})
    captures = iter([fresh, returned])
    monkeypatch.setattr(desktop, "capture", lambda: next(captures))
    monkeypatch.setattr(desktop, "inspect", lambda item: fresh_reading if item.capture_id == fresh.capture_id else screen(item))
    if fault:
        with pytest.raises(DesktopError):
            desktop._dismiss(old, reading)
        assert not calls
    else:
        result, _ = desktop._dismiss(old, reading)
        assert result.capture_id == returned.capture_id
        assert [item["kind"] for item in calls] == ["press_keys", "move_pointer"]
        assert calls[0]["keys"] == ["escape"]


def test_explicit_close_retains_pixel_target_checks(backend, monkeypatch):
    desktop, calls = backend
    old = frame(desktop, "card")
    fresh = frame(desktop, "fresh", image=png(change=(750, 150, 760, 160)))
    card = {"kind": "member", "display_name": "合成昵称", "member_id": "20002", "group_id": None,
            "card": Rect(x=300, y=140, width=500, height=360),
            "close": Rect(x=750, y=150, width=20, height=20), "uncertainties": []}
    monkeypatch.setattr(desktop, "capture", lambda: fresh)
    with pytest.raises(DesktopError, match="target_changed_after_grounding"):
        desktop._dismiss(old, screen(old, profile=card, scene="member_profile"))
    assert not calls


def question_message():
    return GroundedMessage(kind="text", direction="incoming", display_name="合成提问者", text="问题保持不变？",
                           avatar=Rect(x=20, y=200, width=40, height=40),
                           bubble=Rect(x=80, y=200, width=400, height=40), uncertainties=[])


def test_text_proof_tolerates_bad_sticker_geometry_and_cosmetic_background(backend):
    desktop, _ = backend
    original = localized_sticker_frame(desktop, "before")
    actual = localized_sticker_frame(desktop, "after", animated=True)
    with Image.open(io.BytesIO(actual.png)) as image:
        ImageDraw.Draw(image).point((600, 300), fill=(20, 30, 40))
        output = io.BytesIO()
        image.save(output, format="PNG")
    actual = frame(desktop, "after-background", image=output.getvalue())
    question = question_message()
    fresh_question = question.model_copy(update={"display_name": "重复OCR昵称不同",
                                                 "bubble": Rect(x=82, y=198, width=395, height=42)})
    before = screen(original, messages=[question, localized_sticker(shift=24)])
    after = screen(actual, messages=[fresh_question, localized_sticker(shift=-20)])
    assert desktop._sticker_regions(original, before) == ()
    assert desktop._contexts_match(original, actual, expected_screen=before, actual_screen=after)


@pytest.mark.parametrize("fault", ["new_text", "changed_text", "sticker_to_text", "unknown", "other", "uncertain",
                                    "missing_avatar", "missing_bubble", "nonsensical_box", "avatar_pixels", "text_pixels", "title_pixels"])
def test_text_fallback_rejects_new_or_unproven_text_and_changed_identity_pixels(backend, fault):
    desktop, _ = backend
    original = localized_sticker_frame(desktop, "before")
    actual = localized_sticker_frame(desktop, "after", animated=True)
    question = question_message()
    updates = {"changed_text": {"text": "问题已经改变"}, "unknown": {"direction": "unknown"}, "other": {"kind": "other"},
               "uncertain": {"uncertainties": ["synthetic unreadable text"]}, "missing_avatar": {"avatar": None},
               "missing_bubble": {"bubble": None}, "nonsensical_box": {"bubble": Rect(x=0, y=0, width=1, height=1)}}
    after_messages = [question.model_copy(update=updates.get(fault, {})), localized_sticker(shift=-20)]
    if fault == "new_text":
        after_messages.append(question.model_copy(update={"text": "新增问题"}))
    elif fault == "sticker_to_text":
        after_messages[-1] = localized_sticker(kind="text")
    pixel = {"avatar_pixels": (30, 215), "text_pixels": (100, 215), "title_pixels": (100, 90)}.get(fault)
    if pixel:
        with Image.open(io.BytesIO(actual.png)) as image:
            ImageDraw.Draw(image).point(pixel, fill=(10, 20, 30))
            output = io.BytesIO()
            image.save(output, format="PNG")
        actual = frame(desktop, "changed-evidence", image=output.getvalue())
    assert not desktop._contexts_match(original, actual,
                                       expected_screen=screen(original, messages=[question, localized_sticker(shift=24)]),
                                       actual_screen=screen(actual, messages=after_messages))


@pytest.mark.parametrize("historical", [False, True])
def test_inspect_retries_malformed_profile_same_frame_then_caches_only_valid_reading(backend, historical):
    desktop, actions = backend
    source = frame(desktop, "retry-card", moment=NOW - timedelta(hours=2) if historical else NOW)
    profile = {"kind": "member", "display_name": "合成成员", "member_id": "20002", "group_id": None,
               "card": Rect(x=300, y=140, width=500, height=360), "close": None, "uncertainties": []}
    valid = screen(source, profile=profile, scene="member_profile").model_dump(exclude={"frame_id", "evidence_ref", "width", "height"})
    invalid = json.loads(json.dumps(valid))
    invalid["opened_profile"]["close"] = {"x": 1248, "y": 35, "width": 20, "height": 20}
    calls = []

    class LLM:
        def complete_chat_result(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(text=json.dumps(invalid if len(calls) == 1 else valid))

    desktop.llm = LLM()
    result = desktop.inspect(source)
    assert result.opened_profile.close is None and len(calls) == 2
    assert calls[0] == calls[1]  # Exact same evidence/image and prompt.
    assert desktop.inspect(source) is result and len(calls) == 2
    assert len(desktop._pixel_screens) == len(desktop._screens) == 1 and not actions


@pytest.mark.parametrize("failure", ["exhausted", "pause", "expired"])
def test_inspect_retry_failure_never_acts_or_caches_invalid_result(backend, monkeypatch, failure):
    desktop, actions = backend
    source = frame(desktop, "retry-failure")
    count = 0
    current = NOW
    paused = False

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current

    class LLM:
        def complete_chat_result(self, messages, **kwargs):
            nonlocal count, current, paused
            count += 1
            if failure == "pause":
                paused = True
            elif failure == "expired":
                current = NOW + timedelta(seconds=121)
            return SimpleNamespace(text='{"scene":"member_profile",')

    desktop.llm = LLM()
    desktop.should_stop = lambda: paused
    monkeypatch.setattr("harness.qq.desktop.datetime", Clock)
    error = {"exhausted": GroundingError, "pause": InterruptedError, "expired": DesktopError}[failure]
    with pytest.raises(error):
        desktop.inspect(source)
    assert count == (3 if failure == "exhausted" else 1)
    assert not desktop._screens and not desktop._pixel_screens and not actions


@pytest.mark.parametrize("fault", [None, "title", "anchor", "window", "expired"])
def test_group_title_click_depends_on_title_target_not_animated_or_clipped_chat(backend, monkeypatch, fault):
    desktop, actions = backend
    source = frame(desktop, "chat", moment=NOW - timedelta(minutes=3) if fault == "expired" else NOW)
    fragment = question_message().model_copy(update={"avatar": None, "bubble": Rect(x=80, y=110, width=200, height=30)})
    reading = screen(source, messages=[fragment, localized_sticker(shift=25)])
    change = (64, 343, 214, 493)
    if fault == "title":
        change = (100, 90, 100, 90)
    elif fault == "anchor":
        anchor = Rect(x=30, y=65, width=100, height=65)
        reading = reading.model_copy(update={"group_title": reading.group_title.model_copy(update={"rect": anchor})})
        change = (40, 120, 40, 120)  # Outside title ROI, inside actual anchor.
    fresh = frame(desktop, "fresh", image=png(change=change))
    if fault == "window":
        fresh = replace(fresh, active_window_id="different-window")
    monkeypatch.setattr(desktop, "capture", lambda: fresh)
    if fault:
        with pytest.raises(DesktopError):
            desktop._click_group_title(source, reading)
        assert not actions
    else:
        desktop._click_group_title(source, reading)
        assert actions == [{"kind": "click", "x": TITLE.center[0], "y": TITLE.center[1], "button": "left", "count": 1}]


def test_inspect_projects_complete_messages_without_hiding_uncertain_complete_items(backend):
    desktop, actions = backend
    source = frame(desktop, "viewport")
    complete = question_message()
    uncertain = complete.model_copy(update={"direction": "unknown", "uncertainties": ["synthetic author uncertainty"]})
    clipped_bubble = complete.model_copy(update={"bubble": Rect(x=80, y=110, width=200, height=30)})
    clipped_avatar = complete.model_copy(update={"avatar": Rect(x=20, y=500, width=40, height=40)})
    no_avatar = complete.model_copy(update={"avatar": None})
    no_bubble = complete.model_copy(update={"bubble": None})
    original = screen(source, messages=[complete, clipped_bubble, clipped_avatar, no_avatar, no_bubble, uncertain])
    payload = original.model_dump(exclude={"frame_id", "evidence_ref", "width", "height"})

    class LLM:
        def complete_chat_result(self, *args, **kwargs):
            return SimpleNamespace(text=json.dumps(payload))

    desktop.llm = LLM()
    result = desktop.inspect(source)
    assert result.messages == [complete, uncertain]
    assert len(original.messages) == 6 and not actions
    assert desktop.inspect(source).messages == result.messages


def test_popup_projection_is_unchanged(backend):
    desktop, _ = backend
    source = frame(desktop, "profile")
    profile = {"kind": "member", "display_name": "合成成员", "member_id": "20002", "group_id": None,
               "card": Rect(x=300, y=140, width=500, height=360), "close": None, "uncertainties": []}
    reading = screen(source, profile=profile, scene="member_profile")
    assert desktop._complete_chat_messages(reading) is reading and reading.messages == []


def accessibility_frame(desktop, ident, *, image=None, text="hello", moment=None):
    moment = desktop_module.datetime.now(timezone.utc) if moment is None else moment
    from tests.test_qq_accessibility import make_tree

    item = frame(desktop, ident, image=image, moment=moment)
    tree = make_tree()
    tree["scope"]["window_id"] = item.window_id
    tree["scope"]["app_id"] = desktop.config.app_id
    for node in tree["nodes"]:
        node["window_id"] = item.window_id
        node["app_id"] = desktop.config.app_id
        if node["node_id"] == "t1":
            node["text"] = text
    tree["started_at"] = (moment + timedelta(milliseconds=10)).isoformat()
    tree["finished_at"] = (moment + timedelta(milliseconds=500)).isoformat()
    tree["frame_captured_at"] = moment.isoformat()
    return replace(item, a11y_tree=tree,
                   a11y_evidence_ref="evidence://sha256/" + hashlib.sha256(ident.encode()).hexdigest())


def test_bound_accessibility_projection_precedes_vision_and_does_not_use_pixel_cache(backend, monkeypatch):
    desktop, actions = backend
    desktop.config = replace(desktop.config, accessibility_enabled=True)
    monkeypatch.setattr("harness.qq.desktop.inspect_qq_frame", lambda *args, **kwargs: pytest.fail("unexpected vision"))
    first = accessibility_frame(desktop, "a11y-first", text="first")
    second = accessibility_frame(desktop, "a11y-second", text="second")
    assert first.png == second.png
    assert desktop.inspect(first).messages[0].text == "first"
    assert desktop.inspect(second).messages[0].text == "second"
    assert {first.capture_id, second.capture_id} <= desktop._a11y_frames
    assert not desktop._pixel_screens and not actions
    assert desktop.frame_evidence_refs(first) == [first.evidence_ref, first.a11y_evidence_ref]


def test_unbound_accessibility_falls_back_without_claiming_a11y_provenance(backend, monkeypatch):
    desktop, actions = backend
    desktop.config = replace(desktop.config, accessibility_enabled=True)
    item = accessibility_frame(desktop, "unbound")
    item.a11y_tree["scope"]["bound"] = False
    calls = []
    def read(_llm, _png, **kwargs):
        calls.append(kwargs)
        return screen(item, messages=[message()])
    monkeypatch.setattr("harness.qq.desktop.inspect_qq_frame", read)
    assert desktop.inspect(item).messages == [message()]
    assert len(calls) == 1 and item.capture_id not in desktop._a11y_frames
    assert desktop.frame_evidence_refs(item) == [item.evidence_ref] and not actions


@pytest.mark.parametrize("changed_text,same_pixels,expected", [(False, False, True), (True, False, False), (True, True, False)])
def test_a11y_context_proof_uses_current_text_even_when_screenshot_digest_matches(backend, monkeypatch, changed_text, same_pixels, expected):
    desktop, actions = backend
    desktop.config = replace(desktop.config, accessibility_enabled=True)
    monkeypatch.setattr("harness.qq.desktop.inspect_qq_frame", lambda *args, **kwargs: pytest.fail("unexpected vision"))
    before = accessibility_frame(desktop, "before", text="original")
    after = accessibility_frame(desktop, "after", text="changed" if changed_text else "original",
                                 image=before.png if same_pixels else png(change=(400, 350, 420, 370)))
    assert desktop._contexts_match(before, after) is expected
    assert not actions


def test_verify_group_reuses_supplied_frame_and_keeps_both_identity_evidence_sources(backend, monkeypatch):
    desktop, actions = backend
    source, fresh, card, fresh_card, returned = (frame(desktop, name) for name in
                                               ["given", "fresh", "group-card", "fresh-card", "returned"])
    card = replace(card, a11y_evidence_ref="evidence://sha256/" + "f" * 64)
    returned = replace(returned, a11y_evidence_ref="evidence://sha256/" + "e" * 64)
    profile = {"kind": "group", "display_name": "虚构群", "group_id": "10001", "member_id": None,
               "card": Rect(x=300, y=140, width=322, height=336), "close": None, "uncertainties": []}
    readings = {source.capture_id: screen(source), returned.capture_id: screen(returned),
                card.capture_id: screen(card, scene="group_profile", profile=profile),
                fresh_card.capture_id: screen(fresh_card, scene="group_profile", profile=profile)}
    captures = iter([fresh, card, fresh_card, returned])
    captured = []
    def capture():
        value = next(captures)
        captured.append(value.capture_id)
        return value
    monkeypatch.setattr(desktop, "capture", capture)
    monkeypatch.setattr(desktop, "inspect", lambda value: readings[value.capture_id])
    desktop._a11y_frames.update([card.capture_id, returned.capture_id])
    result = desktop.verify_group(frame=source)
    assert result is desktop._group and captured == ["fresh", "group-card", "fresh-card", "returned"]
    assert card.a11y_evidence_ref in result.evidence_refs and returned.a11y_evidence_ref in result.evidence_refs
    assert [action["kind"] for action in actions] == ["click", "press_keys", "move_pointer"]


def test_verify_group_rejects_expired_supplied_frame_before_capture_or_action(backend, monkeypatch):
    desktop, actions = backend
    monkeypatch.setattr(desktop, "capture", lambda: pytest.fail("no fresh capture expected"))
    with pytest.raises(DesktopError, match="expired"):
        desktop.verify_group(frame=frame(desktop, "old-given", moment=NOW - timedelta(minutes=3)))
    assert not actions


@pytest.mark.parametrize("mismatch", [None, "copy", "old"])
def test_deliver_only_reuses_its_exact_fresh_incoming_group_object(backend, monkeypatch, mismatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr("harness.qq.desktop.datetime", Clock)
    desktop, actions = backend
    arguments = delivery_setup(desktop, monkeypatch)
    incoming = desktop.verify_group()
    desktop._group = incoming
    if mismatch == "copy":
        incoming = replace(incoming)
    elif mismatch == "old":
        incoming = replace(incoming, chat_frame=replace(incoming.chat_frame, captured_at=NOW - timedelta(seconds=65)))
        desktop._group = incoming
    arguments["group_verification"] = incoming
    result = desktop.deliver(**arguments)
    if mismatch:
        assert result.status == "not_sent" and result.reason_code == "incoming_group_verification_expired"
        assert not actions
    else:
        assert result.status == "sent" and len(actions) == 3


def test_preclick_proof_exists_before_callback_binds_reservation_and_has_no_plaintext(backend, monkeypatch):
    desktop, actions = backend
    observed = []
    def callback():
        proof = dict(desktop.last_preclick_proof)
        observed.append(proof)
        assert proof["reservation_id"] == "synthetic-reservation"
        assert proof["reply_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
        assert proof["group_id"] == desktop.config.expected_group_id
        assert proof["self_member_id"] == desktop.config.self_member_id
        assert proof["before_outgoing_count"] == 0
        assert len(actions) == 2
        assert TEXT not in json.dumps(proof, ensure_ascii=False)
    result = desktop.deliver(**delivery_setup(desktop, monkeypatch, callback=callback))
    assert result.status == "sent" and len(observed) == 1


@pytest.mark.parametrize("fault", [None, "wrong_reservation", "wrong_hash", "wrong_account", "missing_bubble", "draft", "title"])
def test_reconciliation_is_read_only_and_never_repeats_click(backend, monkeypatch, fault):
    desktop, actions = backend
    arguments = delivery_setup(desktop, monkeypatch, post_missing=True)
    assert desktop.deliver(**arguments).status == "unknown"
    proof = dict(desktop.last_preclick_proof)
    if fault == "wrong_reservation":
        proof["reservation_id"] = "different-reservation"
    elif fault == "wrong_hash":
        proof["reply_sha256"] = "f" * 64
    elif fault == "wrong_account":
        proof["self_member_id"] = "99999"
    after = frame(desktop, "reconcile", image=png(change=(100, 90, 101, 91)) if fault == "title" else png(change=(10, 700, 11, 701)))
    reading = screen(after, messages=[] if fault == "missing_bubble" else [message()], draft=TEXT if fault == "draft" else "")
    monkeypatch.setattr(desktop, "capture", lambda: after)
    monkeypatch.setattr(desktop, "inspect", lambda _value: reading)
    result = desktop.reconcile_delivery(TEXT, reservation_id=arguments["reservation_id"], proof=proof)
    assert result.status == ("unknown" if fault else "sent") and result.phase == "postcheck"
    assert (after.evidence_ref in result.evidence_refs) is (fault not in {"wrong_reservation", "wrong_hash", "wrong_account"})
    assert len(actions) == 3
    assert desktop.deliver(**arguments).status == "unknown" and len(actions) == 3


@pytest.mark.parametrize("marker,wrong_number", [("查看我的QQ空间", False), ("查看他的QQ空间", False), ("查看我的QQ空间", True)])
def test_self_aliases_require_verified_number_and_explicit_own_profile_marker(backend, monkeypatch, marker, wrong_number):
    desktop, actions = backend
    source, card = frame(desktop, "self-chat"), frame(desktop, "self-card")
    own = message(name="已核验群昵称")
    profile_data = {"kind": "member", "display_name": "已核验资料名称", "member_id": "99999" if wrong_number else "20001",
                    "group_id": None, "self_indicator_text": marker, "card": Rect(x=300, y=140, width=322, height=336),
                    "close": None, "uncertainties": []}
    desktop._screens[card.capture_id] = screen(card, profile=profile_data, scene="member_profile")
    desktop._group = GroupVerification("10001", "虚构群", [source.evidence_ref], source, screen(source))
    monkeypatch.setattr(desktop, "capture", lambda: source)
    monkeypatch.setattr(desktop, "inspect", lambda _value: screen(source, messages=[own]))
    monkeypatch.setattr(desktop, "attribute", lambda _message, _frame: AttributedMessage(
        profile_data["member_id"], profile_data["display_name"], card.capture_id, card.captured_at,
        [card.evidence_ref], [card.evidence_ref]))
    if wrong_number:
        with pytest.raises(DesktopError, match="logged_in_account_mismatch"):
            desktop.verify_self()
    else:
        desktop.verify_self()
    if marker == "查看我的QQ空间" and not wrong_number:
        assert {alias.alias for alias in desktop.verified_self_aliases} == {"已核验群昵称", "已核验资料名称"}
        assert all(alias.group_id == "10001" and alias.self_member_id == "20001" and card.evidence_ref in alias.evidence_refs
                   for alias in desktop.verified_self_aliases)
    else:
        assert desktop.verified_self_aliases == []
    assert not actions


def test_verified_roster_self_alias_uses_clicked_row_and_card_only(backend, monkeypatch):
    desktop, actions = backend
    card = roster_setup(desktop, monkeypatch)
    original_inspect = desktop.inspect
    def cache_inspect(item):
        reading = original_inspect(item)
        desktop._screens[item.capture_id] = reading
        return reading
    monkeypatch.setattr(desktop, "inspect", cache_inspect)
    result = desktop.verify_self()
    assert result.capture_item_id == "roster:self"
    assert {alias.alias for alias in desktop.verified_self_aliases} == {"合成本人", "Misty(合成备注)"}
    assert all(card.evidence_ref in alias.evidence_refs for alias in desktop.verified_self_aliases)
    assert [action["kind"] for action in actions] == ["click", "press_keys", "move_pointer"]


def test_only_successful_backend_insertion_establishes_draft_ownership(backend, monkeypatch):
    desktop, actions = backend
    arguments = delivery_setup(desktop, monkeypatch)
    original = desktop.verify_group
    checks = 0
    def verify():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise DesktopError("group_profile_not_verified")
        return original()
    monkeypatch.setattr(desktop, "verify_group", verify)
    result = desktop.deliver(**arguments)
    assert result.status == "not_sent" and len(actions) == 2
    assert desktop._owned_draft == (TEXT, desktop.config.expected_group_id, desktop.config.window_id)
    # A draft that existed before this backend's insertion never grants the
    # cleanup API permission, even when it contains the same desired reply.
    desktop._owned_draft = None
    assert desktop.discard_owned_draft(TEXT) is False and len(actions) == 2


@pytest.mark.parametrize("fault", [None, "unowned", "external_text", "wrong_group", "wrong_window", "pause", "post_nonempty"])
def test_owned_draft_cleanup_verifies_group_exact_text_and_empty_result_without_send(backend, monkeypatch, fault):
    desktop, actions = backend
    desktop._owned_draft = None if fault == "unowned" else (TEXT, desktop.config.expected_group_id, desktop.config.window_id)
    source = frame(desktop, "owned-draft", image=png(change=(30, 565, 90, 580)))
    if fault == "wrong_window":
        source = replace(source, window_id="different")
    reading = screen(source, draft=TEXT + "外部文字" if fault == "external_text" else TEXT)
    group = GroupVerification("99999" if fault == "wrong_group" else "10001", "虚构群", [source.evidence_ref], source, reading)
    checks = []
    def verify():
        checks.append(True)
        return group
    monkeypatch.setattr(desktop, "verify_group", verify)
    fresh = frame(desktop, "owned-fresh", image=source.png)
    after = frame(desktop, "owned-cleared")
    captures = iter([fresh, after])
    monkeypatch.setattr(desktop, "capture", lambda: next(captures))
    monkeypatch.setattr(desktop, "inspect", lambda _item: screen(after, draft=TEXT if fault == "post_nonempty" else ""))
    desktop.should_stop = lambda: fault == "pause"
    assert desktop.discard_owned_draft(TEXT) is (fault is None)
    if fault in {None, "post_nonempty"}:
        assert actions == [
            {"kind": "click", "x": INPUT.center[0], "y": INPUT.center[1], "button": "left", "count": 1},
            {"kind": "press_keys", "keys": ["ctrl", "a"]},
            {"kind": "press_keys", "keys": ["backspace"]},
        ]
        assert desktop._owned_draft is None if fault is None else desktop._owned_draft is not None
    else:
        assert not actions
    assert len(checks) == (0 if fault in {"unowned", "pause"} else 1)


def test_successful_send_clears_backend_draft_ownership(backend, monkeypatch):
    desktop, _actions = backend
    assert desktop.deliver(**delivery_setup(desktop, monkeypatch)).status == "sent"
    assert desktop._owned_draft is None
