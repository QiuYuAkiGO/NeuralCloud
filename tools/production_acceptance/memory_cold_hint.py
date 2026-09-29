"""File-specific cold-cache hints on newly generated isolated synthetic stores.

Linux example, from the checkout:
  python -m tools.production_acceptance.memory_cold_hint --destination-dir /tmp/neuralcloud-isolated-acceptance/cold-100k --rows 100000
Cross-platform harness verification (simulated hint/I/O; not performance evidence):
  python -m tools.production_acceptance.memory_cold_hint --self-test --destination-dir NEW
Imports default to this checkout; --repo explicitly selects another checkout.

No input database option, live state, network, global drop_caches, or deletion.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import stat
import sys
import time


ALLOWED_ROOTS = (Path("/tmp/neuralcloud-isolated-acceptance"), Path("/var/tmp/neuralcloud-isolated-acceptance"))


class Rejected(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise Rejected(code)


def offline_guard(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.bind", "socket.sendto",
                 "subprocess.Popen", "os.system", "os.posix_spawn"}:
        raise Rejected("external_io_forbidden")


def no_links(path):
    require(not any(parent.is_symlink() for parent in (path, *path.parents)), "symlink_path_forbidden")


def read_bytes():
    for line in Path("/proc/self/io").read_text(encoding="ascii").splitlines():
        if line.startswith("read_bytes:"):
            return int(line.split(":", 1)[1].strip())
    raise Rejected("process_read_bytes_unavailable")


class SyntheticFiles:
    """Only files registered after this process creates its exclusive directory."""

    def __init__(self, directory, *, simulated=False):
        no_links(directory.absolute())
        self.directory = directory.resolve()
        require(not self.directory.exists(), "destination_must_be_new")
        if not simulated:
            require(any(self.directory.is_relative_to(root) and self.directory != root for root in ALLOWED_ROOTS),
                    "destination_outside_isolated_acceptance_roots")
        self.directory.mkdir(parents=True, mode=0o700)
        self.simulated = simulated
        self.registered = {}
        self.hint_calls = 0

    def register(self, path):
        no_links(Path(path).absolute())
        path = Path(path).resolve()
        require(path.is_relative_to(self.directory), "file_outside_generated_directory")
        relative = path.relative_to(self.directory)
        allowed = (relative.parts == ("synthetic.sqlite3",)
                   or (len(relative.parts) == 2 and relative.parts[0].startswith("synthetic.prepare-")
                       and relative.parts[1] in {"production.sqlite3", "shared.sqlite3"}))
        require(allowed, "file_not_a_generated_synthetic_database")
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "file_not_single_link_regular_database")
        if hasattr(os, "getuid"):
            require(info.st_uid == os.getuid(), "file_owned_by_another_user")
        with path.open("rb") as stream:
            require(stream.read(16) == b"SQLite format 3\x00", "file_not_sqlite")
        identity = (info.st_dev, info.st_ino)
        require(path not in self.registered or self.registered[path] == identity, "registered_file_replaced")
        self.registered[path] = identity

    def hint(self, paths):
        started = time.perf_counter()
        total = 0
        for path in paths:
            no_links(Path(path).absolute())
            path = Path(path).resolve()
            require(path in self.registered, "unregistered_file")
            no_links(path)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(descriptor)
                require((info.st_dev, info.st_ino) == self.registered[path], "registered_file_replaced")
                require(info.st_nlink == 1 and stat.S_ISREG(info.st_mode), "file_link_or_type_changed")
                if not self.simulated:
                    # Ensure our own generated dirty pages can be considered
                    # by the advisory hint. This affects no other file.
                    os.fsync(descriptor)
                    os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
                total += info.st_size
                self.hint_calls += 1
            finally:
                os.close(descriptor)
        return {"file_count": len(paths), "file_bytes": total,
                "hint_ms": round((time.perf_counter() - started) * 1000, 3)}


def run(args, runner):
    from tools.benchmark_production_memory import benchmark, _production_preparation, _distribution
    from harness.memory.production import ProductionMemoryStore

    generated = SyntheticFiles(args.destination_dir, simulated=args.self_test)
    database = generated.directory / "synthetic.sqlite3"
    rows = 30 if args.self_test else args.rows
    samples = 3 if args.self_test else args.samples
    # Generation and warm ingestion checks are deliberately outside the cold
    # sample window. Every sample then gets a new file-specific hint.
    benchmark(database, rows=rows, queries=1, context_prepare=False)
    generated.register(database)
    counter = [0]

    def io_read():
        if args.self_test:
            counter[0] += 4096
            return counter[0]
        return read_bytes()

    store = ProductionMemoryStore(database, owner_id="benchmark", preset_id="synthetic-v1")
    recall = []
    for index in range(samples):
        hinted = generated.hint([database])
        before = io_read()
        diagnostics = {}
        started = time.perf_counter()
        payload = store.recall(audience="local:main", query="synthetic" if index % 2 == 0 else "topic_1",
            now=datetime(2027, 1, 1, tzinfo=timezone.utc), diagnostics=diagnostics)
        elapsed = (time.perf_counter() - started) * 1000
        after = io_read()
        require(after >= before, "process_io_counter_decreased")
        recall.append({"sample": index + 1, "elapsed_ms": round(elapsed, 3), "process_read_bytes_delta": after - before,
                       "candidate_count": diagnostics["candidate_count"], "entry_count": len(payload.get("entries", [])), **hinted})

    pending, preparation = {}, []

    def before_prepare(copied, index):
        files = [copied]
        shared = copied.parent / "shared.sqlite3"
        if shared.exists():
            files.append(shared)
        for path in files:
            generated.register(path)
        pending[index] = {**generated.hint(files), "io_before": io_read()}

    def after_prepare(copied, index, sample):
        current = pending.pop(index)
        delta = io_read() - current.pop("io_before")
        require(delta >= 0, "process_io_counter_decreased")
        # The real helper keeps five initialization samples. They also receive
        # hints, but are excluded so application initialization is separate.
        if index >= 5:
            preparation.append({"sample": index - 4, "elapsed_ms": round(sample["elapsed_ms"], 3),
                "process_read_bytes_delta": delta, "memory_entries": sample["memory_entries"],
                "message_characters": sample["message_characters"], **current})

    result = runner.run(_production_preparation(database, queries=samples,
        before_prepare=before_prepare, after_prepare=after_prepare))
    guard_checks = {}
    if args.self_test:
        for label, bad in {"unregistered": generated.directory / "unregistered.sqlite3",
                           "live_path": Path("/var/lib/neuralcloud/live.sqlite3")}.items():
            try:
                generated.hint([bad])
            except Rejected:
                guard_checks[label + "_rejected"] = True
            else:
                raise Rejected("self_test_path_guard_failed")
        # Reject a synthetic-looking path that traverses out of our fresh
        # directory before opening or advising any file.
        try:
            generated.register(generated.directory / ".." / "synthetic.sqlite3")
        except Rejected:
            guard_checks["path_traversal_rejected"] = True
        else:
            raise Rejected("self_test_path_traversal_guard_failed")
        require(all(row["process_read_bytes_delta"] == 4096 for row in recall + preparation), "self_test_io_deltas_failed")
    report = {"schema_version": "production-memory-cold-hint-v1", "status": "simulated_self_test" if args.self_test else "complete",
        "scope": "isolated_acceptance_synthetic_files_only", "synthetic_only": True, "rows": rows,
        "platform": platform.system(), "hint": "simulated" if args.self_test else "POSIX_FADV_DONTNEED_after_fsync",
        "cache_claim": "advisory_hint_only_not_guaranteed_cold_cache", "system_drop_caches": False,
        "live_files_accessed": False, "model_calls": 0, "network_calls": 0,
        "io_measurement": "simulated_counter" if args.self_test else "/proc/self/io read_bytes; process-wide actual storage reads",
        "io_interpretation": "positive_delta_proves_process_storage_reads_not_total_page_eviction; zero_delta_does_not_prove_coldness",
        "cold_hint_recall": {"timing_ms": _distribution([item["elapsed_ms"] for item in recall]), "samples": recall},
        "cold_hint_production_prepare": {"path": result["path"], "includes": result["includes"],
            "excludes": result["excludes"], "initialization_samples_excluded": 5,
            "timing_ms": _distribution([item["elapsed_ms"] for item in preparation]), "samples": preparation},
        "hint_calls": generated.hint_calls, "generated_file_count": len(generated.registered),
        "output_directory": str(generated.directory), "self_test_guards": guard_checks,
        "limits": ["application_and_python_objects_remain_loaded", "process_imports_and_non_database_caches_not_evicted",
                   "filesystem_or_kernel_may_ignore_hint", "not_full_QQ_end_to_end_latency"]}
    with (generated.directory / "cold-hint-report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination-dir", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--rows", type=int, choices=(1000, 10000, 100000, 1000000), default=100000)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        require(1 <= args.samples <= 1000, "invalid_sample_count")
        supported = (sys.platform == "linux" and hasattr(os, "posix_fadvise")
                     and hasattr(os, "POSIX_FADV_DONTNEED") and Path("/proc/self/io").is_file())
        if not supported and not args.self_test:
            print(json.dumps({"schema_version": "production-memory-cold-hint-v1", "status": "unsupported",
                              "platform": platform.system(), "files_created": False, "live_files_accessed": False}))
            return 0
        require((args.repo / "tools/benchmark_production_memory.py").is_file(), "repository_path_invalid")
        sys.path.insert(0, str(args.repo.resolve()))
        # Windows initializes a local wake-up socket pair for its event loop.
        # Initialize only that loop before forbidding later network activity.
        with asyncio.Runner() as runner:
            sys.addaudithook(offline_guard)
            print(json.dumps(run(args, runner), ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"schema_version": "production-memory-cold-hint-v1", "status": "failed",
            "error_type": type(error).__name__, "error_code": str(error) if type(error) is Rejected else "details_suppressed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
