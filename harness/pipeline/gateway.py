"""Gateway: raw input becomes an InteractionEvent (02 §阶段 1)."""

from __future__ import annotations

from datetime import UTC, datetime

from harness.core.models import InteractionEvent


class Gateway:
    def ingest(
        self,
        source: InteractionEvent | str,
        *,
        session_id: str | None = None,
        speaker: str = "user",
        channel: str = "chat",
        modality: str = "text",
        timestamp: datetime | None = None,
        event_id: str | None = None,
    ) -> InteractionEvent:
        if isinstance(source, InteractionEvent):
            return source
        kwargs: dict[str, object] = {
            "timestamp": timestamp or datetime.now(UTC),
            "channel": channel,
            "speaker": speaker,
            "modality": modality,
            "raw_text": source,
            "session_id": session_id or "sess_default",
        }
        if event_id is not None:
            kwargs["event_id"] = event_id
        return InteractionEvent.model_validate(kwargs)
