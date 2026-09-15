"""Context Compiler: budgeted high-density ModelContext (01 §7 / 02 §阶段 7)."""

from __future__ import annotations

from typing import Any, Literal

from harness.config import Settings, get_settings
from harness.core.inference import (
    ContextBudget,
    ContextSegment,
    MemoryEvidenceContext,
    MemoryEvidenceEntry,
    ModelContext,
    ProvenanceEntry,
    SessionTurn,
    SituationState,
    StructuredInsight,
)
from harness.core.models import PersonaCore, ScoredMemory
from harness.homeostasis.models import HomeostaticInteroceptiveSummary
from harness.research.grounded_agency.manifest import GroundedAgencyBinding
from harness.research.grounded_agency.neutrality import (
    NEUTRALITY_RULES_DIGEST,
    NEUTRALITY_RULES_VERSION,
    NeutralityReport,
    NeutralityScanner,
)
from harness.research.manifest import ExperimentManifest


MEMORY_EMPTY_CONSTRAINT = (
    "no_fabricated_memory：记忆库中没有相关记录——禁止编造共同经历、"
    "引用不存在的往事或对话（如「你上次说过」「我们之前」）；回应当前内容，必要时只询问具体缺口。"
)

MEMORY_EVIDENCE_CONSTRAINT = (
    "历史引用须逐条有本轮入模记录支持；有记录甲不代表往事乙发生过。"
    "用户陈述不等于已核实事实，暂定或争议记忆不可当成定论。"
    "当前消息不是过去对话的证据；回应可核对的内容，必要时只询问具体缺口。"
)

PERSONA_EXPRESSION_DEFAULTS = (
    "性格特质与表达习惯描述通常状态；有依据的强烈情绪可以暂时突破轻柔、克制等表达习惯，"
    "情绪缓和后逐渐恢复平常表达。身份、事实、价值观和知识边界持续有效。"
)


def emotion_enabled(settings: Any) -> bool:
    return getattr(settings, "emotion_mode", "off") == "on"


def emotion_guidance_text(guidance: Any) -> str:
    if isinstance(guidance, str):
        return guidance.strip()
    if isinstance(guidance, dict):
        return str(guidance.get("text") or "").strip()
    return str(getattr(guidance, "text", "") or "").strip()


def render_emotion_guidance(guidance: Any = None, *, homeostasis: Any = None) -> str:
    """Render an already prepared brief; this boundary never calls a model."""
    summary = None
    if homeostasis:
        try:
            summary = HomeostaticInteroceptiveSummary.model_validate(homeostasis)
        except (TypeError, ValueError):
            pass
    if summary is not None and summary.salience == "critical":
        raise ValueError("critical homeostatic state must pause before model invocation")
    text = emotion_guidance_text(guidance)
    if not text:
        text = (render_homeostatic_interoception(summary) if summary is not None else
                "当前没有可靠的新情绪提示，沿用通常的表达习惯，不凭空制造强烈感受或原因。")
    return ("当前情绪演绎提示：" + text + "\n" + PERSONA_EXPRESSION_DEFAULTS
            + "只自然演绎，不复述提示、权重、内部指标或形成机制；不能把不明原因归咎于当前对象。")


