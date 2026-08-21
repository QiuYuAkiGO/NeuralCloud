"""In-memory recent-turn window per session."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Literal

from harness.core.inference import SessionTurn


class SessionStore:
    def __init__(self, window: int = 10) -> None:
        self.window = window
        self._turns: dict[str, list[SessionTurn]] = defaultdict(list)

    def append(
        self,
        session_id: str,
        role: Literal["user", "character"],
        text: str,
        *,
        event_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        turn = SessionTurn(role=role, text=text, event_id=event_id, timestamp=timestamp)
        bucket = self._turns[session_id]
        bucket.append(turn)
        overflow = len(bucket) - self.window
        if overflow > 0:
            del bucket[:overflow]

    def recent(self, session_id: str) -> list[SessionTurn]:
        return list(self._turns.get(session_id, []))

    def clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._turns.clear()
        else:
            self._turns.pop(session_id, None)
