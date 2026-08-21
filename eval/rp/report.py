"""Markdown comparison report: raw vs harness, with version stamps and trace pointers."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from harness.config import Settings

from eval.rp.schema import RPSample


def write_report(
    samples: list[RPSample],
    path: Path,
    *,
    settings: Settings,
    bench: str,
    extra_notes: list[str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    by_mode: dict[str, list[RPSample]] = defaultdict(list)
    for sample in samples:
        by_mode[sample.mode].append(sample)
    raw_agg = _aggregate(by_mode.get("raw", []))
    harness_agg = _aggregate(by_mode.get("harness", []))
    keys = sorted(set(raw_agg) | set(harness_agg))
    generated = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"# RP eval: {bench}",
        "",
        f"- generated_at: {generated}",
        f"- model_version: {settings.model_version}",
        f"- prompt_version: {settings.prompt_version}",
        f"- schema_version: {settings.schema_version}",
        f"- n_raw: {len(by_mode.get('raw', []))}",
        f"- n_harness: {len(by_mode.get('harness', []))}",
        f"- n_errors: {sum(1 for item in samples if item.error)}",
        "",
        "## Summary",
        "",
        "| metric | raw | harness | delta (harness - raw) |",
        "| --- | --- | --- | --- |",
    ]
    if not keys:
        lines.append("| (no scores) | — | — | — |")
    for key in keys:
        raw_v = raw_agg.get(key)
        har_v = harness_agg.get(key)
        delta = _delta(raw_v, har_v)
        lines.append(f"| {key} | {_fmt(raw_v)} | {_fmt(har_v)} | {delta} |")
    lines += [
        "",
        f"- raw mean latency_ms: {_fmt(_mean([s.latency_ms for s in by_mode.get('raw', [])]))}",
        f"- harness mean latency_ms: {_fmt(_mean([s.latency_ms for s in by_mode.get('harness', [])]))}",
        "",
    ]
    notes = list(extra_notes or [])
    for sample in samples:
        note = sample.meta.get("judge_note")
        if note and note not in notes:
            notes.append(str(note))
    if notes:
        lines.append("## Notes")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")
    lines += [
        "## Samples",
        "",
        "| episode_id | mode | latency_ms | scores | trace_ref |",
        "| --- | --- | --- | --- | --- |",
    ]
    for sample in samples:
        score_s = ", ".join(f"{k}={v:.3f}" for k, v in sorted(sample.scores.items())) or "—"
        if sample.error:
            score_s = f"ERROR: {sample.error}"
        trace = sample.trace_ref or "—"
        lines.append(
            f"| {sample.episode_id} | {sample.mode} | {sample.latency_ms:.1f} | {score_s} | `{trace}` |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _aggregate(samples: list[RPSample]) -> dict[str, float]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        if sample.error:
            continue
        for key, value in sample.scores.items():
            buckets[key].append(float(value))
    return {key: sum(values) / len(values) for key, values in buckets.items() if values}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.3f}"


def _delta(raw: float | None, harness: float | None) -> str:
    if raw is None or harness is None:
        return "—"
    return f"{harness - raw:+.3f}"
