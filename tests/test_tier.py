from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from harness.config import Settings
from harness.core.models import (
    EpisodicEvent,
    InteractionEvent,
    MemoryTier,
    RetrievalPlan,
    RetrievalQuery,
    ScoreBreakdown,
    ScoredMemory,
)
from harness.llm.embeddings import MockEmbedding
from harness.memory.retrieval.signals import score_episodic
from harness.memory.service import build_inmemory_service
from harness.memory.store.inmemory import InMemoryStores
from harness.memory.tier import (
    TierClassifier,
    TierVerdict,
    apply_verdict,
    compute_uniqueness,
    merge_tier,
    register_hits,
    review_expiries,
    rule_verdict,
    stamp_retention,
    ttl_for_uniqueness,
)
from infra.trace.recorder import NullTracer
from tests.factories import vaelith

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)


def _event(
    text: str,
    *,
    event_id: str = "evt_tier",
    importance: float = 0.2,
    valence: float = 0.0,
    event_type: str | None = None,
    when: datetime | None = None,
    tier: MemoryTier = "short",
    uniqueness: float | None = None,
    expire_at: datetime | None = None,
) -> EpisodicEvent:
    moment = when or NOW
    return EpisodicEvent(
        event_id=event_id,
        time=moment,
        participants=["user", "character"],
        summary=text,
        importance=importance,
        emotional_valence=valence,
        evidence=[f"chat:{event_id}"],
        objective_fact=text,
        event_type=event_type,
        tier=tier,
        uniqueness=uniqueness,
        expire_at=expire_at,
        tier_updated_at=moment,
    )


def _msg(
    text: str,
    event_id: str,
    *,
    when: datetime | None = None,
    uniqueness: float | None = None,
) -> InteractionEvent:
    moment = when or NOW
    return InteractionEvent(
        event_id=event_id,
        timestamp=moment,
        speaker="user",
        raw_text=text,
        session_id="sess_tier",
        evidence=[f"chat:sess_tier:{event_id}"],
        uniqueness=uniqueness,
    )


class _StubTierLLM:
    def __init__(self, payload: dict[str, Any] | None = None, *, raises: bool = False) -> None:
        self.payload = payload if payload is not None else {}
        self.raises = raises
        self.calls = 0

    def complete_json(self, *, system: str, user: str, max_tokens: int | None = None) -> dict[str, Any]:
        self.calls += 1
        if self.raises:
            raise ValueError("boom")
        return dict(self.payload)


def test_new_event_defaults_to_short() -> None:
    event = _event("今天天气不错")
    assert event.tier == "short"
    assert event.hit_count == 0
    assert rule_verdict(event).tier == "short"


def test_uniqueness_ttl_curve() -> None:
    settings = Settings()
    assert ttl_for_uniqueness(0.0, settings) == timedelta(minutes=5)
    assert ttl_for_uniqueness(0.4, settings) == timedelta(minutes=30)
    assert ttl_for_uniqueness(1.0, settings) == timedelta(minutes=10080)
    mid = ttl_for_uniqueness(0.2, settings)
    assert timedelta(minutes=5) < mid < timedelta(minutes=30)


def test_greeting_has_low_uniqueness() -> None:
    assert compute_uniqueness(_event("嗯")) <= 0.1
    assert compute_uniqueness(_event("你好")) <= 0.1


def test_commitment_has_high_uniqueness() -> None:
    event = _event("你以后不用管我了", importance=0.86, valence=-0.55, event_type="commitment_or_statement")
    assert compute_uniqueness(event) >= 0.6


def test_stamp_retention_sets_expire_at() -> None:
    settings = Settings()
    stamped = stamp_retention(_event("嗯"), now=NOW, settings=settings)
    assert stamped.uniqueness is not None
    assert stamped.expire_at == NOW + ttl_for_uniqueness(stamped.uniqueness, settings)


def test_stamp_keeps_explicit_uniqueness() -> None:
    settings = Settings()
    stamped = stamp_retention(_event("随便说说", uniqueness=0.0), now=NOW, settings=settings)
    assert stamped.uniqueness == 0.0
    assert stamped.expire_at == NOW + timedelta(minutes=5)


def test_rule_verdict_promotes_commitment() -> None:
    event = _event("你以后不用管我了", importance=0.86, valence=-0.55, event_type="commitment_or_statement")
    assert rule_verdict(event).tier == "mid"


def test_rule_verdict_promotes_extreme_to_long() -> None:
    event = _event("你永远不要再背叛我", importance=0.95, valence=-0.8)
    assert rule_verdict(event).tier == "long"


