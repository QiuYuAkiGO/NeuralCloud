"""Online, project-local bootstrap for the standalone demo distribution (stdlib only)."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import venv

ROOT = Path(__file__).resolve().parents[2]
UV_VERSION = "0.12.10"
MODULES = {"cognitive": "harness.cognitive.service", "homeostasis": "harness.homeostasis.service",
           "runtime": "harness.runtime.service", "gui": "gui.runtime_server"}
DEFAULT = {"mode": "demo", "ports": {"gui": 18890, "runtime": 18870, "cognitive": 18871, "homeostasis": 18872}}


def python_in(path):
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def read_config(root):
    path = root / "standalone.json"
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(DEFAULT, handle, indent=2)
    except FileExistsError:
        pass
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or set(config) != {"mode", "ports"} or config["mode"] != "demo":
        raise ValueError("standalone.json supports mode=demo only. Live models require separate explicit configuration; see docs/STANDALONE_RELEASE.md.")
    ports = config["ports"]
    if (not isinstance(ports, dict) or set(ports) != set(MODULES)
            or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports.values())
            or len(set(ports.values())) != 4):
        raise ValueError("standalone.json needs four distinct integer ports from 1024 to 65535")
    return config


@contextlib.contextmanager
def launch_lock(root):
    with (root / ".standalone.lock").open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("This package is already running or installing. Use its existing console; stop it with Ctrl+C before restarting.") from None
        yield


def prepare(root, log):
    bootstrap = root / ".bootstrap"
    python = python_in(bootstrap)
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(bootstrap)
    environment = dict(os.environ, UV_CACHE_DIR=str(root / ".uv-cache"), UV_PROJECT_ENVIRONMENT=str(root / ".venv"))
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    print("Preparing locked dependencies online; first launch requires PyPI access. See logs/install.log.", flush=True)
    with log.open("a", encoding="utf-8") as output:
        for command in ([str(python), "-m", "pip", "install", f"uv=={UV_VERSION}"],
                        [str(python), "-m", "uv", "sync", "--locked", "--python", sys.executable,
                         "--extra", "gui", "--extra", "qq", "--no-dev"]):
            result = subprocess.run(command, cwd=root, env=environment, stdout=output, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(f"Dependency installation failed ({result.returncode}). Check network/PyPI access and {log}; retry the same launcher.")
    return python_in(root / ".venv")


def environment(root, ports):
    # Never inherit production bindings or provider credentials from the host.
    env = {k: v for k, v in os.environ.items() if not k.startswith("NC_") and k not in {"PYTHONPATH", "PYTHONHOME"}}
    state = root / "standalone-data"
    state.mkdir(exist_ok=True)
    env.update({"NC_MODE": "demo", "NC_CONTROL_MODE": "development_debug", "NC_ISOLATED_DEVELOPMENT": "1",
                "NC_EMBODIMENT_PROVIDER": "none", "NC_SERVICE_TOKEN": secrets.token_hex(32),
                "NC_CONTROL_TOKEN": secrets.token_hex(32), "PYTHONUNBUFFERED": "1",
                "XDG_STATE_HOME": str(state / "os-state"), "LOCALAPPDATA": str(state / "os-state"),
                "NC_RUNTIME_MANIFEST_PATH": str(root / "presets/runtime/development-demo.json"),
                "NC_COGNITIVE_MANIFEST_PATH": str(root / "presets/runtime/development-demo.json"),
                "NC_HOMEOSTASIS_PROFILE_PATH": str(root / "presets/homeostasis/functional-proxy-v1.json")})
    for key, relative in {"NC_GUI_CONFIG": "gui_settings.json", "NC_GUI_PRESETS": "presets",
                          "NC_COGNITIVE_STATE_PATH": "cognitive/state.json", "NC_HOMEOSTASIS_STATE_DIR": "homeostasis",
                          "NC_RUNTIME_STATE_DIR": "runtime", "NC_TRACE_DIR": "traces", "NC_CALL_LOG_DIR": "calls",
                          "NC_PROFILE_REVISION_DIR": "homeostasis/profiles", "NC_MANIFEST_REVISION_DIR": "runtime/manifests",
                          "NC_PENDING_START_PATH": "runtime/pending-start.json"}.items():
        env[key] = str(state / relative)
    for name, port in ports.items():
        env[f"NC_{name.upper()}_HOST"] = "127.0.0.1"
        env[f"NC_{name.upper()}_PORT"] = str(port)
        env[f"NC_{name.upper()}_URL"] = f"http://127.0.0.1:{port}"
    return env


def serve(root, python, config, smoke):
    ports = config["ports"]
    for port in ports.values():
        with socket.socket() as probe:
            if os.name != "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            else:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"Port {port} is occupied. Stop the other instance or edit standalone.json; no existing process was stopped.") from None
    env = environment(root, ports)
    children, logs = [], []
    # Ignore system HTTP proxies for local health checks.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        for name, module in MODULES.items():
            log = (root / "logs" / f"standalone-{name}.log").open("a", encoding="utf-8")
            logs.append(log)
            child = subprocess.Popen([str(python), "-m", module], cwd=root, env=env,
                                     stdout=log, stderr=subprocess.STDOUT)
            children.append(child)
            path = "/api/status" if name == "gui" else "/v1/status" if name == "runtime" else "/v1/health"
            token = env["NC_CONTROL_TOKEN" if name == "runtime" else "NC_SERVICE_TOKEN"]
            deadline = time.monotonic() + 90
            while True:
                if any(p.poll() is not None for p in children):
                    raise RuntimeError(f"Service exited during {name} startup; inspect logs/standalone-*.log")
                try:
                    request = urllib.request.Request(f"http://127.0.0.1:{ports[name]}{path}", headers={"Authorization": f"Bearer {token}"})
                    with opener.open(request, timeout=2) as response:
                        body = json.load(response)
                    ready = (body.get("runtime") is not None if name == "gui" else
                             str(body.get("lifecycle", "")).lower() not in {"", "booting", "recovering", "faulted"}
                             if name == "runtime" else body.get("healthy") is True)
                    if ready:
                        break
                except (OSError, ValueError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{name} readiness timed out; inspect logs/standalone-{name}.log")
                time.sleep(0.25)
        print(f"READY: http://127.0.0.1:{ports['gui']} (standalone demo, four real services)", flush=True)
        print("No model key needed in demo. MistyAI/QQ are disabled. See docs/STANDALONE_RELEASE.md for live configuration boundaries.", flush=True)
        if smoke:
            print("SMOKE PASSED: authenticated domain health, runtime status and composite GUI status.", flush=True)
            return
        print("Keep this console open. Press Ctrl+C to stop these services; your config and data are retained.", flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(0.5)
        raise RuntimeError("A service exited; inspect logs/standalone-*.log")
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for log in logs:
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-test", action="store_true", help="start real services, check HTTP readiness, then stop")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.exit(2, "Install Python 3.12 (including venv/pip) and add it to PATH, then retry. This distribution is validated on Python 3.12.\n")
    try:
        with launch_lock(ROOT):
            config = read_config(ROOT)
            (ROOT / "logs").mkdir(exist_ok=True)
            python = prepare(ROOT, ROOT / "logs/install.log")
            if os.name != "nt":
                signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            serve(ROOT, python, config, args.smoke_test)
    except KeyboardInterrupt:
        print("Stopped. Configuration and data retained.")
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Launch failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
