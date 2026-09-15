"""Configured QQ observation scopes; only the home group may enable sending."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints

from harness.qq.memory import QQID


class QQGroupRosterEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    group_id: QQID
    label: Annotated[str, StringConstraints(max_length=128, pattern=r"^[^\x00-\x1f\x7f]*$")] = ""
    observe: StrictBool = True
    send: StrictBool = False


def configured_groups(settings: object) -> tuple[QQGroupRosterEntry, ...]:
    """Validate a roster, or derive the legacy home scope when it is omitted.

    Session authorization is checked by the watcher against its allowed groups.
    A roster's labels are operator metadata and never supply numeric identity.
    """
    def value(name: str, default=None):
        return settings.get(name, default) if isinstance(settings, Mapping) else getattr(settings, name, default)

    home = value("group_id", value("expected_group_id"))
    automatic_send = value("automatic_send", False)
    # Validate even the legacy scope without accepting strings as booleans.
    fallback = QQGroupRosterEntry(group_id=home, send=automatic_send)
    raw = value("group_roster")
    if raw is None:
        return (fallback,)
    if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= 16:
        raise ValueError("qq_group_roster_size_invalid")
    groups = tuple(QQGroupRosterEntry.model_validate(entry) for entry in raw)
    if len({entry.group_id for entry in groups}) != len(groups):
        raise ValueError("qq_group_roster_duplicate")
    home_entry = next((entry for entry in groups if entry.group_id == home), None)
    if home_entry is None or not home_entry.observe:
        raise ValueError("qq_group_roster_home_observation_required")
    if any(entry.send and (entry.group_id != home or not automatic_send) for entry in groups):
        raise ValueError("qq_group_roster_send_scope_invalid")
    return groups
