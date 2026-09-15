"""Compact critic evidence must preserve attribution and every admitted text."""
from __future__ import annotations

import json

import pytest

from harness.config import Settings
from harness.core.inference import CandidateResponse, MemoryEvidenceContext, MemoryEvidenceEntry
from harness.validator.evidence_projection import COMPACT_EVIDENCE_FORMAT, project_evidence
from harness.validator.validator import ResponseValidator


SEMANTIC_FIELDS = (
    "text", "audience", "source_channel", "source_conversation_id",
    "speaker_id", "observed_at", "status", "kind",
)


def _entry(**updates):
    values = {
        "source_id": "private-source:" + "source_hash_" * 30,
        "text": "第一行：完整正文。\n第二行：保留引号\"和 emoji 🌧️。",
        "audience": "qq:group:private-current-group",
        "source_channel": "qq_group",
        "source_conversation_id": "qq:group:private-current-group",
        "speaker_id": "qq:member:private-current-speaker",
        "observed_at": "2026-09-15T07:12:13.456789+08:00",
        "source_refs": ["private-protocol:" + "reference_hash_" * 40] * 2,
        "status": "user_claim",
        "kind": "history",
        "segment_sha256": "a" * 64,
    }
    values.update(updates)
    return MemoryEvidenceEntry(**values)


def _context(entries=None, **updates):
    values = {
        "audience": "qq:group:private-current-group",
        "current_speaker_id": "qq:member:private-current-speaker",
        "entries": [_entry()] if entries is None else entries,
    }
    values.update(updates)
    return MemoryEvidenceContext(**values)


def _restore(projection, audit):
    """Independent reader of the documented schema, not the projection code."""
    restored = []
    for row in projection["rows"]:
        fields = dict(zip(projection["columns"], row, strict=True))
        meta = dict(zip(projection["metadata_columns"],
                        projection["metadata"][fields["metadata_id"]], strict=True))
        scope = projection["scopes"][meta["scope_id"]]
        date, time = meta["observed_date"], fields["observed_time"]
        restored.append({
            "text": fields["text"],
            "audience": audit["conversation_alias_to_original"].get(scope["audience"]),
            "source_channel": scope["source_channel"],
            "source_conversation_id": audit["conversation_alias_to_original"].get(scope["source_conversation_id"]),
            "speaker_id": audit["speaker_alias_to_original"].get(fields["speaker_id"]),
            "observed_at": f"{date}T{time}" if date is not None else None,
            "status": meta["status"],
            "kind": meta["kind"],
        })
    return restored


def test_all_semantic_fields_and_source_equality_roundtrip_without_deduplication():
    first = _entry(text="完整正文。" * 2500)
    context = _context([
        first, first.model_copy(deep=True),
        first.model_copy(update={"source_id": "different-source-same-text"}),
        first.model_copy(update={"text": "同一来源的另一项正文。"}),
        _entry(source_id="shared-local-source", source_channel="local_chat",
               source_conversation_id="local:operator", speaker_id="operator:local",
               observed_at="2026-09-14T23:59:59Z", status="retained_fact", kind="derived_memory"),
    ])
    before = context.model_dump(mode="json")
    projection, audit = project_evidence(context)

    assert _restore(projection, audit) == [{key: entry[key] for key in SEMANTIC_FIELDS}
                                         for entry in before["entries"]]
    assert len(projection["rows"]) == len(before["entries"]) == 5
    assert projection["same_source_rows"] == [[0, 1, 3]]
    assert audit["projected_rows"] == [{"original_indices": [index]} for index in range(5)]
    assert audit["original_evidence"] == before == context.model_dump(mode="json")
    assert projection["rows"][0][projection["columns"].index("speaker_id")] == projection["current_speaker_id"]
    first_meta = projection["metadata"][projection["rows"][0][projection["columns"].index("metadata_id")]]
    scope = projection["scopes"][first_meta[projection["metadata_columns"].index("scope_id")]]
    assert scope["source_conversation_id"] == scope["audience"] == projection["current_audience"]


@pytest.mark.parametrize("field,value", [
    ("text", "独立正文"), ("audience", "qq:group:other-audience"),
    ("source_channel", "local_chat"), ("source_conversation_id", "local:other-conversation"),
    ("speaker_id", "qq:member:other-speaker"), ("observed_at", "2026-09-14T12:13:14.000001Z"),
    ("status", "disputed"), ("kind", "derived_memory"),
])
def test_each_distinct_semantic_field_survives_as_a_separate_row(field, value):
    context = _context([_entry(), _entry(**{field: value})])
    projection, audit = project_evidence(context)
    restored = _restore(projection, audit)
    assert len(restored) == 2
    original = context.model_dump(mode="json")["entries"]
    assert restored == [{key: entry[key] for key in SEMANTIC_FIELDS} for entry in original]
    assert restored[0][field] != restored[1][field]


