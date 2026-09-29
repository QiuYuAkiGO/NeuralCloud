"""Offline GLiNER2.5 candidate extraction through real evidence manifests.

This experimental frontend never changes production configuration or writes
person judgments. Gold labels are read only by the scorer, never by the model.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

MODES = {
    "direct": "发言者直接陈述自己的实际偏好或经历，不是引用或假设。",
    "reported": "转述或引用别人的话，不可归为当前消息作者本人的自述。",
    "hypothetical": "明确假设、条件或想象，不声称事实已经发生。",
    "denied": "明确否认某句话、行为或偏好的归属，不能提取被否认内容为肯定事实。",
    "request": "向对方提出请求、问题或建议，不等于对方已经选择或完成该动作。",
    "intention": "表达自己的未来计划或意图，不表示动作已完成。",
    "ambiguous": "说话人或被描述对象无法确定，保留未知，不根据代词猜身份。",
    "mixed": "同条消息有多个归属或陈述类型，如对他人的请求和本人的偏好更正。",
}
ENTITIES = {
    "person": "原文中明确出现的人名；不包括我、你、他、她等代词，不猜测账号身份。",
    "statement": "包含人物或代词及其谓语的完整连续原文陈述，保留否定、假设、转述和时间限定。",
    "preference": "原文中关于喜好或偏好的连续表达，保留不喜欢或以前等限定。",
    "time": "原文中明确出现的日期、时段或生效时间表达。",
}
RELATIONS = {
    "supports": "证据支持目标命题，必须是相同人物、相同时间条件、相同行为或偏好。",
    "challenges": "证据与目标命题相冲突，必须是相同人物和相同时间条件。",
    "insufficient": "不能判断目标命题；人物或时间不同，或计划不证明完成、出席不证明完成。",
}
AUDIENCE = "qq:group:gliner-isolated"
REPLAY = {
    "id": "qq_failed_anchor_replay",
    "text": "刚忙完手头的事，我想放松一下，你有什么简单的主意？",
    "required_quotes": ["刚忙完手头的事", "我想放松一下", "你有什么简单的主意"],
}


def write_json(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    path.chmod(0o600)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quantiles(values):
    ordered = sorted(values)
    return {"samples": len(values),
            "p50_ms": ordered[math.ceil(len(ordered) * .5) - 1] if ordered else None,
            "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1] if ordered else None}


def score_candidates(case, verified):
    candidates = verified["candidates"]
    people = sorted({row["anchor"]["quote"] for row in candidates
                     if row["model_interpretation"]["label"] == "person"})
    quotes = [row["anchor"]["quote"] for row in candidates
              if row["model_interpretation"]["label"] in ("statement", "preference")]
    wanted = case["required_quotes"]
    return {"actual_people": people,
            "people_exact": people == sorted(set(case["expected_people"])) if "expected_people" in case else None,
            "required_quotes": wanted,
            "quote_exact": [quote in quotes for quote in wanted],
            "quote_covered": [any(quote in found for found in quotes) for quote in wanted],
            "candidate_count": len(candidates), "rejected_count": len(verified["rejected"])}


def label_of(result, name):
    value = result.get(name)
    return value.get("label") if isinstance(value, dict) else value


def extract_inference(model, torch, text, schema):
    started = time.perf_counter()
    with torch.inference_mode():
        value = model.extract(text, schema, threshold=.5, include_confidence=True, include_spans=True)
    # This is Python serialization, not evidence that the semantics are correct.
    json.dumps(value, ensure_ascii=False, allow_nan=False)
    return value, round((time.perf_counter() - started) * 1000, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("gliner_cases.json"))
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if not args.execute:
        print(json.dumps({"mode": "preview", "cases": len(cases["cases"]),
                          "relations": len(cases["relations"]), "extra_replay": 1,
                          "online_changes": False, "model_calls": 0}))
        return
    if args.model_path is None or not args.model_path.is_dir() or args.output is None:
        parser.error("--execute requires an existing local --model-path and a new --output")
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.environ.update(OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2",
        TOKENIZERS_PARALLELISM="false", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if hasattr(os, "nice"):
        os.nice(10)
    blocked_connections = []

    def no_network(*values, **kwargs):
        blocked_connections.append(True)
        raise RuntimeError("offline_gliner_network_forbidden")

    source_files = ("harness/memory/production.py", "harness/memory/evidence_manifest.py",
                    "harness/core/models.py", "tools/production_acceptance/gliner_bridge.py",
                    "tools/production_acceptance/gliner_scenarios.py", "tools/production_acceptance/gliner_cases.json")
    source_hashes = {name: digest_file(SOURCE / name) for name in source_files}
    model_files = sorted(p for p in args.model_path.rglob("*") if p.is_file())
    model_hashes = {str(p.relative_to(args.model_path)): digest_file(p) for p in model_files}
    rows, relations, warmups = [], [], []
    calls = {"started": 0, "completed": 0}

    def infer(model, torch, text, schema):
        calls["started"] += 1
        value, duration = extract_inference(model, torch, text, schema)
        calls["completed"] += 1
        write_json(args.output / f"inference-{calls['started']:02d}.json",
                   {"text": text, "result": value, "model_ms": duration})
        return value, duration
    report = {"schema_version": "gliner-neuralcloud-scenarios-v1", "started_at": datetime.now(timezone.utc).isoformat(),
        "model_path": str(args.model_path), "source_sha256": source_hashes, "model_sha256": model_hashes,
        "cases_sha256": digest_file(args.cases), "threads": 2, "concurrency": 1,
        "word_splitter": "char", "threshold": .5, "percentile_method": "nearest-rank",
        "schemas": {"modes": MODES, "entities": ENTITIES, "relations": RELATIONS},
        "synthetic_data": True, "production_configuration_read": False, "external_qq_messages": 0,
        "person_judgments_written": 0, "external_model_requests": 0,
        "sample_composition": "16 new synthetic messages, 6 synthetic relation pairs, 1 replay of a previous synthetic QQ benchmark message",
        "measurement_limits": ["mode score is fixture label agreement, not verified speaker/subject attribution",
            "negative self-report, intent and mixed labels have overlapping semantics",
            "model files hashed before load, so load_ms is not a cold filesystem measurement",
            "integration includes source ingestion and manifest validation, not QQ delivery or the main LLM"],
        "trust_boundary": "Only exact original quotations are verified; labels, attribution and evidence roles remain fallible model interpretations."}
    write_json(args.output / "manifest.json", report)
    started = time.perf_counter()
    try:
        with patch.object(socket.socket, "connect", no_network), patch.object(socket, "create_connection", no_network):
            import importlib.metadata
            import resource
            import torch
            from gliner2 import AutoExtractor
            from harness.core.models import MemoryScope
            from harness.memory.production import Experience, ProductionMemoryStore
            from harness.memory.evidence_manifest import EvidenceManifestStore
            from tools.production_acceptance.gliner_bridge import load_extraction_view, verify_entity_candidates

            torch.set_num_threads(2)
            torch.set_num_interop_threads(1)
            report["packages"] = {name: importlib.metadata.version(name)
                for name in ("gliner2", "torch", "transformers", "tokenizers", "pydantic")}
            report["import_ms"] = round((time.perf_counter() - started) * 1000, 3)
            load_start = time.perf_counter()
            model = AutoExtractor.from_pretrained(str(args.model_path), map_location="cpu",
                word_splitter="char", local_files_only=True)
            model.eval()
            report["load_ms"] = round((time.perf_counter() - load_start) * 1000, 3)
            print(json.dumps({"stage": "loaded", "load_ms": report["load_ms"]}), flush=True)
            extraction_schema = model.create_schema().classification("speech_mode", MODES).entities(ENTITIES)
            relation_schema = model.create_schema().classification("evidence_relation", RELATIONS)
            for kind, text, schema in (("extraction", "我喜欢清晨散步。", extraction_schema),
                    ("relation", "待判断命题：某人喜欢散步。\n证据：此人明确说喜欢散步。", relation_schema)):
                value, duration = infer(model, torch, text, schema)
                warmups.append({"kind": kind, "model_ms": duration, "result": value})
            memory = ProductionMemoryStore(args.output / "memory.sqlite3")
            manifests = EvidenceManifestStore(memory)
            for case in [*cases["cases"], REPLAY]:
                case_started = time.perf_counter()
                identifier = "gliner-source:" + case["id"]
                memory.ingest([Experience(record_id=identifier, author_id="qq:member:synthetic-author",
                    text=case["text"], occurred_at=datetime.now(timezone.utc),
                    scope=MemoryScope(channel="qq", conversation_id="gliner-isolated", visibility=[AUDIENCE]),
                    source_refs=(identifier,), independent_key=identifier,
                    original={"record": {"authority": "speaker_utterance"}})])
                views = manifests.prepare_views([identifier], audience=AUDIENCE)
                # Only original text and schema descriptions enter inference.
                manifest = manifests.issue({"evidence_views": views, "extraction_schema": {"modes": MODES, "entities": ENTITIES}},
                    audience=AUDIENCE, command_id="gliner:" + case["id"], runtime_epoch=1)
                view = views[0]
                frozen = load_extraction_view(manifests, manifest["manifest_id"], view["unit_id"], audience=AUDIENCE,
                    command_id="gliner:" + case["id"], runtime_epoch=1)
                value, duration = infer(model, torch, frozen["text"], extraction_schema)
                validation_started = time.perf_counter()
                verified = verify_entity_candidates(manifests, manifest["manifest_id"], view["unit_id"], value,
                    audience=AUDIENCE, command_id="gliner:" + case["id"], runtime_epoch=1)
                validation_ms = round((time.perf_counter() - validation_started) * 1000, 3)
                actual = label_of(value, "speech_mode")
                row = {"id": case["id"], "text": case["text"], "model_ms": duration,
                    "validation_ms": validation_ms, "integration_ms": round((time.perf_counter() - case_started) * 1000, 3),
                    "expected_mode": case.get("expected_mode"), "actual_mode": actual,
                    "mode_correct": actual == case["expected_mode"] if "expected_mode" in case else None,
                    "result": value, "verified": verified, **score_candidates(case, verified)}
                rows.append(row)
                write_json(args.output / (case["id"] + ".json"), row)
                print(json.dumps({k: row[k] for k in ("id", "model_ms", "candidate_count", "mode_correct")}), flush=True)
            for case in cases["relations"]:
                text = "待判断命题：" + case["claim"] + "\n证据：" + case["evidence"]
                value, duration = infer(model, torch, text, relation_schema)
                actual = label_of(value, "evidence_relation")
                row = {**case, "actual_relation": actual, "correct": actual == case["expected_relation"],
                       "model_ms": duration, "result": value}
                relations.append(row)
                write_json(args.output / (case["id"] + ".json"), row)
            with closing(memory._connect()) as db:
                report["stored_records"] = dict(db.execute("SELECT kind,count(*) FROM pm_records GROUP BY kind"))
            assert report["stored_records"] == {"experience": len(rows)}
            report["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        report["completed"] = True
    except Exception as exc:
        report["completed"] = False
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        raise
    finally:
        scored = [row for row in rows if row["expected_mode"] is not None]
        report.update(elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            blocked_network_connections=len(blocked_connections), warmups=warmups, rows=rows, relations=relations,
            model_calls_started=calls["started"], model_calls_completed=calls["completed"],
            source_unchanged=all(digest_file(SOURCE / name) == digest for name, digest in source_hashes.items()),
            model_unchanged=all(digest_file(args.model_path / name) == digest for name, digest in model_hashes.items()),
            extraction={"cases": len(scored), "mode_correct": sum(row["mode_correct"] for row in scored),
                "people_exact_cases": sum(row["people_exact"] for row in scored),
                "people_by_gold_presence": {str(present): {
                    "cases": sum(bool(case["expected_people"]) == present for case in cases["cases"]),
                    "correct": sum(row["people_exact"] for row in scored if any(
                        case["id"] == row["id"] and bool(case["expected_people"]) == present for case in cases["cases"]))}
                    for present in (True, False)},
                "required_quotes": sum(len(row["required_quotes"]) for row in scored),
                "quotes_exact": sum(sum(row["quote_exact"]) for row in scored),
                "quotes_covered": sum(sum(row["quote_covered"]) for row in scored),
                "all_quotes_covered_cases": sum(all(row["quote_covered"]) for row in scored),
                "candidate_count": sum(row["candidate_count"] for row in scored),
                "rejected_count": sum(row["rejected_count"] for row in scored),
                "model_latency": quantiles([row["model_ms"] for row in scored]),
                "integration_latency": quantiles([row["integration_ms"] for row in scored]),
                "confusion": dict(Counter(row["expected_mode"] + "->" + str(row["actual_mode"]) for row in scored))},
            relation_classification={"cases": len(relations), "correct": sum(row["correct"] for row in relations),
                "constant_insufficient_correct": sum(row["expected_relation"] == "insufficient" for row in relations),
                "per_class": {label: {"cases": sum(row["expected_relation"] == label for row in relations),
                    "correct": sum(row["correct"] for row in relations if row["expected_relation"] == label)} for label in RELATIONS},
                "model_latency": quantiles([row["model_ms"] for row in relations])})
        write_json(args.output / "summary.json", report)
        print(json.dumps({k: report[k] for k in ("completed", "extraction", "relation_classification")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
