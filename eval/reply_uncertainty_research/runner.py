"""Pre-register and interleave a 2x2 pilot, with immutable inputs and retained failures."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import random
import subprocess
import sys

from .adapters import ARMS
from .common import file_hash, read, save, source_hashes
from .scenarios import scenarios


def freeze(args):
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    config = read(args.config)
    dataset = scenarios()
    if config.get("research_scenarios"):
        selected = set(config["research_scenarios"])
        dataset = [s for s in dataset if s["id"] in selected]
        if len(dataset) != len(selected):
            raise ValueError("unknown_scenario")
    arms = config.get("research_arms", list(ARMS))
    if not arms or not set(arms).issubset(ARMS):
        raise ValueError("unknown_arm")
    config["fixture_clock"] = datetime.now(timezone.utc).isoformat()
    config["settings"].update(reply_internalization_mode="enforce", reply_internalization_conversations=None)
    save(root / "generation-config.json", config)
    save(root / "scenarios.json", dataset)
    save(root / "persona.json", read(config["persona_file"]))
    sources = {arm: {"root": str(args.source_root.resolve()), "files": source_hashes(args.source_root)} for arm in arms}
    save(
        root / "manifest.json",
        {
            "version": "uncertainty-research-v1",
            "arms": arms,
            "repeats": 2,
            "seed": 10910,
            "created_at": config["fixture_clock"],
            "sources": sources,
            "scenarios": [s["id"] for s in dataset],
            "followup": config.get("followup"),
            "inputs": {
                name: file_hash(root / name) for name in ("generation-config.json", "persona.json", "scenarios.json")
            },
            "evaluator": {p.name: file_hash(p) for p in Path(__file__).parent.glob("*.py")},
            "shared_evaluator": {
                str(p.relative_to(Path(__file__).resolve().parents[2])).replace("\\", "/"): file_hash(p)
                for folder in ("reply_internalization", "qq_brief")
                for p in (Path(__file__).parent.parent / folder).glob("*.py")
            },
            "hypotheses": [
                "Handoff: removing controller instructions from body input reduces irrelevant boundary narration without removing evidence.",
                "Cards: lossless record-oriented evidence lowers false factual rejections without increasing acceptance of unsupported claims.",
                "The combination must preserve task response and necessary qualification; terse outputs alone are not success.",
            ],
            "screening_policy": {
                "controls": 12,
                "minimum_scored_controls": 12,
                "minimum_per_field_accuracy": 0.90,
                "use": "Descriptive screening only; no automatic winner or release decision, even if controls pass.",
                "missing_outcomes": "Retain and report failed and unscored trials in the original denominator.",
            },
            "limitations": [
                "Frozen image/web outputs; actual tool choice remains, remote image recognition does not run.",
                "This is a development pilot, not a held-out release evaluation or conformal guarantee.",
                "The four arms use the same previous v6 research candidate, not the concurrently changed live release.",
            ],
        },
    )
    print(
        f"Frozen {len(dataset)} inputs x {len(arms)} arms x 2 repeats = {len(dataset) * len(arms) * 2} isolated turns."
    )


def run(args):
    root = args.run_dir.resolve()
    manifest = read(root / "manifest.json")
    randomizer = random.Random(manifest["seed"])
    groups = [(s, r) for s in manifest["scenarios"] for r in (1, 2)]
    randomizer.shuffle(groups)
    tasks = []
    for scenario, repeat in groups:
        arms = list(manifest["arms"])
        randomizer.shuffle(arms)
        tasks.extend((scenario, repeat, arm) for arm in arms)

    def launch(task):
        scenario, repeat, arm = task
        if (root / "STOP").exists():
            return
        if (root / "generation" / args.mode / f"{scenario}-r{repeat}-{arm}").exists():
            return
        command = [
            sys.executable,
            "-X",
            "utf8",
            str(Path(__file__).with_name("worker.py")),
            "--run-dir",
            str(root),
            "--scenario",
            scenario,
            "--repeat",
            str(repeat),
            "--arm",
            arm,
            "--mode",
            args.mode,
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=100)
            output = result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            output = "Worker deadline exceeded; partial artifacts retained.\n"
        logs = root / "worker-logs"
        logs.mkdir(exist_ok=True)
        (logs / f"{scenario}-r{repeat}-{arm}.txt").write_text(output, encoding="utf-8")
        print(scenario, repeat, arm, flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(launch, tasks))
    save(
        root / f"run-complete-{args.mode}.json",
        {
            "jobs_expected": len(tasks),
            "external_qq_messages": 0,
            "provider_stopped": (root / "STOP").exists(),
            "note": "Completeness is assessed from per-turn records, not this marker.",
        },
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--source-root", type=Path, required=True)
    f.add_argument("--config", type=Path, required=True)
    f = sub.add_parser("run")
    f.add_argument("--workers", type=int, choices=(1, 2), default=2)
    f.add_argument("--mode", choices=("synthetic", "live"), default="synthetic")
    for s in sub.choices.values():
        s.add_argument("--run-dir", type=Path, required=True)
    args = p.parse_args()
    {"freeze": freeze, "run": run}[args.command](args)


if __name__ == "__main__":
    main()
