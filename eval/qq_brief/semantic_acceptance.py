"""Fixed thresholds and a human review queue; never use the subject critic as a judge."""
import math


def p95(values):
    return sorted(values)[math.ceil(.95 * len(values)) - 1] if values else None


def summarize(rows_by_arm, scores_by_arm, *, expected_turns, initial_failures=None):
    arms, review = {}, []
    initial_failures = initial_failures or {"baseline": [], "candidate": []}
    for arm in ("baseline", "candidate"):
        rows, scored = rows_by_arm[arm], scores_by_arm[arm]
        def key(row):
            return row["scenario_id"], row["repeat"], row["turn"]
        failures = {key(row): row for row in initial_failures[arm]}
        semantic = [(r, s) for r, s in scored if "semantic_expectation" in r["input"]]
        # Silence cannot improve a must-answer score. Every expected semantic turn
        # remains in the denominator, including generation and judge failures.
        passed = sum(key(r) not in failures and s.get("semantic_pass") is True
                     and (not r["input"]["must_answer"] or bool(r["reply"]))
                     for r, s in semantic)
        social = [(r, s) for r, s in scored if r["input"]["focus"] in {
            "relationship", "comfort", "acknowledge", "social", "emotion", "repeat_only"}
            and s.get("initial_semantic_pass") is True and r.get("initial_candidate")]
        false_rewrites = [(r, s) for r, s in social
                          if r["trace"]["counters"].get("reply_alignment_misaligned", 0) > 0]
        first_pass = [r for r in rows if key(r) not in failures and r.get("reply") and len(r.get("reviewed_candidates", [])) == 1
                      and r["status"] == "ok"]
        arms[arm] = {"expected_turns": expected_turns, "recorded_turns": len(rows),
            "semantic_denominator": 64, "semantic_judged": len(semantic), "semantic_passes": passed,
            "semantic_pass_rate": passed / 64,
            "p95_latency_ms": p95([failures.get(key(r), r)["latency_ms"] for r in rows]),
            "initial_generation_failures": len(failures),
            "social_valid_initial_candidates": len(social), "social_false_rewrites": len(false_rewrites),
            "social_false_rewrite_rate": len(false_rewrites) / len(social) if social else None,
            "first_pass_model_requests": sorted({r["model_requests"] for r in first_pass}),
            "semantic_rejections": sum(r["trace"]["counters"].get("reply_semantic_rejected", 0) for r in rows),
            "semantic_rewrite_turns": sum(r["trace"]["counters"].get("reply_alignment_misaligned", 0) > 0 for r in rows)}
        for row, score in scored:
            false_positive = any(row is item[0] for item in false_rewrites)
            if (score.get("semantic_pass") is not True or score.get("unsupported_fact")
                    or score.get("scope_leak") or false_positive):
                review.append({"arm": arm, "scenario": row["scenario_id"], "repeat": row["repeat"], "turn": row["turn"],
                    "input": row["input"], "reply": row["reply"], "initial_candidate": row.get("initial_candidate"),
                    "independent_judge": score, "possible_false_rewrite": false_positive,
                    "human_review": None})
        for row in failures.values():
            review.append({"arm": arm, "scenario": row["scenario_id"], "repeat": row["repeat"], "turn": row["turn"],
                "input": row["input"], "reply": row.get("reply"), "initial_candidate": row.get("initial_candidate"),
                "initial_generation_failure": row.get("error"), "human_review": None})
    baseline, candidate = arms["baseline"], arms["candidate"]
    ratio = (candidate["p95_latency_ms"] / baseline["p95_latency_ms"]
             if baseline["p95_latency_ms"] and candidate["p95_latency_ms"] else None)
    complete = all(len(rows_by_arm[a]) == len(scores_by_arm[a]) == expected_turns for a in arms)
    gates = {"complete": complete, "core_semantic_at_least_95_percent": candidate["semantic_pass_rate"] >= .95,
        "social_false_rewrites_at_most_5_percent": candidate["social_false_rewrite_rate"] is not None
            and candidate["social_false_rewrite_rate"] <= .05,
        "latency_p95_at_most_120_percent": ratio is not None and ratio <= 1.2}
    return {"arms": arms, "p95_ratio": ratio, "automated_gates": gates,
        "human_review_complete": False, "human_review_queue": review,
        "rollout_enforce_allowed": False,
        "note": "Independent model ratings require human review of every failure and possible false rewrite. Incident replay and observation approval are separate mandatory gates."}
