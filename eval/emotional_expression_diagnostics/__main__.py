"""Executable diagnostics CLI. Network generation requires both a sealed opt-in and --live."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from .analysis import analyze_run
from .audit import audit_spec, preflight
from .fixtures import default_cases
from .models import (AnalysisSpec, BudgetSpec, DiagnosticRunSpec, GenerationSpec, PriceSpec,
                     append_jsonl, load_spec, save_spec, write_json)
from .ratings import export_blind_samples, import_ratings
from .runner import DirectHTTPReplayClient, run_experiment


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Isolated expression diagnostics; every output is release_eligible=false.")
    commands = result.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight", help="Read or create a frozen synthetic pilot spec; no model calls.")
    check.add_argument("--spec", type=Path)
    check.add_argument("--create-spec", type=Path)
    check.add_argument("--live", action="store_true", help="Freeze/validate the already authorized live-run flag; does not send requests.")
    check.add_argument("--all-families", action="store_true")
    check.add_argument("--run-id", default="expression-pilot")
    check.add_argument("--model", default="offline-fake")
    check.add_argument("--endpoint", default="https://synthetic.invalid/v1/chat/completions")
    check.add_argument("--temperature", type=float, default=0.3)
    check.add_argument("--max-tokens", type=int, default=2048)
    check.add_argument("--thinking", choices=("enabled", "disabled"), default="disabled")
    check.add_argument("--reasoning-effort")
    check.add_argument("--input-price", type=float, default=0)
    check.add_argument("--output-price", type=float, default=0)
    check.add_argument("--price-source", default="offline-only: live pricing not verified")
    check.add_argument("--price-verified-at")
    check.add_argument("--max-requests", type=int, default=216)
    check.add_argument("--budget-scope", default="emotional-expression-2026-09-14")
    check.add_argument("--extra", choices=("H2", "E-style", "mode"), action="append", default=[])
    check.add_argument("--source-commit")
    audit = commands.add_parser("audit", help="Compare renderer and input visibility, without model calls.")
    audit.add_argument("--spec", type=Path, required=True)
    audit.add_argument("--out", type=Path, required=True)
    pilot = commands.add_parser("pilot", help="Run all planned isolated requests using fake or explicitly authorized live HTTP.")
    pilot.add_argument("--spec", type=Path, required=True)
    pilot.add_argument("--out", type=Path, required=True)
    pilot.add_argument("--live", action="store_true")
    pilot.add_argument("--gui-settings", type=Path, help="Read endpoint/model/key from the existing GUI settings, without modifying it.")
    pilot.add_argument("--budget-ledger", type=Path)
    judge = commands.add_parser("judge", help="Export sealed blind packets or import human ratings; auxiliary Cursor reviews are separate.")
    judge.add_argument("--run", type=Path, required=True)
    judge.add_argument("--import-ratings", type=Path)
    judge.add_argument("--cursor-live", action="store_true")
    judge.add_argument("--max-samples", type=int)
    analysis = commands.add_parser("analyze", help="Analyze independent human ratings with cluster-level intervals.")
    analysis.add_argument("--run", type=Path, required=True)
    return result


def _live_settings(args: Any, spec: DiagnosticRunSpec) -> str:
    from harness.config import Settings
    if args.gui_settings:
        from gui.persist import load_gui_settings
        values = load_gui_settings(args.gui_settings)
        endpoint = (values.base_url or "").rstrip("/") + "/chat/completions"
        model, key = values.model, values.api_key
        if values.mode != "live":
            raise ValueError("GUI settings are not live; refusing an implicit route change")
    else:
        values = Settings()
        endpoint = values.llm_base_url.rstrip("/") + "/chat/completions"
        model, key = values.llm_model, values.llm_api_key
    if endpoint != spec.generation.endpoint or model != spec.generation.model or not key:
        raise ValueError("runtime endpoint/model/key does not match the frozen specification")
    return key


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "preflight":
            if args.spec:
                if args.create_spec:
                    raise ValueError("choose either --spec or --create-spec")
                spec = load_spec(args.spec)
            else:
                commit = args.source_commit or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
                comparisons = list(AnalysisSpec().comparisons)
                if "H2" in args.extra:
                    comparisons.append(("H2-enhanced", "H2-current"))
                if "E-style" in args.extra:
                    comparisons.append(("E-style", "C-verbal"))
                if "mode" in args.extra:
                    comparisons.append(("mode-disabled", "mode-enabled"))
                spec = DiagnosticRunSpec(run_id=args.run_id, source_commit=commit, cases=default_cases(all_families=args.all_families),
                                         extras=tuple(args.extra), live_authorized=args.live,
                                         generation=GenerationSpec(model=args.model, endpoint=args.endpoint, temperature=args.temperature,
                                                                   max_tokens=args.max_tokens, thinking=args.thinking, reasoning_effort=args.reasoning_effort),
                                         price=PriceSpec(input_per_million=args.input_price, output_per_million=args.output_price,
                                                         source=args.price_source, verified_at=args.price_verified_at),
                                         budget=BudgetSpec(max_requests=args.max_requests, scope_id=args.budget_scope),
                                         analysis=AnalysisSpec(comparisons=tuple(comparisons)))
                if args.create_spec:
                    save_spec(args.create_spec, spec)
            output = preflight(spec, live=args.live)
        elif args.command == "audit":
            spec = load_spec(args.spec)
            output = audit_spec(spec)
            if args.out.exists() and any(args.out.iterdir()):
                raise FileExistsError("audit output must be new or empty")
            write_json(args.out / "visibility-matrix.json", output)
            counts = {"actual_http_cases": output["actual_http_cases"], "synthetic_cases": output["synthetic_cases"],
                      "unobservable_contrasts": len(output["unobservable_contrasts"])}
            (args.out / "route-audit.md").write_text(
                "# 路径与可见性审计\n\n" + json.dumps(counts, ensure_ascii=False) +
                "\n\n合成代理未验证线上 QQ 请求。无法定位的投诉、陈旧 revision 与未捕获的实际 HTTP 请求不能作为参数理解归因。\n",
                encoding="utf-8")
            output = {**counts, "planned_calls": output["planned_calls"], "release_eligible": False}
        elif args.command == "pilot":
            spec = load_spec(args.spec)
            client = None
            if args.live:
                key = _live_settings(args, spec)
                # This private sink receives the exact authorized body; console and reports never print it.
                client = DirectHTTPReplayClient(endpoint=spec.generation.endpoint, api_key=key,
                                                timeout_seconds=spec.generation.timeout_seconds,
                                                observer=lambda row: append_jsonl(args.out / "http-evidence.jsonl", row))
            try:
                output = run_experiment(spec, args.out, client=client, live=args.live, budget_path=args.budget_ledger)
            finally:
                if client:
                    client.close()
        elif args.command == "judge":
            output = export_blind_samples(args.run)
            if args.import_ratings:
                output["ratings"] = import_ratings(args.run, args.import_ratings)
            if args.cursor_live:
                from .cursor_review import review_run
                output["auxiliary"] = review_run(args.run, live=True, max_samples=args.max_samples)
        else:
            analysis = analyze_run(args.run)
            output = {key: analysis[key] for key in ("status", "phase", "sample_flow", "limitations", "release_eligible")}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if output.get("passed", True) and output.get("status") != "stopped" else 1
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        # Pydantic validation errors can embed input bodies: never print exception text here.
        print(json.dumps({"error_type": type(exc).__name__, "message": "diagnostic_configuration_or_artifact_invalid", "release_eligible": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
