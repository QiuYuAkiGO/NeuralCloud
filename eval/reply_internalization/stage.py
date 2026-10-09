"""Three-way apply ONLY this feature to a separately frozen production release."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


FILES = (
    "harness/core/expression.py", "harness/core/inference.py",
    "harness/compiler/expression.py", "harness/compiler/compiler.py",
    "harness/pipeline/pipeline.py", "harness/qq/decision.py", "harness/qq/policy.py",
    "harness/qq/prepared_turn.py", "harness/qq/shared.py", "harness/qq/vision_tool.py",
    "harness/qq/tool_evidence.py",
    "harness/validator/semantic.py", "harness/validator/validator.py",
)


def normalized(path):
    return Path(path).read_text(encoding="utf-8").replace("\r\n", "\n").encode()


def stage(local_base, production_base, workspace, output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("candidate_exists_use_new_directory")
    shutil.copytree(production_base, output, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    records, conflicts = {}, []
    scratch = output / ".expression-merge"
    scratch.mkdir()
    for name in FILES:
        before, online, after = (Path(root) / name for root in (local_base, production_base, workspace))
        ours = normalized(after)
        base = normalized(before) if before.exists() else None
        theirs = normalized(online) if online.exists() else None
        if ours == base:
            continue
        if theirs == base or theirs == ours:
            merged = ours
        elif base is None:
            raise ValueError("new_file_already_exists_in_production: " + name)
        else:
            paths = [scratch / leaf for leaf in ("online", "base", "feature")]
            for path, data in zip(paths, (theirs or b"", base, ours), strict=True):
                path.write_bytes(data)
            result = subprocess.run(["git", "merge-file", "-p", "--diff3", *map(str, paths)], capture_output=True)
            if result.returncode:
                conflicts.append(name)
                conflict = scratch / (name.replace("/", "_") + ".conflict")
                conflict.write_bytes(result.stdout)
                continue
            merged = result.stdout
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(merged)
        records[name] = {"local_before": hashlib.sha256(base).hexdigest() if base else None,
                         "production_before": hashlib.sha256(theirs).hexdigest() if theirs else None,
                         "candidate": hashlib.sha256(merged).hexdigest()}
    manifest = {"production_base": str(Path(production_base).resolve()), "files": records, "conflicts": conflicts}
    (output / "expression-stage.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if conflicts:
        raise ValueError("merge_conflicts_require_review: " + ", ".join(conflicts))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("local-base", "production-base", "workspace", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    print(json.dumps(stage(**{k: v for k, v in vars(parser.parse_args()).items()}), indent=2))
