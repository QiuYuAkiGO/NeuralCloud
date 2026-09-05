"""Opaque sensor production pipeline; envelopes and records are Runtime-only."""

from harness.research.interoception.contracts import (
    BodyTruthReference,
    OpaqueInteroceptionEnvelope,
    OpaqueInteroceptiveObservation,
    SensorTransformRecord,
    observation_hash,
)
from harness.research.interoception.renderer import bounded_observations, render_opaque_interoception
from harness.research.interoception.store import OpaqueInteroceptionStore
from harness.research.interoception.transforms import OpaqueSensorTransform

__all__ = [
    "BodyTruthReference", "OpaqueInteroceptionEnvelope", "OpaqueInteroceptiveObservation",
    "SensorTransformRecord", "observation_hash", "bounded_observations", "render_opaque_interoception",
    "OpaqueInteroceptionStore", "OpaqueSensorTransform",
]
