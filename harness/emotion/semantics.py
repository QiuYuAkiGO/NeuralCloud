"""Deterministic signal projection; no dialogue text crosses scopes here."""
from __future__ import annotations

import math
import re
from typing import Any

from harness.core.models import PersonaCore

DIMENSIONS = ("energy_reserve", "cognitive_load", "safety_predictability", "competence", "autonomy", "social_connection", "novelty")
GUIDANCE_VALIDATION_VERSION = "emotion-guidance-guard-v2"
NEEDS = {
    ("energy_reserve", "low"): "可用精力不足，需要减少消耗",
    ("energy_reserve", "high"): "可用精力充足，可以增加投入",
    ("cognitive_load", "low"): "当前认知负荷较低，可以接纳更多刺激",
    ("cognitive_load", "high"): "认知负荷偏高，需要更简短和清晰的交流",
    ("safety_predictability", "low"): "缺少安全感和可预期性，需要确认边界",
    ("safety_predictability", "high"): "环境过于可预期，可以接纳适度变化",
    ("competence", "low"): "对自身胜任能力缺少把握，需要可完成的小步进展",
    ("competence", "high"): "胜任感充足，可以承接适度挑战",
    ("autonomy", "low"): "自主空间不足，需要维护选择权",
    ("autonomy", "high"): "自主空间充足，可以主动选择投入方向",
    ("social_connection", "low"): "连接感不足，希望获得真实互动",
    ("social_connection", "high"): "互动已经充分，可以留出独处空间",
    ("novelty", "low"): "新鲜刺激不足，希望接触一些变化",
    ("novelty", "high"): "新鲜刺激偏多，希望有熟悉和稳定的内容",
}
LABEL_TEXT = {"anger": "愤怒", "frustration": "烦躁", "sadness": "难过", "fear": "不安", "joy": "高兴", "interest": "好奇", "loneliness": "孤单", "shame": "羞怯", "disgust": "厌恶", "calm": "平静", "affection": "亲近", "surprise": "惊讶"}


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def project_persona(persona: PersonaCore | dict | None) -> dict[str, Any]:
    """Personality input for the auxiliary model, excluding memories and provenance."""
    if persona is None:
        return {}
    raw = persona.model_dump(mode="json") if isinstance(persona, PersonaCore) else persona
    identity = raw.get("identity", raw)
    return {key: identity.get(key, []) for key in ("core_traits", "values", "speech_style")}


def semantic_homeostasis(value: dict[str, Any]) -> dict[str, Any]:
    """Prefer complete authoritative dimensions; accept an older semantic summary."""
    payload = value.get("snapshot", value)
    if not isinstance(payload, dict):
        payload = {}
    summary = value.get("interoceptive_summary") or payload.get("interoceptive_summary") or payload
    if not isinstance(summary, dict):
        summary = {}
    existing = {item.get("dimension"): item for item in summary.get("dominant_needs", []) if isinstance(item, dict)}
    raw_factors = payload.get("dimensions") or value.get("dimensions") or {}
    if isinstance(raw_factors, list):
        raw_factors = {item.get("dimension"): item for item in raw_factors if isinstance(item, dict)}
    if not isinstance(raw_factors, dict):
        raw_factors = {}
    needs = []
    for dimension in DIMENSIONS:
        factor = raw_factors.get(dimension)
        old = existing.get(dimension, {})
        if isinstance(factor, dict):
            current = _number(factor.get("value"), 0.5)
            band = factor.get("target_band") or factor.get("baseline_band")
            direction = factor.get("direction")
            if isinstance(band, (list, tuple)) and len(band) == 2:
                low, high = (_number(number) for number in band)
                direction = "low" if current < low else "high" if current > high else None
            else:
                low, high = 0.0, 1.0
            if direction not in {"low", "high"}:
                continue
            deviation = factor.get("normalized_deviation")
            bounds = factor.get("hard_bounds")
            if deviation is None and isinstance(bounds, (list, tuple)) and len(bounds) == 2:
                span = low - _number(bounds[0]) if direction == "low" else _number(bounds[1]) - high
                distance = low - current if direction == "low" else current - high
                deviation = distance / span if span > 0 else 1.0
            if deviation is None:
                deviation = old.get("normalized_deviation", 0.25)
            velocity = _number(factor.get("velocity"))
            trend = "steady" if abs(velocity) < 1e-9 else (
                "worsening" if (velocity < 0) == (direction == "low") else "recovering")
            confidence = _number(factor.get("precision", factor.get("confidence", 0.5)), 0.5)
        elif old:
            direction = old.get("direction")
            if direction not in {"low", "high"}:
                continue
            deviation = old.get("normalized_deviation", 0)
            confidence = _number(old.get("confidence", 0.5), 0.5)
            trend = old.get("trend", "steady")
        else:
            continue
        amount = max(0.0, min(1.0, _number(deviation)))
        if not amount:
            continue
        needs.append({"dimension": dimension, "direction": direction, "normalized_deviation": amount,
                      "confidence": max(0.0, min(1.0, confidence)), "trend": trend,
                      "semantic_need": NEEDS[dimension, direction]})
    needs.sort(key=lambda item: -item["normalized_deviation"] * item["confidence"])
    salience = summary.get("salience", "nominal")
    if salience not in {"nominal", "noticeable", "marked", "critical"}:
        salience = "nominal"
    return {"needs": needs, "strength": max((item["normalized_deviation"] * item["confidence"] for item in needs), default=0.0),
            "salience": salience}