def command_emotion_context(host: Any, payload: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """Separate the Runtime-owned reference before strict request parsing."""
    request = dict(payload)
    reference = request.pop("emotion_context_ref", None)
    request.pop("emotion_revision", None)
    return request, reference


def command_emotion_deadline(host: Any, command: Any, deadline: Any) -> Any:
    """A renewed RPC window cannot extend the frozen expression model budget."""
    deadline = min(command.deadline, deadline)
    settings = getattr(getattr(host, "chat", None), "settings", None)
    reference = command.payload.get("emotion_context_ref")
    if getattr(settings, "emotion_mode", "off") == "off" or reference is None:
        return deadline
    service = getattr(getattr(host, "adapter", None), "emotion_service", None)
    if service is None:
        service = getattr(getattr(getattr(host, "chat", None), "pipeline", None), "emotion_service", None)
    if service is not None:
        observation = service.existing_input(reference)
        if observation is not None and observation.deadline_at is not None:
            deadline = min(deadline, observation.deadline_at)
    return deadline


async def resolve_command_emotion(host: Any, command: Any) -> Any:
    """Resolve after request validation, within the original Runtime deadline."""
    import asyncio
    from datetime import datetime, timezone

    reference = command.payload.get("emotion_context_ref")
    settings = getattr(getattr(host, "chat", None), "settings", None)
    if getattr(settings, "emotion_mode", "off") == "off" or reference is None:
        return None
    adapter = getattr(host, "adapter", None)
    resolve = getattr(adapter, "emotion_guidance", None)
    service = getattr(adapter, "emotion_service", None)
    if service is None:
        service = getattr(getattr(getattr(host, "chat", None), "pipeline", None), "emotion_service", None)
    try:
        if callable(resolve):
            return await resolve(command)
        if service is not None:
            remaining = (command.deadline - datetime.now(timezone.utc)).total_seconds()
            return await asyncio.wait_for(asyncio.to_thread(service.guidance, reference), timeout=max(0, remaining))
    except (KeyError, ValueError, TypeError, TimeoutError):
        # Missing/stale context degrades to semantic needs, never raw numbers.
        return None
    return None

_HOMEOSTATIC_EMOTION_TEXT = {
    "strained": "内在感受偏紧绷",
    "lonely": "有些孤单，更需要真实的连接",
    "engaged": "有投入和靠近的意愿",
    "settled": "内在相对安稳",
    "attentive": "内在更警觉、谨慎",
}
_HOMEOSTATIC_NEED_TEXT = {
    ("energy_reserve", "low"): "需要降低消耗并恢复可用精力",
    ("energy_reserve", "high"): "有余力承担更充分的投入",
    ("cognitive_load", "low"): "需要适量参与以保持专注",
    ("cognitive_load", "high"): "需要降低负荷并简化当前任务",
    ("safety_predictability", "low"): "需要更多确定性、澄清与安全边界",
    ("safety_predictability", "high"): "可以容纳少量不确定性和变化",
    ("competence", "low"): "需要一个可验证的小进展来恢复把握",
    ("competence", "high"): "需要更有挑战、能发挥能力的目标",
    ("autonomy", "low"): "需要保留选择权并重新协商边界",
    ("autonomy", "high"): "可以主动承担更多选择和责任",
    ("social_connection", "low"): "需要低压力的连接与被理解",
    ("social_connection", "high"): "需要留出独处空间并降低社交密度",
    ("novelty", "low"): "需要一点轻量的新鲜感或探索",
    ("novelty", "high"): "需要减少新刺激并回到熟悉节奏",
}
_HOMEOSTATIC_TREND_TEXT = {
    "worsening": "，而且这种倾向正在增强",
    "steady": "，这种倾向暂时稳定",
    "recovering": "，不过这种倾向正在缓和",
}

_INTEROCEPTIVE_RESOURCE_TEXT = {
    "critical": "精力已经不足以继续消耗，需要立刻停下并等待恢复",
    "low": "精力偏低，需要节省消耗并把事情简化",
    "nominal": "精力处在可正常应对的范围",
    "high": "精力比较充足，可以保持投入",
    "full": "精力很充足，可以承担更充分的投入",
}


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 1) // 2)


def fit_text(text: str, budget: int) -> str:
    if budget <= 0 or not text:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


