"""Replay a frozen post-vision incident prefix in three arms, without tool calls or QQ sends.

The original non-system messages, source evidence, character snapshot and emotion
guidance are byte/hash identical across arms. Current baseline planning rules replace
the historical planning-rule prefix; tool outputs and history are never regenerated.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("run-dir", "fixture", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "prompt_only", "candidate"), required=True)
    args = parser.parse_args()
    def read(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))
    manifest, config = read(args.run_dir / "manifest.json"), read(args.run_dir / "generation-config.json")
    source = Path(manifest["sources"][args.arm]["root"])
    sys.path.insert(0, str(source))
    for key in list(os.environ):
        if key.startswith("NC_"):
            del os.environ[key]
    from harness.config import Settings
    from harness.core.inference import MemoryEvidenceEntry
    from harness.core.models import CharacterSnapshot
    from harness.llm.client import build_llm_client
    from harness.qq import decision as qq, semantic_alignment as alignment
    from harness.qq.policy import QQLatencyOptions
    from harness.qq.telemetry import QQDeadline, QQTurnTrace, qq_trace_scope
    from gui.presets import load_preset_file
    from infra.trace.diagnostics import observation_scope
    from eval.qq_brief.common import canonical, digest, file_hash, save, source_hashes
    if source_hashes(source) != manifest["sources"][args.arm]["files"]:
        raise ValueError("frozen_source_changed")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixture = read(args.fixture)
    snapshot = fixture["snapshot"]
    messages = deepcopy(snapshot["messages"])
    if "vision_tool_result" not in messages[-1]["content"]:
        raise ValueError("fixture_must_freeze_post_vision_prefix")
    group = snapshot["decision"]["group_id"]
    account = snapshot["decision"]["self_member_id"]
    historical_mode = (snapshot.get("response_semantics") or {}).get("mode", "off")
    mode = config["settings"]["qq_semantic_alignment_mode"]
    old = qq._gate_rules(semantic_mode=historical_mode)
    if messages[0]["content"].count(old) != 1:
        raise ValueError("historical_planning_rules_changed_requires_explicit_mapping")
    messages[0]["content"] = messages[0]["content"].replace(old, qq._gate_rules(semantic_mode=mode), 1)
    extra = {}
    if args.arm != "baseline":
        from harness.compiler.expression import EXPRESSION_RULES, GATE_RULES
        messages[0]["content"] += EXPRESSION_RULES
        if args.arm == "candidate":
            messages[0]["content"] += GATE_RULES
            extra = {"reply_internalization_mode": "enforce", "reply_internalization_conversations": None}
    keys = read(config["credential_file"])
    settings = Settings(_env_file=None, **(config["settings"] | extra | {"trace_dir": output / "traces",
        "qq_semantic_alignment_conversations": None, "qq_latency_conversations": None,
        "call_log_enabled": True, "record_interaction": False, "continuation_enabled": False}),
        llm_base_url=config["main"]["base_url"], llm_model=config["main"]["model"], llm_api_key=keys["api_key"],
        aux_llm_base_url=config["aux"]["base_url"], aux_llm_model=config["aux"]["model"],
        aux_llm_api_key=keys.get("aux_api_key") or keys["api_key"])
    preset = load_preset_file(args.run_dir / "persona.json")
    client = build_llm_client(settings)
    trace, deadline = QQTurnTrace(), QQDeadline.after(60)
    llm = qq._QQScopedLLM(client, deadline=deadline, trace=trace, timeout=settings.llm_timeout,
        policy=QQLatencyOptions.from_settings(settings), reasoning_options=qq.deepseek_low_reasoning_options(settings))
    events = []
    def sink(event):
        events.append(event)
        with (output / "requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(canonical(event) + "\n")
    fixed = {k: snapshot.get(k) for k in ("memory_evidence", "character_snapshot", "emotion_guidance", "homeostatic_interoception")}
    fixed["non_system_messages"] = [m for m in messages if m["role"] != "system"]
    result = {"arm": args.arm, "fixture_sha256": file_hash(args.fixture), "fixed_input_sha256": digest(fixed),
              "system_sha256": hashlib.sha256(messages[0]["content"].encode()).hexdigest(), "external_messages": 0}
    start = time.monotonic()
    try:
        with observation_scope(sink), qq_trace_scope(trace):
            schema = qq._gate_schema(include_semantics=mode == "enforce", **({"include_expression": True} if args.arm == "candidate" else {}))
            response = llm.complete_chat_result(messages, temperature=.2, stream=False, max_tokens=max(8192, settings.llm_json_max_tokens),
                response_format=qq.decision_format(client, schema, "qq_gate"))
            choice = qq._parse_gate_response(response, trace)
            result["plan"] = choice.model_dump(mode="json")
            if choice.action == "speak":
                entries = [MemoryEvidenceEntry.model_validate(e) for e in snapshot["memory_evidence"]]
                semantics = alignment.task_context(mode=mode, audience=qq.conversation_audience(group),
                    trigger_ids=(snapshot.get("response_semantics") or {}).get("trigger_source_ids", []), choice=choice, entries=entries)
                kwargs = {}
                if args.arm == "candidate":
                    from harness.core.expression import qq_context
                    kwargs["response_expression"] = qq_context(settings, group_id=group, self_member_id=account,
                        plan=choice.reply_plan, style_window=snapshot["selected_payload"].get("delivered_style_window"))
                llm.stage_name = "persona"
                final = qq._prepare_group_speech(settings=settings, preset=preset, llm=llm,
                    payload=canonical({"assessment": choice.model_dump(mode="json")}), group_id=group,
                    evidence_refs=snapshot.get("body_evidence_refs", []), memory_evidence_entries=entries,
                    current_speaker_id="qq:member:"+snapshot["decision"]["trigger_member_id"],
                    prefix_messages=[*messages, {"role": "assistant", "content": choice.model_dump_json()}],
                    homeostatic_interoception=snapshot.get("homeostatic_interoception"), emotion_guidance=snapshot.get("emotion_guidance"),
                    character_snapshot=CharacterSnapshot.model_validate(snapshot["character_snapshot"]) if snapshot.get("character_snapshot") else None,
                    response_semantics=semantics, **kwargs)
                result["final"] = final.model_dump(mode="json")
            result["status"] = "completed"
    except Exception as exc:
        result.update(status="error", error_type=type(exc).__name__)
    finally:
        client.close()
    result.update(latency_ms=(time.monotonic()-start)*1000, trace=trace.snapshot(),
        model_requests=sum(e.get("record_type") == "http_request_prepared" for e in events))
    save(output / "result.json", result)
    print(canonical({k: result.get(k) for k in ("arm", "status", "fixed_input_sha256", "model_requests", "error_type")}))


if __name__ == "__main__":
    main()
