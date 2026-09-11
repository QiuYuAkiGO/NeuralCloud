"""Synthetic AX/pixel actor exercises public group/self verification and sending.

Only capture/projection and the HTTP transport are injected. No GUI, model or
private state is accessed; numeric-card checks and send preconditions are real.
"""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
from types import SimpleNamespace as NS

from PIL import Image, ImageDraw
import pytest

from harness.qq import desktop as module
from harness.qq.desktop import DesktopBackend, DesktopError
from harness.qq.grounding import GroundedScreen, Rect, VisibleMember
from tests.test_qq_desktop import SEND, TEXT, TITLE, backend as backend, frame, message, screen


ROSTER = Rect(x=1100, y=500, width=170, height=32)
AVATAR = Rect(x=1104, y=502, width=28, height=28)


@pytest.fixture
def actor(backend, monkeypatch):
    desktop, calls = backend
    desktop.config = replace(desktop.config, identity_cache_enabled=True, accessibility_enabled=True)
    state = NS(
        desktop=desktop,
        calls=calls,
        tick=1000.0,
        captures=[],
        title="虚构群(3)",
        alias="Misty(合成备注)",
        title_changed=False,
        portrait_changed=False,
        duplicate_self=False,
        missing_bubble=False,
        mode="chat",
    )
    monkeypatch.setattr(module, "time", NS(monotonic=lambda: state.tick, sleep=lambda _: None))

    def capture():
        if calls:
            last = calls[-1]
            if last["kind"] == "type_text":
                state.mode = "staged"
            elif last["kind"] in {"press_keys", "move_pointer"}:
                state.mode = "chat"
            elif last["kind"] == "click":
                xy = (last["x"], last["y"])
                if xy in {(TITLE.x + TITLE.width - 12, TITLE.center[1]), TITLE.center}:
                    state.mode = "group"
                elif xy == AVATAR.center:
                    state.mode = "self"
                elif xy == (640, 45):
                    state.mode = "chat"
                elif xy == SEND.center:
                    state.mode = "after"
        state.tick += 0.01
        image = Image.new("RGB", (1280, 720), "white")
        draw = ImageDraw.Draw(image)
        draw.point((10, 5), fill=(len(state.captures) % 255, 0, 0))
        if state.title_changed:
            draw.rectangle((40, 80, 50, 90), fill="black")
        draw.rectangle((1110, 508, 1125, 523), fill="blue" if state.portrait_changed else "red")
        if state.mode in {"group", "self"}:
            draw.rectangle((760, 360, 1080, 710), fill="gray")
        elif state.mode == "staged":
            draw.rectangle((40, 560, 90, 580), fill="black")
        elif state.mode == "after":
            draw.rectangle((850, 400, 990, 420), fill="black")
        output = io.BytesIO()
        image.save(output, format="PNG")
        name = f"identity-capture-{len(state.captures)}"
        item = replace(
            frame(desktop, name, image=output.getvalue(), moment=datetime.now(timezone.utc)),
            a11y_tree={"nodes": []},
            a11y_evidence_ref="evidence://sha256/" + hashlib.sha256(name.encode()).hexdigest(),
        )
        profile = None
        if state.mode in {"group", "self"}:
            profile = {
                "kind": "group" if state.mode == "group" else "member",
                "display_name": state.title[:-3] if state.mode == "group" else "合成本人",
                "group_id": "10001" if state.mode == "group" else None,
                "member_id": "20001" if state.mode == "self" else None,
                "self_indicator": "self_label" if state.mode == "self" else "none",
                "self_indicator_text": "这是我" if state.mode == "self" else None,
                "card": Rect(x=760, y=360, width=320, height=350),
                "close": None,
                "uncertainties": [],
            }
        reading = screen(
            item,
            draft=TEXT if state.mode == "staged" else "",
            messages=[message()] if state.mode == "after" and not state.missing_bubble else [],
            profile=profile,
            scene={"group": "group_profile", "self": "member_profile"}.get(state.mode, "group_chat"),
        )
        roster = [VisibleMember(display_name=state.alias, row=ROSTER, avatar=AVATAR, uncertainties=[])]
        if state.duplicate_self:
            roster.append(
                VisibleMember(
                    display_name=state.alias,
                    row=Rect(x=1100, y=540, width=170, height=32),
                    avatar=Rect(x=1104, y=542, width=28, height=28),
                    uncertainties=[],
                )
            )
        reading = GroundedScreen.model_validate(
            {
                **reading.model_dump(),
                "visible_members": roster,
                "group_title": {"display_name": state.title, "rect": TITLE},
            }
        )
        desktop._screens[item.capture_id] = reading
        desktop._a11y_frames.add(item.capture_id)
        desktop._remember_capture(item, state.tick)
        state.captures.append(item)
        return item

    monkeypatch.setattr(desktop, "capture", capture)
    monkeypatch.setattr(desktop, "inspect", lambda item: desktop._screens[item.capture_id])
    state.capture = capture
    state.current = capture()
    return state


