"""An isolated three-turn chain. Frozen tools; no QQ transport. Failures stay recorded."""
import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from common import canonical, digest, file_hash, read, save, source_hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "prompt_only", "candidate"), required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument("--mode", choices=("synthetic", "live"), required=True)
    args = parser.parse_args()
    root = args.run_dir.resolve()
    manifest, config = read(root / "manifest.json"), read(root / "generation-config.json")
    source = Path(manifest["sources"][args.arm]["root"]).resolve()
    for name, expected in manifest["evaluator"].items():
        if file_hash(Path(__file__).with_name(name)) != expected:
            raise ValueError("frozen_evaluator_changed")
    for name, expected in manifest["inputs"].items():
        if file_hash(root / name) != expected:
            raise ValueError("frozen_input_changed: " + name)
    if source_hashes(source) != manifest["sources"][args.arm]["files"]:
        raise ValueError("frozen_source_changed")
    for name in list(os.environ):
        if name.startswith("NC_"):
            del os.environ[name]
    sys.path.insert(0, str(source))
    from harness.config import Settings
    from harness.cognitive.shared_memory import ScopedQQMemoryView, SharedQQBinding
    from harness.core.inference import TokenUsage
    from harness.llm.client import ChatResult, MockLLM, build_llm_client
    from harness.memory.service import build_inmemory_service
    from harness.qq import decision
    from harness.qq.telemetry import QQDeadline, QQTurnTrace, qq_trace_scope
    from gui.presets import load_preset_file
    from infra.trace.diagnostics import observation_scope
    from fixtures import Fixtures
    if not Path(decision.__file__).resolve().is_relative_to(source):
        raise ValueError("harness_source_isolation_failed")
    job = root / "generation" / args.mode / f"{args.scenario}-r{args.repeat}-{args.arm}"
    if job.exists():
        raise ValueError("job_exists_choose_new_frozen_run")
    job.mkdir(parents=True)
    os.chdir(job)
    scenario = next(s for s in read(root / "scenarios.json") if s["id"] == args.scenario)
    tuning = config["settings"] | {"call_log_enabled": True, "record_interaction": False,
        "aux_llm_preload": False, "continuation_enabled": False, "max_reply_segments": 3,
        "trace_dir": job / "traces", "model_version": "live", "qq_latency_conversations": None,
        "qq_semantic_alignment_conversations": None}
    if args.arm == "candidate":
        tuning.update(reply_internalization_mode="enforce", reply_internalization_conversations=None)
    credentials = read(config["credential_file"]) if args.mode == "live" else {}
    settings = Settings(_env_file=None, **tuning,
        llm_base_url=config["main"]["base_url"], llm_model=config["main"]["model"],
        llm_api_key=credentials.get("api_key", "synthetic"), aux_llm_base_url=config["aux"]["base_url"],
        aux_llm_model=config["aux"]["model"], aux_llm_api_key=credentials.get("aux_api_key") or credentials.get("api_key", "synthetic"))
    preset = load_preset_file(root / "persona.json")
    fixture = Fixtures(decision, settings, job, scenario, preset, args.arm, config)

    class Synthetic(MockLLM):
        def __init__(self):
            super().__init__(chat_routes={"": "蓝白配色很清爽。"})
            self._structured = SimpleNamespace(settings=settings)

        def complete_json(self, **kwargs):
            if "concrete_physical_evidence_available" in kwargs.get("user", ""):
                return {"coverage_complete": True, "claims": [], "violations": [], "instruction": "",
                    "alignment": {"status": "aligned", "issues": []}, "expression": {"status": "reviewed", "issues": []}}
            return super().complete_json(**kwargs)

        def complete_chat_result(self, messages, **kwargs):
            if decision.is_qq_gate_messages(messages) or (kwargs.get("response_format") and "vision_tool_result" in messages[-1]["content"]):
                request = ({"vision_request": {"image_ids": ["image_1"], "question": "当前问题所需信息"}}
                    if fixture.image is not None and "vision_tool_result" not in messages[-1]["content"] else {})
                return ChatResult(text=canonical({"action": "speak", "reason": "合成测试", **request}),
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=10, source="estimate"))
            return super().complete_chat_result(messages, **kwargs)

    transcript = []
    for index, incoming in enumerate(scenario["turns"], 1):
        if (root / "STOP").exists():
            break
        if "image" in incoming:
            fixture.image = incoming["image"]
        rows = [fixture.observation(text, 100 + index * 10 + j, image=incoming.get("image")) for j, text in enumerate(incoming["messages"])]
        calls, start = [], time.monotonic()
        http_path = job / f"turn-{index}.http.jsonl"
        http_path.write_text("", encoding="utf-8")
        def sink(event):
            calls.append(event)
            with http_path.open("a", encoding="utf-8") as stream:
                stream.write(canonical(event) + "\n")
        client = Synthetic() if args.mode == "synthetic" else build_llm_client(settings)
        memory = build_inmemory_service(settings=settings, llm=client)
        memory.bootstrap_persona(preset.persona)
        binding = SharedQQBinding(settings, preset, client,
            ScopedQQMemoryView(memory, group_id=fixture.group, member_id=fixture.member), {}, (), None)
        deadline, trace, outcome = QQDeadline.after(60), QQTurnTrace(), None
        record = {"scenario_id": scenario["id"], "category": scenario["category"], "turn": index, "repeat": args.repeat,
            "arm": args.arm, "mode": args.mode, "simulation_only": True, "evaluation_kind": "frozen_evidence_chain",
            "input": incoming, "history_before": list(transcript), "status": "generation_error", "human_review": None}
        try:
            guidance = fixture.guidance(index) if settings.emotion_mode != "off" else None
            with observation_scope(sink), qq_trace_scope(trace):
                turn_binding = {"event_id": "qq-watch-event:" + digest({"id": scenario["id"], "turn": index})[:32],
                    "transport": "desktop", "group_id": fixture.group, "self_member_id": fixture.self_id, "trigger_member_id": fixture.member,
                    "runtime_instance_id": "eval:expression", "manifest_revision": "eval:expression:v1", "source_digest": digest(incoming),
                    "deadline_at": (datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()}
                prepared = decision.prepare_qq_turn(memory_db=fixture.state, group_id=fixture.group, self_member_id=fixture.self_id,
                    trigger_member_id=fixture.member, shared_owner=binding, recent_limit_per_member=20, history_limit_per_member=5,
                    verified_self_aliases=fixture.aliases, turn_binding=turn_binding,
                    web_capabilities={"enabled": True}, deadline=deadline, trace=trace,
                    **({"emotion_guidance": guidance} if guidance is not None else {}))
                if prepared["kind"] == "prepared":
                    save(job / f"turn-{index}.checkpoint.json", decision.get_qq_turn_checkpoint(memory_db=fixture.state, prepare_id=prepared["prepare_id"]))
                    outcome = decision.resume_qq_response(memory_db=fixture.state, shared_owner=binding, turn_binding=turn_binding,
                        prepare_id=prepared["prepare_id"], snapshot_hash=prepared["snapshot_hash"],
                        runtime_web_context=incoming.get("web"), deadline=deadline, trace=trace)
                else:
                    outcome = decision.QQReplyDecision.model_validate(prepared["decision"])
            record["status"] = "ok" if outcome.outcome == "ready" else "no_reply" if outcome.outcome in {"natural_wait", "guard_wait"} else "generation_error"
            transcript.extend({"role": "user", "text": text} for text in incoming["messages"])
            if outcome.outcome == "ready":
                fixture.delivered(outcome, 20 + index, rows)
                transcript.append({"role": "assistant", "text": outcome.reply, "delivery": "simulated_complete"})
        except Exception as exc:
            # Raw exceptions can contain headers/keys; call evidence is independently sanitized.
            import traceback
            record["error"] = {"type": type(exc).__name__, "frames": [
                {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                for f in traceback.extract_tb(exc.__traceback__)]}
            if args.mode == "synthetic":
                record["error"]["synthetic_message"] = str(exc)
        finally:
            memory.close()
            close = getattr(client, "close", None)
            if callable(close):
                close()
        record.update(reply=getattr(outcome, "reply", None), decision=outcome.model_dump(mode="json") if outcome else None,
            trace=trace.snapshot(), latency_ms=round((time.monotonic()-start)*1000, 3),
            model_requests=sum(e.get("record_type") == "http_request_prepared" for e in calls),
            response_models=sorted({e["response_model"] for e in calls if e.get("response_model")}),
            fixture_tool_requests=list(fixture.requests), http_evidence_sha256=file_hash(http_path),
            input_sha256=digest(incoming), source_manifest_sha256=digest(manifest["sources"][args.arm]["files"]))
        denied = next((e.get("http_status") for e in calls if e.get("http_status") in {401, 402, 403, 429}), None)
        if denied:
            save(root / "STOP", {"reason": "provider_denied", "status": denied})
        save(job / f"turn-{index}.json", record)
        print(f"{scenario['id']} r{args.repeat} {args.arm} turn={index}: {record['status']}", flush=True)


if __name__ == "__main__":
    main()
