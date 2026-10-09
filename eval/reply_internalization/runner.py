"""Freeze, interleave, blind and assess. No live promotion and no QQ sending."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import random
import subprocess
import sys

from .common import digest, file_hash, read, save, source_hashes
from .scenarios import scenarios


def freeze(args):
    root = args.run_dir.resolve()
    if root.exists():
        raise ValueError("run_directory_exists")
    root.mkdir(parents=True)
    config = read(args.config) if args.config else {"main": {"base_url": "https://synthetic.invalid/v1", "model": "synthetic"},
        "aux": {"base_url": "https://synthetic.invalid/v1", "model": "synthetic"},
        "settings": {"emotion_mode": "on", "validator_enabled": True, "validator_sample_rate": 1.0,
            "qq_body_pipeline": "direct", "qq_semantic_alignment_mode": "enforce"}, "credential_file": None,
        "production_snapshot_sha256": None}
    if any("api_key" in key for key in config):
        raise ValueError("credentials_must_stay_in_private_file")
    from harness.compiler.expression import EXPRESSION_RULES
    config["expression_rules"] = EXPRESSION_RULES
    frozen = scenarios()
    if args.ablation:
        frozen = [s for s in frozen if s["id"] in {"v01_incident", "v03_colors", "v07_manga_premise"}]
    save(root / "scenarios.json", frozen)
    save(root / "generation-config.json", config)
    save(root / "persona.json", read(config.get("persona_file") or args.baseline_root / "presets/builtin/misty.json"))
    arms = ["baseline", "prompt_only", "candidate"] if args.ablation else ["baseline", "candidate"]
    sources = {arm: {"root": str((args.baseline_root if arm == "baseline" else args.candidate_root).resolve())}
               for arm in arms}
    for value in sources.values():
        value["files"] = source_hashes(value["root"])
        if not value["files"]:
            raise ValueError("source_missing")
    save(root / "manifest.json", {"version": "expression-eval-v1", "sources": sources, "arms": arms,
        "created_at": datetime.now(timezone.utc).isoformat(), "repeats": 2, "turns": 3, "ablation": args.ablation,
        "scenarios": [s["id"] for s in frozen], "seed": 10926,
        "inputs": {name: file_hash(root / name) for name in ("scenarios.json", "generation-config.json", "persona.json")},
        "evaluator": {p.name: file_hash(p) for p in Path(__file__).parent.glob("*.py")},
        "limitations": ["Frozen vision/web observations and emotion guidance; real image tools tested separately.",
            "v01 reconstructs the observed incident; not a byte-identical historical replay.",
            "Later turns include each arm's generated history; ablation attribution uses turn 1 only."]})
    print(f"Frozen {len(frozen)*3*2} turns per arm; {', '.join(arms)}")


def run(args):
    root = args.run_dir.resolve()
    manifest = read(root / "manifest.json")
    for name, expected in manifest["evaluator"].items():
        if file_hash(Path(__file__).with_name(name)) != expected:
            raise ValueError("evaluator_changed_choose_new_run")
    if args.mode == "live" and not read(root / "generation-config.json").get("credential_file"):
        raise ValueError("live_credentials_not_configured")
    randomizer = random.Random(manifest["seed"])
    jobs = [(s, r) for s in manifest["scenarios"] for r in (1, 2)]
    randomizer.shuffle(jobs)
    tasks = []
    for scenario, repeat in jobs:
        arms = list(manifest["arms"])
        randomizer.shuffle(arms)
        tasks.extend((scenario, repeat, arm) for arm in arms)
    def launch(task):
        if (root / "STOP").exists():
            return
        scenario, repeat, arm = task
        job = root / "generation" / args.mode / f"{scenario}-r{repeat}-{arm}"
        if job.exists():
            return  # Keep all partial/error evidence; assess detects incompleteness.
        command = [sys.executable, "-X", "utf8", str(Path(__file__).with_name("worker.py")),
            "--run-dir", str(root), "--mode", args.mode, "--scenario", scenario, "--repeat", str(repeat), "--arm", arm]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=230)
        log = root / "worker-logs" / f"{scenario}-r{repeat}-{arm}-{args.mode}.txt"
        log.parent.mkdir(exist_ok=True)
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
        print(f"{scenario} r{repeat} {arm}: exit={result.returncode}", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(launch, tasks))


def packets(args):
    root = args.run_dir.resolve()
    manifest = read(root / "manifest.json")
    cases = {s["id"]: s for s in read(root / "scenarios.json")}
    output = root / ("blind-" + args.mode)
    if output.exists():
        raise ValueError("blind_packet_already_exists")
    randomizer, mapping = random.Random(manifest["seed"] + 1), {}
    for scenario in manifest["scenarios"]:
        for repeat in (1, 2):
            arms = ["baseline", "candidate"]
            randomizer.shuffle(arms)
            key = digest({"scenario": scenario, "repeat": repeat, "mode": args.mode})[:16]
            sides, paths = {}, {}
            for label, arm in zip(("A", "B"), arms, strict=True):
                sides[label], paths[label] = [], []
                for turn in (1, 2, 3):
                    path = root / "generation" / args.mode / f"{scenario}-r{repeat}-{arm}" / f"turn-{turn}.json"
                    record = read(path) if path.exists() else {"status": "missing"}
                    sides[label].append({k: record.get(k) for k in ("input", "history_before", "reply", "status")})
                    paths[label].append({"path": str(path.relative_to(root)), "sha256": file_hash(path) if path.exists() else None})
            packet = {"id": key, "sides": sides, "fixture": cases[scenario], "rubric": "结合当前问题和证据判断，不使用关键词黑名单。"
                "每轮评估不必要免责声明/规则直述/重复边界、必要限定、事实越界和已知身份识别。"
                "以内容有用、自然且不越界为成对偏好依据；失败和未知单列。"}
            save(output / (key + ".json"), packet)
            mapping[key] = {"arms": dict(zip(("A", "B"), arms, strict=True)), "records": paths,
                "packet_sha256": file_hash(output / (key + ".json"))}
    save(root / ("blind-key-" + args.mode + ".json"), mapping)
    print(f"Created {len(mapping)} blind pairs; mapping is stored separately.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("freeze")
    p.add_argument("--baseline-root", type=Path, required=True)
    p.add_argument("--candidate-root", type=Path, required=True)
    p.add_argument("--config", type=Path)
    p.add_argument("--ablation", action="store_true")
    p = subs.add_parser("run")
    p.add_argument("--workers", type=int, choices=(1, 2, 3, 4), default=1)
    subs.add_parser("packets")
    p = subs.add_parser("assess")
    p.add_argument("--reviews", type=Path)
    for p in subs.choices.values():
        p.add_argument("--run-dir", type=Path, required=True)
        if p.prog.split()[-1] != "freeze":
            p.add_argument("--mode", choices=("synthetic", "live"), default="synthetic")
    args = parser.parse_args()
    if args.command == "assess":
        from .acceptance import assess
        result = assess(args.run_dir, args.mode, args.reviews)
        save(args.run_dir / ("acceptance-" + args.mode + ".json"), result)
        print(result)
    else:
        {"freeze": freeze, "run": run, "packets": packets}[args.command](args)


if __name__ == "__main__":
    main()
