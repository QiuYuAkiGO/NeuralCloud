"""Bounded read-only post-release observation. Stores aggregates, never chat content."""

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import time
import urllib.request

import psutil

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--duration", type=int, default=86400)
parser.add_argument("--interval", type=int, default=300)
args = parser.parse_args()
root = args.root
deployment = json.loads((root / "deployment.json").read_text())
deadline = deployment["deployed_at"] + args.duration
token = Path("/var/lib/misty/secrets/memory-server.token").read_text().strip()
fact = json.loads((root / "identity.manifest.json").read_text())[0]
subject = {"platform": fact["platform"], "account_id": fact["account_id"]}
logfile = root / "observation.jsonl"
rows = [json.loads(line) for line in logfile.read_text().splitlines()] if logfile.exists() else []


def request(path, body=None):
    request = urllib.request.Request(
        "http://127.0.0.1:8765" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


while True:
    sample = {"at": time.time(), "issues": []}
    try:
        health = request("/v1/health")
        sample["generation"] = health["generation"]
        sample["hybrid_enabled"] = health.get("hybrid_configured", False)
        assert health.get("retrieval_v2") and health.get("identity_configured"), "RETRIEVAL_CONFIGURATION"
        timings = []
        for query in ("你还记得我是谁吗", "我们的关系是什么", "你知道我的名字吗"):
            start = time.perf_counter()
            result = request("/v1/recall", {"query": query, "subject": subject})
            timings.append((time.perf_counter() - start) * 1000)
            assert any(
                row.get("record_id") == fact["record_id"]
                and row.get("identity", {}).get("confirmation") == "operator_confirmed"
                for row in result["items"]
            ), "IDENTITY_MISSING"
            assert not result.get("diagnostics", {}).get("degraded"), "RECALL_DEGRADED"
        sample["probe_ms"] = timings
        if max(timings) > 1500:
            sample["issues"].append("PROBE_LATENCY")
        wrong = request(
            "/v1/recall", {"query": "我是谁", "subject": {**subject, "account_id": str(int(subject["account_id"]) + 1)}}
        )
        assert not any(row.get("identity") for row in wrong["items"]), "NEIGHBOR_FALSE_IDENTITY"
    except Exception as exc:
        sample["issues"].append(type(exc).__name__ + (":" + str(exc) if isinstance(exc, AssertionError) else ""))
    sample["services"] = {}
    for name in ("neuralcloud-memory", "misty-state", "misty-harness", "misty-operator-notifier"):
        try:
            pid = int(
                subprocess.check_output(["systemctl", "show", "-p", "MainPID", "--value", name + ".service"], text=True)
            )
            assert pid > 0
            process = psutil.Process(pid)
            sample["services"][name] = {
                "rss_bytes": process.memory_info().rss,
                "cpu_seconds": sum(process.cpu_times()[:2]),
            }
        except Exception:
            sample["issues"].append("SERVICE_UNAVAILABLE:" + name)
    sample["system"] = {"available_bytes": psutil.virtual_memory().available, "load_average": psutil.getloadavg()}
    rows.append(sample)
    with logfile.open("a") as stream:
        stream.write(json.dumps(sample) + "\n")
    timings = [t for row in rows for t in row.get("probe_ms", [])]
    complete = time.time() >= deadline
    summary = dict(
        status="COMPLETE" if complete else "OBSERVING",
        deadline=deadline,
        samples=len(rows),
        issue_samples=sum(bool(row["issues"]) for row in rows),
        latest=sample,
        probe_p50_ms=statistics.median(timings) if timings else None,
        note="Read-only synthetic probes; not a replacement for uncached load benchmarks or actual-turn quality review.",
    )
    temp = root / "observation-summary.tmp"
    temp.write_text(json.dumps(summary, indent=2))
    temp.replace(root / "observation-summary.json")
    if complete:
        break
    time.sleep(min(args.interval, max(0, deadline - time.time())))
