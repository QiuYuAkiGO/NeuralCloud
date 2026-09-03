"""Session-level continuation: the model proposes; the program admits new beats (02 §阶段 8)."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Literal

MARKER_CONTINUE = "[CONTINUE]"
MARKER_END = "[END]"

Continuation = Literal["continue", "end"]
StoppedBy = Literal["marker", "max_segments", "empty", "disabled", "duplicate"]

ECHO_RATIO = 0.88
ECHO_CONTAIN_MIN_CHARS = 24
ECHO_CONTAIN_RATIO = 0.85

# Trailing / line-boundary protocol marker. Markdown wrapping, fullwidth
# brackets, and extra punctuation are tolerated because models rarely emit
# the token in the exact canonical form.
_MARKER_CORE = r"(?:\*{1,2}|`)?[\[【](CONTINUE|END)[\]】](?:\*{1,2}|`)?"
_TAIL_MARKER = re.compile(rf"[ \t]*{_MARKER_CORE}[。．.!！?？]*\s*\Z", re.IGNORECASE)
_LINE_MARKER = re.compile(rf"[ \t]*{_MARKER_CORE}[。．.!！?？]*[ \t]*(?:\n+|$)", re.IGNORECASE)
_ANY_MARKER = re.compile(rf"[ \t]*{_MARKER_CORE}[。．.!！?？]*", re.IGNORECASE)

PROTOCOL_DIRECTIVE = (
    "你可以分多条消息说。默认用 [END] 结束本轮；"
    "只有还有尚未说过的下一拍时才标 [CONTINUE]。"
    "不要复述或改写已经说过的内容。"
    "标记不会展示给用户，不要写在正文中间。"
)


def hide_markers(text: str) -> str:
    """Strip protocol markers so they never appear in user-facing text."""
    cleaned = _ANY_MARKER.sub(" ", text or "")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def strip_marker(text: str) -> tuple[str, Continuation]:
    """Keep only a trailing marker. Missing marker defaults to end (safe stop)."""
    match = _TAIL_MARKER.search(text or "")
    if not match:
        return hide_markers(text or ""), "end"
    body = hide_markers(text[: match.start()])
    kind = match.group(1).upper()
    return body, "continue" if kind == "CONTINUE" else "end"


def split_protocol_segments(text: str) -> tuple[list[str], Continuation]:
    """Split one model output on line-boundary markers; hide markers in each part.

    Continuation is continue only if the last marker is CONTINUE and nothing
    follows it — the model stopped to wait for the next turn.
    """
    raw = text or ""
    matches = list(_LINE_MARKER.finditer(raw))
    if not matches:
        body, trailing_marker = strip_marker(raw)
        return ([body] if body else []), trailing_marker

    parts: list[str] = []
    cursor = 0
    last_kind = "END"
    for match in matches:
        chunk = hide_markers(raw[cursor : match.start()])
        if chunk:
            parts.append(chunk)
        last_kind = match.group(1).upper()
        cursor = match.end()
    tail = hide_markers(raw[cursor:])
    if tail:
        parts.append(tail)
        marker: Continuation = "end"
    else:
        marker = "continue" if last_kind == "CONTINUE" else "end"
    return parts, marker


def normalize_for_echo(text: str) -> str:
    return re.sub(r"\s+", "", text or "").strip()


def is_echo(new: str, existing: list[str], *, ratio: float = ECHO_RATIO) -> bool:
    """True when `new` restates an already committed segment (exact, contained, or near-duplicate)."""
    needle = normalize_for_echo(new)
    if not needle:
        return False
    for prev in existing:
        hay = normalize_for_echo(prev)
        if not hay:
            continue
        if needle == hay:
            return True
        shorter, longer = (needle, hay) if len(needle) <= len(hay) else (hay, needle)
        if (
            len(shorter) >= ECHO_CONTAIN_MIN_CHARS
            and (len(shorter) / len(longer)) >= ECHO_CONTAIN_RATIO
            and shorter in longer
        ):
            return True
        if SequenceMatcher(None, needle, hay).ratio() >= ratio:
            return True
    return False


def fresh_bodies(bodies: list[str], existing: list[str]) -> tuple[list[str], bool]:
    """Keep bodies until the first echo of `existing` or an earlier body in this batch."""
    accepted: list[str] = []
    seen = list(existing)
    echoed = False
    for body in bodies:
        if not body:
            continue
        if is_echo(body, seen):
            echoed = True
            break
        accepted.append(body)
        seen.append(body)
    return accepted, echoed


def build_nudge(segments: list[str]) -> str:
    count = len(segments)
    return (
        f"你已经说了 {count} 段（见上方 assistant 消息）。"
        "只输出还没说过的下一拍，段尾标 [CONTINUE]；"
        "没有要补的就标 [END]。不要复述或改写已说内容。"
        "不要把标记写进会展示给用户的句子里。"
    )
