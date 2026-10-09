"""Verify the Git research record and restore its frozen runtime without network access."""

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import tarfile

from eval.qq_brief.common import file_hash, read


def restore(record, output):
    manifest = read(record / "archive-manifest.json")
    for name, expected in manifest["files"].items():
        path = (record / name).resolve()
        if not path.is_relative_to(record.resolve()) or file_hash(path) != expected:
            raise ValueError("research_record_hash_mismatch: " + name)
    expected = manifest["frozen_runtime_files"]
    with tarfile.open(record / "frozen-runtime-v6.tar.gz") as archive:
        members = archive.getmembers()
        if len(members) != len(expected) or {m.name for m in members} != set(expected):
            raise ValueError("unexpected_runtime_archive_members")
        contents = {}
        for member in members:
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe_runtime_archive_member")
            content = archive.extractfile(member).read()
            if hashlib.sha256(content).hexdigest() != expected[member.name]:
                raise ValueError("runtime_source_hash_mismatch")
            destination = (output / member.name).resolve()
            if not destination.is_relative_to(output.resolve()):
                raise ValueError("runtime_path_escapes_output")
            contents[destination] = content
    # Refuse an existing output, including a partial restoration. Never replace
    # a working checkout or silently mix source versions.
    output.mkdir(parents=True, exist_ok=False)
    for destination, content in contents.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return len(contents)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--record", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    count = restore(args.record, args.output)
    print({"verified_runtime_files": count, "output": str(args.output), "network_requests": 0})


if __name__ == "__main__":
    main()
