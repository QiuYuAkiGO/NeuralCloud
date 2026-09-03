"""Context Compiler: budgeted high-density ModelContext (01 §7 / 02 §阶段 7)."""

from __future__ import annotations

from harness.config import Settings, get_settings
from harness.core.inference import (
    ContextBudget,
    ContextSegment,
    ModelContext,
    ProvenanceEntry,
    SessionTurn,
    SituationState,
    StructuredInsight,
)
from harness.core.models import PersonaCore, ScoredMemory


MEMORY_EMPTY_CONSTRAINT = (
    "no_fabricated_memory：记忆库中没有相关记录——禁止编造共同经历、"
    "引用不存在的往事或对话（如「你上次说过」「我们之前」）；不记得就坦率回应"
)


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

    def compile(
        self,
        situation: SituationState,
        *,
        insights: list[StructuredInsight] | None = None,
        session: list[SessionTurn] | None = None,
        additional_segments: list[ContextSegment] | None = None,
    ) -> ModelContext:
        insights = insights or []
        session = session or []
        additional_segments = additional_segments or []
        total = self.settings.context_token_budget
        remaining = total
        segments: list[ContextSegment] = []

        persona = situation.snapshot.persona if situation.snapshot else None
        persona_text = _persona_anchor(persona)
        remaining = _push(
            segments,
            kind="persona",
            text=persona_text,
            source_ids=[persona.persona_id] if persona else [],
            why="persona_core_anchor",
            remaining=remaining,
            preferred=min(400, remaining),
        )

        constraint_lines = [ins.constraint for ins in insights if ins.constraint]
        empty_memory = not situation.relevant_memories
        if empty_memory:
            constraint_lines.append(MEMORY_EMPTY_CONSTRAINT)
        if constraint_lines:
            remaining = _push(
                segments,
                kind="constraint",
                text="红线：" + "；".join(dict.fromkeys(constraint_lines)),
                source_ids=[ins.insight_id for ins in insights] + (["memory_guard"] if empty_memory else []),
                why="subagent_constraint" if insights else "memory_empty_guard",
                remaining=remaining,
                preferred=min(80, remaining),
            )

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
            remaining = _push(
                segments,
                kind=additional.kind,
                text=additional.text,
                source_ids=list(additional.source_ids),
                why=additional.why or "runtime_context",
                remaining=remaining,
                preferred=min(120, remaining),
            )

        psych_text = _psych_text(situation)
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

        rel_text = _relationship_text(situation)
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
                text=_insight_text(insight),
                source_ids=[insight.insight_id],
                why="social_interpreter",
                remaining=remaining,
                preferred=min(200, remaining),
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

        session_text = _session_text(session)
        if session_text:
            remaining = _push(
                segments,
                kind="session",
                text=session_text,
                source_ids=[t.event_id or "session" for t in session],
                why="recent_dialogue_window",
                remaining=remaining,
                preferred=min(400, remaining),
            )

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


def _persona_anchor(persona: PersonaCore | None) -> str:
    if persona is None:
        return "人格锚点：未知角色。用短句、保持人物口吻。"
    ident = persona.identity
    return (
        f"人格锚点：{ident.name}。"
        f"特质：{'、'.join(ident.core_traits)}。"
        f"价值观：{'、'.join(ident.values)}。"
        f"表达：{'、'.join(ident.speech_style)}。"
        "只表演这个人，不要解释心理机制。"
    )


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


def _insight_text(insight: StructuredInsight) -> str:
    intents = ", ".join(f"{item.intent}:{item.confidence:.2f}" for item in insight.possible_intents)
    parts = [f"社交判断：{intents}"]
    if insight.recommended_stance:
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