def card_clicks(state):
    points = {(TITLE.x + TITLE.width - 12, TITLE.center[1]), TITLE.center, AVATAR.center}
    return [call for call in state.calls if call["kind"] == "click" and (call["x"], call["y"]) in points]


def prime(state):
    group = state.desktop.verify_group(frame=state.current)
    own = state.desktop.verify_self()
    return group, own


def test_outgoing_card_cannot_authorize_unopened_roster_hint(actor):
    reading = actor.desktop._screens[actor.current.capture_id]
    actor.desktop._screens[actor.current.capture_id] = reading.model_copy(update={"messages": [message()]})
    group = actor.desktop.verify_group(frame=actor.current)
    # Keep an outgoing bubble present while exercising the exact roster path.
    original = actor.desktop.capture
    def with_outgoing():
        item = original()
        current = actor.desktop._screens[item.capture_id]
        if current.scene == "group_chat":
            actor.desktop._screens[item.capture_id] = current.model_copy(update={"messages": [message()]})
        return item
    actor.desktop.capture = with_outgoing
    actor.desktop.verify_self()
    assert any(call["kind"] == "click" and (call["x"], call["y"]) == AVATAR.center for call in actor.calls)
    assert not any(call["kind"] == "click" and (call["x"], call["y"]) == message().avatar.center for call in actor.calls)
    assert actor.desktop._self_identity is not None and group.group_id == "10001"


def test_precommit_identity_invalidation_is_retryable_and_never_clicks_send(actor):
    from harness.qq.desktop import PreClickIdentityInvalidated
    group, _ = prime(actor)
    def reject():
        actor.desktop.invalidate_identity_cache()
        raise PreClickIdentityInvalidated()
    result = actor.desktop.deliver(TEXT, reservation_id="identity-precommit-rejection",
        expected_content_digest=group.chat_frame.content_digest, expected_context_frame=group.chat_frame,
        group_verification=group, before_click=reject)
    assert result.status == "not_sent" and result.phase == "precheck" and result.retryable
    assert result.reason_code == "identity_cache_invalidated_before_click"
    assert "identity-precommit-rejection" not in actor.desktop._attempted_reservations
    assert not any(call["kind"] == "click" and (call["x"], call["y"]) == SEND.center for call in actor.calls)


def test_group_reverification_cannot_reuse_self_from_previous_epoch(actor, monkeypatch):
    group, _ = prime(actor)
    verify = actor.desktop.verify_group
    def changed_group(*args, **kwargs):
        actor.desktop.invalidate_identity_cache()
        return verify(*args, **kwargs)
    monkeypatch.setattr(actor.desktop, "verify_group", changed_group)
    committed = []
    result = actor.desktop.deliver(TEXT, reservation_id="old-owner-epoch",
        expected_content_digest=group.chat_frame.content_digest, expected_context_frame=group.chat_frame,
        group_verification=group, before_click=lambda: committed.append(True))
    assert result.status == "not_sent" and result.phase == "precheck" and result.retryable
    assert result.reason_code == "identity_cache_invalidated_before_click" and not committed
    assert not any(call["kind"] == "click" and (call["x"], call["y"]) == SEND.center for call in actor.calls)


