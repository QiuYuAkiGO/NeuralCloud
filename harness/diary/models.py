"""Private diary originals shared between Runtime storage and Cognitive memory."""

from __future__ import annotations

import hashlib
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator


Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]


class DiaryOriginal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: Identifier
    decision_id: Identifier
    text: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000, pattern=r"\S")]
    written_at: AwareDatetime
    body_hash: Annotated[str, StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
    source_event_id: Identifier
    homeostatic_summary: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def matching_digest(self):
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if digest != self.body_hash:
            raise ValueError("diary_body_hash_mismatch")
        return self
