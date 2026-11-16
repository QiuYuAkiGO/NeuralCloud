"""Offline calibration and revision-checked activation; no model/network calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .evidence_index import EvidenceIndex, VERSION as INDEX_VERSION
from .rebuild_batch import VERSION as EXTRACTOR_VERSION


def calibrate(labels, *, minimum=30, precision=0.95):
    """Threshold selection uses dev only; the frozen test split decides activation."""
    ids = [r["id"] for r in labels]
    if len(ids) != len(set(ids)):
        raise ValueError("DUPLICATE_LABEL")
    for r in labels:
        if (
            r["split"] not in ["dev", "test"]
            or type(r["correct"]) is not bool
            or r["basis"] not in ["direct", "inferred"]
            or not 0 <= r["confidence"] <= 1
        ):
            raise ValueError("LABEL_SCHEMA")
    policies, direct = {}, []
    details = []
    for basis, predicate in sorted({(r["basis"], r["predicate"]) for r in labels}):
        rows = [r for r in labels if r["basis"] == basis and r["predicate"] == predicate]
        dev = [r for r in rows if r["split"] == "dev"]
        blind = [r for r in rows if r["split"] == "test"]
        thresholds = sorted({r["confidence"] for r in dev}) if basis == "inferred" else [0]
        threshold = None
        for value in thresholds:
            selected = [r for r in dev if r["confidence"] >= value]
            if len(selected) >= minimum and sum(r["correct"] for r in selected) / len(selected) >= precision:
                threshold = value
                break
        selected = [r for r in blind if threshold is not None and r["confidence"] >= threshold]
        actual = sum(r["correct"] for r in selected) / len(selected) if selected else 0
        enabled = len(selected) >= minimum and actual >= precision
        detail = {
            "basis": basis,
            "predicate": predicate,
            "threshold": threshold,
            "blind_count": len(selected),
            "precision": actual,
            "enabled": enabled,
        }
        details.append(detail)
        if enabled and basis == "direct":
            direct.append(predicate)
        if enabled and basis == "inferred":
            policies[predicate] = {"threshold": threshold, "blind_count": len(selected), "precision": actual}
    return {
        "format": "misty-evidence-quality-v1",
        "index_version": INDEX_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "label_sha256": hashlib.sha256(json.dumps(labels, sort_keys=True).encode()).hexdigest(),
        "approved_direct_predicates": direct,
        "inference_policy": policies,
        "evaluations": details,
        "minimum_blind_count": minimum,
        "minimum_precision": precision,
    }


def load_policy(path):
    policy = json.loads(Path(path).read_text())
    if (
        policy.get("format") != "misty-evidence-quality-v1"
        or policy.get("index_version") != INDEX_VERSION
        or policy.get("extractor_version") != EXTRACTOR_VERSION
    ):
        raise ValueError("CALIBRATION_POLICY_VERSION")
    approved = policy.get("approved_direct_predicates", [])
    inferred = policy.get("inference_policy", {})
    for basis, predicates in [("direct", approved), ("inferred", inferred)]:
        for predicate in predicates:
            evidence = next(
                (r for r in policy.get("evaluations", []) if r["basis"] == basis and r["predicate"] == predicate), None
            )
            if (
                not evidence
                or not evidence.get("enabled")
                or evidence.get("precision", 0) < 0.95
                or evidence.get("blind_count", 0) < 30
            ):
                raise ValueError("CALIBRATION_EVIDENCE_REQUIRED")
    return policy


def activate(index, policy):
    if not index.writable:
        raise ValueError("READ_ONLY_INDEX")
    changed = 0
    with index.lock, index.db:
        # No guessed fact is enabled merely because it has a high confidence score.
        for row in index.db.execute("SELECT * FROM facts WHERE extractor!='rules-v1'").fetchall():
            active = False
            if (
                row["extractor"] == policy["extractor_version"]
                and row["confirmation"] == "model_verified"
                and index.valid_refs(json.loads(row["refs"]))
            ):
                if row["basis"] == "direct":
                    active = row["predicate"] in policy["approved_direct_predicates"]
                elif row["basis"] == "inferred":
                    calibrated = policy["inference_policy"].get(row["predicate"], {})
                    active = (
                        calibrated.get("precision", 0) >= 0.95
                        and calibrated.get("blind_count", 0) >= 30
                        and row["confidence"] >= calibrated.get("threshold", 1.1)
                    )
            if bool(row["active"]) != active:
                index.db.execute("UPDATE facts SET active=? WHERE id=?", (int(active), row["id"]))
                changed += 1
        index.db.execute(
            "INSERT OR REPLACE INTO metadata VALUES('calibration_sha256',?)",
            (hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),),
        )
        index.db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
    return {"changed": changed, "index": index.status()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("calibrate")
    c.add_argument("--labels", type=Path, required=True)
    c.add_argument("--output", type=Path, required=True)
    a = sub.add_parser("activate")
    for name in ["source", "index", "policy"]:
        a.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.command == "calibrate":
        result = calibrate(json.loads(args.labels.read_text()))
        args.output.write_text(json.dumps(result, indent=2))
    else:
        policy = load_policy(args.policy)
        index = EvidenceIndex(args.source, args.index, writable=True)
        try:
            result = activate(index, policy)
        finally:
            index.close()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
