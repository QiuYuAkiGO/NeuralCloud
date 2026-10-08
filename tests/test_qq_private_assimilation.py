"""Private QQ observations use the existing durable pipeline without scope widening."""

from datetime import timedelta
import json

import pytest

from harness.memory.qq_assimilation import (
    QQAssimilationQueue, QQMemoryProposer, QQSource, _digest, validate_proposal,
)
from harness.memory.shared import SharedMemoryStore
from tests.test_qq_assimilation import NOW, add_message, impression, old_impression, proposal


PRIVATE = "private:999:200"


def make_queue(store, *, peers=("200",), groups=()):
    return QQAssimilationQueue(store.path, owner_id="misty", preset_id="active", self_id="999",
                               allowed_group_ids=groups, allowed_private_peer_ids=peers)


@pytest.fixture
def private_setup(tmp_path):
    store = SharedMemoryStore(tmp_path / "shared.sqlite3")
    return store, make_queue(store)


def private_job(setup, **kwargs):
    store, queue = setup
    add_message(store, group=PRIVATE, **kwargs)
    return queue.enqueue(now=NOW + timedelta(days=1), backfill=True)[0]


def private_impression(**updates):
    return old_impression(scope={"owner_id": "misty", "channel": "qq_private",
        "conversation_id": "qq:" + PRIVATE, "visibility": ["local:main", "qq:" + PRIVATE]}, **updates)


def test_private_snapshot_is_opt_in_same_account_and_peer(private_setup):
    store, queue = private_setup
    add_message(store, "peer", group=PRIVATE)
    add_message(store, "self", group=PRIVATE, member="999", message_id="2")
    add_message(store, "other-peer", group="private:999:201", member="201")
    add_message(store, "other-account", group="private:998:200")
    add_message(store, "group", group="100")
    before = store.path.read_bytes()
    sources = queue.snapshot()
    assert len(sources) == 2
    assert {source.group_id for source in sources} == {PRIVATE}
    assert {source.record.channel for source in sources} == {"qq_private"}
    assert {source.record.session_id for source in sources} == {"qq:" + PRIVATE}
    assert store.path.read_bytes() == before
    group_only = make_queue(store, peers=(), groups=("100",))
    assert len(group_only.snapshot()) == 1
    assert group_only.snapshot()[0].group_id == "100"
    assert make_queue(store, peers=()).snapshot() == ()


@pytest.mark.parametrize("peers", [("999",), ("private:999:200",), ("bad",)])
def test_private_authorization_must_be_numeric_other_peer(private_setup, peers):
    store, _ = private_setup
    with pytest.raises(ValueError):
        make_queue(store, peers=peers)


def test_private_source_cannot_change_account_or_author(private_setup):
    store, queue = private_setup
    record = add_message(store, group=PRIVATE)
    with pytest.raises(ValueError, match="account and its peer"):
        QQSource.from_record(record, self_id="998")
    source = QQSource.from_record(record, self_id="999")
    with pytest.raises(ValueError):
        queue.enqueue(now=NOW, backfill=True, sources=[source.model_copy(update={"member_id": "201"})])
    with pytest.raises(ValueError, match="trusted authorization"):
        make_queue(store, peers=("201",)).enqueue(now=NOW, backfill=True, sources=[source])


def test_private_protocol_projections_count_once_and_never_cross_scope(private_setup):
    store, _ = private_setup
    queue = make_queue(store, peers=("200", "201"), groups=("100",))
    add_message(store, "first", group=PRIVATE)
    add_message(store, "projection", group=PRIVATE, seconds=1)
    add_message(store, "other-peer", group="private:999:201", member="201")
    add_message(store, "group", group="100")
    jobs = queue.enqueue(now=NOW + timedelta(minutes=1), backfill=True)
    assert len(jobs) == 3
    private = next(job for job in jobs if job.group_id == PRIVATE)
    assert sum(source.independent_eligible for source in private.sources) == 1
    assert len({source.independent_key for source in private.sources}) == 1
    assert len({job.sources[0].independent_key for job in jobs}) == 3
    assert queue.enqueue(now=NOW + timedelta(days=1), backfill=True) == ()


def test_private_queue_recovery_receipts_and_per_conversation_fifo(private_setup):
    store, _ = private_setup
    queue = make_queue(store, groups=("100",))
    add_message(store, "first", group=PRIVATE)
    add_message(store, "second", group=PRIVATE, seconds=60, message_id="2")
    add_message(store, "group", group="100", seconds=1)
    jobs = queue.enqueue(now=NOW + timedelta(minutes=2), backfill=True)
    first = queue.next_job(now=NOW + timedelta(minutes=2))
    assert first.group_id == PRIVATE and first.sources[0].text
    queue.fail(first.job_id, "model_failure", now=NOW + timedelta(minutes=2))
    other = queue.next_job(now=NOW + timedelta(minutes=2))
    assert other.group_id == "100"
    queue.ack(other.job_id, [other.job_id])
    assert queue.next_job(now=NOW + timedelta(minutes=2)) is None
    retry = queue.next_job(now=NOW + timedelta(minutes=3))
    assert retry.job_id == first.job_id
    queue.store_proposal(first.job_id, proposal(first))
    restored = make_queue(store, groups=("100",))
    restored.recover([first.job_id, other.job_id])
    assert restored.get_job(first.job_id).status == "applied"
    assert restored.get_job(first.job_id).proposal == proposal(first)
    assert restored.next_job(now=NOW + timedelta(minutes=4)).job_id not in {first.job_id, other.job_id}
    assert restored.status()["total_jobs"] == len(jobs)
    assert make_queue(store, peers=(), groups=("100",)).get_job(first.job_id) is None