class ContextCompiler:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def validate_final_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        manifest: ExperimentManifest,
        binding: GroundedAgencyBinding | None = None,
    ) -> NeutralityReport | None:
        """Check the exact outbound payload after rendering and all prompt additions.

        Callers must send these messages unchanged after validation. GA-01 exposes
        this opt-in boundary; it does not install a new Runtime/model call route.
        ModelContext provenance and evaluator-only configuration are never scanned.
        """
        if binding is not None:
            binding.assert_matches(manifest)
        condition = manifest.grounded_agency
        if condition is None:
            return None
        if (
            condition.neutrality_rules_version != NEUTRALITY_RULES_VERSION
            or condition.neutrality_rules_digest != NEUTRALITY_RULES_DIGEST
        ):
            raise ValueError("manifest neutrality rules do not match the installed scanner")
        return NeutralityScanner().assert_neutral(messages, opaque=condition.is_opaque)

    def compile(
        self,
        situation: SituationState,
        *,
        insights: list[StructuredInsight] | None = None,
        session: list[SessionTurn] | None = None,
        additional_segments: list[ContextSegment] | None = None,
        homeostatic_interoception: HomeostaticInteroceptiveSummary | dict | None = None,
        emotion_guidance: Any = None,
        opaque_interoception: tuple | list | None = None,
        opaque_token_budget: int = 256,
        required_user_context: bool = False,
        required_additional_context: bool = False,
        has_additional_memory: bool = False,
        memory_evidence: MemoryEvidenceContext | None = None,
        prefix_messages: list[dict[str, str]] | None = None,
    ) -> ModelContext:
        insights = insights or []
        session = session or []
        additional_segments = additional_segments or []
        use_emotion = emotion_enabled(self.settings) and opaque_interoception is None
        if use_emotion:
            additional_segments = [item for item in additional_segments
                                   if item.kind not in {"emotion_guidance", "homeostatic_interoception", "psych", "relationship"}]
        total = self.settings.context_token_budget
        web_segments = [item for item in additional_segments if item.why == "verified_web_context"]
        if web_segments:
            # The web projection is bounded before compile. Preserve its full
            # serialized source records and the original user turn; URLs must
            # not disappear under the ordinary 120-token optional-segment cap.
            required_user_context = True
            total = max(total, sum(estimate_tokens(item.text) for item in additional_segments)
                        + estimate_tokens(situation.utterance) + 3000)
        remaining = total
        segments: list[ContextSegment] = []

        homeostatic: HomeostaticInteroceptiveSummary | None = None
        homeostatic_text = ""
        homeostatic_tokens = 0
        opaque_text = ""
        opaque_sources: list[str] = []
        opaque_tokens = 0
        if opaque_interoception:
            from harness.research.interoception.contracts import observation_hash
            from harness.research.interoception.renderer import render_opaque_interoception

            if homeostatic_interoception is not None or situation.interoception is not None:
                raise ValueError("opaque observations cannot be combined with semantic interoception")
            opaque_text = render_opaque_interoception(opaque_interoception, max_tokens=opaque_token_budget)
            opaque_sources = [observation_hash(item) for item in opaque_interoception]
            opaque_tokens = estimate_tokens(opaque_text)
            if opaque_tokens > total:
                raise ValueError("complete opaque interoception exceeds context token budget")
        if homeostatic_interoception is not None:
            homeostatic = HomeostaticInteroceptiveSummary.model_validate(
                homeostatic_interoception
            )
            if homeostatic.salience == "critical":
                raise ValueError("critical homeostatic state must pause before model invocation")
        if homeostatic is not None and not use_emotion:
            homeostatic_text = render_homeostatic_interoception(homeostatic)
            homeostatic_tokens = estimate_tokens(homeostatic_text)
            if not homeostatic_text or homeostatic_tokens > total:
                raise ValueError(
                    "complete homeostatic interoception exceeds context token budget"
                )

        persona = situation.snapshot.persona if situation.snapshot else None
        persona_text = _persona_anchor(persona)
        if use_emotion:
            homeostatic_text = render_emotion_guidance(emotion_guidance, homeostasis=homeostatic_interoception)
            homeostatic_tokens = estimate_tokens(homeostatic_text)
        remaining = _push(
            segments,
            kind="persona",
            text=persona_text,
            source_ids=[persona.persona_id] if persona else [],
            why="persona_core_anchor",
            remaining=remaining,
            preferred=min(400, max(0, remaining - homeostatic_tokens - opaque_tokens)),
        )

        if opaque_text:
            remaining = _push_required(
                segments,
                kind="opaque_interoception",
                text=opaque_text,
                source_ids=opaque_sources,
                why="model_visible_observation",
                remaining=remaining,
            )

        if use_emotion:
            reference = (emotion_guidance.get("context_id") if isinstance(emotion_guidance, dict)
                         else getattr(emotion_guidance, "context_id", None))
            remaining = _push_required(
                segments, kind="emotion_guidance", text=homeostatic_text,
                source_ids=[reference] if reference else [], why="prepared_emotion_guidance",
                remaining=remaining,
            )
        elif homeostatic is not None:
            remaining = _push_required(
                segments,
                kind="homeostatic_interoception",
                text=homeostatic_text,
                source_ids=homeostatic_interoception_source_ids(homeostatic),
                why=(
                    "homeostatic_interoception:"
                    f"{homeostatic.salience}:revision={homeostatic.snapshot_revision}"
                ),
                remaining=remaining,
            )

        constraint_lines = [ins.constraint for ins in insights if ins.constraint]
        # The old boolean remains accepted for callers, but cannot grant memory
        # authority. Only evidence bound by a trusted adapter can do that.
        empty_memory = not situation.relevant_memories and memory_evidence is None
        if memory_evidence is not None:
            constraint_lines.append(MEMORY_EVIDENCE_CONSTRAINT)
        elif empty_memory:
            constraint_lines.append(MEMORY_EMPTY_CONSTRAINT)
        if constraint_lines:
            remaining = _push(
                segments,
                kind="constraint",
                text="红线：" + "；".join(dict.fromkeys(constraint_lines)),
                source_ids=[ins.insight_id for ins in insights] + (["memory_guard"] if empty_memory else []),
                why=("subagent_constraint" if insights else
                     "memory_evidence_guard" if memory_evidence is not None else "memory_empty_guard"),
                remaining=remaining,
                preferred=min(80, remaining),
            )

        if situation.interoception is not None and not use_emotion:
            interoception = situation.interoception
            remaining = _push(
                segments,
                kind="interoception",
                text=_interoception_text(interoception),
                source_ids=list(interoception.evidence_refs),
                why="verified_internal_resource",
                remaining=remaining,
                preferred=min(80, remaining),
            )

        if required_user_context and situation.utterance:
            remaining = _push_required(
                segments, kind="user", text=situation.utterance,
                source_ids=[situation.trigger.ref], why="trigger_utterance", remaining=remaining,
            )
        else:
            remaining = _push(
                segments,
                kind="user",
                text=situation.utterance,
                source_ids=[situation.trigger.ref],
                why="trigger_utterance",
                remaining=remaining,
                preferred=min(200, remaining),
            )

        for additional in additional_segments:
            if (required_additional_context or additional.why == "verified_web_context") and additional.text:
                remaining = _push_required(
                    segments, kind=additional.kind, text=additional.text,
                    source_ids=list(additional.source_ids), why=additional.why or "runtime_context",
                    remaining=remaining,
                )
                continue
            remaining = _push(
                segments,
                kind=additional.kind,
                text=additional.text,
                source_ids=list(additional.source_ids),
                why=additional.why or "runtime_context",
                remaining=remaining,
                preferred=min(120, remaining),
            )

        psych_text = "" if use_emotion else _psych_text(situation)
        if psych_text:
            remaining = _push(
                segments,
                kind="psych",
                text=psych_text,
                source_ids=[situation.actor.persona_core_ref or "psych"],
                why="psych_short",
                remaining=remaining,
                preferred=min(150, remaining),
            )

        rel_text = "" if use_emotion else _relationship_text(situation)
        if rel_text:
            rel_id = situation.relationship.relation_id if situation.relationship else "rel"
            remaining = _push(
                segments,
                kind="relationship",
                text=rel_text,
                source_ids=[rel_id],
                why="relationship_state",
                remaining=remaining,
                preferred=min(150, remaining),
            )

        world_text = _world_text(situation)
        if world_text:
            remaining = _push(
                segments,
                kind="world",
                text=world_text,
                source_ids=["world"],
                why="world_state",
                remaining=remaining,
                preferred=min(80, remaining),
            )

        for insight in insights:
            remaining = _push(
                segments,
                kind="insight",
                text=_insight_text(insight, include_stance=not use_emotion),
                source_ids=[insight.insight_id],
                why="social_interpreter",
                remaining=remaining,
                preferred=min(200, remaining),
            )

        # The immediately preceding exchange is needed to resolve follow-ups.
        # Keep the newest turns before spending the remaining budget on recall.
        session_budget = min(400, remaining)
        visible_session = _fit_session(session, session_budget)
        session_text = _session_text(visible_session)
        if session_text:
            remaining = _push(
                segments,
                kind="session",
                text=session_text,
                source_ids=[t.event_id or "session" for t in visible_session],
                why="recent_dialogue_window",
                remaining=remaining,
                preferred=session_budget,
            )

        top_k = self.settings.compiler_top_memories
        for memory in situation.relevant_memories[:top_k]:
            remaining = _push(
                segments,
                kind="memory",
                text=_memory_text(memory),
                source_ids=[memory.ref],
                why=",".join(memory.why_retrieved) or "hybrid_retrieval",
                remaining=remaining,
                preferred=min(220, remaining),
            )

        admitted_evidence = None
        if memory_evidence is not None:
            # Compiler-selected retrieval is also available to the same critic,
            # but never includes a clipped record or widens its visibility.
            entries = list(memory_evidence.entries)
            for memory in situation.relevant_memories[:top_k]:
                record = memory.episodic or memory.semantic
                scope = getattr(record, "scope", None)
                if ((scope is not None and memory_evidence.audience not in scope.visibility)
                        or (scope is None and memory_evidence.audience != "local:main")):
                    continue
                full_text = _memory_text(memory)
                segment = next((item for item in segments if item.kind == "memory"
                                and item.source_ids == [memory.ref] and item.text == full_text), None)
                if segment is None:
                    continue
                entry = MemoryEvidenceEntry(
                    source_id=memory.ref, text=full_text, audience=memory_evidence.audience,
                    source_channel=scope.channel if scope else getattr(record, "channel", "chat"),
                    source_conversation_id=(scope.conversation_id if scope else getattr(record, "session_id", None)),
                    source_refs=list(getattr(record, "source_refs", []) or getattr(record, "evidence", [])),
                    status="retained_fact", kind="derived_memory",
                )
                entries.extend(MemoryEvidenceContext.bind(audience=memory_evidence.audience,
                                                         entries=[entry], segment=segment).entries)
            admitted_evidence = memory_evidence.model_copy(update={"entries": entries}).admitted(
                [*segments, *prefix_visible_segments(prefix_messages, memory_evidence)],
                trigger_ref=situation.trigger.ref)

        used = total - remaining
        per_kind: dict[str, int] = {}
        for seg in segments:
            per_kind[seg.kind] = per_kind.get(seg.kind, 0) + seg.token_estimate
        provenance = [
            ProvenanceEntry(segment_kind=seg.kind, source_ids=list(seg.source_ids), why=seg.why)
            for seg in segments
            if seg.text
        ]
        return ModelContext(
            segments=[seg for seg in segments if seg.text],
            budget=ContextBudget(total=total, used=used, per_kind=per_kind),
            provenance=provenance,
            trigger_text=situation.utterance,
            memory_evidence=admitted_evidence,
            prefix_messages=list(prefix_messages or []),
        )


