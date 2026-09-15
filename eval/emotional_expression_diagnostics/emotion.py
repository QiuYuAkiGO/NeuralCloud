"""Offline production-path comparison for the Qwen emotion director.

This executable only constructs scripted clients. It has no live option, judge,
delivery adapter or production-state writer, and makes no quality claims.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from harness.config import Settings
from harness.emotion.semantics import GUIDANCE_VALIDATION_VERSION, compose_weights
from harness.emotion.service import PROMPT_VERSION as EMOTION_PROMPT_VERSION
from harness.llm.client import MockLLM, RoutingLLMClient

from eval.homeostatic_expression.evaluation import DEFAULT_PERSONA_PATH, build_summary, default_scenarios
from eval.homeostatic_expression.production import ProductionPairPipeline, homeostatic_segment, json_hash

FIXED_AT = datetime(2026, 9, 15, tzinfo=timezone.utc)
VARIANTS = ("legacy", "rule-fallback", "qwen-scripted")


def scripted_feeling(payload: dict) -> str:
    """A deterministic stand-in that follows the director's explicit contract."""
    return "；".join(item["degree"] + item["feeling"] + (
        "正在缓和，仍有余韵" if item["trend"] == "recovering" else "仍在当前感受中")
        for item in payload["required_feelings"]) or "此刻自然平静，不刻意制造强烈心情。"


class ScriptedEmotionAux(MockLLM):
    def complete_json(self, *, system, user, max_tokens=None, timeout_seconds=None, response_format=None):
        self.calls.append((system, user))
        payload = json.loads(user)
        if "EMOTION_APPRAISAL_V1" in system:
            return {"events": [{"source_id": source, "signals": [], "recovery": []}
                               for source in payload["new_source_ids"]]}
        if "EMOTION_GUIDANCE_V1" in system:
            needs = payload["homeostatic_needs"]
            if needs:
                need_text = needs[0]["semantic_need"] + "。"
                if any(item["trend"] == "recovering" for item in needs):
                    need_text += "这份需要正在缓和，表达也逐渐恢复平常节奏。"
            else:
                need_text = "没有明显需要，不制造情绪或原因。"
            return {"feeling": scripted_feeling(payload),
                    "delivery": "让当前感受自然影响语气与投入，保持事实边界。",
                    "needs": need_text}
        raise ValueError("unexpected offline helper request")


def offline_settings(variant: str) -> Settings:
    return Settings(_env_file=None, llm_model="offline-scripted-main",
        llm_base_url="https://synthetic.invalid/main/v1", llm_api_key="not-needed",
        aux_llm_model="offline-scripted-qwen", aux_llm_base_url="https://synthetic.invalid/aux/v1",
        aux_llm_api_key="not-needed", emotion_mode="off" if variant == "legacy" else "on")


def weight_controls() -> list[dict]:
    result = []
    for name, homeostasis, dialogue in (("calm", 0., 0.), ("sudden_anger", .3, .95),
                                       ("strong_need", .95, .2), ("competing_signals", .9, .85)):
        dynamic, intensity = compose_weights(homeostasis, dialogue, .25)
        result.append({"case": name, "strengths": {"homeostasis": homeostasis, "dialogue": dialogue, "personality": .25},
                       "fixed_weights": {key: 1 / 3 for key in dynamic}, "dynamic_weights": dynamic,
                       "intensity": intensity, "evidence_kind": "mathematical_control_no_generated_effect"})
    return result


def run_offline(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=False)
    spec = {"schema_version": "emotion-offline-comparison-v1", "live": False, "release_eligible": False,
            "emotion_prompt_version": EMOTION_PROMPT_VERSION,
            "emotion_validation_version": GUIDANCE_VALIDATION_VERSION,
            "emotion_response_format": {"type": "json_schema", "strict": True, "validation": "pydantic"},
            "variants": VARIANTS, "fixed_at": FIXED_AT.isoformat(),
            "scenarios": [item.model_dump(mode="json") for item in default_scenarios()],
            "persona_sha256": json_hash(json.loads(DEFAULT_PERSONA_PATH.read_text(encoding="utf-8"))["persona"]),
            "quality_scoring": "none", "independent_human_ratings": 0}
    spec["spec_sha256"] = json_hash(spec)
    (output_dir / "manifest.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    records = []
    with (output_dir / "evidence.jsonl").open("x", encoding="utf-8") as stream:
        for variant in VARIANTS:
            main = MockLLM(chat_routes={"": "嗯，知道了。"})
            aux = ScriptedEmotionAux()
            llm = RoutingLLMClient(main, aux) if variant == "qwen-scripted" else main
            settings = offline_settings(variant)
            pipeline = ProductionPairPipeline(llm=llm, persona_path=DEFAULT_PERSONA_PATH,
                fixed_user_text="我们接下来做点什么？", fixed_at=FIXED_AT, generator_model=settings.llm_model,
                runtime_model_version="offline-scripted", prompt_version="homeostatic-interoception-v1" if variant == "legacy" else "qwen-emotion-v1",
                temperature=.1, max_tokens=240, emotion_mode=settings.emotion_mode, emotion_settings=settings)
            try:
                for scenario in default_scenarios():
                    for condition in ("nominal", "marked"):
                        summary = build_summary(scenario, profile_ref="functional-proxy-v1@3",
                                                marked=condition == "marked", snapshot_revision=1)
                        prepared = pipeline.prepare(condition=condition, event_id="offline:" + scenario.scenario_id, summary=summary)
                        segment = homeostatic_segment(prepared.context)
                        record = {"variant": variant, "scenario": scenario.scenario_id, "condition": condition,
                                  "expression_segment": segment, "emotion_evidence": prepared.emotion_evidence,
                                  "main_calls": prepared.main_calls, "final": prepared.final.model_dump(mode="json"),
                                  "evidence_kind": "scripted_offline_production_path", "quality_scored": False,
                                  "live": False, "release_eligible": False}
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                        records.append(record)
            finally:
                pipeline.close()
    result = {"schema_version": "emotion-offline-comparison-summary-v1", "live": False, "release_eligible": False,
              "records": len(records), "variants": list(VARIANTS), "main_calls": sum(len(item["main_calls"]) for item in records),
              "independent_human_ratings": 0, "quality_result": "not_evaluated",
              "weight_controls": weight_controls(), "spec_sha256": spec["spec_sha256"]}
    (output_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="A new output directory")
    args = parser.parse_args(argv)
    print(json.dumps(run_offline(args.out), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
