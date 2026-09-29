"""Deterministic, offline checks for the isolated natural-language bridge."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
from pydantic import ValidationError

from harness.core.models import MemoryScope
from harness.memory.evidence_manifest import EvidenceManifestStore
from harness.memory.production import Experience, ProductionMemoryStore
from tools.production_acceptance.nl_gliner_contract import (
    DecisionRecord, JsonDecisionResponse, split_natural_response,
    validate_record, verify_extracted_record,
)


VALUES = {"action": "reply", "subject_id": "user:a", "report_mode": "direct", "evidence_id": "E1",
    "relation": "supports", "judgment": "对方自述现在喜欢茶。", "affect": "平静但保留判断。"}


def extracted(**changes):
    values = {**VALUES, **changes}
    notes, spans = "", {}
    for field, value in values.items():
        notes += field + "："
        spans[field] = {"text": value, "start": len(notes), "end": len(notes) + len(value), "confidence": .9}
        notes += value + "。\n"
    return notes, {"decision": [spans]}


@pytest.fixture
def evidence(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    source = Experience(record_id="original", author_id="user:a", text='隐藏前缀🫖我说：“我现在喜欢茶。”尾部',
        occurred_at=datetime(2026, 9, 23, tzinfo=timezone.utc), independent_key="source:original",
        source_refs=("message:original",),
        scope=MemoryScope(channel="chat", conversation_id="main", visibility=["local:main"]),
        original={"record": {"authority": "speaker_utterance"}})
    memory.ingest([source])
    manifests = EvidenceManifestStore(memory)
    whole = manifests.prepare_views(["original"], audience="local:main")[0]
    start, end = 5, len(source.text) - 2
    view = manifests._view(source, whole["source_revision"], whole["source_sha256"], start, end)
    manifest = manifests.issue({"evidence_views": [view.model_dump(mode="json")]},
        audience="local:main", command_id="test:1", subjects=["user:b"])
    return memory, manifests, manifest


def validate(evidence, record=None, **overrides):
    _, manifests, manifest = evidence
    kwargs = {"manifests": manifests, "manifest_id": manifest["manifest_id"],
        "aliases": {"E1": manifest["views"][0]["unit_id"]},
        "allowed_subjects": {"user:a", "user:b"}, "allowed_actions": {"reply", "wait"}, "audience": "local:main"}
    return validate_record(record or DecisionRecord(**VALUES), **{**kwargs, **overrides})


def test_reply_keeps_quotes_newlines_whitespace_and_is_not_extraction_input():
    reply = '\n  我记住了“茶”。\n他说 "action: rest_start"，那是引文。\\\n\t'
    notes, extraction = extracted(action="wait")
    split = split_natural_response(" \n【回复】" + reply + "【决策说明】" + notes)
    assert split.reply == reply
    assert split.notes == notes
    record = verify_extracted_record(extraction, split.notes)
    assert record.action == "wait"
    encoded = JsonDecisionResponse(reply=split.reply, decision=record).model_dump_json()
    assert json.loads(encoded)["reply"] == reply
    assert JsonDecisionResponse.model_validate_json(encoded).decision == record


@pytest.mark.parametrize("response", [
    "只是普通文本", "【回复】x", "【决策说明】x", "【决策说明】x【回复】y",
    "前缀【回复】x【决策说明】y", "【回复】x【回复】y【决策说明】z",
    "【回复】x【决策说明】y【决策说明】z", "【回复】 【决策说明】y",
    "【回复】x【决策说明】\n", '【回复】引用“【决策说明】”【决策说明】说明', None,
    "【回复】" + "x" * 16001 + "【决策说明】y",
    "【回复】x【决策说明】" + "y" * 12001,
])
def test_ambiguous_or_incomplete_envelope_rejects(response):
    with pytest.raises(ValueError, match="nl_section_"):
        split_natural_response(response)


def test_extraction_complete_record_and_unicode_spans():
    notes, extraction = extracted(affect="🫖平静")
    assert verify_extracted_record(extraction, notes).affect == "🫖平静"
    span = extraction["decision"][0]["affect"]
    span["end"] += 1  # A UTF-16 code-unit interpretation must not silently repair itself.
    with pytest.raises(ValueError, match="text_mismatch"):
        verify_extracted_record(extraction, notes)


@pytest.mark.parametrize("field", list(VALUES))
def test_every_field_is_mandatory_without_defaults(field):
    notes, extraction = extracted()
    del extraction["decision"][0][field]
    with pytest.raises(ValueError, match="fields_invalid"):
        verify_extracted_record(extraction, notes)


@pytest.mark.parametrize("extraction", [None, {}, {"decision": []}, {"decision": [{}] * 2},
    {"decision": {}}, {"decision": [None]}, {"decision": [{}], "reply": "injected"}])
def test_zero_multiple_or_foreign_records_reject(extraction):
    with pytest.raises(ValueError, match="nl_extraction_"):
        verify_extracted_record(extraction, "notes")


@pytest.mark.parametrize("score", [True, False, None, "0.9", float("nan"), float("inf"), -.1, 1.1])
def test_invalid_scores_reject(score):
    notes, extraction = extracted()
    extraction["decision"][0]["action"]["confidence"] = score
    with pytest.raises(ValueError, match="confidence_invalid"):
        verify_extracted_record(extraction, notes)


def test_below_threshold_rejects_and_threshold_is_inclusive():
    notes, extraction = extracted()
    extraction["decision"][0]["action"]["confidence"] = .5
    assert verify_extracted_record(extraction, notes).action == "reply"
    extraction["decision"][0]["action"]["confidence"] = .4999
    with pytest.raises(ValueError, match="below_threshold"):
        verify_extracted_record(extraction, notes)


@pytest.mark.parametrize("threshold", [True, "0.5", None, float("nan"), -1, 2])
def test_bad_threshold_rejects(threshold):
    notes, extraction = extracted()
    with pytest.raises(ValueError, match="threshold_invalid"):
        verify_extracted_record(extraction, notes, min_confidence=threshold)


@pytest.mark.parametrize("patch", [
    {"start": True}, {"start": -1}, {"end": 100000}, {"start": 99, "end": 3},
    {"start": 0.0}, {"text": "rest_start"}, {"source_id": "injected"},
])
def test_bad_spans_reject_without_repair(patch):
    notes, extraction = extracted()
    extraction["decision"][0]["action"].update(patch)
    with pytest.raises(ValueError, match="nl_extraction_"):
        verify_extracted_record(extraction, notes)


def test_list_field_is_ambiguous_even_when_it_has_one_candidate():
    notes, extraction = extracted()
    item = extraction["decision"][0]
    item["action"] = [item["action"]]
    with pytest.raises(ValueError, match="span_shape_invalid"):
        verify_extracted_record(extraction, notes)


@pytest.mark.parametrize("field,value", [("action", "send"), ("report_mode", "fact"),
    ("relation", "proves"), ("evidence_id", "original"), ("evidence_id", "E0"),
    ("subject_id", 12), ("judgment", ""), ("affect", None)])
def test_json_arm_and_record_enforce_identical_typed_contract(field, value):
    values = {**VALUES, field: value}
    with pytest.raises(ValidationError):
        DecisionRecord.model_validate(values)
    with pytest.raises(ValidationError):
        JsonDecisionResponse.model_validate({"reply": "回复", "decision": values})


def test_record_no_extra_fields_and_no_mutation():
    record = DecisionRecord(**VALUES)
    with pytest.raises(ValidationError):
        DecisionRecord(**VALUES, confidence=1)
    with pytest.raises(ValidationError):
        record.action = "rest_start"


def test_server_source_view_supplies_quote_not_generated_notes(evidence):
    notes, extraction = extracted(judgment="生成说明说的是另外一句话。")
    record = verify_extracted_record(extraction, notes)
    result = validate(evidence, record)
    view = evidence[2]["views"][0]
    binding = result["source_binding"]
    assert binding["quote"] == view["text"] != record.judgment
    assert binding["start"] == view["start"] == 5
    assert binding["end"] == view["end"]
    assert binding["author_id"] == "user:a"
    proposal = result["proposal"]["claims"][0]
    assert proposal["anchors"][0]["quote"] == binding["quote"]
    assert proposal["kind"] == "interpretation"
    assert proposal["report_mode"] == "direct"
    assert result["trust_boundary"]["self_report"] == "not_certified"
    assert not result["trust_boundary"]["notes_spans_are_original_evidence"]


def test_unknown_subject_remains_unbound(evidence):
    result = validate(evidence, DecisionRecord(**{**VALUES, "subject_id": "unknown"}))
    claim = result["proposal"]["claims"][0]
    assert claim["subject_id"] is None
    assert claim["subject_mention"] == "unknown"


@pytest.mark.parametrize("subject", ["user:foreign", "unknown"])
def test_unknown_or_known_subject_cannot_authorize_rest(evidence, subject):
    record = DecisionRecord(**{**VALUES, "subject_id": subject, "action": "rest_start"})
    with pytest.raises(ValueError, match="action_not_authorized"):
        validate(evidence, record)


def test_allowed_subject_must_also_be_displayed_in_this_manifest(evidence):
    record = DecisionRecord(**{**VALUES, "subject_id": "user:foreign"})
    with pytest.raises(ValueError, match="subject_not_authorized"):
        validate(evidence, record)
    with pytest.raises(ValueError, match="subject_not_bound"):
        validate(evidence, record, allowed_subjects={"user:foreign"})


def test_alias_has_no_authority_outside_current_manifest(evidence):
    with pytest.raises(ValueError, match="alias_unknown"):
        validate(evidence, aliases={"E2": evidence[2]["views"][0]["unit_id"]})
    with pytest.raises(ValueError, match="unit_not_displayed"):
        validate(evidence, aliases={"E1": "evidence-unit:foreign"})


def test_wrong_audience_and_withdrawn_source_reject(evidence):
    with pytest.raises(ValueError, match="context_mismatch"):
        validate(evidence, audience="qq:group:other")
    evidence[0].retract_source("original", event_id="withdraw", reason="withdrawn")
    with pytest.raises(ValueError, match="source_unavailable"):
        validate(evidence)


def test_revised_source_rejects_stale_manifest(evidence):
    with closing(evidence[0]._connect()) as db, db:
        db.execute("UPDATE pm_records SET revision=revision+1 WHERE id='original'")
    with pytest.raises(ValueError, match="source_version_changed"):
        validate(evidence)


def test_repeating_checks_has_no_persistent_effect_and_does_not_certify_meaning(evidence):
    def snapshot():
        with closing(evidence[0]._connect()) as db:
            return list(db.iterdump())
    before = snapshot()
    record = DecisionRecord(**{**VALUES, "judgment": "无法从该原文证明的推断。", "relation": "challenges"})
    first = validate(evidence, record)
    assert first == validate(evidence, record)
    assert snapshot() == before
    assert first["trust_boundary"]["semantic_relation"] == "not_certified"
    assert first["trust_boundary"]["memory_writes"] is False
    assert first["trust_boundary"]["action_executed"] is False


def test_input_structures_are_not_mutated(evidence):
    notes, extraction = extracted()
    original = deepcopy(extraction)
    record = verify_extracted_record(extraction, notes)
    validate(evidence, record)
    assert extraction == original
