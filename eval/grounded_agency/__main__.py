"""CLI defaults to fake model + simulator; live plugin factories require --live."""

from __future__ import annotations

import argparse
import asyncio
import importlib
from datetime import UTC, datetime
from pathlib import Path

from eval.grounded_agency.evidence import canonical_json, validate_run
from eval.grounded_agency.runner import run_experiment


def _factory(reference: str | None):
    if reference is None:
        return None
    module, separator, symbol = reference.partition(":")
    if not separator or not module or not symbol:
        raise ValueError("plugin factories use module:callable syntax")
    factory = getattr(importlib.import_module(module), symbol)
    if not callable(factory):
        raise ValueError("plugin factory is not callable")
    return factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run")
    run.add_argument("--scenario", action="append", default=[])
    run.add_argument("--condition", action="append", default=[])
    run.add_argument("--repetitions", type=int, default=1)
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--provider", default="simulator")
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--timeout-seconds", type=float, default=30)
    run.add_argument("--live", action="store_true")
    run.add_argument("--model-factory", help="explicit live provider module:callable taking a model seed")
    run.add_argument("--model-id")
    run.add_argument("--provider-factory", help="explicit module:callable taking scenario, seed and clock")
    validate = subcommands.add_parser("validate-run")
    validate.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-run":
            result = validate_run(args.run_dir, write_report=True)
            print(canonical_json(result))
            return 0 if result.passed else 1
        if (args.model_factory or args.provider_factory) and not args.live:
            raise ValueError("CLI plugin factories require --live")
        output = args.output_dir or Path("eval/runs/grounded-agency") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        result = asyncio.run(run_experiment(output, scenarios=args.scenario or ["action_only_v1"],
            conditions=args.condition or ["opaque_current"], repetitions=args.repetitions, seed=args.seed,
            provider=args.provider, live=args.live, timeout_seconds=args.timeout_seconds,
            model_factory=_factory(args.model_factory), provider_factory=_factory(args.provider_factory), model_id=args.model_id))
        print(canonical_json({"run_dir": result.run_dir, "trials": len(result.trials),
            "integrity_passed": result.integrity.passed, "release_eligible": False}))
        return 0 if result.integrity.passed else 1
    except (ValueError, OSError, ImportError, AttributeError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
