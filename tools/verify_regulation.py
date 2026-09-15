"""Read-only preflight or eight-hour observation of an already running Runtime."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.regulation.monitor import REQUIRED_SECONDS, HttpObserver, monitor, preflight  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "monitor"), default="preflight")
    parser.add_argument("--runtime-url", default=os.getenv("NC_RUNTIME_URL", "http://127.0.0.1:8770"))
    parser.add_argument("--token-file", type=Path, help="Read the existing private Runtime control token file.")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--duration-seconds", type=float, default=REQUIRED_SECONDS, help="Shorter runs are always incomplete; no clock acceleration exists.")
    parser.add_argument("--interval-seconds", type=float, default=30)
    args = parser.parse_args()
    token = args.token_file.read_text(encoding="utf-8-sig").strip() if args.token_file else os.getenv("NC_CONTROL_TOKEN", "")
    if not token:
        parser.error("Provide --token-file or NC_CONTROL_TOKEN; token contents never enter reports.")
    output = args.out_dir or ROOT / "eval/reports" / ("regulation-readonly-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    observer = HttpObserver(args.runtime_url, token)
    try:
        report = preflight(observer, output) if args.mode == "preflight" else monitor(observer, output,
            duration_seconds=args.duration_seconds, interval_seconds=args.interval_seconds)
    finally:
        observer.close()
    print(json.dumps({"status": report["status"], "eight_hour_observation_complete": report["eight_hour_observation_complete"], "report": str(output / "report.json")}))


if __name__ == "__main__":
    main()
