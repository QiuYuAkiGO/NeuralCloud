"""Report fresh-process startup and bounded queries without exposing memory text.

OS page caches are deliberately not dropped. Measurements are fresh process /
filesystem-cache-unspecified, not a claim of physical cold-cache performance.
Run only on an isolated/staging database (it acquires the normal writer lease).
"""

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time


def worker(database, character):
    import psutil

    process = psutil.Process()
    before = process.io_counters()
    started, cpu = time.perf_counter(), time.process_time()
    from neuralcloud.storage import MemoryStore

    imported = time.perf_counter()
    store = MemoryStore(database, character)
    opened = time.perf_counter()
    store.health()
    ready = time.perf_counter()
    after = process.io_counters()
    ready_cpu = time.process_time() - cpu
    ready_memory = process.memory_info()
    queries = []
    try:
        for _ in range(10):
            tick = time.perf_counter()
            store.recall("星空", 10)
            queries.append((time.perf_counter() - tick) * 1000)
        count = store.db.execute("SELECT count(*) FROM records").fetchone()[0]
        return {
            "records": count,
            "import_ms": (imported - started) * 1000,
            "open_ms": (opened - imported) * 1000,
            "ready_ms": (ready - started) * 1000,
            "ready_cpu_ms": ready_cpu * 1000,
            "rss_at_ready_bytes": ready_memory.rss,
            "io_read_bytes_to_ready": after.read_bytes - before.read_bytes,
            "io_read_operations_to_ready": after.read_count - before.read_count,
            "query_first_ms": queries[0],
            "query_warm_median_ms": statistics.median(queries[1:]),
            "capabilities": store.health()["capabilities"],
            "external_model_calls": 0,
            "cold_store_reads": 0,
        }
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--character", required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.database, args.character)))
        return
    if not 1 <= args.runs <= 20:
        parser.error("runs must be between 1 and 20")
    measurements = []
    for _ in range(args.runs):
        output = subprocess.run(
            [sys.executable, __file__, "--worker", "--database", str(args.database), "--character", args.character],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        measurements.append(json.loads(output.stdout))
    print(
        json.dumps(
            {
                "version": 1,
                "platform": sys.platform,
                "python": sys.version.split()[0],
                "cache_regime": "fresh process; OS/filesystem caches unspecified; no cache drop",
                "measurements": measurements,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