def _push(
    segments: list[ContextSegment],
    *,
    kind: str,
    text: str,
    source_ids: list[str],
    why: str,
    remaining: int,
    preferred: int,
) -> int:
    budget = min(preferred, remaining)
    clipped = fit_text(text, budget)
    tokens = estimate_tokens(clipped)
    segments.append(
        ContextSegment(
            kind=kind,
            text=clipped,
            source_ids=source_ids,
            why=why,
            token_estimate=tokens,
        )
    )
    return remaining - tokens


def prefix_visible_segments(prefix_messages, memory_evidence) -> list[ContextSegment]:
    """Admit memory evidence against stable QQ prefix JSON without sending it twice."""
    if not prefix_messages or memory_evidence is None:
        return []
    visible = "\n".join(item.get("content") or "" for item in prefix_messages if item.get("role") == "user")
    if not visible:
        return []
    return [ContextSegment(
        kind="prefix_user",
        text=visible,
        source_ids=[entry.source_id for entry in memory_evidence.entries],
        why="qq_stable_prefix",
    )]


def _push_required(
    segments: list[ContextSegment],
    *,
    kind: str,
    text: str,
    source_ids: list[str],
    why: str,
    remaining: int,
) -> int:
    """Append an all-or-nothing segment that must reach the model intact."""

    tokens = estimate_tokens(text)
    if not text or tokens > remaining:
        raise ValueError(f"complete {kind} exceeds context token budget")
    segments.append(
        ContextSegment(
            kind=kind,
            text=text,
            source_ids=source_ids,
            why=why,
            token_estimate=tokens,
        )
    )
    return remaining - tokens


