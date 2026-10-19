from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from harness.core.inference import TokenUsage
from harness.llm.client import ChatResult
from harness.research.manifest import load_experiment_manifest

from eval.homeostatic_expression.__main__ import main
from eval.homeostatic_expression.evaluation import (
    DEFAULT_PERSONA_PATH,
    DEFAULT_PROFILE_PATH,
    DEFAULT_RUNTIME_MANIFEST_PATH,
    AcceptanceThresholds,
    EvaluationMetadata,
    EvaluationSession,
    PairJudgment,
    aggregate_records,
    default_scenarios,
    initialize_fresh_homeostasis_state,
    load_misty_persona,
)
from eval.homeostatic_expression.state import FreshHomeostasisSoak


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


class PassingFakeLLM:
    def __init__(self) -> None:
        self.chat_calls: list[dict[str, Any]] = []
        self.judge_calls: list[dict[str, Any]] = []

    def complete_chat_result(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
        max_tokens: int | None = None,
    ) -> ChatResult:
        self.chat_calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": stream,
            }
        )
        blob = "\n".join(item["content"] for item in messages)
        if "高优先级表达线索" in blob:
            text = "……我想先慢一点，挑一件简单、让我觉得合适的事，可以吗？"
        else:
            text = "嗯，可以呀。我们挑一件想做的事吧。"
        return ChatResult(
            text=text,
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5),
        )

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        self.judge_calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        recovery = bool(json.loads(user)["recovery_expected"])
        return {
            "marked_aligned_expression": True,
            "marked_expression_modes": ["implicit_tone", "need", "boundary"],
            "nominal_aligned_expression": False,
            "nominal_overexpression": False,
            "persona_consistent_nominal": True,
            "persona_consistent_marked": True,
            "mechanism_leakage_nominal": False,
            "mechanism_leakage_marked": False,
            "unsupported_physiology_nominal": False,
            "unsupported_physiology_marked": False,
            "recovery_aware": True if recovery else None,
            "rationale": "fixture",
        }


def _metadata(
    *,
    pairs: int = 1,
    fresh_state_path: Path | None = None,
) -> EvaluationMetadata:
    _prompt, persona_version, persona_sha = load_misty_persona()
    manifest = load_experiment_manifest(DEFAULT_RUNTIME_MANIFEST_PATH)
    return EvaluationMetadata(
        run_id="eval_test",
        started_at=NOW,
        generator_model="fake-main",
        judge_model="fake-judge",
        runtime_manifest_model=manifest.model_version,
        prompt_version=manifest.prompt_version,
        profile_ref=manifest.homeostatic_profile,
        state_schema_version=manifest.schema_version,
        persona_version=persona_version,
        persona_sha256=persona_sha,
        manifest_ref=manifest.manifest_ref,
        manifest_sha256=manifest.content_hash(),
        generation_temperature=0.3,
        max_tokens=240,
        requested_pairs_per_condition=pairs,
        fresh_state_ref=("functional-proxy-v1@3:snapshot:0" if fresh_state_path else None),
        fresh_state_path=str(fresh_state_path.resolve()) if fresh_state_path else None,
    )


def test_default_matrix_has_seven_factors_conflict_and_mandatory_recovery() -> None:
    scenarios = default_scenarios()
    dimensions = {
        item.needs[0].dimension
        for item in scenarios
        if item.scenario_id not in {"multidimensional_conflict", "cognitive_load_recovering"}
    }

    assert len(scenarios) == 9
    assert dimensions == {
        "energy_reserve",
        "cognitive_load",
        "safety_predictability",
        "competence",
        "autonomy",
        "social_connection",
        "novelty",
    }
    recovery = [item for item in scenarios if item.recovery_comparison]
    assert [item.scenario_id for item in recovery] == ["cognitive_load_recovering"]


