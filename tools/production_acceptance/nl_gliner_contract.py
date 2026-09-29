"""Read-only acceptance contract for natural language -> GLiNER -> strict JSON.

This module is never a production action dispatcher or a memory writer. Exact
spans certify what the extractor copied from the model's decision notes, not
what a user said. Original evidence is resolved separately through the issued
manifest. Neither path certifies the model's interpretation or relation label.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from contextlib import closing
import hashlib
import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from harness.memory.evidence_manifest import EvidenceManifestStore
from harness.memory.evidence_models import ClaimProposal, EvidenceAnchor, MemoryUpdateGroup


ShortText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2000)]
Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=160)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, revalidate_instances="always")


class DecisionRecord(_Strict):
    action: Literal["reply", "wait", "rest_start"]
    subject_id: Identifier
    report_mode: Literal["direct", "reported", "hypothetical", "negated", "request", "intention", "ambiguous"]
    evidence_id: Annotated[str, StringConstraints(strict=True, pattern=r"^E[1-9][0-9]*$")]
    relation: Literal["supports", "challenges", "context"]
    judgment: ShortText
    affect: ShortText


class JsonDecisionResponse(_Strict):
    reply: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=16000)]
    decision: DecisionRecord


class NaturalResponse(_Strict):
    reply: str
    notes: str


def split_natural_response(response: str) -> NaturalResponse:
    """Remove only section markers; retain all intervening text and whitespace.

    Exactly one literal marker of each kind is required, in this order. Before
    the first marker only whitespace is permitted. Whitespace before/after the
    reply is part of the reply. Marker occurrences inside quotes are not
    escaped or guessed: they make the envelope ambiguous and reject it.
    """
    reply_marker, notes_marker = "【回复】", "【决策说明】"
    if (not isinstance(response, str) or len(response) > 30000
            or response.count(reply_marker) != 1 or response.count(notes_marker) != 1):
        raise ValueError("nl_section_shape_invalid")
    reply_start, notes_start = response.index(reply_marker), response.index(notes_marker)
    if response[:reply_start].strip() or notes_start < reply_start:
        raise ValueError("nl_section_order_invalid")
    reply = response[reply_start + len(reply_marker):notes_start]
    notes = response[notes_start + len(notes_marker):]
    if not reply.strip() or not notes.strip() or len(reply) > 16000 or len(notes) > 12000:
        raise ValueError("nl_section_content_invalid")
    return NaturalResponse(reply=reply, notes=notes)


def verify_extracted_record(extraction: dict, notes: str, *, min_confidence: float = 0.5) -> DecisionRecord:
    """Require one complete record with exact Unicode spans for all seven fields.

    Scores are extraction scores only. This check performs no fuzzy matching,
    normalization, offset repair, list selection, missing-field defaults, or
    inference from the reply. The original reply never enters this function.
    """
    if not isinstance(notes, str) or not 1 <= len(notes) <= 12000:
        raise ValueError("nl_notes_invalid")
    if (type(min_confidence) not in (int, float) or not math.isfinite(min_confidence)
            or not 0 <= min_confidence <= 1):
        raise ValueError("nl_extraction_threshold_invalid")
    if not isinstance(extraction, dict) or set(extraction) != {"decision"}:
        raise ValueError("nl_extraction_envelope_invalid")
    items = extraction["decision"]
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("nl_extraction_record_count_invalid")
    if set(items[0]) != set(DecisionRecord.model_fields):
        raise ValueError("nl_extraction_fields_invalid")
    result = {}
    for field, span in items[0].items():
        if not isinstance(span, dict) or set(span) != {"text", "start", "end", "confidence"}:
            raise ValueError("nl_extraction_span_shape_invalid")
        start, end = span["start"], span["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(notes):
            raise ValueError("nl_extraction_span_invalid")
        if not isinstance(span["text"], str) or span["text"] != notes[start:end]:
            raise ValueError("nl_extraction_text_mismatch")
        score = span["confidence"]
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("nl_extraction_confidence_invalid")
        if score < min_confidence:
            raise ValueError("nl_extraction_confidence_below_threshold")
        result[field] = span["text"]
    return DecisionRecord.model_validate(result)


def validate_record(record: DecisionRecord, *, manifests: EvidenceManifestStore, manifest_id: str,
                    aliases: Mapping[str, str], allowed_subjects: Collection[str],
                    allowed_actions: Collection[str], audience: str) -> dict:
    """Read-only source/permission checks and an uncommitted interpretation proposal.

    ``aliases`` is supplied by the server, mapping displayed E1/E2 names to unit
    IDs in this exact manifest. Only the server's displayed source view supplies
    original quote offsets. An unknown subject remains unbound. All proposals
    remain interpretations, including ones marked 'direct': this experiment
    never certifies a self-report, supersedes a belief, or executes an action.
    Any later commit must repeat live authority checks in its own transaction.
    """
    record = DecisionRecord.model_validate(record)
    if record.action not in allowed_actions:
        raise ValueError("nl_action_not_authorized")
    if record.subject_id != "unknown" and record.subject_id not in allowed_subjects:
        raise ValueError("nl_subject_not_authorized")
    unit_id = aliases.get(record.evidence_id)
    if not isinstance(unit_id, str) or not unit_id:
        raise ValueError("nl_evidence_alias_unknown")
    with closing(manifests.memory._connect()) as db, db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        manifest = manifests.require_manifest(db, manifest_id, audience=audience)
        if record.subject_id != "unknown":
            manifests.validate_subject(db, manifest_id, record.subject_id, audience=audience)
        views = [view for view in manifest["views"] if view["unit_id"] == unit_id]
        if len(views) != 1:
            raise ValueError("evidence_unit_not_displayed")
        view = views[0]
        original = manifests.resolve_anchor(db, manifest_id, unit_id,
            view["start"], view["end"], view["text"], audience=audience)
    anchor = EvidenceAnchor(unit_id=unit_id, start=original["start"], end=original["end"],
        quote=original["quote"], role=record.relation, rationale=record.judgment)
    subject = ({"subject_mention": "unknown"} if record.subject_id == "unknown"
               else {"subject_id": record.subject_id})
    claim = ClaimProposal(**subject, topic="natural-language-extraction-acceptance",
        statement=record.judgment, kind="interpretation", report_mode=record.report_mode,
        facet="other", anchors=[anchor])
    identity = json.dumps([manifest_id, record.model_dump(mode="json")], ensure_ascii=False,
        sort_keys=True, separators=(",", ":"))
    group = MemoryUpdateGroup(group_id="nl-gliner:" + hashlib.sha256(identity.encode()).hexdigest()[:32],
        claims=[claim])
    return {"record": record.model_dump(mode="json"), "proposal": group.model_dump(mode="json"),
        "source_binding": {"manifest_id": manifest_id, "unit_id": unit_id,
            "source_id": original["canonical_source_id"], "author_id": original["author_id"],
            "revision": original["source_revision"], "start": original["start"], "end": original["end"],
            "quote": original["quote"]},
        "trust_boundary": {"semantic_relation": "not_certified", "self_report": "not_certified",
            "notes_spans_are_original_evidence": False, "memory_writes": False, "action_executed": False,
            "lifetime": "read_snapshot_only_revalidate_before_commit"}}
