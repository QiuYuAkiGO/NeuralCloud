"""Runtime configuration. Retrieval weights and confidence knobs live here, not in call sites."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SCHEMA_VERSION = "state-v1"
PROMPT_VERSION = "homeostatic-interoception-v1"
DEFAULT_EMBEDDING_DIM = 1024
DEFAULT_EMBEDDING_BASE_URL = "http://127.0.0.1:8080/v1"

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
    llm_timeout: float = 60.0
    # Caps generation length for JSON-producing helper calls (parser / gate);
    # uncapped small models tend to ramble prose before the JSON object.
    llm_json_max_tokens: int = 512
    # Optional auxiliary endpoint for complete_json helpers (parser / gate /
    # interpreter / validator / planner / curator). None falls back to llm_*.
    aux_llm_base_url: str | None = None
    aux_llm_model: str | None = None
    aux_llm_api_key: str | None = None
    aux_llm_preload: bool = True
    # ollama keep_alive; "-1" keeps the model resident. Other servers ignore it.
    aux_llm_keep_alive: str = "-1"
    # Cap KV cache. qwen3:4b-instruct defaults to 256K context (~40GB+), which
    # spills onto CPU even on a 32GB GPU. Helper JSON calls do not need that.
    aux_llm_num_ctx: int = 8192
    # When False the Context Parser never calls the LLM and runs rule-only.
    parser_llm_enabled: bool = True
    # When False the Retrieval Planner builds plans from rules only; the plan is a
    # retrieval hint (hybrid search already has time-scope fallback), so the extra
    # LLM round-trip in the state_build stage is opt-in.
    planner_llm_enabled: bool = False
    embedding_base_url: str = DEFAULT_EMBEDDING_BASE_URL
    embedding_model: str = "bge-m3"
    embedding_dim: int = DEFAULT_EMBEDDING_DIM
    schema_version: str = SCHEMA_VERSION
    prompt_version: str = PROMPT_VERSION
    model_version: str = "mock"
    trace_dir: Path = Path("traces")
    # One JSON file per successful top-level call under trace_dir/calls/YYYYMMDD/.
    call_log_enabled: bool = False
    consolidation_threshold: float = 0.70
    initial_candidate_confidence: float = 0.15
    confidence_increment: float = 0.35
    retrieval_pool_size: int = 50
    retrieval_top_k: int = 8
    recency_half_life_hours: float = 24.0
    retrieval_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_RETRIEVAL_WEIGHTS))
    # Episodic lifecycle (tier). Writes start as short with a uniqueness score.
    # Low uniqueness expires in 5–30 minutes; high uniqueness lasts longer.
    # At expire_at a small model decides promote vs forget.
    tier_llm_enabled: bool = True
    tier_promotion_hits_mid: int = 3
    tier_promotion_hits_long: int = 8
    tier_hit_min_relevance: float = 0.35
    uniqueness_ttl_min_minutes: float = 5.0
    uniqueness_ttl_low_minutes: float = 30.0
    uniqueness_low_cutoff: float = 0.4
    uniqueness_ttl_max_minutes: float = 10080.0  # 7 days at uniqueness=1
    short_term_ttl_days: int = 7  # retained as uniqueness_ttl_max alias in docs
    long_term_recency_floor: float = 0.4
    mid_term_half_life_factor: float = 7.0
    # Regex-side deep threshold. The rule score and the gate LLM are two parallel
    # judges: deep path triggers when either votes deep, fast only when both agree.
    # Stands alone only when no gate LLM is configured (or its call fails).
    complexity_threshold: float = 0.5
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