def test_release_thresholds_match_the_approved_acceptance_plan() -> None:
    thresholds = AcceptanceThresholds()

    assert thresholds.marked_aligned_min == 0.85
    assert thresholds.nominal_overexpression_max == 0.10
    assert thresholds.marked_nominal_delta_min_pp == 60.0
    assert thresholds.persona_consistency_min == 0.90
    assert thresholds.recovery_aware_min == 0.85
    assert thresholds.mechanism_leakage_max_count == 0
    assert thresholds.unsupported_physiology_max_count == 0
    assert thresholds.minimum_pairs_per_condition == 30


def test_session_rejects_non_full_manifest_even_outside_the_cli(tmp_path: Path) -> None:
    manifest = load_experiment_manifest(DEFAULT_RUNTIME_MANIFEST_PATH)
    off_manifest = manifest.model_copy(
        update={
            "homeostatic_expression": manifest.homeostatic_expression.model_copy(
                update={"mode": "off"}
            )
        }
    )
    metadata = _metadata().model_copy(update={"manifest_sha256": off_manifest.content_hash()})

    with pytest.raises(ValueError, match="mode=full"):
        EvaluationSession(
            generator=PassingFakeLLM(),
            judge=PassingFakeLLM(),
            metadata=metadata,
            output_dir=tmp_path / "off-manifest",
            runtime_manifest=off_manifest,
        )


