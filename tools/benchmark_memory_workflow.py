"""Private source-grounded retrieval benchmark and isolated generation replay."""

from __future__ import annotations
import argparse
import json
import sqlite3
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from neuralcloud.storage import MemoryStore
from neuralcloud.retrieval import Retriever, IdentityIndex
from neuralcloud.contracts import RecallSubject
from neuralcloud.evidence_index import EvidenceIndex
from neuralcloud.workflow import MemoryWorkflow
from neuralcloud.vector_index import Encoder, VectorIndex
from neuralcloud.rebuild import request_model


def quantiles(values):
    v = sorted(values)
    return (
        {p: v[min(len(v) - 1, int(len(v) * q))] for p, q in [("p50", 0.5), ("p95", 0.95), ("p99", 0.99)]} if v else {}
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--generation-replay", action="store_true")
    parser.add_argument("--skip-latency", action="store_true")
    for name in ["source", "evidence", "vectors", "cases"]:
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--backend", default="onnx-int8")
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--groups", nargs="+", choices=["A", "B", "C"], default=["A", "B", "C"])
    args = parser.parse_args()
    root = args.root
    root.mkdir(exist_ok=True)
    old = Path("/var/lib/misty-retrieval-eval/20261027")
    db = root / "evaluation.sqlite3"
    if not db.exists():
        with (
            sqlite3.connect("file:" + str(args.source or old / "evaluation.sqlite3") + "?mode=ro", uri=True) as source,
            sqlite3.connect(db) as dest,
        ):
            source.backup(dest)
    store = MemoryStore(db, "misty")
    if args.evidence and not (root / "evidence.sqlite3").exists():
        with (
            sqlite3.connect(args.evidence.resolve().as_uri() + "?mode=ro", uri=True) as source,
            sqlite3.connect(root / "evidence.sqlite3") as dest,
        ):
            source.backup(dest)
    index = EvidenceIndex(db, root / "evidence.sqlite3", writable=True, self_accounts=["qq:2015188508", "qq:765681548"])
    while index.sync_batch():
        pass
    identities = IdentityIndex(root / "identity.sqlite3")
    identities.seed(json.loads((old / "identity.manifest.json").read_text()), store)
    report = json.loads((old / "bge-small-zh-v1.5-onnx-int8.report.json").read_text())
    vector_file = root / "vectors.sqlite3"
    if not vector_file.exists():
        with (
            sqlite3.connect(
                (args.vectors or old / "bge-small-zh-v1.5-onnx-int8.vectors.sqlite3").resolve().as_uri() + "?mode=ro",
                uri=True,
            ) as source,
            sqlite3.connect(vector_file) as dest,
        ):
            source.backup(dest)
    started = time.monotonic()
    encoder = Encoder(
        str(args.model_path or old / "models/bge-small-zh-v1.5"),
        args.revision or report["revision"],
        backend=args.backend,
        threads=args.threads,
    )
    vector = VectorIndex(db, vector_file, encoder)
    boot = time.monotonic() - started
    base = Retriever(store, identities, vector, hybrid=True, threshold=0.5, keyword_threshold=0.2)
    flow = MemoryWorkflow(base, index)
    cases = json.loads((old / "queries.frozen.json").read_text())
    names = {}
    for row in index.db.execute(
        "SELECT entity,record_id FROM facts WHERE predicate='name' AND active=1 ORDER BY entity"
    ):
        names.setdefault(row[0], set()).add(row[1])
    for i, (entity, refs) in enumerate(sorted(names.items())[:20]):
        account = entity.split(":")[1]
        cases.append(
            {
                "id": f"account-identity-{i}",
                "query": account + "叫什么名字",
                "category": "account_identity",
                "split": "test",
                "subject": {"platform": "qq", "account_id": "2285027817"},
                "relevant": sorted(refs),
            }
        )
    for i in range(10):
        cases.append(
            {
                "id": f"constraint-negative-{i}",
                "query": "2285027817叫什么名字",
                "category": "constraint_negative",
                "split": "test",
                "subject": {"platform": "qq", "account_id": "2285027817"},
                "constraints": {"conversation_id": "missing-conversation-" + str(i)},
                "relevant": [],
            }
        )
    for i in range(10):
        cases.append(
            {
                "id": f"unknown-account-{i}",
                "query": str(99800000000000 + i) + "叫啥",
                "category": "no_answer",
                "split": "test",
                "relevant": [],
            }
        )
    assert len(cases) == 240
    if args.cases:
        cases = json.loads(args.cases.read_text())
        assert len(cases) == 240
    (root / "cases.private.json").write_text(json.dumps(cases, ensure_ascii=False))

    def call(group, c):
        subject = RecallSubject(**c["subject"]) if c.get("subject") else None
        if group == "A":
            return base.recall(c["query"])
        if group == "B":
            return base.recall(c["query"], subject=subject)
        return flow.recall(c["query"], subject=subject, constraints=c.get("constraints"))

    quality = {}
    all_results = {}
    for group in args.groups:
        outputs = []
        for c in cases:
            start = time.monotonic()
            r = call(group, c)
            outputs.append({"id": c["id"], "elapsed_ms": (time.monotonic() - start) * 1000, "result": r})
        all_results[group] = outputs
        summary = {}
        for category in sorted({c["category"] for c in cases}):
            pairs = [(c, r) for c, r in zip(cases, outputs) if c["category"] == category]
            positives = [(c, r) for c, r in pairs if c["relevant"]]
            negatives = [(c, r) for c, r in pairs if not c["relevant"]]
            summary[category] = {
                "n": len(pairs),
                "hit10": sum(
                    bool(set(c["relevant"]) & {x["record_id"] for x in r["result"]["items"][:10]}) for c, r in positives
                )
                / max(1, len(positives)),
                "unsupported_sufficient": sum(
                    r["result"].get("assessment", {}).get("sufficient", False) for c, r in negatives
                ),
                "nonempty_negatives": sum(bool(r["result"]["items"]) for c, r in negatives),
            }
        quality[group] = summary
        print("QUALITY", group, json.dumps(summary), flush=True)
    (root / "quality-results.private.json").write_text(json.dumps(all_results, ensure_ascii=False))
    latency = {}
    for group in [] if args.skip_latency else args.groups:
        for concurrency in [1, 4, 8]:
            for cache in ["cold", "warm"]:
                values = []
                for repeat in range(3):
                    vector.cache.clear()
                    flow.cache.clear()
                    if cache == "warm":
                        for c in cases:
                            call(group, c)

                    def timed(c):
                        start = time.monotonic()
                        r = call(group, c)
                        return (time.monotonic() - start) * 1000, r.get("assessment", {}).get("state") == "INCOMPLETE"

                    with ThreadPoolExecutor(max_workers=concurrency) as pool:
                        values.extend(pool.map(timed, cases))
                latency[f"{group}:{concurrency}:{cache}"] = {
                    **quantiles([x[0] for x in values]),
                    "n": len(values),
                    "incomplete": sum(x[1] for x in values),
                }
                print(
                    "LATENCY",
                    group,
                    concurrency,
                    cache,
                    json.dumps(latency[f"{group}:{concurrency}:{cache}"]),
                    flush=True,
                )
    summary = {
        "cases": 240,
        "source": "existing session-disjoint 200-case frozen corpus plus 40 explicit account/constraint cases",
        "limitations": [
            "Known relevant IDs are incomplete for equivalent alternate evidence.",
            "A/B do not support new constraints; negative constraint results expose the capability gap.",
            "Cold means query caches cleared; model process startup measured separately.",
        ],
        "startup_seconds": boot,
        "semantic": {
            "backend": encoder.backend,
            "model": encoder.name,
            "revision": encoder.revision,
            "threads": args.threads,
        },
        "quality": quality,
        "latency": latency,
        "index": index.status(),
    }
    (root / "benchmark.json").write_text(json.dumps(summary, indent=2))
    if args.generation_replay:
        config = json.loads(args.model_config.read_text())
        replays = []
        selected = cases[170:190] + cases[190:200] + cases[200:220] + cases[:10]
        assert len(selected) == 60
        for i, c in enumerate(selected):
            for group in ["A", "B", "C", "D"]:
                start = time.monotonic()
                r = call("C" if group == "D" else group, c)
                retrieval_ms = (time.monotonic() - start) * 1000
                judge_ms = 0
                judge_usage = {}
                if group == "D" and not r.get("assessment", {}).get("sufficient"):
                    j, u, t = request_model(
                        config,
                        [
                            {
                                "role": "system",
                                "content": '只输出JSON {"sufficient":bool,"reason":简短理由}。判断所给来源是否足以回答问题，相关提及不等于答案，不补造信息。',
                            },
                            {
                                "role": "user",
                                "content": json.dumps({"query": c["query"], "memory": r}, ensure_ascii=False),
                            },
                        ],
                        512,
                    )
                    judge_ms = t * 1000
                    judge_usage = u
                    r = {**r, "experimental_judge": j}
                answer, u, t = request_model(
                    config,
                    [
                        {
                            "role": "system",
                            "content": '根据给出的记忆证据回答用户。只输出JSON {"answer":简短回答,"supported_ids":使用的记录ID列表,"uncertain":bool}。区分推断和直接证据，无法确定时说明。不调用外部工具，不发送消息。',
                        },
                        {"role": "user", "content": json.dumps({"query": c["query"], "memory": r}, ensure_ascii=False)},
                    ],
                    768,
                )
                replays.append(
                    {
                        "id": c["id"],
                        "group": group,
                        "retrieval_ms": retrieval_ms,
                        "judge_ms": judge_ms,
                        "generation_ms": t * 1000,
                        "total_ms": (time.monotonic() - start) * 1000,
                        "usage": u,
                        "judge_usage": judge_usage,
                        "answer": answer,
                    }
                )
                (root / "replays.private.json").write_text(json.dumps(replays, ensure_ascii=False))
            print("REPLAY", i + 1, flush=True)
        summary["generation_replay"] = {
            g: {
                **quantiles([r["total_ms"] for r in replays if r["group"] == g]),
                "n": 60,
                "model_calls": 60 + sum(r["judge_ms"] > 0 for r in replays if r["group"] == g),
                "tokens": sum(
                    r["usage"].get("total_tokens", 0) + r["judge_usage"].get("total_tokens", 0)
                    for r in replays
                    if r["group"] == g
                ),
            }
            for g in ["A", "B", "C", "D"]
        }
        summary["limitations"].append(
            "Generation replay isolates retrieval and model rounds; it is not a real QQ send-latency measurement."
        )
        (root / "benchmark.json").write_text(json.dumps(summary, indent=2))
    index.close()
    identities.close()
    store.close()


if __name__ == "__main__":
    main()
