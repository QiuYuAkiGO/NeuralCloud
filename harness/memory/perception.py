"""Perception backend protocol. Chat V1 is a stub; desktop capture lands in 03."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from harness.core.models import WorldState


class PerceptionBackend(Protocol):
    def observe(self, *, now: datetime | None = None) -> WorldState: ...


class ChatPerceptionBackend:
    """Chat-scene world state: time, channel, presence. No screenshot."""

    def __init__(self, *, channel: str = "chat", user_presence: str = "active") -> None:
        self.channel = channel
        self.user_presence = user_presence

    def observe(self, *, now: datetime | None = None) -> WorldState:
        moment = now or datetime.now(UTC)
        return WorldState(
            time=moment,
            user_presence=self.user_presence,
            user_activity="chatting" if self.user_presence == "active" else "unknown",
            channel=self.channel,
        )
