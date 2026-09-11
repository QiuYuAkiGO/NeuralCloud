"""A continuation may absorb exactly its own confirmed bubble, never arrivals."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from harness.qq.desktop import DeliveryResult, DesktopError, GroupVerification
from tests.test_qq_desktop import backend as backend, frame, message, screen, TEXT


def prepared(desktop, monkeypatch, *, before=None, after=None):
    moment = datetime.now(timezone.utc)
    old = replace(frame(desktop, "before", moment=moment), a11y_tree={"synthetic": True}, a11y_evidence_ref="ax:before")
    new = replace(frame(desktop, "after", moment=moment), a11y_tree={"synthetic": True}, a11y_evidence_ref="ax:after")
    before = [message(direction="incoming", text="一个问题")] if before is None else before
    after = [*before, message(text=TEXT)] if after is None else after
    old_reading, new_reading = screen(old, messages=before), screen(new, messages=after)
    group = GroupVerification("10001", "虚构群", [new.evidence_ref], new, new_reading)
    result = DeliveryResult("sent", [new.evidence_ref], "fresh_outgoing_bubble_verified",
                            post_send_frame=new, post_send_verification=group)
    desktop._a11y_frames.update([old.capture_id, new.capture_id])
    monkeypatch.setattr(desktop, "inspect", lambda item: old_reading if item is old else new_reading)
    desktop._remember_segment_result(result, TEXT)
    return old, result


def test_confirmed_own_append_advances_exact_post_frame_without_capture(backend, monkeypatch):
    desktop, calls = backend
    old, result = prepared(desktop, monkeypatch)
    monkeypatch.setattr(desktop, "capture", lambda: pytest.fail("must not take an arbitrary later frame"))
    new, group = desktop.advance_segment_context(old, result, TEXT)
    assert new is result.post_send_frame and group is result.post_send_verification
    assert desktop._group is group and not calls


@pytest.mark.parametrize("extra", [message(direction="incoming", text="新的问题"), message(text="另一条自己的消息")])
def test_other_arrival_alongside_confirmed_bubble_does_not_advance(backend, monkeypatch, extra):
    desktop, _ = backend
    original = message(direction="incoming", text="一个问题")
    old, result = prepared(desktop, monkeypatch, before=[original], after=[original, extra, message(text=TEXT)])
    with pytest.raises(DesktopError, match="segment_context_changed"):
        desktop.advance_segment_context(old, result, TEXT)
    assert result.status == "sent"  # Previous real delivery is not rewritten.


def test_unique_visible_suffix_permits_scroll(backend, monkeypatch):
    desktop, _ = backend
    originals = [message(direction="incoming", text=text) for text in ("更早", "仍然可见")]
    old, result = prepared(desktop, monkeypatch, before=originals, after=[originals[-1], message(text=TEXT)])
    assert desktop.advance_segment_context(old, result, TEXT)[0] is result.post_send_frame


@pytest.mark.parametrize("history", [[], [message(direction="incoming", text="重复")]*2])
def test_lost_or_ambiguous_existing_anchor_stops_continuation(backend, monkeypatch, history):
    desktop, _ = backend
    old_items = history or [message(direction="incoming", text="消失")]
    remaining = history[:1]
    old, result = prepared(desktop, monkeypatch, before=old_items, after=[*remaining, message(text=TEXT)])
    with pytest.raises(DesktopError, match="segment_context_changed"):
        desktop.advance_segment_context(old, result, TEXT)


@pytest.mark.parametrize("change", ["forged", "epoch", "missing_ax", "wrong_text", "unknown"])
def test_continuation_requires_backend_result_identity_epoch_and_paired_evidence(backend, monkeypatch, change):
    desktop, _ = backend
    old, result = prepared(desktop, monkeypatch)
    text = TEXT
    if change == "forged":
        result = replace(result)
    elif change == "epoch":
        desktop.invalidate_identity_cache()
    elif change == "missing_ax":
        desktop._a11y_frames.clear()
    elif change == "wrong_text":
        text = "别的正文"
    else:
        result = replace(result, status="unknown")
    with pytest.raises(DesktopError, match="segment_continuity_unavailable"):
        desktop.advance_segment_context(old, result, text)
