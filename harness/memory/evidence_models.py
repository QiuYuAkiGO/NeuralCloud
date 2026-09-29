"""Strict v2 proposals; identity, provenance and permissions remain server-owned."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator


Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=160)]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2000)]
Revision = Annotated[int, Field(strict=True, ge=0)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class EvidenceAnchor(_Frozen):
    unit_id: Identifier
    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, ge=1)]
    quote: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=12000)]
    role: Literal["supports", "challenges", "qualifies", "context", "mixed"] = Field(
        description="Role relative to this exact judgment version; mixed is uncertainty, never two votes."
    )
    rationale: Text

    @model_validator(mode="after")
    def span(self):
        if self.end <= self.start or len(self.quote) != self.end - self.start:
            raise ValueError("evidence_anchor_span_invalid")
        return self


class Supersession(_Frozen):
    record_id: Identifier
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


class ClaimProposal(_Frozen):
    # The runtime validator below cannot be inferred by Pydantic's schema
    # generator. Publish the same exclusive choice to model callers, including
    # when this model is nested in an appraisal or decision. Base properties
    # still enforce Identifier/Text constraints; the other field may be absent
    # or null so existing serialized proposals remain valid.
    model_config = ConfigDict(json_schema_extra={
        "oneOf": [
            {
                "required": ["subject_id"],
                "properties": {
                    "subject_id": {"type": "string"},
                    "subject_mention": {"type": "null"},
                },
            },
            {
                "required": ["subject_mention"],
                "properties": {
                    "subject_id": {"type": "null"},
                    "subject_mention": {"type": "string"},
                },
            },
        ],
    })

    record_id: Identifier | None = Field(default=None, description=(
        "Omit for a new judgment. Existing IDs require exact expected_revision and preserve subject, topic, context, "
        "facet, kind, report_mode and temporal interval. Direct self-report IDs also preserve the original utterance "
        "set; only their spans/interpretation may be revised. New utterances require a new ID; preference/intention "
        "corrections use explicit supersedes. Future changes use a new ID plus explicit supersedes."
    ))
    expected_revision: Revision = 0
    subject_id: Identifier | None = Field(default=None, description=(
        "Supplied stable ID of this claim's subject. Set exactly one non-null subject_id or subject_mention; "
        "omit or set the other to null. The evidence author or reported speaker does not fill this field."
    ))
    subject_mention: Text | None = Field(default=None, description=(
        "Unresolved mention of this claim's subject when no supplied stable ID applies. "
        "Set exactly one non-null subject_id or subject_mention; omit or set the other to null."
    ))
    topic: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
    context: Annotated[str, StringConstraints(strict=True, max_length=1000)] = ""
    statement: Text = Field(description="Model interpretation, never certified by exact quotation matching.")
    kind: Literal["interpretation", "self_report"]
    report_mode: Literal["direct", "reported", "hypothetical", "negated", "request", "intention", "ambiguous"]
    reported_speaker_id: Identifier | None = None
    facet: Literal["preference", "intention", "character", "event", "other"]
    valid_from: AwareDatetime | None = Field(default=None, description="For existing IDs null preserves the current interval; never moves its start.")
    valid_until: AwareDatetime | None = Field(default=None, description="For existing IDs null preserves the current interval; never removes an expiry.")
    anchors: list[EvidenceAnchor] = Field(min_length=1, max_length=32)
    supersedes: list[Supersession] = Field(default_factory=list, max_length=8)
    revision_reason: Text | None = None

    @model_validator(mode="after")
    def identity_and_interval(self):
        if (self.subject_id is None) == (self.subject_mention is None):
            raise ValueError("claim_requires_subject_or_unbound_mention")
        if self.expected_revision and (not self.record_id or not self.revision_reason):
            raise ValueError("claim_revision_requires_id_and_reason")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("claim_effective_interval_invalid")
        if self.supersedes and not (
            self.kind == "self_report" and self.report_mode == "direct"
            and self.facet in {"preference", "intention"} and self.subject_id
        ):
            raise ValueError("supersession_requires_direct_preference_or_intention")
        ids = [item.record_id for item in self.supersedes]
        if len(ids) != len(set(ids)):
            raise ValueError("supersession_target_duplicate")
        return self


class ImportanceProposal(_Frozen):
    record_id: Identifier
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    reason: Text
    anchors: list[EvidenceAnchor] = Field(min_length=1, max_length=16)


class MemoryUpdateGroup(_Frozen):
    group_id: Identifier
    depends_on: list[Identifier] = Field(default_factory=list, max_length=8)
    claims: list[ClaimProposal] = Field(default_factory=list, max_length=8)
    important: list[ImportanceProposal] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def operations_and_dependencies(self):
        if not (self.claims or self.important):
            raise ValueError("evidence_group_empty")
        if self.group_id in self.depends_on or len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("evidence_group_dependency_invalid")
        return self
