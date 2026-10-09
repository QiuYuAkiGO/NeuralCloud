"""Paired body replay from identical captured gates, preserving every user/evidence message."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time

from eval.qq_brief.common import canonical, file_hash, read, save, source_hashes
from .adapters import install, source_contexts

ARMS = ("keep_plan", "source_only")


def body_projection(kwargs):
    """Remove generated planning prose only; retain policies, routing and original sources."""
    result = source_contexts(kwargs)

    def project(value):
        return {key: item for key, item in value.items()
                if key not in {"reply_plan", "reason", "reply_style_reason", "attention_assessment"}}

    messages = deepcopy(kwargs["prefix_messages"])
    for message in messages:
        if message["role"] == "assistant":
            try:
                value = json.loads(message["content"])
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("action") in {"speak", "wait"} and "reason" in value:
                message["content"] = canonical(project(value))
    payload = json.loads(kwargs["payload"])
    if isinstance(payload.get("assessment"), dict):
        payload["assessment"] = project(payload["assessment"])
    result.update(prefix_messages=messages, payload=canonical(payload))
    assert [m for m in messages if m["role"] in {"user", "system"}] == [
        m for m in kwargs["prefix_messages"] if m["role"] in {"user", "system"}]
    return result


def freeze(args):
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    original = args.captured_root.resolve()
    config = read(original / "factorial" / "generation-config.json")
    save(root / "config.json", config)
    save(root / "persona.json", read(original / "factorial" / "persona.json"))
    cases, origins = [], []
    for p in sorted((original / "factorial" / "generation" / "live").glob("*-control/turn-1.checkpoint.json")):
        scenario, repeat, _ = p.parent.name.rsplit("-", 2)
        if scenario.startswith("v04_"):
            p = original / "receipts" / "generation" / "live" / f"{scenario}-{repeat}-cards" / p.name
        checkpoint, record = read(p), read(p.with_name("turn-1.json"))
        key = scenario + "-" + repeat
        save(root / "captures" / (key + ".json"), {"snapshot": checkpoint["snapshot"], "input": record["input"]})
        cases.append(key)
        origins.append({"key": key, "path": str(p), "sha256": file_hash(p), "record_sha256": file_hash(p.with_name("turn-1.json"))})
    if len(cases) != 32:
        raise ValueError("expected_32_captured_gates")
    save(root / "manifest.json", {
        "version": "body-source-projection-v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "arms": list(ARMS), "cases": cases, "expected_trials": len(cases) * 2, "seed": 10912,
        "runtime": {"root": str(args.source_root.resolve()), "files": source_hashes(args.source_root)},
        "origins": origins, "inputs": {str(p.relative_to(root)): file_hash(p) for p in root.rglob("*.json")},
        "evaluator": {str(p.resolve().relative_to(Path.cwd().resolve())): file_hash(p)
                      for folder in (Path(__file__).parent, Path("eval/qq_brief")) for p in folder.glob("*.py")},
        "hypothesis": "Keeping identical original sources and all policies, remove only gate-generated planning prose from body inputs.",
        "common_to_both_arms": ["Source-only critic from the separate mechanism probe.", "Actual captured gate, tool results, persona, emotion and user messages.", "v04 failure snapshots use prior receipt experiment to preserve host failure evidence."],
        "limitations": ["Development replay; no new gate/tool selection or QQ transport, not an end-to-end release test.",
                        "Two captured gates per scenario, one generated replay per arm; not independent repetitions of full conversations.",
                        "Normalness/usefulness require independent human review; no automatic preference winner.",
                        "All policies and persona are retained, unlike the earlier whole-handoff replacement."],
        "external_qq_messages": 0})
    print("Frozen 64 body replays.", flush=True)


def worker(args):
    root = args.run_dir.resolve()
    manifest, config = read(root / "manifest.json"), read(root / "config.json")
    for name, expected in manifest["inputs"].items():
        if file_hash(root / name) != expected:
            raise ValueError("frozen_replay_input_changed")
    for name, expected in manifest["evaluator"].items():
        if file_hash(name) != expected:
            raise ValueError("frozen_replay_evaluator_changed")
    source = Path(manifest["runtime"]["root"])
    if source_hashes(source) != manifest["runtime"]["files"]:
        raise ValueError("frozen_replay_runtime_changed")
    sys.path.insert(0, str(source))
    for key in list(os.environ):
        if key.startswith("NC_"):
            del os.environ[key]
    from harness.config import Settings
    from harness.core.expression import ResponseExpressionContext
    from harness.core.inference import MemoryEvidenceEntry, ResponseSemanticContext
    from harness.core.models import CharacterSnapshot
    from harness.llm.client import build_llm_client
    from harness.qq import decision
    from harness.qq.telemetry import QQDeadline, QQTurnTrace, qq_trace_scope
    from infra.trace.diagnostics import observation_scope
    from gui.presets import load_preset_file

    install("source_only")
    capture = read(root / "captures" / (args.case + ".json"))
    snapshot = capture["snapshot"]
    job = root / "trials" / (args.case + "-" + args.arm)
    job.mkdir(parents=True, exist_ok=False)
    keys = read(config["credential_file"])
    tuning = config["settings"] | snapshot["policy"] | {
        "call_log_enabled": True, "record_interaction": False, "trace_dir": job / "traces",
        "context_token_budget": snapshot["context_token_budget"], "max_reply_segments": 3, "continuation_enabled": False,
        "reply_internalization_mode": "enforce", "reply_internalization_conversations": None}
    settings = Settings(_env_file=None, **tuning, llm_base_url=config["main"]["base_url"], llm_model=config["main"]["model"],
        llm_api_key=keys["api_key"], aux_llm_base_url=config["aux"]["base_url"], aux_llm_model=config["aux"]["model"],
        aux_llm_api_key=keys.get("aux_api_key") or keys["api_key"])
    client = build_llm_client(settings)
    trace = QQTurnTrace()
    llm = decision._QQScopedLLM(client, deadline=QQDeadline.after(45), trace=trace, timeout=settings.llm_timeout,
                              policy=decision.QQLatencyOptions.model_validate(snapshot["policy"]),
                              reasoning_options=decision.deepseek_low_reasoning_options(settings))
    llm.stage_name = "persona"
    llm.stable_messages = snapshot["messages"][:-1]
    payload = {"assessment": snapshot["choice"]}
    web = capture["input"].get("web")
    if web is not None:
        payload["web_context"] = web
    kwargs = dict(settings=settings, preset=load_preset_file(root / "persona.json"), llm=llm,
        payload=canonical(payload), evidence_refs=snapshot["body_evidence_refs"], group_id=snapshot["decision"]["group_id"],
        homeostatic_interoception=snapshot["homeostatic_interoception"], emotion_guidance=snapshot["emotion_guidance"],
        memory_evidence_entries=[MemoryEvidenceEntry.model_validate(e) for e in snapshot["memory_evidence"]],
        runtime_web_context=web, current_speaker_id="qq:member:" + snapshot["decision"]["trigger_member_id"],
        response_expression=ResponseExpressionContext.model_validate(snapshot["response_expression"]) if snapshot["response_expression"] else None,
        response_semantics=ResponseSemanticContext.model_validate(snapshot["response_semantics"]) if snapshot["response_semantics"] else None,
        character_snapshot=CharacterSnapshot.model_validate(snapshot["character_snapshot"]) if snapshot["character_snapshot"] else None,
        prefix_messages=[*snapshot["messages"], {"role": "assistant", "content": snapshot.get("gate_body_text", snapshot["raw_gate"])}, *snapshot["body_extra_messages"]])
    if args.arm == "source_only":
        kwargs = body_projection(kwargs)
    events, started = [], time.monotonic()

    def sink(event):
        events.append(event)
        with (job / "http.jsonl").open("a", encoding="utf-8") as f:
            f.write(canonical(event) + "\n")

    result = {"key": args.case, "arm": args.arm, "status": "error", "capture_sha256": file_hash(root / "captures" / (args.case + ".json"))}
    try:
        with observation_scope(sink), qq_trace_scope(trace):
            final = decision._prepare_group_speech(**kwargs)
        result.update(status="ok" if final.text.strip() and final.validation and final.validation.verdict == "pass" else "rejected",
                      final=final.model_dump(mode="json"))
    except Exception as exc:
        import traceback
        result.update(error_type=type(exc).__name__, frames=[{"file": Path(f.filename).name, "line": f.lineno}
                     for f in traceback.extract_tb(exc.__traceback__)])
    finally:
        client.close()
    result.update(latency_ms=(time.monotonic() - started) * 1000,
        model_requests=sum(e.get("record_type") == "http_request_prepared" for e in events),
        http_sha256=file_hash(job / "http.jsonl") if (job / "http.jsonl").exists() else None)
    save(job / "result.json", result)
    if any(e.get("http_status") in {401, 402, 403, 429} for e in events):
        save(root / "STOP", {"reason": "provider_denied"})


def run(args):
    root = args.run_dir.resolve()
    manifest = read(root / "manifest.json")
    rng = random.Random(manifest["seed"])
    cases = list(manifest["cases"])
    rng.shuffle(cases)
    jobs = []
    for key in cases:
        arms = list(ARMS)
        rng.shuffle(arms)
        jobs.extend((key, arm) for arm in arms)
    save(root / "schedule.json", jobs)

    def launch(job):
        key, arm = job
        if (root / "STOP").exists():
            return
        try:
            p = subprocess.run([sys.executable, "-X", "utf8", "-m", "eval.reply_source_authority.body_replay", "worker",
                "--run-dir", str(root), "--case", key, "--arm", arm], capture_output=True, encoding="utf-8", timeout=60)
            log = p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            log = "Worker timeout; partial data retained."
        logs = root / "worker-logs"
        logs.mkdir(exist_ok=True)
        (logs / (key + "-" + arm + ".txt")).write_text(log, encoding="utf-8")
        print(key, arm, flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(launch, jobs))
    save(root / "complete.json", {"expected": len(jobs), "finished": len(list((root / "trials").glob("*/result.json"))), "external_qq_messages": 0})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--source-root", type=Path, required=True)
    f.add_argument("--captured-root", type=Path, required=True)
    w = sub.add_parser("worker")
    w.add_argument("--case", required=True)
    w.add_argument("--arm", choices=ARMS, required=True)
    sub.add_parser("run")
    for s in sub.choices.values():
        s.add_argument("--run-dir", type=Path, required=True)
    args = p.parse_args()
    {"freeze": freeze, "worker": worker, "run": run}[args.command](args)


if __name__ == "__main__":
    main()
