"""CLI for the isolated research run. Only `run --live` sends generations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .core import canonical, preflight, prepare_run, read_json, read_manifest, run_samples, summarize


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = ROOT / "data" / "evaluations" / "chinese-reply-review-20260916"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="中文角色回复：限次采样与真实人工盲评")
    parser.add_argument("command", choices=["prepare", "preflight", "dry-run", "run", "status", "serve", "import-ratings", "analyze"])
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--key-file", type=Path, default=ROOT / "apiyi-api-key.txt")
    parser.add_argument("--live", action="store_true", help="Only run uses this explicit network generation switch")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--port", type=int, default=8795)
    parser.add_argument("--ratings", type=Path)
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    try:
        if args.command == "prepare":
            manifest = prepare_run(run_dir, ROOT)
            result = {"run_dir": str(run_dir), "run_id": manifest["run_id"], "planned": manifest["limits"]["planned"],
                      "manifest_sha256": manifest["manifest_sha256"], "generation_requests": 0}
        elif args.command == "preflight":
            prepare_run(run_dir, ROOT)
            result = preflight(run_dir, args.key_file)
        elif args.command == "dry-run":
            manifest = read_manifest(run_dir)
            requests = read_json(run_dir / "private" / "requests.json")
            result = {"valid": True, "generation_requests": 0, "planned": len(requests),
                      "models": [{"id": m["id"], "protocol": m["protocol"], "options": m["options"],
                                  "reasoning_policy": m["reasoning_policy"]} for m in manifest["models"]],
                      "scenario_ids": [s["id"] for s in manifest["scenarios"]]}
        elif args.command == "run":
            if not args.live:
                raise ValueError("run_requires_explicit_live_flag")
            if args.timeout <= 0 or args.timeout > 900:
                raise ValueError("timeout_must_be_between_zero_and_900_seconds")
            result = run_samples(run_dir, args.key_file, timeout_seconds=args.timeout,
                                 progress=lambda item: print(canonical(item), flush=True))
            from .review import analyze, export_review
            if result["statuses"].get("complete", 0):
                packet = export_review(run_dir)
                result["review"] = {"sample_count": len(packet["samples"]),
                                    "packet_sha256": packet["packet_sha256"],
                                    "path": str(run_dir / "review" / "index.html")}
            analyze(run_dir)
        elif args.command == "status":
            result = summarize(run_dir)
        elif args.command == "serve":
            from .review import export_review
            from .server import serve
            state = summarize(run_dir)
            if state["remaining"]:
                raise ValueError("finish_sampling_before_freezing_blind_packet")
            if not state["statuses"].get("complete", 0):
                raise ValueError("no_complete_samples_to_review")
            export_review(run_dir)
            print(canonical({"url": f"http://127.0.0.1:{args.port}", "status": "waiting_for_human"}), flush=True)
            serve(run_dir, host="127.0.0.1", port=args.port)
            return 0
        elif args.command == "import-ratings":
            if not args.ratings:
                raise ValueError("ratings_file_required")
            from .review import analyze, import_ratings
            read_manifest(run_dir)
            result = {"import": import_ratings(run_dir, read_json(args.ratings)), "analysis": analyze(run_dir)}
        else:
            from .review import analyze
            read_manifest(run_dir)
            result = analyze(run_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if result.get("ready", True) else 2
    except (ValueError, OSError, KeyError) as exc:
        # No provider body or exception traceback is written by the CLI.
        code = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(canonical({"error": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
