"""Real SQLite authority tests for the optional GLiNER bridge; no model dependencies."""
from contextlib import closing
from datetime import datetime, timezone
import json

import pytest

from harness.core.models import MemoryScope
from harness.memory.evidence_manifest import EvidenceManifestStore
from harness.memory.production import BeliefRevision, Experience, ProductionMemoryStore
from tools.production_acceptance.gliner_bridge import load_extraction_view, verify_entity_candidates


NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
AUDIENCE = "qq:private:synthetic:alice"
TEXT = "前缀🙂小明说喜欢茶，小明现在喜欢咖啡。"


@pytest.fixture
def evidence(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    source = Experience(record_id="source:1", author_id="qq:synthetic:alice", text=TEXT,
        occurred_at=NOW, scope=MemoryScope(channel="qq", conversation_id="synthetic", visibility=[AUDIENCE]),
        source_refs=("original:1",), independent_key="independent:1",
        original={"record": {"authority": "speaker_utterance"}})
    memory.ingest([source])
    manifests = EvidenceManifestStore(memory)
    return memory, manifests, source


def issue(evidence, *, crop=None):
    memory, manifests, _ = evidence
    if crop is None:
        views = manifests.prepare_views(["source:1"], audience=AUDIENCE)
    else:
        with closing(memory._connect()) as db:
            source, revision, digest = manifests._source(db, "source:1", AUDIENCE)
        views = [manifests._view(source, revision, digest, *crop).model_dump(mode="json")]
    manifest = manifests.issue({"evidence_views": views}, audience=AUDIENCE,
        command_id="command:extract", runtime_epoch=2, now=NOW)
    return manifest["manifest_id"], views[0]["unit_id"]


def extraction(text="小明", start=3, end=5, confidence=0.9, label="person_mention", **extra):
    return {"entities": {label: [{"text": text, "start": start, "end": end,
        "confidence": confidence, **extra}]}}


def verify(evidence, ids, payload=None, **kwargs):
    return verify_entity_candidates(evidence[1], *ids, extraction() if payload is None else payload,
        audience=AUDIENCE, now=NOW, **kwargs)


def snapshot(memory):
    with closing(memory._connect()) as db:
        return "\n".join(db.iterdump())


def test_exact_model_input_comes_from_durable_manifest_and_reopens(evidence):
    memory, _, _ = evidence
    ids = issue(evidence)
    reopened = EvidenceManifestStore(ProductionMemoryStore(memory.path))
    view = load_extraction_view(reopened, *ids, audience=AUDIENCE,
        command_id="command:extract", runtime_epoch=2, now=NOW)
    assert view == {"manifest_id": ids[0], "unit_id": ids[1], "text": TEXT,
        "base_offset": 0, "offset_encoding": "unicode_codepoint"}
    result = verify_entity_candidates(reopened, *ids, extraction(), audience=AUDIENCE, now=NOW)
    assert result["candidates"][0]["anchor"]["quote"] == "小明"


def test_only_quote_is_verified_label_and_score_are_not_claims_or_identities(evidence):
    ids = issue(evidence)
    result = verify(evidence, ids, extraction(label="reliable_person", confidence=1,
        subject_id="qq:invented:person", supporting_ids=["invented"], verified=True))
    candidate = result["candidates"][0]
    assert candidate == {
        "anchor": {"manifest_id": ids[0], "unit_id": ids[1], "start": 3, "end": 5,
            "quote": "小明", "canonical_source_id": "source:1", "source_revision": 0,
            "source_author_id": "qq:synthetic:alice"},
        "model_interpretation": {"label": "reliable_person", "confidence": 1.0},
    }
    assert result["trust_boundary"]["verified"] == "exact_displayed_quote_only"
    assert result["trust_boundary"]["semantic_support_or_counterevidence"] == "not_verified"
    assert result["trust_boundary"]["identity_binding"] == "none"
    assert "subject_id" not in json.dumps(result)


def test_emoji_uses_unicode_codepoints_not_utf16_or_bytes(evidence):
    ids = issue(evidence)
    good = verify(evidence, ids, extraction(text="🙂", start=2, end=3))
    assert good["candidates"][0]["anchor"]["quote"] == "🙂"
    for start, end in ((2, 4), (6, 10)):
        bad = verify(evidence, ids, extraction(text="🙂", start=start, end=end))
        assert bad["candidates"] == []
        assert bad["rejected"][0]["code"] == "gliner_candidate_quote_mismatch"


def test_duplicate_text_retains_distinct_exact_spans_without_search(evidence):
    ids = issue(evidence)
    occurrences = [index for index in range(len(TEXT)) if TEXT.startswith("小明", index)]
    result = verify(evidence, ids, {"entities": {"person": [
        {"text": "小明", "start": start, "end": start + 2, "confidence": 0.8} for start in occurrences]}})
    assert [item["anchor"]["start"] for item in result["candidates"]] == occurrences
    mismatch = verify(evidence, ids, extraction(start=4, end=6))
    assert mismatch["candidates"] == []
    assert mismatch["rejected"][0]["code"] == "gliner_candidate_quote_mismatch"


def test_cropped_view_adds_base_offset_and_never_exposes_hidden_source(evidence):
    ids = issue(evidence, crop=(3, 10))
    shown = load_extraction_view(evidence[1], *ids, audience=AUDIENCE, now=NOW)
    assert shown["text"] == TEXT[3:10] and shown["base_offset"] == 3
    result = verify(evidence, ids, extraction(start=0, end=2))
    assert result["candidates"][0]["anchor"]["start"] == 3
    assert result["candidates"][0]["anchor"]["end"] == 5
    assert "前缀" not in json.dumps(result, ensure_ascii=False)
    hidden = verify(evidence, ids, extraction(text="🙂", start=-1, end=0))
    assert hidden["candidates"] == []
    assert hidden["rejected"][0]["code"] == "gliner_candidate_span_invalid"
    # Source-absolute offsets are not silently interpreted as excerpt-relative.
    absolute = verify(evidence, ids, extraction(start=3, end=5))
    assert absolute["candidates"] == []


@pytest.mark.parametrize("start,end", [(True, 5), (3, False), (3.0, 5), (3, 5.0),
    ("3", 5), (None, 5), (-1, 5), (5, 3), (3, 3), (3, 100000)])
def test_invalid_indices_are_rejected_without_coercion(evidence, start, end):
    result = verify(evidence, issue(evidence), extraction(start=start, end=end))
    assert result["candidates"] == []
    assert result["rejected"] == [{"label_index": 0, "entity_index": 0, "code": "gliner_candidate_span_invalid"}]


@pytest.mark.parametrize("confidence", [True, False, "0.9", None, float("nan"), float("inf"),
    -float("inf"), -0.1, 1.01, 10**1000])
def test_invalid_confidence_cannot_become_truth_confidence(evidence, confidence):
    result = verify(evidence, issue(evidence), extraction(confidence=confidence))
    assert result["candidates"] == []
    assert result["rejected"][0]["code"] == "gliner_candidate_confidence_invalid"


@pytest.mark.parametrize("candidate", [None, "小明", {}, {"text": "小明", "start": 3, "end": 5}])
def test_malformed_entity_is_rejected_with_safe_diagnostic(evidence, candidate):
    result = verify(evidence, issue(evidence), {"entities": {"person": [candidate]}})
    assert result["candidates"] == [] and len(result["rejected"]) == 1
    assert set(result["rejected"][0]) == {"label_index", "entity_index", "code"}


@pytest.mark.parametrize("payload", [None, [], {}, {"entities": []}, {"entities": {"person": {}}},
    {"entities": {"": []}}, {"entities": {1: []}}, {"entities": {"x" * 161: []}}])
def test_invalid_extraction_envelope_is_rejected(evidence, payload):
    ids = issue(evidence)
    with pytest.raises(ValueError, match="gliner_entities_shape_invalid"):
        verify_entity_candidates(evidence[1], *ids, payload, audience=AUDIENCE)


def test_good_candidates_survive_individual_bad_spans_without_diagnostic_text_leak(evidence):
    ids = issue(evidence)
    item = extraction()["entities"]["person_mention"][0]
    payload = {"entities": {"person": [item, {**item, "text": "private sentinel"}]}}
    result = verify(evidence, ids, payload)
    assert len(result["candidates"]) == len(result["rejected"]) == 1
    assert "private sentinel" not in json.dumps(result)


@pytest.mark.parametrize("operation", ["load", "verify"])
def test_wrong_audience_is_rejected_by_real_resolver(evidence, operation):
    ids = issue(evidence)
    with pytest.raises(ValueError, match="evidence_manifest_context_mismatch"):
        if operation == "load":
            load_extraction_view(evidence[1], *ids, audience="qq:group:public", now=NOW)
        else:
            verify_entity_candidates(evidence[1], *ids, extraction(), audience="qq:group:public", now=NOW)


@pytest.mark.parametrize("change,expected", [("retract", "evidence_source_unavailable"),
    ("delete", "evidence_source_unavailable"), ("permission", "evidence_source_unavailable"),
    ("revision", "evidence_source_version_changed")])
def test_changed_live_authority_after_model_input_fails_closed(evidence, change, expected):
    memory, manifests, _ = evidence
    ids = issue(evidence)
    load_extraction_view(manifests, *ids, audience=AUDIENCE, now=NOW)
    if change == "retract":
        memory.retract_source("source:1", event_id="retract:1", reason="withdrawn")
    elif change == "delete":
        memory.delete_source("source:1", event_id="delete:1")
    else:
        with closing(memory._connect()) as db, db:
            if change == "permission":
                db.execute("DELETE FROM pm_visibility WHERE id='source:1'")
            else:
                db.execute("UPDATE pm_records SET revision=revision+1 WHERE id='source:1'")
    with pytest.raises(ValueError, match=expected):
        verify(evidence, ids)
    with pytest.raises(ValueError, match=expected):
        load_extraction_view(manifests, *ids, audience=AUDIENCE, now=NOW)


@pytest.mark.parametrize("empty", [True, False])
def test_unknown_unit_is_rejected_even_with_empty_candidates(evidence, empty):
    manifest_id, _ = issue(evidence)
    with pytest.raises(ValueError, match="evidence_unit_not_displayed"):
        verify(evidence, (manifest_id, "unit:unknown"), {"entities": {}} if empty else extraction())


@pytest.mark.parametrize("context,expected", [({"command_id": "different"}, "command_mismatch"),
    ({"runtime_epoch": 3}, "epoch_mismatch")])
def test_manifest_context_is_preserved(evidence, context, expected):
    with pytest.raises(ValueError, match=expected):
        verify(evidence, issue(evidence), **context)


def test_candidate_and_label_budgets_are_bounded_before_any_partial_output(evidence):
    ids = issue(evidence)
    item = extraction()["entities"]["person_mention"][0]
    with pytest.raises(ValueError, match="exceed_candidate_budget"):
        verify(evidence, ids, {"entities": {"person": [item, item]}}, max_candidates=1)
    with pytest.raises(ValueError, match="exceed_label_budget"):
        verify(evidence, ids, {"entities": {"person": [], "preference": []}}, max_labels=1)
    with pytest.raises(ValueError, match="extraction_text_exceeds_budget"):
        verify(evidence, ids, max_characters=2)
    with pytest.raises(ValueError, match="extraction_text_exceeds_budget"):
        load_extraction_view(evidence[1], *ids, audience=AUDIENCE, max_characters=2)


@pytest.mark.parametrize("kwargs", [{"max_candidates": True}, {"max_candidates": 0}, {"max_candidates": 513},
    {"max_labels": 129}, {"max_labels": 1.0}, {"max_characters": 100001}, {"max_characters": False}])
def test_budget_options_are_strict(evidence, kwargs):
    with pytest.raises(ValueError, match="candidate_budget_invalid"):
        verify(evidence, issue(evidence), **kwargs)


def test_bridge_is_read_only_and_repetition_does_not_change_history_or_confidence(evidence):
    memory, manifests, _ = evidence
    memory.revise(BeliefRevision(record_id="old-belief", subject_id="qq:synthetic:alice", topic="饮品",
        statement="先前的解释", revision=1, supporting_ids=("source:1",), valid_from=NOW), event_id="belief:1")
    ids = issue(evidence)
    before = snapshot(memory)
    first = verify(evidence, ids)
    assert first == verify(evidence, ids)
    load_extraction_view(manifests, *ids, audience=AUDIENCE, now=NOW)
    assert snapshot(memory) == before
    # The original source remains immutable even after an extraction "confidently" labels it.
    changed = evidence[2].model_copy(update={"text": "模型改写的过去"})
    with pytest.raises(ValueError, match="immutable_experience_changed"):
        memory.ingest([changed])
    assert snapshot(memory) == before


def test_bridge_rechecks_every_accepted_anchor_using_read_only_transaction(evidence, monkeypatch):
    ids = issue(evidence)
    manifests = evidence[1]
    original = manifests.resolve_anchor
    calls = []

    def traced(db, *args, **kwargs):
        assert db.in_transaction
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        calls.append((args[2:5], kwargs.get("now")))
        return original(db, *args, **kwargs)

    monkeypatch.setattr(manifests, "resolve_anchor", traced)
    verify(evidence, ids)
    assert calls == [((0, len(TEXT), TEXT), NOW), ((3, 5, "小明"), NOW)]
