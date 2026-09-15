"""Embedding backend: OpenAI-compatible HTTP + hash mock."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from typing import Protocol, cast

import httpx
import numpy as np

from harness.config import DEFAULT_EMBEDDING_BASE_URL, Settings, get_settings

logger = logging.getLogger(__name__)


class EmbeddingClient(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]: ...


class OpenAICompatEmbedding:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.dim = self.settings.embedding_dim
        # Persistent client: avoid a fresh TCP+TLS handshake per embed call.
        self._client = httpx.Client(timeout=self.settings.llm_timeout)

    def close(self) -> None:
        self._client.close()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        response = self._client.post(
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


class GuardedEmbedding:
    """Degrade to None vectors when the endpoint fails instead of crashing the
    turn (same resilience discipline as the JSON helpers). Consumers treat a
    None vector as "no semantic signal": cosine scores 0 and pool ranking skips
    vector order. Deliberately looser than the EmbeddingClient protocol."""

    def __init__(self, inner: EmbeddingClient) -> None:
        self.inner = inner
        self.dim = inner.dim
        self._logged_error: str | None = None

    def embed(self, texts: Sequence[str]) -> list[list[float] | None]:
        if not texts:
            return []
        try:
            return list(self.inner.embed(texts))
        except Exception as exc:
            marker = type(exc).__name__
            if self._logged_error != marker:
                self._logged_error = marker
                logger.warning("embedding call failed (%s); semantic signal degraded", marker, exc_info=True)
            return [None] * len(texts)

    def embed_one(self, text: str) -> list[float] | None:
        return self.embed([text])[0]

    def close(self) -> None:
        closer = getattr(self.inner, "close", None)
        if callable(closer):
            closer()


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


def _norm_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/")


def _looks_local(url: str | None) -> bool:
    text = (url or "").lower()
    return "127.0.0.1" in text or "localhost" in text or "[::1]" in text


def _is_factory_embedding_url(url: str | None) -> bool:
    normalized = _norm_url(url)
    return not normalized or normalized == _norm_url(DEFAULT_EMBEDDING_BASE_URL) or ":8080" in (url or "")


def embedding_endpoint_candidates(settings: Settings) -> list[tuple[str, str, str]]:
    """Ordered (base_url, model, api_key) probes for /v1/embeddings.

    The factory default is llama.cpp on :8080. Live GUI stacks often put the
    character model on a cloud chat API and helpers on local Ollama — in that
    case embeddings belong next to the local helper, not DeepSeek. Cloud chat
    URLs are never used as a fallback.
    """
    from harness.llm.client import resolve_aux_endpoint

    model = (settings.embedding_model or "").strip() or "bge-m3"
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []

    def add(url: str | None, key: str | None) -> None:
        text = (url or "").strip()
        marker = _norm_url(text)
        if not marker or marker in seen:
            return
        seen.add(marker)
        out.append((text, model, (key or "").strip() or "not-needed"))

    add(settings.embedding_base_url, settings.llm_api_key)
    if _is_factory_embedding_url(settings.embedding_base_url):
        aux_url, _, aux_key = resolve_aux_endpoint(settings)
        if _looks_local(aux_url):
            add(aux_url, aux_key)
        if _looks_local(settings.llm_base_url):
            add(settings.llm_base_url, settings.llm_api_key)
    return out


def build_embedding_client(
    settings: Settings | None = None,
    *,
    mock: bool = False,
    probe_timeout: float = 0.8,
) -> EmbeddingClient:
    """Live path: first reachable OpenAI-compat endpoint, wrapped so a later
    HTTP failure degrades the semantic signal instead of crashing the turn.
    Unreachable endpoints fall back to MockEmbedding (same as demo)."""
    settings = settings or get_settings()
    if mock:
        return MockEmbedding(dim=settings.embedding_dim)
    from harness.llm.client import probe_endpoint

    for url, model, key in embedding_endpoint_candidates(settings):
        if not probe_endpoint(url, key, timeout=probe_timeout):
            continue
        embed_settings = settings.model_copy(
            update={
                "embedding_base_url": url,
                "embedding_model": model,
                "llm_api_key": key,
                "llm_timeout": min(float(settings.llm_timeout), 8.0),
            }
        )
        live = OpenAICompatEmbedding(embed_settings)
        # Consumers already treat a failed embedding as an absent semantic signal.
        # Keep the public protocol strict for healthy backends while adapting this
        # deliberately degrading wrapper at the composition boundary.
        guarded = cast(EmbeddingClient, GuardedEmbedding(live))
        if guarded.embed_one("."):
            return guarded
        closer = getattr(guarded, "close", None)
        if callable(closer):
            closer()
        logger.info("embedding model %s unusable at %s; trying next", model, url)
    logger.info("embedding endpoint unreachable; using MockEmbedding")
    return MockEmbedding(dim=settings.embedding_dim)


def describe_embedding(
    client: EmbeddingClient,
    *,
    settings: Settings | None = None,
) -> dict[str, str | bool | None]:
    inner = getattr(client, "inner", client)
    fallback = settings or getattr(inner, "settings", None)
    if isinstance(inner, MockEmbedding):
        url = fallback.embedding_base_url if fallback is not None else None
        model = fallback.embedding_model if fallback is not None else None
        return {"backend": "mock", "reachable": False, "base_url": url, "model": model}
    live = getattr(inner, "settings", None) or fallback
    return {
        "backend": "openai",
        "reachable": True,
        "base_url": live.embedding_base_url if live is not None else None,
        "model": live.embedding_model if live is not None else None,
    }
