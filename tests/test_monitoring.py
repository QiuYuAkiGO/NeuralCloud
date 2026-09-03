from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from harness.runtime import ControlPlane, PassiveHomeostasisAdapter, RuntimeSupervisor, create_control_app
from harness.runtime.monitor import MonitorRecorder, MonitorStore, sanitize_payload
from tests.test_runtime_supervisor import _manifest, _pipeline_adapter


def test_monitor_store_sanitizes_projects_runs_and_pages(tmp_path) -> None:
    store = MonitorStore(tmp_path / "observability.sqlite3")
    assert sanitize_payload(
        {
            "api_key": "secret",
            "nested": {"control_token": "also-secret", "total_tokens": 12},
            "embedding": [0.1, 0.2],
        }
    ) == {
        "api_key": "[REDACTED]",
        "nested": {"control_token": "[REDACTED]", "total_tokens": 12},
    }

    accepted = store.append_event(
        "event.accepted",
        {"event_id": "evt_1", "correlation_id": "run_1", "received_at": datetime.now(UTC).isoformat()},
    )
    assert accepted is not None
    completed = store.append_event(
        "event.completed",
        {"event_id": "evt_1", "status": "completed", "completed_at": datetime.now(UTC).isoformat()},
    )
    assert completed is not None
    assert completed.correlation_id == "run_1"

    store.append_event(
        "cognitive.ui",
        {"type": "stage", "run_id": "run_1", "stage": "parse", "latency_ms": 4.5, "data": {}},
    )
    store.append_event(
        "cognitive.ui",
        {
            "type": "final",
            "run_id": "run_1",
            "stage": "final",
            "latency_ms": 20,
            "data": {
                "path": "fast",
                "latency_ms": 20,
                "revisions": 1,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        },
    )
    run = store.get_run("run_1")
    assert run is not None
    assert run["status"] == "completed"
    assert run["path"] == "fast"
    assert run["total_tokens"] == 15
    assert [stage["stage"] for stage in run["stages"]] == ["parse", "final"]

    first = store.list_events(limit=2)
    assert first["has_more"] is True
    second = store.list_events(limit=2, cursor=first["next_cursor"])
    assert {item["seq"] for item in first["items"]}.isdisjoint(
        {item["seq"] for item in second["items"]}
    )
    store.close()


def test_call_log_backfill_is_idempotent_and_cleanup_is_projection_only(tmp_path) -> None:
    store = MonitorStore(tmp_path / "observability.sqlite3", retention_days=7)
    payload = {
        "call_id": "run_backfill",
        "kind": "respond",
        "ts": datetime.now(UTC).isoformat(),
        "latency_ms": 42,
        "steps": [
            {"kind": "ingest", "latency_ms": 1, "data": {"event_id": "evt_backfill"}},
            {"kind": "final", "latency_ms": 2, "data": {"path": "deep"}},
        ],
        "result": {
            "event_id": "evt_backfill",
            "path": "deep",
            "revisions": 0,
            "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
        },
    }
    path = tmp_path / "run_backfill.json"
    store.append_call_log(path, payload)
    store.append_call_log(path, payload)
    events = store.list_events(correlation_id="run_backfill", limit=20)["items"]
    assert len(events) == 2
    assert store.get_run("run_backfill")["total_tokens"] == 12

    old = datetime.now(UTC) - timedelta(days=8)
    store.append_event("watchdog", {"reason": "old"}, observed_at=old, source_ref="old")
    assert store.cleanup() >= 1
    assert not any(item["payload"].get("reason") == "old" for item in store.list_events(limit=20)["items"])
    store.close()


def test_monitor_recorder_projects_live_events_without_affecting_runtime(tmp_path) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime",
    )
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    recorder = MonitorRecorder(
        store,
        journal=supervisor.journal,
        status_provider=supervisor.status,
        trace_dir=tmp_path / "traces",
        sample_seconds=60,
    )

    async def scenario() -> None:
        await recorder.start()
        recorder.enqueue("watchdog", {"action": "degrade", "reason": "test"})
        await asyncio.sleep(0.05)
        assert store.list_events(topic="watchdog", limit=10)["items"]
        assert supervisor.lifecycle.value == "BOOTING"
        await recorder.close()

    asyncio.run(scenario())
    cognitive.pipeline.memory.close()


def test_monitor_api_auth_overview_and_study_debug_gate(tmp_path) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(mode="study_runtime"),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime",
    )
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    recorder = MonitorRecorder(
        store,
        journal=supervisor.journal,
        status_provider=supervisor.status,
        trace_dir=tmp_path / "traces",
    )
    app = create_control_app(ControlPlane(supervisor, token="secret"), monitor=recorder)
    with TestClient(app) as client:
        assert client.get("/v1/monitor/overview").status_code == 401
        headers = {"Authorization": "Bearer secret"}
        overview = client.get("/v1/monitor/overview", headers=headers)
        assert overview.status_code == 200
        assert overview.json()["schema_version"] == "monitor-v1"
        assert overview.json()["manifest"]["control_mode"] == "study_runtime"
        rejected = client.post(
            "/v1/debug/events",
            headers=headers,
            json={"event_type": "diagnostic", "payload": {}},
        )
        assert rejected.status_code == 403
    store.close()
    cognitive.pipeline.memory.close()