def test_rule_verdict_never_forgets_before_expiry() -> None:
    event = _event("嗯")
    assert rule_verdict(event).tier == "short"


def test_rule_verdict_forgets_default_on_expiry() -> None:
    event = _event("嗯")
    assert rule_verdict(event, on_expiry=True).tier == "forgotten"


def test_merge_tier_promotes_only() -> None:
    assert merge_tier("short", "mid") == "mid"
    assert merge_tier("mid", "short") == "mid"
    assert merge_tier("long", "forgotten") == "long"
    assert merge_tier("short", "forgotten") == "forgotten"
    assert merge_tier("short", "long") == "long"


def test_apply_verdict_records_reason() -> None:
    event = _event("普通聊天")
    updated = apply_verdict(event, TierVerdict(tier="mid", reason="llm:salient"), now=NOW)
    assert updated.tier == "mid"
    assert "llm:salient" in updated.tier_reasons
    assert updated.tier_updated_at == NOW
    assert updated.expire_at is None


def test_classifier_uses_llm_when_not_mock() -> None:
    llm = _StubTierLLM(
        {
            "impression": 0.92,
            "tier": "long",
            "importance": 0.88,
            "emotional_valence": -0.4,
            "reason": "turning_point",
        }
    )
    verdict = TierClassifier(llm).classify(_event("那次争吵改变了一切"), persona=None)
    assert llm.calls == 1
    assert verdict.tier == "long"
    assert verdict.importance == 0.88


def test_classifier_expiry_coerces_short_to_forgotten() -> None:
    llm = _StubTierLLM({"impression": 0.2, "tier": "short", "reason": "meh"})
    verdict = TierClassifier(llm).classify(_event("随便"), persona=None, on_expiry=True)
    assert verdict.tier == "forgotten"


def test_classifier_coerces_forget_alias() -> None:
    llm = _StubTierLLM({"impression": 0.05, "tier": "forget", "reason": "noise"})
    assert TierClassifier(llm).classify(_event("嗯嗯"), persona=None).tier == "forgotten"


def test_classifier_falls_back_on_error() -> None:
    llm = _StubTierLLM(raises=True)
    verdict = TierClassifier(llm).classify(_event("今天天气不错"), persona=None)
    assert verdict.tier == "short"
    assert verdict.reason.startswith("rule:")


def test_record_event_stays_short_until_expiry(service) -> None:
    written = service.record_event(
        _msg("你以后不用管我了", "evt_04010"),
        importance=0.86,
        emotional_valence=-0.55,
        event_type="commitment_or_statement",
        curate=False,
        now=NOW,
    )
    assert written.tier == "short"
    assert written.uniqueness is not None and written.uniqueness > 0.4
    assert written.expire_at is not None
    service.wait_background()
    stored = service.stores.episodic.get("evt_04010")
    assert stored is not None
    assert stored.tier == "short"


def test_expiry_review_promotes_commitment(service) -> None:
    service.record_event(
        _msg("你以后不用管我了", "evt_04010"),
        importance=0.86,
        emotional_valence=-0.55,
        event_type="commitment_or_statement",
        uniqueness=0.0,
        curate=False,
        now=NOW,
    )
    service.wait_background()
    later = NOW + timedelta(minutes=6)
    service.record_event(_msg("你好", "evt_hello"), uniqueness=1.0, curate=False, now=later)
    service.wait_background()
    stored = service.stores.episodic.get("evt_04010")
    assert stored is not None
    assert stored.tier == "mid"
    assert any(row["op"] == "TIER" for row in service.stores.audit.list_for("episodic", "evt_04010"))


def test_llm_classify_and_forget_via_service(settings, tmp_path) -> None:
    settings.trace_dir = tmp_path
    llm = _StubTierLLM({"impression": 0.02, "tier": "forgotten", "reason": "phatic"})
    svc = build_inmemory_service(
        settings=settings,
        llm=llm,
        embeddings=MockEmbedding(dim=settings.embedding_dim),
        tracer=NullTracer(),
    )
    try:
        svc.bootstrap_persona(vaelith())
        svc.record_event(
            _msg("嗯", "evt_noise"),
            importance=0.1,
            uniqueness=0.0,
            curate=False,
            generate_insight=False,
            now=NOW,
        )
        svc.wait_background()
        assert svc.stores.episodic.get("evt_noise").tier == "short"
        assert llm.calls == 0
        svc.record_event(
            _msg("嗨", "evt_hi"),
            uniqueness=1.0,
            curate=False,
            generate_insight=False,
            now=NOW + timedelta(minutes=6),
        )
        svc.wait_background()
        stored = svc.stores.episodic.get("evt_noise")
        assert stored is not None
        assert stored.tier == "forgotten"
        assert llm.calls == 1
        hits = svc.query(RetrievalQuery(text="嗯", now=NOW + timedelta(minutes=6), entities=["user", "character"]))
        assert all(item.ref != "evt_noise" for item in hits)
    finally:
        svc.close()


