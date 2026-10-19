"""Build a tracked-source-only, online standalone ZIP; never include runtime data."""
from pathlib import Path
import hashlib
import json
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
ROOTS = ("harness/", "gui/", "infra/", "presets/", "contracts/", "tools/release/")
FILES = {"pyproject.toml", "uv.lock", "README.md", "deploy.sh", "deploy.bat", "docs/STANDALONE_RELEASE.md"}


def build(root=ROOT):
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    names = sorted(n for n in tracked if n in FILES or n.startswith(ROOTS))
    if not FILES.issubset(names):
        raise ValueError("required distribution files must be tracked")
    blobs = {}
    for name in names:
        path = root / name
        if (path.is_symlink() or any(p in {"data", "logs", "__pycache__", ".venv"} for p in path.relative_to(root).parts[:-1])
                and not name.startswith("harness/llm/data/")):
            raise ValueError(f"unexpected source path: {name}")
        if path.name.startswith(".env") or path.suffix.lower() in {".token", ".key", ".pem", ".db", ".sqlite3", ".pyc"}:
            raise ValueError(f"private file in source allowlist: {name}")
        blobs[name] = path.read_bytes()
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    manifest = {"kind": "standalone-online", "revision": sha, "production_pair": False,
                "files": {n: hashlib.sha256(b).hexdigest() for n, b in blobs.items()}}
    output = root / "dist/neuralcloud-standalone.zip"
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, blob in blobs.items():
            archive.writestr("NeuralCloud/" + name, blob)
        archive.writestr("NeuralCloud/RELEASE-MANIFEST.json", json.dumps(manifest, indent=2))
    output.with_suffix(".zip.sha256").write_text(hashlib.sha256(output.read_bytes()).hexdigest() + "  " + output.name + "\n")
    print(output)
    return output


if __name__ == "__main__":
    build()