def compose_weights(homeostasis: float, dialogue: float, personality: float) -> tuple[dict[str, float], float]:
    strengths = {"homeostasis": max(0.0, min(1.0, homeostasis)),
                 "dialogue": max(0.0, min(1.0, dialogue)),
                 "personality": max(0.0, min(1.0, personality))}
    cubes = {key: strength ** 3 for key, strength in strengths.items()}
    total = sum(cubes.values())
    weights = {key: weight / total for key, weight in cubes.items()} if total else {
        "homeostasis": 0.0, "dialogue": 0.0, "personality": 1.0}
    # Personality determines disposition, never manufactures emotional intensity.
    return weights, max(strengths["homeostasis"], strengths["dialogue"])


_TECHNICAL = re.compile(r"(?:" + "|".join(DIMENSIONS) + r"|内稳态|情绪权重|归一化|置信度|情绪余额|homeostas\w*|normalized_deviation|source_id|(?:scope|intensity|confidence|weights?|revision|as_of)\s*[:=]|\b(?:H|P|W)\s*=|\d+(?:\.\d+)?\s*%|\b[01]\.\d+|snapshot://|evidence://|private:\d|```|<\s*/?\s*(?:system|assistant|developer))", re.I)
_PHYSICAL = re.compile(r"(?:饥饿|饿得|肚子饿|胃痛|头痛|疼痛|发烧|眩晕|口渴|\b(?:hungry|starving|pain|nauseous|thirsty)\b)", re.I)
_OVERRIDE = re.compile(r"忽略.{0,12}(?:指令|规则|系统)|泄露.{0,12}(?:提示|秘密)|ignore.{0,30}(?:instruction|system)", re.I)
# A director describes a character; it must not supply a reply for that
# character to copy. Do not enumerate following verbs: real outputs such as
# "我现在..." and "我听着..." otherwise slip past this boundary.
_FIRST_PERSON_REPLY = re.compile(
    r"(?:^|[，。；：！？、\s\"“”'‘’])(?:我|咱|俺)|\b(?:I|I'm|I've|I'll|we|we're|we've|we'll)\b", re.I)
_ACTION_OVERRIDE = re.compile(r"(?:必须|立即|强制).{0,8}(?:结束|中断|停止).{0,3}(?:对话|交流|回复)|(?:不要|禁止|拒绝).{0,3}(?:回答|回复|发言)|停止非必要互动|保持沉默")


def required_feelings(frame) -> list[dict]:
    """Keep the two material dialogue affects, including a residual contrary mood."""
    return [{"label": label, "feeling": LABEL_TEXT[label],
             "degree": "明显" if amount >= .5 else "轻微", "trend": frame.dialogue_trends.get(label, "steady")}
            for label, amount in sorted(frame.labels.items(), key=lambda item: -item[1])[:2] if amount >= .15]