def _persona_anchor(persona: PersonaCore | None) -> str:
    if persona is None:
        return "人格锚点：未知角色。用短句、保持人物口吻。"
    ident = persona.identity
    parts = [f"人格锚点：{ident.name}。"]
    if ident.core_traits:
        parts.append(f"特质：{'、'.join(ident.core_traits)}。")
    if ident.values:
        parts.append(f"价值观：{'、'.join(ident.values)}。")
    if ident.speech_style:
        parts.append(f"表达：{'、'.join(ident.speech_style)}。")
    if ident.profile is not None:
        profile = ident.profile
        facts: list[str] = []
        if profile.aliases:
            facts.append(f"别名={'、'.join(profile.aliases)}")
        if profile.age is not None:
            facts.append(f"年龄={profile.age}岁")
        if profile.birthday:
            facts.append(f"生日={profile.birthday}")
        if profile.height_cm is not None:
            facts.append(f"身高={profile.height_cm:g}cm")
        if profile.hobbies:
            facts.append(f"爱好={'、'.join(profile.hobbies)}")
        if profile.signature_line:
            facts.append(f"代表句={profile.signature_line}")
        if facts:
            parts.append(f"角色资料：{'；'.join(facts)}。")
        if profile.appearance:
            parts.append(f"自我外观认知：{'；'.join(profile.appearance)}。")
        if profile.personal_items:
            parts.append(f"个人物品：{'；'.join(profile.personal_items)}。")
    if ident.knowledge_boundary:
        parts.append(f"知识边界：{'；'.join(ident.knowledge_boundary)}。")
    parts.append("只表演这个人，不要解释心理机制。")
    return "".join(parts)


