"""CLI for live paired evaluation and release/smoke soaks."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from harness.config import Settings
from harness.homeostasis import load_profile
from harness.llm.client import build_llm_client, resolve_aux_endpoint
from harness.research.manifest import load_experiment_manifest

from .evaluation import (
    DEFAULT_PERSONA_PATH,
    DEFAULT_PROFILE_PATH,
    RELEASE_SOAK_MIN_SECONDS,
    AcceptanceThresholds,
    EvaluationMetadata,
    EvaluationSession,
    default_scenarios,
    initialize_fresh_homeostasis_state,
    load_misty_persona,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "presets" / "runtime" / "development-demo.json"
DEFAULT_REPORT_ROOT = ROOT / "eval" / "reports" / "homeostatic_expression"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paired causal evaluation for homeostasis-driven expression.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the behavioral nominal-vs-marked matrix (never release PASS).")
    _common_args(run)
    run.add_argument("--pairs", type=int, default=30, help="Pairs per condition (default: 30).")

    soak = sub.add_parser("soak", help="Run a release-gated live soak with fresh profile-v3 state.")
    _common_args(soak)
    soak.add_argument("--duration-minutes", type=float, default=30.0)
    soak.add_argument("--state-dir", type=Path, default=None)

    smoke = sub.add_parser(
        "soak-smoke",
        help="Exercise live soak wiring quickly; this command can never release-pass.",
    )
    _common_args(smoke)
    smoke.add_argument("--cycles", type=int, default=1)
    smoke.add_argument("--state-dir", type=Path, default=None)
    return parser


def _common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement: call configured main/judge model endpoints.",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--persona", type=Path, default=DEFAULT_PERSONA_PATH)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--max-tokens", type=int, default=240)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("live model calls are opt-in; pass --live after checking NC_LLM_* / NC_AUX_LLM_*")
    if args.temperature < 0 or args.temperature > 2:
        parser.error("--temperature must be within [0, 2]")
    if args.max_tokens < 1:
        parser.error("--max-tokens must be positive")
    if args.command == "run" and args.pairs < 1:
        parser.error("--pairs must be positive")

    settings = Settings()
    manifest = load_experiment_manifest(args.manifest)
    profile = load_profile(args.profile)
    persona_prompt, persona_version, persona_sha = load_misty_persona(args.persona)
    del persona_prompt
    if persona_version != manifest.persona_version:
        parser.error(f"manifest persona {manifest.persona_version!r} does not match preset {persona_version!r}")
    if profile.revision != 3 or manifest.homeostatic_profile != profile.profile_ref:
        parser.error(
            "evaluation requires the manifest and file to select the same profile revision 3 "
            f"(manifest={manifest.homeostatic_profile}, file={profile.profile_ref})"
        )
    if manifest.schema_version != "homeostasis-state-v2":
        parser.error("evaluation requires schema_version=homeostasis-state-v2")
    if manifest.prompt_version != "homeostatic-interoception-v1":
        parser.error("evaluation requires prompt_version=homeostatic-interoception-v1")
    if manifest.homeostatic_expression.mode != "full":
        parser.error("release evaluation requires homeostatic_expression.mode=full")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"homeostatic-expression-{args.command}-{stamp}"
    output_dir = args.out or (DEFAULT_REPORT_ROOT / run_id)
    if output_dir.exists() and any(output_dir.iterdir()):
        parser.error(f"--out must be a new or empty directory: {output_dir}")
    requested_pairs = args.pairs if args.command == "run" else (30 if args.command == "soak" else args.cycles)
    fresh_state_ref = None
    fresh_state_path = None
    if args.command in {"soak", "soak-smoke"}:
        if args.command == "soak" and args.duration_minutes * 60 < RELEASE_SOAK_MIN_SECONDS:
            parser.error("release soak requires --duration-minutes >= 30")
        if args.command == "soak-smoke" and args.cycles < 1:
            parser.error("--cycles must be positive")
        state_dir = args.state_dir or output_dir.with_name(output_dir.name + "-fresh-homeostasis-state")
        state_resolved = state_dir.resolve()
        output_resolved = output_dir.resolve()
        if (
            state_resolved == output_resolved
            or output_resolved in state_resolved.parents
            or state_resolved in output_resolved.parents
        ):
            parser.error("--state-dir must be separate from --out")
        fresh_state_ref = initialize_fresh_homeostasis_state(state_dir, profile_path=args.profile)
        fresh_state_path = str(state_dir.resolve())

    metadata = EvaluationMetadata(
        run_id=run_id,
        started_at=datetime.now(UTC),
        generator_model=settings.llm_model,
        judge_model=resolve_aux_endpoint(settings)[1],
        runtime_manifest_model=manifest.model_version,
        prompt_version=manifest.prompt_version,
        profile_ref=manifest.homeostatic_profile,
        state_schema_version=manifest.schema_version,
        persona_version=persona_version,
        persona_sha256=persona_sha,
        manifest_ref=manifest.manifest_ref,
        manifest_sha256=manifest.content_hash(),
        generation_temperature=args.temperature,
        max_tokens=args.max_tokens,
        requested_pairs_per_condition=requested_pairs,
        fresh_state_ref=fresh_state_ref,
        fresh_state_path=fresh_state_path,
    )
    client = build_llm_client(settings)
    try:
        session = EvaluationSession(
            generator=client,
            judge=client,
            metadata=metadata,
            output_dir=output_dir,
            scenarios=default_scenarios(),
            thresholds=AcceptanceThresholds(),
            persona_path=args.persona,
            profile_path=args.profile,
            runtime_manifest=manifest,
        )
        if args.command == "run":
            artifacts = session.run(pairs_per_condition=args.pairs)
        elif args.command == "soak":
            artifacts = session.run_soak(duration_seconds=args.duration_minutes * 60.0)
        else:
            artifacts = session.run_soak_smoke(cycles=args.cycles)
    finally:
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()
    print(artifacts.report_path.read_text(encoding="utf-8"))
    print(f"evidence: {artifacts.evidence_path}")
    print(f"summary: {artifacts.summary_path}")
    return 0 if artifacts.passed else 1


if __name__ == "__main__":
    sys.exit(main())
