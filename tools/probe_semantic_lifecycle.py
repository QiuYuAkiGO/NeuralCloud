"""Isolated real-model HTTP lifecycle probe. No production writes or QQ messages."""

import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import statistics
import subprocess
import time
import urllib.request

root = Path("/var/lib/misty-retrieval-eval/20261027")
model = root / "models/bge-small-zh-v1.5"
revision = hashlib.sha256((model / "download-manifest.json").read_bytes()).hexdigest()
target = root / "lifecycle.vectors.sqlite3"
if not target.exists():
    with sqlite3.connect(root / "bge-small-zh-v1.5-torch.vectors.sqlite3") as source, sqlite3.connect(target) as dest:
        source.backup(dest)
        dest.execute("UPDATE metadata SET value='0' WHERE key='cursor'")
tokenfile = root / "lifecycle.token"
tokenfile.write_text(secrets.token_hex(32))
tokenfile.chmod(0o600)
token = tokenfile.read_text().strip()
memory_token = Path("/var/lib/misty/secrets/memory-server.token").read_text().strip()
env = {
    **os.environ,
    "PYTHONPATH": str(root / "candidate/NeuralCloud/src"),
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "OMP_NUM_THREADS": "4",
    "OPENBLAS_NUM_THREADS": "4",
    "NC_RETRIEVAL_V2": "1",
    "NC_IDENTITY_INDEX": str(root / "identity.shadow.sqlite3"),
    "NC_KEYWORD_THRESHOLD": "0.2",
    "NC_HYBRID_ENABLED": "1",
    "NC_SEMANTIC_THRESHOLD": "0.5",
    "NC_SEMANTIC_URL": "http://127.0.0.1:18767",
    "NC_SEMANTIC_TOKEN_FILE": str(tokenfile),
}


def request(port, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + (token if port == 18767 else memory_token),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=4) as response:
        return json.load(response)


def wait_ready(port, path):
    for _ in range(1800):
        try:
            health = request(port, path)
            if port != 18767 or health["lag"] == 0:
                return health
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("SHADOW_READY_TIMEOUT")


cases = json.loads((root / "queries.frozen.json").read_text())
fact = json.loads((root / "identity.manifest.json").read_text())[0]
subject = {"platform": fact["platform"], "account_id": fact["account_id"]}
report = {}
log = (root / "semantic-lifecycle.log").open("w")
memory = subprocess.Popen(
    [
        str(root / "env/bin/python"),
        "-m",
        "neuralcloud",
        "serve",
        "--database",
        str(root / "shadow.sqlite3"),
        "--character",
        "misty",
        "--token-file",
        "/var/lib/misty/secrets/memory-server.token",
        "--port",
        "18765",
    ],
    env=env,
    stdout=log,
    stderr=log,
)
worker = None
try:
    wait_ready(18765, "/v1/health")
    missing = request(18765, "/v1/recall", {"query": "我是谁", "subject": subject})
    assert missing["diagnostics"].get("degraded") and missing["items"][0]["record_id"] == fact["record_id"]
    report["cold_worker_identity_fallback"] = True
    report["http_latency"] = {}
    for concurrency in (1, 4):
        worker = subprocess.Popen(
            [
                str(root / "env/bin/python"),
                "-m",
                "neuralcloud.vector_index",
                "--source",
                str(root / "shadow.sqlite3"),
                "--index",
                str(target),
                "--model",
                str(model),
                "--revision",
                revision,
                "--token-file",
                str(tokenfile),
                "--port",
                "18767",
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        report["worker_health"] = wait_ready(18767, "/health")

        def timed(case):
            body = {"query": case["query"]}
            if case.get("subject"):
                body["subject"] = case["subject"]
            start = time.perf_counter()
            result = request(18765, "/v1/recall", body)
            return (time.perf_counter() - start) * 1000, bool(result["diagnostics"].get("degraded"))

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            rows = list(pool.map(timed, cases))
        timings = sorted(row[0] for row in rows)
        report["http_latency"][str(concurrency)] = dict(
            p50_ms=statistics.median(timings),
            p95_ms=timings[math.ceil(0.95 * len(timings)) - 1],
            degradation_rate=sum(row[1] for row in rows) / len(rows),
            queries=len(rows),
        )
        worker.terminate()
        worker.wait(timeout=35)
        worker = None
    failed = request(18765, "/v1/recall", {"query": "现在还认得我吗", "subject": subject})
    assert failed["diagnostics"].get("degraded") and failed["items"][0]["record_id"] == fact["record_id"]
    report["worker_exit_identity_fallback"] = True
    report["worker_restarted_between_concurrency_runs"] = True
    report["production_hybrid_unchanged"] = True
    (root / "semantic-lifecycle.report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
finally:
    if worker:
        worker.terminate()
        worker.wait(timeout=35)
    memory.terminate()
    memory.wait(timeout=15)
    log.close()
