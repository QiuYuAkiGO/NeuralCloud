"""Run Misty path self-check. Never stops port 8766. Never sends QQ."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT = Path("/opt/neuralcloud/current")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for extra in (Path.cwd(), CURRENT):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.append(str(extra))

from tools.selfcheck.common import FAIL_STATUSES, load_launch
from tools.selfcheck.probes_isolated import run_isolated
from tools.selfcheck.probes_production import run_production
from tools.selfcheck.probes_readonly import run_readonly
from tools.selfcheck.report import failed, summarize, write_report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=("0", "1", "2", "all"), default="all")
    parser.add_argument("--no-active", action="store_true",
                        help="skip production chat/web/diary/goal triggers")
    parser.add_argument("--launch-config", type=Path, default=ROOT / "launch.json")
    parser.add_argument("--profile", default="shared")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def run(args) -> dict:
    ctx = load_launch(args.launch_config, args.profile)
    results = []
    tiers = {0, 1, 2} if args.tier == "all" else {int(args.tier)}
    if 0 in tiers:
        results.extend(run_readonly(ctx))
    if 1 in tiers:
        results.extend(asyncio.run(run_isolated(ctx)))
    if 2 in tiers:
        results.extend(run_production(ctx, no_active=args.no_active, timeout=args.timeout))
    folder = write_report(results, destination=args.output, extra={
        "tier": args.tier, "no_active": args.no_active, "profile": args.profile,
        "launch_config": str(ctx.launch_config),
    })
    return {"folder": str(folder), "summary": summarize(results),
            "failed": [item.as_dict() for item in failed(results)],
            "results": [item.as_dict() for item in results]}


def main(argv=None) -> int:
    args = parse_args(argv)
    report = run(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"folder": report["folder"], "summary": report["summary"],
                          "failed": report["failed"]}, ensure_ascii=False, indent=2))
    return 1 if any(item["status"] in FAIL_STATUSES for item in report["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
