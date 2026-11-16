"""Run real CPU embeddings against the frozen private corpus. Never touches live memory."""

import argparse
import json
import math
import time
import statistics
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import psutil
from threadpoolctl import threadpool_limits
from neuralcloud.storage import MemoryStore
from neuralcloud.contracts import RecallSubject
from neuralcloud.retrieval import Retriever, IdentityIndex
from neuralcloud.vector_index import Encoder, VectorIndex

parser = argparse.ArgumentParser()
parser.add_argument("--model", required=True)
parser.add_argument("--backend", default="torch")
parser.add_argument("--backfill-batch", type=int, default=64)
parser.add_argument("--encode-batch", type=int, default=16)
parser.add_argument("--prior-backfill-seconds", type=float, default=0)
args = parser.parse_args()
root = Path("/var/lib/misty-retrieval-eval/20261027")
model = root / "models" / args.model
tag = args.model + "-" + args.backend
result_path = root / (tag + ".report.json")
threadpool_limits(limits=4)
cases = json.loads((root / "queries.frozen.json").read_text())
store = MemoryStore(root / "evaluation.sqlite3", "misty")
identity = IdentityIndex(root / "identity.evaluation.sqlite3")
identity.seed(json.loads((root / "identity.manifest.json").read_text()), store)
revision = hashlib.sha256((model / "download-manifest.json").read_bytes()).hexdigest()
if args.backend == "onnx-int8":
    revision = hashlib.sha256((revision + (model / "quantization-manifest.json").read_text()).encode()).hexdigest()
started = time.perf_counter()
encoder = Encoder(str(model), revision, backend=args.backend)
encoder.encode(["预热查询"], query=True)
cold = time.perf_counter() - started
index = VectorIndex(root / "evaluation.sqlite3", root / (tag + ".vectors.sqlite3"), encoder)
started = time.perf_counter()
batches = 0
while index.sync_batch(batch=args.backfill_batch, encode_batch=args.encode_batch):
    batches += 1
    if batches % 20 == 0:
        print("BACKFILL", tag, batches, round(time.perf_counter() - started, 1), flush=True)
backfill = time.perf_counter() - started + args.prior_backfill_seconds
print("INDEX_READY", tag, len(index.rows), backfill, flush=True)

raw = {}
for case in cases:
    # Quality evaluation is not deadline-limited: report slow configurations separately.
    raw[case["query"]] = index.search(case["query"], timeout=30)


class FrozenSemantic:
    def search(self, query, **kw):
        return raw[query]


engine = Retriever(store, identity, FrozenSemantic(), hybrid=True)


def result(case, mode):
    subject = RecallSubject(**case["subject"]) if case.get("subject") else None
    return engine.recall(case["query"], subject=subject, mode=mode)


def metrics(rows):
    positive = [(c, r) for c, r in rows if c["relevant"]]
    negatives = [(c, r) for c, r in rows if not c["relevant"]]
    recalls = []
    ndcgs = []
    hits = []
    for c, r in positive:
        ids = [item["record_id"] for item in r["items"][:10]]
        gold = set(c["relevant"])
        relevance = [int(rid in gold) for rid in ids]
        recalls.append(len(set(ids) & gold) / len(gold))
        hits.append(int(bool(set(ids) & gold)))
        dcg = sum(v / math.log2(i + 2) for i, v in enumerate(relevance))
        ideal = sum(1 / math.log2(i + 2) for i in range(min(10, len(gold))))
        ndcgs.append(dcg / ideal if ideal else 0)
    return dict(
        recall10=statistics.mean(recalls) if recalls else 0,
        hit10=statistics.mean(hits) if hits else 0,
        ndcg10=statistics.mean(ndcgs) if ndcgs else 0,
        no_answer_false_positive=sum(bool(r["items"]) for _, r in negatives) / len(negatives) if negatives else None,
    )


dev = [c for c in cases if c["split"] == "dev"]
test = [c for c in cases if c["split"] == "test"]
calibration = []
keyword_calibration = []
for threshold in [i / 40 for i in range(41)]:
    engine.keyword_threshold = threshold
    score = metrics([(c, result(c, "keyword")) for c in dev])
    keyword_calibration.append(dict(threshold=threshold, **score))
