from __future__ import annotations

from eval.bench_retrieval import run_bench


def test_retrieval_latency_small_corpus() -> None:
    stats = run_bench(n=200, repeats=5)
    assert stats["p95_ms"] > 0
    # sanity: even a tiny corpus should finish well under a second
    assert stats["p95_ms"] < 1000
