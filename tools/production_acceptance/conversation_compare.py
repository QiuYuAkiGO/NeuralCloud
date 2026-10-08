"""Interleave old/new full-pipeline measurements on identical frozen fixtures."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

from tools.production_acceptance.conversation_acceptance import percentile95


def metrics(rows: list[dict]) -> dict:
    selected = [row for row in rows if not row["warmup"]]
    result = {}
    for variant in ("baseline", "candidate"):
        sample = [row for row in selected if row["variant"] == variant]
        usages = [event["usage"] for row in sample for event in row["http"] if event.get("usage")]
        prompt = sum(u.get("prompt_tokens", 0) for u in usages)
        cached = sum(u.get("prompt_cache_hit_tokens", (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
                     for u in usages)
        calls = [sum(e.get("record_type") == "http_request_prepared" for e in row["http"]) for row in sample]
        result[variant] = {"runs": len(sample), "ready": sum(row["ready"] for row in sample),
            "p95_seconds": percentile95([row["seconds"] for row in sample]),
            "http_calls": calls, "http_call_total": sum(calls), "p95_http_calls": percentile95(calls),
            "revision_calls": sum(row.get("usage", {}).get("revision_calls", 0) for row in sample),
            "models": sorted({e["response_model"] for row in sample for e in row["http"] if e.get("response_model")}),
            "prompt_tokens": prompt, "cache_hit_tokens": cached, "cache_fraction": cached / prompt if prompt else None,
            "review_failures": [row["case"] for row in sample for r in row["reviews"] if r["result"]["verdict"] != "pass"]}
    before, after = result["baseline"], result["candidate"]
    if before["runs"] and after["runs"]:
        allowance = max(.5, before["p95_seconds"] * .1)
        result.update(p95_delta_seconds=after["p95_seconds"] - before["p95_seconds"],
                      p95_allowed_delta_seconds=allowance,
                      latency_passed=after["p95_seconds"] <= before["p95_seconds"] + allowance,
                      same_models=before["models"] == after["models"],
                      calls_not_increased=after["http_call_total"] <= before["http_call_total"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "fixtures", "frozen", "structured-config", "fallback-config", "gui-config", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    args.output_dir.mkdir(exist_ok=False)
    cases = [r["case"] for r in json.loads((args.fixtures / "fixtures.json").read_text())]
    rows = []
    # Each case is warmed on both variants before measurements. Alternate the
    # leading variant, keep failed runs, and never retry to improve a percentile.
    for repeat in range(args.repeats + 1):
        for index, case in enumerate(cases):
            order = ("baseline", "candidate") if (index + repeat) % 2 == 0 else ("candidate", "baseline")
            for variant in order:
                root = getattr(args, variant)
                output = args.output_dir / f"{repeat}-{case}-{variant}.json"
                env = {k: v for k, v in os.environ.items() if not k.startswith("NC_")}
                env["PYTHONPATH"] = str(root)
                command = [str(root / ".venv/bin/python"), str(root / "tools/production_acceptance/conversation_replay.py"),
                    "--fixtures", str(args.fixtures), "--case", case, "--frozen", str(args.frozen),
                    "--structured-config", str(args.structured_config), "--fallback-config", str(args.fallback_config),
                    "--gui-config", str(args.gui_config), "--preset", str(root / "presets/builtin/misty.json"),
                    "--output", str(output), "--variant", variant]
                with output.with_suffix(".log").open("w") as log:
                    run = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=log, timeout=90)
                if not output.exists():
                    raise RuntimeError(f"replay_worker_failed:{variant}:{case}:{run.returncode}")
                row = json.loads(output.read_text())
                row.update(repeat=repeat, warmup=repeat == 0)
                rows.append(row)
                report = {"rows": rows, "summary": metrics(rows), "expected_measured_runs_per_variant": len(cases) * args.repeats}
                (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(json.dumps({"completed": len(rows), "case": case, "variant": variant, "ready": row["ready"]}), flush=True)
    summary = report["summary"]
    return 0 if (summary["latency_passed"] and summary["same_models"] and summary["calls_not_increased"]
                 and summary["candidate"]["ready"] == len(cases) * args.repeats) else 1


if __name__ == "__main__":
    raise SystemExit(main())
