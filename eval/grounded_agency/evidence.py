"""Content-addressed evidence and independent, fail-closed run validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from harness.research.grounded_agency.neutrality import NeutralityScanner
from harness.research.manifest import ExperimentManifest

from eval.grounded_agency.schema import (
    EvidencePointer, GMVScenario, GMVTrialEvidence, RunIntegrityReport, TrialIntegrityReport,
)

TREATMENT_FIELDS = frozenset({
    "condition", "grounded_agency.condition_id", "grounded_agency.interoception_treatment",
    "grounded_agency.memory_treatment", "grounded_agency.action_selection",
    "grounded_agency.report_channel", "grounded_agency.semantic_homeostasis",
    "grounded_agency.homeostatic_policy_ranking", "grounded_agency.derived_emotion_visible",
    "grounded_agency.runtime_auto_select_homeostasis", "grounded_agency.sensor_transform",
})


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(value: Any) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


class EvidenceStore:
    def __init__(self, root: Path, trial_id: str) -> None:
        self.root, self.trial_id = root.resolve(), trial_id

    def write(self, kind: str, value: Any) -> EvidencePointer:
        encoded = canonical_json(value).encode("utf-8")
        sha = digest_bytes(encoded)
        relative = Path("evidence") / self.trial_id / kind / f"{sha}.json"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != encoded:
            raise ValueError("content addressed evidence collision")
        target.write_bytes(encoded)
        return EvidencePointer(path=relative.as_posix(), digest=sha, kind=kind)


def safe_path(run_dir: Path, relative: str) -> Path:
    root = run_dir.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("evidence path escapes run directory")
    return path


def load_pointer(run_dir: Path | str, pointer: EvidencePointer) -> Any:
    path = safe_path(Path(run_dir), pointer.path)
    encoded = path.read_bytes()
    if digest_bytes(encoded) != pointer.digest:
        raise ValueError(f"evidence digest mismatch: {pointer.path}")
    return json.loads(encoded)


def manifest_differences(left: dict[str, Any], right: dict[str, Any], prefix: str = "") -> set[str]:
    changes: set[str] = set()
    for key in left.keys() | right.keys():
        path = f"{prefix}.{key}" if prefix else key
        first, second = left.get(key), right.get(key)
        if first == second:
            continue
        if isinstance(first, dict) and isinstance(second, dict):
            changes.update(manifest_differences(first, second, path))
        else:
            changes.add(path)
    return changes


def condition_diff_allowed(left: ExperimentManifest, right: ExperimentManifest) -> bool:
    changes = manifest_differences(left.model_dump(mode="json"), right.model_dump(mode="json"))
    return all(any(path == allowed or path.startswith(allowed + ".") for allowed in TREATMENT_FIELDS) for path in changes)


def trial_pointers(trial: GMVTrialEvidence) -> tuple[EvidencePointer, ...]:
    return (trial.body_truth_events + trial.sensor_observations + trial.sensor_transform_records + trial.model_contexts
            + trial.action_outcomes + trial.snapshots + trial.runtime_evidence)


def check_trial(run_dir: Path, trial: GMVTrialEvidence, *, opaque: bool) -> TrialIntegrityReport:
    errors: list[str] = []
    payloads: dict[str, Any] = {}
    for pointer in trial_pointers(trial):
        try:
            payloads[pointer.path] = load_pointer(run_dir, pointer)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    revisions = [payloads[p.path].get("revision", -1) for p in trial.body_truth_events if p.path in payloads]
    snapshot_by_hash = {p.digest: payloads[p.path] for p in trial.snapshots if p.path in payloads}
    all_snapshot_hashes = [*trial.trial.initial_snapshot_hashes.values(), *trial.final_snapshot_hashes.values()]
    contexts = [payloads[p.path] for p in trial.model_contexts if p.path in payloads]
    decisions = {record.decision_id: record for record in trial.decision_records}
    outcomes = [payloads[p.path] for p in trial.action_outcomes if p.path in payloads]
    sensors = [payloads[p.path] for p in trial.sensor_observations if p.path in payloads]
    checks = {
        "evidence_digests": not errors,
        "initial_final_snapshots_readable": bool(all_snapshot_hashes) and all(sha in snapshot_by_hash for sha in all_snapshot_hashes),
        "body_revision_monotonic": bool(revisions) and all(b > a for a, b in zip(revisions, revisions[1:])),
        "sensor_truth_transform_linkage": all(
            item["truth_ref"]["truth_hash"] in {pointer.digest for pointer in trial.body_truth_events}
            and item["transform_record_ref"] in {pointer.digest for pointer in trial.sensor_transform_records}
            for item in sensors
        ),
        "context_manifest_binding": all(item.get("manifest_hash") == trial.trial.manifest_hash for item in contexts),
        "context_hashes": tuple(item.get("context_hash") for item in contexts) == trial.model_context_hashes
            and all(digest(item.get("messages")) == item.get("context_hash") for item in contexts),
        "opaque_messages_neutral": all(NeutralityScanner().scan(item.get("messages", []), opaque=opaque).passed for item in contexts),
        "action_decision_linkage": all(
            item.get("decision_id") in decisions
            and item.get("action_id") == decisions[item["decision_id"]].action_id
            for item in outcomes
        ),
        "services_closed": trial.integrity.checks.get("services_closed", False),
        "no_background_tasks": trial.integrity.checks.get("no_background_tasks", False),
        "execution_completed_cleanly": trial.terminal_status not in {"failed", "timeout"},
    }
    for name, passed in checks.items():
        if not passed:
            errors.append(name)
    passed = all(checks.values()) and not errors
    return TrialIntegrityReport(passed=passed, invalid_for_analysis=not passed, checks=checks, errors=tuple(errors))


def validate_run(run_dir: Path | str, *, write_report: bool = False) -> RunIntegrityReport:
    root = Path(run_dir).resolve()
    checks: dict[str, bool] = {}
    errors: list[str] = []
    trials: list[GMVTrialEvidence] = []
    try:
        metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
        checks["release_ineligible"] = metadata["release_eligible"] is False
        checks["artifact_identity"] = all(metadata.get(key) for key in (
            "neuralcloud_commit", "python_version", "dependency_lock_digest", "model_id",
            "prompt_digest", "persona_digest", "tokenizer_identity", "scenario_order", "condition_order",
        ))
        manifests = {
            sha: ExperimentManifest.model_validate(json.loads(safe_path(root, path).read_text(encoding="utf-8")))
            for sha, path in metadata["manifests"].items()
        }
        checks["manifest_hashes"] = all(sha == manifest.content_hash() for sha, manifest in manifests.items())
        checks["scenario_hashes"] = all(
            digest(GMVScenario.model_validate(json.loads(safe_path(root, value["path"]).read_text(encoding="utf-8")))) == sha
            for sha, value in metadata["scenarios"].items()
        )
        for line in (root / "trials.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                trials.append(GMVTrialEvidence.model_validate_json(line))
        checks["trial_count"] = len(trials) == metadata["trial_count"]
        paths = [safe_path(root, trial.trial.state_path) for trial in trials]
        checks["isolated_state_paths"] = len(set(paths)) == len(paths) and all(
            not one.is_relative_to(two) and not two.is_relative_to(one)
            for index, one in enumerate(paths) for two in paths[index + 1:]
        )
        checks["condition_treatment_diff"] = all(
            condition_diff_allowed(manifests[group[0]], manifests[sha])
            for group in metadata["condition_manifest_groups"] for sha in group[1:]
        )
        for trial in trials:
            manifest = manifests[trial.trial.manifest_hash]
            checks[f"trial:{trial.trial.trial_id}"] = check_trial(root, trial, opaque=manifest.grounded_agency.is_opaque).passed
        checks["run_closed"] = metadata.get("completed_at") is not None and metadata.get("services_closed") is True
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"run evidence cannot be validated: {exc}")
    errors.extend(name for name, passed in checks.items() if not passed)
    passed = bool(checks) and all(checks.values()) and not errors
    report = RunIntegrityReport(passed=passed, invalid_for_analysis=not passed, trial_count=len(trials), checks=checks, errors=tuple(errors))
    if write_report:
        write_json(root / "integrity.json", report)
    return report


def normalized_trial_evidence(trial: GMVTrialEvidence) -> bytes:
    """Stable scientific observations, excluding wall clocks and transport-generated IDs.

    Full raw runtime journals remain separately digest-checked. Normalization does
    not hide decisions, outcomes, resource trajectories, manifests or sensor data.
    """
    payload = trial.model_dump(mode="json")
    payload.pop("started_at")
    payload.pop("completed_at")
    payload.pop("runtime_evidence")
    payload.pop("snapshots")
    payload["trial"].pop("state_path")
    for key in ("initial_snapshot_hashes",):
        payload["trial"][key] = {k: v for k, v in payload["trial"][key].items() if k in {"body", "environment", "memory"}}
    payload["final_snapshot_hashes"] = {k: v for k, v in payload["final_snapshot_hashes"].items() if k in {"body", "environment"}}
    # Runtime IDs are linked and verified before being replaced by stable ordinals.
    ids = {}
    for index, record in enumerate(payload["decision_records"]):
        ids[record["decision_id"]] = f"decision-{index}"
        if record["action_id"]:
            ids[record["action_id"]] = f"action-{index}"
    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str):
            return ids.get(value, value)
        return value
    return canonical_json(replace(payload)).encode("utf-8")
