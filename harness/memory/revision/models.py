"""Typed model proposals. Authority, identity and confidence belong to reducers."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CleanedExperience(StrictModel):
    source_ids: list[str] = Field(min_length=1)
    disposition: Literal["keep", "merge", "ignore"]
    summary: str
    evidence_kind: Literal["observation", "speaker_claim", "hearsay", "image_interpretation", "self_expression"]
    reason: str = ""


class MemoryRevision(StrictModel):
    kind: Literal["episodic", "semantic"]
    target_id: str | None = None
    subject: str = Field(min_length=1)
    predicate: str = "fact"
    value: str = ""
    statement: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    counterevidence_ids: list[str] = Field(default_factory=list)
    operation: Literal["upsert", "dispute", "supersede"] = "upsert"
    supersedes: list[str] = Field(default_factory=list)


class ImpressionRevisionProposal(StrictModel):
    target_id: str | None = None
    kind: Literal["person", "relationship", "topic", "group"]
    subject_refs: list[str] = Field(default_factory=list)
    topic: str = ""
    context: str = ""
    statement: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    counterevidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def subjects(self):
        if self.kind == "person" and len(set(self.subject_refs)) != 1:
            raise ValueError("person impression needs one authenticated subject")
        if self.kind == "relationship" and len(set(self.subject_refs)) < 2:
            raise ValueError("relationship impression needs both subjects")
        return self


class RelationshipDeltas(StrictModel):
    closeness: float = Field(default=0, ge=-1, le=1)
    trust: float = Field(default=0, ge=-1, le=1)
    attachment: float = Field(default=0, ge=-1, le=1)
    recent_tension: float = Field(default=0, ge=-1, le=1)


class RelationshipRevision(StrictModel):
    target: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    counterevidence_ids: list[str] = Field(default_factory=list)
    deltas: RelationshipDeltas = Field(default_factory=RelationshipDeltas)


class RevisionProposal(StrictModel):
    cleaned: list[CleanedExperience]
    memories: list[MemoryRevision] = Field(default_factory=list)
    impressions: list[ImpressionRevisionProposal] = Field(default_factory=list)
    relationships: list[RelationshipRevision] = Field(default_factory=list)
    depends_on_run_ids: list[str] = Field(default_factory=list)


class DailyRevisionRun(StrictModel):
    run_id: str
    owner_id: str
    preset_id: str
    day: str
    partition: str
    sources: list[dict[str, Any]]
    basis: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    dry_run: bool = False
    config_digest: str = ""


class RevisionReceipt(StrictModel):
    run_id: str
    commit_seq: int
    status: Literal["applied", "rolled_back", "invalidated"]
    effect_ids: list[str]
    committed_at: datetime
    proposal_digest: str
    source_ids: list[str]
    invalidated_by: str | None = None


def strict_json_schema(model: type[BaseModel]) -> dict:
    """Providers require every property, including nullable/defaulted fields."""
    schema = model.model_json_schema()

    def walk(value):
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                value["additionalProperties"] = False
                value["required"] = list(value.get("properties", {}))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(schema)
    return schema
