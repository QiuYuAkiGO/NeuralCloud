"""Transport and duplicate-text recovery with synthetic pixels and HTTP only."""
from dataclasses import replace
import hashlib
import json

import httpx
import pytest

from tests.test_qq_desktop import (
    SEND, TEXT, backend as backend, delivery_setup, frame, message, screen,
)


@pytest.mark.parametrize("click_landed", [False, True])
def test_send_http_read_timeout_never_reposts_and_only_fresh_bubble_can_reconcile(backend, monkeypatch, click_landed):
    desktop, actions = backend
    before_calls = []
    arguments = delivery_setup(desktop, monkeypatch, callback=lambda: before_calls.append(True))
    send_requests = []

    def transport(request):
        if request.url.path == "/agent/v1/health":
            return httpx.Response(200, json={"body_instance_id": desktop.config.body_instance_id,
                "environment_revision": desktop.config.environment_revision,
                "status": "healthy", "takeover_active": False})
        assert request.url.path == "/agent/v1/primitives:execute"
        payload = json.loads(request.content)
        actions.append(payload["primitive"])
        if payload["step_id"].startswith("qq-send-") and payload["step_id"].endswith(":send-once"):
            expected_action = "qq-send-" + hashlib.sha256(arguments["reservation_id"].encode()).hexdigest()
            assert payload["action_id"] == expected_action
            assert payload["step_id"] == expected_action + ":send-once"
            send_requests.append(payload["action_id"])
            # The server may have executed the click before its response is lost.
            raise httpx.ReadTimeout("synthetic response loss", request=request)
        return httpx.Response(200, json={"success": True, "action_id": payload["action_id"],
                                        "step_id": payload["step_id"]})

    with httpx.Client(base_url=desktop.config.agent_url, transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(desktop, "agent", client)
        result = desktop.deliver(**arguments)
        assert result.status == "unknown" and result.phase == "click_started"
        assert not result.retryable and result.click_acknowledged_at is None
        assert len(send_requests) == len(before_calls) == 1
        proof = dict(desktop.last_preclick_proof)
        assert desktop.deliver(**arguments).status == "unknown"
        after = frame(desktop, "synthetic-timeout-reconciliation")
        reading = screen(after, messages=[message()] if click_landed else [])
        monkeypatch.setattr(desktop, "capture", lambda: after)
        monkeypatch.setattr(desktop, "inspect", lambda _: reading)
        reconciled = desktop.reconcile_delivery(TEXT, reservation_id=arguments["reservation_id"], proof=proof)
        assert reconciled.status == ("sent" if click_landed else "unknown")
        assert len(send_requests) == 1
        assert [action["kind"] for action in actions] == ["click", "type_text", "click"]
        assert desktop.deliver(**arguments).status == "unknown" and len(send_requests) == 1
        if click_landed:
            assert reconciled.click_acknowledged_at is None
            assert reconciled.click_observed_at is not None
            assert reconciled.click_observed_evidence_ref == after.evidence_ref
        else:
            assert reconciled.click_observed_at is None


@pytest.mark.parametrize("new_own_bubble", [False, True])
def test_other_members_identical_text_and_old_own_bubble_do_not_fake_send_success(backend, monkeypatch, new_own_bubble):
    desktop, actions = backend
    arguments = delivery_setup(desktop, monkeypatch)
    original_group, original_inspect = desktop.verify_group, desktop.inspect
    old_own = message()
    incoming = [message(direction="incoming", name=f"fictional-member-{index}").model_copy(
        update={"avatar": message(direction="incoming").avatar.model_copy(update={"y": 200 + index * 60}),
                "bubble": message(direction="incoming").bubble.model_copy(update={"y": 200 + index * 60})})
                for index in range(2)]

    def with_old_own(group):
        return replace(group, chat_screen=group.chat_screen.model_copy(update={"messages": [old_own]}))

    def inspect(item):
        reading = original_inspect(item)
        messages = [old_own]
        if item.capture_id == "after":
            messages += incoming
            if new_own_bubble:
                messages.append(message().model_copy(update={
                    "avatar": old_own.avatar.model_copy(update={"y": 450}),
                    "bubble": old_own.bubble.model_copy(update={"y": 450})}))
        return reading.model_copy(update={"messages": messages})

    monkeypatch.setattr(desktop, "verify_group", lambda: with_old_own(original_group()))
    monkeypatch.setattr(desktop, "inspect", inspect)
    result = desktop.deliver(**arguments)
    assert desktop.last_preclick_proof["before_outgoing_count"] == 1
    assert result.status == ("sent" if new_own_bubble else "unknown")
    if not new_own_bubble:
        assert result.reason_code == "new_outgoing_bubble_not_verified"
    assert len([action for action in actions if action.get("x") == SEND.center[0]]) == 1


def test_window_backgrounded_after_staging_rejects_final_click(backend, monkeypatch):
    desktop, actions = backend
    entered = []
    arguments = delivery_setup(desktop, monkeypatch, callback=lambda: entered.append(True))
    original_capture = desktop.capture

    def capture():
        item = original_capture()
        return replace(item, active_window_id="different-window") if item.capture_id == "fresh_staged" else item

    monkeypatch.setattr(desktop, "capture", capture)
    result = desktop.deliver(**arguments)
    assert result.status == "not_sent" and result.phase == "precheck"
    assert not entered and desktop.last_preclick_proof is None
    assert [action["kind"] for action in actions] == ["click", "type_text"]
