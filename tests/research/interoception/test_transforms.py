from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from harness.research.grounded_agency.manifest import SensorTransformSpec
from harness.research.interoception import OpaqueSensorTransform

NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)


def spec(**overrides: object) -> SensorTransformSpec:
    return SensorTransformSpec.model_validate({
        "transform_id": "test-v1", "seed": 42, "channel_alias": "Q7", "encoding": "ordinal_symbols",
        "direction": "direct", "noise_profile": "none", "delay_profile": "none", **overrides,
    })


def encode(transform: OpaqueSensorTransform, value: float = 0.5, **overrides: object):
    return transform.encode(**{
        "value": value, "previous_value": 0.6, "captured_at": NOW, "sample_sequence": 7, **overrides,
    })


@pytest.mark.parametrize("bins", range(5, 10))
def test_deterministic_mapping_and_every_bucket_boundary(bins: int):
    transform = OpaqueSensorTransform(spec(), episode_id="a", bin_count=bins)
    assert encode(transform) == encode(OpaqueSensorTransform(spec(), episode_id="a", bin_count=bins))
    for index in range(bins):
        observation, record = encode(transform, index / bins)
        assert observation.reading == record.reading_symbols[index]
    assert encode(transform, 1.0)[0].reading == transform.reading_symbols[-1]
    assert encode(transform, 0.0)[0].reading == transform.reading_symbols[0]
    for index in range(1, bins):
        assert encode(transform, index / bins - 1e-10)[0].reading == transform.reading_symbols[index - 1]


def test_inversion_reverses_private_order_and_changes_without_changing_mapping():
    direct = OpaqueSensorTransform(spec(), episode_id="a")
    inverse = OpaqueSensorTransform(spec(direction="inverted"), episode_id="a")
    assert direct.reading_symbols == inverse.reading_symbols
    for value in (0.0, 0.2, 0.5, 0.9, 1.0):
        obs_d, record_d = encode(direct, value)
        obs_i, record_i = encode(inverse, value)
        assert record_d.reading_symbols.index(obs_d.reading) + record_i.reading_symbols.index(obs_i.reading) == 8
        assert record_d.change_symbols.index(obs_d.change) + record_i.change_symbols.index(obs_i.change) == 4


def test_episode_mapping_is_stable_and_seeded_without_global_rng_effects():
    state = random.getstate()
    a = OpaqueSensorTransform(spec(), episode_id="a")
    b = OpaqueSensorTransform(spec(), episode_id="b")
    c = OpaqueSensorTransform(spec(seed=43), episode_id="a")
    assert a.reading_symbols != b.reading_symbols
    assert a.reading_symbols != c.reading_symbols
    encode(a)
    encode(b)
    assert random.getstate() == state


@pytest.mark.parametrize("noise", ("uniform:0.04", "normal:0.02", "none"))
def test_noise_and_delayed_availability_are_exactly_reproducible(noise: str):
    transform = OpaqueSensorTransform(spec(noise_profile=noise, delay_profile="fixed:3.25"), episode_id="a")
    observation, record = encode(transform)
    assert encode(transform) == encode(OpaqueSensorTransform(transform.spec, episode_id="a"))
    assert observation.observed_at == NOW + timedelta(seconds=3.25)
    assert record.delay_seconds == 3.25
    if noise != "none":
        assert record.noise_sample != 0
        assert encode(transform, sample_sequence=8)[1].noise_sample != record.noise_sample


def test_observation_alphabet_and_nested_contracts_are_frozen():
    observation, record = encode(OpaqueSensorTransform(spec()))
    assert set(observation.model_dump()) == {
        "schema_version", "channel_id", "reading", "change", "confidence_band", "sample_sequence", "observed_at", "transform_ref",
    }
    assert isinstance(record.bin_edges, tuple)
    with pytest.raises(ValidationError):
        observation.reading = "M0"
    with pytest.raises(TypeError):
        record.reading_symbols[0] = "M0"
    for field, value in (("reading", "low"), ("change", "falling"), ("channel_id", "energy"), ("confidence_band", "high")):
        with pytest.raises(ValidationError):
            type(observation).model_validate({**observation.model_dump(), field: value})


@pytest.mark.parametrize("value", (-0.1, 1.1, float("nan"), float("inf"), True))
def test_invalid_input_cannot_be_silently_encoded(value: float):
    with pytest.raises(ValueError):
        encode(OpaqueSensorTransform(spec()), value)


@pytest.mark.parametrize("overrides", (
    {"noise_profile": "unknown"}, {"noise_profile": "uniform:-0.2"}, {"noise_profile": "normal:nan"},
    {"delay_profile": "fixed:inf"}, {"delay_profile": "random:1"}, {"channel_alias": "available_ratio"},
    {"encoding": "continuous"},
))
def test_unsupported_declarations_fail_closed(overrides: dict[str, object]):
    with pytest.raises(ValueError):
        OpaqueSensorTransform(spec(**overrides))


def test_naive_time_and_noninteger_sequence_rejected():
    transform = OpaqueSensorTransform(spec())
    with pytest.raises(ValueError):
        encode(transform, captured_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        encode(transform, sample_sequence=True)