def test_llm_promotes_to_long_via_service(settings, tmp_path) -> None:
    settings.trace_dir = tmp_path
    llm = _StubTierLLM({"impression": 0.95, "tier": "long", "importance": 0.9, "reason": "core"})
    svc = build_inmemory_service(
        settings=settings,
        llm=llm,
        embeddings=MockEmbedding(dim=settings.embedding_dim),
        tracer=NullTracer(),
    )
    try:
        svc.bootstrap_persona(vaelith())
        svc.record_event(
            _msg("那是我们关系真正开始的那天", "evt_origin"),
            importance=0.4,
            uniqueness=0.0,
            curate=False,
            generate_insight=False,
            now=NOW,
        )
        svc.wait_background()
        svc.record_event(
            _msg("嗨", "evt_hi"),
            uniqueness=1.0,
            curate=False,
            generate_insight=False,
            now=NOW + timedelta(minutes=6),
        )
        svc.wait_background()
        stored = svc.stores.episodic.get("evt_origin")
        assert stored is not None
        assert stored.tier == "long"
        assert stored.importance == 0.9
    finally:
        svc.close()


def test_register_hits_promotes_short_to_mid_then_long() -> None:
    stores = InMemoryStores()
    settings = Settings(tier_promotion_hits_mid=3, tier_promotion_hits_long=8)
    event = _event("今天天气不错", event_id="evt_hits", uniqueness=0.2)
    stores.episodic.insert(event)
    hit = ScoredMemory(
        memory_type="episodic",
        ref="evt_hits",
        relevance=0.8,
        score_breakdown=ScoreBreakdown(total=0.8),
        episodic=event,
    )
    weak = ScoredMemory(
        memory_type="episodic",
        ref="evt_hits",
        relevance=0.1,
        score_breakdown=ScoreBreakdown(total=0.1),
        episodic=event,
    )
    register_hits(stores, [weak], settings, now=NOW)
    assert stores.episodic.get("evt_hits").hit_count == 0

    for i in range(3):
        register_hits(stores, [hit], settings, now=NOW + timedelta(minutes=i))
    stored = stores.episodic.get("evt_hits")
    assert stored.hit_count == 3
    assert stored.tier == "mid"
    assert "hits:short_to_mid" in stored.tier_reasons
    assert stored.expire_at is None

    for i in range(5):
        register_hits(stores, [hit], settings, now=NOW + timedelta(minutes=10 + i))
    stored = stores.episodic.get("evt_hits")
    assert stored.hit_count == 8
    assert stored.tier == "long"
    assert "hits:mid_to_long" in stored.tier_reasons


def test_hits_extend_expire_at() -> None:
    stores = InMemoryStores()
    settings = Settings()
    event = stamp_retention(_event("今天天气不错", event_id="evt_hits", uniqueness=0.0), now=NOW, settings=settings)
    stores.episodic.insert(event)
    original_expiry = event.expire_at
    hit = ScoredMemory(
        memory_type="episodic",
        ref="evt_hits",
        relevance=0.8,
        score_breakdown=ScoreBreakdown(total=0.8),
        episodic=event,
    )
    later = NOW + timedelta(minutes=2)
    register_hits(stores, [hit], settings, now=later)
    stored = stores.episodic.get("evt_hits")
    assert stored.tier == "short"
    assert stored.expire_at == later + timedelta(minutes=5)
    assert stored.expire_at != original_expiry


def test_query_hits_promote_through_service(service) -> None:
    service.record_event(
        _msg("今天天气不错", "evt_weather"),
        importance=0.2,
        uniqueness=1.0,
        curate=False,
        now=NOW,
    )
    service.wait_background()
    assert service.stores.episodic.get("evt_weather").tier == "short"
    query = RetrievalQuery(text="今天天气不错", now=NOW, entities=["user", "character"])
    for _ in range(3):
        hits = service.query(query)
        assert hits
        service.wait_background()
    stored = service.stores.episodic.get("evt_weather")
    assert stored.hit_count >= 3
    assert stored.tier == "mid"


