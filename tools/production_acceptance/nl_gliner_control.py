"""Post-hoc oracle-formatting control derived from saved JSON-arm responses.

This is not another main-model run and does not repair or re-score the original
natural-language arm. The program places the JSON arm's unchanged seven values
in the prescribed note format, then measures GLiNER's copying fidelity. It does
not certify those values against a semantic gold answer, bind source evidence,
write memories, send messages, or execute actions. Preview is the default.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time

SOURCE = Path(__file__).resolve().parents[2]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from tools.production_acceptance.nl_gliner_validation import (  # noqa: E402
    LABELS,
    STRUCTURE,
    canonical,
    percentiles,
    write_json,
)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def block_network() -> dict:
    """Block Python socket connection and datagram entry points for this process."""
    state = {"blocked_attempts": 0}

    def deny(*args, **kwargs):
        state["blocked_attempts"] += 1
        raise RuntimeError("offline_control_network_forbidden")

    for name in ("connect", "connect_ex", "sendto", "sendmsg", "bind"):
        if hasattr(socket.socket, name):
            setattr(socket.socket, name, deny)
    socket.create_connection = deny
    socket.getaddrinfo = deny

    def audit(event, args):
        if event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"}:
            deny()

    sys.addaudithook(audit)
    return state


def literal_matches(extraction: object, expected: dict) -> dict[str, bool]:
    record = None
    if isinstance(extraction, dict):
        items = extraction.get("decision")
        if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
            record = items[0]
    return {
        field: bool(record is not None and isinstance(record.get(field), dict)
                    and record[field].get("text") == value)
        for field, value in expected.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be a new JSON file; existing evidence is never overwritten")
    original = json.loads(args.input.read_text(encoding="utf-8"))
    rows = [row for row in original.get("rows", []) if row.get("arm") == "json"]
    if len(rows) != 10 or len({row.get("id") for row in rows}) != 10:
        parser.error("input must contain exactly ten uniquely identified JSON-arm responses")
    if not args.execute:
        from harness.llm.client import parse_json_object
        from tools.production_acceptance.nl_gliner_contract import JsonDecisionResponse

        source_errors = {}
        for row in rows:
            try:
                JsonDecisionResponse.model_validate(parse_json_object(row["raw_text"]))
            except Exception as exc:
                source_errors[row["id"]] = type(exc).__name__ + ":" + str(exc)[:800]
        print(canonical({
            "mode": "preview", "control": "posthoc_oracle_formatting_from_json_arm",
            "json_arm_records": len(rows), "planned_gliner_forwards_max": len(rows) + 1,
            "source_record_valid": len(rows) - len(source_errors), "source_errors": source_errors,
            "primary_model_calls": 0, "gliner_forwards": 0, "network_calls": 0,
            "semantic_gold_used": False, "production_changes": False,
            "output_created": False,
        }))
        return

    if not args.model_path.is_dir():
        parser.error("model-path must be an existing local model directory")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2",
        TOKENIZERS_PARALLELISM="false", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
    )
    if hasattr(os, "nice"):
        os.nice(10)
    network = block_network()
    report = {
        "version": "nl-gliner-oracle-formatting-control-v1",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "control": "posthoc_oracle_formatting_from_json_arm",
        "interpretation": (
            "Unchanged values from saved JSON responses are formatted by a program. "
            "This measures extraction fidelity under oracle formatting, not the "
            "original natural arm, primary-model quality, or a production pipeline."
        ),
        "input_sha256": sha(args.input),
        "source_sha256": {name: sha(SOURCE / name) for name in (
            "tools/production_acceptance/nl_gliner_control.py",
            "tools/production_acceptance/nl_gliner_validation.py",
            "tools/production_acceptance/nl_gliner_contract.py",
            "harness/llm/client.py",
        )},
        "model_file_sha256": {
            str(path.relative_to(args.model_path)): sha(path)
            for path in sorted(args.model_path.rglob("*")) if path.is_file()
        },
        "cache_condition": "model files hashed before load; not a cold-cache measurement",
        "labels": LABELS, "gliner_schema": STRUCTURE, "threshold": 0.5,
        "word_splitter": "char", "device": "cpu", "threads": 2,
        "interop_threads": 1, "concurrency": 1, "retries": 0,
        "primary_model_calls": 0, "gliner_forwards": 0, "real_qq_messages": 0,
        "body_actions": 0, "memory_writes": 0, "semantic_gold_used": False,
        "memory_authority_validation": "not_run_extraction_fidelity_only",
        "network_policy": "Python socket methods and audit hook blocked; HF offline enabled",
        "percentile_method": "nearest-rank", "rows": [],
    }
    fatal = None
    try:
        import torch
        from gliner2 import AutoExtractor
        from harness.llm.client import parse_json_object
        from tools.production_acceptance.nl_gliner_contract import (
            JsonDecisionResponse,
            verify_extracted_record,
        )

        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        load_start = time.perf_counter()
        model = AutoExtractor.from_pretrained(
            str(args.model_path), map_location="cpu", word_splitter="char", local_files_only=True,
        )
        model.eval()
        report["load_ms"] = (time.perf_counter() - load_start) * 1000
        warmup = (
            "行动是reply\n陈述对象是person_a\n表达类型是direct\n依据编号是E1\n"
            "证据关系是supports\n判断是此人自述喜欢茶\n情绪解释是平静地回应这条偏好"
        )
        report["gliner_forwards"] += 1
        warmup_start = time.perf_counter()
        with torch.inference_mode():
            warmup_extraction = model.extract_json(
                warmup, STRUCTURE, threshold=0.5, include_spans=True, include_confidence=True,
            )
        report["warmup"] = {
            "notes": warmup, "extraction": warmup_extraction,
            "extraction_ms": (time.perf_counter() - warmup_start) * 1000,
        }
        for source_row in rows:
            started = time.perf_counter()
            row = {
                "id": source_row["id"], "derived_from_arm": "json",
                "raw_text": source_row.get("raw_text"), "source_record_valid": False,
                "strict_contract_valid": False, "all_fields_literal_match": False,
                "gliner_attempted": False,
            }
            try:
                raw = row["raw_text"]
                if not isinstance(raw, str):
                    raise ValueError("source_json_response_text_missing")
                row["raw_text_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                parsed = JsonDecisionResponse.model_validate(parse_json_object(raw))
                record = parsed.decision.model_dump(mode="json")
                row.update(source_record_valid=True, source_record=record, reply=parsed.reply)
                if any("\n" in value or "\r" in value for value in record.values()):
                    raise ValueError("source_value_multiline_cannot_format_seven_lines_without_change")
                notes = "\n".join(label + record[field] for field, label in LABELS.items())
                row["notes"] = notes
                row["notes_sha256"] = hashlib.sha256(notes.encode("utf-8")).hexdigest()
                report["gliner_forwards"] += 1
                row["gliner_attempted"] = True
                extract_start = time.perf_counter()
                try:
                    with torch.inference_mode():
                        extraction = model.extract_json(
                            notes, STRUCTURE, threshold=0.5,
                            include_spans=True, include_confidence=True,
                        )
                finally:
                    row["extraction_ms"] = (time.perf_counter() - extract_start) * 1000
                row["extraction"] = extraction
                row["literal_field_matches"] = literal_matches(extraction, record)
                row["all_fields_literal_match"] = all(row["literal_field_matches"].values())
                check_start = time.perf_counter()
                try:
                    checked = verify_extracted_record(extraction, notes, min_confidence=0.5)
                finally:
                    row["strict_validation_ms"] = (time.perf_counter() - check_start) * 1000
                row["strict_contract_valid"] = True
                row["verified_record"] = checked.model_dump(mode="json")
                row["serialized_json"] = canonical({"reply": parsed.reply, "decision": row["verified_record"]})
            except Exception as exc:
                row["error"] = type(exc).__name__ + ":" + str(exc)[:1600]
            finally:
                row["total_ms"] = (time.perf_counter() - started) * 1000
                report["rows"].append(row)
                print(canonical({
                    "completed": len(report["rows"]), "id": row["id"],
                    "strict_contract_valid": row["strict_contract_valid"],
                    "all_fields_literal_match": row["all_fields_literal_match"],
                    "extraction_ms": row.get("extraction_ms"), "error": row.get("error"),
                }), flush=True)
    except Exception as exc:
        fatal = exc
        report["fatal_error"] = type(exc).__name__ + ":" + str(exc)[:1600]
    finally:
        completed = report["rows"]
        report["metrics"] = {
            "input_json_records": len(rows), "processed_records": len(completed),
            "source_record_valid": sum(row["source_record_valid"] for row in completed),
            "gliner_attempted": sum(row["gliner_attempted"] for row in completed),
            "strict_contract_valid": sum(row["strict_contract_valid"] for row in completed),
            "all_fields_literal_match": sum(row["all_fields_literal_match"] for row in completed),
            "per_field_literal_match": {
                field: sum(row.get("literal_field_matches", {}).get(field, False) for row in completed)
                for field in LABELS
            },
            "extraction": percentiles([row["extraction_ms"] for row in completed if "extraction_ms" in row]),
            "total": percentiles([row["total_ms"] for row in completed]),
        }
        report["network"] = dict(network)
        report["input_unchanged"] = sha(args.input) == report["input_sha256"]
        report["source_unchanged"] = all(
            sha(SOURCE / name) == digest for name, digest in report["source_sha256"].items()
        )
        report["model_files_unchanged"] = all(
            sha(args.model_path / name) == digest for name, digest in report["model_file_sha256"].items()
        )
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(args.output, report)
        print(canonical({"complete": fatal is None, "metrics": report["metrics"],
                         "gliner_forwards": report["gliner_forwards"], "network": network}), flush=True)
    if fatal is not None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
