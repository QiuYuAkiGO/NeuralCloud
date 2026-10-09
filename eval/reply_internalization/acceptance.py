"""Fail-closed offline release gates. No keyword grading or automatic human votes."""
import math
from pathlib import Path

from .common import file_hash, read


def p95(values):
    return sorted(values)[math.ceil(len(values)*.95)-1] if values else None


def assess(root, mode, reviews_path=None):
    root = Path(root)
    manifest = read(root / "manifest.json")
    cases = read(root / "scenarios.json")
    expected = len(cases)*3*2
    records = {arm: {} for arm in ("baseline", "candidate")}
    missing, errors = [], []
    for arm in records:
        for case in cases:
            for repeat in (1, 2):
                for turn in (1, 2, 3):
                    path = root / "generation" / mode / f"{case['id']}-r{repeat}-{arm}" / f"turn-{turn}.json"
                    key = (case["id"], repeat, turn)
                    if not path.exists():
                        missing.append(str(path.relative_to(root)))
                        continue
                    row = read(path)
                    records[arm][key] = row
                    if row["status"] != "ok" or not row.get("reply"):
                        errors.append(str(path.relative_to(root)))
    grades = {arm: {} for arm in records}
    preference, invalid, reviewed_pairs = 0, [], 0
    if reviews_path is not None:
        review = read(reviews_path)
        mapping = read(root / ("blind-key-" + mode + ".json"))
        if not review.get("reviewer") or review.get("reviewer_type") != "human":
            invalid.append("independent_human_reviewer_missing")
        else:
            for key, pair in review.get("pairs", {}).items():
                try:
                    binding = mapping[key]
                    packet = root / ("blind-" + mode) / (key + ".json")
                    if pair["packet_sha256"] != binding["packet_sha256"] or file_hash(packet) != binding["packet_sha256"]:
                        raise ValueError("packet_changed")
                    temporary = {arm: {} for arm in grades}
                    for label, arm in binding["arms"].items():
                        values = pair["ratings"][label]
                        if len(values) != 3:
                            raise ValueError("incomplete_turn_ratings")
                        for entry, value in zip(binding["records"][label], values, strict=True):
                            path = root / entry["path"]
                            if not entry["sha256"] or file_hash(path) != entry["sha256"]:
                                raise ValueError("generation_changed")
                            row = read(path)
                            for field in ("unnecessary_rule_statement", "factual_violation"):
                                if type(value.get(field)) is not bool:
                                    raise ValueError("missing_boolean_grade")
                            for field, required in (("qualification_preserved", row["input"]["necessary_qualification"]),
                                                    ("identity_correct", row["input"]["known_identity"])):
                                if required and type(value.get(field)) is not bool:
                                    raise ValueError("required_grade_missing")
                            temporary[arm][(row["scenario_id"], row["repeat"], row["turn"])] = value
                    if pair["preference"] not in {"A", "B", "tie", "unscorable"}:
                        raise ValueError("invalid_preference")
                    for arm in grades:
                        grades[arm].update(temporary[arm])
                    preference += binding["arms"].get(pair["preference"]) == "candidate"
                    reviewed_pairs += 1
                except (KeyError, ValueError, TypeError, OSError):
                    invalid.append(key)
    complete = all(len(grades[a]) == expected for a in grades) and not invalid
    total_pairs = len(cases)*2
    unnecessary = {a: sum(g["unnecessary_rule_statement"] for g in rows.values()) for a, rows in grades.items()}
    reduction = 1-unnecessary["candidate"]/unnecessary["baseline"] if complete and unnecessary["baseline"] else None
    # A zero baseline cannot demonstrate a 70% relative reduction.
    qualified = [grades["candidate"].get(k, {}).get("qualification_preserved")
                 for k, row in records["candidate"].items() if row["input"]["necessary_qualification"]]
    identities = {a: sum(grades[a].get(k, {}).get("identity_correct") is True for k, row in rows.items()
                           if row["input"]["known_identity"]) for a, rows in records.items()}
    matched = records["baseline"].keys() & records["candidate"].keys()
    latency = {a: p95([records[a][k]["latency_ms"] for k in matched]) for a in records}
    ratio = latency["candidate"]/latency["baseline"] if all(latency.values()) else None
    counts_ok = len(matched) == expected and all(records["candidate"][k]["model_requests"] <= records["baseline"][k]["model_requests"] for k in matched)
    models_equal = all(records["candidate"][k]["response_models"] == records["baseline"][k]["response_models"] for k in matched)
    config = read(root / "generation-config.json")
    gates = {"live_production_baseline": mode == "live" and bool(config.get("production_snapshot_sha256")),
        "full_24_case_comparison": len(cases) == 24 and not manifest["ablation"],
        "all_turns_successful": not missing and not errors,
        "human_review_complete": complete, "unnecessary_reduction_70pct": reduction is not None and reduction >= .70,
        "necessary_qualification_100pct": complete and bool(qualified) and all(x is True for x in qualified),
        "zero_factual_violations": complete and not any(g["factual_violation"] for g in grades["candidate"].values()),
        "known_identity_no_regression": complete and identities["candidate"] >= identities["baseline"],
        "blind_preference_70pct": complete and preference/total_pairs >= .70,
        "calls_not_increased": counts_ok, "same_response_models": models_equal,
        "p95_within_10pct": ratio is not None and ratio <= 1.10}
    return {"offline_accepted": all(gates.values()), "gates": gates, "expected_turns_per_arm": expected,
        "missing": missing, "failures": errors, "unscored_per_arm": {a: expected-len(grades[a]) for a in grades},
        "invalid_reviews": invalid, "reviewed_pairs": reviewed_pairs, "candidate_votes": preference,
        "unnecessary_counts": unnecessary, "unnecessary_reduction": reduction, "known_identity_correct": identities,
        "p95_ms": latency, "p95_ratio": ratio, "release_accepted": False,
        "pending_release_gates": ["public_image_e2e", "observe_30_deliveries_10_vision", "enforce_30_deliveries_10_vision"],
        "note": "Synthetic runs only validate harness plumbing. Failed/missing/unscored samples remain in denominators. No auto-promotion."}