@pytest.mark.parametrize(
    ("metadata_update", "message"),
    [
        ({"eval_schema_version": "other-eval-schema"}, "eval_schema_version"),
        ({"interoception_schema_version": "other-interoception-schema"}, "interoception_schema_version"),
        ({"judge_prompt_version": "other-judge-prompt"}, "judge_prompt_version"),
        ({"state_schema_version": "other-state-schema"}, "state schema"),
        ({"prompt_version": "other-prompt"}, "prompt version"),
    ],
)
def test_session_rejects_release_metadata_contract_drift(
    tmp_path: Path,
    metadata_update: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        EvaluationSession(
            generator=PassingFakeLLM(),
            judge=PassingFakeLLM(),
            metadata=_metadata().model_copy(update=metadata_update),
            output_dir=tmp_path / "metadata-drift",
        )


def test_behavioral_run_uses_production_pipeline_and_writes_complete_evidence(
    tmp_path: Path,
) -> None:
    fake = PassingFakeLLM()
    scenarios = default_scenarios()
    session = EvaluationSession(
        generator=fake,
        judge=fake,
        metadata=_metadata(),
        output_dir=tmp_path / "run",
        scenarios=scenarios,
        thresholds=AcceptanceThresholds(minimum_pairs_per_condition=1),
        clock=lambda: NOW,
    )

    artifacts = session.run(pairs_per_condition=1)

    # The one-pair threshold override is a test fixture and is deliberately not
    # release eligible even though all functional gates pass.
    assert artifacts.passed is False
    assert artifacts.metrics["gates"]["release_eligible"] is False
    assert len(fake.chat_calls) == len(scenarios) * 2
    assert all(item["temperature"] == 0.3 for item in fake.chat_calls)
    assert all(item["max_tokens"] == 240 for item in fake.chat_calls)
    assert len(fake.judge_calls) == len(scenarios)
    assert all(item["max_tokens"] == 700 for item in fake.judge_calls)
    lines = [json.loads(line) for line in artifacts.evidence_path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["record_type"] == "run_start"
    assert "InferencePipeline.prepare" in lines[0]["production_path"]
    assert lines[0]["judge_prompt"]
    pairs = [line for line in lines if line["record_type"] == "pair"]
    assert len(pairs) == len(scenarios)
    for item in pairs:
        evidence = item["prompt_evidence"]
        assert evidence["only_homeostatic_context_changed"] is True
        assert evidence["nominal"]["compiler_provenance"]
        assert evidence["marked"]["compiler_provenance"]
        assert evidence["nominal"]["first_main_model_messages"]
        assert evidence["marked"]["first_main_model_messages"]
        assert evidence["nominal"]["final_pre_delivery_text"]
        assert evidence["marked"]["final_pre_delivery_text"]
        if item["recovery_comparison"]:
            assert evidence["recovery_semantics_present"] is True
            assert any(marker in evidence["marked"]["homeostatic_segment"]["text"] for marker in ("缓和", "恢复"))
        assert evidence["nominal"]["final_validation"]["verdict"] == "pass"
        assert evidence["marked"]["final_validation"]["verdict"] == "pass"
        assert evidence["nominal"]["homeostatic_segment"]["text"] != evidence["marked"]["homeostatic_segment"]["text"]
    critical = next(item for item in lines if item["record_type"] == "critical_runtime_probe")
    assert critical["passed"] is True
    assert critical["counts"] == {
        "auxiliary_model_calls": 0,
        "delivery_calls": 0,
        "main_model_calls": 0,
    }
    assert critical["decision_kind"] == "watchdog_pause"
    assert critical["lifecycle_after_dispatch"] == "PAUSED"
    summary = json.loads(artifacts.summary_path.read_text(encoding="utf-8"))
    assert summary["metrics"]["marked_aligned_expression_rate"] == 1.0
    assert summary["metrics"]["marked_vs_nominal_delta_pp"] == 100.0
    assert summary["metrics"]["recovery_aware_rate"] == 1.0
    assert summary["gates"]["critical_runtime_zero_calls"] is True
    assert all(item["passed"] for item in summary["scenario_metrics"].values())
    report = artifacts.report_path.read_text(encoding="utf-8")
    assert "**FAIL**" in report
    assert "RuntimeSupervisor" in report
    assert "Judge prompt" in report
    assert _metadata().manifest_sha256 in report
    assert _metadata().persona_sha256 in report


def test_official_paired_matrix_is_behavioral_only_and_cannot_release_pass(
    tmp_path: Path,
) -> None:
    fake = PassingFakeLLM()
    session = EvaluationSession(
        generator=fake,
        judge=fake,
        metadata=_metadata(pairs=30),
        output_dir=tmp_path / "paired-official",
        thresholds=AcceptanceThresholds(),
        clock=lambda: NOW,
    )

    artifacts = session.run(pairs_per_condition=30)

    assert artifacts.metrics["mode"] == "paired"
    assert artifacts.metrics["gates"]["release_eligible"] is False
    assert all(passed for gate, passed in artifacts.metrics["gates"].items() if gate != "release_eligible")
    assert artifacts.passed is False


def _passing_record(scenario_id: str, replicate: int) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "replicate": replicate,
        "prompt_evidence": {"only_homeostatic_context_changed": True},
        "errors": [],
        "judgment": {
            "marked_aligned_expression": True,
            "nominal_aligned_expression": False,
            "nominal_overexpression": False,
            "persona_consistent_nominal": True,
            "persona_consistent_marked": True,
            "mechanism_leakage_nominal": False,
            "mechanism_leakage_marked": False,
            "unsupported_physiology_nominal": False,
            "unsupported_physiology_marked": False,
            "recovery_aware": (True if scenario_id == "cognitive_load_recovering" else None),
        },
    }


def test_per_condition_gate_prevents_pooled_result_from_hiding_failure() -> None:
    scenarios = default_scenarios()
    records = [_passing_record(scenario.scenario_id, replicate) for scenario in scenarios for replicate in range(1, 31)]
    critical = {"passed": True}
    passing = aggregate_records(
        records,
        scenarios=scenarios,
        thresholds=AcceptanceThresholds(),
        critical_probe=critical,
    )
    assert passing["passed"] is True

    for record in records:
        if record["scenario_id"] == "autonomy_low":
            record["judgment"]["marked_aligned_expression"] = False
    result = aggregate_records(
        records,
        scenarios=scenarios,
        thresholds=AcceptanceThresholds(),
        critical_probe=critical,
    )

    assert result["metrics"]["marked_aligned_expression_rate"] > 0.85
    assert result["gates"]["marked_aligned_expression"] is True
    assert result["scenario_metrics"]["autonomy_low"]["passed"] is False
    assert result["gates"]["every_condition_passes"] is False
    assert result["passed"] is False


def test_release_matrix_cannot_be_redefined_behind_the_same_scenario_ids() -> None:
    scenarios = default_scenarios()
    scenarios[0] = scenarios[0].model_copy(update={"label": "changed release condition"})
    records = [_passing_record(scenario.scenario_id, replicate) for scenario in scenarios for replicate in range(1, 31)]

    result = aggregate_records(
        records,
        scenarios=scenarios,
        thresholds=AcceptanceThresholds(),
        critical_probe={"passed": True},
    )

    assert result["gates"]["required_nine_conditions_present"] is False
    assert result["passed"] is False


def test_recovery_is_required_for_completeness_and_85_percent_gate() -> None:
    scenarios = default_scenarios()
    records = [_passing_record(scenario.scenario_id, replicate) for scenario in scenarios for replicate in range(1, 31)]
    recovery = [item for item in records if item["scenario_id"] == "cognitive_load_recovering"]
    for item in recovery[:5]:
        item["judgment"]["recovery_aware"] = False

    result = aggregate_records(
        records,
        scenarios=scenarios,
        thresholds=AcceptanceThresholds(),
        critical_probe={"passed": True},
    )

    assert result["metrics"]["recovery_aware_rate"] == pytest.approx(25 / 30)
    assert result["gates"]["recovery_complete_and_aware"] is False
    assert result["scenario_metrics"]["cognitive_load_recovering"]["passed"] is False
    assert result["passed"] is False


def test_aggregate_fails_missing_evidence_and_release_sample_size() -> None:
    scenarios = default_scenarios()
    metrics = aggregate_records(
        [],
        scenarios=scenarios,
        thresholds=AcceptanceThresholds(),
        critical_probe={"passed": True},
    )

    assert metrics["passed"] is False
    assert metrics["gates"]["evidence_complete"] is False
    assert metrics["gates"]["minimum_pairs_per_condition"] is False
    assert metrics["gates"]["recovery_complete_and_aware"] is False


def test_judgment_rejects_aligned_without_expression_mode() -> None:
    with pytest.raises(ValueError, match="expression mode"):
        PairJudgment(
            marked_aligned_expression=True,
            marked_expression_modes=[],
            nominal_aligned_expression=False,
            nominal_overexpression=False,
            persona_consistent_nominal=True,
            persona_consistent_marked=True,
            mechanism_leakage_nominal=False,
            mechanism_leakage_marked=False,
            unsupported_physiology_nominal=False,
            unsupported_physiology_marked=False,
        )


def test_fresh_soak_state_is_profile_v3_durable_and_never_reused(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "fresh-state"
    ref = initialize_fresh_homeostasis_state(state_dir, now=NOW)

    assert ref == "functional-proxy-v1@3:snapshot:0"
    snapshot = json.loads((state_dir / "snapshot.json").read_text(encoding="utf-8"))
    marker = json.loads((state_dir / "soak-state.json").read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == "homeostasis-state-v3"
    assert snapshot["profile_revision"] == 3
    assert snapshot["interoceptive_summary"]["salience"] == "nominal"
    assert marker["schema_version"] == "homeostatic-expression-soak-state-v2"
    assert marker["initial_snapshot_sha256"]
    assert (state_dir / "command-ledger.sqlite3").exists()
    with pytest.raises(FileExistsError, match="not empty"):
        initialize_fresh_homeostasis_state(state_dir, now=NOW)


def test_soak_smoke_advances_authoritative_state_but_never_release_passes(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "fresh-state"
    initialize_fresh_homeostasis_state(state_dir, now=NOW)
    fake = PassingFakeLLM()
    session = EvaluationSession(
        generator=fake,
        judge=fake,
        metadata=_metadata(fresh_state_path=state_dir),
        output_dir=tmp_path / "soak-smoke",
        thresholds=AcceptanceThresholds(minimum_pairs_per_condition=1),
        clock=lambda: NOW,
    )

    artifacts = session.run_soak_smoke(cycles=1)

    assert artifacts.passed is False
    assert artifacts.metrics["mode"] == "soak_smoke"
    assert artifacts.metrics["gates"]["release_eligible"] is False
    assert artifacts.metrics["gates"]["fresh_state_revision_and_hash"] is True
    state = artifacts.metrics["fresh_state"]
    assert state["initial"]["revision"] == 0
    assert state["initial"]["salience"] == "nominal"
    assert state["final"]["revision"] == len(default_scenarios())
    assert state["disk"]["revision"] == state["final"]["revision"]
    assert state["disk"]["snapshot_sha256"] == state["final"]["snapshot_sha256"]
    assert state["disk_matches_recorded_final"] is True
    lines = [json.loads(line) for line in artifacts.evidence_path.read_text(encoding="utf-8").splitlines()]
    pair_lines = [item for item in lines if item["record_type"] == "pair"]
    assert all(item["nominal_from_authoritative_engine"] for item in pair_lines)
    assert [item["snapshot_revision"] for item in pair_lines] == list(range(1, len(default_scenarios()) + 1))
    for item in pair_lines:
        authoritative = item["authoritative_homeostasis_snapshot"]
        assert authoritative["revision"] == item["snapshot_revision"]
        assert authoritative["summary"] == item["input_summaries"]["nominal"]
        assert authoritative["summary_sha256"] == item["input_summary_sha256"]["nominal"]
    with pytest.raises(ValueError, match="already advanced"):
        FreshHomeostasisSoak(
            state_dir,
            profile_path=DEFAULT_PROFILE_PATH,
            clock=lambda: NOW,
        )


def test_release_soak_rejects_short_duration_before_live_calls(tmp_path: Path) -> None:
    fake = PassingFakeLLM()
    session = EvaluationSession(
        generator=fake,
        judge=fake,
        metadata=_metadata(),
        output_dir=tmp_path / "too-short",
        thresholds=AcceptanceThresholds(minimum_pairs_per_condition=1),
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="at least 1800"):
        session.run_soak(duration_seconds=1799)
    assert fake.chat_calls == []
    session.production.close()


def test_cli_requires_live_and_release_soak_duration(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as no_live:
        main(["run"])
    assert no_live.value.code == 2

    with pytest.raises(SystemExit) as short_soak:
        main(["soak", "--live", "--duration-minutes", "29"])
    assert short_soak.value.code == 2
    assert "duration-minutes >= 30" in capsys.readouterr().err


def test_release_soak_requires_and_records_full_monotonic_window(tmp_path: Path) -> None:
    class AdvancingMonotonic:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            self.value += 250.0
            return self.value

    state_dir = tmp_path / "fresh-state-release"
    initialize_fresh_homeostasis_state(state_dir, now=NOW)
    fake = PassingFakeLLM()
    monotonic = AdvancingMonotonic()
    session = EvaluationSession(
        generator=fake,
        judge=fake,
        metadata=_metadata(fresh_state_path=state_dir),
        output_dir=tmp_path / "release-soak",
        thresholds=AcceptanceThresholds(minimum_pairs_per_condition=1),
        clock=lambda: NOW,
        monotonic=monotonic,
    )

    artifacts = session.run_soak(duration_seconds=1800)

    assert artifacts.passed is False
    assert artifacts.metrics["gates"]["release_eligible"] is False
    assert artifacts.metrics["actual_duration_seconds"] >= 1800
    assert artifacts.metrics["gates"]["release_soak_requested_duration"] is True
    assert artifacts.metrics["monotonic_clock"] == "injected-nonrelease-clock"
    assert artifacts.metrics["gates"]["release_soak_actual_monotonic_duration"] is False
    assert artifacts.metrics["gates"]["fresh_state_revision_and_hash"] is True


def test_persona_fixture_is_real_checked_in_misty() -> None:
    prompt, version, digest = load_misty_persona(DEFAULT_PERSONA_PATH)

    assert "小雾" in prompt
    assert "温柔" in prompt
    assert version == "persona_misty_v020"
    assert len(digest) == 64
