"""A direct self-report keeps its original utterances across reinterpretation."""
from contextlib import closing
from datetime import timedelta

import pytest

from harness.memory.evidence_manifest import EvidenceManifestStore
from harness.memory.evidence_v2 import EvidenceV2Store, history, is_visible, project_entry
from harness.memory.production import ProductionMemoryStore
from tests.test_evidence_manifest import NOW, source
from tests.test_evidence_v2 import anchor, claim, freeze, group


@pytest.fixture
def env(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "provenance.sqlite3")
    coffee = source("coffee", text="我现在喜欢咖啡。").model_copy(update={"occurred_at": NOW - timedelta(days=2)})
    memory.ingest([coffee, source("tea", text="我现在改喝茶。"), source("other", text="我最近喜欢徒步。")])
    return memory, EvidenceManifestStore(memory), EvidenceV2Store(memory)


def apply(env, manifest, groups, event):
    return env[2].apply_groups(groups, manifest_id=manifest["manifest_id"], event_id=event,
                              audience="local:main", now=NOW)


def create(env, refs=("coffee",)):
    manifest = freeze(env, "initial", refs)
    initial = claim(manifest, record_id="drink", statement="原话的初次解释",
                    anchors=[anchor(manifest, index) for index in range(len(manifest["views"]))])
    assert apply(env, manifest, [group(manifest, claims=[initial])], "initial")["accepted_count"] == 1


@pytest.mark.parametrize("old_refs,new_refs", [
    (("coffee",), ("tea",)),
    (("coffee",), ("coffee", "tea")),
    (("coffee", "tea"), ("tea",)),
])
def test_utterance_replacement_addition_or_removal_rejected_durably(env, old_refs, new_refs):
    create(env, old_refs)
    with closing(env[0]._connect()) as db:
        before = project_entry(db, "drink", "local:main", NOW)
        prior_timeline = tuple(db.execute("SELECT * FROM pm_v2_timeline WHERE id='drink'").fetchone())
    manifest = freeze(env, "new-report", new_refs)
    proposed = claim(manifest, record_id="drink", expected_revision=1, revision_reason="新发言",
                     valid_from=None, anchors=[anchor(manifest, i) for i in range(len(manifest["views"]))])
    groups = [group(manifest, claims=[proposed])]
    result = apply(env, manifest, groups, "rejected-report")
    assert result["accepted_count"] == 0
    assert result["groups"][0]["reason"] == "evidence_direct_report_utterances_changed"
    with closing(env[0]._connect()) as db:
        assert project_entry(db, "drink", "local:main", NOW) == before
        assert tuple(db.execute("SELECT * FROM pm_v2_timeline WHERE id='drink'").fetchone()) == prior_timeline
        assert len(history(db, "drink")) == 1
        assert db.execute("SELECT count(*) FROM pm_sources").fetchone()[0] == 3
    reopened = EvidenceV2Store(ProductionMemoryStore(env[0].path))
    assert reopened.apply_groups(groups, manifest_id=manifest["manifest_id"], event_id="rejected-report",
                                 audience="local:main", now=NOW) == result
    assert len(reopened.pending_tasks(audience="local:main")) == 1


@pytest.mark.parametrize("same_root", [False, True])
def test_repeated_text_with_new_origin_or_time_is_not_same_utterance(env, same_root):
    create(env)
    repeated = source("repeat", text="我现在喜欢咖啡。", independent="coffee" if same_root else "repeat")
    # A later occurrence is not an alias of the earlier utterance, even when a
    # caller reuses its independent key. Ingestion only binds that key to text/author.
    if not same_root:
        repeated = repeated.model_copy(update={"occurred_at": NOW - timedelta(days=2)})
    env[0].ingest([repeated])
    manifest = freeze(env, "repeated", ["repeat"])
    proposal = claim(manifest, record_id="drink", expected_revision=1, revision_reason="同样措辞的新发言")
    result = apply(env, manifest, [group(manifest, claims=[proposal])], "repeated")
    assert result["groups"][0]["reason"] == "evidence_direct_report_utterances_changed"


def test_same_original_alias_and_new_span_role_interpretation_can_revise(env):
    create(env)
    alias = source("capture-alias", text="我现在喜欢咖啡。", independent="coffee").model_copy(
        update={"occurred_at": NOW - timedelta(days=2)})
    env[0].ingest([alias])
    manifest = freeze(env, "reinterpreted", ["capture-alias"])
    view = manifest["views"][0]
    start = view["text"].index("咖啡")
    proposal = claim(manifest, record_id="drink", expected_revision=1, revision_reason="重新解释同一原话",
                     statement="该原话只说明当时偏好", anchors=[anchor(manifest, start=start, end=start + 2,
                         quote="咖啡", role="qualifies", rationale="仍指向完整原始发言")])
    result = apply(env, manifest, [group(manifest, claims=[proposal])], "reinterpreted")
    assert result["accepted_count"] == 1
    with closing(env[0]._connect()) as db:
        entry = project_entry(db, "drink", "local:main", NOW)
        assert entry["revision"] == 2 and entry["model_interpretation"] == "该原话只说明当时偏好"
        assert entry["valid_from"] == (NOW - timedelta(days=2)).isoformat()
        assert "我现在喜欢咖啡。" in entry["current_impression"]
        assert entry["quotations"][0]["quote"] == "咖啡"
        assert len(history(db, "drink")) == 2


def test_new_report_with_new_id_and_supersession_preserves_correct_start(env):
    create(env)
    manifest = freeze(env, "new-report", ["tea"])
    proposal = claim(manifest, record_id="new-drink", statement="新发言说现在改喝茶",
                     supersedes=[{"record_id": "drink", "expected_revision": 1}])
    result = apply(env, manifest, [group(manifest, claims=[proposal])], "new-report")
    assert result["accepted_count"] == 1
    with closing(env[0]._connect()) as db:
        yesterday = NOW - timedelta(days=1)
        assert is_visible(db, "drink", "local:main", yesterday)
        assert not is_visible(db, "new-drink", "local:main", yesterday)
        assert not is_visible(db, "drink", "local:main", NOW)
        entry = project_entry(db, "new-drink", "local:main", NOW)
        assert entry["valid_from"] == entry["quotations"][0]["occurred_at"] == NOW.isoformat()
        assert "我现在改喝茶。" in entry["current_impression"]
        assert len(history(db, "drink")) == 1


def test_rejected_report_group_does_not_discard_independent_valid_interpretation(env):
    create(env)
    manifest = freeze(env, "two-groups", ["tea"])
    replacement = claim(manifest, record_id="drink", expected_revision=1, revision_reason="新发言")
    valid = claim(manifest, record_id="independent", kind="interpretation", facet="event", topic="新经历",
                  statement="对这次新发言的独立暂定解释")
    result = apply(env, manifest, [group(manifest, "bad", claims=[replacement]),
                                  group(manifest, "good", claims=[valid])], "two-groups")
    assert (result["accepted_count"], result["rejected_count"]) == (1, 1)
    with closing(env[0]._connect()) as db:
        assert project_entry(db, "independent", "local:main", NOW) is not None
        assert project_entry(db, "drink", "local:main", NOW)["revision"] == 1
