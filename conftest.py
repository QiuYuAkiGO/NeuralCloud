from __future__ import annotations

import pytest

from harness.config import Settings
from harness.llm.embeddings import MockEmbedding
from harness.memory.service import MemoryService, build_inmemory_service
from infra.trace.recorder import NullTracer
from tests.factories import vaelith

_CONTROL_ENV = (
    "NC_CONTROL_MODE",
    "NC_RUNTIME_MANIFEST_PATH",
    "NC_COGNITIVE_MANIFEST_PATH",
    "NC_EXPERIMENT_MANIFEST",
    "NC_ISOLATED_DEVELOPMENT",
)


@pytest.fixture(autouse=True)
def isolate_control_authority(monkeypatch):
    """Each test starts without leaked launch authority or manifest paths."""
    for name in _CONTROL_ENV:
        monkeypatch.delenv(name, raising=False)


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
    yield svc
    svc.close()


@pytest.fixture
def legacy_json_mode(monkeypatch):
    """Verify the rollback switch preserves pre-repair parser contracts."""
    from harness.config import get_settings
    monkeypatch.setenv("NC_JSON_REPAIR_ENABLED", "false")
    monkeypatch.setattr(get_settings(), "json_repair_enabled", False)
