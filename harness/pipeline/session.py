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
        self._persistence_version = 0
        self._persistence_dirty: dict[str, int] = {}

    def _mark_changed(self, session_id: str) -> None:
        self._persistence_version += 1
        self._persistence_dirty[session_id] = self._persistence_version

    def persistence_changes(self):
        """Return only changed windows; None is an explicitly deleted session."""
        versions = dict(self._persistence_dirty)
        return versions, {key: list(self._turns[key]) if key in self._turns else None for key in versions}

    def acknowledge_persistence(self, versions: dict[str, int]) -> None:
        for key, version in versions.items():
            if self._persistence_dirty.get(key) == version:
                del self._persistence_dirty[key]

    def append(
        self,
        session_id: str,
        role: Literal["user", "character"],
        text: str,
        *,
        speaker: str | None = None,
        event_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        turn = SessionTurn(
            role=role,
            text=text,
            speaker=speaker,
            event_id=event_id,
            timestamp=timestamp,
        )
        bucket = self._turns[session_id]
        bucket.append(turn)
        overflow = len(bucket) - self.window
        if overflow > 0:
            del bucket[:overflow]
        self._mark_changed(session_id)

    def recent(self, session_id: str) -> list[SessionTurn]:
        return list(self._turns.get(session_id, []))

    def replace(self, session_id: str, turns: list[SessionTurn]) -> None:
        bucket = list(turns[-self.window :]) if turns else []
        self._turns[session_id] = bucket
        self._mark_changed(session_id)

    def clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            for key in self._turns:
                self._mark_changed(key)
            self._turns.clear()
        else:
            self._turns.pop(session_id, None)
            self._mark_changed(session_id)

    def snapshot(self) -> dict[str, list[SessionTurn]]:
        """Return a detached snapshot suitable for a persistent projection."""

        return {session_id: list(turns) for session_id, turns in self._turns.items()}

    def restore(self, sessions: dict[str, list[SessionTurn]]) -> None:
        """Replace all sessions from a previously persisted projection."""

        self.clear()
        for session_id, turns in sessions.items():
            self.replace(session_id, turns)

    def contains_event(self, session_id: str, event_id: str) -> bool:
        return any(turn.event_id == event_id for turn in self._turns.get(session_id, []))
