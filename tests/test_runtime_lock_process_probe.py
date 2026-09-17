"""Read-only process probes must never signal or evict a live lock owner."""
import ctypes
import errno
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from harness.runtime import locking


class NativeFunction:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def fake_kernel(monkeypatch, *, handle=321, wait=258, error=0):
    kernel = SimpleNamespace(OpenProcess=NativeFunction(handle),
        WaitForSingleObject=NativeFunction(wait), CloseHandle=NativeFunction(True))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    return kernel


@pytest.mark.parametrize("error,may_exist", [(87, False), (5, True), (0, True), (6, True), (299, True)])
def test_windows_open_failure_reclaims_only_known_absent_pid(monkeypatch, error, may_exist):
    kernel = fake_kernel(monkeypatch, handle=None, error=error)
    assert locking._windows_process_may_exist(123) is may_exist
    assert kernel.OpenProcess.calls == [(0x00100000, False, 123)]
    assert not kernel.WaitForSingleObject.calls and not kernel.CloseHandle.calls


@pytest.mark.parametrize("wait,may_exist", [(0, False), (258, True), (0xFFFFFFFF, True), (128, True)])
def test_windows_wait_is_nonblocking_and_always_closes_handle(monkeypatch, wait, may_exist):
    kernel = fake_kernel(monkeypatch, wait=wait)
    assert locking._windows_process_may_exist(123) is may_exist
    assert kernel.WaitForSingleObject.calls == [(321, 0)]
    assert kernel.CloseHandle.calls == [(321,)]


def test_windows_probe_error_is_conservative_and_closes_handle(monkeypatch):
    kernel = fake_kernel(monkeypatch, wait=OSError("synthetic probe failure"))
    assert locking._windows_process_may_exist(123)
    assert kernel.CloseHandle.calls == [(321,)]


def test_windows_pid_never_wraps_to_dword(monkeypatch):
    kernel = fake_kernel(monkeypatch, handle=None, error=87)
    assert locking._windows_process_may_exist(0x100000001)
    assert not kernel.OpenProcess.calls


@pytest.mark.parametrize("error,may_exist", [(errno.ESRCH, False), (errno.EPERM, True),
    (errno.EACCES, True), (errno.EIO, True), (errno.EINVAL, True)])
def test_posix_only_esrch_authorizes_reclaim(monkeypatch, error, may_exist):
    calls = []
    def kill(pid, signal):
        calls.append((pid, signal))
        raise OSError(error, "synthetic probe failure")
    monkeypatch.setattr(locking.os, "kill", kill)
    assert locking._posix_process_may_exist(123) is may_exist
    assert calls == [(123, 0)]


def test_posix_success_and_overflow_preserve_owner(monkeypatch):
    monkeypatch.setattr(locking.os, "kill", lambda *args: None)
    assert locking._posix_process_may_exist(123)
    def overflow(*args):
        raise OverflowError()
    monkeypatch.setattr(locking.os, "kill", overflow)
    assert locking._posix_process_may_exist(1 << 100)


def test_real_self_lock_cannot_be_stolen_or_signalled(tmp_path, monkeypatch):
    if os.name == "nt":
        def no_signal(*args):
            pytest.fail("Windows process existence must never call os.kill")
        monkeypatch.setattr(locking.os, "kill", no_signal)
    path = tmp_path / "instance.lock"
    owner = locking.SingleInstanceLock(path, runtime_instance_id="owner")
    challenger = locking.SingleInstanceLock(path, runtime_instance_id="challenger")
    owner.acquire()
    original = path.read_bytes()
    try:
        assert locking._process_may_exist(os.getpid())
        with pytest.raises(locking.InstanceLockError):
            challenger.acquire()
        assert path.read_bytes() == original and not challenger.owned
        challenger.release()
        assert path.read_bytes() == original
    finally:
        owner.release()
    assert not path.exists()


def test_real_child_live_then_exited_lock_lifecycle(tmp_path, monkeypatch):
    if os.name == "nt":
        def no_signal(*args):
            pytest.fail("Windows process existence must never call os.kill")
        monkeypatch.setattr(locking.os, "kill", no_signal)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    path = tmp_path / "instance.lock"
    path.write_text(json.dumps({"pid": child.pid, "runtime_instance_id": "child"}), encoding="utf-8")
    original = path.read_bytes()
    challenger = locking.SingleInstanceLock(path, runtime_instance_id="replacement")
    try:
        with pytest.raises(locking.InstanceLockError):
            challenger.acquire()
        assert child.poll() is None and path.read_bytes() == original
    finally:
        child.communicate(timeout=10)  # EOF exits the child without sending a signal.
    challenger.acquire()
    try:
        assert challenger.owned
        assert json.loads(path.read_text())["runtime_instance_id"] == "replacement"
    finally:
        challenger.release()


def test_reclaim_preserves_lock_replaced_while_probing(tmp_path, monkeypatch):
    path = tmp_path / "instance.lock"
    path.write_text(json.dumps({"pid": 123, "runtime_instance_id": "old"}))
    newer = json.dumps({"pid": 456, "runtime_instance_id": "new"})
    def replaced(pid):
        path.write_text(newer)
        return False
    monkeypatch.setattr(locking, "_process_may_exist", replaced)
    challenger = locking.SingleInstanceLock(path, runtime_instance_id="challenger")
    with pytest.raises(locking.InstanceLockError):
        challenger.acquire()
    assert path.read_text() == newer and not challenger.owned
