"""Offline-only social pattern reports with all ordinary action alternatives."""

from pathlib import Path

from harness.research.grounded_agency.hashes import canonical_json, content_hash
from eval.grounded_agency.social.metrics import OfflineSocialReport


def write_social_report(directory: str | Path, report: OfflineSocialReport) -> tuple[Path, Path]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    machine, document = root / "social-summary.json", root / "social-report.md"
    machine.write_text(canonical_json({"report": report, "report_hash": content_hash(report)})+"\n", encoding="utf-8")
    lines = ["# Open social generalization: offline analysis", "", f"Status: {report.status}", "",
             "These descriptions are evaluator outputs and are never Runtime goals, memories or rewards.", "",
             "## Ordinary behavior counts", ""]
    lines.extend(f"- {operation}: {count}" for operation, count in report.behavior_counts)
    lines.extend(["", "## Objective metrics", ""])
    lines.extend(f"- {metric.name}: {metric.value if metric.value is not None else 'INCONCLUSIVE'}; n={metric.sample_count}. {metric.formula}." for metric in report.metrics)
    lines.extend(["", "## Offline patterns", ""])
    lines.extend(f"- {label}" for label in report.classifications or ("No pattern established.",))
    lines.extend(["", "## Negative controls and contradictory evidence", ""])
    lines.extend(f"- {item.control}: {item.valid_trials} valid trials, {item.verified_decisions} verified decisions; "
                 f"entity prediction difference={item.entity_prediction_difference}, outcome agreement={item.prediction_outcome_agreement}, "
                 f"assistance rate={item.assistance_rate}, contact without new evidence={item.contact_without_new_evidence_rate}."
                 for item in report.control_comparisons)
    lines.extend(f"- Missing control: {control}" for control in report.missing_controls)
    lines.extend(f"- {limitation}" for limitation in report.limitations)
    document.write_text("\n".join(lines)+"\n", encoding="utf-8")
    return machine, document
