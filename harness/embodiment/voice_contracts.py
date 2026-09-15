"""Typed voice asset contracts; producing an asset is not playback or delivery.

These contracts are owned by NeuralCloud and require no MistyAI import. The
adapter translates the configured character scope into voice-contract-v1.
"""

from __future__ import annotations

from itertools import pairwise
import math
import re
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue, StringConstraints, field_validator, model_validator

from harness.embodiment.capabilities import EmbodimentStatus, StrictModel

VOICE_CONTRACT = "voice-contract-v1"
Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Seed = Annotated[int, Field(strict=True, ge=0, le=2**32 - 1)]


class VoiceError(RuntimeError):
    """Stable diagnostic codes, without credentials or remote response bodies."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class VoiceScope(StrictModel):
    body_instance_id: str = Field(min_length=1, max_length=256, strict=True)
    profile_id: Identifier
    profile_revision: int = Field(ge=1, strict=True)


class ProfileReference(StrictModel):
    voice_profile_id: Identifier
    revision: int = Field(ge=1, strict=True)


def _plain_text(value: str) -> str:
    if not value.strip() or any(character in value for character in "<>[]"):
        raise ValueError("voice text requires plain nonblank text without control markup")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError("voice text cannot contain control characters")
    return value


class Pronunciation(StrictModel):
    text: str = Field(min_length=1, max_length=64)
    phonemes: tuple[str, ...] = Field(min_length=1, max_length=64)

    _plain = field_validator("text")(_plain_text)

    @field_validator("phonemes")
    @classmethod
    def safe_phonemes(cls, value):
        if any(re.fullmatch(r"[A-Za-z0-9āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜüê]+", token) is None for token in value):
            raise ValueError("phonemes must be individual tokens without markup")
        return value


class SpeechSegment(StrictModel):
    kind: Literal["speech"] = "speech"
    segment_id: Identifier
    text: str = Field(min_length=1, max_length=1000, repr=False)
    style: Literal["neutral", "happy", "sad", "angry", "surprised", "gentle"] = "neutral"
    strength: Literal["low", "medium", "high"] = "medium"
    rate: float = Field(default=1.0, ge=0.75, le=1.25, allow_inf_nan=False)
    pronunciations: tuple[Pronunciation, ...] = Field(default_factory=tuple, max_length=32)

    _plain = field_validator("text")(_plain_text)

    @model_validator(mode="after")
    def valid_pronunciations(self):
        keys = [item.text for item in self.pronunciations]
        if len(keys) != len(set(keys)) or any(key not in self.text for key in keys):
            raise ValueError("pronunciations must uniquely refer to segment text")
        spans = []
        for key in keys:
            position = self.text.find(key)
            while position >= 0:
                spans.append((position, position + len(key)))
                position = self.text.find(key, position + 1)
        if any(first[1] > second[0] for first, second in pairwise(sorted(spans))):
            raise ValueError("pronunciation replacements cannot overlap")
        return self


class PauseSegment(StrictModel):
    kind: Literal["pause"] = "pause"
    segment_id: Identifier
    duration_ms: int = Field(ge=1, le=30_000, strict=True)


class VocalizationSegment(StrictModel):
    kind: Literal["vocalization"] = "vocalization"
    segment_id: Identifier
    vocalization: Literal["breath", "laughter", "sigh"]
    approved_asset_id: Identifier | None = None


PerformanceSegment = Annotated[SpeechSegment | PauseSegment | VocalizationSegment, Field(discriminator="kind")]


class VoiceScript(StrictModel):
    """Character performance; the provider supplies the immutable voice profile."""

    segments: tuple[PerformanceSegment, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_segments(self):
        ids = [segment.segment_id for segment in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("voice segment ids must be unique")
        if sum(len(segment.text) for segment in self.segments if isinstance(segment, SpeechSegment)) > 10_000:
            raise ValueError("a voice script may contain at most 10000 text characters")
        return self


class VoiceRenderRequest(StrictModel):
    schema_version: Literal["voice-contract-v1"] = VOICE_CONTRACT
    idempotency_key: str = Field(min_length=1, max_length=256, strict=True, repr=False)
    script: VoiceScript
    seed: Seed = 0
    source_job_id: Identifier | None = None
    regenerate_segment_ids: tuple[Identifier, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def valid_regeneration(self):
        selected = set(self.regenerate_segment_ids)
        if len(selected) != len(self.regenerate_segment_ids):
            raise ValueError("regeneration segment ids must be unique")
        if selected and self.source_job_id is None:
            raise ValueError("regeneration requires source_job_id")
        if not selected.issubset({segment.segment_id for segment in self.script.segments}):
            raise ValueError("regeneration ids must refer to script segments")
        return self


class GenerationProvenance(StrictModel):
    backend: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    model_revision: str = Field(min_length=1, max_length=128)
    weights_sha256: Digest
    code_revision: str = Field(min_length=1, max_length=128)
    seed: Seed
    parameters: dict[str, JsonValue] = Field(default_factory=dict, repr=False)
    metrics: dict[str, float] = Field(default_factory=dict)

    @field_validator("metrics")
    @classmethod
    def finite_metrics(cls, value):
        if any(not math.isfinite(number) or number < 0 for number in value.values()):
            raise ValueError("metrics must be finite and non-negative")
        return value


class VoiceProfile(StrictModel):
    schema_version: Literal["voice-contract-v1"] = VOICE_CONTRACT
    voice_profile_id: Identifier
    revision: int = Field(ge=1, strict=True)
    description: str = Field(repr=False)
    reference_audio_digest: Digest
    reference_text: str = Field(repr=False)
    design_job_id: Identifier
    candidate_id: Identifier
    provenance: GenerationProvenance
    created_at: AwareDatetime


class RenderedSegment(StrictModel):
    segment_id: Identifier
    kind: Literal["speech", "pause", "vocalization"]
    input_sha256: Digest
    audio_digest: Digest
    sample_count: int = Field(ge=1, strict=True)
    text: str | None = Field(default=None, repr=False)
    provenance: tuple[GenerationProvenance, ...] = Field(default_factory=tuple)
    compiler_version: str
    reused_from_job_id: str | None = None


class TimelineSegment(StrictModel):
    segment_id: Identifier
    kind: Literal["speech", "pause", "vocalization"]
    audio_digest: Digest
    start_sample: int = Field(ge=0, strict=True)
    end_sample: int = Field(gt=0, strict=True)
    sample_rate: Literal[24000] = 24000
    text: str | None = Field(default=None, repr=False)

    @model_validator(mode="before")
    @classmethod
    def accept_derived_seconds(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            for label in ("start", "end"):
                key = f"{label}_seconds"
                if key in value:
                    sample = value.get(f"{label}_sample")
                    if not isinstance(sample, int) or value[key] != sample / 24000:
                        raise ValueError("derived seconds do not match sample positions")
                    value.pop(key)
        return value

    @model_validator(mode="after")
    def ordered(self):
        if self.end_sample <= self.start_sample:
            raise ValueError("voice segment must contain at least one sample")
        return self


class VoiceAsset(StrictModel):
    schema_version: Literal["voice-contract-v1"] = VOICE_CONTRACT
    profile: ProfileReference
    audio_digest: Digest
    timeline_digest: Digest
    script_digest: Digest
    manifest_digest: Digest
    sample_rate: Literal[24000] = 24000
    sample_count: int = Field(gt=0, strict=True)
    segments: tuple[TimelineSegment, ...]
    quality_checks: dict[str, JsonValue] = Field(default_factory=dict)


class RenderResult(StrictModel):
    kind: Literal["render"] = "render"
    asset: VoiceAsset


class VoiceJob(StrictModel):
    schema_version: Literal["voice-contract-v1"] = VOICE_CONTRACT
    job_id: Identifier
    kind: Literal["render"] = "render"
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    input_sha256: Digest
    created_at: AwareDatetime
    updated_at: AwareDatetime
    result: RenderResult | None = None
    completed_segments: tuple[RenderedSegment, ...] = Field(default_factory=tuple)
    error_code: str | None = None
    error_message: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def successful_asset(self):
        if self.state == "succeeded" and self.result is None:
            raise ValueError("successful voice jobs require a render asset")
        return self


class VoiceStatus(StrictModel):
    scope: VoiceScope
    embodiment: EmbodimentStatus
