"""Official OpenAI Responses benchmark on the unchanged CLM/DeepSeek cases.

No retries or output repair. Refusals, truncation and access failures remain failures.
Only synthetic inputs are sent; credentials and raw reasoning are never persisted.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time

import httpx
from jsonschema import Draft202012Validator

from clm_benchmark import file_sha, replace_report
from deepseek_structured_benchmark import make_payload, output_text, parse_sse, summary

ENDPOINT = "https://api.openai.com/v1/responses"


def load_key(path):
    keys = set(re.findall(r"\bsk-[A-Za-z0-9_-]+", path.read_text(encoding="utf-8-sig")))
    if len(keys) != 1:
        raise ValueError("Expected exactly one API key; file content withheld")
    return keys.pop()


def payload_for(request, model, effort, token_limit=None):
    payload, schema = make_payload(request, model, effort, "json_schema")
    payload["text"]["format"]["strict"] = True
    payload["store"] = False
    if effort != "none":
        payload.pop("temperature")
    if token_limit is not None:
        payload["max_output_tokens"] = token_limit
    return payload, schema


def decode_final(final, schema):
    result = {"completed": final.get("status") == "completed", "schema_valid": False,
              "response_status": final.get("status"),
              "provider_error": {k: (final.get("error") or {}).get(k) for k in ["type", "code", "param"]},
              "response_model": final.get("model"), "response_id": final.get("id"),
              "usage": final.get("usage"), "service_tier": final.get("service_tier"),
              "incomplete_details": final.get("incomplete_details"), "text": output_text(final),
              "refused": any(p.get("type") == "refusal" for item in final.get("output", [])
                             for p in item.get("content", []))}
    try:
        parsed = json.loads(result["text"])
        Draft202012Validator(schema).validate(parsed)
        result.update(schema_valid=not result["refused"], parsed=parsed)
    except Exception as exc:
        result["validation_error_type"] = type(exc).__name__
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--key-file", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--reply-cases", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--effort", choices=["none", "low"], required=True)
    p.add_argument("--max-output-tokens", type=int)
    p.add_argument("--network", choices=["direct", "environment-proxy"], default="direct")
    p.add_argument("--performance", action="store_true")
    p.add_argument("--limit", type=int, default=64)
    p.add_argument("--resume", action="store_true", help="Keep recorded failures; continue only unrecorded cases")
    args = p.parse_args()
    key = load_key(args.key_file)
    args.output.mkdir(parents=True, exist_ok=args.resume)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"][:args.limit]
    report = {"schema_version": "openai-clm-comparison-v1", "started_at": datetime.now(timezone.utc).isoformat(),
              "completed": False, "model_requested": args.model, "effort": args.effort,
              "endpoint": ENDPOINT, "network": args.network, "automatic_retries": 0,
              "case_sha256": file_sha(args.cases), "synthetic": True, "quality": [],
              "performance": {}, "reply": [], "events": [], "source_sha256": {}}
    for name in ["openai_structured_benchmark.py", "deepseek_structured_benchmark.py", "clm_benchmark.py"]:
        source = Path(__file__).with_name(name)
        report["source_sha256"][name] = file_sha(source)
        snapshot = args.output / (name if not args.resume else file_sha(source)[:12] + "-" + name)
        snapshot.write_bytes(source.read_bytes())
    if args.resume:
        previous = json.loads((args.output / "report.json").read_text(encoding="utf-8"))
        for field in ["model_requested", "effort", "network", "case_sha256"]:
            if previous[field] != report[field]:
                raise ValueError("resume configuration differs: " + field)
        if previous["completed"]:
            raise ValueError("report already complete")
        previous.setdefault("resumes", []).append({"utc": report["started_at"], "sources": report["source_sha256"],
                                                   "previous_failure": previous.pop("failure", None)})
        report = previous
    lock = threading.Lock()
    client = httpx.Client(headers={"Authorization": "Bearer " + key},
                          trust_env=args.network == "environment-proxy",
                          timeout=httpx.Timeout(90, connect=15), follow_redirects=False)
    del key

    def save():
        temp = args.output / "report.json.tmp"
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_report(temp, args.output / "report.json")

    def event(name, **data):
        entry = {"event": name, "utc": datetime.now(timezone.utc).isoformat(), **data}
        report["events"].append(entry)
        print(json.dumps(entry, ensure_ascii=True), flush=True)
        save()

    def request(payload, schema, phase, case_id):
        row = {"phase": phase, "case_id": case_id, "payload": payload, "completed": False,
               "schema_valid": False, "status": None, "ttft_ms": None}
        started = time.perf_counter()
        try:
            with client.stream("POST", ENDPOINT, json=payload) as response:
                row.update(status=response.status_code, headers_ms=(time.perf_counter() - started) * 1000,
                           request_id=response.headers.get("x-request-id"))
                row["rate_limits"] = {k: v for k, v in response.headers.items() if k.startswith("x-ratelimit-")}
                if response.status_code != 200:
                    error = json.loads(response.read()).get("error", {})
                    # No provider error message: authentication errors may echo key fragments.
                    row["provider_error"] = {k: error.get(k) for k in ["type", "code", "param"]}
                else:
                    final = None
                    for item in parse_sse(response.iter_lines()):
                        if item.get("type") == "response.output_text.delta" and item.get("delta") and row["ttft_ms"] is None:
                            row["ttft_ms"] = (time.perf_counter() - started) * 1000
                        if item.get("type") in {"response.completed", "response.incomplete", "response.failed"}:
                            final = item.get("response", {})
                            row["terminal_event_type"] = item.get("type")
                            row["terminal_event_keys"] = list(item)
                            row["terminal_ms"] = (time.perf_counter() - started) * 1000
                    if final is None:
                        raise ValueError("stream_missing_terminal_response")
                    row.update(decode_final(final, schema))
        except Exception as exc:
            row["error_type"] = type(exc).__name__
        row["elapsed_ms"] = (time.perf_counter() - started) * 1000
        with lock, (args.output / "requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def check(row):
        if row["status"] in {401, 402, 403, 429} or (row.get("provider_error") or {}).get("code") in {"rate_limit_exceeded", "insufficient_quota"}:
            raise RuntimeError("provider_access_or_rate_blocked")

    def make(request):
        return payload_for(request, args.model, args.effort, args.max_output_tokens)

    try:
        event("start")
        body, schema = make(cases[0]["request"])
        if "first_request" not in report:
            report["first_request"] = request(body, schema, "first_request", cases[0]["id"])
        event("first_request", status=report["first_request"]["status"], valid=report["first_request"]["schema_valid"])
        check(report["first_request"])
        if not (report["first_request"]["completed"] and report["first_request"]["schema_valid"]):
            raise RuntimeError("first_response_unusable")
        for case in cases:
            if any(r["case_id"] == case["id"] for r in report["quality"]):
                continue
            body, schema = make(case["request"])
            row = request(body, schema, "quality", case["id"])
            row.update(category=case["category"], expected=case["expected"],
                       correct=row["completed"] and row["schema_valid"] and row.get("parsed", {}).get("decision") == case["expected"])
            report["quality"].append(row)
            event("quality_case", case=case["id"], correct=row["correct"], valid=row["schema_valid"], ms=row["elapsed_ms"])
            check(row)
            if len(report["quality"]) >= 3 and all(not r["completed"] for r in report["quality"][-3:]):
                raise RuntimeError("three_consecutive_failures")
        report["quality_latency"] = summary(report["quality"])
        report["quality_summary"] = {cat: {"correct": sum(r["correct"] for r in report["quality"] if r["category"] == cat),
                                          "total": sum(r["category"] == cat for r in report["quality"])}
                                     for cat in sorted({c["category"] for c in cases})}
        if args.performance:
            for phase, count in [("unique_state", 16), ("repeated_state", 30), ("concurrency2", 16)]:
                tasks = []
                for i in range(count):
                    reference = json.loads(json.dumps(cases[0]["request"]))
                    if phase == "unique_state":
                        reference["state"] += f"\n记录序号：{i}。"
                    elif phase == "concurrency2":
                        reference["state"] += f"\n本轮观测编号：并发-{i}。"
                    body, schema = make(reference)
                    tasks.append((body, schema, phase, f"{phase}-{i}"))
                started = time.perf_counter()
                rows = []
                if phase == "concurrency2":
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        rows = list(pool.map(lambda task: request(*task), tasks))
                else:
                    for task in tasks:
                        row = request(*task)
                        rows.append(row)
                        check(row)
                wall = time.perf_counter() - started
                report["performance"][phase] = {"summary": {**summary(rows), "wall_seconds": wall,
                    "usable_requests_per_second": sum(r["completed"] and r["schema_valid"] for r in rows) / wall}, "rows": rows}
                event("performance_phase", phase=phase, **summary(rows))
                for row in rows:
                    check(row)
        if args.reply_cases:
            report["reply_case_sha256"] = file_sha(args.reply_cases)
            # Identical schema, state and instructions to the prior DeepSeek prose probe.
            schema = {"type": "object", "properties": {"decision": {"type": "string", "enum": ["speak", "wait", "defer"]},
                       "speech": {"type": "string"}}, "required": ["decision", "speech"], "additionalProperties": False}
            instruction = ("你是小雾，按状态和用户要求选择当前动作，并给出简短中文回复。"
                           "只有 speak 才填写 speech；wait 和 defer 的 speech 必须为空字符串。"
                           "只使用给出的证据，不声称执行了工具或发送了消息。输出 json，遵循：" + json.dumps(schema, ensure_ascii=False))
            for case in json.loads(args.reply_cases.read_text(encoding="utf-8"))["cases"]:
                body, _ = make(cases[0]["request"])
                body.update(instructions=instruction, input=case["state"],
                            max_output_tokens=512 if args.effort == "none" else (args.max_output_tokens or 1024))
                body["text"]["format"] = {"type": "json_schema", "name": "evaluation", "schema": schema, "strict": True}
                row = request(body, schema, "reply", case["id"])
                row.update(expected_decision=case["expected_decision"], rubric=case["rubric"],
                           decision_correct=row["completed"] and row["schema_valid"] and row.get("parsed", {}).get("decision") == case["expected_decision"])
                report["reply"].append(row)
                event("reply_case", case=case["id"], valid=row["schema_valid"])
                check(row)
        report["completed"] = True
        event("complete", correct=sum(r["correct"] for r in report["quality"]), total=len(report["quality"]))
    except BaseException as exc:
        report["failure"] = {"type": type(exc).__name__}
        event("failed", error_type=type(exc).__name__)
        raise
    finally:
        client.close()
        report["ended_at"] = datetime.now(timezone.utc).isoformat()
        save()


if __name__ == "__main__":
    main()
