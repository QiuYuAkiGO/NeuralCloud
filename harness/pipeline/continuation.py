"""Session-level continuation: the model decides whether to keep speaking (02 §阶段 8)."""

from __future__ import annotations

import re
from typing import Literal

MARKER_CONTINUE = "[CONTINUE]"
MARKER_END = "[END]"

Continuation = Literal["continue", "end"]
StoppedBy = Literal["marker", "max_segments", "empty", "disabled"]

_TAIL_MARKER = re.compile(r"\[(CONTINUE|END)\]\s*\Z", re.IGNORECASE)

PROTOCOL_DIRECTIVE = (
    "你可以分多条消息说。每段末尾必须用 [CONTINUE] 表示还要接着说，"
    "或用 [END] 表示这轮说完。标记不会展示给用户。"
    "不要在正文中间使用这些标记。"
)


def strip_marker(text: str) -> tuple[str, Continuation]:
    """Keep only a trailing marker. Missing marker defaults to end (safe stop)."""
    match = _TAIL_MARKER.search(text)
    if not match:
        return text.strip(), "end"
    body = text[: match.start()].strip()
    kind = match.group(1).upper()
    return body, "continue" if kind == "CONTINUE" else "end"


def build_nudge(segments: list[str]) -> str:
    last = segments[-1] if segments else ""
    count = len(segments)
    return (
        f"你已经说了 {count} 段。刚刚说的是：\n{last}\n\n"
        "如果还有要说的，接着讲下一段，段尾标 [CONTINUE]；"
        "如果这轮已经说完，标 [END]。不要重复刚说过的内容。"
    )
