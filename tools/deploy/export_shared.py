"""Generate a private source inventory for the Windows shared launch profile.

Example (no services are stopped or copied by inventory):
  python tools/deploy/export_shared.py inventory --output sources.json
  python -m harness.deploy.snapshot backup --sources-manifest sources.json \
      --output snapshot --writers-stopped --desktop-status stopped

Pass a previously exported WSL Board directory with:
  --extra-source board-state=C:/migration/board=state/board
The generated inventory contains private file paths, but never token contents.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from harness.deploy.snapshot import (  # noqa: E402
    SnapshotError, SourceSpec, _BINDING_PATHS, build_sources_manifest, discover_shared_sources,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inventory",))
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--launch", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--linux-config-root", default="/etc/neuralcloud")
    parser.add_argument("--linux-state-root", default="/var/lib/neuralcloud")
    parser.add_argument("--extra-source", action="append", default=[], metavar="ROLE=SOURCE=TARGET")
    args = parser.parse_args(argv)
    try:
        repo = args.repo.absolute()
        launch = args.launch or repo / "launch.json"
        raw = json.loads(launch.read_text(encoding="utf-8-sig"))
        profile = raw["profiles"][args.profile or raw.get("profile", "shared")]

        def expand(value: str) -> str:
            expanded = os.path.expandvars(os.path.expanduser(value))
            if "%" in expanded or "$" in expanded:
                raise SnapshotError("unexpanded source environment variable; run inventory on the source host")
            path = Path(expanded)
            return str(path.absolute() if path.is_absolute() else (repo / path).absolute())

        bindings = {key: expand(str(value)) for key, value in profile.get("bindings", {}).items()
                    if key in _BINDING_PATHS and value is not None}
        state_root = Path(expand(profile.get("state_root") or "data"))
        bindings.setdefault("NC_COGNITIVE_STATE_PATH", str(state_root / "cognitive/state.json"))
        bindings.setdefault("NC_RUNTIME_STATE_DIR", str(state_root / "runtime"))
        bindings.setdefault("NC_HOMEOSTASIS_STATE_DIR", str(state_root / "homeostasis"))
        bindings["NC_RUNTIME_MANIFEST_PATH"] = expand(profile["manifest_path"])
        bindings["NC_HOMEOSTASIS_PROFILE_PATH"] = expand(profile["profile_path"])
        extras = []
        for value in args.extra_source:
            parts = value.split("=", 2)
            if len(parts) != 3:
                raise SnapshotError("--extra-source must be ROLE=SOURCE=TARGET")
            extras.append(SourceSpec(parts[0], expand(parts[1]), parts[2]))
        sources = discover_shared_sources(repo, bindings, extras=extras)
        result = build_sources_manifest(sources, config_root=args.linux_config_root, state_root=args.linux_state_root)
        output = args.output.absolute()
        if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
            raise SnapshotError("inventory output must not traverse symlinks")
        output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation protects an existing operator-reviewed manifest.
        descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(json.dumps({"ok": True, "sources": len(sources), "output": str(output)}, ensure_ascii=False))
        return 0
    except (SnapshotError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"inventory error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
