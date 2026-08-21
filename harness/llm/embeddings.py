"""Embedding backend: OpenAI-compatible HTTP + hash mock."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Protocol

import httpx
import numpy as np

from harness.config import Settings, get_settings


class EmbeddingClient(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]: ...


class OpenAICompatEmbedding:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.dim = self.settings.embedding_dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{self.settings.embedding_base_url.rstrip('/')}/embeddings",
                json={"model": self.settings.embedding_model, "input": list(texts)},
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()["data"]
        data = sorted(data, key=lambda item: item["index"])
        return [row["embedding"] for row in data]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class MockEmbedding:
    """Process-stable bag-of-ngrams hashing. Similar strings share dimensions."""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_one(text) for text in texts]

    def embed_one(self, text: str) -> list[float]:
        vec = np.zeros(self.dim, dtype=np.float64)
        normalized = text.lower().strip()
        tokens = _ngrams(normalized)
        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.astype(float).tolist()


def _ngrams(text: str) -> list[str]:
    chars = [ch for ch in text if not ch.isspace()]
    grams = list(chars)
    grams.extend("".join(chars[i : i + 2]) for i in range(len(chars) - 1))
    grams.extend("".join(chars[i : i + 3]) for i in range(max(0, len(chars) - 2)))
    for word in text.split():
        grams.append(word)
    return grams or [text or "empty"]
