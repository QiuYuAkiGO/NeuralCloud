"""Run isolated real CLM evaluations with a shared Transformers pooling backend.

Uses pinned upstream CLM Engine/schema/HTTP server unchanged. The encoder is a
local Transformers replacement for vLLM, with last non-padding token pooling.
Results are NOT vLLM throughput numbers or live NeuralCloud/QQ acceptance.
"""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import secrets
import socket
import sys
import threading
import time


def file_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def replace_report(source, destination):
    """Retry Windows scanner sharing violations without repeating inference."""
    for attempt in range(20):
        try:
            Path(source).replace(destination)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(min(.4, .02 * 1.7 ** attempt))


def percentile(values, q):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)] if ordered else None


def summarize(rows):
    good = [r["elapsed_ms"] for r in rows if r.get("status") == 200]
    return {"attempts": len(rows), "successes": len(good), "failures": len(rows) - len(good),
            "p50_ms": percentile(good, .5), "p95_ms": percentile(good, .95),
            "p99_ms": percentile(good, .99), "max_ms": max(good) if good else None,
            "percentile_method": "nearest-rank; successful responses only; failures separately retained"}


def prediction(answer):
    if answer["type"] == "choice":
        return answer["choice"]
    if answer["type"] == "noul":
        return answer["noul"] >= .5
    return int(max(answer["probabilities"], key=answer["probabilities"].get))


