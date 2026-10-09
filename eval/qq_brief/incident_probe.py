"""Replay the frozen incident prefix in an isolated database, with no QQ transport.

Run as a script so baseline/candidate harness imports stay in separate processes.
This supplemental probe does not change the frozen 320-turn paired evaluation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time


VARIANTS = {
    "original": None,
    "concession": "即使他们很孤单，也不能在没人接着照护的情况下随便让他们离院。",
    "correction": "我不是在说怎么安慰他们，我是说就算没人探望，也不能随便让他们离院，照护还得继续。",
    "cause": "没人来探望也不代表能随便让他们离院，所以院里还是得继续照护，费用也就还在。",
}
EXPECTATION = ("承接不能随意让患者离院以及仍需有人照护的解释；不把解释替换为孤单或被抛弃的安慰，"
               "不反转否定、让步或因果。不要求复述、赞同观点或展开医学分析；群友说法只是本人陈述。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--variant", choices=tuple(VARIANTS), required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    def read(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))
    root = args.run_dir.resolve()
    manifest = read(root / "manifest.json")
    source = Path(manifest["sources"][args.arm]["root"])
    sys.path.insert(0, str(source))
    from eval.qq_brief.common import canonical, digest, file_hash, save
    from eval.qq_brief.runner import validate_run_inputs
    validate_run_inputs(root, arms=(args.arm,))
    for name in list(os.environ):
        if name.startswith("NC_"):
            del os.environ[name]
    output = args.output.resolve()
    fixture = read(args.fixture)
    rows = fixture["messages"][:19]
    if VARIANTS[args.variant] is not None:
        rows[-1] = rows[-1] | {"text": VARIANTS[args.variant]}
    frozen_input = {"prefix": rows, "expectation": EXPECTATION,
                    "fixture_sha256": file_hash(args.fixture), "variant": args.variant}
    if (output / "result.json").exists():
        prior = read(output / "result.json")
        if (read(output / "input.json") != frozen_input or prior["arm"] != args.arm
                or prior["source_manifest_sha256"] != digest(manifest["sources"][args.arm]["files"])):
            raise ValueError("probe_binding_changed")
        print(canonical({"reused": str(output), "model_requests_this_invocation": 0}))
        return
    output.mkdir(parents=True, exist_ok=False)
    os.chdir(output)
    save(output / "input.json", frozen_input)
    from harness.config import Settings
    from harness.cognitive.shared_memory import ScopedQQMemoryView, SharedQQBinding
    from harness.emotion import EmotionService, EmotionInput, EmotionMessage
    from harness.llm.client import build_llm_client
    from harness.memory.service import build_inmemory_service
    from harness.qq import decision
    from harness.qq.conversation import conversation_audience
    from harness.qq.memory import QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput
    from harness.qq.memory import protocol_message_evidence_ref
    from harness.qq.telemetry import QQDeadline, QQTurnTrace, qq_trace_scope
    from gui.presets import load_preset_file
    from infra.trace.diagnostics import observation_scope
    config = read(root / "generation-config.json")
    credentials = read(config["credential_file"])
    settings = Settings(_env_file=None, **(config["settings"] | {
        "record_interaction": False, "aux_llm_preload": False, "continuation_enabled": False,
        "max_reply_segments": 3, "trace_dir": output / "traces", "call_log_enabled": True,
        "qq_latency_conversations": None, "qq_semantic_alignment_conversations": None,
        "qq_semantic_alignment_mode": "enforce" if args.arm == "candidate" else "off",
    }), llm_base_url=config["main"]["base_url"], llm_model=config["main"]["model"],
        llm_api_key=credentials["api_key"], aux_llm_base_url=config["aux"]["base_url"],
        aux_llm_model=config["aux"]["model"], aux_llm_api_key=credentials.get("aux_api_key") or credentials["api_key"])
    preset = load_preset_file(root / "persona.json")
    state = output / "state.sqlite3"
    store = QQVisualMemoryStore(state)
    group, self_id = "10001", "29999"
    scope = conversation_audience(group)
    authors = {"member_1": "20003", "member_2": "20004", "misty": self_id}
    # Keep the exact order and relative timing without making the replay stale.
    shift = datetime.now(timezone.utc) - datetime.fromisoformat(rows[-1]["time"])
    observations, emotion_messages = [], []
    for index, row in enumerate(rows, 1):
        author = authors[row["author"]]
        stamp = datetime.fromisoformat(row["time"]) + shift
        ref = protocol_message_evidence_ref(group, author, str(10000 + index))
        metadata_ref = "qq-protocol://sha256/" + digest({"fixture": row["id"], "author": author})
        if row["role"] == "assistant":
            decision.record_qq_delivery(memory_db=state, group_id=group, self_member_id=self_id,
                delivered_text=row["text"], evidence_refs=[ref],
                source_record_ids=[observations[-1].record_id], verification="qq_ui_verified", delivered_at=stamp)
        else:
            profile = store.record_protocol_profile(QQVerifiedProfileInput(group_id=group, member_id=author,
                display_name=row["author"], display_name_source="qq_metadata", verification="qq_protocol_verified",
                capture_id=row["id"], observed_at=stamp, group_evidence=[metadata_ref], member_evidence=[metadata_ref])).profile
            observation = store.record_protocol_observation(QQVisualObservationInput(group_id=group, member_id=author,
                profile_record_id=profile.record_id, capture_id=row["id"], capture_item_id=row["id"],
                observed_at=stamp, text=row["text"], evidence=[metadata_ref, ref], author_attribution="qq_protocol_verified",
                attribution_evidence=[metadata_ref, ref], extraction_method="isolated_incident_fixture",
                message_id=str(10000 + index), message_id_source="qq_metadata")).observation
            observations.append(observation)
        emotion_messages.append(EmotionMessage(source_id=row["id"], scope=scope,
            role="character" if row["role"] == "assistant" else "user",
            text=row["text"], timestamp=stamp, speaker=author))
    client = build_llm_client(settings)
    memory = build_inmemory_service(settings=settings, llm=client)
    memory.bootstrap_persona(preset.persona)
    binding = SharedQQBinding(settings, preset, client,
        ScopedQQMemoryView(memory, group_id=group, member_id=authors["member_1"]), {}, (), None)
    emotion = EmotionService(settings, client)
    events, trace = [], QQTurnTrace()
    http_path = output / "requests.jsonl"
    def sink(event):
        events.append(event)
        with http_path.open("a", encoding="utf-8") as stream:
            stream.write(canonical(event) + "\n")
    started = time.monotonic()
    result = {"arm": args.arm, "variant": args.variant, "external_messages": 0,
              "source_manifest_sha256": digest(manifest["sources"][args.arm]["files"]),
              "probe_sha256": file_hash(__file__), "input_sha256": file_hash(output / "input.json"),
              "emotion_initial_state": "fresh_isolated_not_historical_snapshot"}
    try:
        with observation_scope(sink), qq_trace_scope(trace):
            deadline = QQDeadline.after(60)
            frame = emotion.observe(EmotionInput(context_id="incident-probe", scope=scope,
                as_of=datetime.now(timezone.utc), persona=preset.persona, messages=emotion_messages,
                new_source_ids=[m.source_id for m in emotion_messages if m.role == "user"],
                deadline_at=datetime.now(timezone.utc) + timedelta(seconds=deadline.remaining())))
            guidance = emotion.guidance(frame, **({"expression_only": True} if args.arm == "candidate" else {}))
            outcome = decision.decide_qq_reply(memory_db=state, group_id=group, self_member_id=self_id,
                trigger_member_id=authors["member_1"], shared_owner=binding, deadline=deadline, trace=trace,
                recent_limit_per_member=20, history_limit_per_member=5, emotion_guidance=guidance,
                verified_self_aliases=[decision.QQVerifiedSelfAlias(group_id=group, self_member_id=self_id,
                    alias="Misty", evidence_refs=[metadata_ref], verification="qq_protocol_verified")])
        result.update(decision=outcome.model_dump(mode="json"), reply=outcome.reply)
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        if callable(getattr(exc, "errors", None)):
            result["validation_errors"] = [{k: error[k] for k in ("loc", "msg", "type")}
                                           for error in exc.errors(include_input=False, include_url=False)]
    finally:
        if emotion._emotion_client is not None:
            emotion._emotion_client.close()
        memory.close()
        client.close()
    result.update(latency_ms=round((time.monotonic() - started) * 1000, 3), trace=trace.snapshot(),
        model_requests=sum(e.get("record_type") == "http_request_prepared" for e in events),
        http_sha256=file_hash(http_path) if http_path.exists() else None)
    save(output / "result.json", result)
    print(canonical({k: result.get(k) for k in ("arm", "variant", "reply", "error_type", "latency_ms", "model_requests")}))


if __name__ == "__main__":
    main()
