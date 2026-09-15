"""Private, transport-neutral contracts for Misty's persistent thought board."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from harness.embodiment.capabilities import EmbodimentStatus, StrictModel

Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9:._-]{1,256}$")]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=8000)]


class BoardScope(StrictModel):
    body_instance_id: Identifier
    board_id: Identifier = "misty-thoughts"
    owner_id: Identifier = "misty"


class _BoundedRequest(StrictModel):
    scope: BoardScope
    requested_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def bounded_deadline(self):
        if not 0 < (self.deadline - self.requested_at).total_seconds() <= 60:
            raise ValueError("board request requires a positive deadline at most 60 seconds away")
        return self


class BoardAppendRequest(_BoundedRequest):
    entry_id: Identifier
    decision_id: Identifier
    text: Text = Field(repr=False)
    title: str = Field(default="", max_length=120, repr=False)
    source_event_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=32)

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("board text must be nonblank and cannot contain nulls")
        return value

    @field_validator("source_event_ids")
    @classmethod
    def distinct_sources(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("board source event identifiers must be distinct")
        return value


class BoardReceipt(StrictModel):
    scope: BoardScope
    entry_id: Identifier
    decision_id: Identifier
    text_sha256: Digest
    created_at: AwareDatetime
    evidence_ref: str = Field(min_length=1, max_length=2048)
    sequence: int = Field(ge=1, strict=True)


class BoardEntry(StrictModel):
    receipt: BoardReceipt
    text: Text = Field(repr=False)
    title: str = Field(default="", max_length=120, repr=False)
    source_event_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=32)


class BoardReadRequest(_BoundedRequest):
    after_sequence: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=50, ge=1, le=100, strict=True)
    latest: bool = Field(default=False, strict=True)


class BoardReadResult(StrictModel):
    scope: BoardScope
    entries: tuple[BoardEntry, ...] = Field(default_factory=tuple, max_length=100, repr=False)
    observed_at: AwareDatetime


class BoardReconcileRequest(_BoundedRequest):
    entry_id: Identifier
    decision_id: Identifier
    text_sha256: Digest

    @model_validator(mode="after")
    def readonly_deadline(self):
        if (self.deadline - self.requested_at).total_seconds() > 15:
            raise ValueError("board reconciliation is bounded to 15 seconds")
        return self


class BoardReconcileResult(StrictModel):
    scope: BoardScope
    entry_id: Identifier
    decision_id: Identifier
    status: Literal["committed", "not_found"]
    receipt: BoardReceipt | None = None

    @model_validator(mode="after")
    def bound_receipt(self):
        if (self.status == "committed") != (self.receipt is not None):
            raise ValueError("committed board results require a durable receipt")
        if self.receipt is not None and (
            self.receipt.scope != self.scope or self.receipt.entry_id != self.entry_id
            or self.receipt.decision_id != self.decision_id
        ):
            raise ValueError("board reconciliation receipt identity mismatch")
        return self


class BoardEvidenceRequest(_BoundedRequest):
    entry_id: Identifier

    @model_validator(mode="after")
    def readonly_deadline(self):
        if (self.deadline - self.requested_at).total_seconds() > 15:
            raise ValueError("board evidence lookup is bounded to 15 seconds")
        return self


class BoardStatus(StrictModel):
    scope: BoardScope
    embodiment: EmbodimentStatus
    uncertain_entries: tuple[Identifier, ...] = Field(default_factory=tuple)
