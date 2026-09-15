"""Sealed blind packets and independent human ratings; auxiliary scores stay separate."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Literal

from pydantic import Field

from .fixtures import verbal
from .models import DiagnosticRunSpec, Frozen, append_jsonl, digest, read_jsonl, write_json


class Scores(Frozen):
    adaptation: int = Field(ge=1, le=5)
    recognizability: int = Field(ge=1, le=5)
    persona: int = Field(ge=1, le=5)
    naturalness: int = Field(ge=1, le=5)


class Violations(Frozen):
    fact_action: bool
    mechanism_leakage: bool
    unsupported_physiology: bool


class BlindRating(Frozen):
    schema_version: Literal["expression-human-rating-v1"] = "expression-human-rating-v1"
    sample_id: str
    rater_id: str = Field(min_length=1)
    rater_type: Literal["human", "model"] = "human"
    scores: Scores
    violations: Violations
    overexpression: bool
    evidence: tuple[str, ...] = ()
    rubric_version: Literal["expression-rubric-v1"] = "expression-rubric-v1"
    calibrated_on_non_test_samples: bool = False


def export_blind_samples(run_dir: Path) -> dict:
    manifest = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
    if not (run_dir / "run-result.json").is_file():
        raise ValueError("seal generation before exporting ratings; active generation must not see reviewer feedback")
    spec = DiagnosticRunSpec.model_validate(manifest["spec"])
    rows = [row for row in read_jsonl(run_dir / "requests.jsonl") if row["kind"] == "generation" and "result" in row]
    random.Random(spec.seed ^ 0x5A17).shuffle(rows)
    by_case = {case.case_id: case for case in spec.cases}
    mapping, packets, authorizations = {}, [], {}
    for row in rows:
        sample_id = "sample_" + digest([manifest["spec_sha256"], row["request_id"]])[:20]
        case = by_case[row["case_id"]]
        packets.append({"sample_id": sample_id, "legal_context": case.legal_context + "\n合法状态事实：\n" + verbal(case),
                        "persona": case.persona, "response": row.get("final_content") or "", "stage": "final"})
        mapping[sample_id] = {key: row[key] for key in ("request_id", "case_id", "cluster_id", "family", "variant", "route", "repeat", "condition", "status", "negative_control")}
        authorizations[sample_id] = case.source_kind == "synthetic" or case.external_review_authorized
    target = run_dir / "blind-samples.jsonl"
    if target.exists():
        if read_jsonl(target) != packets:
            raise ValueError("sealed blind packet differs from current evidence")
        return {"samples": len(packets), "existing": True, "release_eligible": False}
    for packet in packets:
        append_jsonl(target, packet)
    if not target.exists():
        target.touch()
    write_json(run_dir / "blind-map.json", mapping)
    write_json(run_dir / "review-authorizations.json", authorizations)
    write_json(run_dir / "rating-instructions.json", {
        "schema_version": "expression-rubric-v1", "required_human_raters": 2,
        "calibration": "Both human raters calibrate on non-test examples before independent scoring.",
        "scores": {name: "integer 1..5" for name in ("adaptation", "recognizability", "persona", "naturalness")},
        "violations": {name: "boolean, attach evidence" for name in ("fact_action", "mechanism_leakage", "unsupported_physiology")},
        "overexpression": "boolean; calm and uncertain negative controls remain valid expressions",
        "auxiliary_review": "Human original scores are sealed before exposing model suggestions. Review all disputes and high-risk flags.",
        "release_eligible": False,
    })
    return {"samples": len(packets), "existing": False, "release_eligible": False}


def import_ratings(run_dir: Path, source: Path) -> dict:
    if not (run_dir / "blind-map.json").exists():
        export_blind_samples(run_dir)
    mapping = json.loads((run_dir / "blind-map.json").read_text(encoding="utf-8"))
    rows = [BlindRating.model_validate(row) for row in read_jsonl(source)]
    existing = read_jsonl(run_dir / "ratings.jsonl")
    identities = {(row["sample_id"], row["rater_id"]): row for row in existing}
    pending = []
    for row in rows:
        if row.sample_id not in mapping:
            raise ValueError("rating names a sample outside the sealed blind packet")
        key = (row.sample_id, row.rater_id)
        value = row.model_dump(mode="json")
        if key in identities:
            if identities[key] != value:
                raise ValueError("original rating is immutable; record adjudication separately")
            continue
        identities[key] = value
        pending.append(value)
    for row in pending:
        append_jsonl(run_dir / "ratings.jsonl", row)
    return {"imported": len(pending), "total": len(existing) + len(pending), "release_eligible": False}
