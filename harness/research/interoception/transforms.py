"""Deterministic, episode-stable opaque encoding using isolated random streams."""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from datetime import datetime, timedelta

from pydantic import TypeAdapter

from harness.research.grounded_agency.hashes import content_hash
from harness.research.grounded_agency.manifest import SensorTransformSpec
from harness.research.interoception.contracts import Channel, OpaqueInteroceptiveObservation, SensorTransformRecord


def _profile_number(profile: str, prefixes: tuple[str, ...]) -> tuple[str, float]:
    if profile in {"none", "zero", "none-v1", "zero-v1"}:
        return "none", 0.0
    kind, separator, number = profile.partition(":")
    if not separator or kind not in prefixes:
        raise ValueError(f"unsupported sensor profile: {profile!r}")
    try:
        magnitude = float(number)
    except ValueError as exc:
        raise ValueError("sensor profile requires a numeric magnitude") from exc
    if not math.isfinite(magnitude) or magnitude < 0:
        raise ValueError("sensor profile magnitude must be finite and nonnegative")
    return kind, magnitude


def _seed(*parts: object) -> int:
    return int(content_hash(parts), 16)


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("sensor times must be timezone-aware")


class OpaqueSensorTransform:
    """Map a unit interval to symbols. Profiles: none, uniform:A, normal:S, fixed:S.

    ``uniform:A`` draws noise in [-A, A]; ``normal:S`` draws zero-mean Gaussian
    noise with standard deviation S. Noise is clamped to the unit interval before
    binning. ``fixed:S`` delays publication by S seconds. Symbols are shuffled
    once from (seed, episode), independently of direction and sampling noise.
    """

    def __init__(self, spec: SensorTransformSpec, *, episode_id: str | None = None, bin_count: int = 9) -> None:
        self.spec = SensorTransformSpec.model_validate(spec.model_dump(mode="python"))
        TypeAdapter(Channel).validate_python(spec.channel_alias)
        if spec.encoding != "ordinal_symbols":
            raise ValueError("opaque sensor v1 supports ordinal_symbols encoding only")
        if isinstance(bin_count, bool) or not isinstance(bin_count, int) or not 5 <= bin_count <= 9:
            raise ValueError("bin_count must be an integer from 5 to 9")
        if episode_id is not None and (not isinstance(episode_id, str) or not episode_id.strip()):
            raise ValueError("episode_id must be nonblank")
        self.episode_id = episode_id
        self.bin_count = bin_count
        self.noise_kind, self.noise_magnitude = _profile_number(spec.noise_profile, ("uniform", "normal"))
        _, self.delay_seconds = _profile_number(spec.delay_profile, ("fixed",))
        self.bin_edges = tuple(index / bin_count for index in range(bin_count + 1))
        symbols = [f"M{index}" for index in range(bin_count)]
        changes = [f"D{index}" for index in range(5)]
        mapping_rng = random.Random(_seed("opaque-mapping-v1", spec.seed, episode_id))
        mapping_rng.shuffle(symbols)
        mapping_rng.shuffle(changes)
        self.reading_symbols = tuple(symbols)
        self.change_symbols = tuple(changes)
        self.transform_spec_hash = content_hash({"spec": self.spec, "bin_count": bin_count})
        self.transform_ref = "T" + content_hash({
            "version": "opaque-transform-v1", "spec_hash": self.transform_spec_hash,
            "episode_id": episode_id, "reading_symbols": symbols, "change_symbols": changes,
        })

    def encode(
        self, *, value: float, previous_value: float | None, captured_at: datetime, sample_sequence: int,
    ) -> tuple[OpaqueInteroceptiveObservation, SensorTransformRecord]:
        _aware(captured_at)
        for item in (value, previous_value):
            if item is not None and (isinstance(item, bool) or not math.isfinite(item) or not 0 <= item <= 1):
                raise ValueError("sensor inputs must be finite values in [0, 1]")
        if isinstance(sample_sequence, bool) or not isinstance(sample_sequence, int) or sample_sequence < 1:
            raise ValueError("sample_sequence must be a positive integer")
        rng = random.Random(_seed("opaque-noise-v1", self.spec.seed, self.episode_id, sample_sequence))
        noise = 0.0
        if self.noise_kind == "uniform":
            noise = rng.uniform(-self.noise_magnitude, self.noise_magnitude)
        elif self.noise_kind == "normal":
            noise = rng.gauss(0.0, self.noise_magnitude)
        noisy_value = min(1.0, max(0.0, value + noise))
        index = min(self.bin_count - 1, bisect_right(self.bin_edges, noisy_value) - 1)
        delta = 0.0 if previous_value is None else value - previous_value
        # Five private difference bins, including a central stationary band.
        epsilon = 1 / (self.bin_count * 4)
        step = 1 / self.bin_count
        change_index = 0 if delta < -step else 1 if delta < -epsilon else 2 if delta <= epsilon else 3 if delta <= step else 4
        if self.spec.direction == "inverted":
            index = self.bin_count - 1 - index
            change_index = 4 - change_index
        observation = OpaqueInteroceptiveObservation(
            channel_id=self.spec.channel_alias, reading=self.reading_symbols[index],
            change=self.change_symbols[change_index], confidence_band="C3" if self.noise_magnitude == 0 else "C2",
            sample_sequence=sample_sequence, observed_at=captured_at + timedelta(seconds=self.delay_seconds),
            transform_ref=self.transform_ref,
        )
        record = SensorTransformRecord(
            transform_ref=self.transform_ref, transform_spec_hash=self.transform_spec_hash, seed=self.spec.seed,
            direction=self.spec.direction, bin_edges=self.bin_edges, reading_symbols=self.reading_symbols,
            change_symbols=self.change_symbols, delay_seconds=self.delay_seconds, noise_sample=noise,
        )
        return observation, record
