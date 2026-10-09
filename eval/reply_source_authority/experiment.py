"""Preregister, interleave and run fixed-candidate mechanism probes in fresh processes."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import random
import subprocess
import sys
import time

from eval.qq_brief.common import canonical, digest, file_hash, read, save, source_hashes
from .adapters import ARMS, FOLLOWUP_ARMS, install
from .cases import cases


def freeze(args):
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    config = read(args.config)
    config["fixture_clock"] = datetime.now(timezone.utc).isoformat()
    config["settings"].update(reply_internalization_mode="enforce", reply_internalization_conversations=None)
    save(root / "config.json", config)
    save(root / "cases.json", cases())
    save(root / "persona.json", read(config["persona_file"]))
    arms = list(FOLLOWUP_ARMS if args.study == "explicit_sources" else ARMS)
    repeats = 1 if args.study == "explicit_sources" else 2
    expected = len(cases()) * len(arms) * 2 * repeats
    evaluator = {str(p.resolve().relative_to(Path.cwd().resolve())).replace("\\", "/"): file_hash(p)
                 for folder in (Path(__file__).parent, Path("eval/qq_brief")) for p in folder.glob("*.py")}
    save(root / "manifest.json", {
        "version": "source-authority-v2", "study": args.study, "arms": arms, "plan_states": ["faithful", "drifted"],
        "repeats": repeats, "seed": 10911, "case_count": len(cases()), "expected_trials": expected,
        "runtime": {"root": str(args.source_root.resolve()), "files": source_hashes(args.source_root)},
        "inputs": {name: file_hash(root / name) for name in ("config.json", "cases.json", "persona.json")},
        "evaluator": evaluator,
        "hypotheses": [
            "Planner-only changes can perturb a critic even with identical question, evidence and candidate.",
            "Removing generated interpretation/constraint lists from critic inputs reduces that dependence without losing factual rejection or necessary disclosure.",
            "A single conditional example edit may explain part of the image-receipt error; compare separately from the input architecture change.",
            *(["Post hoc development follow-up: add exact admitted trigger text beside its row reference to the plan-free critic, without a generated task summary or new call."] if args.study == "explicit_sources" else [])],
        "metrics": ["accuracy including unscored trials", "false rejection of good replies", "false acceptance of bad replies", "verdict flips between plan states", "schema/provider failures", "requests and latency"],
        "limitations": ["Synthetic, development-set perturbations, not an estimate of live defect frequency.",
                        "No human preference or naturalness score. Gold labels fixed by the implementation agent.",
                        "Previous frozen v6 candidate retained to isolate the mechanism; live release has since gained JSON repair.",
                        "All image, failure and memory records are fixtures, not newly verified real-world facts.",
                        "Repeated trials and paired cases are dependent; do not treat them as independent samples."],
        "external_qq_messages": 0})
    print("Frozen", expected, "critic trials.", flush=True)


def worker(args):
    root = args.run_dir.resolve()
    manifest, config = read(root / "manifest.json"), read(root / "config.json")
    for name, expected in manifest["inputs"].items():
        if file_hash(root / name) != expected:
            raise ValueError("frozen_input_changed")
    for name, expected in manifest["evaluator"].items():
        if file_hash(Path(name)) != expected:
            raise ValueError("frozen_evaluator_changed")
    source = Path(manifest["runtime"]["root"])
    if source_hashes(source) != manifest["runtime"]["files"]:
        raise ValueError("frozen_runtime_changed")
    sys.path.insert(0, str(source))
    for key in list(os.environ):
        if key.startswith("NC_"):
            del os.environ[key]
    from harness.config import Settings
    from harness.core.inference import CandidateResponse, MemoryEvidenceContext, MemoryEvidenceEntry, ReplyUnderstanding, ResponseSemanticContext
    from harness.core.expression import ResponseExpressionContext, ReplyConstraint, DisclosureRequirement
    from harness.llm.client import build_llm_client
    from harness.validator.validator import ResponseValidator
    from infra.trace.diagnostics import observation_scope
    from gui.presets import load_preset_file

    install(args.arm)
    keys = read(config["credential_file"])
    job = root / "trials" / f"{args.case}-r{args.repeat}-{args.plan}-{args.arm}"
    job.mkdir(parents=True, exist_ok=False)
    tuning = config["settings"] | {"trace_dir": job / "traces", "call_log_enabled": True,
                                  "record_interaction": False, "validator_enabled": True, "validator_sample_rate": 1.0}
    settings = Settings(_env_file=None, **tuning, llm_base_url=config["main"]["base_url"], llm_model=config["main"]["model"],
                        llm_api_key=keys["api_key"], aux_llm_base_url=config["aux"]["base_url"], aux_llm_model=config["aux"]["model"],
                        aux_llm_api_key=keys.get("aux_api_key") or keys["api_key"])
    case = next(c for c in read(root / "cases.json") if c["id"] == args.case)
    scope = "local:main"
    evidence = MemoryEvidenceContext(audience=scope, current_speaker_id="alice", entries=[
        MemoryEvidenceEntry(source_id="trigger", text=case["question"], source_channel="qq_group", audience=scope,
                            source_conversation_id=scope, speaker_id="alice", status="user_claim", kind="current",
                            observed_at=config["fixture_clock"]),
        *[MemoryEvidenceEntry(source_id=f"evidence:{i}", audience=scope, source_conversation_id=scope,
                              observed_at=config["fixture_clock"], **entry) for i, entry in enumerate(case["evidence"])]] )
    summary = case[args.plan + "_plan"]
    task = ResponseSemanticContext(mode="enforce", audience=scope, trigger_source_ids=["trigger"],
           understanding=ReplyUnderstanding(acts=["question"], summary=summary, source_ids=["trigger"]), must_address=summary)
    expression = ResponseExpressionContext(mode="enforce", audience=scope,
        internal_constraints=[ReplyConstraint(subject=case["drifted_constraint"], handling="qualify")] if args.plan == "drifted" else [],
        disclosure_requirements=[DisclosureRequirement(subject=case["drifted_constraint"], reason="answer_material")] if args.plan == "drifted" else [])
    client = build_llm_client(settings)
    events, started = [], time.monotonic()

    def sink(event):
        events.append(event)
        with (job / "http.jsonl").open("a", encoding="utf-8") as f:
            f.write(canonical(event) + "\n")

    result = {"case_id": case["id"], "family": case["family"], "domain": case["domain"], "arm": args.arm,
              "plan": args.plan, "repeat": args.repeat, "expected_pass": case["expected_pass"], "status": "error",
              "fixed_input_hash": digest({"question": case["question"], "evidence": evidence.model_dump(mode="json"), "candidate": case["candidate"]})}
    save(job / "input.json", {"candidate": case["candidate"], "evidence": evidence.model_dump(mode="json"),
                              "semantics": task.model_dump(mode="json"), "expression": expression.model_dump(mode="json")})
    try:
        with observation_scope(sink):
            review = ResponseValidator(settings=settings, llm=client).check(CandidateResponse(text=case["candidate"]),
                    persona=load_preset_file(root / "persona.json").persona, memory_evidence=evidence,
                    response_semantics=task, response_expression=expression)
        result.update(status="ok", validation=review.model_dump(mode="json"), correct=(review.verdict == "pass") == case["expected_pass"])
    except Exception as exc:
        result["error_type"] = type(exc).__name__
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
    groups = [(c["id"], r) for c in read(root / "cases.json") for r in range(1, manifest["repeats"] + 1)]
    rng.shuffle(groups)
    jobs = []
    for key, repeat in groups:
        conditions = [(arm, state) for arm in manifest["arms"] for state in manifest["plan_states"]]
        rng.shuffle(conditions)
        jobs.extend((key, repeat, arm, state) for arm, state in conditions)
    save(root / "schedule.json", jobs)

    def launch(job):
        key, repeat, arm, state = job
        if (root / "STOP").exists():
            return
        command = [sys.executable, "-X", "utf8", "-m", "eval.reply_source_authority.experiment", "worker", "--run-dir", str(root),
                   "--case", key, "--repeat", str(repeat), "--arm", arm, "--plan", state]
        try:
            process = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=45)
            log = process.stdout + process.stderr
        except subprocess.TimeoutExpired:
            log = "Worker timeout; incomplete data retained."
        logs = root / "worker-logs"
        logs.mkdir(exist_ok=True)
        (logs / f"{key}-r{repeat}-{state}-{arm}.txt").write_text(log, encoding="utf-8")
        print(key, repeat, arm, state, flush=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(launch, jobs))
    save(root / "complete.json", {"expected": len(jobs), "finished": len(list((root / "trials").glob("*/result.json"))),
                                  "provider_stopped": (root / "STOP").exists(), "external_qq_messages": 0})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--source-root", type=Path, required=True)
    f.add_argument("--config", type=Path, required=True)
    f.add_argument("--study", choices=("original", "explicit_sources"), default="original")
    w = sub.add_parser("worker")
    w.add_argument("--case", required=True)
    w.add_argument("--arm", choices=sorted({*ARMS, *FOLLOWUP_ARMS}), required=True)
    w.add_argument("--plan", choices=("faithful", "drifted"), required=True)
    w.add_argument("--repeat", type=int, choices=(1, 2), required=True)
    sub.add_parser("run")
    for s in sub.choices.values():
        s.add_argument("--run-dir", type=Path, required=True)
    args = p.parse_args()
    {"freeze": freeze, "worker": worker, "run": run}[args.command](args)


if __name__ == "__main__":
    main()
