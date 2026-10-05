"""Bounded paid synthetic Schema and cache checks; never executes decisions."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pydantic import TypeAdapter  # noqa: E402
from harness.config import Settings  # noqa: E402
from harness.llm.structured import build_structured_client, ParserOutput, GateOutput, InterpreterOutput, InsightOutput  # noqa: E402
from harness.llm.deepseek_structured import decision_format  # noqa: E402
from harness.core.models import RetrievalPlan, CuratorOutput  # noqa: E402
from harness.core.inference import ValidationResult  # noqa: E402
from harness.memory.tier import TierVerdict  # noqa: E402
from harness.emotion.models import EmotionAppraisal, GuidanceOutput  # noqa: E402
from harness.memory.qq_assimilation import QQMemoryProposal  # noqa: E402
from harness.cognitive.production import ProductionChoice, ProductionAppraisal  # noqa: E402
from harness.qq.decision import _GateDecision, _ProductionGateDecision  # noqa: E402
from harness.contracts.capability_calls import CapabilityChoice  # noqa: E402
from harness.contracts.goals import GoalChoice  # noqa: E402
from harness.cognitive.web import WebStepChoice  # noqa: E402
from harness.cognitive.board import _Expression as BoardExpression  # noqa: E402
from harness.cognitive.diary import _Expression as DiaryExpression  # noqa: E402
from harness.cognitive.basic_terminal import _DECISION  # noqa: E402
from harness.contracts.experimental_decisions import EXPERIMENTAL_DECISION_ADAPTER  # noqa: E402
from harness.agency.models import AgencyModelOutput  # noqa: E402
from harness.body_belief.appraisal import _Proposals  # noqa: E402
from harness.social_world.observations import EntityAppraisalProposal  # noqa: E402


def summary(rows):
    known = [r for r in rows if r.get("usage", {}).get("cache_status") == "known"]
    tokens = sum(r["usage"]["prompt_tokens"] for r in known)
    hits = sum(r["usage"]["cache_hit_tokens"] for r in known)
    return {"attempts": len(rows), "valid": sum(r["ok"] for r in rows), "input_tokens": tokens,
            "cache_hit_tokens": hits, "token_weighted_hit_rate": hits / tokens if tokens else None,
            "peak_cost_yuan": sum(r.get("peak_cost_yuan", 0) for r in rows),
            "mean_ms": sum(r["elapsed_ms"] for r in rows) / len(rows) if rows else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--persona", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-yuan", type=float, default=2)
    args = parser.parse_args()
    if not 0 < args.budget_yuan <= 5:
        raise ValueError("bounded_budget_required")
    args.output.mkdir(parents=True, exist_ok=False)
    client = build_structured_client(Settings(_env_file=None, structured_llm_config=args.config,
        call_log_enabled=False, model_request_log_enabled=False, llm_timeout=30))
    persona = json.loads(args.persona.read_text(encoding="utf-8"))["persona"]
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "rows": [], "budget_yuan": args.budget_yuan,
              "synthetic": True, "external_actions": 0, "model": "deepseek-flash", "reasoning": "none",
              "cache_caveat": "Provider cache cannot be cleared. First two requests are warmup, not guaranteed cold.",
              "completed": False}
    charged = 0.0

    def save():
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def invoke(name, adapter, messages, **labels):
        nonlocal charged
        schema = adapter.json_schema()
        # UTF-8 bytes conservatively bound text tokens. Reserve at peak rates,
        # including failed/unknown requests; never spend beyond this bound.
        bound = ((len(json.dumps([messages, schema], ensure_ascii=False).encode()) + 2048) * 2 + 1024 * 8) / 1e6
        if charged + bound > args.budget_yuan:
            raise RuntimeError("probe_budget_exhausted")
        charged += bound
        row = {"name": name, **labels, "ok": False, "messages": deepcopy(messages), "schema": schema,
               "reserved_peak_yuan": bound}
        report["rows"].append(row)
        start = time.perf_counter()
        try:
            result = client.complete_chat_result(messages, max_tokens=1024, timeout_seconds=30,
                response_format=decision_format(client, schema, name))
            row.update(usage=result.usage.model_dump(mode="json"), text=result.text)
            adapter.validate_json(result.text)
            row["ok"] = True
            usage = result.usage
            cost = ((usage.cache_hit_tokens or 0) * .04 +
                    (usage.prompt_tokens - (usage.cache_hit_tokens or 0)) * 2 + usage.completion_tokens * 8) / 1e6
            row["peak_cost_yuan"] = cost
            charged += cost - bound
        except Exception as exc:
            row["error_type"] = type(exc).__name__
            raise
        finally:
            row["elapsed_ms"] = (time.perf_counter() - start) * 1000
            report["charged_or_reserved_peak_yuan"] = charged
            save()
            print(json.dumps({k: v for k, v in row.items() if k in {"name", "variant", "turn", "ok", "elapsed_ms", "error_type"}}), flush=True)

    cases = [("context_parser", ParserOutput, "解析用户的你好；gate path=fast。"),
        ("complexity_gate", GateOutput, "普通问候，path=fast。"),
        ("social_interpreter", InterpreterOutput, "用户只说你好，没有其他意图证据。"),
        ("memory_insight", InsightOutput, "没有客观事件，insight=null，confidence=0。"),
        ("retrieval_plan", RetrievalPlan, "用户问上周的事情，time_scope=last_week。"),
        ("memory_curator", CuratorOutput, "没有新事件，ops=[]。"),
        ("memory_tier", TierVerdict, "普通问候，无需长期记忆，tier=short。"),
        ("response_validation", ValidationResult, "当前回复只是你好，没有违规，verdict=pass。"),
        ("emotion_appraisal", EmotionAppraisal, "没有新消息或新证据，不提出情绪更新。"),
        ("emotion_guidance", GuidanceOutput, "当前平静，表达简洁自然，暂无额外需要。"),
        ("qq_memory", QQMemoryProposal, "来源 synthetic:greeting：用户说你好。只生成一个低重要性问候 episode，source_ids=[synthetic:greeting]，insight=null，valence=0，impressions=[]。"),
        ("production_choice", ProductionChoice, "当前没有行动机会，kind=wait。"),
        ("production_appraisal", ProductionAppraisal, "没有记忆、关系或情绪更新。"),
        ("qq_gate", _GateDecision, "用户要求先别回复，action=wait。"),
        ("production_qq_gate", _ProductionGateDecision, "先别回复，action=wait，无记忆引用。"),
        ("capability", CapabilityChoice, "没有可用能力，选择 no_action。"),
        ("goal", GoalChoice, "episode_operation=wait，choice=wait，无其他更新。"),
        ("web", WebStepChoice, "没有可用网页工具，不搜索也不读取。"),
        ("board", BoardExpression, "action=wait，text和title均为空字符串。"),
        ("diary", DiaryExpression, "action=wait，text为空字符串。"),
        ("agency", AgencyModelOutput, "没有目标或证据，不提出计划或新目标，arbitration无改变。"),
        ("body", _Proposals, "没有身体经验，candidates=[]。"),
        ("entity", EntityAppraisalProposal, "没有主体观察，不提出信念。"),
        ("terminal", _DECISION, "没有终端请求，no_action。"),
        ("grounded", EXPERIMENTAL_DECISION_ADAPTER, "没有可用动作，no_action。")]
    try:
        for name, model, task in cases:
            adapter = model if isinstance(model, TypeAdapter) else TypeAdapter(model)
            invoke(name, adapter, [{"role": "system", "content": "隔离协议测试，只返回符合给定 Schema 的 JSON，不编造事实或引用。"},
                {"role": "user", "content": task}], phase="schema")
        for name, model, task in [cases[13], cases[9], cases[11]]:
            adapter = TypeAdapter(model)
            for variant in ["baseline", "optimized"]:
                client.settings.deepseek_cache_friendly_inputs = variant == "optimized"
                for i in range(6):
                    payload = {"current_event": {"at": report["started_at"], "sequence": i, "text": task},
                        "persona": persona, "memories": ([{"source_id": "synthetic-cat", "text": "用户曾说猫叫团团。"}] if i < 4 else []),
                        "recent_conversation": [{"role": "user", "text": "这是协议测试，请暂不执行行动。"}],
                        "production": {"allowed_actions": [], "remaining_budget": 8 - i}, "instruction": task}
                    messages = [{"role": "system", "content": "只输出给定 Schema 的 JSON。人格只影响表述，不补造事实；不执行动作。"},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]
                    invoke(name, adapter, messages, phase="cache", variant=variant, turn=i, warmup=i < 2)
                    time.sleep(2)
        report["completed"] = True
    finally:
        client.close()
        report["summary"] = summary(report["rows"])
        report["cache_windows"] = {f"{n}:{v}": {"all": summary([r for r in report["rows"] if r.get("phase") == "cache" and r["name"] == n and r["variant"] == v]),
            "warm": summary([r for r in report["rows"] if r.get("phase") == "cache" and r["name"] == n and r["variant"] == v and not r["warmup"]])}
            for n in ["qq_gate", "emotion_guidance", "production_choice"] for v in ["baseline", "optimized"]}
        save()


if __name__ == "__main__":
    main()
