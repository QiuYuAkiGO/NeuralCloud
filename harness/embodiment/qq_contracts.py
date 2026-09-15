"""Transport-neutral QQ communication contracts; no GUI or store dependency.

Only a configured driver supplies frozen plans and independently verified
receipts. A DTO itself is not evidence that a GUI action was executed.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from harness.embodiment.capabilities import EmbodimentStatus, StrictModel

QQIdentifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[1-9][0-9]{0,19}$")]
Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9:._-]{1,256}$")]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Evidence = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)]
ReasonCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,95}$")]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]


def _single_line(text: str) -> str:
    if not text.strip() or any(char in text for char in "\r\n\x85\u2028\u2029"):
        raise ValueError("QQ send segments must be nonblank single lines")
    return text


class QQScope(StrictModel):
    body_instance_id: Identifier
    environment_revision: int = Field(ge=1, strict=True)
    window_id: Identifier
    group_id: QQIdentifier
    self_member_id: QQIdentifier


class QQDriverStatus(StrictModel):
    observed_at: AwareDatetime
    active_scope: QQScope | None = None
    context_signature: Digest | None = None
    connected: bool = Field(default=False, strict=True)
    observer_ready: bool = Field(default=False, strict=True)
    send_ready: bool = Field(default=False, strict=True)
    paused: bool = Field(default=True, strict=True)
    has_uncertain_delivery: bool = Field(default=False, strict=True)
    reason_code: ReasonCode | None = None


class QQCommunicationStatus(StrictModel):
    embodiment: EmbodimentStatus
    driver: QQDriverStatus | None = None


class _BoundedRequest(StrictModel):
    requested_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def bounded_deadline(self):
        if not 0 < (self.deadline - self.requested_at).total_seconds() <= 60:
            raise ValueError("QQ communication request must have a positive deadline at most 60 seconds away")
        return self


class QQReadRequest(_BoundedRequest):
    scope: QQScope


class QQVisibleMessage(StrictModel):
    capture_item_id: Identifier
    direction: Literal["incoming", "outgoing", "unknown"]
    kind: Literal["text", "sticker", "image", "unknown"]
    text: str | None = Field(default=None, max_length=32768, repr=False)
    # UI display text alone never supplies a numeric author identity.
    display_name: str | None = Field(default=None, max_length=256, repr=False)


class QQReadResult(StrictModel):
    scope: QQScope
    capture_id: Identifier
    observed_at: AwareDatetime
    context_signature: Digest
    evidence_refs: tuple[Evidence, ...] = Field(min_length=1, max_length=32)
    messages: tuple[QQVisibleMessage, ...] = Field(default_factory=tuple, max_length=200, repr=False)


class QQSendRequest(_BoundedRequest):
    scope: QQScope
    decision_id: Identifier
    reservation_id: Identifier
    segment_index: int = Field(ge=0, le=2, strict=True)
    segment_count: int = Field(ge=1, le=3, strict=True)
    text: Text = Field(repr=False)
    context_signature: Digest

    @field_validator("text")
    @classmethod
    def single_line(cls, value):
        return _single_line(value)

    @model_validator(mode="after")
    def valid_index(self):
        if self.segment_index >= self.segment_count:
            raise ValueError("QQ segment index is outside the frozen plan")
        return self


class QQFrozenSendPlan(StrictModel):
    scope: QQScope
    decision_id: Identifier
    reservation_id: Identifier
    input_hash: Digest
    reply_segments: tuple[Text, ...] = Field(min_length=1, max_length=3, repr=False)
    first_seen_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def frozen_plan(self):
        for item in self.reply_segments:
            _single_line(item)
        if len("\n\n".join(self.reply_segments)) > 8000:
            raise ValueError("QQ frozen reply exceeds the complete plan limit")
        if not 0 < (self.deadline - self.first_seen_at).total_seconds() <= 60:
            raise ValueError("QQ frozen plan must retain its original deadline")
        return self


class QQReconcileRequest(_BoundedRequest):
    send_request: QQSendRequest

    @model_validator(mode="after")
    def short_readonly_deadline(self):
        if (self.deadline - self.requested_at).total_seconds() > 15:
            raise ValueError("QQ read-only reconciliation is bounded to 15 seconds")
        return self


class QQDeliveryResult(StrictModel):
    scope: QQScope
    decision_id: Identifier
    reservation_id: Identifier
    segment_index: int = Field(ge=0, le=2, strict=True)
    segment_count: int = Field(ge=1, le=3, strict=True)
    status: Literal["sent", "not_sent", "unknown"]
    reason_code: ReasonCode
    receipt_id: Identifier | None = None
    delivered_at: AwareDatetime | None = None
    evidence_refs: tuple[Evidence, ...] = Field(default_factory=tuple, max_length=32)

    @model_validator(mode="after")
    def bounded_result(self):
        if self.segment_index >= self.segment_count:
            raise ValueError("QQ delivery result segment is outside its plan")
        if self.status == "sent" and (self.receipt_id is None or self.delivered_at is None or not self.evidence_refs):
            raise ValueError("Sent requires a durable verified receipt; an executor claim is insufficient")
        if self.status != "sent" and (self.receipt_id is not None or self.delivered_at is not None):
            raise ValueError("Unconfirmed delivery cannot claim a receipt")
        return self


class QQVerifiedReceipt(StrictModel):
    scope: QQScope
    decision_id: Identifier
    reservation_id: Identifier
    segment_index: int = Field(ge=0, le=2, strict=True)
    segment_count: int = Field(ge=1, le=3, strict=True)
    receipt_id: Identifier
    delivered_text: Text = Field(repr=False)
    delivered_at: AwareDatetime
    evidence_refs: tuple[Evidence, ...] = Field(min_length=1, max_length=32)
    verification: Literal["qq_ui_verified"]

    @field_validator("delivered_text")
    @classmethod
    def single_line(cls, value):
        return _single_line(value)

    @model_validator(mode="after")
    def valid_index(self):
        if self.segment_index >= self.segment_count:
            raise ValueError("QQ receipt segment is outside its plan")
        return self
