"""Opt-in stopped-writer scheduled backups; failures never resume traffic."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from . import snapshot, systemd
from .config import CORE_SERVICES, DeployError, DeploymentConfig, ensure_linux, load_config

SCHEDULED_NAME = re.compile(r"scheduled-(\d{8}T\d{12}Z)\Z")
SERVICE_ORDER = ("cognitive", "homeostasis", "board", "runtime", "gui", "qq_direct")
STOP_ORDER = ("qq_direct", "gui", "runtime", "cognitive", "homeostasis", "board")
DEFAULT_PROXY = "neuralcloud-proxy.service"


def _private_root(path: Path) -> Path:
    absolute = path.absolute()
    snapshot._no_links(absolute)
    if absolute == absolute.parent:
        raise DeployError("backup root cannot be a filesystem root")
    absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
    absolute.chmod(0o700)
    return absolute.resolve(strict=True)


@contextmanager
def _job_lock(root: Path):
    path = root / ".scheduled-backup.lock"
    snapshot._no_links(path)
    handle = path.open("a+b")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b" ")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DeployError("another scheduled backup is running") from None
        yield
    finally:
        handle.close()


def _proxy_state(unit: str) -> dict[str, str]:
    # Restrict the optional selector to application-specific units, never SSH or
    # a shared system-wide Caddy instance serving unrelated applications.
    if not re.fullmatch(r"[a-zA-Z0-9_.@-]*neuralcloud[a-zA-Z0-9_.@-]*\.service", unit):
        raise DeployError("proxy unit must be specific to neuralcloud")
    response = systemd._systemctl("show", unit, "--property=LoadState,ActiveState,SubState,Result,MainPID", check=False)
    return dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)


def _state(name: str) -> dict[str, str]:
    return _proxy_state(name) if name.endswith(".service") else systemd.unit_status(name)


def _unit(name: str) -> str:
    return name if name.endswith(".service") else systemd.unit_name(name)


def running_scope(cfg: DeploymentConfig, proxy_unit: str | None,
                  extra_writer_units: tuple[str, ...] = ()) -> tuple[list[str], str | None]:
    if len(set(extra_writer_units)) != len(extra_writer_units) or any(
        unit == proxy_unit or unit in {systemd.unit_name(name) for name in SERVICE_ORDER}
        for unit in extra_writer_units
    ):
        raise DeployError("extra writer units must be distinct from managed core/optional/proxy units")
    states = {name: systemd.unit_status(name) for name in SERVICE_ORDER}
    if any(states[name].get("ActiveState") != "active" for name in CORE_SERVICES):
        return [], "core_not_running"
    try:
        runtime = systemd._health(cfg, "runtime")
        if runtime.get("lifecycle") != "RUNNING":
            return [], "runtime_not_running"
        if not all(runtime.get("health", {}).get(key) is True
                   for key in ("journal_writable", "cognitive_healthy", "homeostasis_healthy")):
            return [], "core_unhealthy"
        if any(systemd._health(cfg, name).get("healthy") is not True for name in ("cognitive", "homeostasis")):
            return [], "core_unhealthy"
        if systemd._health(cfg, "gui").get("runtime", {}).get("lifecycle") != "RUNNING":
            return [], "gui_not_ready"
    except (OSError, ValueError, DeployError):
        return [], "health_unavailable"
    scope = []
    for name, state in states.items():
        if state.get("ActiveState") in {"activating", "deactivating", "reloading"}:
            return [], "service_transition_in_progress"
        if state.get("ActiveState") == "active":
            if not systemd._managed_unit(state):
                raise DeployError("scheduled backup refuses an unmanaged core or optional unit")
            if name not in cfg.services:
                raise DeployError("a running optional service is missing from enabled_components")
            scope.append(name)
    for named_unit in ((*extra_writer_units, proxy_unit) if proxy_unit else extra_writer_units):
        state = _proxy_state(named_unit)
        if state.get("ActiveState") in {"activating", "deactivating", "reloading"}:
            return [], "external_unit_transition_in_progress"
        if state.get("ActiveState") == "active":
            scope.append(named_unit)
    return scope, None


def _stop_one(cfg: DeploymentConfig, name: str, *, pause_runtime: bool) -> None:
    if name == "runtime" and pause_runtime and _state(name).get("ActiveState") == "active":
        systemd._pause_runtime(cfg)
    systemd._systemctl("stop", _unit(name), timeout=cfg.stop_timeout_seconds + 15)
    state = _state(name)
    if state.get("ActiveState") not in {"inactive", "failed"} or state.get("MainPID", "0") != "0":
        raise DeployError("a service still has a writer; scheduled export and restart blocked")
    if state.get("Result", "success") not in {"", "success"}:
        raise DeployError("a service did not stop cleanly; scheduled export and restart blocked")


def _stop_scope(cfg: DeploymentConfig, scope: list[str], *, cleanup: bool = False,
                extra_writer_units: tuple[str, ...] = ()) -> list[str]:
    names = [name for name in scope if name.endswith(".service") and name not in extra_writer_units]
    names += [name for name in STOP_ORDER if name in scope]
    names += [name for name in extra_writer_units if name in scope]
    failures = []
    for name in names:
        try:
            _stop_one(cfg, name, pause_runtime=not cleanup)
        except Exception:
            if not cleanup:
                raise
            failures.append(_unit(name))
    return failures


def _start_scope(cfg: DeploymentConfig, scope: list[str], *, extra_writer_units: tuple[str, ...] = ()) -> None:
    names = [item for item in extra_writer_units if item in scope]
    names += [item for item in SERVICE_ORDER if item in scope]
    names += [item for item in scope if item.endswith(".service") and item not in extra_writer_units]
    for name in names:
        systemd._systemctl("start", _unit(name), timeout=cfg.startup_timeout_seconds + 15)
        if not name.endswith(".service"):
            systemd.wait_healthy(cfg, name)
        elif _state(name).get("ActiveState") != "active":
            raise DeployError("proxy failed to restart after backup")


def _safe_snapshot_child(root: Path, path: Path) -> Path:
    snapshot._no_links(root)
    snapshot._no_links(path)
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved.parent != resolved_root or not SCHEDULED_NAME.fullmatch(path.name) or not resolved.is_dir():
        raise DeployError("retention target is not a direct scheduled snapshot child")
    return resolved


def _scheduled_time(name: str) -> datetime:
    if not SCHEDULED_NAME.fullmatch(name):
        raise ValueError("not a scheduled backup name")
    return datetime.strptime(name.removeprefix("scheduled-"), "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)


def prune_scheduled(root: Path, *, now: datetime | None = None) -> dict:
    """Only verified scheduled snapshots are eligible; manual/invalid data stays."""
    root = root.absolute()
    snapshot._no_links(root)
    if not root.is_dir():
        raise DeployError("backup root is missing")
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    current_monday = moment.date() - timedelta(days=moment.weekday())
    candidates, skipped = [], []
    for child in sorted(root.iterdir()):
        if not SCHEDULED_NAME.fullmatch(child.name):
            continue
        try:
            safe = _safe_snapshot_child(root, child)
            snapshot.verify_snapshot(safe)
            stamp = _scheduled_time(child.name)
            candidates.append((stamp, safe))
        except (OSError, ValueError, DeployError):
            skipped.append(child.name)
    candidates.sort(reverse=True)
    days, weeks, retained = set(), set(), set()
    for stamp, path in candidates:
        day = stamp.date()
        monday = day - timedelta(days=day.weekday())
        keep_day = day not in days and len(days) < 7
        if keep_day:
            days.add(day)
        keep_week = monday not in weeks and current_monday - timedelta(weeks=3) <= monday <= current_monday
        if keep_week:
            weeks.add(monday)
        if keep_day or keep_week:
            retained.add(path)
    removed = []
    for _, path in candidates:
        if path in retained:
            continue
        # Verify again immediately before the destructive operation, including
        # containment and link checks. shutil never receives a computed parent.
        safe = _safe_snapshot_child(root, path)
        snapshot.verify_snapshot(safe)
        shutil.rmtree(safe)
        removed.append(path.name)
    return {"retained": sorted(path.name for path in retained), "removed": removed, "invalid_preserved": skipped}


def copy_offsite(source: Path, destination_root: Path) -> Path:
    root = _private_root(destination_root)
    source = source.resolve(strict=True)
    if root == source.parent or root.is_relative_to(source) or source.is_relative_to(root):
        raise DeployError("offsite directory overlaps local backup data")
    snapshot.verify_snapshot(source)
    destination = root / source.name
    if destination.exists() or destination.is_symlink():
        raise DeployError("offsite destination already exists")
    stage = root / (".copying-" + uuid4().hex)
    shutil.copytree(source, stage)
    snapshot.verify_snapshot(stage)
    os.replace(stage, destination)
    return destination


def _desktop_declaration(cfg: DeploymentConfig) -> str:
    # A configured legacy desktop writer needs an operator-controlled migration
    # check; an unattended job cannot assert another machine has stopped.
    if cfg.effective_bindings().get("NC_QQ_WATCHER_CONFIG"):
        raise DeployError("scheduled backup requires desktop watcher binding removed after migration")
    direct_path = cfg.effective_bindings().get("NC_QQ_DIRECT_CONFIG")
    if direct_path:
        snapshot._no_links(Path(direct_path).absolute())
        direct = json.loads(Path(direct_path).read_text(encoding="utf-8-sig"))
        status_path = direct.get("desktop_status_file")
        if status_path:
            path = Path(status_path).absolute()
            snapshot._no_links(path)
            if path.exists():
                status = json.loads(path.read_text(encoding="utf-8-sig"))
                if status.get("running") is True:
                    raise DeployError("desktop status reports a running writer; scheduled backup blocked")
    return "not-configured"


def run_backup(cfg: DeploymentConfig, *, offsite_dir: Path | None = None,
               proxy_unit: str | None = DEFAULT_PROXY, extra_writer_units: tuple[str, ...] = (),
               now: datetime | None = None) -> dict:
    scope, reason = running_scope(cfg, proxy_unit, extra_writer_units)
    if reason:
        return {"status": "skipped", "reason": reason, "services_woken": False}
    desktop = _desktop_declaration(cfg)
    root = _private_root(cfg.path("backup_root"))
    for source in (cfg.path("state_root"), cfg.path("config_root")):
        resolved = source.resolve(strict=True)
        if root == resolved or root.is_relative_to(resolved) or resolved.is_relative_to(root):
            raise DeployError("backup and source roots must not overlap")
        if offsite_dir is not None:
            candidate = offsite_dir.absolute()
            snapshot._no_links(candidate)
            if candidate == resolved or candidate.is_relative_to(resolved) or resolved.is_relative_to(candidate):
                raise DeployError("offsite destination overlaps live data")
    with _job_lock(root):
        # Recheck after acquiring exclusivity. Never wake a paused/stopped brain.
        scope, reason = running_scope(cfg, proxy_unit, extra_writer_units)
        if reason:
            return {"status": "skipped", "reason": reason, "services_woken": False}
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        name = "scheduled-" + moment.strftime("%Y%m%dT%H%M%S%fZ")
        target = root / name
        if target.exists() or target.is_symlink():
            raise DeployError("scheduled snapshot destination already exists")
        record_path = root / "scheduled-last-run.json"
        record = {"status": "stopping", "started_at": moment.isoformat(), "snapshot": name, "restart_attempted": False,
                  "original_running": scope, "replication": "offsite-directory" if offsite_dir else "local-only"}

        def save():
            snapshot._no_links(record_path)
            temporary = root / (".scheduled-report-" + uuid4().hex)
            temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            os.replace(temporary, record_path)

        save()
        try:
            _stop_scope(cfg, scope, extra_writer_units=extra_writer_units)
            record["status"] = "exporting"
            save()
            sources = snapshot._deployment_sources(cfg)
            snapshot.export_snapshot(sources, target, stopped_ack=True, desktop_status=desktop)
            verification = snapshot.verify_snapshot(target)
            if offsite_dir is not None:
                copied = copy_offsite(target, offsite_dir)
                record["offsite_snapshot"] = str(copied)
                record["offsite_retention"] = prune_scheduled(copied.parent, now=moment)
            record["retention"] = prune_scheduled(root, now=moment)
            record["status"] = "restoring-original-service-scope"
            record["restart_attempted"] = True
            save()
            _start_scope(cfg, scope, extra_writer_units=extra_writer_units)
            record.update(status="completed", verification=verification, finished_at=datetime.now(UTC).isoformat())
            save()
            return record
        except Exception as exc:
            failures = _stop_scope(cfg, scope, cleanup=True, extra_writer_units=extra_writer_units)
            record.update(status="failed", error_type=type(exc).__name__,
                          cleanup_failures=failures, finished_at=datetime.now(UTC).isoformat())
            save()
            raise DeployError("scheduled backup failed; automatic resume blocked; inspect scheduled-last-run.json") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/neuralcloud/deploy.json"))
    parser.add_argument("--offsite-dir", type=Path)
    parser.add_argument("--proxy-unit", default=DEFAULT_PROXY)
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--extra-writer-unit", action="append", default=[], help="additional neuralcloud-specific writer; repeatable")
    args = parser.parse_args(argv)
    try:
        ensure_linux()
        result = run_backup(load_config(args.config), offsite_dir=args.offsite_dir,
                            proxy_unit=None if args.no_proxy else args.proxy_unit,
                            extra_writer_units=tuple(args.extra_writer_unit))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DeployError, OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, DeployError) else type(exc).__name__
        print("Scheduled backup failed: " + reason, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
