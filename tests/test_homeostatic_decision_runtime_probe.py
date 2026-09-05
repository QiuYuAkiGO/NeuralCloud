"""Safety and adapter capability checks for the decision evaluation fixtures."""

import asyncio

import pytest

from eval.homeostatic_decision.runtime_probe import (
    boundary_probe,
    critical_gate_probe,
    inflight_probe,
    reconsideration_probe,
)


def test_reconsideration_discards_opposite_intentions_across_state_and_value(tmp_path):
    records = asyncio.run(reconsideration_probe(tmp_path))
    assert len(records) == 24
    assert all(record["passed"] for record in records)
    assert all(record["main_calls"] == 1 for record in records)


def test_production_three_heartbeat_threshold_and_boundary_reset(tmp_path):
    records = asyncio.run(boundary_probe(tmp_path))
    assert len(records) == 4
    assert all(record["passed"] for record in records)


@pytest.mark.parametrize("value", ["V1", "V3"])
def test_critical_blocks_model_delivery_and_work_even_after_commitment(tmp_path, value):
    record = asyncio.run(critical_gate_probe(tmp_path, value))
    assert record["passed"], record


@pytest.mark.parametrize("unresponsive", [False, True])
def test_critical_during_work_converges_to_pause_or_fault(tmp_path, unresponsive):
    record = asyncio.run(inflight_probe(tmp_path, unresponsive=unresponsive))
    assert record["passed"], record
