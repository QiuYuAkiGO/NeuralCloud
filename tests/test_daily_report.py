from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from harness.diary.models import DiaryOriginal
from harness.diary.store import DiaryStore
from harness.runtime.daily_report import DailyReportBuilder, DailyReportSettings, DailyReportStore, day_bounds
from harness.runtime.monitor import MonitorStore
from harness.runtime.resources import ResourceSample


DAY = "2026-09-15"


def _insert_sample(store: MonitorStore, at: datetime, lifecycle: str) -> None:
    with store._lock:
        store._connection.execute(
            """INSERT INTO monitor_samples(
                observed_at, lifecycle, queue_size, receive_process_lag, pending_actions,
                coalesced, dropped, rejected, healthy, payload_json
            ) VALUES(?, ?, 0, 0, 0, 0, 0, 0, 1, '{}')""",
            (at.isoformat(), lifecycle),
        )
        store._connection.commit()


def _insert_run(store: MonitorStore, at: datetime, path: str, latency: float, tokens: dict) -> None:
    with store._lock:
        store._connection.execute(
            """INSERT INTO monitor_runs(
                run_id, started_at, updated_at, status, path, latency_ms,
                prompt_tokens, completion_tokens, total_tokens, cache_hit_tokens, cache_miss_tokens,
                revisions, stages_json
            ) VALUES(?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                f"run-{path}-{latency}", at.isoformat(), at.isoformat(), path, latency,
                tokens["prompt"], tokens["completion"], tokens["prompt"] + tokens["completion"],
                tokens["hit"], tokens["miss"], json.dumps([{"stage": "generate", "latency_ms": latency}]),
            ),
        )
        store._connection.commit()


def test_daily_report_aggregates_uptime_paths_tokens_and_diary(tmp_path) -> None:
    store = MonitorStore(tmp_path / "observability.sqlite3")
    start, _ = day_bounds(DAY, "Asia/Shanghai")
    _insert_sample(store, start + timedelta(minutes=1), "RUNNING")
    _insert_sample(store, start + timedelta(minutes=1, seconds=10), "RUNNING")
    _insert_run(store, start + timedelta(hours=1), "fast", 20, {"prompt": 10, "completion": 5, "hit": 6, "miss": 4})
    _insert_run(store, start + timedelta(hours=2), "deep", 80, {"prompt": 20, "completion": 8, "hit": 2, "miss": 18})
    store.append_resources([ResourceSample(
        observed_at=(start + timedelta(hours=1)).isoformat(), role="runtime", pid=1,
        cpu_percent=3.0, rss_bytes=8_000_000, num_threads=4, host_cpu_percent=10.0,
        host_mem_percent=30.0, available=True,
    )])
    diary = DiaryStore(tmp_path / "diary.sqlite3")
    original = DiaryOriginal(
        entry_id="diary-entry:one", decision_id="diary-decision:one",
        text="把今天记下。", written_at=start + timedelta(hours=14),
        body_hash=__import__("hashlib").sha256("把今天记下。".encode()).hexdigest(),
        source_event_id="evt-1",
    )
    diary.put(original, day=DAY, revision=1)

    class Homeo:
        async def read_traces(self, **kwargs):
            return [{
                "occurred_at": (start + timedelta(hours=3)).isoformat(),
                "values_before": {"energy_reserve": 0.5},
                "values_after": {"energy_reserve": 0.4},
                "salience": "noticeable",
                "dominant_needs": ["energy_reserve"],
                "episode_id": "ep-1",
                "affect_valence": 0.1, "affect_arousal": 0.2, "affect_dominance": 0.3,
            }]

    builder = DailyReportBuilder(
        monitor=store, state_dir=tmp_path, settings=DailyReportSettings(),
        homeostasis=Homeo(), diary_store=diary,
        runtime_status=lambda: SimpleNamespace(lifecycle_since=datetime.now(UTC) - timedelta(hours=2)),
    )
    import asyncio
    report = asyncio.run(builder.build(DAY, finalized=False))
    assert report["finalized"] is False
    assert report["pipelines"]["paths"]["fast"] == 1
    assert report["pipelines"]["paths"]["deep"] == 1
    assert report["tokens"]["combined"]["hit_rate"] > 0
    assert report["diary"]["wrote"] is True
    assert report["homeostasis"]["available"] is True
    assert report["resources"]["available"] is True
    archive = DailyReportStore(tmp_path / "reports")
    report["finalized"] = True
    archive.save(report)
    assert archive.load(DAY)["diary"]["wrote"] is True
    store.close()
    diary.close()