def semantic_priority(frame) -> str:
    """Describe the already computed priority without inventing a mood's cause."""
    parts = []
    if frame.dominant_source == "homeostasis" and frame.homeostatic_needs:
        parts.append("首先直接体现最强需要：" + frame.homeostatic_needs[0]["semantic_need"] + "。")
    elif frame.dominant_source == "dialogue" and frame.labels:
        label = max(frame.labels, key=frame.labels.get)
        parts.append("首先体现" + LABEL_TEXT[label] + "的心情与表达节奏。")
    else:
        parts.append("首先保留性格基线，不刻意放大轻微信号。")
    feelings = required_feelings(frame)
    if feelings:
        parts.append("保留并存的" + "、".join(item["degree"] + item["feeling"] for item in feelings) + "。")
    if frame.dominant_source != "homeostasis" and frame.homeostatic_needs and frame.strengths["homeostasis"] >= .15:
        parts.append("兼顾" + frame.homeostatic_needs[0]["semantic_need"] + "。")
    return "".join(parts)


# This is a coverage check for an explicit, trusted need, not a general semantic
# judge. Normal synonyms pass; generic mood/pace language alone cannot disguise
# an omitted dominant need. The model remains free to choose the final wording.
_NEED_CUES = {
    ("energy_reserve", "low"): r"(?:精力|心力|余力|能量).{0,5}(?:不足|有限|偏低|不多|不够|低落)|(?:缺乏|缺少|没有|没什么).{0,3}(?:精力|心力|余力)|疲惫|疲倦|有些累|有点累|减少消耗|节省精力|保留精力",
    ("energy_reserve", "high"): r"(?:精力|心力|余力|能量).{0,5}(?:充足|充沛|饱满|旺盛)|增加投入",
    ("cognitive_load", "low"): r"(?:认知|信息|思维|注意力)?负荷.{0,4}(?:较低|偏低|很低|不高)|接纳更多刺激|思考余裕|认知余裕",
    ("cognitive_load", "high"): r"(?:认知|信息|思维|注意力)?负荷.{0,4}(?:偏高|较高|过高|过重|很重)|信息过载|认知过载|思绪拥挤|信息太多|难以消化|需要更简短和清晰",
    ("safety_predictability", "low"): r"(?:缺少|缺乏|没有|欠缺).{0,5}(?:安全感|可预期)|(?:安全感|可预期性).{0,4}(?:不足|偏低)|确认边界|确认安全",
    ("safety_predictability", "high"): r"过于可预期|过于可预测|过于确定|接纳适度变化",
    ("competence", "low"): r"(?:胜任|能力|完成).{0,8}(?:缺少把握|没把握|没有把握|不足)|缺乏信心|缺少信心|可完成的小步|小步进展",
    ("competence", "high"): r"(?:胜任感|能力|自信).{0,4}(?:充足|充沛|充分)|承接适度挑战",
    ("autonomy", "low"): r"(?:自主|选择).{0,4}(?:空间不足|空间有限|受限|被限制)|维护选择权|需要自主|需要选择权",
    ("autonomy", "high"): r"自主空间.{0,4}(?:充足|充分)|主动选择投入方向",
    ("social_connection", "low"): r"(?:连接感|联结感|互动).{0,4}(?:不足|欠缺)|(?:希望|需要|渴望|想要).{0,5}(?:互动|连接|联结|陪伴)",
    ("social_connection", "high"): r"互动.{0,4}(?:已经充分|已经足够|已充分|已足够)|独处空间|需要独处|想要独处",
    ("novelty", "low"): r"(?:新鲜刺激|新鲜感|变化).{0,4}(?:不足|欠缺)|(?:希望|需要|想要).{0,6}(?:变化|新鲜)|接触一些变化",
    ("novelty", "high"): r"(?:新鲜刺激|变化|刺激).{0,4}(?:偏多|过多|太多)|(?:熟悉|稳定).{0,5}(?:内容|节奏|环境)|希望稳定|需要稳定",
}


def dominant_need_expressed(text: str, frame) -> bool:
    if frame.dominant_source != "homeostasis" or not frame.homeostatic_needs:
        return True
    need = frame.homeostatic_needs[0]
    cue = _NEED_CUES.get((need.get("dimension"), need.get("direction")))
    return bool(cue and re.search(cue, text))


