"""Bounded, process-local QQ identity hints backed by an earlier real UI card.

A visual hint is not a QQ identifier. The caller must obtain its exact label
from validated AX and hash location-independent avatar pixels from that same
frame. No model output, persistence, clock refresh or GUI action lives here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from threading import RLock


def _text(value: str, field: str, maximum: int) -> None:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid_" + field)


def _qq_id(value: str, field: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None:
        raise ValueError("invalid_" + field)


def _refs(value: tuple[str, ...], field: str) -> tuple[str, ...]:
    # Match visual memory's bounded reference contract; references may use a
    # provider-specific scheme. Never parse or dereference their contents here.
    if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 16:
        raise ValueError("invalid_" + field)
    for ref in value:
        _text(ref, field, 2048)
    return tuple(dict.fromkeys(value))


@dataclass(frozen=True)
class IdentityScope:
    login_epoch: str
    body_instance_id: str
    environment_revision: int
    window_id: str
    group_id: str
    self_member_id: str

    def __post_init__(self) -> None:
        for field in ("login_epoch", "body_instance_id", "window_id"):
            _text(getattr(self, field), field, 256)
        if type(self.environment_revision) is not int or not 0 <= self.environment_revision <= 2**63 - 1:
            raise ValueError("invalid_environment_revision")
        _qq_id(self.group_id, "group_id")
        _qq_id(self.self_member_id, "self_member_id")


@dataclass(frozen=True)
class VisualHint:
    display_name: str
    avatar_sha256: str

    def __post_init__(self) -> None:
        _text(self.display_name, "display_name", 256)
        if (not isinstance(self.avatar_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.avatar_sha256) is None):
            raise ValueError("invalid_avatar_sha256")


@dataclass(frozen=True)
class VerifiedIdentity:
    member_id: str
    profile_record_id: str
    capture_id: str
    observed_at: datetime
    display_name: str
    profile_evidence_refs: tuple[str, ...]
    identity_evidence_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _qq_id(self.member_id, "member_id")
        _text(self.profile_record_id, "profile_record_id", 2048)
        _text(self.capture_id, "capture_id", 2048)
        _text(self.display_name, "display_name", 256)
        if not isinstance(self.observed_at, datetime) or self.observed_at.utcoffset() is None:
            raise ValueError("invalid_observed_at")
        for field in ("profile_evidence_refs", "identity_evidence_refs", "evidence_refs"):
            object.__setattr__(self, field, _refs(getattr(self, field), field))
        required = set(self.profile_evidence_refs) | set(self.identity_evidence_refs)
        if not required <= set(self.evidence_refs):
            raise ValueError("incomplete_identity_evidence")


class SessionIdentityCache:
    """Immutable original bindings, exact scope, and fail-closed visual conflicts.

    Entries and conflict tombstones share the fixed capacity. Nothing is evicted:
    forgetting a conflict could resurrect an unsafe hint later in the session.
    A new process starts empty; invalidate clears the session explicitly.
    """

    def __init__(self, max_entries: int = 256):
        if type(max_entries) is not int or not 1 <= max_entries <= 256:
            raise ValueError("invalid_max_entries")
        self._maximum = max_entries
        self._entries: dict[tuple[IdentityScope, VisualHint], VerifiedIdentity | None] = {}
        self._lock = RLock()
        self._last_reason = "empty"

    @property
    def last_reason(self) -> str:
        with self._lock:
            return self._last_reason

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @staticmethod
    def _visible(scope: IdentityScope, hint: VisualHint, visible_hints: list[VisualHint] | tuple[VisualHint, ...]) -> bool:
        if type(scope) is not IdentityScope or type(hint) is not VisualHint:
            raise ValueError("invalid_identity_lookup")
        if (not isinstance(visible_hints, (tuple, list)) or len(visible_hints) > 256
                or any(type(item) is not VisualHint for item in visible_hints)):
            raise ValueError("invalid_visible_hints")
        return hint in visible_hints

    def resolve(self, scope: IdentityScope, hint: VisualHint,
                visible_hints: list[VisualHint] | tuple[VisualHint, ...]) -> VerifiedIdentity | None:
        visible = self._visible(scope, hint, visible_hints)
        with self._lock:
            if not visible:
                self._last_reason = "hint_not_visible"
                return None
            if any(item.display_name == hint.display_name and item.avatar_sha256 != hint.avatar_sha256
                   for item in visible_hints):
                self._last_reason = "visible_name_collision"
                return None
            key = (scope, hint)
            if key not in self._entries:
                self._last_reason = "not_found"
                return None
            identity = self._entries[key]
            self._last_reason = "permanent_identity_conflict" if identity is None else "cache_hit"
            return identity

    def record(self, scope: IdentityScope, hint: VisualHint, identity: VerifiedIdentity,
               visible_hints: list[VisualHint] | tuple[VisualHint, ...]) -> bool:
        visible = self._visible(scope, hint, visible_hints)
        if type(identity) is not VerifiedIdentity:
            raise ValueError("invalid_verified_identity")
        with self._lock:
            key = (scope, hint)
            if key in self._entries:
                existing = self._entries[key]
                if existing is None or existing.member_id != identity.member_id:
                    # A newly verified contradictory card permanently poisons
                    # this exact visual hint, even if the current view collides.
                    self._entries[key] = None
                    self._last_reason = "permanent_identity_conflict"
                    return False
            if not visible:
                self._last_reason = "hint_not_visible"
                return False
            if any(item.display_name == hint.display_name and item.avatar_sha256 != hint.avatar_sha256
                   for item in visible_hints):
                self._last_reason = "visible_name_collision"
                return False
            if key in self._entries:
                # Preserve the original profile snapshot, evidence and time;
                # repeated lookup or verification cannot pretend they are fresh.
                self._last_reason = "already_recorded"
                return True
            if len(self._entries) >= self._maximum:
                self._last_reason = "capacity_exhausted"
                return False
            self._entries[key] = identity
            self._last_reason = "recorded"
            return True

    def invalidate(self) -> None:
        with self._lock:
            self._entries.clear()
            self._last_reason = "invalidated"