valid_keywords = [r for r in keyword_calibration if r["no_answer_false_positive"] <= 0.05]
keyword_choice = max(valid_keywords, key=lambda r: (r["recall10"], r["ndcg10"], -r["threshold"]))
engine.keyword_threshold = keyword_choice["threshold"]
for threshold in [i / 40 for i in range(41)]:
    engine.threshold = threshold
    score = metrics([(c, result(c, "hybrid")) for c in dev])
    calibration.append(dict(threshold=threshold, **score))
valid = [r for r in calibration if r["no_answer_false_positive"] <= 0.05]
chosen = max(valid, key=lambda r: (r["recall10"], r["ndcg10"], -r["threshold"])) if valid else None
engine.threshold = chosen["threshold"] if chosen else 1.0
output = dict(
    model=args.model,
    backend=args.backend,
    revision=revision,
    cold_start_seconds=cold,
    backfill_seconds=backfill,
    backfill_batch=args.backfill_batch,
    encode_batch=args.encode_batch,
    prior_attempt_seconds=args.prior_backfill_seconds,
    chunks=len(index.rows),
    rss_bytes=psutil.Process().memory_info().rss,
    threshold=engine.threshold,
    calibration=calibration,
    keyword_threshold=engine.keyword_threshold,
    keyword_calibration=keyword_calibration,
    dataset_sha256=hashlib.sha256((root / "queries.frozen.json").read_bytes()).hexdigest(),
    modes={},
)
for mode in ["legacy", "keyword", "semantic", "hybrid"]:
    rows = [(c, result(c, mode)) for c in test]
    output["modes"][mode] = dict(
        metrics=metrics(rows),
        paraphrases=metrics([(c, r) for c, r in rows if c["category"] == "paraphrase"]),
        identity_accuracy=sum(
            bool(set(c["relevant"]) & {i["record_id"] for i in r["items"]})
            for c, r in rows
            if c["category"] == "identity"
        )
        / 10,
        results=[dict(id=c["id"], retrieved=[i["record_id"] for i in r["items"]]) for c, r in rows],
    )
# Use the real foreground path, including the 1.2 second semantic deadline. One unique uncached query per case.
live = Retriever(
    store, identity, index, hybrid=True, threshold=engine.threshold, keyword_threshold=engine.keyword_threshold
)
latencies = {}
for concurrency in [1, 4]:
    index.cache.clear()

    def timed(case):
        start = time.perf_counter()
        r = live.recall(case["query"], subject=RecallSubject(**case["subject"]) if case.get("subject") else None)
        return (time.perf_counter() - start) * 1000, bool(r["diagnostics"]["degraded"])

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        measurements = list(pool.map(timed, cases))
    ordered = sorted(t for t, _ in measurements)
    latencies[str(concurrency)] = {
        "p50_ms": statistics.median(ordered),
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "degradation_rate": sum(d for _, d in measurements) / len(measurements),
        "queries": len(ordered),
    }
output["latency"] = latencies
base = output["modes"]["keyword"]
hybrid = output["modes"]["hybrid"]
output["gates"] = {
    "identity": hybrid["identity_accuracy"] == 1,
    "paraphrase_gain": hybrid["paraphrases"]["recall10"] - base["paraphrases"]["recall10"] >= 0.10,
    "ndcg_no_regression": hybrid["metrics"]["ndcg10"] >= base["metrics"]["ndcg10"],
    "negative_no_regression": hybrid["metrics"]["no_answer_false_positive"]
    <= base["metrics"]["no_answer_false_positive"] + 0.02,
    "latency": all(v["p95_ms"] <= 1500 and v["degradation_rate"] <= 0.01 for v in latencies.values()),
    "pooled_relevance_review": False,
}
result_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
print(
    "RESULT",
    tag,
    json.dumps({k: v for k, v in output.items() if k in ["threshold", "latency", "gates"]}, ensure_ascii=False),
    flush=True,
)
identity.close()
store.close()
