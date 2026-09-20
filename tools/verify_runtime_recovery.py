"""Compare strict and checkpoint recovery on an isolated journal copy.

This tool never creates domain clients or executes actions. The input journal
is only read; all repair/recovery writes happen in a temporary directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from harness.research.manifest import load_experiment_manifest
from harness.runtime.journal import RuntimeJournal
from harness.runtime.recovery import RecoveryStore, encode


def digest(store):
    result = {"state": hashlib.sha256(encode(store.projector.state).encode()).hexdigest()}
    for table, order in (
        ("kv", "namespace,key"),
        ("locations", "seq"),
        ("catalog", "seq"),
        ("events", "id"),
        ("ranges", "type,source,first"),
        ("actions", "id"),
        ("commands", "id"),
    ):
        value = hashlib.sha256()
        for row in store.rows(f"SELECT * FROM {table} ORDER BY {order}"):
            value.update((encode(row) + "\n").encode())
        result[table] = value.hexdigest()
    return result


def verify(source, *, manifest_hash, tail=1000):
    paths = sorted(source.glob("runtime-*.jsonl")) if source.is_dir() else [source]
    if not paths:
        raise ValueError("journal contains no segments")
    count, character = 0, None
    for path in paths:
        with path.open("rb") as handle:
            for line in handle:
                if line.endswith(b"\n") or line.endswith(b"\r"):
                    count += 1
                    if character is None:
                        character = json.loads(line)["character_id"]
    cut = max(1, count - tail)
    if count < 2:
        raise ValueError("at least two records are required to compare incremental recovery")

    class CutStore(RecoveryStore):
        def observe(self, record, *args, **kwargs):
            super().observe(record, *args, **kwargs)
            if record.record_seq == cut:
                self.commit()

    with tempfile.TemporaryDirectory(prefix="misty-recovery-verification-") as directory:
        root = Path(directory)
        journal_dir = root / "journal"
        journal_dir.mkdir()
        for index, path in enumerate(paths, 1):
            shutil.copyfile(path, journal_dir / f"runtime-{index:06d}.jsonl")
        identity = {"character": character, "manifest": manifest_hash}
        strict = CutStore(root / "recovery.sqlite3", identity=identity)
        journal = RuntimeJournal(
            journal_dir,
            runtime_instance_id="verification",
            character_id=character,
            manifest_revision=1,
            recovery_store=strict,
            fsync=False,
        )
        expected, last_hash = digest(strict), journal.last_hash
        strict.close()
        fast = RecoveryStore(root / "recovery.sqlite3", identity=identity)
        recovered = RuntimeJournal(
            journal_dir,
            runtime_instance_id="verification",
            character_id=character,
            manifest_revision=1,
            recovery_store=fast,
            recovery_mode="fast",
            fsync=False,
        )
        actual = digest(fast)
        report = dict(
            records=count,
            checkpoint_seq=cut,
            replayed_records=recovered.recovered_records,
            mode=fast.mode,
            equal=expected == actual and recovered.last_hash == last_hash,
            mismatched_projections=[key for key in expected if expected[key] != actual[key]],
        )
        fast.close()
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tail", type=int, default=1000)
    args = parser.parse_args()
    if args.tail < 1:
        parser.error("--tail must be positive")
    report = verify(args.journal, manifest_hash=load_experiment_manifest(args.manifest).content_hash(), tail=args.tail)
    print(json.dumps(report, indent=2))
    if not report["equal"] or report["mode"] != "fast":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
