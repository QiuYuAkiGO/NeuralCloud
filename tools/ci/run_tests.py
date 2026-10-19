"""Route Windows launcher contracts to Windows without dropping their release gate."""
import argparse
from pathlib import Path
import subprocess
import sys

# These modules execute native Windows launchers/ACL or WSL entrypoints. Merely
# finding pwsh on Ubuntu does not make those operating-system contracts available.
WINDOWS = (
    "test_web_launcher.py", "test_qq_session_launcher.py", "test_start_recovery.py",
    "test_qq_direct_launcher.py", "test_start_script.py", "test_docker_desktop_recovery.py",
    "test_launcher_recovery.py", "test_shared_start_tokens.py", "test_qq_acceptance_launcher.py",
    "test_qq_watcher_launcher.py", "test_shared_launcher.py", "test_voice_launcher.py",
    "test_oneclick_launcher.py", "test_shared_launch_bindings.py", "test_shared_start_mode.py",
    "test_dev_launcher.py",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=["core", "windows"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    files = ["tests/" + name for name in WINDOWS]
    for name in files:
        if not (root / name).is_file():
            parser.error(f"test routing contains a missing module: {name}")
    if args.suite == "windows" and sys.platform != "win32":
        parser.error("the windows suite requires a Windows runner")
    selection = (["--ignore=" + name for name in files] if args.suite == "core"
                 else [*files, "tests/test_standalone_release.py"])
    return subprocess.call([sys.executable, "-m", "pytest", "-q", *selection], cwd=root)


if __name__ == "__main__":
    sys.exit(main())
