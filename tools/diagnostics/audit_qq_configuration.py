"""Read-only QQ configuration snapshot. Never opens a database or contacts QQ."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from infra.trace.request_spans import safe_endpoint


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def capture(bindings_path: Path) -> dict:
    bindings = load(bindings_path)
    session = load(Path(bindings["NC_QQ_SESSION_CONFIG"]))
    model = load(Path(session["model_config_path"]))
    persona = load(Path(session["preset_path"]))
    start = load(Path(bindings["NC_RUNTIME_STATE_DIR"]) / "active-start.json")
    manifest = load(Path(start["manifest_path"]))
    profile = load(Path(start["profile_path"]))
    snapshot = load(Path(bindings["NC_HOMEOSTASIS_STATE_DIR"]) / "snapshot.json")
    summary = snapshot.get("interoceptive_summary") or {}
    return {
        "schema_version": "qq-expression-configuration-audit-v1",
        "captured_at": datetime.now(UTC).isoformat(), "release_eligible": False,
        "evidence_kind": "read_only_disk_configuration_not_process_attestation",
        "active_model_configuration": {"requested_model": model.get("model"),
            "endpoint": safe_endpoint(model.get("base_url") or ""),
            "credential_available": bool(model.get("api_key")),
            "actual_response_model": None, "provider_defaults_effective_unverified": True},
        "manifest": {"sha256": sha(manifest), "experiment_id": manifest.get("experiment_id"),
            "revision": manifest.get("revision"), "homeostatic_profile": manifest.get("homeostatic_profile"),
            "homeostatic_expression": manifest.get("homeostatic_expression"),
            "model_version": manifest.get("model_version"), "command_timeout_seconds": manifest.get("command_timeout_seconds")},
        "persona": {"working_copy_sha256": sha(persona.get("persona")),
                    "builtin_substitution_used_for_audit": False},
        "homeostasis": {"profile_sha256": sha(profile), "snapshot_sha256": sha(snapshot),
            "profile_id": snapshot.get("profile_id"), "profile_revision": snapshot.get("profile_revision"),
            "snapshot_revision": snapshot.get("revision"), "summary_sha256": sha(summary),
            "summary_revision": summary.get("snapshot_revision"), "salience": summary.get("salience"),
            "same_revision": snapshot.get("revision") == summary.get("snapshot_revision")},
        "routes": {"qq_group": True, "qq_private": bool(session.get("allowed_private_peer_ids")),
                   "shared_services": not session.get("development_mode", False)},
        "actual_reply_requests": {"status": "payload_unverified", "captured": 0,
            "user_marked_flat_examples": 0,
            "reason": "No historical final HTTP payload or user-labeled complaint packet was imported; no original QQ text was exported."},
        "conclusion_boundary": "This audit identifies current disk configuration only. Synthetic HTTP wiring tests and future observer captures carry separate evidence identities.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", type=Path, default=Path("data/shared-misty-launch.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = capture(args.bindings)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"output": str(args.output), "release_eligible": False}))


if __name__ == "__main__":
    main()
