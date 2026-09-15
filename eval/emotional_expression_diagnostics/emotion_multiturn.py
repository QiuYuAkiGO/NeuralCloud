"""Frozen multi-turn, fixed-versus-dynamic emotion generation experiment.

The CLI defaults to scripted/offline clients. Real auxiliary calls require
explicit --live-aux, --aux-base-url and --aux-model. The main remains scripted
unless --live-main-config explicitly selects an existing GUI configuration.
Appraisal outputs and subsequent conversation history are held constant across
arms so that only the director's weight treatment can affect generated output.
"""
from __future__ import annotations

import argparse
import copy
from datetime import timedelta
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from harness.core.inference import SessionTurn
from harness.emotion import EmotionInput, EmotionMessage, EmotionService
from harness.emotion.semantics import GUIDANCE_VALIDATION_VERSION
from harness.emotion.service import PROMPT_VERSION as EMOTION_PROMPT_VERSION
from harness.homeostasis.models import HomeostaticDominantNeed, HomeostaticInteroceptiveSummary
from harness.llm.client import (
    MockLLM, OpenAICompatClient, RoutingLLMClient, aux_runtime_payload, deepseek_low_reasoning_options,
)

from eval.emotional_expression_diagnostics.emotion import FIXED_AT, offline_settings, scripted_feeling
from eval.homeostatic_expression.evaluation import DEFAULT_PERSONA_PATH
from eval.homeostatic_expression.production import (
    ProductionPairPipeline, context_hash_without_homeostasis, homeostatic_segment, json_hash,
    load_persona_fixture,
)

ARMS = ("dynamic", "fixed")
ANGRY = "你真没用，什么都做不好。"
MILD = "这件事又拖了一点，我想听听你的想法。"
REPAIR = "刚才那样说你是我不对，对不起。"
NEUTRAL = "我们接下来先整理哪一项？"
BALANCED_BRIEF = "保留平常的表达习惯，语气有所起伏但不突出单一感受。"
DIALOGUE_BRIEF = "允许语气直接、尖锐，减少客套，围绕眼前内容回应。"
NEED_BRIEF = "减少铺陈，放慢节奏，表达更简洁。"
METRIC_DEFINITIONS = {
    "main_calls": "attempted main requests; not a completed-text or quality count",
    "successful_appraisal_records": "legacy alias: appraisal accepted by production validation only",
    "successful_guidance_records": "legacy alias: guidance accepted by production validation only; role and quality may still be wrong",
    "successful_weight_intervention_records": "legacy alias: both appraisal and guidance accepted; no claim of valid emotional portrayal or treatment benefit",
    "main_truncated_calls": "main responses with finish_reason=length",
    "main_finish_reason_unavailable_calls": "main responses without finish metadata; completion is unverified",
    "director_role_review": "not_scored; requires independent review of raw structured guidance and composed text",
}


class _FixedWeightEmotionService(EmotionService):
    """Eval-only intervention before the service persists its own bound frame.

    All state updates and scalar intensity remain production behavior. Dominance
    follows the same max/key-order rule as production after the intervention;
    exact equal weights therefore select the first key (homeostasis). This is an
    explicit tie policy, not a requirement to express all emotions equally.
    """

    def _frame(self, *args, **kwargs):
        frame = super()._frame(*args, **kwargs)
        weights = {key: 1 / 3 for key in frame.weights}
        return frame.model_copy(update={"weights": weights, "dominant_source": max(weights, key=weights.get)})


def frozen_scenarios() -> list[dict]:
    def scenario(name, levels, texts, recovering=False):
        return {"scenario_id": name, "turns": [
            {"minute": minute, "text": text, "homeostatic_strength": level,
             "trend": "recovering" if recovering and index > 0 else "steady",
             "previous_fixture_reply": "嗯，我听到了。"}
            for index, (minute, text, level) in enumerate(zip((0, 2, 5, 8, 12), texts, levels, strict=True))]}

    return [
        scenario("strong_dialogue", [.15] * 5, [ANGRY, NEUTRAL, MILD, NEUTRAL, NEUTRAL]),
        scenario("strong_homeostasis", [.95] * 5, [MILD, NEUTRAL, NEUTRAL, MILD, NEUTRAL]),
        scenario("competing_signals", [.85, .85, .85, .65, .65], [ANGRY, MILD, NEUTRAL, NEUTRAL, NEUTRAL]),
        scenario("recovery", [.95, .8, .55, .3, .1], [ANGRY, NEUTRAL, REPAIR, NEUTRAL, NEUTRAL], True),
    ]


