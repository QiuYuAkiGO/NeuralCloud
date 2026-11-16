"""Read-only HTTP acceptance and latency probe; never sends a chat message."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import statistics
import time
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("--base", default="http://127.0.0.1:8765")
parser.add_argument("--token-file", type=Path, required=True)
parser.add_argument("--queries", type=Path, required=True)
parser.add_argument("--identity-manifest", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
assert args.base in ("http://127.0.0.1:8765", "http://127.0.0.1:18765")
token = args.token_file.read_text().strip()


def request(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        args.base + path,
        data=data,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.load(response)


health = request("/v1/health")
fact = json.loads(args.identity_manifest.read_text())[0]
subject = {"platform": fact["platform"], "account_id": fact["account_id"]}
identity_queries = ["对了，你还记得我是谁吗", "你还认得我吗", "我们的关系是什么", "你知道我的全名吗"]
for query in identity_queries:
    result = request("/v1/recall", dict(query=query, subject=subject))
    assert result["items"][0]["record_id"] == fact["record_id"]
    assert result["items"][0]["identity"]["confirmation"] == "operator_confirmed"
neighbor = {**subject, "account_id": str(int(subject["account_id"]) + 1)}
wrong = request("/v1/recall", dict(query="我是谁", subject=neighbor))
assert not any(item.get("identity") for item in wrong["items"])
assert request("/v1/recall", dict(query="创造者", limit=10))["status"] == "OK"


def timed(case):
    body = {"query": case["query"]}
    if case.get("subject"):
        body["subject"] = case["subject"]
    start = time.perf_counter()
    result = request("/v1/recall", body)
    return (time.perf_counter() - start) * 1000, bool(result.get("diagnostics", {}).get("degraded"))


cases = json.loads(args.queries.read_text())
latency = {}
for concurrency in (1, 4):
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        rows = list(pool.map(timed, cases))
    values = sorted(row[0] for row in rows)
    latency[str(concurrency)] = dict(
        queries=len(rows),
        p50_ms=statistics.median(values),
        p95_ms=values[math.ceil(len(values) * 0.95) - 1],
        degradation_rate=sum(row[1] for row in rows) / len(rows),
    )
report = dict(
    identity_queries_passed=len(identity_queries),
    neighbor_account_unknown=True,
    legacy_contract_compatible=True,
    http_latency=latency,
    health=health,
    cache_note="No server result cache; semantic query caches must be cleared separately before semantic benchmarking.",
)
args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False))
assert all(row["p95_ms"] <= 1500 and row["degradation_rate"] == 0 for row in latency.values())
