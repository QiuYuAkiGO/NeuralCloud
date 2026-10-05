"""Replay the exact CLM length-sweep states against DeepSeek; one sample per length."""
import argparse
import json
from pathlib import Path
import time

import httpx
from jsonschema import Draft202012Validator

from clm_benchmark import file_sha, replace_report
from deepseek_structured_benchmark import make_payload, output_text, parse_sse


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--clm-report", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if config["base_url"].rstrip("/") not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        raise ValueError("official endpoint required")
    source = json.loads(args.clm_report.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"source_clm_report_sha256": file_sha(args.clm_report), "runner_sha256": file_sha(__file__),
              "note": "Same state/question/candidates, different tokenizers and protocol overhead. One sample per length, not p95.",
              "rows": [], "completed": False}
    with httpx.Client(headers={"Authorization": "Bearer " + config["api_key"]}, trust_env=False, timeout=120) as client:
        del config
        for original in source["performance"]["length_sweep"]["rows"]:
            for effort in ["none", "low"]:
                payload, schema = make_payload(original["payload"], "deepseek-flash", effort, "json_schema")
                row = {"effort": effort, "target_tokens": original["target_tokens"],
                       "qwen_state_question_tokens": original["actual_state_question_tokens"], "payload": payload,
                       "schema_valid": False, "completed": False, "ttft_ms": None}
                started = time.perf_counter()
                try:
                    with client.stream("POST", "https://api.deepseek.com/responses", json=payload) as response:
                        row["status"] = response.status_code
                        if response.status_code != 200:
                            row["error"] = json.loads(response.read())
                        else:
                            for event in parse_sse(response.iter_lines()):
                                if event.get("type") == "response.output_text.delta" and event.get("delta") and row["ttft_ms"] is None:
                                    row["ttft_ms"] = (time.perf_counter() - started) * 1000
                                if event.get("type") in {"response.completed", "response.incomplete", "response.failed"}:
                                    final = event["response"]
                                    row.update(response_model=final.get("model"), usage=final.get("usage"),
                                               completed=final.get("status") == "completed", incomplete_details=final.get("incomplete_details"),
                                               text=output_text(final))
                            parsed = json.loads(row["text"])
                            Draft202012Validator(schema).validate(parsed)
                            row.update(schema_valid=True, parsed=parsed)
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                row["elapsed_ms"] = (time.perf_counter() - started) * 1000
                report["rows"].append(row)
                temp = args.output / "report.json.tmp"
                temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                replace_report(temp, args.output / "report.json")
                print(json.dumps({k: row.get(k) for k in ["effort", "target_tokens", "status", "schema_valid", "elapsed_ms"]}), flush=True)
                if row.get("status") in {401, 402, 403}:
                    raise RuntimeError("provider_access_blocked")
    report["completed"] = True
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
