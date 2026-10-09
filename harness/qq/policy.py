"""Versioned QQ latency policy, shared by persistent settings and frozen turns."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class QQLatencyOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    qq_context_projection: Literal["legacy", "compact"] = "compact"
    qq_body_pipeline: Literal["legacy", "direct"] = "direct"
    qq_history_selection: Literal["legacy", "shadow", "bounded"] = "bounded"
    qq_gate_history_soft_characters: int = Field(default=24_000, ge=1000)
    qq_body_history_soft_characters: int = Field(default=64_000, ge=1000)
    qq_web_policy: Literal["legacy", "after_gate", "off"] = "legacy"
    qq_reply_scheduler: Literal["legacy", "shadow", "coalesced"] = "shadow"
    qq_stage_budgets_enabled: bool = False
    qq_gate_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    qq_web_timeout_seconds: float = Field(default=10.0, gt=0, le=10)
    qq_body_reserve_seconds: float = Field(default=10.0, ge=10, le=30)
    qq_delivery_reserve_seconds: float = Field(default=3.0, ge=3, le=10)
    cognitive_incremental_persistence_enabled: bool = False
    # History selection applies to every conversation. Other request-local
    # optimizations use this rollout: None enables all, [] enables none.
    # Account-qualified keys keep group/private canaries isolated.
    qq_latency_conversations: list[str] | None = Field(default=None, max_length=128)
    qq_semantic_alignment_mode: Literal["off", "observe", "enforce"] = "off"
    qq_semantic_alignment_conversations: list[str] | None = Field(default_factory=list, max_length=128)
    reply_internalization_mode: Literal["off", "observe", "enforce"] = "off"
    reply_internalization_conversations: list[str] | None = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_budget_rollout(self):
        if self.qq_stage_budgets_enabled and self.qq_context_projection != "compact":
            raise ValueError("QQ stage budgets require compact input rollout")
        return self

    @field_validator("reply_internalization_conversations")
    @classmethod
    def validate_expression_conversations(cls, value):
        if value is None:
            return None
        if len(value) != len(set(value)):
            raise ValueError("duplicate expression conversation")
        cls.validate_conversations([key for key in value if key != "local:main"])
        return sorted(value)

    @field_validator("qq_latency_conversations", "qq_semantic_alignment_conversations")
    @classmethod
    def validate_conversations(cls, value):
        if value is None:
            return value
        from harness.qq.conversation import QQ_NUMBER, conversation_scope
        import re
        if len(value) != len(set(value)):
            raise ValueError("duplicate QQ latency conversation")
        for key in value:
            account, separator, scope_id = key.partition("/")
            if not separator or re.fullmatch(QQ_NUMBER, account) is None:
                raise ValueError("invalid QQ latency conversation")
            scope = conversation_scope(scope_id)
            if scope["kind"] == "private" and scope["self_member_id"] != account:
                raise ValueError("QQ latency conversation account mismatch")
        return sorted(value)

    @classmethod
    def from_settings(cls, settings):
        return cls.model_validate({key: getattr(settings, key, field.get_default(call_default_factory=True))
                                   for key, field in cls.model_fields.items()})

    def for_conversation(self, *, group_id: str, self_member_id: str):
        if (self.qq_latency_conversations is None
                or f"{self_member_id}/{group_id}" in self.qq_latency_conversations):
            return self
        # History selection remains effective outside the rollout so newly
        # observed conversations can bound their unhandled backlog too.
        # Persistence is owner-wide and also independent of this rollout.
        return self.model_copy(update={"qq_context_projection": "legacy", "qq_body_pipeline": "legacy",
            "qq_web_policy": "legacy",
            "qq_reply_scheduler": "shadow", "qq_stage_budgets_enabled": False})

    @property
    def version(self) -> str:
        values = self.model_dump()
        if self.reply_internalization_mode == "off" and self.reply_internalization_conversations == []:
            values.pop("reply_internalization_mode")
            values.pop("reply_internalization_conversations")
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return "qq-policy:" + hashlib.sha256(encoded).hexdigest()

    def stage_budget(self, stage: str, remaining: float) -> float:
        """Every stage subtracts downstream reserves from the original deadline."""
        if stage == "gate":
            return max(0.0, min(self.qq_gate_timeout_seconds,
                remaining - self.qq_body_reserve_seconds - self.qq_delivery_reserve_seconds))
        if stage == "web":
            budget = min(self.qq_web_timeout_seconds,
                remaining - self.qq_body_reserve_seconds - self.qq_delivery_reserve_seconds)
            return max(0.0, budget) if budget >= 2 else 0.0
        if stage == "body":
            return max(0.0, remaining - self.qq_delivery_reserve_seconds)
        raise ValueError("unknown QQ budget stage")


EFFECTIVE_FIELDS = (*QQLatencyOptions.model_fields,
    "validator_enabled", "parser_llm_enabled", "planner_llm_enabled", "llm_model", "aux_llm_model",
    "emotion_mode", "emotion_appraisal_timeout_seconds", "emotion_guidance_timeout_seconds",
    "aux_llm_keep_alive", "aux_llm_num_ctx", "qq_input_token_budget")


def settings_with_policy(settings, policy, *, source="frozen_request"):
    updates = policy.model_dump()
    changed = [name for name, value in updates.items() if getattr(settings, name) != value]
    frozen = settings.model_copy(update=updates)
    frozen.mark_configuration_source(changed, source)
    return frozen


def effective_configuration(settings) -> dict:
    """An allowlisted snapshot; endpoints, credentials and content are excluded."""
    sources = getattr(settings, "_configuration_sources", {})
    values = {name: {"value": getattr(settings, name), "source": sources.get(name, "runtime")}
              for name in EFFECTIVE_FIELDS}
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": "qq-effective-config-v1", "version": "qq-config:" + digest,
            "policy_version": QQLatencyOptions.from_settings(settings).version, "values": values}
