"""Map benchmark role profiles onto PersonaCore (01 §2.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from harness.core.models import PersonaCore, PersonaIdentity

from eval.rp.schema import RPEpisode, Turn


def is_character_turn(turn: Turn, episode: RPEpisode) -> bool:
    if turn.role == "character":
        return True
    if turn.role in {"user", "other"}:
        return False
    speaker = turn.speaker.strip()
    if speaker in {"character", "assistant", episode.role_id}:
        return True
    name = str(episode.profile.get("姓名") or episode.profile.get("name") or "")
    return bool(name) and speaker == name


def render_profile(profile: dict[str, Any]) -> str:
    if not profile:
        return ""
    if set(profile) <= {"text"}:
        return str(profile.get("text") or "")
    lines: list[str] = []
    for key, value in profile.items():
        text = _as_text(value)
        if text:
            lines.append(f"{key}：{text}")
    return "\n".join(lines)


def persona_from_episode(episode: RPEpisode, *, now: datetime) -> PersonaCore:
    profile = episode.profile
    name = str(profile.get("姓名") or profile.get("name") or episode.role_id)
    traits = _split_list(profile.get("人物性格") or profile.get("core_traits") or profile.get("personality"))
    values = _split_list(profile.get("价值观") or profile.get("values") or profile.get("喜欢的事情/东西"))
    speech = _split_list(profile.get("说话风格") or profile.get("speech_style") or profile.get("人物说话风格"))
    boundary = _split_list(profile.get("knowledge_boundary"))
    if episode.bench == "timechara":
        period = str(episode.meta.get("character_period") or "")
        series = str(episode.meta.get("series") or profile.get("series") or "")
        if series and series not in traits:
            traits.append(f"来自 {series}")
        if period:
            boundary.append(f"不知道 {period} 之后发生的事")
            boundary.append(f"当前时间点：{period}")
        if not speech:
            speech = ["第一人称", "符合原著口吻"]
    elif episode.bench == "roleeval":
        category = str(episode.meta.get("category") or "")
        if category:
            traits.append(f"熟悉{category}角色知识")
        if not speech:
            speech = ["直接回答选择题", "只输出选项字母"]
    elif episode.bench == "charactereval":
        if not speech:
            speech = ["保持角色口吻", "用角色会说的话"]
        extra = profile.get("人物经历")
        if extra and not values:
            values = [_as_text(extra)[:80]]
    if not traits:
        traits = [name]
    return PersonaCore(
        persona_id="persona_eval",
        version="eval",
        identity=PersonaIdentity(
            name=name,
            core_traits=traits[:8],
            values=values[:6],
            speech_style=speech[:6],
            knowledge_boundary=boundary[:6],
            locked=True,
        ),
        created_at=now,
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "、".join(_as_text(item) for item in value if _as_text(item))
    if isinstance(value, dict):
        return "；".join(f"{key}:{_as_text(item)}" for key, item in value.items() if _as_text(item))
    return str(value)


def _split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in (_as_text(v) for v in value) if item]
    text = _as_text(value)
    if not text:
        return []
    for sep in ("，", "、", ";", "；", "/", "|"):
        text = text.replace(sep, ",")
    parts = [part.strip() for part in text.split(",")]
    return [part for part in parts if part]
