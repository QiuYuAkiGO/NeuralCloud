import asyncio
import sqlite3
import threading

import pytest

from harness.body_belief import BodyBeliefAppraiser, BodyBeliefService, BodyBeliefStore
from harness.body_belief.consolidation import BodyBeliefConsolidator
from harness.body_belief.models import ConsolidationProfile, FrozenBeliefSnapshot
from tests.body_belief.conftest import candidate, experience
from tests.body_belief.test_retrieval import ObservationalModel


def record(service, item):
    return service.record_decision(
        decision_id=item.decision_id, episode_id=item.episode_id, context_hash=item.context_hash, context_key="room_a",
        observation_before=item.observation_before, decision=item.decision, action_outcome_refs=item.action_outcome_refs,
        outcome_success=item.outcome_success, outcome_verified=item.outcome_verified, completed_at=item.completed_at,
    )


def test_terminal_experience_waits_for_next_sample_and_restart_deduplicates(tmp_path):
    path = tmp_path / "service.sqlite3"
    item = experience(0)

    async def scenario():
        first = BodyBeliefService(BodyBeliefStore(path), memory_treatment="objective_events")
        assert record(first, item) and not record(first, item)
        assert await first.observe(item.observation_before) == ()
        await first.close()
        second = BodyBeliefService(BodyBeliefStore(path), memory_treatment="objective_events")
        formed = await second.observe(item.observation_after)
        assert len(formed) == 1 and formed[0] == item.model_copy(update={"experience_id": formed[0].experience_id})
        assert not record(second, item)
        assert await second.observe(item.observation_after) == ()
        assert len(second.store.experiences()) == 1
        await second.close()

    asyncio.run(scenario())


def test_appraisal_inflight_pause_drains_without_consolidating_until_resume(tmp_path):
    started, release = threading.Event(), threading.Event()

    class BlockingModel(ObservationalModel):
        def complete_chat_result(self, messages, *, stream=False):
            started.set()
            assert release.wait(2)
            return super().complete_chat_result(messages, stream=stream)

    async def scenario():
        store = BodyBeliefStore(tmp_path / "pause.sqlite3")
        for index in range(2):
            store.append_experience(experience(index))
        service = BodyBeliefService(store, memory_treatment="body_causal_beliefs", appraiser=BodyBeliefAppraiser(BlockingModel()))
        third = experience(2)
        record(service, third)
        await service.observe(third.observation_after)
        assert await asyncio.to_thread(started.wait, 1)
        await service.pause()
        assert not await service.drain(.01)
        release.set()
        assert await service.drain(1)
        assert not store.beliefs()
        await service.resume()
        assert await service.drain(1)
        assert len(store.beliefs()) == 1
        revision = store.beliefs()[0].revision
        await service.close()
        reopened = BodyBeliefStore(tmp_path / "pause.sqlite3")
        assert reopened.beliefs()[0].revision == revision
        reopened.close()

    asyncio.run(scenario())


def test_frozen_snapshot_ablation_rescue_and_unverified_belief_import(store):
    episodes = [experience(index) for index in range(3)]
    for item in episodes:
        store.append_experience(item)
    learned = BodyBeliefConsolidator(store).apply(candidate(episodes))
    snapshot = store.snapshot()
    identity = snapshot.content_hash()

    def learned_agent():
        return "station.use.v1" if store.beliefs() else "no_action"

    assert learned_agent() == "station.use.v1"
    assert store.restore(snapshot, include_beliefs=False) == identity
    assert store.experiences() == snapshot.experiences
    assert learned_agent() == "no_action"
    store.restore(snapshot, include_beliefs=True)
    assert learned_agent() == "station.use.v1"
    assert store.snapshot().content_hash() == identity
    with pytest.raises(ValueError):
        store.restore(FrozenBeliefSnapshot(beliefs=(learned,)))
    assert store.snapshot().content_hash() == identity


def test_model_appraiser_failure_preserves_committed_objective_outcome(tmp_path):
    class FailingAppraiser:
        def appraise(self, *args, **kwargs):
            raise RuntimeError("model unavailable")

    async def scenario():
        service = BodyBeliefService(BodyBeliefStore(tmp_path / "failed.sqlite3"),
                                    memory_treatment="body_causal_beliefs", appraiser=FailingAppraiser())
        item = experience(0)
        record(service, item)
        await service.observe(item.observation_after)
        assert await service.drain(1)
        assert len(service.store.experiences()) == 1 and not service.store.beliefs()
        assert any(entry["event"] == "appraisal_failed" for entry in service.store.audit())
        await service.close()

    asyncio.run(scenario())


def test_snapshot_cannot_elevate_precision_or_bypass_consolidation_profile(store):
    episodes = [experience(index) for index in range(3)]
    for item in episodes:
        store.append_experience(item)
    learned = BodyBeliefConsolidator(store).apply(candidate(episodes))
    snapshot = store.snapshot()
    one_support = learned.model_copy(update={"supporting_experience_refs": ("exp-0",)})
    with pytest.raises(ValueError, match="independent support"):
        store.restore(snapshot.model_copy(update={"beliefs": (one_support,)}))
    changed_profile = snapshot.model_copy(update={"consolidation_profile": ConsolidationProfile(minimum_support=4)})
    with pytest.raises(ValueError):
        store.restore(changed_profile)
    assert store.snapshot() == snapshot


def test_consolidation_profile_is_persisted_and_drift_is_rejected(tmp_path):
    path = tmp_path / "profile.sqlite3"
    initial = BodyBeliefStore(path, profile=ConsolidationProfile(minimum_support=4))
    initial.close()
    reopened = BodyBeliefStore(path)
    assert reopened.profile.minimum_support == 4
    reopened.close()
    with pytest.raises(ValueError, match="profile drift"):
        BodyBeliefStore(path, profile=ConsolidationProfile())


def test_next_observation_and_appraisal_work_commit_atomically_across_restart(tmp_path):
    async def scenario():
        path = tmp_path / "atomic.sqlite3"
        service = BodyBeliefService(BodyBeliefStore(path), memory_treatment="body_causal_beliefs",
                                    appraiser=BodyBeliefAppraiser(ObservationalModel()))
        await service.pause()
        item = experience(0)
        record(service, item)
        with service.store.db:
            service.store.db.execute("CREATE TRIGGER interrupt_work BEFORE INSERT ON appraisal_work "
                                     "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
            await service.observe(item.observation_after)
        assert not service.store.experiences()
        assert len(service.store.pending()) == 1
        with service.store.db:
            service.store.db.execute("DROP TRIGGER interrupt_work")
        await service.close()
        reopened = BodyBeliefService(BodyBeliefStore(path), memory_treatment="body_causal_beliefs",
                                    appraiser=BodyBeliefAppraiser(ObservationalModel()))
        await reopened.pause()
        assert len(await reopened.observe(item.observation_after)) == 1
        assert not reopened.store.pending()
        assert len(reopened.store.db.execute("SELECT id FROM appraisal_work").fetchall()) == 1
        assert await reopened.observe(item.observation_after) == ()
        await reopened.close()

    asyncio.run(scenario())
