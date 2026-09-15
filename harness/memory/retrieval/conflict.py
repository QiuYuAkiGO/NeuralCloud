"""Conflict / reverse retrieval: find memories that contradict a current inference (01 §4.2)."""

from __future__ import annotations

from harness.config import Settings, get_settings
from harness.core.models import CurrentInference, RetrievalQuery, ScoredMemory
from harness.memory.retrieval.hybrid import HybridRetriever
from harness.memory.retrieval.planner import RetrievalPlanner


class ConflictRetriever:
    def __init__(self, hybrid: HybridRetriever, planner: RetrievalPlanner, settings: Settings | None = None) -> None:
        self.hybrid = hybrid
        self.planner = planner
        self.settings = settings or get_settings()

    def find(self, query: RetrievalQuery, inference: CurrentInference) -> list[ScoredMemory]:
        probe_text = _probe_text(inference)
        probe = query.model_copy(
            update={
                "text": probe_text,
                "current_inference": inference,
                "include_conflicts": False,
            }
        )
        plan = self.planner.plan(probe)
        if inference.polarity == "positive":
            plan = plan.model_copy(update={"emotional_relevance": "negative_counterevidence"})
        results = self.hybrid.search(probe, plan)
        conflicts: list[ScoredMemory] = []
        for item in results:
            if _is_conflict(item, inference):
                why = list(item.why_retrieved) + ["conflict_counterevidence"]
                conflicts.append(item.model_copy(update={"why_retrieved": why}))
        return conflicts


def _probe_text(inference: CurrentInference) -> str:
    about = inference.about
    value = inference.value
    if inference.polarity == "positive":
        return f"反证：与「{about}={value}」冲突的失望、不信任、拒绝或负面事件"
    return f"反证：与「{about}={value}」冲突的正面支持证据"


def _is_conflict(item: ScoredMemory, inference: CurrentInference) -> bool:
    if item.episodic is not None:
        valence = item.episodic.emotional_valence
        if inference.polarity == "positive" and valence < -0.05:
            return True
        if inference.polarity == "negative" and valence > 0.05:
            return True
        blob = item.episodic.summary + item.episodic.objective_fact
        if inference.polarity == "positive" and any(token in blob for token in ("失望", "不信任", "不管", "拒绝", "生气")):
            return True
    if item.semantic is not None:
        blob = f"{item.semantic.predicate} {item.semantic.value}"
        if inference.polarity == "positive" and any(token in blob for token in ("不喜欢", "不信任", "讨厌")):
            return True
    return False
