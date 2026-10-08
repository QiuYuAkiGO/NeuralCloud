"""Real structured-critic acceptance, with frozen inputs and no message transport.

Run with the candidate checkout on PYTHONPATH. Credentials are read only by the
normal provider client; reports whitelist HTTP timings/usage, never headers.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
from pathlib import Path
import time

from harness.config import Settings
from harness.core.inference import CandidateResponse, MemoryEvidenceContext
from harness.core.models import PersonaCore
from harness.llm.structured import build_structured_client
from harness.validator.validator import ResponseValidator
from infra.trace.diagnostics import observation_scope


def read_cases(path: Path) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    identities, ids = set(), set()
    for case in cases:
        identity = json.dumps({k: case.get(k) for k in ("text", "expected", "evidence", "evidence_fixture")},
                              ensure_ascii=False, sort_keys=True)
        if identity in identities or case["id"] in ids:
            raise ValueError("duplicate_acceptance_case")
        if case["expected"] not in {"pass", "revise"}:
            raise ValueError("invalid_acceptance_expectation")
        identities.add(identity)
        ids.add(case["id"])
    return cases


def percentile95(values: list[float]) -> float | None:
    return sorted(values)[math.ceil(.95 * len(values)) - 1] if values else None


def http_observer(events: list[dict]):
    def capture(event):
        events.append({k: event[k] for k in (
            "record_type", "request_id", "elapsed_ms", "http_status", "error_type",
            "usage", "response_model", "endpoint", "finish_reason", "response_content",
        ) if k in event})
    return capture


def summarize(rows: list[dict], *, expected_runs: int) -> dict:
    positive = [r for r in rows if r["expected"] == "pass"]
    critical = [r for r in rows if r["critical"]]
    failures = [{"id": r["id"], "repeat": r["repeat"], "expected": r["expected"],
                 "actual": r["actual"], "violations": r["violations"]} for r in rows if not r["ok"]]
    usage = [e["usage"] for r in rows for e in r["http"] if e.get("usage")]
    tokens = sum(u.get("prompt_tokens", 0) for u in usage)
    cached = sum(u.get("prompt_cache_hit_tokens", (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
                 for u in usage)
    positive_rate = sum(r["ok"] for r in positive) / len(positive) if positive else 0
    return {"completed": len(rows), "expected_runs": expected_runs, "matched": len(rows) - len(failures),
            "critical_runs": len(critical), "critical_matched": sum(r["ok"] for r in critical),
            "positive_runs": len(positive), "positive_pass_rate": positive_rate,
            "p95_seconds": percentile95([r["seconds"] for r in rows]),
            "http_requests": sum(e.get("record_type") == "http_request_prepared" for r in rows for e in r["http"]),
            "prompt_tokens": tokens, "cached_prompt_tokens": cached,
            "cache_token_fraction": cached / tokens if tokens else None,
            "failures": failures,
            "accepted": len(rows) == expected_runs and all(r["ok"] for r in critical) and positive_rate >= .95}


def run_case(case: dict, repeat: int, *, settings: Settings, persona: PersonaCore, fixtures: dict) -> dict:
    reports, events = [], []

    class Critic:
        def __init__(self):
            self._structured = build_structured_client(settings)

        def complete_json(self, **kwargs):
            report = self._structured.complete_json(**kwargs)
            reports.append(report)
            return report

    client = Critic()
    context = (MemoryEvidenceContext.model_validate(fixtures[case["evidence_fixture"]])
               if "evidence_fixture" in case else MemoryEvidenceContext(
                   audience="qq:group:test", current_speaker_id="alice", entries=case["evidence"]))
    started = time.monotonic()
    row = {"id": case["id"], "repeat": repeat, "candidate": case["text"],
           "expected": case["expected"], "critical": case["critical"]}
    try:
        with observation_scope(http_observer(events)):
            result = ResponseValidator(settings=settings, llm=client).check(
                CandidateResponse(text=case["text"]), persona=persona, memory_evidence=context)
        row.update(actual=result.verdict, violations=result.violations, validation=result.model_dump(mode="json"),
                   ok=result.verdict == case["expected"] and "semantic_review_unavailable" not in result.violations)
    except Exception as exc:
        row.update(actual="error", violations=[type(exc).__name__], ok=False)
    finally:
        client._structured.close()
    row.update(seconds=round(time.monotonic() - started, 4), reports=reports, http=events)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("conversation_acceptance_cases.json"))
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--structured-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1 or not 1 <= args.workers <= 4:
        parser.error("positive repeats and 1..4 workers required")
    cases = read_cases(args.cases)
    frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
    persona = PersonaCore.model_validate(frozen["persona"])
    settings = Settings(_env_file=None, structured_llm_config=args.structured_config, validator_enabled=True,
                        validator_sample_rate=0, call_log_enabled=False, model_request_log_enabled=False)
    rows = []
    report = {"schema_version": 1, "qq_messages_sent": 0, "production_state_writes": 0,
              "case_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
              "frozen_sha256": hashlib.sha256(args.frozen.read_bytes()).hexdigest(), "rows": rows}
    expected_runs = len(cases) * args.repeats
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_case, case, repeat, settings=settings, persona=persona,
                               fixtures=frozen.get("fixtures", {}))
                   for repeat in range(1, args.repeats + 1) for case in cases]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            report["summary"] = summarize(rows, expected_runs=expected_runs)
            temporary = args.output.with_suffix(".tmp")
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(args.output)
            print(json.dumps({k: row[k] for k in ("id", "repeat", "ok", "actual", "seconds")}), flush=True)
    return 0 if report["summary"]["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
