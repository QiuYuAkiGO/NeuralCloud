"""Deployment CLI. Snapshot operations never start business services."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import SERVICE_MODULES, DeployError, ensure_linux, load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/neuralcloud/deploy.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="read-only host and configuration checks")
    package = commands.add_parser("package", help="produce a hashed allowlisted source tarball")
    package.add_argument("--source", type=Path, default=Path.cwd())
    package.add_argument("--output", type=Path, required=True)
    package.add_argument("--release-id", required=True)
    install = commands.add_parser("install", help="stage an immutable release and install systemd units")
    install.add_argument("--archive", type=Path)
    install.add_argument("--uv", default="uv")
    install.add_argument("--skip-sync", action="store_true", help="stage source only; activation refuses a missing venv")
    render = commands.add_parser("render", help="render systemd units without installation")
    render.add_argument("--output", type=Path)
    run = commands.add_parser("run", help=argparse.SUPPRESS)
    run.add_argument("service", choices=list(SERVICE_MODULES))
    wait = commands.add_parser("wait", help=argparse.SUPPRESS)
    wait.add_argument("service", choices=list(SERVICE_MODULES))
    wait.add_argument("--boot-only", action="store_true")
    for name in ("activate", "rollback"):
        action = commands.add_parser(name)
        action.add_argument("--release", required=name == "activate")
        action.add_argument("--core-only", action="store_true")
    start = commands.add_parser("start")
    start.add_argument("--core-only", action="store_true")
    commands.add_parser("stop")
    commands.add_parser("status")
    for name in ("backup", "restore", "verify"):
        commands.add_parser(name, add_help=False, help="offline snapshot utility; pass --help for arguments")
    return parser


def verify_configuration(cfg) -> dict:
    from .package import verify_release
    from .runner import read_token
    from harness.research.manifest import load_experiment_manifest
    from harness.homeostasis.profiles import load_profile

    release = cfg.path("current").resolve(strict=True)
    result = verify_release(release)
    manifest = load_experiment_manifest(release / cfg.manifest_path)
    expected = "development_debug" if cfg.development else "study_runtime"
    if manifest.control_mode != expected:
        raise DeployError("deployment authority conflicts with manifest")
    load_profile(release / cfg.profile_path)
    bindings = cfg.effective_bindings()
    if read_token(bindings["NC_CONTROL_TOKEN_FILE"]) == read_token(bindings["NC_SERVICE_TOKEN_FILE"]):
        raise DeployError("control and service token values must differ")
    for key in ("NC_GUI_CONFIG", "NC_QQ_SESSION_CONFIG", "NC_QQ_DIRECT_CONFIG", "NC_BOARD_CONFIG", "NC_VOICE_CONFIG", "NC_WEB_CONFIG"):
        if key in bindings:
            target = Path(bindings[key])
            if target.is_symlink() or not target.is_file():
                raise DeployError(f"missing or linked private configuration: {key}")
    return {**result, "configuration_valid": True, "business_services_started": False}


def main(argv=None) -> int:
    parser = _parser()
    args, extra = parser.parse_known_args(argv)
    try:
        if args.command in {"backup", "restore"} or (args.command == "verify" and extra):
            from . import snapshot
            return snapshot.main([args.command, "--config", str(args.config), *extra])
        if extra:
            parser.error("unrecognized arguments")
        if args.command == "package":
            from .package import create_package
            result = create_package(args.source, args.output, args.release_id)
        else:
            cfg = load_config(args.config)
            if args.command == "run":
                ensure_linux()
                from .runner import run_service
                run_service(cfg, args.service)
                return 0
            from . import systemd
            if args.command == "render":
                units = systemd.render_units(cfg)
                if args.output is not None:
                    args.output.mkdir(parents=True, exist_ok=True)
                    for name, content in units.items():
                        (args.output / name).write_text(content, encoding="utf-8")
                    result = {"units": list(units), "output": str(args.output.absolute())}
                else:
                    result = units
            elif args.command == "preflight":
                result = systemd.preflight(cfg)
            elif args.command == "verify":
                result = verify_configuration(cfg)
            else:
                ensure_linux()
                if args.command == "install":
                    result = systemd.install(cfg, archive=args.archive, uv=args.uv, skip_sync=args.skip_sync)
                elif args.command == "activate":
                    result = systemd.activate(cfg, args.release, core_only=args.core_only)
                elif args.command == "rollback":
                    result = systemd.rollback(cfg, args.release, core_only=args.core_only)
                elif args.command == "start":
                    systemd.start_services(cfg, core_only=args.core_only)
                    result = systemd.status(cfg)
                elif args.command == "stop":
                    systemd.stop_services(cfg)
                    result = systemd.status(cfg)
                elif args.command == "status":
                    result = systemd.status(cfg)
                elif args.command == "wait":
                    systemd.wait_healthy(cfg, args.service, boot_only=args.boot_only)
                    result = {"service": args.service, "ready": True}
                else:
                    raise DeployError("unsupported command")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if args.command == "preflight" and not result.get("ready") else 0
    except (DeployError, OSError, ValueError, KeyError) as exc:
        # Configuration/file parse failures can contain secrets; disclose no raw values.
        message = str(exc) if isinstance(exc, DeployError) else f"{type(exc).__name__}; inspect the private configuration or artifact"
        print(f"Deployment failed: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
