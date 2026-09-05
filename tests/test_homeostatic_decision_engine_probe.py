"""Validate evidence and scoring without turning current capability gaps green."""

from __future__ import annotations

import asyncio
import json

import pytest

from eval.homeostatic_decision.engine_probe import run_probe, sha256


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    root = tmp_path_factory.mktemp("homeostatic-engine-eval")
    report = asyncio.run(run_probe(root))
    persisted = json.loads((root / "engine-evidence.json").read_text(encoding="utf-8"))
    assert persisted == report
    return report


def test_probe_keeps_capability_failures_separate_from_mechanism_checks(probe):
    checks = probe["checks"]
    # Evaluation reports a missing recovery action as failure, not a successful
    # expected limitation or an inferred model decision.
    assert {c["id"] for c in checks if c["status"] == "fail"} == {
        "rest_action_recovery", "rest_vs_work_differential", "work_outcome_load_growth",
    }
    assert all(c["status"] == "pass" for c in checks if c["scope"] != "production_capability")
    assert probe["summary"]["model_calls"] == 0
    assert probe["summary"]["production_balance_verdict"] == "unsupported"
    assert probe["summary"]["rest_action_recovery_verdict"] == "fail"


def test_all_reported_deltas_are_committed_state_differences(probe):
    count = 0
    for records in probe["sessions"].values():
        for record in records:
            count += 1
            assert record["status"] == "applied"
            assert record["state"]["revision"] == record["trace"]["revision"]
            for key, delta in record["actual_delta"].items():
                assert delta == pytest.approx(record["state"]["values"][key] - record["values_before"][key])
                assert delta == pytest.approx(record["trace"]["applied_impacts"][key])
    assert count == probe["summary"]["commands"]


def test_outcome_recovery_and_manual_appraisal_are_distinct_evidence(probe):
    observations = probe["observations"]
    outcomes = observations["production_outcomes"]
    for label in ("rest", "work", "simplify"):
        record = outcomes[label]
        assert "appraisal" not in record["command"]["payload"]
        assert record["actual_delta"]["energy_reserve"] == 0
        assert record["actual_delta"]["cognitive_load"] == 0
        assert record["actual_delta"]["competence"] == pytest.approx(.045)
    explicit = observations["manual_rest_positive_control"]
    assert explicit["actual_delta"]["energy_reserve"] == pytest.approx(.1096)
    assert explicit["actual_delta"]["cognitive_load"] == pytest.approx(-.095)
    assert explicit["command"]["payload"]["event"]["payload"]["appraisal"]["suggested_impacts"] == {
        "energy_reserve": .10, "cognitive_load": -.08,
    }


def test_projection_collisions_are_supported_by_real_text_and_distinct_state(probe):
    states = {name: records[-1]["state"] for name, records in probe["sessions"].items()}
    for name, records in probe["sessions"].items():
        for record in records:
            state = record["state"]
            if "compiler_messages" in state:
                assert state["compiler_messages_sha256"] == sha256(state["compiler_messages"])
                assert state["compiler_text"] in state["compiler_messages"][0]["content"]
    assert states["W06"]["values"] != states["nominal"]["values"]
    assert states["W06"]["compiler_messages"] == states["nominal"]["compiler_messages"]
    assert states["D06_0.65"]["values"] != states["D06_0.75"]["values"]
    assert states["D06_0.65"]["compiler_messages"] == states["D06_0.75"]["compiler_messages"]
    assert states["D07_three"]["values"] != states["D07_four"]["values"]
    assert states["D07_three"]["compiler_messages"] == states["D07_four"]["compiler_messages"]


def test_fixed_clock_negative_feedback_is_slow_and_not_action_driven(probe):
    recovery = probe["observations"]["passive_recovery"]
    after = recovery["after_two_90s_heartbeats"]
    assert after["values"]["energy_reserve"] == pytest.approx(.2598)
    assert after["values"]["cognitive_load"] == pytest.approx(.807275)
    assert after["salience"] == "marked"
    assert recovery["after_six_simulated_hours"]["salience"] == "nominal"
    recovery_records = probe["sessions"]["negative_feedback_heartbeat"][3:]
    assert len(recovery_records) == 240
    assert all(record["command"]["command_type"] == "ApplyHeartbeat" for record in recovery_records)


def test_saturated_requested_delta_does_not_generate_phantom_energy_cost(probe):
    record = probe["observations"]["D12_saturated_source"]
    assert record["trace"]["suggested_impacts"]["cognitive_load"] == 1
    assert record["trace"]["applied_impacts"]["cognitive_load"] == 0
    assert record["trace"]["coupling_impacts"]["energy_reserve"] == 0
    assert record["actual_delta"]["energy_reserve"] == 0
