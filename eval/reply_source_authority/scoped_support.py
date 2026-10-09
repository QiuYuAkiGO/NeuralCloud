"""Build an isolated source prototype; change one source-role invariant, no model text."""

import argparse
from pathlib import Path
import shutil

from eval.qq_brief.common import file_hash, save, source_hashes

OLD = '''if any(i < 0 or i >= evidence_count or evidence.entries[i].status != "user_claim"
                           or evidence.entries[i].audience != response_semantics.audience
                           or evidence.entries[i].source_conversation_id != response_semantics.audience
                           for i in issue.evidence_rows):'''
NEW = '''# project_semantics already binds trigger rows to original user claims.
                    # Supporting rows may be admitted image/tool/history context;
                    # they do not replace the mandatory trigger intersection below.
                    if any(i < 0 or i >= evidence_count
                           or evidence.entries[i].audience != response_semantics.audience
                           for i in issue.evidence_rows):'''


def prepare(source, target):
    path = source / "harness/validator/semantic.py"
    original = path.read_text(encoding="utf-8")
    if original.count(OLD) != 1:
        raise ValueError("frozen_alignment_guard_anchor_changed")
    target.mkdir(parents=True, exist_ok=False)
    for folder in ("harness", "infra", "gui", "presets", "contracts"):
        if (source / folder).exists():
            shutil.copytree(source / folder, target / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (target / "harness/validator/semantic.py").write_text(original.replace(OLD, NEW), encoding="utf-8")
    before, after = source_hashes(source), source_hashes(target)
    changed = [name for name in before if before[name] != after.get(name)]
    if changed != ["harness/validator/semantic.py"] or before.keys() != after.keys():
        raise ValueError("prototype_changed_unexpected_files")
    save(target / "prototype.json", {"changed": changed, "before": file_hash(path),
        "after": file_hash(target / changed[0]), "purpose": "Allow admitted supporting context while requiring the original trigger and same audience.",
        "runtime_enabled": False, "source": str(source.resolve())})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--target", type=Path, required=True)
    args = p.parse_args()
    prepare(args.source, args.target)
    print(args.target)


if __name__ == "__main__":
    main()