def test_review_expiries_forgets_unused_short() -> None:
    stores = InMemoryStores()
    settings = Settings()
    classifier = TierClassifier(_StubTierLLM({"tier": "forgotten", "reason": "stale"}))
    stale = _event("买了瓶水", event_id="evt_stale", uniqueness=0.0, expire_at=NOW - timedelta(minutes=1))
    stores.episodic.insert(stale)
    reviewed = review_expiries(stores, settings, classifier, None, now=NOW)
    assert reviewed == [("evt_stale", "forgotten")]
    assert stores.episodic.get("evt_stale").tier == "forgotten"


def test_not_yet_expired_is_not_reviewed() -> None:
    stores = InMemoryStores()
    settings = Settings()
    classifier = TierClassifier(_StubTierLLM({"tier": "forgotten"}))
    fresh = stamp_retention(_event("买了瓶水", event_id="evt_fresh", uniqueness=0.0), now=NOW, settings=settings)
    stores.episodic.insert(fresh)
    assert review_expiries(stores, settings, classifier, None, now=NOW) == []
    assert stores.episodic.get("evt_fresh").tier == "short"


def test_record_event_reviews_expired_short(service) -> None:
    stale = _event("买了瓶水", event_id="evt_stale", uniqueness=0.0, expire_at=NOW - timedelta(minutes=1))
    service.stores.episodic.insert(stale)
    service.record_event(_msg("你好", "evt_hello"), uniqueness=1.0, importance=0.1, curate=False, now=NOW)
    service.wait_background()
    assert service.stores.episodic.get("evt_stale").tier == "forgotten"


def test_forgotten_excluded_from_search_pool(service) -> None:
    event = _event("一段被遗忘的闲聊", event_id="evt_gone")
    event = event.model_copy(update={"tier": "forgotten"})
    service.stores.episodic.insert(event)
    pool = service.stores.episodic.search_pool(
        query_embedding=None,
        since=None,
        until=None,
        channels=None,
        entities=None,
        limit=10,
    )
    assert all(item.event_id != "evt_gone" for item in pool)
    hits = service.query(RetrievalQuery(text="一段被遗忘的闲聊", now=NOW, entities=["user", "character"]))
    assert all(item.ref != "evt_gone" for item in hits)


def test_long_tier_recency_floor() -> None:
    old = _event("核心承诺", event_id="evt_old_long", when=NOW - timedelta(days=400), importance=0.9, tier="long")
    query = RetrievalQuery(text="核心承诺", now=NOW, entities=["user", "character"])
    plan = RetrievalPlan(entities=["user", "character"])
    settings = Settings(long_term_recency_floor=0.4, recency_half_life_hours=24.0)
    embedding = MockEmbedding(dim=settings.embedding_dim).embed_one("核心承诺")
    breakdown, why = score_episodic(
        old,
        query=query,
        plan=plan,
        query_embedding=embedding,
        settings=settings,
    )
    assert breakdown.recency == 0.4
    assert "tier:long" in why


def test_mid_tier_slower_recency_decay() -> None:
    age = NOW - timedelta(hours=48)
    event = _event("中期记忆", event_id="evt_mid", when=age, tier="mid")
    short = event.model_copy(update={"tier": "short"})
    query = RetrievalQuery(text="中期记忆", now=NOW)
    plan = RetrievalPlan()
    settings = Settings(recency_half_life_hours=24.0, mid_term_half_life_factor=7.0)
    embedding = MockEmbedding(dim=settings.embedding_dim).embed_one("中期记忆")
    mid_score, _ = score_episodic(event, query=query, plan=plan, query_embedding=embedding, settings=settings)
    short_score, _ = score_episodic(short, query=query, plan=plan, query_embedding=embedding, settings=settings)
    assert mid_score.recency > short_score.recency


def test_query_with_conflicts_does_not_count_hits(service) -> None:
    from harness.core.models import CurrentInference

    service.record_event(
        _msg("今天天气不错", "evt_weather2"),
        importance=0.2,
        uniqueness=1.0,
        curate=False,
        now=NOW,
    )
    service.wait_background()
    service.query_with_conflicts(
        RetrievalQuery(
            text="今天天气不错",
            now=NOW,
            current_inference=CurrentInference(about="mood", value="fine", polarity="positive"),
        )
    )
    service.wait_background()
    assert service.stores.episodic.get("evt_weather2").hit_count == 0
