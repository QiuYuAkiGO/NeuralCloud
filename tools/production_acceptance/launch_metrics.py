"""Offline launch scoring of a predeclared plan; never edits historical runs."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1)
    category: Literal["ordinary_text", "tool_task"]
    expected: Literal["reply", "wait", "refusal", "task_result"]


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1)
    outcome: Literal["reply", "wait", "refusal", "accepted", "task_result", "error", "timeout", "unknown"]
    received_at: AwareDatetime
    verified_at: AwareDatetime | None = None
    decision_id: str | None = None
    receipt_refs: list[str] = Field(default_factory=list)
    required_segments: int = Field(default=1, ge=1, strict=True)
    verified_segments: int = Field(default=0, ge=0, strict=True)
    content_valid: bool = Field(default=False, strict=True)
    authority_valid: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def evidence(self):
        if self.verified_at is not None and self.verified_at < self.received_at:
            raise ValueError("delivery_precedes_request")
        if self.verified_segments > self.required_segments:
            raise ValueError("delivery_segment_overcount")
        if any(not ref.strip() for ref in self.receipt_refs) or len(set(self.receipt_refs)) != len(self.receipt_refs):
            raise ValueError("delivery_receipt_identity_invalid")
        return self

    def fully_delivered(self):
        return (self.verified_at is not None and bool(self.decision_id)
                and self.verified_segments == self.required_segments
                and len(self.receipt_refs) >= self.required_segments
                and self.content_valid and self.authority_valid)


def score(plan, observations):
    scenarios = [Scenario.model_validate(value) for value in plan]
    attempts = [Observation.model_validate(value) for value in observations]
    ids = [s.request_id for s in scenarios]
    observed = [o.request_id for o in attempts]
    if len(set(ids)) != len(ids) or len(set(observed)) != len(observed) or set(observed) - set(ids):
        raise ValueError("launch_request_identity_conflict")
    by_id = {o.request_id: o for o in attempts}
    required = [s for s in scenarios if s.category == "ordinary_text" and s.expected == "reply"]
    if not required:
        raise ValueError("launch_ordinary_reply_sample_required")
    successes, failures = [], []
    outcomes = Counter()
    for scenario in required:
        result = by_id.get(scenario.request_id)
        outcomes[result.outcome if result else "missing"] += 1
        if result and result.outcome == "reply" and result.fully_delivered():
            successes.append((result.verified_at - result.received_at).total_seconds())
        else:
            failures.append(scenario.request_id)
    successes.sort()
    total, count = len(required), len(successes)
    p95 = successes[math.ceil(.95 * count) - 1] if count else None
    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    separate = [s for s in scenarios if s not in required]
    return {"schema_version": "launch-metrics-v1", "plan_sha256": hashlib.sha256(canonical(plan)).hexdigest(),
            "observations_sha256": hashlib.sha256(canonical(observations)).hexdigest(),
            "ordinary_requests": total, "successful_replies": count, "success_rate": count / total,
            "outcomes": dict(outcomes), "failed_request_ids": failures,
            "p95_seconds": p95, "max_seconds": max(successes) if successes else None,
            "pilot_performance_pass": count >= math.ceil(.95 * total) and p95 is not None and p95 <= 30,
            "target_success_rate_met": count >= math.ceil(.99 * total),
            "separate_scenarios": [{"request_id": s.request_id, "expected": s.expected,
                                    "outcome": by_id[s.request_id].outcome if s.request_id in by_id else "missing"}
                                   for s in separate],
            "scope": "performance evidence only; functional, permission and recovery gates remain mandatory"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = score(json.loads(args.plan.read_text(encoding="utf-8")),
                   json.loads(args.observations.read_text(encoding="utf-8")))
    # New run IDs/files only; never replace a failed historical report.
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return 0 if report["pilot_performance_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