class TransformersEmbedder:
    """Raw text, no chat template, right padding; reject overlength, never truncate."""

    def __init__(self, model_path, device, threads, max_tokens=2048, batch=1):
        import numpy as np
        import torch
        from transformers import AutoModel, AutoTokenizer

        self.np, self.torch, self.device = np, torch, device
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        if device == "cuda":
            torch.backends.cuda.matmul.allow_tf32 = False
        self.max_tokens, self.batch = max_tokens, batch
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self.cache_limit = 8192
        self.forward_calls = self.encoded_tokens = 0
        self.fail = False
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, padding_side="right")
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        started = time.perf_counter()
        self.model = AutoModel.from_pretrained(model_path, local_files_only=True,
            torch_dtype=torch.bfloat16, device_map={"": device}, attn_implementation="sdpa").eval()
        self.load_seconds = time.perf_counter() - started
        self.parameter_dtype = str(next(self.model.parameters()).dtype)

    def healthy(self):
        return not self.fail

    def lengths(self, texts):
        return [len(x) for x in self.tokenizer(texts, add_special_tokens=True, truncation=False)["input_ids"]]

    def embed(self, texts):
        import torch
        from clm.embedder import EmbedderError

        with self.lock:
            if self.fail:
                raise EmbedderError("isolated evaluation fault injection")
            missing = [text for text in dict.fromkeys(texts) if text not in self.cache]
            lengths = self.lengths(missing) if missing else []
            if any(n > self.max_tokens for n in lengths):
                raise ValueError("evaluation_input_exceeds_max_tokens; no truncation")
            spent = 0
            for start in range(0, len(missing), self.batch):
                chunk = missing[start:start + self.batch]
                inputs = self.tokenizer(chunk, return_tensors="pt", padding=True, truncation=False).to(self.device)
                with torch.inference_mode():
                    hidden = self.model(**inputs, use_cache=False).last_hidden_state
                    last = inputs["attention_mask"].sum(dim=1) - 1
                    vectors = hidden[torch.arange(len(chunk), device=self.device), last].float()
                    vectors = torch.nn.functional.normalize(vectors, dim=-1).cpu().numpy()
                for text, vector in zip(chunk, vectors):
                    self.cache[text] = vector
                count = int(inputs["attention_mask"].sum().item())
                spent += count
                self.encoded_tokens += count
                self.forward_calls += 1
                del hidden, inputs, vectors
            result = self.np.stack([self.cache[text] for text in texts])
            for text in texts:
                self.cache.move_to_end(text)
            while len(self.cache) > self.cache_limit:
                self.cache.popitem(last=False)
            return result, spent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--vendor", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=["cpu", "cuda"], required=True)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=2048)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    sys.path.insert(0, str(args.vendor.resolve()))
    import httpx
    import psutil
    import torch
    import transformers
    import uvicorn
    from clm import Engine
    from clm.schema import build_pairs
    from clm.server import create_app

    process = psutil.Process()
    report = {"schema_version": "clm-machine-benchmark-v1", "started_at": datetime.now(timezone.utc).isoformat(),
              "completed": False, "synthetic": True, "live_qq": False,
              "backend": "Transformers AutoModel BF16 SDPA last-nonpad-token + unchanged upstream CLM Engine/HTTP server",
              "not_measured": ["vLLM serving", "live QQ delivery", "production memory writes", "independent human ratings"],
              "environment": {"platform": platform.platform(), "machine": platform.machine(),
                  "python": sys.version, "torch": torch.__version__, "transformers": transformers.__version__,
                  "cuda_runtime": torch.version.cuda, "device": args.device, "threads": args.threads,
                  "encoder_batch": 1, "encoder_concurrency": "serialized lock; API concurrency measured including queue",
                  "cpu_logical": psutil.cpu_count(), "ram_total": psutil.virtual_memory().total,
                  "ram_available_at_start": psutil.virtual_memory().available,
                  "gpu": torch.cuda.get_device_name() if args.device == "cuda" else None},
              "case_sha256": file_sha(args.cases), "runner_sha256": file_sha(__file__),
              "upstream_commit": "bb42c6c5bf914fd449bed2f6ca65be80602cb1f7",
              "vendor_sha256": {p.name: file_sha(p) for p in sorted((args.vendor / "clm").glob("*.py"))},
              "quality": [], "permutation": [], "performance": {}, "contract": [], "events": []}
    report_path = args.output / "report.json"
    rows_path = args.output / "requests.jsonl"

    def save():
        temp = report_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_report(temp, report_path)

    def event(name, **details):
        value = {"event": name, "utc": datetime.now(timezone.utc).isoformat(), **details}
        report["events"].append(value)
        print(json.dumps(value, ensure_ascii=False), flush=True)
        save()

    stop_sampling = threading.Event()
    resources = []

    def sample():
        while not stop_sampling.wait(.5):
            resources.append({"time": time.time(), "rss": process.memory_info().rss,
                "cpu_percent": process.cpu_percent(), "system_available": psutil.virtual_memory().available})

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    server = None
    client = None
    try:
        manifest = json.loads((args.models / "download-manifest.json").read_text())
        hashes = {}
        for label, info in manifest.items():
            for item in info["files"]:
                path = args.models / label / item["rfilename"]
                if path.stat().st_size != item["size"]:
                    raise ValueError("model_file_size_mismatch")
                actual = file_sha(path)
                expected = item.get("lfs", {}).get("sha256")
                if expected and actual != expected:
                    raise ValueError("model_file_checksum_mismatch")
                hashes[f"{label}/{path.name}"] = actual
        report["models"] = {k: {"repo": v["repo"], "revision": v["revision"]} for k, v in manifest.items()}
        report["model_file_sha256"] = hashes
        report["load_caveat"] = "checksum read warms OS file cache; not cold disk load"
        event("weights_verified", files=len(hashes))
        embedder = TransformersEmbedder(str(args.models / "encoder"), args.device, args.threads, args.max_tokens)
        engine = Engine(embedder=embedder, checkpoint=str(args.models / "head" / "CLM_v0.1-8B.pt"),
                        device=args.device, action_cache="32MiB")
        report["model_load_seconds"] = embedder.load_seconds
        report["parameter_dtype"] = embedder.parameter_dtype
        report["head_config"] = engine.heads["clm-latest"].cfg
        report["head_scale"] = engine.heads["clm-latest"].scale
        event("model_loaded", seconds=embedder.load_seconds, device=args.device)
        api_key = secrets.token_hex(24)
        app = create_app(engine, api_key=api_key, ui=False)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("isolated_server_start_timeout")
            time.sleep(.05)
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180,
                             headers={"Authorization": "Bearer " + api_key}, trust_env=False)
        write_lock = threading.Lock()

        def request(payload, phase, case_id, path="/v1/systemone", auth=True):
            started = time.perf_counter()
            row = {"phase": phase, "case_id": case_id, "payload": payload}
            try:
                response = client.post(path, json=payload, headers={} if auth else {"Authorization": "Bearer invalid"})
                row.update(status=response.status_code, response=response.json(),
                           server_ms=response.headers.get("X-CLM-Latency-Ms"))
            except Exception as exc:
                row.update(status=None, error_type=type(exc).__name__)
            row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 4)
            with write_lock, rows_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            return row

        def reset_cache():
            with embedder.lock:
                embedder.cache.clear()
                engine.arena = engine._reserve("32MiB")

        cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
        def quality(case, phase):
            row = request(case["request"], phase, case["id"])
            row.update(category=case["category"], expected=case["expected"], correct=False)
            if row["status"] == 200:
                answer = row["response"]["answers"]["decision"]
                row.update(predicted=prediction(answer), correct=prediction(answer) == case["expected"])
                if answer["type"] == "score":
                    row["absolute_error"] = abs(answer["score"] - case["expected"])
            return row

        # First real request is kept separate from warm measurements.
        report["first_inference"] = request(cases[0]["request"], "first_inference", cases[0]["id"])
        if report["first_inference"]["status"] != 200:
            raise RuntimeError("first_real_inference_failed")
        event("first_inference", ms=report["first_inference"]["elapsed_ms"])
        for case in cases:
            row = quality(case, "quality")
            report["quality"].append(row)
            event("quality_case", case=case["id"], correct=row["correct"], ms=row["elapsed_ms"])

        # Same examples with reversed candidate order: consistency, not new gold.
        for original in [c for c in cases if c["request"]["questions"]["decision"]["type"] == "choice"][::4]:
            case = json.loads(json.dumps(original))
            q = case["request"]["questions"]["decision"]
            q["criteria"] = dict(reversed(list(q["criteria"].items())))
            row = quality(case, "candidate_order_reversed")
            base = next(r for r in report["quality"] if r["case_id"] == original["id"])
            row["same_prediction"] = row.get("predicted") == base.get("predicted") and row["status"] == 200
            report["permutation"].append(row)
        event("permutation_complete", cases=len(report["permutation"]))

        reference = cases[0]["request"]
        for phase, count in [("uncached", 10), ("candidates_cached_state_new", 16), ("fully_cached", 30)]:
            rows = []
            for i in range(count):
                payload = json.loads(json.dumps(reference))
                if phase != "fully_cached":
                    reset_cache()
                if phase == "candidates_cached_state_new":
                    qid, (_, _, candidate_texts) = next(iter(build_pairs(payload["state"], payload["questions"]).items()))
                    head = engine.heads["clm-latest"]
                    engine._cached(f"{head.namespace}/action", head.proj_dim, candidate_texts, [], head.project_actions)
                    if args.device == "cuda":
                        torch.cuda.synchronize()
                    payload["state"] += f"\n记录序号：{i}。"
                if phase == "fully_cached" and i == 0:
                    request(payload, "warmup_excluded", "fully_cached")
                rows.append(request(payload, phase, f"{phase}-{i}"))
            report["performance"][phase] = {"summary": summarize(rows), "rows": rows}
            event("performance_phase", phase=phase, **summarize(rows))

        # Serialized encoder, concurrent HTTP requests: measures queueing as well.
        reset_cache()
        request(reference, "warmup_excluded", "concurrency")
        payloads = []
        for i in range(16):
            payload = json.loads(json.dumps(reference))
            payload["state"] += f"\n本轮观测编号：并发-{i}。"
            payloads.append(payload)
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda pair: request(pair[1], "concurrency2", str(pair[0])), enumerate(payloads)))
        elapsed = time.perf_counter() - started
        report["performance"]["concurrency2"] = {"summary": {**summarize(rows), "wall_seconds": elapsed,
            "successful_requests_per_second": sum(r["status"] == 200 for r in rows) / elapsed}, "rows": rows}
        event("performance_phase", phase="concurrency2", **summarize(rows))

        length_rows = []
        for target in [128, 512, 1024, 2048]:
            reset_cache()
            payload = json.loads(json.dumps(reference))
            suffix = "\n" + str(payload["state"])
            base = "无关背景记录。" * target
            tokens = embedder.tokenizer.encode(base, add_special_tokens=False)
            instruction = payload["questions"]["decision"]["instructions"]
            n = max(0, target - len(embedder.tokenizer.encode(suffix + "\n\n" + instruction)) - 4)
            payload["state"] = embedder.tokenizer.decode(tokens[:n]) + suffix
            actual = embedder.lengths([build_pairs(payload["state"], payload["questions"])["decision"][0]])[0]
            row = request(payload, "length_sweep_uncached", str(target))
            row.update(target_tokens=target, actual_state_question_tokens=actual)
            length_rows.append(row)
            event("length_case", target=target, actual=actual, ms=row["elapsed_ms"], status=row["status"])
        report["performance"]["length_sweep"] = {"rows": length_rows, "note": "one uncached request per length; not a percentile estimate"}

        checks = [("wrong_auth", reference, 401, False), ("empty_questions", {"state": "test", "questions": {}}, 422, True),
                  ("unknown_model", {**reference, "model": "missing-eval-model"}, 422, True),
                  ("invalid_temperature", {**reference, "temperature": 0}, 422, True),
                  ("overlength_rejected", {**reference, "state": "测试输入。" * 3000}, 422, True)]
        for name, payload, expected, auth in checks:
            row = request(payload, "contract", name, auth=auth)
            report["contract"].append({"name": name, "expected_status": expected, "actual_status": row["status"], "pass": row["status"] == expected})
        reset_cache()
        embedder.fail = True
        row = request(reference, "contract", "encoder_failure")
        report["contract"].append({"name": "encoder_failure", "expected_status": 502, "actual_status": row["status"], "pass": row["status"] == 502})
        embedder.fail = False
        row = request(reference, "contract", "encoder_recovered")
        report["contract"].append({"name": "encoder_recovered", "expected_status": 200, "actual_status": row["status"], "pass": row["status"] == 200})
        rank = request({"context": "甲的猫叫什么？", "question": "选择直接回答问题的记忆。",
                        "answers": ["甲的猫叫团团。", "乙的狗叫小黄。"]}, "contract", "rank", path="/v1/rank")
        report["contract"].append({"name": "rank_endpoint", "actual_status": rank["status"],
            "pass": rank["status"] == 200 and len(rank["response"].get("ranked", [])) == 2})
        groups = sorted({r["category"] for r in report["quality"]})
        report["quality_summary"] = {}
        for group in groups:
            rows = [r for r in report["quality"] if r["category"] == group]
            report["quality_summary"][group] = {"correct": sum(r["correct"] for r in rows), "total": len(rows),
                "confusion": dict(Counter(f"{r['expected']}->{r.get('predicted', 'ERROR')}" for r in rows))}
        report["forward_calls"] = embedder.forward_calls
        report["encoded_tokens"] = embedder.encoded_tokens
        report["completed"] = True
        event("complete", correct=sum(r["correct"] for r in report["quality"]), total=len(report["quality"]))
    except BaseException as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:1500]}
        event("failed", error_type=type(exc).__name__)
        raise
    finally:
        if client:
            client.close()
        if server:
            server.should_exit = True
        stop_sampling.set()
        sampler.join(timeout=2)
        report["resources"] = {"peak_rss_bytes": max((r["rss"] for r in resources), default=process.memory_info().rss),
            "minimum_system_available_bytes": min((r["system_available"] for r in resources), default=None),
            "cpu_seconds": process.cpu_times().user + process.cpu_times().system,
            "gpu_peak_allocated_bytes": torch.cuda.max_memory_allocated() if args.device == "cuda" else None,
            "gpu_peak_reserved_bytes": torch.cuda.max_memory_reserved() if args.device == "cuda" else None}
        (args.output / "resources.json").write_text(json.dumps(resources), encoding="utf-8")
        report["ended_at"] = datetime.now(timezone.utc).isoformat()
        save()


if __name__ == "__main__":
    main()
