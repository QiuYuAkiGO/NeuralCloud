"""Exercise the actual ZIP entrypoints twice with isolated demo services and no model calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def main():
    archive = Path(sys.argv[1]).resolve()
    destination = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "NeuralCloud smoke 中文"
    destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        package.extractall(destination)
    root = destination / "NeuralCloud"
    manifest = json.loads((root / "RELEASE-MANIFEST.json").read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
    command = (["cmd.exe", "/d", "/c", str(root / "deploy.bat"), "--smoke-test"] if os.name == "nt"
               else ["bash", str(root / "deploy.sh"), "--smoke-test"])
    env = dict(os.environ, NC_DEPLOY_NO_PAUSE="1")
    for attempt in range(2):
        subprocess.run(command, cwd=destination.parent, env=env, check=True, timeout=600)
        if attempt == 0:
            config = (root / "standalone.json").read_bytes()
            sentinel = root / "standalone-data/user-data.txt"
            sentinel.write_text("keep this user data", encoding="utf-8")
        else:
            assert (root / "standalone.json").read_bytes() == config
            assert sentinel.read_text(encoding="utf-8") == "keep this user data"
    # A bad user config must fail without modifying it or waiting for keyboard input.
    (root / "standalone.json").write_text('{"mode":"live","ports":{}}', encoding="utf-8")
    try:
        failed = subprocess.run(command, cwd=destination.parent, env=env, timeout=30)
        assert failed.returncode != 0
    finally:
        (root / "standalone.json").write_bytes(config)
    print("ZIP smoke verified: real four-process startup twice, Unicode/space paths, foreign CWD, data preservation, failure exit.")


if __name__ == "__main__":
    main()
