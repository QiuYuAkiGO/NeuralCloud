"""QQ cleaning source, queue recovery, and untrusted model boundaries."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest
from pydantic import ValidationError

from harness.memory.qq_assimilation import (
    QQAssimilationQueue, QQImpressionProposal, QQMemoryProposal, QQMemoryProposer,
    validate_proposal,
)
from harness.memory.shared import SharedMemoryConflict, SharedMemoryStorageError, SharedMemoryStore
from harness.qq.memory import QQPersonContext, QQVerifiedProfile, QQVisualObservation, protocol_message_evidence_ref


NOW = datetime(2026, 9, 14, 5, tzinfo=timezone.utc)
PROTOCOL = "qq-protocol://sha256/" + "a" * 64


def add_message(store, suffix="one", *, group="100", member="200", seconds=0,
                text="我会认真看你的问题。", message_id="1", image_analysis=()):
    protocol = message_id is not None
    observed = NOW + timedelta(seconds=seconds)
    profile = QQVerifiedProfile(
        record_id=f"qq-profile:{group}:{member}:{suffix}", group_id=group, member_id=member,
        display_name="同一个昵称", display_name_source="qq_metadata" if protocol else "qq_profile",
        verification="qq_protocol_verified" if protocol else "qq_ui_verified", capture_id=f"profile-{suffix}",
        observed_at=observed, stored_at=observed, group_evidence=[PROTOCOL], member_evidence=[PROTOCOL],
    )
    observation = QQVisualObservation(
        record_id=f"qq-visual:{group}:{member}:{suffix}", group_id=group, member_id=member,
        profile_record_id=profile.record_id, capture_id=f"capture-{suffix}", capture_item_id="1",
        observed_at=observed, stored_at=observed, text=text,
        author_attribution="qq_protocol_verified" if protocol else "qq_ui_verified",
        attribution_evidence=[PROTOCOL, protocol_message_evidence_ref(group, member, message_id)] if protocol else [PROTOCOL],
        evidence=[PROTOCOL], message_id=message_id, message_id_source="qq_metadata" if protocol else None,
        image_analysis=list(image_analysis),
    )
    results = store.import_qq_context(QQPersonContext(group_id=group, member_id=member,
                                                    profiles=[profile], observations=[observation]))
    return next(result.record for result in results if result.record.kind == "qq_observation")


@pytest.fixture
def setup(tmp_path):
    store = SharedMemoryStore(tmp_path / "shared.sqlite3")
    queue = QQAssimilationQueue(store.path, owner_id="misty", preset_id="active", self_id="999",
                                allowed_group_ids=("100", "101"))
    return store, queue


def make_job(setup, **kwargs):
    store, queue = setup
    add_message(store, **kwargs)
    return queue.enqueue(now=NOW + timedelta(days=1), backfill=True)[0]


def proposal(job, *, impressions=()):
    return QQMemoryProposal.model_validate({"episodes": [{"source_ids": [source.source_id for source in job.sources],
        "summary": "成员提出了一段发言。", "insight": None, "importance": 0.3, "valence": 0}],
        "impressions": list(impressions)})


def impression(job, **updates):
    return {"kind": "person", "subject_refs": ["200"], "topic": "交流习惯", "context": "认真求助时",
            "statement": "暂时觉得这位成员愿意认真回应。", "stance": "support",
            "source_ids": [job.sources[0].source_id], **updates}


def old_impression(**updates):
    return {"impression_id": "impression:one", "kind": "person", "subject_refs": ["qq:member:200"],
            "topic": "交流习惯", "context": "认真求助时", "statement": "暂时觉得这位成员爱调侃。",
            "scope": {"owner_id": "misty", "channel": "qq_group", "conversation_id": "qq:group:100",
                      "visibility": ["local:main", "qq:group:100"]}, **updates}


@pytest.mark.parametrize("field", ["evidence", "continuation_evidence"])
def test_continuation_references_verified_target_evidence(setup, field):
    job = make_job(setup)
    prior = old_impression(**{field: [{"episode_id": "old-episode", "stance": "support"}]})
    proposed = proposal(job, impressions=[impression(job, target_impression_id="impression:one",
                                                    continues_episode_id="old-episode")])
    assert validate_proposal(job, proposed, old_impressions=[prior]) == proposed
    with pytest.raises(ValueError, match="verified same-stance"):
        validate_proposal(job, proposed, old_impressions=[old_impression()])


@pytest.mark.parametrize("changes", [{"target_impression_id": None}, {"stance": "context"},
    {"topic": "another topic"}, {"context": "another context"}, {"subject_refs": ["qq:member:201"]}])
def test_continuation_cannot_escape_target_scope_or_identity(setup, changes):
    job = make_job(setup)
    prior = old_impression(evidence=[{"episode_id": "old-episode", "stance": "support"}])
    with pytest.raises(ValueError):
        proposed = proposal(job, impressions=[impression(job, **{
            "target_impression_id": "impression:one", "continues_episode_id": "old-episode", **changes})])
        validate_proposal(job, proposed, old_impressions=[prior])


def test_queue_loads_old_proposal_without_new_optional_field(setup):
    from harness.memory.qq_assimilation import _digest

    _, queue = setup
    job = make_job(setup)
    proposed = proposal(job, impressions=[impression(job)])
    queue.store_proposal(job.job_id, proposed)
    old_payload = proposed.model_dump(mode="json")
    for item in old_payload["impressions"]:
        item.pop("continues_episode_id")
    with sqlite3.connect(queue.path) as connection:
        connection.execute("UPDATE qq_memory_jobs SET proposal_json=?,proposal_sha256=? WHERE job_id=?",
                           (json.dumps(old_payload), _digest(old_payload), job.job_id))
    recovered = queue.get_job(job.job_id)
    assert recovered.proposal == proposed


def test_snapshot_is_read_only_and_only_authorized_observations(setup):
    store, queue = setup
    add_message(store)
    add_message(store, "other", group="102")
    before = store.path.read_bytes()
    sources = queue.snapshot()
    assert len(sources) == 1 and sources[0].group_id == "100"
    assert sources[0].message_key == "qq-message:999:100:200:1"
    assert sources[0].record.source.original_json
    assert store.path.read_bytes() == before


def test_snapshot_does_not_create_a_missing_database(setup):
    store, queue = setup
    assert queue.snapshot() == () and queue.status()["total_jobs"] == 0
    assert not store.path.exists()


@pytest.mark.parametrize("column,value", [("source_sha256", "0" * 64), ("author_principal", "qq:member:777")])
def test_snapshot_revalidates_source_and_index_binding(setup, column, value):
    store, queue = setup
    add_message(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(f"UPDATE shared_memory_entries SET {column}=? WHERE source_kind='qq_observation'", (value,))
    with pytest.raises(SharedMemoryStorageError):
        queue.snapshot()


def test_enqueue_does_not_touch_original_rows(setup):
    store, queue = setup
    add_message(store)
    with sqlite3.connect(store.path) as connection:
        before = connection.execute("SELECT * FROM shared_memory_entries ORDER BY record_id").fetchall()
    queue.enqueue(now=NOW, backfill=True)
    with sqlite3.connect(store.path) as connection:
        after = connection.execute("SELECT * FROM shared_memory_entries ORDER BY record_id").fetchall()
    assert before == after


def test_window_closes_on_silence_and_does_not_refreeze_on_new_data(setup):
    store, queue = setup
    add_message(store)
    assert queue.enqueue(now=NOW + timedelta(seconds=29)) == ()
    first = queue.enqueue(now=NOW + timedelta(seconds=30))[0]
    add_message(store, "two", message_id="2", seconds=5)
    second = queue.enqueue(now=NOW + timedelta(seconds=40))[0]
    assert first.sources != second.sources
    assert queue.get_job(first.job_id).source_digest == first.source_digest
    assert len(queue.get_job(first.job_id).sources) == 1
    assert queue.enqueue(now=NOW + timedelta(days=1), backfill=True) == ()


def test_source_manifest_is_frozen_before_historical_enqueue(setup):
    store, queue = setup
    add_message(store)
    sources = queue.snapshot()
    add_message(store, "two", message_id="2")
    queued = queue.enqueue(sources=sources, now=NOW, backfill=True)
    assert len(queued[0].sources) == 1
    assert len(queue.enqueue(now=NOW, backfill=True)[0].sources) == 1


@pytest.mark.parametrize("seconds,count", [([0, 30, 60], 3), ([0, 20, 40, 60, 80, 100, 120], 2), (list(range(33)), 2)])
def test_silence_duration_and_message_count_window_limits(setup, seconds, count):
    store, queue = setup
    for index, second in enumerate(seconds):
        add_message(store, str(index), message_id=str(index), seconds=second)
    jobs = queue.enqueue(now=NOW + timedelta(minutes=5), backfill=True)
    assert len(jobs) == count
    assert sum(len(job.sources) for job in jobs) == len(seconds)
    assert all(len(job.sources) <= 32 for job in jobs)


def test_window_parameters_and_groups_are_independent(setup):
    store, _ = setup
    for index, group in enumerate(("100", "100", "101")):
        add_message(store, str(index), group=group, seconds=index * 2, message_id=str(index))
    queue = QQAssimilationQueue(store.path, owner_id="misty", preset_id="active", self_id="999",
                                allowed_group_ids=("100", "101"), idle_seconds=2, max_seconds=4, max_messages=2)
    jobs = queue.enqueue(now=NOW + timedelta(seconds=10), backfill=True)
    assert len(jobs) == 3
    assert all({source.group_id for source in job.sources} == {job.group_id} for job in jobs)


def test_protocol_desktop_and_image_projections_are_one_evidence(setup):
    store, queue = setup
    add_message(store, "protocol", seconds=0)
    add_message(store, "desktop", seconds=1, message_id=None)
    add_message(store, "projection", seconds=2)
    sources = queue.enqueue(now=NOW, backfill=True)[0].sources
    # The initial explicit cutoff sees just the first source; remaining captures
    # in a later window must retain the already frozen evidence identity.
    later = queue.enqueue(now=NOW + timedelta(seconds=40), backfill=True)[0].sources
    assert sum(source.independent_eligible for source in (*sources, *later)) == 1
    assert len({source.independent_key for source in (*sources, *later)}) == 1
    assert all(source.suspected_duplicate_of == sources[0].source_id for source in later)


def test_two_identical_protocol_messages_remain_two_after_ambiguous_desktop_capture(setup):
    store, queue = setup
    add_message(store, "desktop", message_id=None)
    add_message(store, "first", message_id="1", seconds=1)
    add_message(store, "second", message_id="2", seconds=2)
    sources = queue.enqueue(now=NOW + timedelta(seconds=40), backfill=True)[0].sources
    assert len({source.independent_key for source in sources}) == 2
    assert [source.independent_eligible for source in sources] == [True, False, True]


def test_same_text_different_member_or_group_never_merges(setup):
    store, queue = setup
    add_message(store)
    add_message(store, "member", member="201")
    add_message(store, "group", group="101")
    jobs = queue.enqueue(now=NOW, backfill=True)
    assert len({source.independent_key for job in jobs for source in job.sources}) == 3


def test_conflicting_verified_message_identity_is_not_silently_deduplicated(setup):
    store, queue = setup
    add_message(store)
    add_message(store, "two", text="另一句不同的话。")
    with pytest.raises(SharedMemoryConflict):
        queue.enqueue(now=NOW, backfill=True)


@pytest.mark.parametrize("text,member,eligible", [
    ("我总结我很耐心。", "999", False), ("[引用消息 12]", "200", False),
    ("[引用消息 12] 我已认真测试过，步骤如下。", "200", True),
    ("[图片 1]", "200", False), ("[图片 1] 这是我今天画的。", "200", True),
])
def test_self_quote_only_and_media_only_are_not_independent(setup, text, member, eligible):
    job = make_job(setup, text=text, member=member)
    assert job.sources[0].independent_eligible is eligible


def test_retry_and_restart_preserve_frozen_proposal_until_preset_receipt(setup):
    _, queue = setup
    job = make_job(setup)
    running = queue.next_job(now=NOW + timedelta(days=1))
    assert running.status == "preparing" and running.attempts == 1
    queued = queue.store_proposal(job.job_id, proposal(job), basis_digest="basis:one")
    assert queued.proposal_basis_digest == "basis:one"
    queue.fail(job.job_id, "model failed with secret text", now=NOW + timedelta(days=1))
    assert queue.get_job(job.job_id).error_code == "processing_failed"
    assert queue.next_job(now=NOW + timedelta(days=1, seconds=9)) is None
    assert queue.next_job(now=NOW + timedelta(days=1, seconds=10)).proposal == proposal(job)
    assert queue.recover([]) == 1
    assert queue.get_job(job.job_id).status == "pending"
    with pytest.raises(ValueError, match="receipt"):
        queue.ack(job.job_id, [])
    assert queue.ack(job.job_id, [job.job_id]).status == "applied"
    assert queue.recover([job.job_id]) == 0
    assert queue.recover([]) == 1  # Restoring an older preset replays the same proposal.
    assert queue.next_job().proposal == proposal(job)


def test_saved_preset_but_missing_queue_ack_recovers_without_reprocessing(setup):
    _, queue = setup
    job = make_job(setup)
    queue.next_job()
    queue.store_proposal(job.job_id, proposal(job))
    assert queue.recover([job.job_id]) == 1
    assert queue.next_job() is None
    assert queue.status()["applied"] == 1


def test_proposal_and_preset_basis_are_immutable_once_saved(setup):
    _, queue = setup
    job = make_job(setup)
    queue.store_proposal(job.job_id, proposal(job), basis_digest="one")
    with pytest.raises(SharedMemoryConflict):
        queue.store_proposal(job.job_id, proposal(job), basis_digest="two")
    changed = proposal(job).model_dump()
    changed["episodes"][0]["summary"] = "另一个模型输出"
    with pytest.raises(SharedMemoryConflict):
        queue.store_proposal(job.job_id, changed, basis_digest="one")


def test_jobs_and_receipts_are_preset_scoped(setup):
    store, queue = setup
    job = make_job(setup)
    other = QQAssimilationQueue(store.path, owner_id="misty", preset_id="other", self_id="999", allowed_group_ids=("100",))
    assert other.get_job(job.job_id) is None
    new_job = other.enqueue(now=NOW, backfill=True)[0]
    assert new_job.job_id != job.job_id
    other.recover([job.job_id])
    assert other.get_job(new_job.job_id).status == "pending"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign"])
def test_episodes_must_partition_all_frozen_sources_exactly_once(setup, mutation):
    job = make_job(setup)
    raw = proposal(job).model_dump()
    if mutation == "missing":
        raw["episodes"] = []
    elif mutation == "duplicate":
        raw["episodes"] = [raw["episodes"][0], raw["episodes"][0]]
    else:
        raw["episodes"][0]["source_ids"] = ["shared-memory:forged"]
    with pytest.raises(ValueError):
        validate_proposal(job, raw)


@pytest.mark.parametrize("field,value", [("confidence", 1), ("scope", {"group_id": "101"}), ("effect_id", "fake")])
def test_model_cannot_assign_confidence_scope_or_effect_ids(setup, field, value):
    job = make_job(setup)
    with pytest.raises(ValidationError):
        proposal(job, impressions=[impression(job, **{field: value})])


def test_namespaced_members_and_other_group_target_are_checked(setup):
    job = make_job(setup)
    valid = proposal(job, impressions=[impression(job, target_impression_id="impression:one", stance="counter")])
    assert valid.impressions[0].subject_refs == ("qq:member:200",)
    assert validate_proposal(job, valid, old_impressions=[old_impression()]) == valid
    with pytest.raises(ValueError, match="group"):
        validate_proposal(job, valid, old_impressions=[old_impression(scope={"conversation_id": "qq:group:101"})])
    with pytest.raises(ValueError, match="comparable context"):
        validate_proposal(job, valid, old_impressions=[old_impression(context="闲聊时")])


def test_third_party_labels_cannot_target_the_mentioned_person(setup):
    job = make_job(setup, text="我说201很懒。")
    invalid = proposal(job, impressions=[impression(job, subject_refs=["201"], statement="201很懒。")])
    with pytest.raises(ValueError, match="own attributed utterances"):
        validate_proposal(job, invalid)


def test_another_authors_fresh_message_cannot_supply_independence_for_quoted_subject(setup):
    store, queue = setup
    add_message(store, "quoted", member="200", text="[引用消息 123]")
    add_message(store, "fresh", member="201", message_id="2", seconds=1, text="200说他很喜欢编程。")
    job = queue.enqueue(now=NOW + timedelta(minutes=1), backfill=True)[0]
    invalid = proposal(job, impressions=[impression(job, source_ids=[source.source_id for source in job.sources])])
    with pytest.raises(ValueError, match="own independent evidence"):
        validate_proposal(job, invalid)


def test_proposer_keeps_native_scope_impressions_and_strictly_rejects_invalid_model_output(setup):
    job = make_job(setup)
    expected = proposal(job, impressions=[impression(job, target_impression_id="impression:one", stance="counter")])

    class Model:
        def complete_json(self, *, system, user, **kwargs):
            assert "第三方" in system and "status=unknown" in system
            payload = json.loads(user)
            assert payload["old_impressions"] == [old_impression()]
            assert payload["frozen_sources"][0]["source_id"] == job.sources[0].source_id
            return expected.model_dump(mode="json")

    assert QQMemoryProposer(Model()).propose(job, old_impressions=[old_impression(), old_impression(
        impression_id="elsewhere", scope={"conversation_id": "qq:group:101"})]) == expected

    class InvalidModel:
        def complete_json(self, **kwargs):
            return {"unrelated": "reply"}

    with pytest.raises(ValueError):
        QQMemoryProposer(InvalidModel()).propose(job)


def test_topic_and_group_do_not_require_invented_person_subjects(setup):
    job = make_job(setup)
    for kind in ("topic", "group"):
        result = proposal(job, impressions=[impression(job, kind=kind, subject_refs=[])])
        assert validate_proposal(job, result).impressions[0].subject_refs == ()
    with pytest.raises(ValidationError):
        QQImpressionProposal.model_validate(impression(job, kind="relationship", subject_refs=["200"]))


def test_get_job_revalidates_persisted_proposal_and_frozen_sources(setup):
    store, queue = setup
    job = make_job(setup)
    queue.store_proposal(job.job_id, proposal(job))
    with sqlite3.connect(store.path) as connection:
        raw = proposal(job).model_dump(mode="json")
        raw["episodes"][0]["source_ids"] = ["shared-memory:forged"]
        connection.execute("UPDATE qq_memory_jobs SET proposal_json=? WHERE job_id=?", (json.dumps(raw), job.job_id))
    with pytest.raises(SharedMemoryStorageError):
        queue.get_job(job.job_id)


def test_failed_group_head_blocks_later_windows_but_other_groups_continue(setup):
    store, queue = setup
    add_message(store, "first", seconds=0)
    add_message(store, "later", seconds=40, message_id="2")
    add_message(store, "elsewhere", group="101", seconds=50)
    jobs = queue.enqueue(now=NOW + timedelta(seconds=90), backfill=True)
    first = queue.next_job(now=NOW + timedelta(seconds=90))
    assert first.job_id == jobs[0].job_id
    queue.fail(first.job_id, "model_failed", now=NOW + timedelta(seconds=90))
    elsewhere = queue.next_job(now=NOW + timedelta(seconds=91))
    assert elsewhere.group_id == "101"
    assert queue.next_job(now=NOW + timedelta(seconds=91)) is None
    queue.ack(elsewhere.job_id, [elsewhere.job_id])
    assert queue.next_job(now=NOW + timedelta(seconds=100)).job_id == first.job_id
    queue.ack(first.job_id, [first.job_id])
    assert queue.next_job(now=NOW + timedelta(seconds=101)).job_id == jobs[1].job_id


def test_validly_shaped_proposal_tampering_fails_its_saved_digest(setup):
    store, queue = setup
    job = make_job(setup)
    queue.store_proposal(job.job_id, proposal(job))
    with sqlite3.connect(store.path) as connection:
        raw = proposal(job).model_dump(mode="json")
        raw["episodes"][0]["summary"] = "符合schema但篡改了模型原始提案。"
        connection.execute("UPDATE qq_memory_jobs SET proposal_json=? WHERE job_id=?", (json.dumps(raw), job.job_id))
    with pytest.raises(SharedMemoryStorageError):
        queue.get_job(job.job_id)


def test_supplied_manifest_is_revalidated_against_trusted_queue_scope(setup):
    store, queue = setup
    add_message(store)
    source = queue.snapshot()[0]
    with pytest.raises(ValueError):
        queue.enqueue(sources=[source.model_copy(update={"group_id": "101"})], now=NOW, backfill=True)
    other = QQAssimilationQueue(store.path, owner_id="misty", preset_id="active", self_id="998", allowed_group_ids=("100",))
    with pytest.raises(ValueError, match="authorization"):
        other.enqueue(sources=[source], now=NOW, backfill=True)


def test_self_and_quoted_subject_cannot_be_laundered_through_relationship(setup):
    store, queue = setup
    add_message(store, "quoted", member="200", text="[引用消息 123]")
    add_message(store, "self", member="999", message_id="2", seconds=1, text="我觉得200很耐心。")
    add_message(store, "fresh", member="201", message_id="3", seconds=2)
    job = queue.enqueue(now=NOW + timedelta(minutes=1), backfill=True)[0]
    bad = proposal(job, impressions=[impression(job, kind="relationship", subject_refs=["200", "201"],
                    source_ids=[source.source_id for source in job.sources])])
    with pytest.raises(ValueError, match="own independent evidence"):
        validate_proposal(job, bad)


def test_proposal_store_rechecks_frozen_old_impression_context(setup):
    _, queue = setup
    job = make_job(setup)
    valid = proposal(job, impressions=[impression(job, target_impression_id="impression:one", stance="counter")])
    with pytest.raises(ValueError, match="different subjects"):
        queue.store_proposal(job.job_id, valid, old_impressions=[old_impression(subject_refs=["qq:member:201"])])
    assert queue.get_job(job.job_id).proposal is None


def test_reset_uncommitted_proposal_keeps_sources_and_checks_old_basis(setup):
    _, queue = setup
    job = make_job(setup)
    queue.next_job()
    queue.store_proposal(job.job_id, proposal(job), basis_digest="old-context")
    queue.fail(job.job_id, "context_changed")
    with pytest.raises(SharedMemoryConflict, match="basis changed"):
        queue.reset_proposal(job.job_id, expected_digest="another-context")
    cleared = queue.reset_proposal(job.job_id, expected_digest="old-context")
    assert cleared.status == "pending" and cleared.attempts == 0
    assert cleared.proposal is None and cleared.proposal_basis_digest is None
    assert cleared.sources == job.sources and cleared.source_digest == job.source_digest
    assert queue.next_job().job_id == job.job_id
    changed = proposal(job).model_dump()
    changed["episodes"][0]["summary"] = "依新上下文重新整理的提案。"
    assert queue.store_proposal(job.job_id, changed, basis_digest="new-context").proposal.episodes[0].summary == changed["episodes"][0]["summary"]


def test_reset_committed_proposal_is_rejected(setup):
    _, queue = setup
    job = make_job(setup)
    queue.store_proposal(job.job_id, proposal(job), basis_digest="old-context")
    queue.ack(job.job_id, [job.job_id])
    with pytest.raises(ValueError, match="committed"):
        queue.reset_proposal(job.job_id, expected_digest="old-context")
    assert queue.get_job(job.job_id).proposal == proposal(job)


@pytest.mark.parametrize("kind", ["self_report", "third_party", "image_interpretation", "inference"])
def test_unverified_content_cannot_strengthen_a_factual_impression(setup, kind):
    job = make_job(setup, text="我自称很有钱。")
    with pytest.raises(ValidationError, match="cannot strengthen factual impressions"):
        proposal(job, impressions=[impression(job, evidence_kind=kind, stance="support")])
    uncertain = proposal(job, impressions=[impression(job, evidence_kind=kind, stance="uncertain")])
    assert validate_proposal(job, uncertain).impressions[0].stance == "uncertain"
    assert uncertain.episodes[0].insight is None


def test_actual_speakers_behavior_can_be_observed_without_asserting_self_report_truth(setup):
    job = make_job(setup, text="我自称很有钱。")
    observed = proposal(job, impressions=[impression(job, statement="这次发言在强调自己的财富。",
                                                    evidence_kind="observed_behavior")])
    assert validate_proposal(job, observed).impressions[0].evidence_kind == "observed_behavior"


def test_live_model_must_explicitly_classify_evidence_kind(setup):
    job = make_job(setup)
    raw = proposal(job, impressions=[impression(job)]).model_dump(mode="json")
    del raw["impressions"][0]["evidence_kind"]

    class Model:
        def complete_json(self, *, system, user, **kwargs):
            schema = json.loads(user)["schema"]
            assert "evidence_kind" in schema["$defs"]["QQImpressionProposal"]["required"]
            assert "persona 仅用于主观" in system
            assert "反差可以向正面或负面修订" in system
            return raw

    with pytest.raises(ValueError, match="evidence validation"):
        QQMemoryProposer(Model()).propose(job)


def test_live_subject_binding_is_explicit_without_relaxing_evidence(setup):
    from copy import deepcopy
    from jsonschema import Draft202012Validator
    job = make_job(setup)
    raw = proposal(job, impressions=[impression(job)]).model_dump(mode="json")

    class Model:
        def complete_json(self, *, system, user, **kwargs):
            schema = json.loads(user)["schema"]
            definition = schema["$defs"]["QQImpressionProposal"]
            assert "subject_refs" in definition["required"]
            assert "default" not in definition["properties"]["subject_refs"]
            validator = Draft202012Validator(schema)
            assert validator.is_valid(raw)
            for kind in ("person", "topic", "group"):
                missing = deepcopy(raw)
                missing["impressions"][0]["kind"] = kind
                del missing["impressions"][0]["subject_refs"]
                assert not validator.is_valid(missing)
            topic = deepcopy(raw)
            topic["impressions"][0].update(kind="topic", subject_refs=[])
            assert validator.is_valid(topic)
            forged = deepcopy(raw)
            forged["impressions"][0]["subject_refs"] = ["qq:member:987654"]
            assert not validator.is_valid(forged)
            return raw

    assert QQMemoryProposer(Model()).propose(job).impressions[0].subject_refs == ("qq:member:200",)


def test_persona_is_separate_from_immutable_evidence_in_model_input(setup):
    job = make_job(setup)

    class Model:
        def complete_json(self, *, system, user, **kwargs):
            payload = json.loads(user)
            assert payload["persona"] == {"name": "Misty", "values": ["认真"]}
            assert "Misty" not in payload["frozen_sources"][0]["text"]
            assert payload["frozen_sources"][0]["authority"] == "attributed_utterance_only_not_verified_content"
            assert "按引用、参与者和话题拆成情景" in system
            return proposal(job).model_dump(mode="json")

    assert QQMemoryProposer(Model()).propose(job, persona={"name": "Misty", "values": ["认真"]}).episodes[0].insight is None


def test_unrecoverable_head_is_skipped_and_same_group_continues(setup):
    store, queue = setup
    add_message(store, "first", seconds=0)
    add_message(store, "later", seconds=40, message_id="2")
    jobs = queue.enqueue(now=NOW + timedelta(seconds=90), backfill=True)
    first = queue.next_job(now=NOW + timedelta(seconds=90))
    assert first.job_id == jobs[0].job_id
    failed = queue.fail(first.job_id, "ValueError", now=NOW + timedelta(seconds=90))
    assert failed.status == "skipped" and failed.error_code == "ValueError"
    later = queue.next_job(now=NOW + timedelta(seconds=90))
    assert later.job_id == jobs[1].job_id and later.group_id == "100"


def test_transient_http_retries_then_skips_after_attempt_limit(setup):
    store, _ = setup
    add_message(store, "first", seconds=0)
    add_message(store, "later", seconds=40, message_id="2")
    add_message(store, "elsewhere", group="101", seconds=50)
    queue = QQAssimilationQueue(store.path, owner_id="misty", preset_id="active", self_id="999",
                                allowed_group_ids=("100", "101"), max_attempts=2)
    jobs = queue.enqueue(now=NOW + timedelta(seconds=90), backfill=True)
    first = queue.next_job(now=NOW + timedelta(seconds=90))
    assert first.job_id == jobs[0].job_id
    retried = queue.fail(first.job_id, "HTTPStatusError", now=NOW + timedelta(seconds=90))
    assert retried.status == "retry" and retried.error_code == "HTTPStatusError"
    elsewhere = queue.next_job(now=NOW + timedelta(seconds=91))
    assert elsewhere.group_id == "101"
    queue.ack(elsewhere.job_id, [elsewhere.job_id])
    assert queue.next_job(now=NOW + timedelta(seconds=91)) is None
    second = queue.next_job(now=NOW + timedelta(seconds=100))
    assert second.job_id == first.job_id
    skipped = queue.fail(second.job_id, "HTTPStatusError", now=NOW + timedelta(seconds=100))
    assert skipped.status == "skipped" and skipped.error_code == "HTTPStatusError"
    later = queue.next_job(now=NOW + timedelta(seconds=100))
    assert later.job_id == jobs[1].job_id


def test_invalid_frozen_head_is_dead_lettered_without_blocking_the_group(setup):
    store, queue = setup
    add_message(store, "first", seconds=0)
    add_message(store, "later", seconds=40, message_id="2")
    jobs = queue.enqueue(now=NOW + timedelta(seconds=90), backfill=True)
    with sqlite3.connect(store.path) as connection:
        raw = proposal(jobs[0]).model_dump(mode="json")
        raw["episodes"][0]["source_ids"] = ["shared-memory:forged"]
        connection.execute("UPDATE qq_memory_jobs SET proposal_json=? WHERE job_id=?",
                           (json.dumps(raw), jobs[0].job_id))
    later = queue.next_job(now=NOW + timedelta(seconds=90))
    assert later.job_id == jobs[1].job_id
    assert queue.status()["skipped"] == 1
    queue.ack(later.job_id, [later.job_id])
    with pytest.raises(SharedMemoryStorageError):
        queue.get_job(jobs[0].job_id)
    recovered = QQAssimilationQueue(store.path, owner_id="misty", preset_id="active", self_id="999",
                                    allowed_group_ids=("100", "101"))
    assert recovered.recover([later.job_id]) == 0
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT status FROM qq_memory_jobs WHERE job_id=?",
                                  (jobs[0].job_id,)).fetchone()[0] == "skipped"


def test_schema_rejection_receives_bounded_feedback_and_keeps_identity_constraints(setup):
    from harness.llm.deepseek_structured import StructuredResponseError
    job = make_job(setup)
    expected = proposal(job)
    calls = []

    class Model:
        def complete_json(self, *, user, **kwargs):
            payload = json.loads(user)
            calls.append(payload)
            if len(calls) == 1:
                raise StructuredResponseError("deepseek_structured_schema_invalid", diagnostics=[
                    {"validator": "enum", "path": ["impressions", 0, "subject_refs", 0], "missing_fields": []}])
            assert payload["schema"] == calls[0]["schema"]
            assert payload["frozen_sources"] == calls[0]["frozen_sources"]
            assert payload["validation_feedback"][0]["validator"] == "enum"
            return expected.model_dump(mode="json")

    assert QQMemoryProposer(Model()).propose(job) == expected
    assert len(calls) == 2


def test_exhausted_model_business_validation_is_retryable_but_bounded(setup):
    from harness.memory.qq_assimilation import QQProposalValidationError, UNRECOVERABLE_JOB_ERRORS
    job = make_job(setup)
    calls = []

    class Model:
        def complete_json(self, **kwargs):
            calls.append(1)
            return {"unrelated": "private invalid output"}

    with pytest.raises(QQProposalValidationError) as caught:
        QQMemoryProposer(Model()).propose(job)
    assert len(calls) == 2
    assert caught.value.error_code not in UNRECOVERABLE_JOB_ERRORS
    assert "private" not in str(caught.value)


def test_schema_feedback_does_not_loop_or_mask_second_failure(setup):
    from harness.llm.deepseek_structured import StructuredResponseError
    calls = []
    class Model:
        def complete_json(self, **kwargs):
            calls.append(1)
            raise StructuredResponseError("deepseek_structured_schema_invalid")
    with pytest.raises(StructuredResponseError):
        QQMemoryProposer(Model()).propose(make_job(setup))
    assert len(calls) == 2


def test_model_rejection_reason_is_content_free():
    from harness.memory.qq_assimilation import QQProposalValidationError
    assert QQProposalValidationError(ValueError("impression needs new non-self, non-duplicate, non-quoted evidence")).error_code == "qq_model_validation_independent_evidence"
    assert "secret" not in str(QQProposalValidationError(ValueError("secret")))


def test_duplicate_window_planning_does_not_hold_sqlite_writer_lock(setup, monkeypatch):
    import sqlite3
    store, queue = setup
    add_message(store)
    original = queue._link_duplicates
    checked = []
    def unlocked(source, previous):
        with sqlite3.connect(queue.path, timeout=0.1) as other:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        checked.append(True)
        return original(source, previous)
    monkeypatch.setattr(queue, "_link_duplicates", unlocked)
    queue.enqueue(now=NOW + timedelta(seconds=90))
    assert checked


def test_concurrent_window_assignment_is_rechecked_before_commit(setup, monkeypatch):
    store, queue = setup
    add_message(store)
    other = QQAssimilationQueue(store.path, owner_id="misty", preset_id="active", self_id="999",
                               allowed_group_ids=("100", "101"))
    original = queue._link_duplicates
    def raced(source, previous):
        other.enqueue(now=NOW + timedelta(seconds=90))
        return original(source, previous)
    monkeypatch.setattr(queue, "_link_duplicates", raced)
    with pytest.raises(SharedMemoryConflict, match="assignments changed"):
        queue.enqueue(now=NOW + timedelta(seconds=90))
    monkeypatch.setattr(queue, "_link_duplicates", original)
    assert queue.enqueue(now=NOW + timedelta(seconds=90)) == ()
    assert queue.status()["total_jobs"] == 1