def _psych_text(situation: SituationState) -> str:
    short = situation.actor.psych_state.get("short") if situation.actor.psych_state else None
    if not short:
        return ""
    return f"当前心理（short）：{short}"


def _relationship_text(situation: SituationState) -> str:
    rel = situation.relationship
    if rel is None:
        return ""
    return (
        f"关系状态 target={rel.target} closeness={rel.closeness:.2f} "
        f"trust={rel.trust:.2f} recent_tension={rel.recent_tension:.2f}"
    )


def _world_text(situation: SituationState) -> str:
    world = situation.world
    if world is None:
        return ""
    return (
        f"世界状态 time={world.time.isoformat()} channel={world.channel} "
        f"presence={world.user_presence}"
    )


def _interoception_text(state: object) -> str:
    from harness.core.inference import InteroceptiveState

    value = InteroceptiveState.model_validate(state)
    if value.trend < -1e-9:
        trend_text = "，而且精力还在下降"
    elif value.trend > 1e-9:
        trend_text = "，不过精力正在恢复"
    else:
        trend_text = "，目前相对稳定"
    return (
        "这是有可靠依据支持的精力感受，只作为自然表达与选择的背景："
        f"{_INTEROCEPTIVE_RESOURCE_TEXT[value.level]}{trend_text}。"
        "不要复述测量方式、内部标签、数值或时间估计。"
    )


