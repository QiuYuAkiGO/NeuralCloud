"""Tool results must fit after an immutable, nearly full gate prefix."""
from copy import deepcopy
from dataclasses import replace

import pytest

from harness.qq.decision import _QQContextBudgetExceeded, _checked_model_payload
from harness.qq.history_selection import additional_body_messages, fit_body_history, select_bounded_history
from harness.qq.telemetry import QQTurnTrace
from tests.test_qq_bounded_history import _history, _payload, _wire_records
from tests.test_qq_decision import setup as setup


def test_tool_headroom_preserves_whole_sources_and_fits_continuation(setup):
    records = _history(setup[0], count=105)
    _, payload = _payload(setup[0])
    payload["conversation_window"]["closed_observation_ids"] = []
    payload["conversation_window"]["open_observation_ids"] = [r.record_id for r in records]
    original = deepcopy(payload)
    required = {records[-1].record_id}
    options = dict(required_ids=required, gate_target=80_000, body_target=80_000,
                   max_characters=120_000, assessment_reserve=56_300, projection="compact")
    before = select_bounded_history(payload, **options)
    # Reproduce a returned tool result that overflows the frozen old prefix.
    tool = {"vision_tool_result": "完整图片观察" * 1500}
    trace = QQTurnTrace()
    with pytest.raises(_QQContextBudgetExceeded):
        _checked_model_payload(tool, trace=trace, stage="final",
            reserve=before.gate.characters + 56_300, max_characters=120_000)
    selected = select_bounded_history(payload, **options, gate_tool_reserve=16_384)
    assert selected.gate.fits
    assert selected.gate.characters + 56_300 + 16_384 <= 120_000
    encoded = _checked_model_payload(tool, trace=QQTurnTrace(), stage="final",
        reserve=selected.gate.characters + 56_300, max_characters=120_000)
    fitted = fit_body_history(selected.body, selected.gate,
        prefix_characters=selected.gate.characters + len(encoded), max_characters=120_000,
        assessment_reserve=56_300, projection="compact", original_ids=[r.record_id for r in records])
    extra = additional_body_messages(fitted, selected.gate, projection="compact")
    assert selected.gate.characters + len(encoded) + sum(len(m["content"]) for m in extra) + 56_300 <= 120_000
    assert required <= set(selected.gate.observation_ids) <= set(fitted.observation_ids)
    for candidate in (selected.gate, fitted):
        wire = _wire_records(candidate.payload)
        for record in records:
            if record.record_id in wire:
                assert wire[record.record_id]["text"] == record.text
    assert payload == original
    # Headroom is not a budget override for arbitrarily large evidence.
    with pytest.raises(_QQContextBudgetExceeded):
        _checked_model_payload({"vision_tool_result": "图" * 120_000},
            trace=QQTurnTrace(), stage="final", reserve=56_300, max_characters=120_000)


@pytest.mark.parametrize("tool_growth", [0, 2053])
def test_missing_optional_headroom_still_refits_body_history(setup, tool_growth):
    records = _history(setup[0], count=100)
    _, payload = _payload(setup[0])
    payload["conversation_window"]["closed_observation_ids"] = []
    payload["conversation_window"]["open_observation_ids"] = [r.record_id for r in records]
    payload["request"] = "x" * 43_000
    original = deepcopy(payload)
    selected = select_bounded_history(payload, required_ids={records[-1].record_id},
        gate_target=32_000, body_target=80_000, max_characters=120_000,
        assessment_reserve=56_300, projection="compact", gate_tool_reserve=16_384)
    assert selected.gate.characters + 56_300 + 16_384 > 120_000
    assert selected.gate.fits
    # Stale flags from earlier selection must not bypass actual wire fitting.
    fitted = fit_body_history(replace(selected.body, fits=False), replace(selected.gate, fits=False),
        prefix_characters=selected.gate.characters + tool_growth, max_characters=120_000,
        assessment_reserve=56_300, projection="compact", original_ids=[r.record_id for r in records])
    extra = additional_body_messages(fitted, selected.gate, projection="compact")
    assert selected.gate.characters + tool_growth + sum(len(m["content"]) for m in extra) + 56_300 <= 120_000
    assert fitted.fits
    assert records[-1].record_id in selected.gate.observation_ids
    assert set(selected.gate.observation_ids) <= set(fitted.observation_ids)
    for candidate in (selected.gate, fitted):
        wire = _wire_records(candidate.payload)
        for record in records:
            if record.record_id in wire:
                assert wire[record.record_id]["text"] == record.text
    assert payload == original


def test_mandatory_hard_overflow_remains_rejected(setup):
    records = _history(setup[0], count=5)
    _, payload = _payload(setup[0])
    payload["request"] = "x" * 120_000
    selected = select_bounded_history(payload, required_ids={records[-1].record_id},
        gate_target=32_000, body_target=80_000, max_characters=120_000,
        assessment_reserve=56_300, projection="compact", gate_tool_reserve=16_384)
    assert not selected.gate.fits and not selected.body.fits
    assert records[-1].record_id in selected.gate.observation_ids
    with pytest.raises(_QQContextBudgetExceeded):
        _checked_model_payload(selected.gate.payload, trace=QQTurnTrace(), stage="final",
            reserve=56_300, max_characters=120_000)
