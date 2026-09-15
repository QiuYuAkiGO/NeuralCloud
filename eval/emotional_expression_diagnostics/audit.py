"""Visibility and immutable treatment audit without external requests."""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

from harness.compiler.compiler import render_homeostatic_interoception

from .fixtures import treatments
from .models import DiagnosticRunSpec, digest


def audit_spec(spec: DiagnosticRunSpec) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    rows = []
    for case in spec.cases:
        for treatment in treatments(spec, case):
            row = {"case_id": case.case_id, "cluster_id": case.cluster_id, "family": case.family,
                   "route": case.route, "variant": case.variant, "condition": treatment.name,
                   "baseline_kind": treatment.baseline_kind, "factor": treatment.factor,
                   "payload_sha256": digest(treatment.payload), "information_sha256": treatment.information_sha256,
                   "summary_sha256": digest(case.summary),
                   "renderer_sha256": digest(render_homeostatic_interoception(case.summary)),
                   "information_reviewed": case.information_reviewed,
                   "payload_status": "captured_http_verified" if treatment.name == "A-prod" else
                       "synthetic_payload_not_production_http" if treatment.name == "A-synthetic-proxy" else "manipulated_pending_http"}
            rows.append(row)
            groups[(case.cluster_id, treatment.name)].append(row)
    a_references = []
    for case in spec.cases:
        if not case.actual_request:
            continue
        original = json.loads(case.actual_request.request_body_utf8)
        expected = treatments(spec, case)[1].payload
        user_messages = [message.get("content") for message in original["messages"] if message.get("role") == "user"]
        original_options = {key: value for key, value in original.items() if key != "messages"}
        expected_options = {key: value for key, value in expected.items() if key != "messages"}
        a_references.append({"case_id": case.case_id, "reference_only": True,
                             "user_text_matches": bool(user_messages and user_messages[-1] == case.user_text),
                             "generation_options_match": original_options == expected_options,
                             "mismatched_option_names": sorted(key for key in original_options.keys() | expected_options.keys()
                                                                if original_options.get(key) != expected_options.get(key)),
                             "causal_attribution": "A-prod is a product reference, not an isolated H1 contrast"})
    unobservable = []
    for (cluster, condition), items in groups.items():
        if len({item["variant"] for item in items}) > 1 and len({item["payload_sha256"] for item in items}) == 1:
            unobservable.append({"cluster_id": cluster, "condition": condition,
                                 "case_ids": [item["case_id"] for item in items], "status": "unobservable_contrast"})
    base = len(spec.cases) * (5 * spec.repeats + 3)
    extra_conditions = (2 if "H2" in spec.extras else 0) + (1 if "E-style" in spec.extras else 0) + (2 if "mode" in spec.extras else 0)
    total = base + len(spec.cases) * spec.repeats * extra_conditions
    return {"schema_version": "expression-visibility-v1", "spec_sha256": digest(spec), "release_eligible": False,
            "planned_base_calls": base, "planned_extra_calls": total - base, "planned_calls": total,
            "independent_clusters": len({case.cluster_id for case in spec.cases}),
            "actual_http_cases": sum(case.actual_request is not None for case in spec.cases),
            "synthetic_cases": sum(case.source_kind == "synthetic" for case in spec.cases),
            "unobservable_contrasts": unobservable, "conditions": rows, "a_prod_references": a_references,
            "confirmation_eligible": spec.phase == "confirmation" and all(case.information_reviewed and case.information_reviewer for case in spec.cases)}


def preflight(spec: DiagnosticRunSpec, *, live: bool = False) -> dict[str, Any]:
    audit = audit_spec(spec)
    problems = []
    if live:
        if not spec.live_authorized:
            problems.append("live_not_authorized_in_frozen_spec")
        if not spec.price.verified_at or not spec.price.source.startswith("https://") or spec.price.input_per_million <= 0 or spec.price.output_per_million <= 0:
            problems.append("live_prices_unverified")
        if spec.generation.model == "offline-fake" or "synthetic.invalid" in spec.generation.endpoint:
            problems.append("live_endpoint_unconfigured")
        if any(case.source_kind != "synthetic" and (not case.actual_request or not case.actual_request.cloud_replay_authorized) for case in spec.cases):
            problems.append("private_case_cloud_replay_not_authorized")
        for case in spec.cases:
            if case.actual_request:
                if not case.actual_request.cloud_replay_authorized:
                    problems.append("actual_request_cloud_replay_not_authorized")
                if case.actual_request.endpoint != spec.generation.endpoint:
                    problems.append("actual_request_endpoint_mismatch")
                if json.loads(case.actual_request.request_body_utf8)["model"] != spec.generation.model:
                    problems.append("actual_request_model_mismatch")
    per_call = (spec.budget.max_input_tokens * spec.price.input_per_million + spec.generation.max_tokens * spec.price.output_per_million) / 1_000_000
    return {"schema_version": "expression-preflight-v1", "spec_sha256": digest(spec), "live": live,
            "passed": not problems, "problems": sorted(set(problems)), "release_eligible": False,
            "plan": {key: audit[key] for key in ("planned_base_calls", "planned_extra_calls", "planned_calls", "independent_clusters")},
            "max_requests": spec.budget.max_requests, "request_cap_can_stop_before_plan_end": spec.budget.max_requests < audit["planned_calls"],
            "conservative_reserve_per_call_cny": per_call,
            "conservative_plan_reserve_cny": per_call * audit["planned_calls"],
            "stop_limit_cny": 45.0, "hard_limit_cny": 50.0,
            "notes": ["A synthetic proxy is not a verified A-prod baseline.",
                      "Pilot repeats do not increase the number of independent scenarios.",
                      "Helpers, revisions, Cursor reviews and optional contrasts are separately counted."]}