def render_homeostatic_interoception(
    state: HomeostaticInteroceptiveSummary | dict,
    *,
    purpose: Literal["response", "assessment"] = "response",
) -> str:
    """Render only the semantic projection, never raw factor names or scores."""

    value = HomeostaticInteroceptiveSummary.model_validate(state)
    if value.salience == "critical":
        raise ValueError("critical homeostatic state must pause before model invocation")
    if value.salience == "nominal":
        guidance = (
            "内在状态相对平稳；它本身不足以成为主动打扰用户的理由。"
            if purpose == "assessment"
            else "内在状态相对平稳，只作为表达背景；无需主动制造或强调不适。"
        )
        return (
            "这是角色自身的内在感受线索，不是供对外解释的系统信息。"
            + guidance
            + "不要复述内部标签、指标、数值或形成机制。"
        )

    needs = [
        _HOMEOSTATIC_NEED_TEXT[(item.dimension, item.direction)]
        + _HOMEOSTATIC_TREND_TEXT[item.trend]
        for item in value.dominant_needs
    ]
    need_text = "；".join(dict.fromkeys(needs))
    emotion_text = ""
    if value.emotion_hint is not None:
        emotion_text = _HOMEOSTATIC_EMOTION_TEXT.get(value.emotion_hint.label, "")
        if value.salience == "marked" and value.emotion_hint.label == "settled":
            emotion_text = ""

    if purpose == "assessment":
        if value.salience == "noticeable":
            guidance = (
                "这些倾向已有所显现，可纳入是否开口的判断，但不要仅因轻微波动而说话。"
            )
        else:
            guidance = (
                "这些需要已经较明显，可把自然表达感受、需要或边界视为开口价值；"
                "仍应结合关系和时机，也允许保持沉默。"
            )
    elif value.salience == "noticeable":
        guidance = "让这些倾向自然影响语气和选择；可以含蓄，不必逐项明说。"
    else:
        guidance = (
            "把这些需要作为高优先级表达线索：自然表达感受、需要或边界；"
            "可以直接或含蓄，若情境不合适也不必强行说明。"
        )

    parts = ["这是角色自身的内在感受线索，不是供对外解释的系统信息。"]
    if need_text:
        parts.append(f"当前更需要：{need_text}。")
    if emotion_text:
        parts.append(f"感受底色：{emotion_text}。")
    parts.append(guidance)
    parts.append("不要复述内部标签、指标、数值或形成机制，也不要机械照抄这些措辞。")
    return "".join(parts)


def homeostatic_interoception_source_ids(
    state: HomeostaticInteroceptiveSummary | dict,
) -> list[str]:
    value = HomeostaticInteroceptiveSummary.model_validate(state)
    return [
        f"homeostasis:{value.snapshot_revision}",
        value.profile_ref,
        value.episode_signature,
    ]


def _insight_text(insight: StructuredInsight, *, include_stance: bool = True) -> str:
    intents = ", ".join(f"{item.intent}:{item.confidence:.2f}" for item in insight.possible_intents)
    parts = [f"社交判断：{intents}"]
    if include_stance and insight.recommended_stance:
        parts.append(f"建议姿态：{insight.recommended_stance}")
    if insight.constraint:
        parts.append(f"约束：{insight.constraint}（角色绝不能说出心理分析）")
    return " ".join(parts)


def _memory_text(memory: ScoredMemory) -> str:
    if memory.episodic is not None:
        ev = memory.episodic
        insight = ev.persona_conditioned_insight or ""
        return (
            f"记忆 {ev.event_id} relevance={memory.relevance:.2f} "
            f"事实：{ev.objective_fact}"
            + (f" 角色视角：{insight}" if insight else "")
        )
    if memory.semantic is not None:
        fact = memory.semantic
        return f"语义 {fact.fact_id} {fact.subject} {fact.predicate} {fact.value} c={fact.confidence:.2f}"
    return f"记忆 {memory.ref} relevance={memory.relevance:.2f}"


def _session_text(turns: list[SessionTurn]) -> str:
    if not turns:
        return ""
    lines = [f"{turn.speaker or turn.role}: {turn.text}" for turn in turns]
    return "近期对话：\n" + "\n".join(lines)


def _fit_session(turns: list[SessionTurn], budget: int) -> list[SessionTurn]:
    """Keep a chronological suffix, without cutting off the newest exchange."""
    kept: list[SessionTurn] = []
    for turn in reversed(turns):
        candidate = [turn, *kept]
        if estimate_tokens(_session_text(candidate)) <= budget:
            kept = candidate
            continue
        if not kept:
            # A single long turn may exceed the whole window. Preserve its end
            # and mark the omission rather than silently presenting old text.
            prefix = f"近期对话：\n{turn.speaker or turn.role}: …"
            available_chars = max(0, budget * 2 - len(prefix))
            if available_chars:
                kept = [turn.model_copy(update={"text": "…" + turn.text[-available_chars:]})]
        break
    return kept
