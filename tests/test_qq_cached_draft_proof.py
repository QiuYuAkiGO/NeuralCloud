"""Public delivery path with synthetic final-frame caret and AX variations."""
from dataclasses import replace
import hashlib
import io
from types import SimpleNamespace

from PIL import Image, ImageDraw

from harness.qq.desktop import DesktopBackend, _pixels
from tests.test_qq_desktop import INPUT, SEND, TEXT, backend as backend
from tests.test_qq_desktop_identity_cache import actor as actor, card_clicks, prime


def exercise(actor, monkeypatch, *, cursor_changes, fresh_text=TEXT, fresh_ax=True):
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
        if len(staged) != 2:
            return frame
        # The first staged capture proves the entered draft; the second is
        # obtained by production _fresh(force_capture=True) before send.
        png = frame.png
        if cursor_changes:
            with Image.open(io.BytesIO(png)) as image:
                pixels = image.convert("RGB")
            ImageDraw.Draw(pixels).line((100, 560, 100, 580), fill="black", width=1)
            output = io.BytesIO()
            pixels.save(output, format="PNG")
            png = output.getvalue()
        changed = replace(frame, png=png, evidence_ref="evidence://sha256/" + hashlib.sha256(png).hexdigest())
        changed = replace(changed, content_digest=actor.desktop.content_digest(changed))
        if not fresh_ax:
            changed = replace(changed, a11y_tree=None, a11y_evidence_ref=None)
            actor.desktop._a11y_frames.discard(changed.capture_id)
        reading = actor.desktop._screens[frame.capture_id]
        actor.desktop._screens[frame.capture_id] = reading.model_copy(update={
            "evidence_ref": changed.evidence_ref, "visible_draft": fresh_text,
        })
        actor.desktop._remember_capture(changed, actor.tick)
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
    assert len(staged) == 2, "The mutation must reach the final fresh capture, not an earlier precheck."
    assert len(card_clicks(actor)) == cards_before
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


def test_fresh_ax_different_reply_cannot_send_even_when_draft_pixels_match(actor, monkeypatch):
    outcome = exercise(actor, monkeypatch, cursor_changes=False, fresh_text="另一个合成草稿")
    before, fresh = outcome.staged
    assert _pixels(before.png, (INPUT.bounds,)) == _pixels(fresh.png, (INPUT.bounds,))
    assert actor.desktop.inspect(fresh).visible_draft != TEXT
    assert outcome.result.status == "not_sent" and outcome.result.phase == "precheck"
    assert outcome.result.reason_code == "fresh_draft_not_verified"
    assert not outcome.committed and not outcome.send_calls
    assert fresh.evidence_ref in outcome.result.evidence_refs


def test_caret_pixel_change_without_fresh_ax_keeps_strict_pixel_rejection(actor, monkeypatch):
    outcome = exercise(actor, monkeypatch, cursor_changes=True, fresh_ax=False)
    before, fresh = outcome.staged
    assert _pixels(before.png, (INPUT.bounds,)) != _pixels(fresh.png, (INPUT.bounds,))
    assert fresh.a11y_tree is None and fresh.capture_id not in actor.desktop._a11y_frames
    assert outcome.result.status == "not_sent" and outcome.result.phase == "precheck"
    assert outcome.result.reason_code == "draft_pixels_changed_before_send"
    assert not outcome.committed and not outcome.send_calls
    assert fresh.evidence_ref in outcome.result.evidence_refs
