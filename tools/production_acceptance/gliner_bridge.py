"""Read-only experimental bridge from GLiNER spans to displayed source quotes.

GLiNER labels and confidence scores are model interpretations, not evidence of
identity, self-report attribution, support, contradiction, or factual accuracy.
Only the exact displayed quotation is checked by the production resolver. This
module never creates claims, revises beliefs, binds people, or rewrites sources.
The caller must recheck anchors at any later production commit boundary.

Offsets returned by GLiNER must be Python/Unicode codepoint offsets relative to
the *exact* text returned by ``load_extraction_view``. No fuzzy search, duplicate
text matching, byte conversion, offset repair, or hidden source expansion occurs.
"""
from __future__ import annotations

from contextlib import closing
import math

from harness.memory.evidence_manifest import EvidenceManifestStore


SCHEMA_VERSION = "gliner-quote-candidates-v1"
TRUST_BOUNDARY = {
    "verified": "exact_displayed_quote_only",
    "label_and_confidence": "untrusted_model_interpretation",
    "identity_binding": "none",
    "memory_writes": "none",
    "semantic_support_or_counterevidence": "not_verified",
    "lifetime": "read_snapshot_only_revalidate_before_commit",
}


def _budget(value, ceiling):
    if type(value) is not int or not 1 <= value <= ceiling:
        raise ValueError("gliner_candidate_budget_invalid")


def _read_view(manifests, db, manifest_id, unit_id, *, audience, command_id,
               runtime_epoch, now, max_characters):
    manifest = manifests.require_manifest(db, manifest_id, audience=audience,
        command_id=command_id, runtime_epoch=runtime_epoch)
    matches = [view for view in manifest["views"] if view["unit_id"] == unit_id]
    if len(matches) != 1:
        raise ValueError("evidence_unit_not_displayed")
    view = matches[0]
    if len(view["text"]) > max_characters:
        raise ValueError("gliner_extraction_text_exceeds_budget")
    # A persisted manifest alone does not prove that its source is still live.
    manifests.resolve_anchor(db, manifest_id, unit_id, view["start"], view["end"], view["text"],
        audience=audience, command_id=command_id, runtime_epoch=runtime_epoch, now=now)
    return view


def load_extraction_view(manifests: EvidenceManifestStore, manifest_id: str, unit_id: str, *,
                         audience: str, command_id=None, runtime_epoch=None, now=None,
                         max_characters=12000) -> dict:
    """Load the exact authorized model input from a durable, already issued manifest.

    A cropped view retains its source ``base_offset``. Callers must send ``text``
    unchanged to the extractor and pass its relative spans to the verifier.
    Missing, retracted, differently scoped, or changed sources fail closed.
    """
    _budget(max_characters, 100000)
    with closing(manifests.memory._connect()) as db, db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        view = _read_view(manifests, db, manifest_id, unit_id, audience=audience,
            command_id=command_id, runtime_epoch=runtime_epoch, now=now, max_characters=max_characters)
    return {"manifest_id": manifest_id, "unit_id": unit_id, "text": view["text"],
        "base_offset": view["start"], "offset_encoding": "unicode_codepoint"}


def _candidate_error(candidate, text):
    if not isinstance(candidate, dict):
        return "gliner_candidate_shape_invalid"
    start, end = candidate.get("start"), candidate.get("end")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        return "gliner_candidate_span_invalid"
    quote = candidate.get("text")
    if not isinstance(quote, str) or quote != text[start:end]:
        return "gliner_candidate_quote_mismatch"
    score = candidate.get("confidence")
    if type(score) not in (int, float) or not 0 <= score <= 1 or not math.isfinite(score):
        return "gliner_candidate_confidence_invalid"
    return None


def verify_entity_candidates(manifests: EvidenceManifestStore, manifest_id: str, unit_id: str,
                             extraction: dict, *, audience: str, command_id=None,
                             runtime_epoch=None, now=None, max_candidates=128,
                             max_labels=32, max_characters=12000) -> dict:
    """Validate GLiNER ``entities`` without granting their labels evidence authority.

    ``extraction`` is ``{"entities": {label: [{text, start, end, confidence}]}}``.
    Spans are relative to the displayed excerpt, never the full hidden source.
    Malformed individual candidates are rejected with fixed diagnostic codes;
    invalid envelopes, exceeded budgets, and live authority failures raise
    ``ValueError`` and return no candidates. Every accepted candidate goes through
    ``EvidenceManifestStore.resolve_anchor`` in one read-only SQLite snapshot.

    Extra model fields are deliberately discarded. In particular, a proposed
    subject ID cannot turn an entity mention into an authenticated identity.
    Source author attribution identifies who authored the source, not whom a
    extracted statement describes. Scores never become belief confidence.
    """
    _budget(max_candidates, 512)
    _budget(max_labels, 128)
    _budget(max_characters, 100000)
    entities = extraction.get("entities") if isinstance(extraction, dict) else None
    if not isinstance(entities, dict):
        raise ValueError("gliner_entities_shape_invalid")
    if len(entities) > max_labels:
        raise ValueError("gliner_entities_exceed_label_budget")
    count = 0
    for label, items in entities.items():
        if not isinstance(label, str) or not 1 <= len(label) <= 160 or not isinstance(items, list):
            raise ValueError("gliner_entities_shape_invalid")
        count += len(items)
        if count > max_candidates:
            raise ValueError("gliner_entities_exceed_candidate_budget")

    candidates, rejected = [], []
    with closing(manifests.memory._connect()) as db, db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        view = _read_view(manifests, db, manifest_id, unit_id, audience=audience,
            command_id=command_id, runtime_epoch=runtime_epoch, now=now, max_characters=max_characters)
        for label_index, (label, items) in enumerate(entities.items()):
            for entity_index, candidate in enumerate(items):
                error = _candidate_error(candidate, view["text"])
                if error is not None:
                    rejected.append({"label_index": label_index, "entity_index": entity_index, "code": error})
                    continue
                start, end = view["start"] + candidate["start"], view["start"] + candidate["end"]
                unit = manifests.resolve_anchor(db, manifest_id, unit_id, start, end, candidate["text"],
                    audience=audience, command_id=command_id, runtime_epoch=runtime_epoch, now=now)
                candidates.append({
                    "anchor": {"manifest_id": manifest_id, "unit_id": unit_id,
                        "start": unit["start"], "end": unit["end"], "quote": unit["quote"],
                        "canonical_source_id": unit["canonical_source_id"],
                        "source_revision": unit["source_revision"], "source_author_id": unit["author_id"]},
                    "model_interpretation": {"label": label, "confidence": float(candidate["confidence"])},
                })
    return {"schema_version": SCHEMA_VERSION, "trust_boundary": dict(TRUST_BOUNDARY),
        "candidates": candidates, "rejected": rejected}
