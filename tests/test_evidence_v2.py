"""Real v2 transactions over frozen manifests and isolated SQLite, without LLMs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import json

import pytest
from pydantic import ValidationError

from harness.memory.evidence_manifest import EvidenceManifestStore
from harness.memory.evidence_models import ClaimProposal, EvidenceAnchor, MemoryUpdateGroup
from harness.memory.evidence_v2 import EvidenceV2Store, history, is_visible, project_entry
from harness.memory.production import BeliefRevision, ProductionMemoryStore
from tests.test_evidence_manifest import NOW, source


@pytest.fixture
def env(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "v2.sqlite3")
    memory.ingest([source("a", text="我现在喜欢茶。"), source("b", text="我现在喜欢咖啡。")])
    return memory, EvidenceManifestStore(memory), EvidenceV2Store(memory)


def freeze(env, command, refs=("a",), *, audience="local:main", now=NOW):
    memory, manifests, _ = env
    recall = memory.recall(audience=audience, now=now)
    payload = {"evidence_views": manifests.prepare_views(refs, audience=audience),
        "memories": [{"source_ids": [entry["record_id"] for entry in recall.get("entries", [])],
            "text": json.dumps({"derived_memory": recall}, ensure_ascii=False)}]}
    return manifests.issue(payload, audience=audience, command_id=command, now=now)


def anchor(manifest, index=0, **updates):
    view = manifest["views"][index]
    return {"unit_id": view["unit_id"], "start": view["start"], "end": view["end"], "quote": view["text"],
        "role": "supports", "rationale": "Synthetic declared relationship", **updates}


def claim(manifest, **updates):
    return {"subject_id": "user:a", "topic": "饮品", "statement": "模型对饮品的解释", "kind": "self_report",
        "report_mode": "direct", "facet": "preference", "anchors": [anchor(manifest)], **updates}


def group(manifest, group_id="one", **updates):
    return {"group_id": group_id, "claims": [claim(manifest)], **updates}


def apply(env, manifest, groups, event="event:1", **kwargs):
    return env[2].apply_groups(groups, manifest_id=manifest["manifest_id"], event_id=event,
                              audience="local:main", now=NOW, **kwargs)


def record_id(result, index=0):
    return result["groups"][index]["record_ids"][0]


def test_self_report_primary_is_original_not_assistant_paraphrase_and_reopen(env):
    manifest = freeze(env, "self")
    result = apply(env, manifest, [group(manifest, claims=[claim(manifest, statement="I chose rest_start.")])])
    assert result["accepted_count"] == 1
    identifier = record_id(result)
    with closing(env[0]._connect()) as db:
        entry = project_entry(db, identifier, "local:main", NOW)
        assert "我现在喜欢茶。" in entry["current_impression"]
        assert "rest_start" not in entry["current_impression"]
        assert entry["model_interpretation"] == "I chose rest_start."
        assert entry["semantic_status"] == "model_interpretation" and entry["quote_verified"] is True
        assert entry["confidence"] is None and entry["evidence_state"] == "support_only"
        assert db.execute("SELECT text FROM pm_records WHERE id=?", (identifier,)).fetchone()[0] == entry["current_impression"]
    reopened = ProductionMemoryStore(env[0].path)
    assert any(row["record_id"] == identifier for row in reopened.recall(audience="local:main", now=NOW)["entries"])
    next_manifest = freeze(env, "next")
    assert identifier in next_manifest["targets"]


def test_bad_independent_group_does_not_rollback_valid_and_rejection_durable(env):
    manifest = freeze(env, "separate")
    bad = claim(manifest, subject_id="user:not-shown")
    result = apply(env, manifest, [group(manifest, "good"), group(manifest, "bad", claims=[bad])])
    assert (result["accepted_count"], result["rejected_count"]) == (1, 1)
    assert result["groups"][1]["reason"] == "evidence_subject_not_authorized"
    task = EvidenceV2Store(ProductionMemoryStore(env[0].path)).pending_tasks(audience="local:main")
    assert len(task) == 1 and task[0]["group_id"] == "bad"
    assert env[2].pending_tasks(audience="qq:group:unrelated") == []


def test_group_is_atomic_and_dependency_failure_propagates_only_to_dependents(env):
    manifest = freeze(env, "dependencies")
    failed = group(manifest, "failed", claims=[claim(manifest, record_id="half"), claim(manifest, subject_id="unknown")])
    dependent = group(manifest, "dependent", depends_on=["failed"])
    result = apply(env, manifest, [dependent, failed, group(manifest, "independent")])
    assert [item["status"] for item in result["groups"]] == ["rejected", "rejected", "applied"]
    assert result["groups"][0]["reason"] == "evidence_dependency_rejected"
    with closing(env[0]._connect()) as db:
        assert db.execute("SELECT 1 FROM pm_records WHERE id='half'").fetchone() is None
        assert db.execute("SELECT count(*) FROM pm_sources").fetchone()[0] == 2


def test_dependency_cycle_and_unknown_dependency_have_durable_reasons(env):
    manifest = freeze(env, "cycle")
    result = apply(env, manifest, [group(manifest, "a", depends_on=["b"]), group(manifest, "b", depends_on=["a"]),
                                  group(manifest, "c", depends_on=["missing"])])
    assert result["rejected_count"] == 3
    assert {item["reason"] for item in result["groups"]} == {"evidence_dependency_cycle", "evidence_dependency_rejected"}


def test_retry_receipt_identical_and_changed_payload_conflicts(env):
    manifest = freeze(env, "replay")
    groups = [group(manifest)]
    result = apply(env, manifest, groups)
    assert apply(env, manifest, groups) == result
    with pytest.raises(ValueError, match="identity_conflict"):
        apply(env, manifest, [group(manifest, claims=[claim(manifest, statement="Changed")])])
    with closing(env[0]._connect()) as db:
        assert db.execute("SELECT count(*) FROM pm_v2_versions").fetchone()[0] == 1
        assert db.execute("SELECT event_seq FROM pm_v2_projection").fetchone()[0] == db.execute("SELECT max(seq) FROM pm_v2_events").fetchone()[0]


def test_schema_failure_is_separate_from_other_valid_groups(env):
    manifest = freeze(env, "schema")
    result = apply(env, manifest, [group(manifest, "good"), group(manifest, "bad", invented_authority="verified")])
    assert result["accepted_count"] == 1 and result["rejected_count"] == 1
    assert result["groups"][1]["reason"] == "evidence_group_schema_invalid"


def test_same_span_conflict_rejected_but_explicit_mixed_never_double_counts(env):
    manifest = freeze(env, "roles")
    conflicting = claim(manifest, anchors=[anchor(manifest), anchor(manifest, role="challenges")])
    mixed = claim(manifest, anchors=[anchor(manifest, role="mixed")], kind="interpretation", facet="other")
    result = apply(env, manifest, [group(manifest, "bad", claims=[conflicting]), group(manifest, "mixed", claims=[mixed])])
    assert result["groups"][0]["reason"] == "evidence_same_span_conflicting_roles"
    with closing(env[0]._connect()) as db:
        entry = project_entry(db, record_id(result, 1), "local:main", NOW)
        assert entry["evidence_state"] == "neither" and entry["needs_review"] is True


def test_same_span_different_targets_opposite_roles_allowed(env):
    manifest = freeze(env, "targets")
    first = claim(manifest, kind="interpretation", facet="event", statement="Judgment one")
    second = claim(manifest, kind="interpretation", facet="event", statement="Judgment two",
                   anchors=[anchor(manifest, role="challenges")])
    result = apply(env, manifest, [group(manifest, claims=[first, second])])
    assert result["accepted_count"] == 1
    with closing(env[0]._connect()) as db:
        states = [project_entry(db, identifier, "local:main", NOW)["evidence_state"] for identifier in result["groups"][0]["record_ids"]]
        assert states == ["support_only", "challenge_only"]


def test_duplicate_target_across_groups_cannot_be_sequentially_mutated(env):
    manifest = freeze(env, "duplicate")
    result = apply(env, manifest, [group(manifest, "one", claims=[claim(manifest, record_id="same")]),
                                  group(manifest, "two", claims=[claim(manifest, record_id="same")])])
    assert result["rejected_count"] == 2
    assert all(row["reason"] == "evidence_target_shared_across_groups" for row in result["groups"])


def test_two_frozen_writers_have_one_cas_winner_and_one_durable_rejection(env):
    initial = freeze(env, "initial")
    identifier = record_id(apply(env, initial, [group(initial, claims=[claim(initial, kind="interpretation", facet="other")])]))
    left, right = freeze(env, "left"), freeze(env, "right")
    def write(manifest, event):
        proposal = claim(manifest, record_id=identifier, expected_revision=1, revision_reason="Explicit reevaluation",
                         statement=event, kind="interpretation", facet="other")
        return apply(env, manifest, [group(manifest, claims=[proposal])], event)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: write(*args), [(left, "left"), (right, "right")]))
    assert sorted(item["accepted_count"] for item in results) == [0, 1]
    with closing(env[0]._connect()) as db:
        assert len(history(db, identifier)) == 2
    assert len(env[2].pending_tasks(audience="local:main")) == 1


def test_future_preference_and_withdrawal_do_not_resurrect_previous(env):
    old = freeze(env, "old")
    old_id = record_id(apply(env, old, [group(old)]))
    future = freeze(env, "future", ["b"])
    new = claim(future, valid_from=(NOW + timedelta(days=1)).isoformat(),
                supersedes=[{"record_id": old_id, "expected_revision": 1}])
    new_id = record_id(apply(env, future, [group(future, claims=[new])], "future-event"))
    with closing(env[0]._connect()) as db:
        assert is_visible(db, old_id, "local:main", NOW)
        assert not is_visible(db, new_id, "local:main", NOW)
        assert not is_visible(db, old_id, "local:main", NOW + timedelta(days=1))
        assert is_visible(db, new_id, "local:main", NOW + timedelta(days=1))
    env[0].retract_source("b", event_id="withdraw", reason="Synthetic withdrawal")
    with closing(env[0]._connect()) as db:
        assert not is_visible(db, old_id, "local:main", NOW + timedelta(days=2))
        assert not is_visible(db, new_id, "local:main", NOW + timedelta(days=2))
        assert history(db, new_id)[-1] == {"record_id": new_id, "revision": 1, "status": "source_withdrawn"}


def test_legacy_revision_is_unchanged_and_cannot_be_overwritten(env):
    memory = env[0]
    memory.revise(BeliefRevision(record_id="legacy", subject_id="user:a", topic="饮品", statement="Legacy",
        supporting_ids=("a",), revision=1, valid_from=NOW), event_id="legacy")
    before = memory.history("legacy")
    manifest = freeze(env, "legacy")
    result = apply(env, manifest, [group(manifest, claims=[claim(manifest, record_id="legacy", expected_revision=1,
                                                             revision_reason="Attempt")])])
    assert result["groups"][0]["reason"] == "evidence_cannot_rewrite_legacy_record"
    assert memory.history("legacy") == before


@pytest.mark.parametrize("field,value", [("confidence", .9), ("author_id", "user:a"), ("expected_revision", True)])
def test_typed_proposal_does_not_accept_authority_scores_or_bool_versions(env, field, value):
    manifest = freeze(env, "typed:" + field)
    with pytest.raises(ValidationError):
        ClaimProposal.model_validate({**claim(manifest), field: value})


def test_typed_offsets_and_group_shape_are_strict(env):
    manifest = freeze(env, "span")
    with pytest.raises(ValidationError):
        EvidenceAnchor.model_validate(anchor(manifest, start="0"))
    with pytest.raises(ValidationError):
        MemoryUpdateGroup(group_id="empty")


def test_task_disposition_requires_scope_and_accepted_replacement_and_is_idempotent(env):
    manifest = freeze(env, "task")
    bad = apply(env, manifest, [group(manifest, claims=[claim(manifest, subject_id="not-displayed")])])
    task_id = bad["groups"][0]["task_id"]
    with pytest.raises(ValueError, match="unavailable"):
        env[2].resolve_task(task_id, audience="other", event_id="wrong", resolution="acknowledged", reason="Review")
    with pytest.raises(ValueError, match="not_accepted"):
        env[2].resolve_task(task_id, audience="local:main", event_id="missing", resolution="replaced", reason="Review",
                            replacement_event_id="event:1", replacement_group_id="one")
    good = freeze(env, "replacement")
    apply(env, good, [group(good, "fixed")], "replacement")
    options = dict(audience="local:main", event_id="resolve", resolution="replaced", reason="Accepted new proposal",
                   replacement_event_id="replacement", replacement_group_id="fixed")
    receipt = env[2].resolve_task(task_id, **options)
    assert receipt["status"] == "replaced"
    assert env[2].resolve_task(task_id, **options) == receipt
    assert env[2].pending_tasks(audience="local:main") == []
    with pytest.raises(ValueError, match="identity_conflict"):
        env[2].resolve_task(task_id, **{**options, "reason": "Changed history"})
    with closing(env[0]._connect()) as db:
        assert db.execute("SELECT count(*) FROM pm_v2_tasks").fetchone()[0] == 1


def test_task_acknowledgement_does_not_claim_successful_repair(env):
    manifest = freeze(env, "acknowledge")
    rejected = apply(env, manifest, [group(manifest, claims=[claim(manifest, subject_id="unknown")])])
    result = env[2].resolve_task(rejected["groups"][0]["task_id"], audience="local:main", event_id="ack",
                                resolution="acknowledged", reason="Explicitly leave this interpretation unresolved")
    assert result["status"] == "acknowledged" and result["replacement_event_id"] is None
    with closing(env[0]._connect()) as db:
        assert db.execute("SELECT count(*) FROM pm_v2_versions").fetchone()[0] == 0


@pytest.mark.parametrize("change", [{"context": "work"}, {"facet": "character"}, {"topic": "other"},
                                    {"kind": "interpretation"}, {"report_mode": "ambiguous"},
                                    {"valid_from": (NOW + timedelta(days=1)).isoformat()},
                                    {"valid_from": (NOW - timedelta(days=1)).isoformat()},
                                    {"valid_until": (NOW + timedelta(days=1)).isoformat()}])
def test_same_id_temporal_or_context_change_rejected_without_hiding_current(env, change):
    first = freeze(env, "first")
    identifier = record_id(apply(env, first, [group(first)]))
    next_manifest = freeze(env, "revision", ["b"])
    revised = claim(next_manifest, record_id=identifier, expected_revision=1, revision_reason="Changed", **change)
    result = apply(env, next_manifest, [group(next_manifest, claims=[revised])], "revision")
    assert result["rejected_count"] == 1
    with closing(env[0]._connect()) as db:
        assert is_visible(db, identifier, "local:main", NOW)
        assert _latest_revision(db, identifier) == 1


def _latest_revision(db, identifier):
    return db.execute("SELECT revision FROM pm_records WHERE id=?", (identifier,)).fetchone()[0]


def test_same_id_revision_omitting_dates_preserves_original_interval(env):
    first = freeze(env, "first")
    initial = claim(first, valid_from=(NOW - timedelta(days=2)).isoformat(), valid_until=(NOW + timedelta(days=2)).isoformat())
    identifier = record_id(apply(env, first, [group(first, claims=[initial])]))
    next_manifest = freeze(env, "revision", ["a"])
    revised = claim(next_manifest, record_id=identifier, expected_revision=1, revision_reason="Interpretation corrected")
    result = apply(env, next_manifest, [group(next_manifest, claims=[revised])], "revision")
    assert result["accepted_count"] == 1
    with closing(env[0]._connect()) as db:
        versions = history(db, identifier)
        assert versions[0]["valid_from"] == versions[1]["valid_from"]
        assert versions[0]["valid_until"] == versions[1]["valid_until"]


def test_new_id_exact_overlapping_judgment_cannot_hide_old_challenge(env):
    first = freeze(env, "challenge")
    old = claim(first, record_id="old", kind="interpretation", facet="event", statement="  对方  喜欢茶  ",
                valid_from=(NOW - timedelta(days=2)).isoformat(), anchors=[anchor(first, role="challenges")])
    assert apply(env, first, [group(first, claims=[old])])["accepted_count"] == 1
    next_manifest = freeze(env, "duplicate", ["b"])
    new = claim(next_manifest, record_id="new", kind="interpretation", facet="event", statement="对方 喜欢茶",
                valid_from=(NOW - timedelta(days=1)).isoformat())
    result = apply(env, next_manifest, [group(next_manifest, claims=[new])], "duplicate")
    assert result["groups"][0]["reason"] == "existing_claim_requires_revision"
    with closing(env[0]._connect()) as db:
        assert project_entry(db, "old", "local:main", NOW)["evidence_state"] == "challenge_only"
        assert db.execute("SELECT 1 FROM pm_records WHERE id='new'").fetchone() is None


def test_nonoverlapping_interval_and_different_statement_or_context_remain_allowed(env):
    first = freeze(env, "old-interval")
    prior = claim(first, record_id="old", kind="interpretation", facet="event", statement="Same judgment",
                  valid_from=(NOW - timedelta(days=1)).isoformat(), valid_until=(NOW + timedelta(days=1)).isoformat())
    apply(env, first, [group(first, claims=[prior])])
    next_manifest = freeze(env, "new-interval")
    new = claim(next_manifest, record_id="new", kind="interpretation", facet="event", statement="Same judgment",
                valid_from=(NOW + timedelta(days=1)).isoformat())
    another_context = {**new, "record_id": "context", "context": "at work", "valid_from": NOW.isoformat()}
    another_statement = {**new, "record_id": "statement", "statement": "Different judgment", "valid_from": NOW.isoformat()}
    result = apply(env, next_manifest, [group(next_manifest, claims=[new, another_context, another_statement])], "new-interval")
    assert result["accepted_count"] == 1


def test_exact_key_does_not_use_private_challenge_to_block_public_claim(env):
    memory = env[0]
    memory.ingest([source("secret", audience="qq:private:1:2")])
    private_views = env[1].prepare_views(["secret"], audience="qq:private:1:2")
    private = env[1].issue({"evidence_views": private_views}, audience="qq:private:1:2", command_id="private")
    update = claim(private, kind="interpretation", facet="event", statement="Same judgment", anchors=[anchor(private, role="challenges")])
    env[2].apply_groups([group(private, claims=[update])], manifest_id=private["manifest_id"], event_id="private", audience="qq:private:1:2", now=NOW)
    public = freeze(env, "public")
    result = apply(env, public, [group(public, claims=[claim(public, kind="interpretation", facet="event", statement="Same judgment")])], "public")
    assert result["accepted_count"] == 1


@pytest.mark.parametrize("invalid_id", [None, 123, "x" * 161])
def test_schema_rejection_preserves_actual_raw_proposal_for_invalid_group_id(env, invalid_id):
    manifest = freeze(env, "bad-id:" + str(type(invalid_id)))
    bad = {"claims": [claim(manifest)], "marker": "original-invalid-proposal"}
    if invalid_id is not None:
        bad["group_id"] = invalid_id
    result = apply(env, manifest, [bad, group(manifest, "good")])
    assert result["accepted_count"] == 1 and result["rejected_count"] == 1
    assert result["groups"][0]["group_id"] == "invalid:0"
    with closing(env[0]._connect()) as db:
        stored = json.loads(db.execute("SELECT payload FROM pm_v2_tasks").fetchone()[0])
        assert stored == bad


def test_self_report_key_uses_original_meaning_not_identical_bad_paraphrase(env):
    first = freeze(env, "tea")
    old = record_id(apply(env, first, [group(first)]))
    env[0].ingest([source("coffee-later", text="我现在改喝咖啡。", independent="later").model_copy(
        update={"occurred_at": NOW + timedelta(hours=1)})])
    later = freeze(env, "coffee", ["coffee-later"], now=NOW + timedelta(hours=1))
    receipt = env[2].apply_groups([group(later)], manifest_id=later["manifest_id"], event_id="coffee",
        audience="local:main", now=NOW + timedelta(hours=1))
    assert receipt["accepted_count"] == 1
    with closing(env[0]._connect()) as db:
        assert not is_visible(db, old, "local:main", NOW + timedelta(hours=1))
        assert "改喝咖啡" in project_entry(db, record_id(receipt), "local:main", NOW + timedelta(hours=1))["current_impression"]


def test_repeated_original_at_different_time_does_not_evade_self_report_key(env):
    first = freeze(env, "first")
    apply(env, first, [group(first)])
    env[0].ingest([source("repeat", text="我现在喜欢茶。").model_copy(update={"occurred_at": NOW + timedelta(hours=1)})])
    repeat = freeze(env, "repeat", ["repeat"], now=NOW + timedelta(hours=1))
    receipt = env[2].apply_groups([group(repeat, claims=[claim(repeat, statement="Different paraphrase")])],
        manifest_id=repeat["manifest_id"], event_id="repeat", audience="local:main", now=NOW + timedelta(hours=1))
    assert receipt["groups"][0]["reason"] == "existing_claim_requires_revision"
