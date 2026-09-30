"""Fail closed on an unspecified/dirty production body release; emit provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def revision(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("MISTYAI_PRODUCTION_REVISION must name a published full 40-character commit SHA")
    return value


def production_contracts(root):
    schemas = root / "contracts" / "schemas"
    required = ("body-state", "body-state-capture-response", "body-rest-command", "body-rest-receipt", "body-rest-status",
                "production-cognitive-usage", "production-cognitive-usage-receipt")
    result = {}
    for name in required:
        path = schemas / f"{name}.schema.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        if name == "body-state":
            properties = value.get("properties", {})
            if (not {"physiology_mode", "episode_id", "sample_sequence", "resting"}.issubset(properties)
                    or "production_v1" not in json.dumps(properties["physiology_mode"])):
                raise ValueError("MistyAI revision lacks the production body contract")
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        result[str(path.relative_to(root)).replace("\\", "/")] = hashlib.sha256(canonical).hexdigest()
    return result


def git(root, *arguments):
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()


def attest(root, body, expected):
    if git(body, "rev-parse", "HEAD") != revision(expected):
        raise ValueError("MistyAI checkout does not match the frozen revision")
    if git(body, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("MistyAI checkout has unpublished changes; commit and publish the paired release first")
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("NeuralCloud checkout has unpublished changes; release evidence requires an exact checkout")
    contracts = production_contracts(body)
    frozen = json.loads((root / "contracts/production-body-schema-lock.json").read_text(encoding="utf-8"))
    if contracts != frozen["canonical_json_sha256"]:
        raise ValueError("MistyAI production contracts differ from the reviewed schema lock")
    return {"schema_version": "production-release-pair-v1", "neuralcloud_revision": git(root, "rev-parse", "HEAD"),
            "mistyai_revision": expected, "contracts": contracts,
            "lockfiles": {name: hashlib.sha256((path / "uv.lock").read_bytes()).hexdigest()
                          for name, path in (("neuralcloud", root), ("mistyai", body))}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default=os.environ.get("MISTYAI_PRODUCTION_REVISION", ""))
    parser.add_argument("--mistyai", type=Path)
    args = parser.parse_args()
    try:
        expected = revision(args.revision)
        print(json.dumps(attest(Path(__file__).resolve().parents[2], args.mistyai, expected), indent=2)
              if args.mistyai else expected)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"Production release pair unavailable: {exc}\n")


if __name__ == "__main__":
    main()
