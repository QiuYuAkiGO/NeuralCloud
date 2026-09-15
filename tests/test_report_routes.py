from __future__ import annotations

from fastapi.testclient import TestClient

from harness.runtime import ControlPlane, PassiveHomeostasisAdapter, RuntimeSupervisor, create_control_app
from harness.runtime.daily_report import DailyReportBuilder, DailyReportController, DailyReportSettings, DailyReportStore
from harness.runtime.diary import DiaryController
from harness.diary.config import DiarySettings
from harness.runtime.monitor import MonitorStore
from tests.test_runtime_supervisor import _manifest, _pipeline_adapter


def test_report_and_diary_routes_require_auth(tmp_path) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(mode="study_runtime"),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime",
    )
    supervisor.diary = DiaryController(supervisor, DiarySettings(autonomous_enabled=False), tmp_path / "runtime")
    monitor = MonitorStore(tmp_path / "observability.sqlite3")
    supervisor.daily_report = DailyReportController(
        DailyReportBuilder(monitor=monitor, state_dir=tmp_path / "runtime", settings=DailyReportSettings(),
                           homeostasis=supervisor.homeostasis, diary_store=supervisor.diary.store,
                           runtime_status=supervisor.status),
        DailyReportStore(tmp_path / "runtime" / "reports"),
        DailyReportSettings(),
    )
    app = create_control_app(ControlPlane(supervisor, token="secret"))
    with TestClient(app) as client:
        assert client.get("/v1/report/daily").status_code == 401
        headers = {"Authorization": "Bearer secret"}
        report = client.get("/v1/report/daily", headers=headers, params={"day": "2026-09-15"})
        assert report.status_code == 200
        body = report.json()
        assert body["day"] == "2026-09-15"
        assert body["diary"]["wrote"] is False
        days = client.get("/v1/report/days", headers=headers)
        assert days.status_code == 200
        diary = client.get("/v1/diary", headers=headers)
        assert diary.status_code == 200
        assert diary.json()["wrote_today"] is False
        bodies = client.get("/v1/diary/entries", headers=headers)
        assert bodies.status_code == 403
        forbidden = client.post("/v1/events", headers=headers, json={"event_type": "diary_reflection", "payload": {}})
        assert forbidden.status_code == 403
    monitor.close()
    supervisor.diary.close()
    cognitive.pipeline.memory.close()
