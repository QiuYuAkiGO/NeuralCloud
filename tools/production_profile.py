"""Generate an opt-in production manifest without changing its research source."""
import argparse
import json
from pathlib import Path

from harness.production.config import ProductionConfig
from harness.research.manifest import ExperimentManifest, load_experiment_manifest


def build(source, *, body_instance_id):
    original = load_experiment_manifest(Path(source))
    value = original.model_dump(mode="json")
    value.update(experiment_id="neuralcloud-embodied-production-v1", revision=1,
        condition="llm_top3", grounded_agency=None, emotion_mode="off", prompt_version="embodied-production-v1",
        heartbeat_seconds=5, homeostatic_expression={"mode": "off"}, goal_regulation={"enabled": False},
        basic_terminal={"enabled": False}, production=ProductionConfig().model_dump(mode="json"))
    value["embodiment"] = {"mode": "grounded", "body_provider": "mistyai", "body_instance_id": body_instance_id,
        "required_for_runtime": False, "capability_requirements": [
            {"capability": name, "required": False} for name in (
                "observation.snapshot.v1", "evidence.reference.v1", "physiology.body-state.v1",
                "physiology.virtual-resource-ledger.v1", "physiology.rest.v1", "physiology.cognitive-usage.v1")]}
    return ExperimentManifest.model_validate(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--body-instance-id", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    manifest = build(args.source, body_instance_id=args.body_instance_id)
    with Path(args.destination).open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"manifest_ref": manifest.manifest_ref, "path": str(Path(args.destination).resolve()),
        "deployed": False, "body_instance_id": args.body_instance_id}))


if __name__ == "__main__":
    main()
