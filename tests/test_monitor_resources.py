from __future__ import annotations

from harness.runtime.monitor import MonitorStore
from harness.runtime.resources import ResourceSampler, ResourceSample


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
