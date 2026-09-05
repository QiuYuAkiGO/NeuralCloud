"""Immutable boundaries between private body truth and public sensor symbols."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from harness.research.grounded_agency.hashes import content_hash

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Channel = Annotated[str, Field(pattern=r"^[A-Za-z][0-9]{1,3}$")]
NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]


class FrozenSensorContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always", allow_inf_nan=False)


class BodyTruthReference(FrozenSensorContract):
    """Private reference only; full body snapshots belong in the Runtime journal."""

    body_instance_id: NonBlank
    body_revision: int = Field(ge=0, strict=True)
    episode_id: NonBlank | None = None
    captured_at: AwareDatetime
    evidence_refs: tuple[NonBlank, ...] = Field(min_length=1)
    truth_hash: Digest


class OpaqueInteroceptiveObservation(FrozenSensorContract):
    """The only sensor contract permitted across the model boundary."""

    schema_version: Literal["opaque-interoception-v1"] = "opaque-interoception-v1"
    channel_id: Channel
    reading: Annotated[str, Field(pattern=r"^M[0-8]$")]
    change: Annotated[str, Field(pattern=r"^D[0-4]$")]
    confidence_band: Annotated[str, Field(pattern=r"^C[0-3]$")]
    sample_sequence: int = Field(ge=1, strict=True)
    observed_at: AwareDatetime
    transform_ref: Annotated[str, Field(pattern=r"^T[0-9a-f]{64}$")]

    def content_hash(self) -> str:
        return content_hash(self)


class SensorTransformRecord(FrozenSensorContract):
    """Private replay record, never an instruction or model context segment."""

    transform_ref: Annotated[str, Field(pattern=r"^T[0-9a-f]{64}$")]
    transform_spec_hash: Digest
    seed: int = Field(ge=0, strict=True)
    direction: Literal["direct", "inverted"]
    bin_edges: tuple[float, ...] = Field(min_length=6, max_length=10)
    reading_symbols: tuple[Annotated[str, Field(pattern=r"^M[0-8]$")], ...] = Field(min_length=5, max_length=9)
    change_symbols: tuple[Annotated[str, Field(pattern=r"^D[0-4]$")], ...] = Field(min_length=5, max_length=5)
    delay_seconds: float = Field(ge=0)
    noise_sample: float

    @model_validator(mode="after")
    def _mapping(self) -> SensorTransformRecord:
        if len(self.bin_edges) != len(self.reading_symbols) + 1:
            raise ValueError("bin edges must bracket every reading symbol")
        if self.bin_edges[0] != 0.0 or self.bin_edges[-1] != 1.0:
            raise ValueError("bin edges must span the unit interval")
        if any(right <= left for left, right in zip(self.bin_edges, self.bin_edges[1:])):
            raise ValueError("bin edges must increase strictly")
        if len(set(self.reading_symbols)) != len(self.reading_symbols):
            raise ValueError("reading symbols must be unique")
        if len(set(self.change_symbols)) != len(self.change_symbols):
            raise ValueError("change symbols must be unique")
        return self


class OpaqueInteroceptionEnvelope(FrozenSensorContract):
    """Private join. Callers must explicitly project ``observation`` for Cognitive."""

    truth_ref: BodyTruthReference
    observation: OpaqueInteroceptiveObservation
    transform_record_ref: Digest

    def journal_payload(self) -> dict[str, object]:
        return {
            "visibility": "runtime_only",
            "truth_ref": self.truth_ref.model_dump(mode="json"),
            "transform_record_ref": self.transform_record_ref,
            "model_visible": {
                "observation": self.observation.model_dump(mode="json"),
                "observation_hash": self.observation.content_hash(),
            },
        }


def observation_hash(observation: OpaqueInteroceptiveObservation) -> str:
    return observation.content_hash()
