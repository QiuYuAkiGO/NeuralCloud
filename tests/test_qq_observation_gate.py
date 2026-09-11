import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.qq.desktop import DesktopError
from harness.qq.telemetry import QQDeadline
from tests import test_qq_desktop as fixtures

backend = fixtures.backend


@pytest.mark.parametrize("operation", ["capture", "action"])
def test_gui_waits_for_sensor_and_releases_gate_on_failure(backend, monkeypatch, operation):
    desktop, _ = backend
    gate = threading.Lock()
    desktop._observation_lock = gate
    entered = threading.Event()

    def fail(*args, **kwargs):
        entered.set()
        raise RuntimeError("synthetic_capture_failure")

    monkeypatch.setattr(desktop, "_capture_unlocked" if operation == "capture" else "_act_unlocked", fail)
    invoke = desktop.capture if operation == "capture" else lambda: desktop._act({"kind": "press_keys", "keys": ["escape"]})
    gate.acquire()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(invoke)
        assert not entered.wait(.03)
        gate.release()
        with pytest.raises(RuntimeError, match="synthetic_capture_failure"):
            result.result(timeout=1)
    assert entered.is_set()
    assert gate.acquire(blocking=False)
    gate.release()


def test_sensor_queue_wait_cannot_extend_event_deadline(backend, monkeypatch):
    desktop, _ = backend
    gate = threading.Lock()
    desktop._observation_lock = gate
    called = []
    monkeypatch.setattr(desktop, "_capture_unlocked", lambda: called.append(True))
    gate.acquire()
    started = time.monotonic()
    try:
        with desktop.turn(deadline=QQDeadline.after(.04)):
            with pytest.raises(DesktopError, match="observation_gate_busy"):
                desktop.capture()
        assert time.monotonic() - started < .5
        assert called == []
        assert gate.locked()
    finally:
        gate.release()
