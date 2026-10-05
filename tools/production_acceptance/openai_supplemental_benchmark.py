"""Replay frozen DeepSeek schema and length probes against official OpenAI."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import httpx

from clm_benchmark import file_sha, replace_report
from openai_structured_benchmark import ENDPOINT, access_blocked, decode_final, load_key


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--key-file", type=Path, required=True)
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--effort", choices=["none", "low"], required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    reference_contract = args.evidence / "deepseek-reply-contract.json"
    reference_length = args.evidence / "deepseek-length.json"
    report = {"completed": False, "model": args.model, "effort": args.effort, "endpoint": ENDPOINT,
              "network": "environment-proxy", "automatic_retries": 0,
              "started_at": datetime.now(timezone.utc).isoformat(), "rows": [],
              "source_sha256": {p.name: file_sha(p) for p in [reference_contract, reference_length, Path(__file__)]}}
    tasks = [r for r in json.loads(reference_contract.read_text(encoding="utf-8"))["rows"] if r["phase"].startswith("schema_")]
    for row in json.loads(reference_length.read_text(encoding="utf-8"))["rows"]:
        if row["effort"] == "none":
            tasks.append({**row, "phase": "length", "id": str(row["target_tokens"])})

    def save():
        temp = args.output / "report.json.tmp"
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_report(temp, args.output / "report.json")

    try:
        with httpx.Client(headers={"Authorization": "Bearer " + load_key(args.key_file)}, trust_env=True,
                          timeout=httpx.Timeout(90, connect=15), follow_redirects=False) as client:
            for reference in tasks:
                body = json.loads(json.dumps(reference["payload"]))
                body.update(model=args.model, reasoning={"effort": args.effort}, store=False, stream=False,
                            max_output_tokens=512 if args.effort == "none" else 1024)
                body["text"]["format"]["strict"] = True
                if args.effort != "none":
                    body.pop("temperature", None)
                row = {"id": reference["id"], "phase": reference["phase"], "payload": body,
                       "status": None, "completed": False, "schema_valid": False}
                if "qwen_state_question_tokens" in reference:
                    row["qwen_state_question_tokens"] = reference["qwen_state_question_tokens"]
                start = time.perf_counter()
                try:
                    response = client.post(ENDPOINT, json=body)
                    row.update(status=response.status_code, request_id=response.headers.get("x-request-id"))
                    data = response.json()
                    if response.status_code == 200:
                        row.update(decode_final(data, body["text"]["format"]["schema"]))
                    else:
                        row["provider_error"] = {k: data.get("error", {}).get(k) for k in ["type", "code", "param"]}
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                row["elapsed_ms"] = (time.perf_counter() - start) * 1000
                report["rows"].append(row)
                save()
                print(json.dumps({k: row[k] for k in ["id", "phase", "status", "schema_valid", "elapsed_ms"]}), flush=True)
                if access_blocked(row):
                    raise RuntimeError("provider_access_or_rate_blocked")
        report["completed"] = True
    finally:
        report["ended_at"] = datetime.now(timezone.utc).isoformat()
        save()


if __name__ == "__main__":
    main()
