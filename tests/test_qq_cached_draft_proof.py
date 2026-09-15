"""Public delivery path with synthetic final-frame caret and AX variations."""
from dataclasses import replace
import hashlib
import io
from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest

from harness.qq.desktop import DesktopBackend, _pixels
from tests.test_qq_desktop import INPUT, SEND, TEXT, backend as backend, message
from tests.test_qq_desktop_identity_cache import actor as actor, card_clicks, prime


def exercise(actor, monkeypatch, *, cursor_changes, fresh_text=TEXT, fresh_ax=True,
             fresh_age_seconds=0, staged_change=None):
    prime(actor)
    current = actor.capture()
    group = actor.desktop.verify_group(frame=current)
    cards_before = len(card_clicks(actor))
    original_capture = actor.desktop.capture
    staged = []
    committed = []

    def capture_with_final_frame_change():
        frame = original_capture()
        if actor.mode != "staged":
            return frame
        staged.append(frame)
        is_final = len(staged) == 2
        if not is_final and staged_change is None:
            return frame
        # The first staged capture proves the entered draft; the second is
        # obtained by production _fresh(force_capture=True) before send.
        png = frame.png
        if (is_final and cursor_changes) or (not is_final and staged_change == "context"):
            with Image.open(io.BytesIO(png)) as image:
                pixels = image.convert("RGB")
            draw = ImageDraw.Draw(pixels)
            if is_final:
                draw.line((100, 560, 100, 580), fill="black", width=1)
            else:
                draw.rectangle((80, 400, 250, 440), fill="black")
            output = io.BytesIO()
            pixels.save(output, format="PNG")
            png = output.getvalue()
        changed = replace(frame, png=png, evidence_ref="evidence://sha256/" + hashlib.sha256(png).hexdigest())
        changed = replace(changed, content_digest=actor.desktop.content_digest(changed))
        if is_final and not fresh_ax:
            changed = replace(changed, a11y_tree=None, a11y_evidence_ref=None)
            actor.desktop._a11y_frames.discard(changed.capture_id)
        reading = actor.desktop._screens[frame.capture_id]
        updates = {"evidence_ref": changed.evidence_ref}
        if is_final:
            updates["visible_draft"] = fresh_text
        elif staged_change == "text":
            updates["visible_draft"] = "错误的合成草稿"
        elif staged_change == "context":
            updates["messages"] = [message(text="新的合成上下文", direction="incoming")]
        actor.desktop._screens[frame.capture_id] = reading.model_copy(update=updates)
        actor.desktop._remember_capture(changed, actor.tick - (fresh_age_seconds if is_final else 0))
        actor.captures[-1] = changed
        staged[-1] = changed
        return changed

    monkeypatch.setattr(actor.desktop, "capture", capture_with_final_frame_change)
    # Guard against the fixture accidentally replacing the paths under test.
    assert actor.desktop.deliver.__func__ is DesktopBackend.deliver
    assert actor.desktop._fresh.__func__ is DesktopBackend._fresh

    def before_click():
        assert len(staged) == 2
        assert actor.desktop.last_preclick_proof["frame_evidence_ref"] == staged[-1].evidence_ref
        committed.append(True)

    result = actor.desktop.deliver(
        TEXT, reservation_id="synthetic-cached-caret-proof",
        expected_content_digest=current.content_digest, expected_context_frame=current,
        group_verification=group, before_click=before_click,
    )
    assert len(staged) == (1 if staged_change else 2), "The mutation must reach the intended capture."
    assert len(card_clicks(actor)) == cards_before
    if staged_change is None:
        assert staged[0].capture_id != staged[1].capture_id
        assert actor.desktop.inspect(staged[0]).visible_draft == TEXT
    send_calls = [call for call in actor.calls
                  if call["kind"] == "click" and (call["x"], call["y"]) == SEND.center]
    return SimpleNamespace(result=result, staged=staged, committed=committed, send_calls=send_calls)


def test_caret_pixel_change_with_fresh_ax_exact_reply_can_send(actor, monkeypatch):
    outcome = exercise(actor, monkeypatch, cursor_changes=True)
    before, fresh = outcome.staged
    assert _pixels(before.png, (INPUT.bounds,)) != _pixels(fresh.png, (INPUT.bounds,))
    assert _pixels(before.png, (SEND.bounds,)) == _pixels(fresh.png, (SEND.bounds,))
    assert actor.desktop.inspect(fresh).input_box == actor.desktop.inspect(before).input_box
    assert actor.desktop.inspect(fresh).visible_draft == TEXT
    assert outcome.result.status == "sent", outcome.result.reason_code
    assert outcome.committed == [True] and len(outcome.send_calls) == 1
    assert fresh.evidence_ref in outcome.result.evidence_refs
    assert fresh.a11y_evidence_ref in outcome.result.evidence_refs
    assert actor.captures[-1].capture_id != fresh.capture_id
    assert actor.desktop.inspect(actor.captures[-1]).messages[0].text == TEXT
    assert actor.captures[-1].evidence_ref in outcome.result.evidence_refs


def test_final_ax_draft_is_not_rechecked_after_staged_text_verification(actor, monkeypatch):
    outcome = exercise(actor, monkeypatch, cursor_changes=False, fresh_text="另一个合成草稿")
    before, fresh = outcome.staged
    assert _pixels(before.png, (INPUT.bounds,)) == _pixels(fresh.png, (INPUT.bounds,))
    assert actor.desktop.inspect(fresh).visible_draft != TEXT
    assert outcome.result.status == "sent", outcome.result.reason_code
    assert outcome.committed == [True] and len(outcome.send_calls) == 1
    assert fresh.evidence_ref in outcome.result.evidence_refs


def test_caret_pixel_change_without_final_ax_can_send(actor, monkeypatch):
    outcome = exercise(actor, monkeypatch, cursor_changes=True, fresh_ax=False)
    before, fresh = outcome.staged
    assert _pixels(before.png, (INPUT.bounds,)) != _pixels(fresh.png, (INPUT.bounds,))
    assert fresh.a11y_tree is None and fresh.capture_id not in actor.desktop._a11y_frames
    assert outcome.result.status == "sent", outcome.result.reason_code
    assert outcome.committed == [True] and len(outcome.send_calls) == 1
    assert fresh.evidence_ref in outcome.result.evidence_refs


def test_final_capture_taking_over_one_second_can_send(actor, monkeypatch):
    outcome = exercise(actor, monkeypatch, cursor_changes=False, fresh_age_seconds=1.015)
    fresh = outcome.staged[-1]
    assert not actor.desktop._frame_is_recent(fresh, 1.0)
    assert actor.desktop._frame_is_recent(fresh)
    assert outcome.result.status == "sent", outcome.result.reason_code
    assert outcome.committed == [True] and len(outcome.send_calls) == 1
    assert fresh.evidence_ref in outcome.result.evidence_refs


@pytest.mark.parametrize("staged_change", ["text", "context"])
def test_staged_text_and_context_still_must_match_before_final_capture(actor, monkeypatch, staged_change):
    outcome = exercise(actor, monkeypatch, cursor_changes=False, staged_change=staged_change)
    assert outcome.result.status == "not_sent" and outcome.result.phase == "precheck"
    assert outcome.result.reason_code == "staged_text_or_context_mismatch"
    assert not outcome.committed and not outcome.send_calls
    assert outcome.staged[0].evidence_ref in outcome.result.evidence_refs