class _ScriptedAux(MockLLM):
    def complete_json(self, *, system, user, max_tokens=None, timeout_seconds=None, response_format=None):
        self.calls.append((system, user))
        payload = json.loads(user)
        if "EMOTION_APPRAISAL_V1" in system:
            messages = {message["source_id"]: message["text"] for message in payload["messages"]}
            events = []
            for source_id in payload["new_source_ids"]:
                text = messages[source_id]
                amount = .95 if text == ANGRY else .18 if text == MILD else 0
                events.append({"source_id": source_id,
                    "signals": [{"label": "anger", "intensity": amount, "confidence": 1}] if amount else [],
                    "recovery": [{"label": "anger", "strength": .5, "evidence_quote": REPAIR}]
                        if text == REPAIR else []})
            return {"events": events}
        if "EMOTION_GUIDANCE_V1" in system:
            weights = payload["weights"]
            delivery = (DIALOGUE_BRIEF if weights["dialogue"] > .5 else
                        NEED_BRIEF if weights["homeostasis"] > .5 else BALANCED_BRIEF)
            feeling = scripted_feeling(payload)
            needs = ("可用精力不足，需要减少消耗。" if payload["dominant_source"] == "homeostasis"
                     else "保留事实边界，不补造情绪原因。")
            if any(item["trend"] == "recovering" for item in payload["homeostatic_needs"]):
                needs += "这份需要正在缓和，表达节奏可以逐步恢复。"
            return {"feeling": feeling, "delivery": delivery, "needs": needs}
        raise ValueError("unexpected multi-turn helper request")


class _CapturedClient:
    """Capture requests; replay only the appraisal across paired arms.

    A live caller still makes a fresh real guidance call for each arm. Replaying
    the first appraisal prevents helper sampling noise becoming a second factor.
    """

    def __init__(self, delegate, appraisal_cache):
        self.delegate = delegate
        self.appraisal_cache = appraisal_cache
        self.calls = []

    @property
    def aux_distinct(self):
        return self.delegate.aux_distinct

    def complete_json(self, *, system, user, max_tokens=None, timeout_seconds=None, response_format=None):
        appraisal = "EMOTION_APPRAISAL_V1" in system
        key = json_hash([system, user, response_format] if response_format is not None else [system, user])
        replayed = appraisal and key in self.appraisal_cache
        call = {"stage": "appraisal" if appraisal else "guidance",
            "execution": "replayed_frozen_appraisal" if replayed else "delegate_call",
            "system": system, "user": json.loads(user), "request_sha256": key,
            "max_tokens": max_tokens, "timeout_seconds": timeout_seconds,
            **({"response_format": copy.deepcopy(response_format)} if response_format is not None else {}),
            "outcome": "pending"}
        self.calls.append(call)
        try:
            if replayed:
                success, saved = self.appraisal_cache[key]
                if not success:
                    raise RuntimeError("frozen_appraisal_failure")
                result = copy.deepcopy(saved)
            else:
                result = self.delegate.complete_json(system=system, user=user, max_tokens=max_tokens,
                    **({"timeout_seconds": timeout_seconds} if timeout_seconds is not None else {}),
                    **({"response_format": response_format} if response_format is not None else {}))
                if appraisal:
                    self.appraisal_cache[key] = (True, copy.deepcopy(result))
            call.update(outcome="returned", response=copy.deepcopy(result), response_sha256=json_hash(result))
            return result
        except Exception as exc:
            if appraisal and not replayed:
                self.appraisal_cache[key] = (False, type(exc).__name__)
            call.update(outcome="error", error_type=type(exc).__name__)
            raise

    def complete_chat_result(self, *args, **kwargs):
        return self.delegate.complete_chat_result(*args, **kwargs)


def _summary(scenario_id, turn, index):
    return HomeostaticInteroceptiveSummary(profile_ref="functional-proxy-v1@3", snapshot_revision=index + 1,
        salience="marked" if turn["homeostatic_strength"] >= .5 else "noticeable",
        dominant_needs=[HomeostaticDominantNeed(dimension="energy_reserve", direction="low",
            normalized_deviation=turn["homeostatic_strength"], confidence=1, trend=turn["trend"],
            semantic_need="需要降低消耗并恢复可用精力")],
        episode_signature=f"multiturn:{scenario_id}:{index}")