def test_first_numeric_group_and_self_checks_then_hot_queries_make_no_card_clicks(actor):
    original_group, original_self = prime(actor)
    assert len(card_clicks(actor)) == 2
    aliases = [item.model_dump() for item in actor.desktop.verified_self_aliases]
    assert aliases
    current = actor.capture()
    before = list(actor.calls)
    group = actor.desktop.verify_group(frame=current)
    own = actor.desktop.verify_self()
    assert actor.calls == before
    assert group.chat_frame is current and set(original_group.evidence_refs) <= set(group.evidence_refs)
    assert current.evidence_ref in group.evidence_refs and current.a11y_evidence_ref in group.evidence_refs
    assert own.capture_id == original_self.capture_id and own.observed_at == original_self.observed_at
    assert own.profile_evidence_refs == original_self.profile_evidence_refs
    assert current.evidence_ref in own.evidence_refs and current.a11y_evidence_ref in own.evidence_refs
    assert own.attribution_method == "qq_session_cached_self_v1"
    assert [item.model_dump() for item in actor.desktop.verified_self_aliases] == aliases


@pytest.mark.parametrize(
    "defect",
    [
        "expired",
        "not_projected",
        "no_tree",
        "no_a11y_ref",
        "title_pixels",
        "title_text",
        "unknown_scene",
        "blocked",
        "window_id",
        "active_window",
        "window_rect",
    ],
)
def test_identity_helpers_refuse_stale_unverified_changed_or_different_window_evidence(actor, defect):
    prime(actor)
    item = actor.capture()
    reading = actor.desktop.inspect(item)
    if defect == "expired":
        actor.tick += 1.01
    elif defect == "not_projected":
        actor.desktop._a11y_frames.remove(item.capture_id)
    elif defect == "no_tree":
        item = replace(item, a11y_tree=None)
    elif defect == "no_a11y_ref":
        item = replace(item, a11y_evidence_ref=None)
    elif defect == "title_pixels":
        actor.title_changed = True
        item = actor.capture()
        reading = actor.desktop.inspect(item)
    elif defect == "title_text":
        reading = reading.model_copy(
            update={"group_title": reading.group_title.model_copy(update={"display_name": "另一个合成群"})}
        )
    elif defect in {"unknown_scene", "blocked"}:
        reading = reading.model_copy(update={"scene": "uncertain"} if defect == "unknown_scene" else {"blocked": True})
    elif defect == "window_id":
        item = replace(item, window_id="0x999")
    elif defect == "active_window":
        item = replace(item, active_window_id="0x999")
    else:
        item = replace(item, window_rect=(1, 27, 1280, 693))
    assert not actor.desktop._group_identity_matches(item, reading)
    assert actor.desktop._self_visual_hint(item, reading) is None


@pytest.mark.parametrize("change", ["title_pixels", "title_text", "vision_only"])
def test_group_cache_miss_repeats_numeric_verification_and_invalidates_self(actor, change):
    prime(actor)
    old_epoch = actor.desktop._identity_epoch
    if change == "title_pixels":
        actor.title_changed = True
    elif change == "title_text":
        actor.title = "另一个合成标题(3)"
    current = actor.capture()
    if change == "vision_only":
        actor.desktop._a11y_frames.remove(current.capture_id)
    before = len(card_clicks(actor))
    result = actor.desktop.verify_group(frame=current)
    assert result.group_id == "10001" and len(card_clicks(actor)) == before + 1
    assert actor.desktop._identity_epoch != old_epoch
    assert actor.desktop._self_identity is None and actor.desktop.verified_self_aliases == []


@pytest.mark.parametrize("defect", ["blocked", "unknown_scene", "background_window"])
def test_ambiguous_chat_invalidates_cache_and_never_clicks(actor, defect):
    prime(actor)
    current = actor.capture()
    reading = actor.desktop.inspect(current)
    if defect == "background_window":
        current = replace(current, active_window_id="0x999")
    else:
        actor.desktop._screens[current.capture_id] = reading.model_copy(
            update={"blocked": True} if defect == "blocked" else {"scene": "uncertain"}
        )
    before = list(actor.calls)
    with pytest.raises(DesktopError, match="unambiguous_group_chat_required"):
        actor.desktop.verify_group(frame=current)
    assert actor.calls == before
    assert actor.desktop._group_identity_anchor is actor.desktop._self_identity is None
    assert actor.desktop.verified_self_aliases == []