def test_unknown_author_and_conversation_remain_unknown_and_are_not_identity_links():
    context = _context([
        _entry(source_id="unknown-source-a", speaker_id=None, source_conversation_id=None, observed_at=None),
        _entry(source_id="unknown-source-b", speaker_id=None, source_conversation_id=None, observed_at=None),
    ], current_speaker_id=None)
    projection, audit = project_evidence(context)
    assert projection["current_speaker_id"] is None
    assert all(row[projection["columns"].index("speaker_id")] is None for row in projection["rows"])
    assert all(scope["source_conversation_id"] is None for scope in projection["scopes"].values())
    assert not projection["same_source_rows"]
    assert not audit["speaker_alias_to_original"]
    assert all(entry["observed_at"] is None for entry in _restore(projection, audit))
    assert "never infer two null IDs have the same identity" in COMPACT_EVIDENCE_FORMAT


def test_provenance_is_private_and_audit_is_complete_without_mutating_source():
    context = _context()
    before = context.model_dump(mode="json")
    projection, audit = project_evidence(context)
    model_json = json.dumps(projection, ensure_ascii=False)
    entry = before["entries"][0]
    for private_value in (entry["source_id"], *entry["source_refs"], entry["segment_sha256"],
                          entry["speaker_id"], entry["source_conversation_id"]):
        assert private_value not in model_json
    assert audit["original_evidence"] == before
    assert audit["original_evidence"]["entries"][0]["source_refs"] == entry["source_refs"]
    audit["original_evidence"]["entries"][0]["source_refs"].append("audit-only-edit")
    projection["rows"][0][-1] = "projection-only-edit"
    assert context.model_dump(mode="json") == before


def test_ids_quoted_in_admitted_text_are_preserved_as_text():
    # Projection removes protocol fields, never rewrites a user's actual words.
    entry = _entry()
    quoted_text = f"原话中的 ID 是 {entry.source_id}；引用字符串为 {entry.source_refs[0]}。"
    projection, audit = project_evidence(_context([entry.model_copy(update={"text": quoted_text})]))
    assert _restore(projection, audit)[0]["text"] == quoted_text


def test_empty_evidence_retains_current_scope_and_does_not_invent_rows():
    projection, audit = project_evidence(_context([]))
    assert projection["rows"] == projection["metadata"] == projection["same_source_rows"] == []
    assert audit["original_evidence"]["entries"] == []
    assert audit["conversation_alias_to_original"][projection["current_audience"]] == _context().audience


def test_validator_sends_compact_table_with_unknown_identity_instructions(tmp_path):
    captured = {}

    class CaptureLLM:
        def complete_json(self, **kwargs):
            captured.update(kwargs)
            return {"verdict": "pass", "violations": [], "severity": 0, "instruction": None}

        complete_validation_json = complete_json

    evidence = _context([_entry(speaker_id=None, source_conversation_id=None)])
    result = ResponseValidator(llm=CaptureLLM(), settings=Settings(trace_dir=tmp_path)).check(
        CandidateResponse(text="能再说具体一点吗？"), persona=None, memory_evidence=evidence)
    assert result.verdict == "pass"
    actual = json.loads(captured["user"])["memory_evidence"]
    expected, _ = project_evidence(evidence)
    assert actual == expected
    assert COMPACT_EVIDENCE_FORMAT in captured["system"]
    assert "unknown fields provide no author identity evidence" in captured["system"]


def test_persona_provenance_is_audited_without_entering_model_input(tmp_path):
    from datetime import UTC, datetime
    from harness.core.models import PersonaCore, PersonaIdentity, PersonaProfile

    captured = {}

    class CaptureLLM:
        def complete_json(self, **kwargs):
            captured.update(kwargs)
            return {"verdict": "pass", "violations": [], "severity": 0, "instruction": None}

    persona = PersonaCore(identity=PersonaIdentity(name="Misty", profile=PersonaProfile(
        hobbies=["读书"], source_refs=["private-persona-proof://opaque-reference"])), created_at=datetime.now(UTC))
    original = persona.identity.model_dump(mode="json")
    result = ResponseValidator(llm=CaptureLLM(), settings=Settings(trace_dir=tmp_path, call_log_enabled=True)).check(
        CandidateResponse(text="这本书不错。"), persona=persona)
    assert result.verdict == "pass"
    assert "private-persona-proof" not in captured["user"]
    assert json.loads(captured["user"])["persona"]["profile"]["hobbies"] == ["读书"]
    log = json.loads(next(tmp_path.glob("calls/*/*.json")).read_text(encoding="utf-8"))
    before = next(step["data"] for step in log["steps"] if step["kind"] == "validator_input_before")
    assert before["original_persona_identity"] == original == persona.identity.model_dump(mode="json")
