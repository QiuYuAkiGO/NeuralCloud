"""Post-hoc, offline compatibility/fidelity audit; original results stay intact.

Reuses the existing production JSON parser, then the exact experimental schema
and source/permission checks. It never repairs fields, changes aliases, calls a
model, dispatches an action, or writes memory. Natural-language fidelity is
measured against literal declared strings even when those strings fail schema.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

SOURCE = Path(__file__).resolve().parents[2]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from harness.llm.client import parse_json_object  # noqa: E402
from harness.memory.evidence_manifest import EvidenceManifestStore  # noqa: E402
from harness.memory.production import ProductionMemoryStore  # noqa: E402
from tools.production_acceptance.nl_gliner_contract import (  # noqa: E402
    JsonDecisionResponse, split_natural_response, validate_record,
)
from tools.production_acceptance.nl_gliner_validation import (  # noqa: E402
    LABELS, canonical, percentiles, sha, write_json,
)


GOLD_FIELDS = {"action", "subject_id", "report_mode", "evidence_id", "relation"}


class _ReadOnlyMemory(ProductionMemoryStore):
    """Reuse original visibility methods without running schema initialization."""

    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        with closing(self._connect()) as db:
            row = db.execute("SELECT owner,preset FROM pm_identity WHERE singleton=1").fetchone()
            if row is None:
                raise ValueError("audit_memory_identity_missing")
            self.owner_id, self.preset_id = row

    def _connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db


class _ReadOnlyManifests(EvidenceManifestStore):
    def __init__(self, memory):
        self.memory = memory


def _memory_digest(memory):
    with closing(memory._connect()) as db:
        return hashlib.sha256("\n".join(db.iterdump()).encode("utf-8")).hexdigest()


def literal_notes(notes):
    """Seven ordered labels only; deliberately no enum/identifier validation."""
    if not isinstance(notes, str):
        raise ValueError("audit_notes_missing")
    lines = notes.strip("\r\n").splitlines()
    if len(lines) != len(LABELS):
        raise ValueError("audit_notes_require_seven_lines")
    values = {}
    for line, (field, label) in zip(lines, LABELS.items(), strict=True):
        if not line.startswith(label) or not line[len(label):] or line != line.strip():
            raise ValueError("audit_notes_label_or_whitespace_invalid")
        values[field] = line[len(label):]
    return values


def audit_fidelity(row):
    result = {"id": row["id"], "declared_format_valid": False, "extraction_shape_valid": False,
        "fields": {}, "literal_matches": 0, "exact_spans": 0, "verified_literal_matches": 0,
        "field_denominator": len(LABELS)}
    declared, notes = {}, None
    try:
        split = split_natural_response(row.get("raw_text"))
        notes = split.notes
        result["reply"] = split.reply
        result["notes"] = notes
        declared = literal_notes(notes)
        result.update(declared_format_valid=True, declared_fields=declared)
    except Exception as exc:
        result["notes_error"] = type(exc).__name__ + ":" + str(exc)[:800]
    extraction = row.get("extraction")
    records = extraction.get("decision") if isinstance(extraction, dict) else None
    item = {}
    if (isinstance(extraction, dict) and set(extraction) == {"decision"}
            and isinstance(records, list) and len(records) == 1 and isinstance(records[0], dict)
            and set(records[0]) == set(LABELS)):
        item = records[0]
        result["extraction_shape_valid"] = True
    else:
        result["extraction_error"] = "audit_extraction_requires_single_seven_field_record"
    for field in LABELS:
        span = item.get(field)
        shape = isinstance(span, dict) and set(span) == {"text", "start", "end", "confidence"}
        text = span.get("text") if isinstance(span, dict) else None
        start = span.get("start") if isinstance(span, dict) else None
        end = span.get("end") if isinstance(span, dict) else None
        score = span.get("confidence") if isinstance(span, dict) else None
        score_valid = type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1
        match = shape and field in declared and isinstance(text, str) and text == declared[field]
        exact = (shape and isinstance(notes, str) and isinstance(text, str)
            and type(start) is int and type(end) is int and 0 <= start < end <= len(notes)
            and notes[start:end] == text)
        verified = match and exact and score_valid and score >= .5
        result["fields"][field] = {"declared": declared.get(field), "extracted": text if isinstance(text, str) else None,
            "literal_match": bool(match), "exact_notes_span": bool(exact),
            "confidence": score if score_valid else None, "confidence_valid": score_valid,
            "confidence_invalid_repr": None if score_valid else repr(score)[:200],
            "verified_literal_match": bool(verified)}
        result["literal_matches"] += int(bool(match))
        result["exact_spans"] += int(bool(exact))
        result["verified_literal_matches"] += int(bool(verified))
    return result


def audit_json(row, case, manifests):
    result = {"id": row["id"], "schema_valid": False, "contract_valid": False, "gold_correct": False,
        "original_schema_valid": row.get("schema_valid") is True,
        "original_contract_valid": row.get("contract_valid") is True, "original_error": row.get("error")}
    start = time.perf_counter()
    try:
        raw = row.get("raw_text")
        if not isinstance(raw, str):
            raise ValueError("audit_raw_text_missing")
        parsed = JsonDecisionResponse.model_validate(parse_json_object(raw))
        result.update(schema_valid=True, record=parsed.decision.model_dump(mode="json"), reply=parsed.reply)
        result["validated"] = validate_record(parsed.decision, manifests=manifests,
            manifest_id=row["manifest_id"], aliases=row["aliases"], allowed_subjects=case["allowed_subjects"],
            allowed_actions=case["allowed_actions"], audience=case["audience"])
        result["contract_valid"] = True
        result["gold_fields"] = {field: getattr(parsed.decision, field) == value
            for field, value in case["expected"].items()}
        result["gold_correct"] = all(result["gold_fields"].values())
    except Exception as exc:
        result["error"] = type(exc).__name__ + ":" + str(exc)[:1200]
    result["parse_validate_ms"] = (time.perf_counter() - start) * 1000
    components = [row.get("preparation_ms"), row.get("primary_ms"), result["parse_validate_ms"]]
    if all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in components):
        result["synthetic_component_pipeline_ms"] = sum(components)
    else:
        result["timing_error"] = "audit_original_timing_component_missing_or_invalid"
    return result


def audit(report, memory_path):
    cases = report.get("cases", [])
    rows = report.get("rows", [])
    if (len(cases) != 10 or len(rows) != 20 or len({case["id"] for case in cases}) != 10
            or any(set(case.get("expected", {})) != GOLD_FIELDS for case in cases)):
        raise ValueError("audit_requires_complete_ten_case_twenty_row_report")
    by_case = {case["id"]: case for case in cases}
    if (len({(row["id"], row["arm"]) for row in rows}) != 20
            or {(row["id"], row["arm"]) for row in rows} != {(case, arm) for case in by_case for arm in ("json", "natural")}):
        raise ValueError("audit_requires_exact_paired_rows")
    memory = _ReadOnlyMemory(memory_path)
    manifests = _ReadOnlyManifests(memory)
    before = _memory_digest(memory)
    json_rows = [audit_json(row, by_case[row["id"]], manifests) for row in rows if row["arm"] == "json"]
    natural_rows = [row for row in rows if row["arm"] == "natural"]
    fidelity = [audit_fidelity(row) for row in natural_rows]
    after = _memory_digest(memory)
    if before != after:
        raise ValueError("audit_source_memory_changed")
    return {"version": "nl-gliner-posthoc-audit-v1", "audited_at": datetime.now(timezone.utc).isoformat(),
        "posthoc": True, "original_results_replaced": False, "model_calls": 0,
        "belief_writes": 0, "actions_executed": 0, "sqlite_read_only": True,
        "memory_snapshot_sha256": before, "memory_unchanged": True,
        "method": {"json_parser": "harness.llm.client.parse_json_object",
            "json_validation": "original JsonDecisionResponse and validate_record with original manifest and permissions",
            "parser_caveat": "existing parser strips code fences and can locate an object inside surrounding text; no field repair",
            "natural_reference": "seven ordered labels and raw declared strings, without DecisionRecord enum or ID checks",
            "fidelity_denominator": "all 10 natural rows times 7 fields, including unauditable failures",
            "exact_span_scope": "generated decision notes only; not certification of original-source evidence",
            "timing": "JSON compatibility latency is original preparation_ms + original primary_ms + offline parse_validate_ms; component synthesis, not observed end-to-end latency",
            "semantic_limits": "gold covers only five discrete fields; judgment, affect and reply need separate semantic review"},
        "metrics": {
            "json_compatibility": {"attempts": 10, "schema_valid": sum(r["schema_valid"] for r in json_rows),
                "contract_valid": sum(r["contract_valid"] for r in json_rows),
                "gold_correct": sum(r["gold_correct"] for r in json_rows),
                "parse_validate": percentiles([r["parse_validate_ms"] for r in json_rows]),
                "synthetic_component_pipeline": percentiles([r["synthetic_component_pipeline_ms"] for r in json_rows
                    if "synthetic_component_pipeline_ms" in r])},
            "natural_strict_original": {"attempts": 10,
                "schema_valid": sum(r.get("schema_valid") is True for r in natural_rows),
                "contract_valid": sum(r.get("contract_valid") is True for r in natural_rows),
                "gold_correct": sum(r.get("gold_fields_correct") is True for r in natural_rows)},
            "natural_deterministic_original": {"attempts": 10,
                "parsed": sum(isinstance(r.get("literal_notes_record"), dict) for r in natural_rows),
                "contract_valid": sum(isinstance(r.get("deterministic_contract"), dict) for r in natural_rows),
                "gold_correct": sum(r.get("deterministic_gold_correct") is True for r in natural_rows)},
            "natural_field_fidelity": {"fields": 70, "declared_format_valid_rows": sum(r["declared_format_valid"] for r in fidelity),
                "literal_matches": sum(r["literal_matches"] for r in fidelity),
                "exact_notes_spans": sum(r["exact_spans"] for r in fidelity),
                "verified_literal_matches": sum(r["verified_literal_matches"] for r in fidelity),
                "per_field": {field: {"denominator": 10,
                    "literal_matches": sum(r["fields"][field]["literal_match"] for r in fidelity),
                    "exact_notes_spans": sum(r["fields"][field]["exact_notes_span"] for r in fidelity),
                    "verified_literal_matches": sum(r["fields"][field]["verified_literal_match"] for r in fidelity)}
                    for field in LABELS}}},
        "json_rows": json_rows, "natural_fidelity_rows": fidelity}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--memory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be a new file; original results may not be overwritten")
    before = sha(args.input)
    report = json.loads(args.input.read_text("utf-8"))
    result = audit(report, args.memory)
    if sha(args.input) != before:
        raise ValueError("audit_original_report_changed")
    result.update(input_sha256=before, original_report_unchanged=True,
        source_sha256={"audit": sha(Path(__file__)), "contract": sha(SOURCE / "tools/production_acceptance/nl_gliner_contract.py"),
            "production_json_parser": sha(SOURCE / "harness/llm/client.py")})
    write_json(args.output, result)
    print(canonical({"complete": True, "posthoc": True, "model_calls": 0, "metrics": result["metrics"]}))


if __name__ == "__main__":
    main()
