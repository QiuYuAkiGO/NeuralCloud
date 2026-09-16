from __future__ import annotations

import os
import sys
from types import SimpleNamespace

from harness.runtime.monitor import MonitorStore
from harness.runtime.resources import ResourceSampler, ResourceSample


def _fake_processes(monkeypatch):
    processes = {os.getpid(): {"generation": 1, "cpu": 0.0}, 42: {"generation": 1, "cpu": 0.0}}

    class Process:
        def __init__(self, pid):
            if pid not in processes:
                raise ProcessLookupError(pid)
            self.pid = pid
            self.generation = processes[pid]["generation"]
            self.previous_cpu = None

        def is_running(self):
            return self.pid in processes and processes[self.pid]["generation"] == self.generation

        def cpu_percent(self, interval=None):
            assert interval is None
            current_cpu = processes[self.pid]["cpu"]
            result = 0.0 if self.previous_cpu is None else current_cpu - self.previous_cpu
            self.previous_cpu = current_cpu
            return result

        def memory_info(self):
            return SimpleNamespace(rss=4096)

        def num_threads(self):
            return 2

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        Process=Process, cpu_percent=lambda interval: 10.0,
        virtual_memory=lambda: SimpleNamespace(percent=20.0),
    ))
    return processes


def test_resource_sampler_measures_cpu_between_samples_and_resets_reused_pid(tmp_path, monkeypatch) -> None:
    processes = _fake_processes(monkeypatch)
    sampler = ResourceSampler(state_dir=tmp_path)
    monkeypatch.setattr(sampler, "_registry_pids", lambda: [("cognitive", 42)])
    first = sampler.sample()
    assert next(row for row in first if row.pid == 42).cpu_percent == 0.0

    processes[42]["cpu"] = 57.0
    second = sampler.sample()
    assert next(row for row in second if row.pid == 42).cpu_percent == 57.0

    processes[42] = {"generation": 2, "cpu": 2.0}
    third = sampler.sample()
    assert next(row for row in third if row.pid == 42).cpu_percent == 0.0
    processes[42]["cpu"] = 35.0
    fourth = sampler.sample()
    assert next(row for row in fourth if row.pid == 42).cpu_percent == 33.0


def test_resource_sampler_discards_exited_and_untracked_processes(tmp_path, monkeypatch) -> None:
    processes = _fake_processes(monkeypatch)
    sampler = ResourceSampler(state_dir=tmp_path)
    monkeypatch.setattr(sampler, "_registry_pids", lambda: [("cognitive", 42)])
    sampler.sample()
    assert 42 in sampler._processes
    del processes[42]
    rows = sampler.sample()
    assert not next(row for row in rows if row.pid == 42).available
    assert 42 not in sampler._processes

    processes[42] = {"generation": 2, "cpu": 0.0}
    sampler.sample()
    assert 42 in sampler._processes
    monkeypatch.setattr(sampler, "_registry_pids", lambda: [])
    monkeypatch.setattr(sampler, "_cmdline_pids", lambda _: [])
    sampler.sample()
    assert set(sampler._processes) == {os.getpid()}


def test_resource_sampler_uses_pid_file_without_scanning_all_processes(tmp_path, monkeypatch) -> None:
    pid_file = tmp_path / ".neuralcloud.pids.json"
    pid_file.write_text('{"services":[{"name":"cognitive","pid":65534}]}', encoding="utf-8")
    sampler = ResourceSampler(pid_file=pid_file, state_dir=tmp_path)
    called = {"cmdline": False}
    monkeypatch.setattr(sampler, "_cmdline_pids", lambda psutil: called.__setitem__("cmdline", True) or [])
    rows = sampler.sample()
    roles = {item.role for item in rows}
    assert "runtime" in roles
    assert "cognitive" in roles
    assert called["cmdline"] is False


def test_resource_sampler_records_self_process(tmp_path) -> None:
    sampler = ResourceSampler(pid_file=tmp_path / "missing.json", state_dir=tmp_path)
    first = sampler.sample()
    second = sampler.sample()
    rows = second or first
    self_rows = [item for item in rows if item.role == "runtime"]
    assert self_rows
    host = [item for item in rows if item.host_cpu_percent is not None or item.role == "host"]
    assert host
    if self_rows[0].available:
        assert self_rows[0].pid is not None
        assert self_rows[0].rss_bytes is not None


def test_monitor_store_persists_and_queries_resources(tmp_path) -> None:
    store = MonitorStore(tmp_path / "observability.sqlite3")
    observed = "2026-09-15T20:00:00+00:00"
    store.append_resources([
        ResourceSample(observed_at=observed, role="runtime", pid=1, cpu_percent=2.5,
                       rss_bytes=10_000_000, num_threads=8, host_cpu_percent=11.0,
                       host_mem_percent=40.0, available=True),
        ResourceSample(observed_at=observed, role="cognitive", pid=2, available=False, detail="NoSuchProcess",
                       host_cpu_percent=11.0, host_mem_percent=40.0),
    ])
    rows = store.query_resources("2026-09-15T00:00:00+00:00", "2026-09-16T00:00:00+00:00")
    assert len(rows) == 2
    assert rows[0]["role"] == "runtime" and rows[0]["cpu_percent"] == 2.5
    assert rows[1]["available"] is False
    store.close()
