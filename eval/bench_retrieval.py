"""Measure hybrid retrieval latency. Report only — no hard gate."""

from __future__ import annotations

import argparse
import statistics
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from harness.config import Settings
from harness.core.models import InteractionEvent, RetrievalQuery
from harness.llm.embeddings import MockEmbedding
from harness.memory.service import build_inmemory_service
from tests.factories import vaelith

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)


def _populate(service, n: int) -> None:
    base = datetime(2026, 1, 1, tzinfo=TZ)
    for i in range(n):
        when = base + timedelta(hours=i)
        text = f"日常事件编号 {i} 天气与文件整理" if i % 17 else f"用户提到猫和承诺 {i}"
        service.record_event(
            InteractionEvent(
                event_id=f"evt_bench_{i:05d}",
                timestamp=when,
                speaker="user",
                raw_text=text,
                session_id="bench",
                evidence=[f"bench:{i}"],
            ),
            importance=0.3 if i % 17 else 0.8,
            curate=False,
            generate_insight=False,
        )


def run_bench(n: int = 10_000, repeats: int = 30) -> dict[str, float]:
    settings = Settings(embedding_dim=1024)
    service = build_inmemory_service(settings=settings, embeddings=MockEmbedding(dim=1024))
    service.bootstrap_persona(vaelith())
    _populate(service, n)
    query = RetrievalQuery(text="你昨天不是说不会再管我了吗？猫怎么样了", now=NOW)
    # warmup
    service.query(query)
    times_ms: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter()
        service.query(query)
        times_ms.append((time.perf_counter() - start) * 1000.0)
    times_ms.sort()
    p95_index = min(len(times_ms) - 1, int(round(0.95 * (len(times_ms) - 1))))
    return {
        "n": float(n),
        "repeats": float(repeats),
        "p50_ms": statistics.median(times_ms),
        "p95_ms": times_ms[p95_index],
        "mean_ms": statistics.mean(times_ms),
        "min_ms": min(times_ms),
        "max_ms": max(times_ms),
    }


def write_report(stats: dict[str, float], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    budget = "02-inference-flow.md §4 Harness/RAG 预算 30–100ms（参考，非门禁）"
    path.write_text(
        "\n".join(
            [
                "# Hybrid retrieval latency baseline",
                "",
                f"- corpus size: {int(stats['n'])} episodic events (in-memory, mock embeddings)",
                f"- repeats: {int(stats['repeats'])}",
                f"- P50: {stats['p50_ms']:.2f} ms",
                f"- P95: {stats['p95_ms']:.2f} ms",
                f"- mean: {stats['mean_ms']:.2f} ms",
                f"- min/max: {stats['min_ms']:.2f} / {stats['max_ms']:.2f} ms",
                f"- reference budget: {budget}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10_000)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--out", type=Path, default=Path("eval/reports/retrieval_baseline.md"))
    args = parser.parse_args()
    stats = run_bench(n=args.n, repeats=args.repeats)
    write_report(stats, args.out)
    print(args.out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
