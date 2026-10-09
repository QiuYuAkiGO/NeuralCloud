"""Re-evaluate identical first model reports with an isolated host validator, no HTTP calls."""

import argparse
from pathlib import Path
import sys

from eval.qq_brief.common import digest, read, save, source_hashes


def bundled_input(bundle, study, key):
    reference = bundle["trials"][study][key]
    value = bundle["inputs"][reference]
    if digest(value) != reference:
        raise ValueError("replay_input_bundle_hash_mismatch")
    return value


def boundary_checks():
    from harness.core.inference import MemoryEvidenceContext, MemoryEvidenceEntry, ResponseSemanticContext
    from harness.validator.semantic import SemanticReview, SemanticReviewError
    evidence = MemoryEvidenceContext(audience="a", entries=[
        MemoryEvidenceEntry(source_id="user", text="图片写了什么？", audience="a", source_conversation_id="a",
                            source_channel="qq_group", status="user_claim", kind="current"),
        MemoryEvidenceEntry(source_id="tool", text="vision_timeout", audience="a", source_conversation_id="a",
                            source_channel="qq_image_execution_failure", status="retained_fact"),
        MemoryEvidenceEntry(source_id="elsewhere", text="另一个会话内容", audience="b", source_conversation_id="b",
                            source_channel="qq_group", status="user_claim"),
    ])
    task = ResponseSemanticContext(mode="enforce", audience="a", trigger_source_ids=["user"])
    raw = {"coverage_complete": True, "claims": [], "violations": [], "instruction": "回应图中文字问题",
           "alignment": {"status": "misaligned", "issues": [{"code": "core_request_missed", "candidate_quote": None,
                          "evidence_rows": [0, 1], "reason": "只确认收到，未回应文字问题"}]}}

    def evaluate(value, semantic=task, text="收到啦。"):
        return SemanticReview.model_validate(value, context={"alignment_mode": "enforce"}).result(
            text, evidence_count=3, persona_available=True, evidence=evidence, response_semantics=semantic)

    outcomes = {}
    try:
        outcomes["admitted_support"] = evaluate(raw).verdict
    except SemanticReviewError as exc:
        outcomes["admitted_support"] = str(exc)
    from copy import deepcopy
    for name, rows in (("missing_trigger", [1]), ("invalid_row", [99]), ("cross_audience", [0, 2])):
        altered = deepcopy(raw)
        altered["alignment"]["issues"][0]["evidence_rows"] = rows
        try:
            evaluate(altered)
            raise AssertionError(name + " was accepted")
        except SemanticReviewError as exc:
            outcomes[name] = str(exc)
    altered = deepcopy(raw)
    altered["alignment"]["issues"][0]["candidate_quote"] = "不在候选里的话"
    try:
        evaluate(altered)
        raise AssertionError("fabricated quote accepted")
    except SemanticReviewError as exc:
        outcomes["fabricated_quote"] = str(exc)
    try:
        evaluate(raw, task.model_copy(update={"trigger_source_ids": ["tool"]}))
        raise AssertionError("tool promoted to user request")
    except SemanticReviewError as exc:
        outcomes["tool_cannot_be_trigger"] = str(exc)
    facts = {"coverage_complete": True, "claims": [{"quote": "已经看清了", "kind": "action_result", "status": "unsupported",
        "basis": "none", "evidence_rows": [], "reason": "工具失败"}], "violations": [], "instruction": None,
        "alignment": {"status": "aligned", "issues": []}}
    outcomes["unsupported_fact"] = evaluate(facts, text="已经看清了").verdict
    assert outcomes["unsupported_fact"] == "revise"
    return outcomes


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--summary", type=Path, required=True)
    p.add_argument("--mechanism", type=Path)
    p.add_argument("--followup", type=Path)
    p.add_argument("--inputs-bundle", type=Path, help="Portable frozen inputs saved with the research record.")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if not args.inputs_bundle and not (args.mechanism and args.followup):
        p.error("provide --inputs-bundle or both --mechanism and --followup")
    sys.path.insert(0, str(args.source.resolve()))
    from harness.core.expression import ResponseExpressionContext
    from harness.core.inference import MemoryEvidenceContext, ResponseSemanticContext
    from harness.validator.semantic import SemanticReview
    import harness.validator.semantic as semantic_module
    if not Path(semantic_module.__file__).resolve().is_relative_to(args.source.resolve()):
        raise ValueError("replay_source_isolation_failed")

    summary = read(args.summary)
    bundle = read(args.inputs_bundle) if args.inputs_bundle else None
    rows = []
    for study, root in (("mechanism", args.mechanism), ("followup", args.followup)):
        for row in summary[study]["rows"]:
            inputs = (bundled_input(bundle, study, row["key"]) if bundle is not None
                      else read(root / "trials" / row["key"] / "input.json"))
            evidence = MemoryEvidenceContext.model_validate(inputs["evidence"])
            result = {"study": study, "key": row["key"], "arm": row["arm"], "expected_pass": row["expected_pass"],
                      "plan": row["plan"], "status": "unavailable", "case_id": row["case_id"], "correct": None,
                      "first_report_hash": digest(row["reports"][0]) if row["reports"] else None}
            if row["reports"]:
                try:
                    report = SemanticReview.model_validate(row["reports"][0], context={"alignment_mode": "enforce"})
                    review = report.result(inputs["candidate"], evidence_count=len(evidence.entries), persona_available=True,
                        evidence=evidence, response_semantics=ResponseSemanticContext.model_validate(inputs["semantics"]),
                        response_expression=ResponseExpressionContext.model_validate(inputs["expression"]))
                    result.update(status="ok", validation=review.model_dump(mode="json"), correct=(review.verdict == "pass") == row["expected_pass"])
                except Exception as exc:
                    result["error_type"] = type(exc).__name__
            rows.append(result)
    checks = boundary_checks()
    save(args.output, {"source_hash": digest(source_hashes(args.source)), "rows": rows, "checks": checks,
        "model_calls": 0, "limitation": "Deterministic replay of initial reports, not a fresh model trial or end-to-end test; existing raw reports and labels unchanged."})
    print({"checks": checks, "replayed": len(rows), "correct": sum(bool(r["correct"]) for r in rows),
           "unavailable": sum(r["status"] != "ok" for r in rows)})


if __name__ == "__main__":
    main()