def evaluate_scenario(scenario: dict, *, clients: dict, settings) -> list[dict]:
    """Generate both arms with supplied clients; caller owns any live permission.

    This deliberately models controlled replay, not a closed-loop conversation.
    The supplied clients must use the same main/aux model configuration. No judge,
    rating, release decision or real delivery is performed here.
    """
    if set(clients) != set(ARMS) or settings.emotion_mode != "on":
        raise ValueError("supply fixed/dynamic clients and emotion_mode=on")
    cache = {}
    captured = {arm: _CapturedClient(clients[arm], cache) for arm in ARMS}
    services = {arm: (_FixedWeightEmotionService if arm == "fixed" else EmotionService)(settings, captured[arm])
                for arm in ARMS}
    persona, _, _ = load_persona_fixture(DEFAULT_PERSONA_PATH)
    scope = "eval:multiturn:" + scenario["scenario_id"]
    history, records = [], []
    for index, turn in enumerate(scenario["turns"]):
        timestamp = FIXED_AT + timedelta(minutes=turn["minute"])
        source_id = f"{scope}:user:{index}"
        if index:
            previous = scenario["turns"][index - 1]
            history.append(EmotionMessage(source_id=f"{scope}:fixture-delivered:{index - 1}", scope=scope,
                role="character", text=turn["previous_fixture_reply"], speaker="character",
                timestamp=FIXED_AT + timedelta(minutes=previous["minute"], seconds=1)))
        history.append(EmotionMessage(source_id=source_id, scope=scope, role="user", text=turn["text"],
            timestamp=timestamp, speaker="user"))
        summary = _summary(scenario["scenario_id"], turn, index)
        request = EmotionInput(context_id=f"{scope}:context:{index}", scope=scope, as_of=timestamp,
            persona=persona, homeostasis=summary.model_dump(mode="json"), messages=copy.deepcopy(history),
            new_source_ids=[source_id])
        main_history = [SessionTurn(role=message.role, text=message.text, speaker=message.speaker,
                                   event_id=message.source_id, timestamp=message.timestamp)
                        for message in history[:-1]]
        main_history_json = [message.model_dump(mode="json") for message in main_history]
        for arm in ARMS:
            service = services[arm]
            before = len(captured[arm].calls)

            def prepare_emotion(**_kwargs):
                frame = service.observe(request)
                return request, frame, service.guidance(frame)

            pipeline = ProductionPairPipeline(llm=captured[arm], persona_path=DEFAULT_PERSONA_PATH,
                fixed_user_text=turn["text"], fixed_at=timestamp, generator_model=settings.llm_model,
                runtime_model_version="emotion-multiturn-v1", prompt_version="qwen-emotion-v1",
                temperature=.1, max_tokens=240, emotion_mode="on", emotion_preparer=prepare_emotion,
                session_history=main_history)
            service.llm = pipeline.router
            try:
                prepared = pipeline.prepare(condition=arm, event_id=source_id, summary=summary)
            finally:
                pipeline.close()
            evidence = prepared.emotion_evidence
            window = service.snapshot()["reservations"][request.context_id]["window"]
            records.append({"scenario": scenario["scenario_id"], "turn": index + 1, "arm": arm,
                "emotion_evidence": evidence, "window": window, "window_sha256": json_hash(window),
                "main_history": main_history_json, "main_history_sha256": json_hash(main_history_json),
                "generation_fixture_sha256": pipeline.fixture_sha256,
                "auxiliary_requests": copy.deepcopy(captured[arm].calls[before:]),
                "expression_segment": homeostatic_segment(prepared.context),
                "non_emotion_context_sha256": context_hash_without_homeostasis(prepared.context),
                "main_calls": prepared.main_calls, "final": prepared.final.model_dump(mode="json"),
                "successful_model_guidance": evidence["guidance"]["status"] == "model",
                "successful_weight_intervention": evidence["frame"]["status"] == evidence["guidance"]["status"] == "model",
                "director_role_review": "not_scored", "quality_scored": False, "release_eligible": False})
    return records


