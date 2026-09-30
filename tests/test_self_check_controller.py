"""Runtime SelfCheckController: heartbeat throttle, journal, DIAGNOSTIC routes."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from harness.runtime import ControlPlane, PassiveHomeostasisAdapter, RuntimeSupervisor, create_control_app
from harness.runtime.models import RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle
from harness.runtime.self_check import SelfCheckController, SelfCheckSettings
from tests.test_runtime_supervisor import _manifest, _pipeline_adapter


def _supervisor(tmp_path, *, mode="study_runtime"):
    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(mode=mode),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime",
    )
    supervisor.lifecycle = RuntimeLifecycle.RUNNING
    supervisor.self_check = SelfCheckController(
        supervisor, tmp_path / "runtime", SelfCheckSettings(interval_seconds=60, evidence_window_hours=24),
    )
    return supervisor, cognitive


def test_heartbeat_schedules_background_task_and_throttles(tmp_path):
    async def scenario():
        supervisor, cognitive = _supervisor(tmp_path)
        started = asyncio.Event()
        released = asyncio.Event()
        original = supervisor.self_check.collect

        async def blocked():
            started.set()
            await released.wait()
            return await original()

        supervisor.self_check.collect = blocked
        await supervisor.self_check.heartbeat()
        await asyncio.wait_for(started.wait(), timeout=1)
        assert supervisor.self_check._task is not None and not supervisor.self_check._task.done()
        await supervisor.self_check.heartbeat()
        assert supervisor.self_check._task is not None
        released.set()
        await supervisor.self_check._task
        types = [record.record_type for record in supervisor.journal.iter_records()]
        assert "self_check_completed" in types
        await supervisor.self_check.heartbeat()
        assert supervisor.self_check._task is None or supervisor.self_check._task.done()
        await supervisor.self_check.close()
        cognitive.pipeline.memory.close()
    asyncio.run(scenario())


def test_selfcheck_routes_are_diagnostic_on_study_runtime(tmp_path):
    supervisor, cognitive = _supervisor(tmp_path, mode="study_runtime")
    app = create_control_app(ControlPlane(supervisor, token="secret"))
    with TestClient(app) as client:
        assert client.get("/v1/selfcheck").status_code == 401
        assert client.post("/v1/selfcheck:run").status_code == 401
        headers = {"Authorization": "Bearer secret"}
        snapshot = client.get("/v1/selfcheck", headers=headers)
        assert snapshot.status_code == 200
        assert snapshot.json()["enabled"] is True
        # collect() touches sqlite on the creating thread; TestClient portals
        # POST /v1/selfcheck:run onto another thread.
        report = asyncio.run(supervisor.self_check.run_now())
        assert report["lifecycle"] == "RUNNING"
        latest = (tmp_path / "runtime" / "self-check" / "latest.json")
        assert latest.is_file()
        reread = client.get("/v1/selfcheck", headers=headers)
        assert reread.status_code == 200
        assert reread.json()["latest"]["lifecycle"] == "RUNNING"
    asyncio.run(supervisor.self_check.close())
    cognitive.pipeline.memory.close()


def test_selfcheck_heartbeat_from_runtime_event_does_not_block_queue(tmp_path):
    async def scenario():
        supervisor, cognitive = _supervisor(tmp_path)
        supervisor.self_check._last_started_at = None
        await supervisor.submit_event(
            event_type=RuntimeEventType.HEARTBEAT, priority=RuntimeEventPriority.HEARTBEAT,
            source="test", payload={})
        queued = await supervisor.queue.get(timeout=0)
        result = await supervisor.dispatch(queued)
        assert result.status == "completed"
        if supervisor.self_check._task is not None:
            await supervisor.self_check._task
        await supervisor.self_check.close()
        cognitive.pipeline.memory.close()
    asyncio.run(scenario())
