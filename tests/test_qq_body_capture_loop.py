"""Real DesktopBackend capture under the provider loop, with synthetic Body I/O."""
import asyncio
from dataclasses import replace
import threading
from types import SimpleNamespace as NS

import pytest

from harness.qq import desktop as module
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired
from tests.test_qq_desktop import backend as backend, frame
from tests.test_qq_embodied_driver import action, case

embodied_case = case


def fake_body_io(desktop, monkeypatch, *, block=False):
    evidence = NS(created=[], calls=[], cancelled=False, closed=0)
    output = frame(desktop, "synthetic-loop-capture")
    actor = threading.get_ident()

    class Body:
        def __init__(self, *args, **kwargs):
            evidence.created.append(threading.get_ident())

        def record(self, operation):
            evidence.calls.append((operation, threading.get_ident(), id(asyncio.get_running_loop())))

        async def observe(self, **kwargs):
            self.record("observe")
            if block:
                try:
                    await asyncio.sleep(60)
                finally:
                    evidence.cancelled = True
            await asyncio.sleep(0)
            return NS(frame=NS(sha256="a" * 64), a11y_ref=None)

        async def get_evidence(self, *args, **kwargs):
            self.record("evidence")
            return output.png

        async def close(self):
            self.record("close")
            evidence.closed += 1

    def project(*args, **kwargs):
        assert threading.get_ident() == actor
        return output

    monkeypatch.setattr(module, "MistyAIClient", Body)
    monkeypatch.setattr(desktop, "frame_from_observation", project)
    return evidence, output


def test_capture_and_close_inside_provider_loop_keep_gui_actor_and_one_io_loop(backend, monkeypatch):
    desktop, actions = backend
    evidence, output = fake_body_io(desktop, monkeypatch)
    actor = threading.get_ident()

    async def provider_send():
        # The provider calls the synchronous actor while its loop is running.
        with desktop._lock:
            assert desktop.capture() is output
        desktop.close()

    try:
        assert desktop.capture() is output
        asyncio.run(provider_send())
        assert len(evidence.created) == 1 and evidence.created[0] != actor
        assert {item[1] for item in evidence.calls} == set(evidence.created)
        assert len({item[2] for item in evidence.calls}) == 1
        assert [item[0] for item in evidence.calls] == ["observe", "evidence", "observe", "evidence", "close"]
        assert evidence.closed == 1 and not actions
        assert not any(item.ident in evidence.created and item.is_alive() for item in threading.enumerate())
        desktop.close()
        assert evidence.closed == 1
    finally:
        desktop.close()


def test_capture_timeout_cancels_body_io_and_closes_its_thread(backend, monkeypatch):
    desktop, actions = backend
    evidence, _ = fake_body_io(desktop, monkeypatch, block=True)
    desktop.config = replace(desktop.config, request_timeout_seconds=0.5)

    async def provider_send():
        with desktop.turn(deadline=QQDeadline.after(0.2)):
            # The Body timeout and caller deadline share the same bound;
            # either may report first due to independent monotonic samples.
            with pytest.raises((module.DesktopError, QQDeadlineExpired)):
                desktop.capture()

    try:
        asyncio.run(provider_send())
    finally:
        desktop.close()
    assert evidence.cancelled and evidence.closed == 1 and not actions
    assert not any(item.ident in evidence.created and item.is_alive() for item in threading.enumerate())


def test_negotiated_send_callback_can_capture_without_moving_gui_actor(backend, embodied_case, monkeypatch):
    desktop, gui_actions = backend
    evidence, output = fake_body_io(desktop, monkeypatch)
    actor_thread = threading.get_ident()

    def read_only_actor():
        assert asyncio.get_running_loop() is embodied_case.actor._qq_loop
        assert threading.get_ident() == actor_thread
        with desktop._lock:
            assert desktop.capture() is output
        return module.DeliveryResult("not_sent", [], "synthetic_readonly_precheck")

    try:
        result = action(embodied_case, execute_fn=read_only_actor)
        assert result.status == "not_sent" and result.reason_code == "synthetic_readonly_precheck"
        assert not gui_actions and not embodied_case.clicks and not embodied_case.persistence
        assert [item[0] for item in evidence.calls] == ["observe", "evidence"]
        assert evidence.created[0] != actor_thread
    finally:
        desktop.close()
