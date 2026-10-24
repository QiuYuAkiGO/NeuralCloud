"""App-scoped QQ Open Platform identifiers; never aliases for numeric QQ IDs."""

from __future__ import annotations

import base64
import re

APP = r"[1-9][0-9]{0,19}"
OPENID = r"[A-Za-z0-9_-]{1,128}"
GROUP = rf"qqbot:{APP}:group:{OPENID}"
PERSON = rf"qqbot:{APP}:(?:bot|member|user):{OPENID}"
IDENTITY = rf"(?:{GROUP}|{PERSON})"
MESSAGE = rf"qqbot:{APP}:message:[A-Za-z0-9_-]{{2,684}}"


def identity(app_id: str, kind: str, openid: str) -> str:
    if not all(isinstance(item, str) for item in (app_id, kind, openid)):
        raise ValueError("qqbot_identity_invalid")
    value = f"qqbot:{app_id}:{kind}:{openid}"
    if kind not in {"bot", "member", "user", "group"} or re.fullmatch(IDENTITY, value) is None:
        raise ValueError("qqbot_identity_invalid")
    return value


def split_identity(value: str, *, kind: str, app_id: str) -> str:
    if not isinstance(value, str) or not value.startswith(f"qqbot:{app_id}:{kind}:"):
        raise ValueError("qqbot_identity_scope_invalid")
    raw = value.rsplit(":", 1)[1]
    if identity(app_id, kind, raw) != value:
        raise ValueError("qqbot_identity_scope_invalid")
    return raw


def message_id(app_id: str, raw: str) -> str:
    if not isinstance(raw, str) or not 1 <= len(raw.encode()) <= 512 or any(ord(c) < 33 or ord(c) == 127 for c in raw):
        raise ValueError("qqbot_message_id_invalid")
    encoded = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    value = f"qqbot:{app_id}:message:{encoded}"
    if re.fullmatch(MESSAGE, value) is None:
        raise ValueError("qqbot_message_id_invalid")
    return value


def raw_message_id(app_id: str, value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(MESSAGE, value) is None or not value.startswith(f"qqbot:{app_id}:"):
        raise ValueError("qqbot_message_id_scope_invalid")
    part = value.rsplit(":", 1)[1]
    try:
        raw = base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True).decode()
    except (ValueError, UnicodeError):
        raise ValueError("qqbot_message_id_invalid") from None
    if message_id(app_id, raw) != value:
        raise ValueError("qqbot_message_id_invalid")
    return raw
