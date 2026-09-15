"""QQ conversation keys; private peers never masquerade as numeric QQ groups.

Older tables and DTOs name their partition key ``group_id``. A private channel
uses the explicit ``private:<self>:<peer>`` namespace in that legacy field; only
the transport adapter converts a validated scope into OneBot wire arguments.
"""
from __future__ import annotations

from typing import Annotated
import re

from pydantic import StringConstraints

QQ_NUMBER = r"[1-9][0-9]{0,19}"
CONVERSATION_PATTERN = rf"^(?:{QQ_NUMBER}|private:{QQ_NUMBER}:{QQ_NUMBER})$"
QQConversationID = Annotated[str, StringConstraints(strict=True, pattern=CONVERSATION_PATTERN)]


def conversation_scope(value: str) -> dict:
    if not isinstance(value, str) or re.fullmatch(CONVERSATION_PATTERN, value) is None:
        raise ValueError("qq_conversation_scope_invalid")
    if value.startswith("private:"):
        _, self_id, peer_id = value.split(":")
        if self_id == peer_id:
            raise ValueError("qq_private_self_peer_invalid")
        return {"kind": "private", "self_member_id": self_id, "peer_id": peer_id}
    return {"kind": "group", "group_id": value}


def private_scope_id(self_member_id: str, peer_id: str) -> str:
    value = f"private:{self_member_id}:{peer_id}"
    conversation_scope(value)
    return value


def is_private_scope(value: str) -> bool:
    return conversation_scope(value)["kind"] == "private"


def conversation_audience(value: str) -> str:
    scope = conversation_scope(value)
    return (f"qq:private:{scope['self_member_id']}:{scope['peer_id']}"
            if scope["kind"] == "private" else "qq:group:" + value)