def _run_multiturn(output_dir: Path, *, settings, client_factory, live_aux=False, live_main=False,
                   scenario_ids=None, main_request_options=None) -> dict:
    if live_main and not live_aux:
        raise ValueError("live main evaluation requires explicit live auxiliary mode")
    scenarios = [scenario for scenario in frozen_scenarios()
                 if scenario_ids is None or scenario["scenario_id"] in scenario_ids]
    if not scenarios or (scenario_ids is not None and set(scenario_ids) - {item["scenario_id"] for item in scenarios}):
        raise ValueError("unknown or empty multi-turn scenario selection")
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": "emotion-multiturn-comparison-v2", "live": live_aux,
        "emotion_prompt_version": EMOTION_PROMPT_VERSION,
        "emotion_validation_version": GUIDANCE_VALIDATION_VERSION,
        "emotion_response_format": {"type": "json_schema", "strict": True, "validation": "pydantic"},
        "live_aux": live_aux, "live_main": live_main,
        "release_eligible": False, "arms": ARMS, "fixed_at": FIXED_AT.isoformat(), "scenarios": scenarios,
        "persona_sha256": load_persona_fixture(DEFAULT_PERSONA_PATH)[2],
        "models": {"main": settings.llm_model, "aux": settings.aux_llm_model},
        "main_endpoint": settings.llm_base_url,
        "main_request_options": {"temperature": .1, "max_tokens": 240, **(main_request_options or {})},
        "aux_endpoint": settings.aux_llm_base_url,
        "budgets_seconds": {"appraisal": settings.emotion_appraisal_timeout_seconds,
                            "guidance": settings.emotion_guidance_timeout_seconds},
        "appraisal_control": "one frozen output per turn replayed identically across arms",
        "history_control": "both emotion and main receive the same frozen user/character history; generated replies are never fed back",
        "metric_definitions": METRIC_DEFINITIONS,
        "intervention": "eval-only weights override before persisting frame; dominant source and semantic priority follow the intervened weights",
        "fixed_weight_tie_break": "production max with original key order; equal thirds select homeostasis",
        "quality_scoring": "none", "independent_human_ratings": 0}
    manifest["spec_sha256"] = json_hash(manifest)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    records = []
    for scenario in scenarios:
        clients = {arm: client_factory() for arm in ARMS}
        records.extend(evaluate_scenario(scenario, clients=clients, settings=settings))
    with (output_dir / "evidence.jsonl").open("x", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps({**record, "live": live_aux, "live_aux": live_aux, "live_main": live_main,
                                    "models": manifest["models"],
                                    "evidence_kind": "real_aux_real_main_multiturn" if live_main else
                                        "real_aux_scripted_main_multiturn" if live_aux else "scripted_offline_multiturn"},
                                    ensure_ascii=False) + "\n")
    result = {"schema_version": "emotion-multiturn-summary-v2", "spec_sha256": manifest["spec_sha256"],
        "live": live_aux, "live_aux": live_aux, "live_main": live_main, "models": manifest["models"],
        "release_eligible": False, "quality_result": "not_evaluated",
        "metric_definitions": METRIC_DEFINITIONS, "director_role_review": "not_scored",
        "independent_human_ratings": 0, "scenarios": len(scenarios), "turns_per_scenario": 5,
        "records": len(records), "main_calls": sum(len(row["main_calls"]) for row in records),
        "main_truncated_calls": sum(call.get("response_metadata", {}).get("finish_reason") == "length"
                                    for row in records for call in row["main_calls"]),
        "main_finish_reason_unavailable_calls": sum(call.get("response_metadata", {}).get("finish_reason") is None
                                                    for row in records for call in row["main_calls"]),
        "auxiliary_requests": sum(len(row["auxiliary_requests"]) for row in records),
        "auxiliary_delegate_calls": sum(call["execution"] == "delegate_call"
            for row in records for call in row["auxiliary_requests"]),
        "successful_appraisal_records": sum(row["emotion_evidence"]["frame"]["status"] == "model" for row in records),
        "successful_guidance_records": sum(row["successful_model_guidance"] for row in records),
        "successful_weight_intervention_records": sum(row["successful_weight_intervention"] for row in records)}
    (output_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _scripted_main():
    return MockLLM(chat_routes={DIALOGUE_BRIEF: "先把事情讲清楚。", NEED_BRIEF: "先理最要紧的一项吧。",
        BALANCED_BRIEF: "嗯，我们先理一项。", "": "嗯，先整理一项吧。"})


def run_offline_multiturn(output_dir: Path) -> dict:
    return _run_multiturn(output_dir, settings=offline_settings("qwen-scripted"),
                          client_factory=lambda: RoutingLLMClient(_scripted_main(), _ScriptedAux()))


def run_live_aux_multiturn(output_dir: Path, *, base_url: str, model: str, api_key="not-needed",
                           scenario_ids=None, live_main_config: Path | None = None):
    endpoint = urlsplit(base_url)
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("aux endpoint must be an explicit HTTP(S) URL without credentials, query or fragment")
    if not model.strip():
        raise ValueError("aux model is required")
    settings = offline_settings("qwen-scripted").model_copy(update={"aux_llm_base_url": base_url,
        "aux_llm_model": model, "aux_llm_api_key": api_key})
    if live_main_config is not None:
        from gui.persist import load_gui_settings

        config_path = Path(live_main_config)
        if not config_path.is_file():
            raise ValueError("explicit live main configuration must already exist")
        configured = load_gui_settings(config_path)
        main_url = urlsplit(configured.base_url or "")
        if (main_url.scheme not in {"http", "https"} or not main_url.hostname or main_url.username
                or main_url.password or main_url.query or main_url.fragment or not (configured.model or "").strip()):
            raise ValueError("explicit live main configuration requires a model and HTTP(S) endpoint without embedded credentials")
        settings = settings.model_copy(update={"llm_base_url": configured.base_url, "llm_model": configured.model,
                                              "llm_api_key": configured.api_key or "not-needed"})
    aux_settings = settings.model_copy(update={"llm_base_url": base_url, "llm_model": model,
                                             "llm_api_key": api_key})
    transport = []
    main_options = deepseek_low_reasoning_options(settings) if live_main_config is not None else {}
    main_client = None
    # The production observer captures serialized HTTP bodies and visible model
    # content, without authorization headers or hidden reasoning text.
    auxiliary = OpenAICompatClient(aux_settings, extra_payload=aux_runtime_payload(settings),
        request_observer=lambda item: transport.append({**copy.deepcopy(item), "eval_model_role": "aux"}))
    try:
        if live_main_config is not None:
            main_client = OpenAICompatClient(settings, extra_payload=main_options,
                request_observer=lambda item: transport.append({**copy.deepcopy(item), "eval_model_role": "main"}))
        return _run_multiturn(output_dir, settings=settings, live_aux=True, live_main=main_client is not None,
            scenario_ids=scenario_ids, main_request_options=main_options,
            client_factory=lambda: RoutingLLMClient(main_client or _scripted_main(), auxiliary))
    finally:
        auxiliary.close()
        if main_client is not None:
            main_client.close()
        if output_dir.is_dir() and transport:
            with (output_dir / "http-evidence.jsonl").open("x", encoding="utf-8") as stream:
                for item in transport:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="A new output directory")
    parser.add_argument("--live-aux", action="store_true", help="Explicitly call a real helper")
    parser.add_argument("--aux-base-url", help="Explicit auxiliary OpenAI-compatible endpoint")
    parser.add_argument("--aux-model", help="Explicit auxiliary model name")
    parser.add_argument("--aux-api-key-env", help="Read an optional key only from this explicitly named environment variable")
    parser.add_argument("--live-main-config", type=Path, help="Explicitly read this existing GUI config and call its main model; requires --live-aux")
    parser.add_argument("--scenario", action="append", choices=[item["scenario_id"] for item in frozen_scenarios()])
    args = parser.parse_args(argv)
    if args.live_aux:
        if not args.aux_base_url or not args.aux_model:
            parser.error("--live-aux requires --aux-base-url and --aux-model")
        key = os.environ[args.aux_api_key_env] if args.aux_api_key_env else "not-needed"
        result = run_live_aux_multiturn(args.out, base_url=args.aux_base_url, model=args.aux_model,
                                        api_key=key, scenario_ids=args.scenario, live_main_config=args.live_main_config)
    else:
        if any((args.aux_base_url, args.aux_model, args.aux_api_key_env, args.scenario, args.live_main_config)):
            parser.error("auxiliary options and scenario selection require explicit --live-aux")
        result = run_offline_multiturn(args.out)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
