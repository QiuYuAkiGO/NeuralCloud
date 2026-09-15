"""Generate a genuine legacy/current human comparison from frozen Qwen evidence.

Auxiliary responses are strictly verified replays. Both main answers are freshly
generated through the production pipeline; no reply is sent to a user channel.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from harness.core.inference import SessionTurn
from harness.emotion import EmotionInput, EmotionService
from harness.emotion.semantics import GUIDANCE_VALIDATION_VERSION
from harness.emotion.service import PROMPT_VERSION
from harness.homeostasis.models import HomeostaticInteroceptiveSummary
from harness.llm.client import OpenAICompatClient, deepseek_low_reasoning_options

from eval.emotional_expression_diagnostics.emotion import offline_settings
from eval.homeostatic_expression.evaluation import DEFAULT_PERSONA_PATH
from eval.homeostatic_expression.production import (
    ProductionPairPipeline, homeostatic_segment, json_hash, load_persona_fixture,
)


class ReplayMismatch(ValueError):
    """The current implementation no longer requests the frozen experiment."""


class VerifiedAuxReplay:
    aux_distinct = True

    def __init__(self):
        self.pending = []
        self.calls = []
        self.mismatches = []

    def bind(self, calls):
        if self.pending:
            raise ReplayMismatch("unconsumed auxiliary evidence")
        self.pending = copy.deepcopy(calls)

    def complete_json(self, *, system, user, max_tokens=None, timeout_seconds=None, response_format=None):
        if not self.pending:
            self.mismatches.append("unexpected auxiliary request")
            raise ReplayMismatch(self.mismatches[-1])
        source = self.pending.pop(0)
        actual = {"system": system, "user": json.loads(user), "max_tokens": max_tokens,
                  "timeout_seconds": timeout_seconds, "response_format": response_format}
        expected = {key: source.get(key) for key in actual}
        if actual != expected:
            self.mismatches.append("auxiliary request mismatch: " + ",".join(
                key for key in actual if actual[key] != expected[key]))
            raise ReplayMismatch(self.mismatches[-1])
        key = json_hash([system, user, response_format])
        if key != source["request_sha256"]:
            self.mismatches.append("auxiliary serialized request hash mismatch")
            raise ReplayMismatch(self.mismatches[-1])
        call = {**actual, "request_sha256": key, "stage": source["stage"],
                "execution": "verified_recorded_real_qwen_replay", "outcome": source["outcome"]}
        self.calls.append(call)
        if source["outcome"] == "error":
            call["error_type"] = source["error_type"]
            if source["error_type"] == "ReadTimeout":
                raise httpx.ReadTimeout("recorded auxiliary ReadTimeout")
            if source["error_type"] == "TimeoutError":
                raise TimeoutError("recorded auxiliary timeout")
            raise RuntimeError("recorded auxiliary failure")
        result = copy.deepcopy(source["response"])
        if json_hash(result) != source["response_sha256"]:
            self.mismatches.append("auxiliary response hash mismatch")
            raise ReplayMismatch(self.mismatches[-1])
        call.update(response=result, response_sha256=json_hash(result))
        return result

    def assert_complete(self):
        if self.pending or self.mismatches:
            raise ReplayMismatch(str(self.mismatches or "unconsumed auxiliary evidence"))


def load_source(source_dir: Path):
    manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (source_dir / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    rows = [row for row in rows if row["arm"] == "dynamic"]
    expected = [(case["scenario_id"], index + 1) for case in manifest["scenarios"]
                for index, _ in enumerate(case["turns"])]
    if [(row["scenario"], row["turn"]) for row in rows] != expected or len(rows) != 20:
        raise ValueError("require every dynamic record in the complete four-scenario source")
    if not manifest["live_aux"] or not manifest["live_main"] or manifest["emotion_prompt_version"] != PROMPT_VERSION:
        raise ValueError("source must contain matching real model evidence")
    if manifest["persona_sha256"] != load_persona_fixture(DEFAULT_PERSONA_PATH)[2]:
        raise ValueError("source persona differs from the checked-in fixture")
    return manifest, rows


def prepare_replays(source_manifest, rows):
    """Preflight every input before opening a paid main-model connection."""
    replay = VerifiedAuxReplay()
    settings = offline_settings("qwen-scripted").model_copy(update={
        "llm_model": source_manifest["models"]["main"],
        "aux_llm_model": source_manifest["models"]["aux"],
    })
    services = {}
    prepared = []
    for row in rows:
        service = services.setdefault(row["scenario"], EmotionService(settings, replay))
        request = EmotionInput.model_validate(row["emotion_evidence"]["input"])
        replay.bind(row["auxiliary_requests"])
        before = len(replay.calls)
        frame = service.observe(request)
        # Every causal state field must match. Runtime latency diagnostics are
        # expected to differ because no network request is performed here.
        actual_frame = frame.model_dump(mode="json", exclude={"diagnostics"})
        original_frame = {key: value for key, value in row["emotion_evidence"]["frame"].items()
                          if key != "diagnostics"}
        if actual_frame != original_frame:
            raise ReplayMismatch("frozen emotion frame changed")
        guidance = service.guidance(frame)
        replay.assert_complete()
        prepared.append({"request": request, "frame": frame, "guidance": guidance,
                         "auxiliary_replays": copy.deepcopy(replay.calls[before:])})
    return prepared


def describe_state(row):
    need = row["emotion_evidence"]["frame"]["homeostatic_needs"][0]
    value = need["normalized_deviation"]
    level = "严重精力不足" if value >= .9 else "明显精力不足" if value >= .6 else "中等精力不足" if value >= .4 else "轻度精力不足" if value >= .2 else "轻微精力不足"
    trend = "精力正在恢复" if need["trend"] == "recovering" else "本轮可信快照标记为稳定"
    return f"{level}（精力需求信号 {value:.2f}）；{trend}。"


def result_status(text, calls, error=None):
    if error is not None:
        return "error"
    if not text.strip():
        return "empty"
    reasons = [call.get("response_metadata", {}).get("finish_reason") for call in calls]
    if "length" in reasons:
        return "truncated"
    return "complete" if reasons and all(reason == "stop" for reason in reasons) else "completion_unverified"


def generate_side(row, frozen, *, llm, main_model, side):
    request = frozen["request"]
    summary = HomeostaticInteroceptiveSummary.model_validate(request.homeostasis)
    current = next(message for message in request.messages if message.source_id in request.new_source_ids)

    def emotion_preparer(**_kwargs):
        return request, frozen["frame"], frozen["guidance"]

    pipeline = ProductionPairPipeline(llm=llm, persona_path=DEFAULT_PERSONA_PATH,
        fixed_user_text=current.text, fixed_at=request.as_of, generator_model=main_model,
        runtime_model_version="emotion-human-before-after-v1",
        prompt_version="homeostatic-interoception-v1" if side == "before" else "qwen-emotion-v1",
        temperature=.1, max_tokens=1024, emotion_mode="off" if side == "before" else "on",
        emotion_preparer=emotion_preparer if side == "after" else None,
        session_history=[SessionTurn.model_validate(turn) for turn in row["main_history"]])
    try:
        prepared = pipeline.prepare(condition=side, event_id=current.source_id, summary=summary)
        text = prepared.final.text
        calls = prepared.main_calls
        return {"text": text, "status": result_status(text, calls),
                "finish_reason": calls[-1].get("response_metadata", {}).get("finish_reason"),
                "main_calls": calls, "final": prepared.final.model_dump(mode="json"),
                "expression_segment": homeostatic_segment(prepared.context),
                "emotion_evidence": prepared.emotion_evidence,
                "generation_fixture_sha256": pipeline.fixture_sha256}
    except Exception as exc:
        calls = copy.deepcopy(pipeline.router.main_calls)
        return {"text": "", "status": "error", "finish_reason": None,
                "error_type": type(exc).__name__, "main_calls": calls,
                "http_status": getattr(getattr(exc, "response", None), "status_code", None),
                "generation_fixture_sha256": pipeline.fixture_sha256}
    finally:
        pipeline.close()


def _json_write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run(source_dir: Path, output_dir: Path, live_main_config: Path):
    from gui.persist import load_gui_settings

    source_manifest, rows = load_source(source_dir)
    replays = prepare_replays(source_manifest, rows)
    config = load_gui_settings(live_main_config)
    url = urlsplit(config.base_url or "")
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
            or url.query or url.fragment or not (config.model or "").strip()):
        raise ValueError("explicit main configuration must contain a model and credential-free HTTP(S) URL")
    settings = offline_settings("qwen-scripted").model_copy(update={
        "llm_base_url": config.base_url, "llm_model": config.model, "llm_api_key": config.api_key or "not-needed"})
    options = deepseek_low_reasoning_options(settings)
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": "emotion-human-before-after-v1",
        "created_at": datetime.now(timezone.utc).isoformat(), "source_directory": str(source_dir.resolve()),
        "source_manifest_sha256": json_hash(source_manifest),
        "source_evidence_file_sha256": hashlib.sha256((source_dir / "evidence.jsonl").read_bytes()).hexdigest(),
        "source_arm": "dynamic", "pairs": len(rows), "live_main": True, "live_aux": False,
        "aux_execution": "strict replay of recorded real Qwen responses, including recorded failures",
        "main_model": config.model, "aux_model": source_manifest["models"]["aux"],
        "main_request_options": {"temperature": .1, "max_tokens": 1024, **options},
        "before": {"emotion_mode": "off", "prompt_version": "homeostatic-interoception-v1"},
        "after": {"emotion_mode": "on", "prompt_version": "qwen-emotion-v1",
                  "emotion_prompt_version": PROMPT_VERSION, "validation_version": GUIDANCE_VALIDATION_VERSION},
        "source_code_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in [
            Path(__file__), Path("harness/emotion/service.py"), Path("harness/emotion/semantics.py"),
            Path("harness/compiler/compiler.py"), Path("harness/validator/validator.py")]},
        "history_control": "same frozen synthetic user/character history for both sides; newly generated replies are never fed back",
        "pair_order": "before/after on odd pairs, after/before on even pairs",
        "persona_sha256": source_manifest["persona_sha256"],
        "persona": {"name": load_persona_fixture(DEFAULT_PERSONA_PATH)[0].identity.name,
                    "description": "使用固定评测角色：温柔、慢热、容易害羞、情绪细腻；各对使用同一人格与心理初值。"},
        "independent_human_ratings": 0,
        "quality_result": "not_evaluated", "release_eligible": False}
    manifest["spec_sha256"] = json_hash(manifest)
    _json_write(output_dir / "manifest.json", manifest)
    transport_context = {}
    transport = []

    def observer(item):
        transport.append({**copy.deepcopy(item), **transport_context, "eval_model_role": "main"})

    client = OpenAICompatClient(settings, extra_payload=options, request_observer=observer)
    records = []
    report = []
    try:
        with (output_dir / "evidence.jsonl").open("x", encoding="utf-8") as stream:
            for index, (row, frozen) in enumerate(zip(rows, replays, strict=True)):
                pair_id = f"P{index + 1:02d}"
                sides = {}
                order = ("before", "after") if index % 2 == 0 else ("after", "before")
                for side in order:
                    transport_context.update(pair_id=pair_id, side=side)
                    sides[side] = generate_side(row, frozen, llm=client, main_model=config.model, side=side)
                if sides["before"]["generation_fixture_sha256"] != sides["after"]["generation_fixture_sha256"]:
                    raise AssertionError("pair generation fixtures differ")
                request = frozen["request"]
                current = next(message for message in request.messages if message.source_id in request.new_source_ids)
                guidance = frozen["guidance"].model_dump(mode="json")
                record = {"pair_id": pair_id, "scenario": row["scenario"], "turn": row["turn"],
                    "input": request.model_dump(mode="json"), "main_history": row["main_history"],
                    "main_history_sha256": row["main_history_sha256"], "order": order,
                    "frozen_frame": frozen["frame"].model_dump(mode="json"), "current_guidance": guidance,
                    "auxiliary_replays": frozen["auxiliary_replays"], **sides}
                records.append(record)
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                source_case = next(case for case in source_manifest["scenarios"] if case["scenario_id"] == row["scenario"])
                brief = {side: {key: sides[side].get(key) for key in ("text", "status", "finish_reason", "error_type", "http_status")}
                         for side in ("before", "after")}
                brief["after"].update(guidance_status=guidance["status"],
                    fallback_reason=guidance["diagnostics"].get("fallback_reason"), guidance_text=guidance["text"],
                    appraisal_status=frozen["frame"].status,
                    appraisal_fallback_reason=frozen["frame"].diagnostics.get("fallback_reason"))
                report.append({"id": pair_id, "scenario": row["scenario"], "turn": row["turn"],
                    "minute": source_case["turns"][row["turn"] - 1]["minute"], "user_input": current.text,
                    "state": describe_state(row), "history": row["main_history"], **brief,
                    "provenance": {"source_arm": "dynamic", "source_turn": row["turn"],
                        "main_model": config.model, "main_execution": "fresh_real_call",
                        "aux_model": source_manifest["models"]["aux"], "aux_execution": manifest["aux_execution"],
                        "generation_fixture_sha256": sides["before"]["generation_fixture_sha256"],
                        "main_history_sha256": row["main_history_sha256"]}})
                _json_write(output_dir / "report-data.json", {"metadata": manifest, "pairs": report})
                print(f"{pair_id} {row['scenario']} turn {row['turn']}: before={sides['before']['status']} after={sides['after']['status']} guidance={guidance['status']}", flush=True)
    finally:
        client.close()
        with (output_dir / "http-evidence.jsonl").open("x", encoding="utf-8") as stream:
            for item in transport:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    summary = {"schema_version": "emotion-human-before-after-summary-v1", "pairs": len(records),
        "main_calls": sum(len(row[side]["main_calls"]) for row in records for side in ("before", "after")),
        "status_counts": {side: {status: sum(row[side]["status"] == status for row in records)
            for status in ("complete", "truncated", "empty", "error", "completion_unverified")}
            for side in ("before", "after")},
        "guidance_model": sum(row["current_guidance"]["status"] == "model" for row in records),
        "guidance_fallback": sum(row["current_guidance"]["status"] == "fallback" for row in records),
        "appraisal_model": sum(row["frozen_frame"]["status"] == "model" for row in records),
        "appraisal_fallback": sum(row["frozen_frame"]["status"] == "fallback" for row in records),
        "auxiliary_recorded_returned": sum(call["outcome"] == "returned" for row in records for call in row["auxiliary_replays"]),
        "auxiliary_recorded_errors": sum(call["outcome"] == "error" for row in records for call in row["auxiliary_replays"]),
        "verified_auxiliary_replays": sum(len(row["auxiliary_replays"]) for row in records),
        "new_auxiliary_network_calls": 0, "independent_human_ratings": 0, "quality_result": "not_evaluated",
        "release_eligible": False, "spec_sha256": manifest["spec_sha256"]}
    _json_write(output_dir / "summary.json", summary)
    _json_write(output_dir / "report-data.json", {"metadata": {**manifest, "counts": summary}, "pairs": report})
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--live-main-config", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.live_main_config.is_file():
        parser.error("the explicit live main configuration must already exist")
    print(json.dumps(run(args.source, args.out, args.live_main_config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
