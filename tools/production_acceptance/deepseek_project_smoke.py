"""Opt-in live test of project schemas through the production LLM adapter.

Uses only synthetic prompts. No Runtime, tools, memory writes or message sends.
Run from the repository root; the supplied config must be official DeepSeek Flash.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pydantic import TypeAdapter  # noqa: E402
from harness.config import Settings  # noqa: E402
from harness.llm.client import OpenAICompatClient  # noqa: E402
from harness.llm.deepseek_structured import decision_format, supported  # noqa: E402
from harness.cognitive.production import ProductionChoice, ProductionAppraisal  # noqa: E402
from harness.qq.decision import _ProductionGateDecision  # noqa: E402
from harness.contracts.capability_calls import CapabilityChoice  # noqa: E402
from harness.contracts.goals import GoalChoice  # noqa: E402
from harness.cognitive.web import WebStepChoice  # noqa: E402
from harness.cognitive.board import _Expression as BoardExpression  # noqa: E402
from harness.cognitive.diary import _Expression as DiaryExpression  # noqa: E402
from harness.cognitive.basic_terminal import _DECISION  # noqa: E402
from harness.contracts.experimental_decisions import EXPERIMENTAL_DECISION_ADAPTER  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    settings = Settings(_env_file=None, llm_base_url=config["base_url"], llm_model=config["model"],
                        llm_api_key=config["api_key"], call_log_enabled=False, model_request_log_enabled=False,
                        llm_timeout=45)
    if not supported(settings):
        raise ValueError("official_deepseek_flash_configuration_required")
    del config
    args.output.mkdir(parents=True, exist_ok=False)
    client = OpenAICompatClient(settings)
    cases = [("production_choice", TypeAdapter(ProductionChoice), "选择 wait，不提出任何记忆、目标或情绪更新。"),
             ("production_appraisal", TypeAdapter(ProductionAppraisal), "没有可以提出的情绪、关系或记忆更新。"),
             ("qq_gate", TypeAdapter(_ProductionGateDecision), "用户要求保持安静，选择 wait。没有记忆或网页信息。"),
             ("capability", TypeAdapter(CapabilityChoice), "没有能力 offer，选择 no_action。"),
             ("goal", TypeAdapter(GoalChoice), "episode_operation 选择 wait，choice 选择 wait，不提出其他更新。"),
             ("web", TypeAdapter(WebStepChoice), "没有查询或阅读请求，不调用网页工具。"),
             ("board", TypeAdapter(BoardExpression), "选择 wait，text 和 title 为空字符串。"),
             ("diary", TypeAdapter(DiaryExpression), "选择 wait，text 为空字符串。"),
             ("grounded_agency", EXPERIMENTAL_DECISION_ADAPTER, "没有可用动作，选择 no_action。"),
             ("terminal", _DECISION, "没有终端操作请求，选择 no_action。")]
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "completed": False, "synthetic": True,
              "model": "deepseek-flash", "reasoning": "none", "automatic_retries": 0,
              "network": "existing project httpx client; environment proxy settings inherited",
              "rows": [], "source_sha256": {}}
    for path in [Path(__file__), Path("harness/llm/client.py"), Path("harness/llm/deepseek_structured.py")]:
        report["source_sha256"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()

    def save():
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        for name, adapter, task in cases:
            schema = adapter.json_schema()
            messages = [{"role": "system", "content": "这是隔离接口测试，当前没有任务或人物证据。只返回符合给定 schema 的 JSON 对象。"},
                        {"role": "user", "content": task}]
            row = {"schema_name": name, "schema": schema, "messages": messages, "ok": False}
            start = time.perf_counter()
            try:
                result = client.complete_chat_result(messages, response_format=decision_format(client, schema, name))
                parsed = adapter.validate_json(result.text)
                row.update(ok=True, parsed=adapter.dump_python(parsed, mode="json"), usage=result.usage.model_dump(mode="json"),
                           response_metadata=result.response_metadata())
            except Exception as exc:
                row["error_type"] = type(exc).__name__
                response = getattr(exc, "response", None)
                if response is not None:
                    row["http_status"] = response.status_code
            row["elapsed_ms"] = (time.perf_counter() - start) * 1000
            report["rows"].append(row)
            save()
            print(json.dumps({k: row[k] for k in ["schema_name", "ok", "elapsed_ms"]}), flush=True)
            if row.get("http_status") in {401, 402, 403, 429}:
                break
        report["completed"] = len(report["rows"]) == len(cases)
        report["all_passed"] = report["completed"] and all(row["ok"] for row in report["rows"])
    finally:
        client.close()
        report["ended_at"] = datetime.now(timezone.utc).isoformat()
        save()


if __name__ == "__main__":
    main()
