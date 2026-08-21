"""CLI: python -m eval.rp.run_eval --bench roleeval --mode both --limit 20"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from harness.config import Settings
from harness.llm.client import LLMClient, OpenAICompatClient
from harness.llm.embeddings import MockEmbedding
from infra.trace.recorder import JsonlTracer

from eval.rp.datasets import load_bench
from eval.rp.fetch import hint_for
from eval.rp.fixtures import eval_mock_llm, mini_episodes
from eval.rp.judges import CharacterRMJudge, apply_judges
from eval.rp.paths import REPORTS_DIR, TRACES_DIR
from eval.rp.report import write_report
from eval.rp.runners import HarnessRunner, RawRunner
from eval.rp.schema import DatasetMissingError, RPEpisode, RPSample, RunMode

ALL_BENCHES = ("charactereval", "roleeval", "timechara")


def run(
    *,
    benches: list[str],
    modes: list[RunMode],
    limit: int | None,
    settings: Settings,
    llm: LLMClient,
    use_fixtures: bool = False,
    out_dir: Path | None = None,
    trace_dir: Path | None = None,
    character_rm: CharacterRMJudge | None = None,
) -> list[Path]:
    out_dir = out_dir or REPORTS_DIR
    trace_dir = trace_dir or TRACES_DIR
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    rm = character_rm or CharacterRMJudge()
    written: list[Path] = []
    for bench in benches:
        episodes = _load(bench, limit=limit, use_fixtures=use_fixtures)
        trace_path = trace_dir / f"rp_{bench}_{stamp}.jsonl"
        tracer = JsonlTracer(trace_path, settings=settings)
        samples: list[RPSample] = []
        notes: list[str] = []
        if "raw" in modes:
            raw = RawRunner(llm, settings=settings, tracer=tracer)
            for episode in episodes:
                sample = raw.run(episode)
                samples.append(apply_judges(episode, sample, character_rm=rm))
        if "harness" in modes:
            harness = HarnessRunner(
                llm,
                settings=settings,
                embeddings=MockEmbedding(dim=settings.embedding_dim),
                tracer=tracer,
            )
            for episode in episodes:
                sample = harness.run(episode)
                samples.append(apply_judges(episode, sample, character_rm=rm))
        if bench == "charactereval" and not rm.available:
            notes.append(rm.unavailable_reason or "CharacterRM unavailable")
        report_path = out_dir / f"rp_{bench}_{stamp[:8]}.md"
        write_report(samples, report_path, settings=settings, bench=bench, extra_notes=notes)
        written.append(report_path)
    return written


def _load(bench: str, *, limit: int | None, use_fixtures: bool) -> list[RPEpisode]:
    if use_fixtures:
        episodes = [ep for ep in mini_episodes() if ep.bench == bench]
        if limit is not None:
            episodes = episodes[:limit]
        if not episodes:
            raise DatasetMissingError(bench, f"no fixture episodes for {bench}")
        return episodes
    return load_bench(bench, limit=limit)


def _parse_modes(value: str) -> list[RunMode]:
    if value == "both":
        return ["raw", "harness"]
    if value in {"raw", "harness"}:
        return [value]  # type: ignore[list-item]
    raise ValueError(f"unknown mode: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CharacterEval / RoleEval / TimeChara against raw vs harness.")
    parser.add_argument("--bench", default="roleeval", help="charactereval | roleeval | timechara | all")
    parser.add_argument("--mode", default="both", choices=["raw", "harness", "both"])
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--mock", action="store_true", help="Use MockLLM (no GPU).")
    parser.add_argument("--fixtures", action="store_true", help="Use built-in mini episodes, skip eval/external/.")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    benches = list(ALL_BENCHES) if args.bench == "all" else [args.bench]
    modes = _parse_modes(args.mode)
    settings = Settings(
        record_interaction=False,
        continuation_enabled=False,
        validator_sample_rate=0.0,
        session_window=40,
        model_version="mock" if args.mock or args.fixtures else Settings().llm_model,
    )
    if args.mock or args.fixtures:
        llm: LLMClient = eval_mock_llm()
    else:
        llm = OpenAICompatClient(settings)
    try:
        paths = run(
            benches=benches,
            modes=modes,
            limit=args.limit,
            settings=settings,
            llm=llm,
            use_fixtures=args.fixtures,
            out_dir=args.out,
        )
    except DatasetMissingError as exc:
        print(exc.hint or hint_for(exc.bench))
        return 2
    for path in paths:
        print(path.read_text(encoding="utf-8"))
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