@pytest.mark.parametrize("change", ["portrait", "alias", "ambiguous_roster"])
def test_self_changed_or_ambiguous_hint_never_reuses_old_profile(actor, change):
    _, original = prime(actor)
    if change == "portrait":
        actor.portrait_changed = True
    elif change == "alias":
        actor.alias = "Misty(另一合成备注)"
    else:
        actor.duplicate_self = True
    actor.capture()
    before = len(card_clicks(actor))
    if change == "ambiguous_roster":
        with pytest.raises(DesktopError, match="unique_visible_self_candidate_required"):
            actor.desktop.verify_self()
        assert len(card_clicks(actor)) == before and actor.desktop._self_identity is None
    else:
        refreshed = actor.desktop.verify_self()
        assert len(card_clicks(actor)) == before + 1 and refreshed.capture_id != original.capture_id
        assert refreshed.attribution_method == "qq_visual_avatar_profile_v1"


def test_restarting_backend_does_not_inherit_identity_or_alias_cache(actor):
    prime(actor)
    restarted = DesktopBackend(actor.desktop.config, agent_client=actor.desktop.agent, llm=object())
    assert restarted._group_identity_anchor is restarted._self_identity is None
    assert restarted.verified_self_aliases == []
    assert restarted._identity_epoch != actor.desktop._identity_epoch


def test_portrait_hash_is_inner_pixels_not_position_and_rejects_tiny_regions(actor):
    item = actor.capture()
    white_a = Rect(x=200, y=200, width=24, height=24)
    white_b = Rect(x=600, y=300, width=24, height=24)
    assert actor.desktop._portrait_digest(item, white_a) == actor.desktop._portrait_digest(item, white_b)
    assert actor.desktop._portrait_digest(item, white_a) != actor.desktop._portrait_digest(item, AVATAR)
    assert actor.desktop._portrait_digest(item, Rect(x=200, y=200, width=15, height=24)) is None


@pytest.mark.parametrize("missing_bubble", [False, True])
def test_hot_delivery_still_captures_fresh_preclick_and_requires_new_bubble(actor, missing_bubble):
    original_group, original_self = prime(actor)
    current = actor.capture()
    group = actor.desktop.verify_group(frame=current)
    before_cards = len(card_clicks(actor))
    actor.missing_bubble = missing_bubble
    callback = []

    def before_click():
        proof = actor.desktop.last_preclick_proof
        assert proof is not None
        assert proof["frame_evidence_ref"] == actor.captures[-1].evidence_ref
        assert actor.captures[-1].capture_id != current.capture_id
        assert actor.desktop.inspect(actor.captures[-1]).visible_draft == TEXT
        callback.append(len(actor.calls))

    result = actor.desktop.deliver(
        TEXT,
        reservation_id="synthetic-hot-send",
        expected_content_digest=current.content_digest,
        expected_context_frame=current,
        group_verification=group,
        before_click=before_click,
    )
    assert result.status == ("unknown" if missing_bubble else "sent")
    assert result.reason_code == (
        "new_outgoing_bubble_not_verified" if missing_bubble else "fresh_outgoing_bubble_verified"
    )
    assert len(card_clicks(actor)) == before_cards and len(callback) == 1
    assert actor.calls[callback[0]]["kind"] == "click"
    assert (actor.calls[callback[0]]["x"], actor.calls[callback[0]]["y"]) == SEND.center
    assert actor.captures[-1].evidence_ref in result.evidence_refs
    if not missing_bubble:
        assert actor.desktop.inspect(actor.captures[-1]).messages[0].text == TEXT
    assert set(original_group.evidence_refs + original_self.profile_evidence_refs) <= set(result.evidence_refs)
    count = len(actor.calls)
    duplicate = actor.desktop.deliver(
        TEXT,
        reservation_id="synthetic-hot-send",
        expected_content_digest=current.content_digest,
        before_click=lambda: pytest.fail("The same reservation must never click twice"),
    )
    assert duplicate.status == "unknown" and len(actor.calls) == count