def test_private_self_reply_cannot_create_independent_impressions(private_setup):
    job = private_job(private_setup, member="999")
    assert job.sources[0].is_self and not job.sources[0].independent_eligible
    with pytest.raises(ValueError, match="self-confirming"):
        validate_proposal(job, proposal(job, impressions=[impression(job, subject_refs=["999"])]))
    with pytest.raises(ValueError, match="non-self"):
        validate_proposal(job, proposal(job, impressions=[impression(job, kind="topic", subject_refs=[])]))


def test_private_relationship_uses_peer_evidence_with_self_participation(private_setup):
    store, queue = private_setup
    add_message(store, "peer", group=PRIVATE)
    add_message(store, "self", group=PRIVATE, member="999", seconds=1, message_id="2")
    job = queue.enqueue(now=NOW + timedelta(minutes=1), backfill=True)[0]
    proposed = proposal(job, impressions=[impression(job, kind="relationship", subject_refs=["200", "999"],
        source_ids=[source.source_id for source in job.sources])])
    assert validate_proposal(job, proposed) == proposed
    with pytest.raises(ValueError, match="attributed utterances"):
        validate_proposal(job, proposal(job, impressions=[impression(job, subject_refs=["201"])]))


def test_private_cannot_form_group_impressions(private_setup):
    job = private_job(private_setup)
    with pytest.raises(ValueError, match="group atmosphere"):
        validate_proposal(job, proposal(job, impressions=[impression(job, kind="group", subject_refs=[])]))


@pytest.mark.parametrize("conversation,channel", [
    ("qq:group:200", "qq_group"), ("qq:private:999:201", "qq_private"),
    ("qq:private:998:200", "qq_private"), ("qq:private:999:200", "qq_group"),
])
def test_private_counterevidence_and_continuation_cannot_target_other_scope(private_setup, conversation, channel):
    job = private_job(private_setup)
    prior = private_impression(evidence=[{"episode_id": "prior", "stance": "contradict"}])
    prior["scope"] = {"channel": channel, "conversation_id": conversation}
    proposed = proposal(job, impressions=[impression(job, stance="counter", target_impression_id="impression:one",
        continues_episode_id="prior")])
    with pytest.raises(ValueError, match="another kind or group/private scope"):
        validate_proposal(job, proposed, old_impressions=[prior])


def test_private_continuation_and_proposer_share_only_same_conversation(private_setup):
    job = private_job(private_setup)
    prior = private_impression(evidence=[{"episode_id": "prior", "stance": "support"}])
    proposed = proposal(job, impressions=[impression(job, target_impression_id="impression:one",
        continues_episode_id="prior", evidence_kind="observed_behavior")])
    assert validate_proposal(job, proposed, old_impressions=[prior]) == proposed

    class Model:
        def complete_json(self, *, system, user, **kwargs):
            payload = json.loads(user)
            assert payload["conversation_kind"] == "private"
            assert payload["conversation_id"] == "qq:" + PRIVATE
            assert payload["old_impressions"] == [{**prior,
                "current_window_update_eligible": True,
                "current_window_evidence_role": "historical_context_only_not_new_evidence"}]
            assert payload["schema"]["$defs"]["QQImpressionProposal"]["properties"]["kind"]["enum"] == [
                "person", "topic"]
            return proposed.model_dump(mode="json")

    assert QQMemoryProposer(Model()).propose(job, old_impressions=[prior, old_impression()]) == proposed


def test_group_source_shape_and_identity_remain_backward_compatible(private_setup):
    store, _ = private_setup
    queue = make_queue(store, groups=("100",))
    record = add_message(store)
    job = queue.enqueue(now=NOW + timedelta(days=1), backfill=True)[0]
    # Construct the old serialized source independently: no added default fields
    # may alter already committed source digests or their receipt/job identities.
    legacy = {"source_id": record.record_id, "source_sha256": record.source.original_sha256,
        "group_id": "100", "member_id": "200", "self_id": "999", "occurred_at": "2026-09-14T05:00:00Z",
        "text": record.text, "message_key": "qq-message:999:100:200:1",
        "independent_key": "qq-message:999:100:200:1", "is_self": False,
        "contains_quote": False, "quote_only": False, "media_only": False,
        "suspected_duplicate_of": None, "independent_eligible": True, "record": record.model_dump(mode="json")}
    assert job.sources[0].model_dump(mode="json") == legacy
    assert job.source_digest == _digest([legacy])
    assert job.job_id == "qq-memory-job:" + _digest([
        "misty", "active", "qq-memory-v1", "999", "100", _digest([legacy])])
