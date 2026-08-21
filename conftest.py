from __future__ import annotations

import pytest

from harness.config import Settings
from harness.llm.embeddings import MockEmbedding
from harness.memory.service import MemoryService, build_inmemory_service
from infra.trace.recorder import NullTracer
from tests.factories import vaelith


@pytest.fixture
def settings() -> Settings:
    return Settings(embedding_dim=1024, model_version="mock", trace_dir="traces")


@pytest.fixture
def service(settings: Settings, tmp_path) -> MemoryService:
    settings.trace_dir = tmp_path
    svc = build_inmemory_service(
        settings=settings,
        embeddings=MockEmbedding(dim=settings.embedding_dim),
        tracer=NullTracer(),
    )
    svc.bootstrap_persona(vaelith())
    return svc
