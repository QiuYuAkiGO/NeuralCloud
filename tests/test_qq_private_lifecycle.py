"""Private QQ uses the same receipt-owned lifecycle without widening visibility."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from gui.presets import CharacterPreset, hydrate_memory, snapshot_memory
from harness.config import Settings
from harness.core.models import (
    CuratorOp, CuratorOutput, EpisodicEvent, MemoryScope, RetrievalPlan,
    RetrievalQuery, ScoreBreakdown, ScoredMemory, SemanticFact,
)
from harness.memory.service import build_inmemory_service
from harness.memory.store.postgres import PostgresEpisodicStore, PostgresSemanticStore
from harness.memory.tier import (
    TierVerdict, apply_verdict, maintain_qq_events, qq_event_strength, reinforce_qq_event,
)


NOW = datetime(2026, 9, 14, tzinfo=UTC)
AUDIENCE = "qq:private:2015188508:2285027817"
OTHER_PEER = "qq:private:2015188508:2285027818"
OTHER_SELF = "qq:private:2015188509:2285027817"


def private_scope(audience=AUDIENCE, **changes):
    return MemoryScope(channel="qq_private", conversation_id=audience,
                       visibility=["local:main", audience], **changes)


def event(identifier="private-event", *, tier="short", scope=None, **changes):
    values = dict(event_id=identifier, time=NOW, participants=["qq:member:2285027817"],
        summary="私聊中耐心解释一道题", objective_fact="对方向 Misty 解释解题步骤", importance=0.8,
        emotional_valence=0.1, evidence=[identifier], source_refs=[identifier],
        channel="qq_private", tier=tier, scope=scope or private_scope(),
        uniqueness=0.2, strength_updated_at=NOW, tier_updated_at=NOW)
    values.update(changes)
    return EpisodicEvent(**values)


@pytest.mark.parametrize("tier,days,next_tier", [("mid", 14, "short"), ("long", 90, "mid")])
def test_private_memory_decays_and_demotes_without_erasing_evidence(tier, days, next_tier):
    row = event(tier=tier)
    assert qq_event_strength(row, Settings(), now=NOW + timedelta(days=days)) == pytest.approx(0.5)
    reviewed = maintain_qq_events([row], Settings(), now=NOW + timedelta(days=days * 3))[0]
    assert reviewed.tier == next_tier and reviewed.strength == 0.5 and reviewed.decay_demoted
    assert reviewed.evidence == row.evidence and reviewed.scope == row.scope
    assert maintain_qq_events([reviewed], Settings(), now=reviewed.strength_updated_at)[0] is reviewed


def test_private_short_expiry_and_no_promotion_bounce():
    expiry = NOW + timedelta(minutes=10)
    row = event(expire_at=expiry)
    assert maintain_qq_events([row], Settings(), now=expiry - timedelta(seconds=1))[0] is row
    assert maintain_qq_events([row], Settings(), now=expiry)[0].tier == "mid"
    demoted = maintain_qq_events([event(tier="mid")], Settings(), now=NOW + timedelta(days=42))[0]
    assert demoted.tier == "short"
    assert apply_verdict(demoted, TierVerdict(tier="long"), now=demoted.tier_updated_at).tier == "short"
    assert maintain_qq_events([demoted], Settings(), now=demoted.expire_at)[0].tier == "forgotten"


def test_private_adoption_and_contrast_use_existing_strength_semantics():
    row = event(tier="mid")
    adopted = reinforce_qq_event(row, now=NOW + timedelta(days=14))
    assert adopted.strength == pytest.approx(0.65) and adopted.hit_count == 1
    contrast = reinforce_qq_event(row, now=NOW, adopted=False)
    assert contrast.strength == 1.15 and contrast.hit_count == 0 and contrast.last_hit_at is None
    assert reinforce_qq_event(event(hit_count=2), now=NOW).tier == "mid"
    demoted = event(hit_count=2, decay_demoted=True)
    assert reinforce_qq_event(demoted, now=NOW).tier == "short"
    forgotten = event(tier="forgotten")
    assert reinforce_qq_event(forgotten, now=NOW) is forgotten
    with pytest.raises(ValueError, match="backwards"):
        reinforce_qq_event(row, now=NOW - timedelta(seconds=1))


def test_private_service_query_and_async_review_do_not_mutate_worker_owned_memories(service):
    row = event(expire_at=NOW - timedelta(seconds=1), hit_count=2)
    service.stores.episodic.insert(row)
    hit = ScoredMemory(memory_type="episodic", ref=row.event_id, relevance=1,
                       score_breakdown=ScoreBreakdown())
    service._after_query([hit], NOW)
    assert service.stores.episodic.get(row.event_id) is row
    found = service.query(RetrievalQuery(text="解释解题步骤", now=NOW, trusted_audience=AUDIENCE))
    assert row.event_id in {item.ref for item in found}
    assert service.drain_maintenance()
    assert service.stores.episodic.get(row.event_id) is row


@pytest.mark.parametrize("audience", ["qq:group:2285027817", OTHER_PEER, OTHER_SELF, "local:other"])
def test_private_visibility_rejects_extra_recipients_in_models_and_mutable_store_rows(service, audience):
    with pytest.raises(ValidationError, match="outside"):
        MemoryScope(channel="qq_private", conversation_id=AUDIENCE,
                    visibility=["local:main", AUDIENCE, audience])
    # Pydantic model_copy intentionally bypasses validation; enforce the same
    # bound identity before a corrupt row enters the retrieval candidate pool.
    corrupted = private_scope().model_copy(update={"visibility": [audience, AUDIENCE]})
    service.stores.episodic.insert(event().model_copy(update={"scope": corrupted}))
    assert not service.stores.episodic.search_pool(query_embedding=None, since=None, until=None,
        channels=None, entities=None, limit=10, trusted_audience=audience)


@pytest.mark.parametrize("conversation", ["2285027817", "private:2015188508:2285027817",
    "qq:private:2015188508:2015188508", "qq:private:0:2285027817"])
def test_private_scope_requires_canonical_account_and_peer(conversation):
    with pytest.raises(ValidationError, match="exact self and peer"):
        MemoryScope(channel="qq_private", conversation_id=conversation)


def test_unbound_legacy_private_records_remain_local_and_do_not_enter_qq_lifecycle(service):
    boundary = MemoryScope(channel="qq_private")
    row = event(scope=boundary, tier="mid")
    service.stores.episodic.insert(row)
    assert maintain_qq_events([row], Settings(), now=NOW + timedelta(days=1000))[0] is row
    assert reinforce_qq_event(row, now=NOW) is row
    kwargs = dict(query_embedding=None, since=None, until=None, channels=None, entities=None, limit=10)
    assert service.stores.episodic.search_pool(**kwargs) == [row]
    assert service.stores.episodic.search_pool(**kwargs, trusted_audience=AUDIENCE) == []
    with pytest.raises(ValidationError, match="outside"):
        MemoryScope(channel="qq_private", visibility=[AUDIENCE])


def test_private_scope_filters_before_limit_and_stays_bound_through_fallback(service):
    for index, boundary in enumerate((private_scope(OTHER_PEER), private_scope(OTHER_SELF),
        MemoryScope(channel="qq_group", conversation_id="qq:group:100", visibility=["qq:group:100"]),
        MemoryScope(channel="chat"))):
        service.stores.episodic.insert(event(str(index), scope=boundary, embedding=[1, 0]))
    own = event(embedding=[0, 1])
    service.stores.episodic.insert(own)
    pool = service.stores.episodic.search_pool(query_embedding=[1, 0], since=None, until=None,
        channels=None, entities=None, limit=1, trusted_audience=AUDIENCE)
    assert pool == [own]
    found = service.hybrid.search(RetrievalQuery(text="解释", now=NOW, trusted_audience=AUDIENCE),
        RetrievalPlan(channels=["missing"], time_scope="yesterday"), query_embedding=[1, 0])
    assert {row.ref for row in found} == {own.event_id}
    for audience in ("qq:group:100", OTHER_PEER, OTHER_SELF):
        assert own.event_id not in {row.event_id for row in service.stores.episodic.search_pool(
            query_embedding=None, since=None, until=None, channels=None, entities=None,
            limit=10, trusted_audience=audience)}


def test_private_equal_claims_never_merge_other_peers_accounts_groups_or_local(service):
    addition = CuratorOutput(ops=[CuratorOp(op="ADD", type="candidate", subject="qq:member:2285027817",
        predicate="helps", value="解释题目", claim="愿意解释题目", confidence=0.15)])
    boundaries = [private_scope(), private_scope(OTHER_PEER), private_scope(OTHER_SELF),
        MemoryScope(channel="qq_group", conversation_id="qq:group:100"), MemoryScope(channel="chat")]
    for index, boundary in enumerate(boundaries):
        service.pipeline.apply(addition, event=event(str(index), scope=boundary), now=NOW)
    assert len(service.stores.candidates.list_open()) == len(boundaries)
    service.pipeline.apply(addition, event=event("another", scope=private_scope()), now=NOW)
    rows = service.stores.candidates.list_open()
    assert len(rows) == len(boundaries)
    assert next(row for row in rows if row.scope == private_scope()).evidence == ["0", "another"]


def test_private_preset_roundtrip_keeps_provenance_and_owner_readability(service):
    row = event(tier="mid", strength=0.4, decay_demoted=True, processing_version="qq-memory-v1")
    service.stores.episodic.insert(row)
    preset = snapshot_memory(service, preset_id="misty", session_id="test", transcript=[])
    restored = build_inmemory_service(settings=Settings(embedding_dim=4))
    try:
        hydrate_memory(restored, CharacterPreset.model_validate_json(preset.model_dump_json()))
        actual = restored.stores.episodic.get(row.event_id)
        assert actual.model_dump(exclude={"embedding"}) == row.model_dump(exclude={"embedding"})
        kwargs = dict(query_embedding=None, since=None, until=None, channels=None, entities=None, limit=10)
        assert restored.stores.episodic.search_pool(**kwargs) == [actual]
        assert restored.stores.episodic.search_pool(**kwargs, trusted_audience=AUDIENCE) == [actual]
    finally:
        restored.close()


@pytest.mark.parametrize("kind", ["episodic", "semantic"])
def test_postgres_private_visibility_is_bound_before_pool_limit(kind):
    statements = []

    class Session:
        def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    for audience in (AUDIENCE, "qq:group:100"):
        if kind == "episodic":
            PostgresEpisodicStore(Session()).search_pool(query_embedding=None, since=None, until=None,
                channels=None, entities=None, limit=1, trusted_audience=audience)
        else:
            PostgresSemanticStore(Session()).search_pool(query_embedding=None, subject=None,
                limit=1, trusted_audience=audience)
        compiled = statements[-1].compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert sql.index("WHERE") < sql.index("ORDER BY") < sql.index("LIMIT")
        assert "channel" in compiled.params.values() and "qq_private" in compiled.params.values()
        if audience == AUDIENCE:
            assert "conversation_id" in compiled.params.values() and AUDIENCE in compiled.params.values()


def test_private_semantic_store_also_rejects_corrupted_group_visibility(service):
    scope = private_scope().model_copy(update={"visibility": [AUDIENCE, "qq:group:100"]})
    row = SemanticFact(subject="qq:member:2285027817", predicate="helps", value="解释题目",
        confidence=0.8, supporting_events=["source"], formed_at=NOW, scope=private_scope())
    row = row.model_copy(update={"scope": scope})
    service.stores.semantic.insert(row)
    kwargs = dict(query_embedding=None, subject=None, limit=10)
    assert service.stores.semantic.search_pool(**kwargs, trusted_audience="qq:group:100") == []
    assert service.stores.semantic.search_pool(**kwargs, trusted_audience=AUDIENCE) == [row]
