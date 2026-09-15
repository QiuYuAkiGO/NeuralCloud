"""Send diagnostics retain bounded QQ gate codes, never exception message text."""
import pytest

from harness.embodiment.providers.qq import QQCommunicationError
from harness.qq.watcher import error_code
from tests import test_qq_homeostatic_watcher as fixtures

controller = fixtures.controller
model_setup = fixtures.model_setup
watcher_setup = fixtures.watcher_setup
isolated_static_parser_cache = fixtures.isolated_static_parser_cache


@pytest.mark.parametrize("code, expected", [
    ("capability_unavailable", "capability_unavailable"),
    ("driver_callback_failed", "driver_callback_failed"),
    ("chat_context_changed", "chat_context_changed"),
    ("PRIVATE QQ message\nsecret", "QQCommunicationError"),
    ("x" * 101, "QQCommunicationError"),
])
def test_qq_error_codes_are_bounded(code, expected):
    assert error_code(QQCommunicationError(code)) == expected
    assert error_code(RuntimeError("PRIVATE QQ message")) == "RuntimeError"


def test_preclick_provider_failure_keeps_specific_reason_in_ledger_and_status(controller, monkeypatch):
    state = controller
    def rejected(*args, **kwargs):
        raise QQCommunicationError("capability_unavailable")
    monkeypatch.setattr(state.watch, "_qq_action", rejected)
    with pytest.raises(QQCommunicationError, match="capability_unavailable"):
        state.watch.cycle()
    reservation = fixtures.reservations(state)[0]
    assert reservation["status"] == "not_sent" and reservation["reason"] == "capability_unavailable"
    assert state.watch.status["last_error_type"] == "capability_unavailable"
    assert not state.desktop.clicked
