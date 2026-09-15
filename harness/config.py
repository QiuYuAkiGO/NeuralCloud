"""Runtime configuration. Retrieval weights and confidence knobs live here, not in call sites."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SCHEMA_VERSION = "state-v1"
PROMPT_VERSION = "homeostatic-interoception-v1"
DEFAULT_EMBEDDING_DIM = 1024
DEFAULT_EMBEDDING_BASE_URL = "http://127.0.0.1:8080/v1"
DEFAULT_QQ_INPUT_TOKEN_BUDGET = 96_000
DEFAULT_QQ_MEMORY_RECALL_CHARACTERS = 16_000

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
    # The frozen runtime manifest selects the rollout mode for shared owners.
    emotion_mode: Literal["off", "shadow", "on"] = "off"
    emotion_half_life_seconds: float = Field(default=600.0, gt=0)
    emotion_window_seconds: float = Field(default=1800.0, gt=0)
    emotion_window_messages: int = Field(default=20, ge=1, le=100)
    emotion_personality_baseline: float = Field(default=0.25, gt=0, le=1)
    emotion_appraisal_timeout_seconds: float = Field(default=8.0, gt=0)
    emotion_guidance_timeout_seconds: float = Field(default=4.0, gt=0)
    emotion_main_reserve_seconds: float = Field(default=10.0, ge=0)
    emotion_input_token_budget: int = Field(default=6000, ge=512, le=6000)
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
    # QQ observation distillation is opt-in and separate from legacy chat memory.
    qq_memory_enabled: bool = False
    qq_memory_recall_enabled: bool = False
    qq_memory_poll_seconds: float = Field(default=5.0, gt=0)
    qq_memory_window_idle_seconds: float = Field(default=30.0, gt=0)
    qq_memory_window_max_seconds: float = Field(default=120.0, gt=0)
    qq_memory_window_max_messages: int = Field(default=32, ge=1)
    qq_memory_model_timeout_seconds: float = Field(default=45.0, gt=0)
    qq_memory_recall_max_items: int = Field(default=8, ge=1)
    qq_memory_recall_max_characters: int = Field(default=DEFAULT_QQ_MEMORY_RECALL_CHARACTERS, ge=1)
    # QQ's whole-record input uses the compiler's two-characters/token estimate.
    # This ceiling never changes completion limits or the caller's deadline.
    qq_input_token_budget: int = Field(default=DEFAULT_QQ_INPUT_TOKEN_BUDGET, ge=8192)
    qq_memory_decay_mid_days: float = Field(default=14.0, gt=0)
    qq_memory_decay_long_days: float = Field(default=90.0, gt=0)
    qq_memory_decay_threshold: float = Field(default=0.20, gt=0, lt=0.5)
    qq_memory_review_hours: float = Field(default=24.0, gt=0)
    qq_impression_initial_confidence: float = Field(default=0.15, ge=0, le=1)
    qq_impression_support_increment: float = Field(default=0.35, gt=0, le=1)
    qq_impression_counter_multiplier: float = Field(default=0.65, ge=0, lt=1)
    qq_impression_stable_confidence: float = Field(default=0.70, gt=0, le=1)
    qq_impression_min_support_episodes: int = Field(default=4, ge=2)
    qq_impression_min_distinct_days: int = Field(default=2, ge=1)
    qq_impression_continuation_gap_minutes: float = Field(default=60.0, ge=0, le=1440)
    # Regex-side deep threshold. The rule score and the gate LLM are two parallel
    # judges: deep path triggers when either votes deep, fast only when both agree.
    # Stands alone only when no gate LLM is configured (or its call fails).
    complexity_threshold: float = 0.5
    context_token_budget: int = 3000
    compiler_top_memories: int = 5
    # Reversible master switch for response post-validation (rules and model).
    # Request/DTO/schema validation is independent of this inference setting.
    validator_enabled: bool = True
    validator_sample_rate: float = 0.2
    max_revisions: int = 2
    session_window: int = 10
    record_interaction: bool = True
    continuation_enabled: bool = True
    max_reply_segments: int = 3


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
