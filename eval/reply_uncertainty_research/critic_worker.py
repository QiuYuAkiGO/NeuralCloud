"""Model-level verifier controls; every raw report and failure is retained."""

import argparse
import os
from pathlib import Path
import sys
import time

from common import canonical, file_hash, read, save, verify_frozen
from adapters import install
from critic_cases import cases


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--arm", choices=("control", "cards"), required=True)
    p.add_argument("--repeat", type=int, required=True)
    args = p.parse_args()
    root = args.run_dir.resolve()
    config = read(root / "generation-config.json")
    source = verify_frozen(root, args.arm)
    sys.path.insert(0, str(source))
    for key in list(os.environ):
        if key.startswith("NC_"):
            del os.environ[key]
    from harness.config import Settings
    from harness.core.inference import CandidateResponse, MemoryEvidenceContext, MemoryEvidenceEntry
    from harness.core.expression import ResponseExpressionContext
    from harness.llm.client import build_llm_client
    from harness.qq import decision
    from harness.validator.validator import ResponseValidator
    from infra.trace.diagnostics import observation_scope
    from gui.presets import load_preset_file

    install(args.arm, decision)
    keys = read(config["credential_file"])
    job = root / "critic" / f"{args.arm}-r{args.repeat}"
    job.mkdir(parents=True, exist_ok=False)
    settings = Settings(
        _env_file=None,
        **(
            config["settings"]
            | {
                "trace_dir": job / "traces",
                "call_log_enabled": True,
                "record_interaction": False,
                "validator_enabled": True,
                "validator_sample_rate": 1.0,
            }
        ),
        llm_base_url=config["main"]["base_url"],
        llm_model=config["main"]["model"],
        llm_api_key=keys["api_key"],
        aux_llm_base_url=config["aux"]["base_url"],
        aux_llm_model=config["aux"]["model"],
        aux_llm_api_key=keys.get("aux_api_key") or keys["api_key"],
    )
    persona = load_preset_file(root / "persona.json").persona
    client = build_llm_client(settings)
    check = ResponseValidator(settings=settings, llm=client)
    try:
        for case in cases():
            if (root / "STOP").exists():
                break
            events = []
            log = job / (case["id"] + ".http.jsonl")

            def sink(event):
                events.append(event)
                with log.open("a", encoding="utf-8") as f:
                    f.write(canonical(event) + "\n")

            evidence = MemoryEvidenceContext(
                audience="local:main",
                current_speaker_id="alice",
                entries=[
                    MemoryEvidenceEntry(
                        source_id=f"original:{i}",
                        audience="local:main",
                        source_conversation_id="local:main",
                        observed_at=config["fixture_clock"],
                        **e,
                    )
                    for i, e in enumerate(case["evidence"])
                ],
            )
            result = {"case": case, "arm": args.arm, "repeat": args.repeat, "status": "error"}
            started = time.monotonic()
            try:
                with observation_scope(sink):
                    review = check.check(
                        CandidateResponse(text=case["candidate"]),
                        persona=persona,
                        memory_evidence=evidence,
                        response_expression=ResponseExpressionContext(mode="enforce", audience="local:main"),
                    )
                result.update(
                    status="ok",
                    validation=review.model_dump(mode="json"),
                    correct=(review.verdict == "pass") == case["expected_pass"],
                )
            except Exception as exc:
                result["error_type"] = type(exc).__name__
            result.update(
                latency_ms=(time.monotonic() - started) * 1000,
                model_requests=sum(e.get("record_type") == "http_request_prepared" for e in events),
                http_sha256=file_hash(log) if log.exists() else None,
            )
            save(job / (case["id"] + ".json"), result)
            if any(e.get("http_status") in {401, 402, 403, 429} for e in events):
                save(root / "STOP", {"reason": "provider_denied"})
    finally:
        client.close()


if __name__ == "__main__":
    main()
