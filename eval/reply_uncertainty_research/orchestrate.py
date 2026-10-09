"""One bounded offline experiment. No production service or QQ transport access."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys

from .common import read, save


def main():
    root = Path(sys.argv[1]).resolve()
    script = Path(__file__).parent

    def launch(arguments):
        name = "-".join(arguments).replace("--", "").replace(".py", "")
        output = root / "phase-logs" / (name + ".txt")
        output.parent.mkdir(exist_ok=True)
        with output.open("w", encoding="utf-8") as stream:
            code = subprocess.call(
                [sys.executable, "-X", "utf8", str(script / arguments[0]), "--run-dir", str(root), *arguments[1:]],
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        return {"job": arguments, "exit_code": code}

    phases = [
        (
            "critic",
            [
                ["critic_worker.py", "--arm", arm, "--repeat", str(repeat)]
                for repeat in (1, 2)
                for arm in ("control", "cards")
            ],
        ),
        ("grader-controls", [["grader.py", "--controls", "--part", str(part)] for part in (0, 1)]),
    ]
    for name, jobs in phases:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(launch, jobs))
        save(root / (name + "-complete.json"), results)
        if any(r["exit_code"] for r in results) or (root / "STOP").exists():
            return
    # A reviewer that misses these simple controls must not auto-label the main run.
    controls = [read(p) for p in (root / "grader-controls").glob("*.json")]
    fields = read(root / "manifest.json")["screening_policy"]
    scored = [r for r in controls if r["status"] == "scored"]
    valid = len(scored) == fields["minimum_scored_controls"] and all(
        sum(r["grade"][k] == r["gold"][k] for r in scored) / len(scored) >= fields["minimum_per_field_accuracy"]
        for k in scored[0]["gold"]
    )
    save(root / "screening-control-result.json", {"passed": valid, "scored": len(scored)})
    code = subprocess.call(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "eval.reply_uncertainty_research.runner",
            "run",
            "--run-dir",
            str(root),
            "--mode",
            "live",
            "--workers",
            "2",
        ]
    )
    if code or (root / "STOP").exists():
        return
    if valid:
        with ThreadPoolExecutor(max_workers=2) as pool:
            grades = list(pool.map(launch, [["grader.py", "--part", str(part)] for part in (0, 1)]))
        save(root / "grades-complete.json", grades)
    save(root / "experiment-complete.json", {"screening_controls_passed": valid, "qq_messages_sent": 0})


if __name__ == "__main__":
    main()
