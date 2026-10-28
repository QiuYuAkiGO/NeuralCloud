"""Pinned production architecture; small configurations are explicit test fixtures."""
from __future__ import annotations

from pathlib import Path
from pydantic import Field, model_validator

from .contracts import Contract, digest

HY_REVISION = "688878171e9d98ade3871fecf7eca603c7c43a76"


class EncoderConfig(Contract):
    backbone: str = "Qwen/Qwen3.8-27B"
    backbone_revision: str = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
    encoder: str = "google-bert/bert-base-chinese"
    encoder_revision: str = "8f23c25b06e129b6c986331a13d8d025a92cf0ea"
    hidden_size: int = 5120
    backbone_layers: int = 64
    encoder_hidden_size: int = 768
    encoder_frozen_layers: int = 8
    reader_layers: tuple[int, ...] = (16, 32, 48, 64)
    memory_dim: int = 512
    heads: int = 8
    resampler_layers: int = 2
    chunk_tokens: int = 128
    slots_per_chunk: int = 32
    max_chunks: int = 16
    text_tokens: int = 2048
    learning_rate: float = 1e-4
    accumulation: int = 8
    epochs: int = Field(default=3, ge=1, le=3)
    seed: int = 20261019
    peak_gib_limit: float = 28.0

    @model_validator(mode="after")
    def dimensions(self):
        if self.memory_dim % self.heads or self.chunk_tokens < 8 or self.max_chunks < 1:
            raise ValueError("invalid memory dimensions")
        if not self.reader_layers or any(x < 1 or x > self.backbone_layers for x in self.reader_layers):
            raise ValueError("invalid reader layer")
        if len(set(self.reader_layers)) != len(self.reader_layers):
            raise ValueError("duplicate reader layer")
        return self

    @property
    def fingerprint(self):
        return digest(self.model_dump(mode="json"))

    @classmethod
    def read(cls, path: str | Path | None = None):
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8")) if path else cls()
