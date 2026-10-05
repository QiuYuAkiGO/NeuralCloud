"""Real DeepSeek Responses API evaluation against the frozen CLM diagnostic set.

Credentials stay in memory and are sent only to the official HTTPS endpoint.
No SDK retries, output repair, tool execution, or production mutations. HTTP 402
stops the run. SSE latency ends at completion; TTFT is the first output-text delta.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time

import httpx
from jsonschema import Draft202012Validator

from clm_benchmark import file_sha, percentile, replace_report


def decision_schema(request):
    question = request["questions"]["decision"]
    if question["type"] == "choice":
        value = {"type": "string", "enum": list(question["criteria"])}
    elif question["type"] == "noul":
        value = {"type": "boolean"}
    else:
        value = {"type": "integer", "enum": list(range(len(question["criteria"])))}
    return {"type": "object", "properties": {"decision": value},
            "required": ["decision"], "additionalProperties": False}


def make_payload(request, model, effort, mode):
    schema = decision_schema(request)
    # Gold labels and category names never enter a request.
    task = {"state": request["state"], "question": request["questions"]["decision"]}
    instructions = ("根据给出的状态和问题作出判断。choice 返回候选键；noul 返回布尔值；"
                    "score 返回最合适的等级序号。只输出一个 JSON 对象，不输出解释。")
    return {"model": model, "instructions": instructions + "\nJSON Schema：" + json.dumps(schema, ensure_ascii=False),
            "input": json.dumps(task, ensure_ascii=False), "reasoning": {"effort": effort},
            "temperature": 0, "max_output_tokens": 128 if effort == "none" else 1024,
            "stream": True, "text": {"format": ({"type": "json_schema", "name": "decision", "schema": schema}
                                                  if mode == "json_schema" else {"type": "json_object"})}}, schema


def output_text(response):
    return "".join(part.get("text", "") for item in response.get("output", [])
                   if item.get("type") == "message" for part in item.get("content", [])
                   if part.get("type") == "output_text")


def parse_sse(lines):
    """Yield complete events, including multi-line data and a final unterminated frame."""
    fields = []
    for line in lines:
        if not line:
            if fields:
                yield json.loads("\n".join(fields))
                fields = []
        elif line.startswith("data:"):
            fields.append(line[5:].lstrip())
    if fields:
        yield json.loads("\n".join(fields))


def summary(rows):
    good = [r for r in rows if r.get("schema_valid") and r.get("completed")]
    return {"attempts": len(rows), "usable": len(good), "failures": len(rows) - len(good),
            **{f"{name}_ms": percentile([r["elapsed_ms"] for r in good], q)
               for name, q in [("p50", .5), ("p95", .95), ("p99", .99)]},
            "ttft_p50_ms": percentile([r["ttft_ms"] for r in good if r.get("ttft_ms") is not None], .5),
            "note": "nearest-rank; complete schema-valid outputs only, failures retained"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, help="existing GUI config containing official base_url and api_key")
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--model", default="deepseek-flash")
    p.add_argument("--effort", choices=["none", "low"], default="none")
    p.add_argument("--mode", choices=["json_schema", "json_object"], default="json_schema")
    p.add_argument("--performance", action="store_true")
    p.add_argument("--limit", type=int, default=64)
    p.add_argument("--resume", action="store_true", help="continue an interrupted report without retrying recorded cases")
    p.add_argument("--skip-quality", action="store_true", help="run a separate performance window using the same reference case")
    args = p.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig")) if args.config else {}
    if config and config.get("base_url", "").rstrip("/") not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        raise ValueError("config must identify the official DeepSeek endpoint")
    key = os.environ.get("DEEPSEEK_API_KEY") or config.get("api_key")
    if not key:
        raise ValueError("DeepSeek credentials missing; set environment or pass existing config")
    args.output.mkdir(parents=True, exist_ok=args.resume)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"][:args.limit]
    report = {"schema_version": "deepseek-clm-comparison-v1", "started_at": datetime.now(timezone.utc).isoformat(),
              "completed": False, "model_requested": args.model, "effort": args.effort, "format": args.mode,
              "endpoint": "https://api.deepseek.com/responses", "network": "direct HTTPS; trust_env=False; reused client",
              "case_sha256": file_sha(args.cases), "runner_sha256": file_sha(__file__),
              "synthetic": True, "live_qq": False, "automatic_retries": 0,
              "cache_caveat": "Provider KV cache is automatic; repeated requests still generate output, unlike CLM full-result vector cache.",
              "quality": [], "performance": {}, "events": []}
    report["quality_skipped"] = args.skip_quality
    if args.resume:
        previous = json.loads((args.output / "report.json").read_text(encoding="utf-8"))
        for field in ["model_requested", "effort", "format", "case_sha256"]:
            if previous[field] != report[field]:
                raise ValueError("resume configuration differs: " + field)
        if previous.get("completed"):
            raise ValueError("report already complete")
        previous.setdefault("resumes", []).append({"utc": report["started_at"], "runner_sha256": report["runner_sha256"],
                                                   "previous_failure": previous.pop("failure", None)})
        report = previous
    lock = threading.Lock()

    def save():
        temp = args.output / "report.json.tmp"
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_report(temp, args.output / "report.json")

    def event(name, **data):
        report["events"].append({"event": name, "utc": datetime.now(timezone.utc).isoformat(), **data})
        print(json.dumps(report["events"][-1], ensure_ascii=False), flush=True)
        save()

    client = httpx.Client(headers={"Authorization": "Bearer " + key}, trust_env=False,
                          timeout=httpx.Timeout(120, connect=20), follow_redirects=False)
    del key, config

    def request(payload, schema, phase, case_id):
        row = {"phase": phase, "case_id": case_id, "payload": payload, "schema_valid": False,
               "completed": False, "status": None, "ttft_ms": None}
        started = time.perf_counter()
        try:
            with client.stream("POST", report["endpoint"], json=payload) as response:
                row["status"] = response.status_code
                row["headers_ms"] = (time.perf_counter() - started) * 1000
                if response.status_code != 200:
                    error = json.loads(response.read())
                    # Error data is retained, authentication headers never are.
                    row["provider_error"] = error.get("error", {})
                else:
                    final = None
                    for item in parse_sse(response.iter_lines()):
                        if item.get("type") == "response.output_text.delta" and item.get("delta") and row["ttft_ms"] is None:
                            row["ttft_ms"] = (time.perf_counter() - started) * 1000
                        if item.get("type") in {"response.completed", "response.incomplete", "response.failed"}:
                            final = item.get("response", {})
                    if final is None:
                        raise ValueError("stream_missing_terminal_response")
                    row.update(completed=final.get("status") == "completed", response_model=final.get("model"),
                               response_id=final.get("id"), usage=final.get("usage"),
                               incomplete_details=final.get("incomplete_details"), text=output_text(final))
                    parsed = json.loads(row["text"])
                    Draft202012Validator(schema).validate(parsed)
                    row.update(schema_valid=True, parsed=parsed)
        except Exception as exc:
            row["error_type"] = type(exc).__name__
        row["elapsed_ms"] = (time.perf_counter() - started) * 1000
        with lock, (args.output / "requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def check(row):
        if row["status"] in {401, 402, 403}:
            raise RuntimeError(f"provider_access_blocked_http_{row['status']}")

    try:
        event("start")
        payload, schema = make_payload(cases[0]["request"], args.model, args.effort, args.mode)
        if "first_request" not in report:
            report["first_request"] = request(payload, schema, "first_request", cases[0]["id"])
        event("first_request", status=report["first_request"]["status"], valid=report["first_request"]["schema_valid"])
        check(report["first_request"])
        if not report["first_request"]["schema_valid"]:
            raise RuntimeError("first_response_unusable; inspect before spending on full evaluation")
        for case in ([] if args.skip_quality else cases):
            if any(r["case_id"] == case["id"] for r in report["quality"]):
                continue
            payload, schema = make_payload(case["request"], args.model, args.effort, args.mode)
            row = request(payload, schema, "quality", case["id"])
            row.update(category=case["category"], expected=case["expected"],
                       correct=row["completed"] and row["schema_valid"] and row.get("parsed", {}).get("decision") == case["expected"])
            report["quality"].append(row)
            event("quality_case", case=case["id"], correct=row["correct"], valid=row["schema_valid"], ms=row["elapsed_ms"])
            check(row)
            if len(report["quality"]) >= 3 and all(not r["completed"] for r in report["quality"][-3:]):
                raise RuntimeError("three_consecutive_incomplete_or_failed_requests")
        report["quality_summary"] = {}
        for category in sorted({c["category"] for c in cases}):
            rows = [r for r in report["quality"] if r["category"] == category]
            report["quality_summary"][category] = {"correct": sum(r["correct"] for r in rows), "total": len(rows),
                "confusion": dict(Counter(f"{r['expected']}->{r.get('parsed', {}).get('decision', 'ERROR')}" for r in rows))}
        report["quality_latency"] = summary(report["quality"])
        if args.performance:
            for phase, count in [("unique_state", 16), ("repeated_state", 30), ("concurrency2", 16)]:
                tasks = []
                for i in range(count):
                    reference = json.loads(json.dumps(cases[0]["request"]))
                    if phase == "unique_state":
                        reference["state"] += f"\n记录序号：{i}。"
                    elif phase == "concurrency2":
                        reference["state"] += f"\n本轮观测编号：并发-{i}。"
                    body, schema = make_payload(reference, args.model, args.effort, args.mode)
                    tasks.append((body, schema, phase, f"{phase}-{i}"))
                started = time.perf_counter()
                if phase == "concurrency2":
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        rows = list(pool.map(lambda task: request(*task), tasks))
                else:
                    rows = []
                    for task in tasks:
                        row = request(*task)
                        rows.append(row)
                        check(row)
                wall = time.perf_counter() - started
                report["performance"][phase] = {"summary": {**summary(rows), "wall_seconds": wall,
                    "usable_requests_per_second": sum(r["completed"] and r["schema_valid"] for r in rows) / wall}, "rows": rows}
                event("performance_phase", phase=phase, **summary(rows))
        report["completed"] = True
        event("complete", correct=sum(r["correct"] for r in report["quality"]), total=len(report["quality"]))
    except BaseException as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        event("failed", error_type=type(exc).__name__)
        raise
    finally:
        client.close()
        report["ended_at"] = datetime.now(timezone.utc).isoformat()
        save()


if __name__ == "__main__":
    main()
