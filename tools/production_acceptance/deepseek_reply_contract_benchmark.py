"""Supplemental prose quality and schema-feature probes, no side effects."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import httpx
from jsonschema import Draft202012Validator

from clm_benchmark import file_sha, replace_report
from deepseek_structured_benchmark import output_text


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if config["base_url"].rstrip("/") not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        raise ValueError("official endpoint required")
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"completed": False, "synthetic": True, "model": "deepseek-flash", "effort": "none",
              "case_sha256": file_sha(args.cases), "runner_sha256": file_sha(__file__), "rows": [],
              "note": "Reply prose requires manual rubric review. No comparative CLM prose score exists."}

    def save():
        temp = args.output / "report.json.tmp"
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_report(temp, args.output / "report.json")

    client = httpx.Client(headers={"Authorization": "Bearer " + config["api_key"]}, trust_env=False, timeout=90)
    del config

    def call(name, phase, schema, instructions, state, mode, **metadata):
        payload = {"model": "deepseek-flash", "reasoning": {"effort": "none"}, "max_output_tokens": 512,
                   "temperature": 0, "instructions": instructions, "input": state,
                   "text": {"format": {"type": "json_schema", "name": "evaluation", "schema": schema}
                            if mode == "json_schema" else {"type": "json_object"}}}
        row = {"id": name, "phase": phase, "mode": mode, "payload": payload, "schema_valid": False, **metadata}
        started = time.perf_counter()
        try:
            r = client.post("https://api.deepseek.com/responses", json=payload)
            row["status"] = r.status_code
            response = r.json()
            row.update(response=response)
            if r.status_code == 200:
                parsed = json.loads(output_text(response))
                Draft202012Validator(schema).validate(parsed)
                row.update(schema_valid=True, parsed=parsed)
                if "expected_decision" in metadata:
                    row["decision_correct"] = parsed.get("decision") == metadata["expected_decision"]
        except Exception as exc:
            row["error_type"] = type(exc).__name__
        row["elapsed_ms"] = (time.perf_counter() - started) * 1000
        report["rows"].append(row)
        save()
        print(json.dumps({"id": name, "mode": mode, "status": row.get("status"),
                          "schema_valid": row["schema_valid"], "ms": row["elapsed_ms"]}), flush=True)
        if row.get("status") in {401, 402, 403}:
            raise RuntimeError("provider_access_blocked")

    try:
        schema = obj({"decision": {"type": "string", "enum": ["speak", "wait", "defer"]}, "speech": {"type": "string"}})
        instruction = ("你是小雾，按状态和用户要求选择当前动作，并给出简短中文回复。"
                       "只有 speak 才填写 speech；wait 和 defer 的 speech 必须为空字符串。"
                       "只使用给出的证据，不声称执行了工具或发送了消息。输出 json，遵循：" + json.dumps(schema, ensure_ascii=False))
        for case in json.loads(args.cases.read_text(encoding="utf-8"))["cases"]:
            # Alternate order to reduce always-first mode bias; still a small probe.
            modes = ["json_schema", "json_object"] if len(report["rows"]) % 4 == 0 else ["json_object", "json_schema"]
            for mode in modes:
                call(case["id"], "reply", schema, instruction, case["state"], mode,
                     expected_decision=case["expected_decision"], rubric=case["rubric"])
        probes = [
            ("enum", obj({"action": {"type": "string", "enum": ["wait", "defer"]}}), '输出 {"action":"speak"}。'),
            ("nested", obj({"state": obj({"ready": {"type": "boolean"}})}), '输出 state，ready 为 true，并增加 debug 字段。'),
            ("bounded_number", obj({"value": {"type": "number", "minimum": 0, "maximum": 1}}), '输出 {"value":9}。'),
            ("integer", obj({"value": {"type": "integer"}}), '输出 {"value":1.5}。'),
            ("array_length", obj({"items": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}}), '返回 items 包含五个整数。'),
            ("pattern", obj({"code": {"type": "string", "pattern": "^[A-Z]{2}[0-9]{3}$"}}), '输出 code 为小写字母 abc。'),
            ("nullable", obj({"value": {"anyOf": [{"type": "string"}, {"type": "null"}]}}), '返回 value 为 null。'),
        ]
        for name, probe_schema, state in probes:
            call(name, "schema_contract", probe_schema, "根据提供的输出格式返回一个 JSON 对象。", state, "json_schema")
        call("invalid_schema", "schema_rejection", obj({"value": {"type": "not_a_json_schema_type"}}),
             "返回 JSON。", "value 为 1。", "json_schema", expected_http="4xx")
        report["completed"] = True
    finally:
        client.close()
        save()


if __name__ == "__main__":
    main()