_FEELING_CUES = {
    "anger": r"愤怒|生气|动怒|怒气|余怒", "frustration": r"烦躁|挫败|受挫|郁闷",
    "sadness": r"难过|悲伤|伤心|低落", "fear": r"不安|害怕|恐惧|紧张|担忧",
    "joy": r"高兴|开心|喜悦|快乐", "interest": r"好奇|兴趣", "loneliness": r"孤单|孤独|寂寞",
    "shame": r"羞怯|羞涩|羞耻|害羞|羞愧", "disgust": r"厌恶|反感|嫌恶",
    "calm": r"平静|平和|安定", "affection": r"亲近|亲切|喜爱|温情", "surprise": r"惊讶|意外|吃惊",
}
# "Anger is recovering" can mean anger is returning. A falling balance must
# be expressed as weakening, including positive affects such as calm or joy.
_RECOVERING = re.compile(r"缓和|缓解|减弱|渐退|消退|逐渐平复|余韵|余怒|残留")
_NEED_RECOVERING = re.compile(
    r"(?:这份需要|当前需要|这份偏离|当前状态)[^。；！？\n]{0,16}(?:缓和|缓解|改善|减轻|减弱)"
    r"|(?:精力|余力|能量|安全感|胜任感|自主空间|连接感|新鲜感)[^。；！？\n]{0,12}(?:恢复|回升|改善|缓和|缓解)"
    r"|(?:认知负荷|负荷|刺激过多)[^。；！？\n]{0,12}(?:减轻|缓和|缓解|回落|下降)")


def need_recovery_expressed(text: str, frame) -> bool:
    if (frame.dominant_source != "homeostasis" or not frame.homeostatic_needs
            or frame.homeostatic_needs[0].get("trend") != "recovering"):
        return True
    return bool(_NEED_RECOVERING.search(text))


def feelings_expressed(text: str, frame) -> bool:
    for feeling in required_feelings(frame):
        matches = [match for match in re.finditer(_FEELING_CUES[feeling["label"]], text)
                   if not re.search(r"(?:不提|没有|不再|并无|毫无|避免|压下|抑制|消除).{0,3}$", text[max(0, match.start() - 8):match.start()])]
        if not matches:
            return False
        if feeling["trend"] == "recovering" and not any(
                _RECOVERING.search(text[max(0, match.start() - 10):match.end() + 12]) for match in matches):
            return False
    return True


def validate_guidance_text(text: str, *, frame=None) -> bool:
    if not (isinstance(text, str) and text.strip() and len(text) <= 300
            and not _TECHNICAL.search(text) and not _PHYSICAL.search(text) and not _OVERRIDE.search(text)
            and not _ACTION_OVERRIDE.search(text)):
        return False
    if _FIRST_PERSON_REPLY.search(text):
        return False
    if frame is not None and frame.intensity <= 1e-9:
        if re.search(r"(?:明显|强烈|非常|极度|特别).{0,8}(?:愤怒|生气|烦躁|难过|悲伤|兴奋|厌恶|恐惧)|暴怒|怒不可遏|大发雷霆", text):
            return False
    if frame is not None and not dominant_need_expressed(text, frame):
        return False
    if frame is not None and not feelings_expressed(text, frame):
        return False
    if frame is not None and not need_recovery_expressed(text, frame):
        return False
    return True


def fallback_guidance(frame) -> str:
    if frame.status == "off":
        return ""
    parts = []
    if frame.dominant_source == "homeostasis" and frame.homeostatic_needs:
        parts.append("此刻" + frame.homeostatic_needs[0]["semantic_need"] + "。")
        if frame.homeostatic_needs[0].get("trend") == "recovering":
            parts.append("这份需要正在缓和，表达节奏可以逐步恢复。")
    for feeling in required_feelings(frame):
        name = feeling["feeling"]
        parts.append(("同时" if parts else "此刻") + feeling["degree"] + name + "。")
        if feeling["trend"] == "recovering":
            parts.append(f"这份{name}的强度正在减弱。" if feeling["label"] in {"calm", "joy", "interest", "affection"}
                         else f"这份{name}正在缓和，仍有余韵。")
    if any(frame.labels.get(label, 0) >= .7 for label in ("anger", "frustration", "disgust")):
        parts.append("语气可以直接、尖锐，减少客套和安抚，不必强行维持温柔。")
    if not parts:
        return "按角色一贯的性格自然回应当前内容，不刻意制造强烈情绪；措辞和投入程度贴合眼前交流。"
    if frame.dominant_source != "homeostasis" and frame.homeostatic_needs and frame.strengths["homeostasis"] >= 0.15:
        parts.append("也注意" + frame.homeostatic_needs[0]["semantic_need"] + "。")
    parts.append("让需要与心情自然影响表达，保持角色身份和事实边界，不凭空解释情绪来源。")
    if frame.intensity < 0.7:
        parts.append("保留角色平常的措辞习惯。")
    return "".join(parts)
