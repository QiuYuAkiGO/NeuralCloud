"""Hybrid retrieval: candidate pool + weighted six-signal rerank."""

from __future__ import annotations

from harness.config import Settings, get_settings
from harness.core.models import RetrievalPlan, RetrievalQuery, ScoredMemory
from harness.llm.embeddings import EmbeddingClient
from harness.memory.retrieval.signals import score_episodic, score_semantic, time_window
from harness.memory.store.protocols import MemoryStores


class HybridRetriever:
    def __init__(
        self,
        stores: MemoryStores,
        embeddings: EmbeddingClient,
        settings: Settings | None = None,
    ) -> None:
        self.stores = stores
        self.embeddings = embeddings
        self.settings = settings or get_settings()

    def search(self, query: RetrievalQuery, plan: RetrievalPlan) -> list[ScoredMemory]:
        query_embedding = self.embeddings.embed_one(query.text)
        since, until = time_window(plan, query.now)
        pool_size = self.settings.retrieval_pool_size
        events = self.stores.episodic.search_pool(
            query_embedding=query_embedding,
            since=since,
            until=until,
            channels=plan.channels or None,
            entities=None,
            limit=pool_size,
        )
        time_fallback = False
        if not events and (since is not None or until is not None):
            # user-stated dates are often wrong; a scoped miss must not empty the pool
            events = self.stores.episodic.search_pool(
                query_embedding=query_embedding,
                since=None,
                until=None,
                channels=plan.channels or None,
                entities=None,
                limit=pool_size,
            )
            time_fallback = True
        facts = self.stores.semantic.search_pool(
            query_embedding=query_embedding,
            subject=None,
            limit=max(8, pool_size // 4),
        )
        scored: list[ScoredMemory] = []
        for event in events:
            breakdown, why = score_episodic(
                event,
                query=query,
                plan=plan,
                query_embedding=query_embedding,
                settings=self.settings,
            )
            if time_fallback:
                why = [*why, "time:fallback"]
            scored.append(
                ScoredMemory(
                    memory_type="episodic",
                    ref=event.event_id,
                    relevance=breakdown.total,
                    why_retrieved=why,
                    score_breakdown=breakdown,
                    episodic=event.model_copy(update={"embedding": None}),
                )
            )
        for fact in facts:
            breakdown, why = score_semantic(
                fact,
                query=query,
                plan=plan,
                query_embedding=query_embedding,
                settings=self.settings,
            )
            scored.append(
                ScoredMemory(
                    memory_type="semantic",
                    ref=fact.fact_id,
                    relevance=breakdown.total,
                    why_retrieved=why,
                    score_breakdown=breakdown,
                    semantic=fact.model_copy(update={"embedding": None}),
                )
            )
        scored.sort(key=lambda item: item.relevance, reverse=True)
        top_k = query.top_k or self.settings.retrieval_top_k
        return scored[:top_k]
