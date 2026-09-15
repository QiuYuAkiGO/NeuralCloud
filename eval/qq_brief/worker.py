"""One isolated scenario/arm/repetition. Executed as a script to isolate harness imports."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import random
import shutil
import sqlite3
import sys
import time

from common import canonical, digest, file_hash, lexical_metrics, read, save


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument("--mode", choices=("synthetic", "live"), required=True)
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args()
    root = args.run_dir.resolve()
    if (root / f"STOP-{args.arm}").exists():
        print(f"STOPPED {args.scenario} {args.arm} before_start", flush=True)
        return
    manifest = read(root / "manifest.json")
    source = Path(manifest["sources"][args.arm]["root"]).resolve()
    sys.path.insert(0, str(source))
    # Never inherit production runtime paths/config from NC_* environment.
    for name in list(os.environ):
        if name.startswith("NC_"):
            del os.environ[name]
    job = root / "generation" / args.mode / f"{args.scenario}-r{args.repeat}-{args.arm}"
    job.mkdir(parents=True, exist_ok=True)
    os.chdir(job)
    scenario = next(s for s in read(root / "scenarios.json") if s["id"] == args.scenario)
    config = read(root / "generation-config.json")
    for filename in ("scenarios.json", "generation-config.json", "persona.json"):
        if file_hash(root / filename) != manifest["inputs"][filename]:
            raise ValueError("frozen_input_changed")
    from harness.config import Settings
    from harness.cognitive.shared_memory import ScopedQQMemoryView, SharedQQBinding
    from harness.core.inference import TokenUsage
    from harness.llm.client import ChatResult, MockLLM, build_llm_client
    from harness.memory.service import build_inmemory_service
    from harness.qq import decision
    from harness.qq.memory import (QQVerifiedProfileInput, QQVisualMemoryStore,
                                   QQVisualObservationInput, protocol_message_evidence_ref)
    from harness.qq.telemetry import QQDeadline, QQTurnTrace
    from gui.presets import load_preset_file
    from infra.trace.diagnostics import observation_scope

    loaded = Path(decision.__file__).resolve()
    if not loaded.is_relative_to(source):
        raise RuntimeError("harness_source_isolation_failed")
    random.seed(7300 + args.repeat)
    safe_tuning = config["settings"] | {
        "call_log_enabled": False, "record_interaction": False, "aux_llm_preload": False,
        "continuation_enabled": False, "max_reply_segments": 3,
        "trace_dir": job / "traces", "model_version": "live",
    }
    if args.mode == "live":
        credentials = read(config["credential_file"])
        main_key = credentials.get("api_key")
        aux_key = credentials.get("aux_api_key") or main_key
        if not main_key:
            raise ValueError("generation_credential_missing")
    else:
        main_key = aux_key = "synthetic-not-a-key"
    settings = Settings(_env_file=None, **safe_tuning,
        llm_base_url=config["main"]["base_url"], llm_model=config["main"]["model"], llm_api_key=main_key,
        aux_llm_base_url=config["aux"]["base_url"], aux_llm_model=config["aux"]["model"], aux_llm_api_key=aux_key)
    preset = load_preset_file(root / "persona.json")
    state_path = job / "working.sqlite3"
    group = "private:29999:20003" if scenario.get("private") else "10001"
    member, self_id = "20003", "29999"
    base_time = datetime.now(timezone.utc)
    simulation_ref = "qq-protocol://sha256/" + digest({"simulation": scenario["id"]})

    class SyntheticLLM(MockLLM):
        """Exercise real gates/compiler/validator, with clearly marked fake inference."""
        def __init__(self):
            super().__init__(chat_routes={"": "我会先选安静的，睡觉舒服些；预算紧就先试用。"})

        def complete_chat_result(self, messages, **kwargs):
            if decision.is_qq_gate_messages(messages):
                return ChatResult(text=canonical({"action": "speak", "reason": "合成测试有新问题。"}),
                                  usage=TokenUsage(prompt_tokens=10, completion_tokens=10, source="estimate"))
            return super().complete_chat_result(messages, **kwargs)

    def add_observation(store, text, serial, *, target=group, historical=False):
        # Historical fixtures are an hour old. Live opportunities and their
        # delivered replies use the actual clock, so the cross-turn recency
        # policy sees the same temporal situation as a running QQ conversation.
        stamp = (base_time + timedelta(seconds=-3600 + serial) if historical
                 else datetime.now(timezone.utc))
        evidence = "qq-protocol://sha256/" + digest({"scope": target, "text": text, "serial": serial})
        profile = store.record_protocol_profile(QQVerifiedProfileInput(group_id=target, member_id=member,
            display_name="合成对话者", display_name_source="qq_metadata", verification="qq_protocol_verified",
            capture_id=f"eval-profile:{serial}:{target}", observed_at=stamp,
            group_evidence=[evidence], member_evidence=[evidence])).profile
        message_id = str(10000 + serial)
        ref = protocol_message_evidence_ref(target, member, message_id)
        return store.record_protocol_observation(QQVisualObservationInput(group_id=target, member_id=member,
            profile_record_id=profile.record_id, capture_id=f"eval-event:{serial}:{target}",
            capture_item_id=f"eval-item:{serial}:{target}", observed_at=stamp, text=text,
            evidence=[evidence, ref], author_attribution="qq_protocol_verified", attribution_evidence=[evidence, ref],
            extraction_method="isolated_synthetic_protocol_fixture", message_id=message_id,
            message_id_source="qq_metadata")).observation

    def checkpoint(destination):
        with sqlite3.connect(state_path) as db, sqlite3.connect(destination) as copy:
            db.backup(copy)

    def initial_state():
        store = QQVisualMemoryStore(state_path)
        for i, text in enumerate(scenario.get("history", [])):
            add_observation(store, text, i + 1, historical=True)
        if scenario.get("wrong_scope_history"):
            add_observation(store, scenario["wrong_scope_history"], 9, target="10002", historical=True)
        if scenario.get("partial_seed"):
            obs = add_observation(store, "请分两条告诉我排查顺序。", 8, historical=True)
            segments = ["先备份配置。", "再检查网络连接，连接正常就看错误日志。"]
            seed = decision.QQReplyDecision(decision_id="qq-decision:" + "a" * 32,
                input_hash="a" * 64, created_at=base_time-timedelta(seconds=100), action="speak",
                reason="合成部分送达夹具。", reply="\n\n".join(segments), reply_segments=segments,
                decision_source="model", outcome="ready", group_id=group, self_member_id=self_id,
                trigger_member_id=member, participants=[member], source_record_ids=[obs.record_id],
                new_observation_record_ids=[obs.record_id], evidence_refs=[simulation_ref], delivery_refs=[],
                model="synthetic-seed-not-generation", persona_version=preset.persona.version, preset_id=preset.preset_id)
            with closing(decision._connect(state_path)) as db, db:
                db.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)",
                    (seed.decision_id, seed.input_hash, group, "completed", seed.model_dump_json()))
            decision.record_qq_protocol_segment_delivery(memory_db=state_path, group_id=group, self_member_id=self_id,
                decision_id=seed.decision_id, segment_index=0, segment_count=2, delivered_text=segments[0],
                evidence_refs=[simulation_ref, protocol_message_evidence_ref(group, self_id, "90001")],
                source_record_ids=[obs.record_id], protocol_message_id="90001",
                delivered_at=base_time-timedelta(seconds=90))
        checkpoint(job / "state-0.sqlite3")

    if not (job / "state-0.sqlite3").exists():
        if state_path.exists():
            raise ValueError("incomplete_initial_state_requires_manual_inspection")
        initial_state()
    transcript = []
    for index, incoming in enumerate(scenario["turns"], 1):
        if (root / f"STOP-{args.arm}").exists():
            print(f"STOPPED {args.scenario} {args.arm} before_turn={index}", flush=True)
            return
        result_path = job / f"turn-{index}.json"
        if result_path.exists():
            prior = read(result_path)
            if prior["status"] == "ok":
                transcript = prior["transcript"]
                continue
            if not args.retry_errors:
                print(f"BLOCKED {args.scenario} {args.arm} turn={index} {prior['status']}", flush=True)
                return
        marker = job / f"turn-{index}.inflight.json"
        if marker.exists() and not args.retry_errors:
            print(f"BLOCKED {args.scenario} {args.arm} turn={index} interrupted_request", flush=True)
            return
        if result_path.exists() or marker.exists():
            archive = job / "prior-attempts" / f"{index}-{time.time_ns()}"
            archive.mkdir(parents=True)
            for path in (result_path, marker, job / f"turn-{index}.http.jsonl"):
                if path.exists():
                    shutil.copy2(path, archive / path.name)
        shutil.copy2(job / f"state-{index-1}.sqlite3", state_path)
        store = QQVisualMemoryStore(state_path)
        observations = [add_observation(store, text, index * 10 + i) for i, text in enumerate(incoming["messages"])]
        requests = []
        http_path = job / f"turn-{index}.http.jsonl"
        http_path.write_text("", encoding="utf-8")

        def sink(event):
            # DiagnosticObserver already excludes credentials and hidden reasoning.
            requests.append(event)
            with http_path.open("a", encoding="utf-8") as stream:
                stream.write(canonical(event) + "\n")

        client = SyntheticLLM() if args.mode == "synthetic" else build_llm_client(settings)
        memory = build_inmemory_service(settings=settings, llm=client)
        memory.bootstrap_persona(preset.persona)
        binding = SharedQQBinding(settings, preset, client,
            ScopedQQMemoryView(memory, group_id=group, member_id=member), {}, (), None)
        trace = QQTurnTrace()
        aliases = [decision.QQVerifiedSelfAlias(group_id=group, self_member_id=self_id, alias="Misty",
                    evidence_refs=[simulation_ref], verification="qq_protocol_verified")]
        save(marker, {"started_at": datetime.now(timezone.utc).isoformat(), "source_root": str(source),
                      "mode": args.mode, "simulation_only": True, "input_sha256": digest(incoming)})
        started = time.monotonic()
        previous = list(transcript)
        transcript.extend({"role": "user", "text": text} for text in incoming["messages"])
        outcome = None
        try:
            with observation_scope(sink):
                outcome = decision.decide_qq_reply(memory_db=state_path, group_id=group, self_member_id=self_id,
                    trigger_member_id=member, shared_owner=binding, verified_self_aliases=aliases,
                    recent_limit_per_member=20, history_limit_per_member=5,
                    deadline=QQDeadline.after(60), trace=trace)
            if outcome.outcome not in {"ready", "natural_wait", "guard_wait"}:
                raise RuntimeError("generation_outcome_" + outcome.outcome)
            texts = decision.effective_reply_segments(outcome)
            receipt_ids, refs = [], []
            for segment_index, text in enumerate(texts):
                stamp = datetime.now(timezone.utc)
                protocol_id = str(90000 + index * 10 + segment_index)
                ref = "qq-protocol://sha256/" + digest({"mode": "simulated_delivery", "text": text, "turn": index})
                receipt = decision.record_qq_protocol_segment_delivery(memory_db=state_path, group_id=group,
                    self_member_id=self_id, decision_id=outcome.decision_id, segment_index=segment_index,
                    segment_count=len(texts), delivered_text=text,
                    evidence_refs=[ref, protocol_message_evidence_ref(group, self_id, protocol_id)],
                    source_record_ids=[o.record_id for o in observations], protocol_message_id=protocol_id,
                    delivered_at=stamp)
                receipt_ids.append(protocol_id)
                refs.extend(receipt.evidence_refs)
            if texts:
                decision.record_qq_protocol_delivery(memory_db=state_path, group_id=group, self_member_id=self_id,
                    decision_id=outcome.decision_id, delivered_text=outcome.reply, evidence_refs=sorted(set(refs)),
                    protocol_message_ids=receipt_ids, source_record_ids=[o.record_id for o in observations], delivered_at=stamp)
                transcript.append({"role": "assistant", "text": outcome.reply, "delivery": "simulated_complete"})
            checkpoint(job / f"state-{index}.sqlite3")
            status = "ok"
        except Exception as exc:
            status = "generation_error"
            error = {"type": type(exc).__name__, "outcome": getattr(outcome, "outcome", None),
                     "error_type": getattr(outcome, "error_type", None)}
        finally:
            memory.close()
            close = getattr(client, "close", None)
            if callable(close):
                close()
        result = {"schema_version": "qq-brief-turn-v1", "scenario_id": scenario["id"], "turn": index,
            "arm": args.arm, "repeat": args.repeat, "mode": args.mode, "simulation_only": True,
            "status": status, "input": incoming, "facts": scenario.get("facts", []),
            "history_before": previous, "transcript": transcript, "reply": getattr(outcome, "reply", None),
            "decision": outcome.model_dump(mode="json") if outcome else None, "trace": trace.snapshot(),
            "latency_ms": round((time.monotonic()-started)*1000, 3),
            "model_requests": sum(x.get("record_type") == "http_request_prepared" for x in requests),
            "response_models": sorted({x["response_model"] for x in requests if x.get("response_model")}),
            "http_evidence_sha256": file_hash(http_path), "input_sha256": digest(incoming),
            "metrics": lexical_metrics(getattr(outcome, "reply", None) or ""),
            "source_manifest_sha256": digest(manifest["sources"][args.arm]["files"])}
        if status != "ok":
            result["error"] = error
        save(result_path, result)
        marker.unlink(missing_ok=True)
        print(f"DONE {scenario['id']} r{args.repeat} {args.arm} turn={index} {status}", flush=True)
        if status != "ok":
            return


if __name__ == "__main__":
    main()
