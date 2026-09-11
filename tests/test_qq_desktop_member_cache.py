"""Exercise real attribution and session cache with synthetic paired evidence."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io

import pytest
from PIL import Image, ImageDraw

from harness.qq.desktop import DesktopError, _pixels
from harness.qq.grounding import Rect, VisibleMember
from tests.test_qq_desktop import backend as backend, message
from tests.test_qq_desktop_identity_cache import actor as actor


@pytest.fixture
def member_actor(actor, monkeypatch):
    state = actor
    state.incoming = message(direction="incoming", text="第一条")
    state.member_id = "30001"
    state.extra = []
    state.is_member = False
    state.changed_avatar = False
    capture = state.capture

    def next_capture():
        if state.calls:
            last = state.calls[-1]
            if last["kind"] == "click" and (last["x"], last["y"]) == state.incoming.avatar.center:
                state.is_member = True
            elif last["kind"] in {"press_keys", "move_pointer"} or (last["kind"] == "click" and (last["x"], last["y"]) == (640, 45)):
                state.is_member = False
        item = capture()
        reading = state.desktop._screens[item.capture_id]
        if state.changed_avatar:
            with Image.open(io.BytesIO(item.png)) as pixels:
                ImageDraw.Draw(pixels).rectangle((26, 418, 29, 421), fill="blue")
                output = io.BytesIO()
                pixels.save(output, format="PNG")
            item = replace(item, png=output.getvalue(), evidence_ref="evidence://sha256/" + hashlib.sha256(output.getvalue()).hexdigest())
            item = replace(item, content_digest=state.desktop.content_digest(item))
            reading = reading.model_copy(update={"evidence_ref": item.evidence_ref})
            state.desktop._remember_capture(item, state.tick)
        if state.is_member:
            reading = type(reading).model_validate({**reading.model_dump(), "scene": "member_profile", "opened_profile": {
                "kind": "member", "display_name": "真实资料名", "member_id": state.member_id,
                "group_id": None, "card": Rect(x=600, y=250, width=300, height=300),
                "close": None, "uncertainties": []}})
        elif reading.scene == "group_chat":
            reading = reading.model_copy(update={"messages": [state.incoming, *state.extra]})
        state.desktop._screens[item.capture_id] = reading
        return item

    monkeypatch.setattr(state.desktop, "capture", next_capture)
    state.capture = next_capture
    state.current = next_capture()
    state.desktop.verify_group(frame=state.current)
    return state


def cold(state):
    source = state.capture()
    profile = state.desktop.attribute(state.incoming, source)
    return source, profile


def remember(state):
    source, profile = cold(state)
    assert state.desktop.remember_verified_identity(state.incoming, source, profile, "qq-profile:synthetic")
    return source, profile


def test_first_card_then_different_message_reuses_original_profile_without_click(member_actor):
    state = member_actor
    source, profile = remember(state)
    clicks = len(state.calls)
    state.incoming = state.incoming.model_copy(update={"text": "第二条",
        "avatar": Rect(x=20, y=300, width=40, height=40), "bubble": Rect(x=80, y=300, width=170, height=40)})
    current = state.capture()
    hot = state.desktop.attribute(state.incoming, current)
    assert len(state.calls) == clicks
    assert hot.profile_record_id == "qq-profile:synthetic"
    assert hot.member_id == profile.member_id and hot.display_name == profile.display_name
    assert hot.capture_id == profile.capture_id and hot.observed_at == profile.observed_at
    assert hot.text == "第二条" and hot.attribution_method == "qq_session_cached_identity_v1"
    assert set(profile.profile_evidence_refs + state.desktop.frame_evidence_refs(source)
               + state.desktop.frame_evidence_refs(current)) <= set(hot.evidence_refs)


@pytest.mark.parametrize("change", ["nickname", "portrait_edge", "invalidate", "new_scope", "untrusted"])
def test_changed_identity_requires_real_card(member_actor, monkeypatch, change):
    state = member_actor
    remember(state)
    if change == "nickname":
        state.incoming = state.incoming.model_copy(update={"display_name": "新昵称"})
    elif change == "invalidate":
        state.desktop.invalidate_identity_cache()
    elif change == "new_scope":
        state.desktop._identity_epoch = "new-login"
    elif change == "portrait_edge":
        state.changed_avatar = True
    current = state.capture()
    if change == "untrusted":
        state.desktop._a11y_frames.clear()
        capture = state.capture
        def untrusted_capture():
            item = capture()
            state.desktop._a11y_frames.clear()
            return item
        monkeypatch.setattr(state.desktop, "capture", untrusted_capture)
    clicks = len(state.calls)
    profile = state.desktop.attribute(state.incoming, current)
    assert profile.profile_record_id is None
    assert len(state.calls) > clicks


def test_persist_hook_requires_exact_completed_card_and_original_frame(member_actor):
    state = member_actor
    source, profile = cold(state)
    assert not state.desktop.remember_verified_identity(state.incoming, source, replace(profile), "qq-profile:x")
    assert not state.desktop.remember_verified_identity(state.incoming, source, profile, "qq-profile:x")
    source, profile = cold(state)
    assert not state.desktop.remember_verified_identity(state.incoming, state.capture(), profile, "qq-profile:x")
    assert state.desktop._member_identities is None


def test_binding_does_not_survive_epoch_change_during_storage(member_actor):
    state = member_actor
    source, profile = cold(state)
    state.desktop.invalidate_identity_cache()
    assert not state.desktop.remember_verified_identity(state.incoming, source, profile, "qq-profile:x")


def test_source_frame_may_age_while_card_is_verified(member_actor):
    state = member_actor
    source, profile = cold(state)
    state.tick += 5
    assert not state.desktop._frame_is_recent(source, 1)
    assert state.desktop.remember_verified_identity(state.incoming, source, profile, "qq-profile:x")


def test_changed_message_pixels_cannot_use_cache(member_actor, monkeypatch):
    state = member_actor
    remember(state)
    source = state.capture()
    from tests.test_qq_desktop import png
    altered = replace(source, capture_id="altered", png=png(change=(25, 405, 45, 425)),
                      captured_at=datetime.now(timezone.utc))
    reading = state.desktop._screens[source.capture_id].model_copy(update={"frame_id": "altered"})
    state.desktop._screens["altered"] = reading
    state.desktop._a11y_frames.add("altered")
    monkeypatch.setattr(state.desktop, "_current_ax_frame_or_capture", lambda: altered)
    assert _pixels(source.png, (state.incoming.avatar.bounds,)) != _pixels(altered.png, (state.incoming.avatar.bounds,))
    clicks = len(state.calls)
    with pytest.raises(DesktopError, match="message_changed_before_attribution"):
        state.desktop.attribute(state.incoming, source)
    assert len(state.calls) == clicks


def test_visible_same_name_with_different_portrait_requires_card(member_actor):
    state = member_actor
    remember(state)
    state.extra = [state.incoming.model_copy(update={"text": "同名另一人",
        "avatar": Rect(x=20, y=300, width=40, height=40), "bubble": Rect(x=80, y=300, width=170, height=40)})]
    state.changed_avatar = True
    current = state.capture()
    clicks = len(state.calls)
    profile = state.desktop.attribute(state.incoming, current)
    assert profile.profile_record_id is None and len(state.calls) > clicks
    assert not state.desktop.remember_verified_identity(state.incoming, current, profile, "qq-profile:collision")


def test_two_distinct_roster_rows_even_with_same_name_disable_visual_hint(member_actor):
    state = member_actor
    remember(state)
    current = state.capture()
    reading = state.desktop.inspect(current)
    assert state.desktop._member_visual_hints(state.incoming, current, reading) is not None
    reading = reading.model_copy(update={"visible_members": [
        VisibleMember(display_name=state.incoming.display_name, row=Rect(x=1100, y=y, width=170, height=32),
                      avatar=Rect(x=1104, y=y+2, width=28, height=28), uncertainties=[]) for y in (500, 540)]})
    assert state.desktop._member_visual_hints(state.incoming, current, reading) is None
