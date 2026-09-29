"""Actual SQLite manifests over synthetic immutable sources; no model calls."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json

import pytest

from harness.core.models import MemoryScope
from harness.memory.evidence_manifest import EvidenceManifestStore
from harness.memory.production import BeliefRevision, Experience, ProductionMemoryStore


NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


def source(identifier="raw-a", *, author="user:a", text="我现在喝茶。", independent=None,
           audience="local:main", kind="observation", refs=None, authority="speaker_utterance"):
    return Experience(record_id=identifier, author_id=author, text=text, occurred_at=NOW,
        scope=MemoryScope(channel="chat", conversation_id="main", visibility=[audience]),
        source_refs=refs or ("alias:" + identifier,), independent_key=independent or identifier,
        kind=kind, original={"record": {"authority": authority}})


@pytest.fixture
def stores(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    memory.ingest([source(), source("raw-b", author="user:b", text="甲说我喜欢咖啡。"),
                   source("private", audience="qq:private:1:2")])
    return memory, EvidenceManifestStore(memory)


def issue(manifests, refs=("raw-a",), **kwargs):
    views = manifests.prepare_views(refs, audience="local:main")
    return manifests.issue({"evidence_views": views}, audience="local:main", command_id="command:1", **kwargs)


def anchor(manifests, manifest, *, view_index=0, audience="local:main", **kwargs):
    view = manifest["views"][view_index]
    values = {"start": view["start"], "end": view["end"], "quote": view["text"], **kwargs}
    with closing(manifests.memory._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        return manifests.resolve_anchor(db, manifest["manifest_id"], view["unit_id"], audience=audience, **values)


def recall_payload(memory):
    recall = memory.recall(audience="local:main", now=NOW)
    return {"memories": [{"source_ids": [entry["record_id"] for entry in recall["entries"]],
        "text": json.dumps({"derived_memory": recall}, ensure_ascii=False)}]}


def test_durable_manifest_resolves_exact_original_after_reopen(stores):
    memory, manifests = stores
    manifest = issue(manifests, runtime_epoch=2, subjects=["authenticated:current"])
    reopened = EvidenceManifestStore(ProductionMemoryStore(memory.path))
    assert reopened.get(manifest["manifest_id"], audience="local:main") == manifest
    unit = anchor(reopened, manifest)
    assert unit["quote"] == unit["text"] == "我现在喝茶。"
    assert unit["author_id"] == "user:a"
    assert unit["source_id"] == "raw-a" and unit["revision"] == 0
    assert "authored_utterance" in unit["allowed_uses"]
    assert manifest["subjects"] == ["authenticated:current", "user:a"]


def test_belief_summary_ids_never_expose_original_quotations(stores):
    memory, manifests = stores
    memory.revise(BeliefRevision(record_id="belief", subject_id="user:a", topic="饮品", statement="喜欢茶",
        revision=1, supporting_ids=("raw-a",), valid_from=NOW), event_id="belief:1")
    assert manifests.prepare_views(["belief"], audience="local:main") == []
    payload = recall_payload(memory)
    payload["source_refs"] = ["raw-a"]
    manifest = manifests.issue(payload, audience="local:main", command_id="recall-only")
    assert manifest["views"] == []
    assert "belief" in manifest["targets"]
    with closing(memory._connect()) as db:
        with pytest.raises(ValueError, match="not_displayed"):
            manifests.resolve_anchor(db, manifest["manifest_id"], "raw-a", 0, 6, "我现在喝茶。", audience="local:main")


def test_final_pruning_removes_authority_even_if_source_is_audience_visible(stores):
    _, manifests = stores
    views = manifests.prepare_views(["raw-a", "raw-b"], audience="local:main")
    manifest = manifests.issue({"evidence_views": views[:1], "source_refs": ["raw-b"]},
        audience="local:main", command_id="pruned")
    with closing(manifests.memory._connect()) as db:
        with pytest.raises(ValueError, match="not_displayed"):
            manifests.resolve_anchor(db, manifest["manifest_id"], views[1]["unit_id"], 0, 2, "甲说", audience="local:main")
    assert manifest["subjects"] == ["user:a"]


@pytest.mark.parametrize("changed", ["text", "author_id", "authority", "start", "source_revision", "allowed_uses"])
def test_tampered_view_cannot_grant_authority(stores, changed):
    _, manifests = stores
    view = manifests.prepare_views(["raw-a"], audience="local:main")[0]
    replacements = {"text": "伪造内容", "author_id": "user:b", "authority": "verified_action_outcome",
                    "start": 1, "source_revision": 9, "allowed_uses": ["verified_fact"]}
    view[changed] = replacements[changed]
    with pytest.raises(ValueError, match="evidence_view_source_mismatch"):
        manifests.issue({"evidence_views": [view]}, audience="local:main", command_id="tampered")


def test_quote_offsets_are_unicode_codepoints_not_utf8_or_utf16(stores):
    memory, manifests = stores
    memory.ingest([source("emoji", text="甲🙂喝茶")])
    manifest = issue(manifests, ["emoji"])
    assert anchor(manifests, manifest, start=1, end=2, quote="🙂")["quote"] == "🙂"
    with pytest.raises(ValueError, match="quote_not_displayed"):
        anchor(manifests, manifest, start=1, end=3, quote="🙂")


def test_partial_original_view_does_not_authorize_hidden_prefix(stores):
    memory, manifests = stores
    with closing(memory._connect()) as db:
        item, revision, digest = manifests._source(db, "raw-a", "local:main")
    view = manifests._view(item, revision, digest, 2, len(item.text)).model_dump(mode="json")
    manifest = manifests.issue({"evidence_views": [view]}, audience="local:main", command_id="partial")
    assert anchor(manifests, manifest)["quote"] == item.text[2:]
    with pytest.raises(ValueError, match="quote_not_displayed"):
        anchor(manifests, manifest, start=0, end=2, quote=item.text[:2])


def test_source_retraction_between_prompt_and_commit_rejects(stores):
    memory, manifests = stores
    manifest = issue(manifests)
    memory.retract_source("raw-a", event_id="retract", reason="withdrawn")
    with pytest.raises(ValueError, match="source_unavailable"):
        anchor(manifests, manifest)
    assert manifests.get(manifest["manifest_id"], audience="local:main")["views"]


def test_source_revision_changed_between_prompt_and_commit_rejects(stores):
    memory, manifests = stores
    manifest = issue(manifests)
    with closing(memory._connect()) as db, db:
        db.execute("UPDATE pm_records SET revision=revision+1 WHERE id='raw-a'")
    with pytest.raises(ValueError, match="source_version_changed"):
        anchor(manifests, manifest)


def test_private_legacy_and_ambiguous_sources_have_no_quote_grants(stores):
    memory, manifests = stores
    memory.ingest([source("legacy", kind="legacy_episode", refs=("raw-a",)),
        source("alias-a", refs=("ambiguous",)), source("alias-b", author="user:b", refs=("ambiguous",))])
    assert manifests.prepare_views(["private", "legacy", "ambiguous", "missing"], audience="local:main") == []


def test_duplicate_captures_and_aliases_deduplicate_provenance(stores):
    memory, manifests = stores
    memory.ingest([source("duplicate", independent="raw-a", refs=("duplicate-alias",))])
    views = manifests.prepare_views(["raw-a", "alias:raw-a", "duplicate"], audience="local:main")
    assert len(views) == 1
    first = issue(manifests, ["raw-a"])
    second = manifests.issue({"evidence_views": manifests.prepare_views(["duplicate"], audience="local:main")},
        audience="local:main", command_id="duplicate-command")
    assert anchor(manifests, first)["canonical_unit_key"] == anchor(manifests, second)["canonical_unit_key"]


def test_alias_changes_never_rebind_a_persisted_canonical_source(stores):
    memory, manifests = stores
    manifest = issue(manifests, ["alias:raw-a"])
    with closing(memory._connect()) as db, db:
        db.execute("DELETE FROM pm_source_alias WHERE alias='alias:raw-a'")
        db.execute("INSERT INTO pm_source_alias VALUES('alias:raw-a','raw-b')")
    assert anchor(manifests, manifest)["source_id"] == "raw-a"


def test_self_expression_never_becomes_person_authored_utterance(stores):
    memory, manifests = stores
    memory.ingest([source("intent", author="character:misty", kind="self_expression", text="我将开始休息。")])
    manifest = issue(manifests, ["intent"])
    assert anchor(manifests, manifest)["allowed_uses"] == ["self_expression_context"]


def test_manifest_replay_exact_and_changed_payload_conflict(stores):
    _, manifests = stores
    manifest = issue(manifests, runtime_epoch=1)
    assert issue(manifests, runtime_epoch=1) == manifest
    with pytest.raises(ValueError, match="identity_conflict"):
        issue(manifests, ["raw-b"], runtime_epoch=1)


@pytest.mark.parametrize("kwargs,code", [({"audience": "other"}, "context_mismatch"),
    ({"command_id": "other"}, "command_mismatch"), ({"runtime_epoch": 8}, "epoch_mismatch"),
    ({"runtime_instance_id": "other"}, "runtime_mismatch"), ({"identity_revision": 9}, "identity_revision_mismatch")])
def test_manifest_context_fences(stores, kwargs, code):
    memory, manifests = stores
    manifest = issue(manifests, runtime_epoch=1, runtime_instance_id="runtime:1", identity_revision=2)
    with closing(memory._connect()) as db:
        with pytest.raises(ValueError, match=code):
            manifests.require_manifest(db, manifest["manifest_id"], **{"audience": "local:main", **kwargs})


def test_target_must_be_displayed_with_exact_revision_and_live_cas(stores):
    memory, manifests = stores
    memory.revise(BeliefRevision(record_id="belief", subject_id="user:a", topic="饮品", statement="喜欢茶",
        revision=1, supporting_ids=("raw-a",), valid_from=NOW), event_id="belief:1")
    manifest = manifests.issue(recall_payload(memory), audience="local:main", command_id="target")
    with closing(memory._connect()) as db:
        assert manifests.validate_target(db, manifest["manifest_id"], "belief", 1, audience="local:main")["revision"] == 1
        with pytest.raises(ValueError, match="not_displayed"):
            manifests.validate_target(db, manifest["manifest_id"], "private", 0, audience="local:main")
    memory.revise(BeliefRevision(record_id="belief", subject_id="user:a", topic="饮品", statement="仍喜欢茶",
        revision=2, supporting_ids=("raw-a",), valid_from=NOW), event_id="belief:2")
    with closing(memory._connect()) as db:
        with pytest.raises(ValueError, match="version_changed"):
            manifests.validate_target(db, manifest["manifest_id"], "belief", 1, audience="local:main")


def test_qq_single_entry_shape_and_subject_not_inferred_from_names(stores):
    memory, manifests = stores
    entry = next(item for item in memory.recall(audience="local:main", now=NOW)["entries"] if item["record_id"] == "raw-a")
    payload = {"memories": [{"source_ids": ["raw-a"], "text": json.dumps(entry)}],
               "mentioned_name": "new:user", "subjects": ["forged:subject"]}
    manifest = manifests.issue(payload, audience="local:main", command_id="qq-entry")
    assert manifest["subjects"] == ["user:a"]
    with closing(memory._connect()) as db:
        with pytest.raises(ValueError, match="subject_not_bound"):
            manifests.validate_subject(db, manifest["manifest_id"], "forged:subject", audience="local:main")


def test_tampered_durable_manifest_is_not_trusted(stores):
    memory, manifests = stores
    manifest = issue(manifests)
    with closing(memory._connect()) as db, db:
        db.execute("UPDATE pm_evidence_manifests SET payload=replace(payload,'user:a','user:b')")
    with pytest.raises(ValueError, match="integrity_failure"):
        manifests.get(manifest["manifest_id"], audience="local:main")


def test_commit_cannot_use_a_different_database_even_with_same_owner(stores, tmp_path):
    _, manifests = stores
    manifest = issue(manifests)
    other = ProductionMemoryStore(tmp_path / "other.sqlite3")
    EvidenceManifestStore(other)
    with closing(other._connect()) as db:
        with pytest.raises(ValueError, match="database_identity_mismatch"):
            manifests.require_manifest(db, manifest["manifest_id"], audience="local:main")


def test_source_selection_and_whole_quote_display_budget_are_bounded(stores):
    _, manifests = stores
    assert manifests.prepare_views(["raw-a"], audience="local:main", max_characters=1) == []
    with pytest.raises(ValueError, match="selection_exceeds_budget"):
        manifests.prepare_views(["raw-a"] * 513, audience="local:main")


def test_changed_target_text_or_omitted_revision_cannot_grant_update_target(stores):
    memory, manifests = stores
    entry = next(item for item in memory.recall(audience="local:main", now=NOW)["entries"] if item["record_id"] == "raw-a")
    for changed in ({**entry, "summary": "模型改写"}, {k: v for k, v in entry.items() if k != "revision"}):
        payload = {"memories": [{"source_ids": ["raw-a"], "text": json.dumps(changed)}]}
        with pytest.raises(ValueError, match="target_display_mismatch"):
            manifests.issue(payload, audience="local:main", command_id="bad-display")


def test_explicit_now_is_forwarded_for_manifest_and_live_target_checks(stores, monkeypatch):
    memory, manifests = stores
    original = memory._visible
    at = NOW + timedelta(days=5)
    observed = []

    def visible(db, identifier, audience, *, now=None):
        observed.append(now)
        return original(db, identifier, audience, now=now)

    monkeypatch.setattr(memory, "_visible", visible)
    payload = recall_payload(memory)
    payload["evidence_views"] = manifests.prepare_views(["raw-a"], audience="local:main", now=at)
    observed.clear()
    manifest = manifests.issue(payload, audience="local:main", command_id="controlled-time", now=at)
    assert observed and all(value == at for value in observed)
    with closing(memory._connect()) as db:
        observed.clear()
        manifests.validate_target(db, manifest["manifest_id"], "raw-a", 0, audience="local:main", now=at)
        assert observed == [at]
        view = manifest["views"][0]
        observed.clear()
        manifests.resolve_anchor(db, manifest["manifest_id"], view["unit_id"], view["start"], view["end"], view["text"],
            audience="local:main", now=at)
        assert observed == [at]


def test_future_claim_display_and_target_cas_use_same_explicit_time(stores):
    from harness.memory.evidence_v2 import EvidenceV2Store

    memory, manifests = stores
    initial = issue(manifests, now=NOW)
    view = initial["views"][0]
    future = datetime(2090, 1, 1, tzinfo=timezone.utc)
    claim = {"subject_id": "user:a", "topic": "future preference", "statement": "暂定偏好",
        "kind": "self_report", "report_mode": "direct", "facet": "preference", "valid_from": future.isoformat(),
        "anchors": [{"unit_id": view["unit_id"], "start": view["start"], "end": view["end"],
                     "quote": view["text"], "role": "supports", "rationale": "synthetic future-effective fixture"}]}
    receipt = EvidenceV2Store(memory).apply_groups([{"group_id": "future", "claims": [claim]}],
        manifest_id=initial["manifest_id"], event_id="future:1", audience="local:main", now=NOW)
    assert receipt["accepted_count"] == 1
    identifier = receipt["groups"][0]["record_ids"][0]
    recall = memory.recall(audience="local:main", now=future)
    payload = {"memories": [{"source_ids": [entry["record_id"] for entry in recall["entries"]],
                            "text": json.dumps({"derived_memory": recall})}]}
    shown = manifests.issue(payload, audience="local:main", command_id="future-view", now=future)
    with closing(memory._connect()) as db:
        assert manifests.validate_target(db, shown["manifest_id"], identifier, 1, audience="local:main", now=future)
        with pytest.raises(ValueError, match="target_unavailable"):
            manifests.validate_target(db, shown["manifest_id"], identifier, 1,
                                      audience="local:main", now=future - timedelta(seconds=1))
