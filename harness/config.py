"""Runtime configuration. Retrieval weights and confidence knobs live here, not in call sites."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SCHEMA_VERSION = "state-v1"
PROMPT_VERSION = "memory-v1"
DEFAULT_EMBEDDING_DIM = 1024

# 01-cognitive-harness.md §4.1 default hybrid weights (sum = 1.0).
DEFAULT_RETRIEVAL_WEIGHTS: dict[str, float] = {
    "semantic": 0.30,
    "entity": 0.15,
    "recency": 0.10,
    "importance": 0.15,
    "relationship": 0.15,
    "persona": 0.15,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NC_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://neuralcloud:neuralcloud@localhost:5432/neuralcloud"
    llm_base_url: str = "http://127.0.0.1:8080/v1"
    llm_api_key: str = "not-needed"
    llm_model: str = "qwen"
    embedding_base_url: str = "http://127.0.0.1:8080/v1"
    embedding_model: str = "bge-m3"
    embedding_dim: int = DEFAULT_EMBEDDING_DIM
    schema_version: str = SCHEMA_VERSION
    prompt_version: str = PROMPT_VERSION
    model_version: str = "mock"
    trace_dir: Path = Path("traces")
    consolidation_threshold: float = 0.70
    initial_candidate_confidence: float = 0.15
    confidence_increment: float = 0.35
    retrieval_pool_size: int = 50
    retrieval_top_k: int = 8
    recency_half_life_hours: float = 24.0
    retrieval_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_RETRIEVAL_WEIGHTS))
    complexity_threshold: float = 0.5
    # gray zone [gray_lower, threshold): an LLM arbiter may promote fast → deep (never demotes)
    complexity_gray_lower: float = 0.15
    context_token_budget: int = 3000
    compiler_top_memories: int = 5
    validator_sample_rate: float = 0.2
    max_revisions: int = 2
    session_window: int = 10
    record_interaction: bool = True
    continuation_enabled: bool = True
    max_reply_segments: int = 3


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
