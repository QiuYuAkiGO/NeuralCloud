from datetime import datetime, timezone

import pytest

from harness.research.grounded_agency.neutrality import NeutralityScanner
from harness.research.interoception import OpaqueInteroceptiveObservation, render_opaque_interoception


def observation(sequence: int) -> OpaqueInteroceptiveObservation:
    return OpaqueInteroceptiveObservation(
        channel_id="Q7", reading="M3", change="D1", confidence_band="C3", sample_sequence=sequence,
        observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc), transform_ref="T" + "a" * 64,
    )


def test_current_is_exact_neutral_machine_text_without_private_metadata():
    rendered = render_opaque_interoception((observation(184),))
    assert rendered == "Internal channel sample:\nchannel=Q7\nreading=M3\nchange=D1\nconfidence=C3\nsample=184"
    assert NeutralityScanner().scan([{"role": "system", "content": rendered}]).passed
    for forbidden in ("transform", "evidence", "seed", "energy", "available_ratio", "terminal", "2026", "aaaa"):
        assert forbidden not in rendered


def test_history_orders_by_sequence_and_trims_oldest_to_strict_budget():
    rendered = render_opaque_interoception(tuple(observation(i) for i in range(20, 0, -1)), max_tokens=128)
    assert len(rendered.encode("utf-8")) <= 128
    assert "@20" in rendered
    assert "@1\n" not in rendered
    sequences = [int(line.rsplit("@", 1)[1]) for line in rendered.splitlines()[1:]]
    assert sequences == sorted(sequences)
    assert NeutralityScanner().scan([{"role": "system", "content": rendered}]).passed


def test_empty_input_empty_output_and_insufficient_budget_rejected():
    assert render_opaque_interoception(()) == ""
    with pytest.raises(ValueError, match="budget"):
        render_opaque_interoception((observation(1),), max_tokens=1)


def test_renderer_rejects_untyped_truth_payload_and_duplicate_samples():
    with pytest.raises(TypeError, match="only opaque observations"):
        render_opaque_interoception(({"available_ratio": 0.1},))
    with pytest.raises(ValueError, match="unique"):
        render_opaque_interoception((observation(1), observation(1)))
