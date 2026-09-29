"""Durable authority for original quotations actually included in a prompt.

References in a recalled belief are not exposure of its original evidence. The
assembler must explicitly add ``prepare_views`` output to ``evidence_views`` in
the payload, trim that payload, then call ``issue`` on the final value. The
manifest attests that payload; it does not attest a provider's attention or
semantic understanding. No model-supplied manifest is trusted at commit.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from harness.memory.production import Experience


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class EvidenceView(BaseModel):
    """The exact source excerpt and authenticated attribution sent to the model."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    unit_id: str
    canonical_source_id: str
    source_revision: int = Field(ge=0)
    source_sha256: str
    author_id: str
    source_kind: Literal["observation", "self_expression"]
    authority: str
    occurred_at: str
    visibility: list[str]
    independent_key: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)
    allowed_uses: list[str]
    offset_encoding: Literal["unicode_codepoint"] = "unicode_codepoint"


class EvidenceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["production-evidence-manifest-v2"] = "production-evidence-manifest-v2"
    manifest_id: str
    owner_id: str
    preset_id: str
    command_id: str
    audience: str
    runtime_epoch: int | None
    runtime_instance_id: str | None = None
    identity_revision: str | int | None = None
    subjects: tuple[str, ...] = ()
    targets: dict[str, dict] = Field(default_factory=dict)
    payload_sha256: str
    issued_at: AwareDatetime
    views: tuple[EvidenceView, ...]


class EvidenceUnit(BaseModel):
    """Internal verified anchor; ``text`` is full source, ``quote`` the shown span.

    The full text is for trusted validation only, never an expansion of the
    model's displayed authority. canonical_unit_key deduplicates provenance;
    it does not prove statistical independence or a support/counter relation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    unit_id: str
    canonical_unit_key: str
    canonical_source_id: str
    source_revision: int
    source_sha256: str
    author_id: str
    source_kind: str
    authority: str
    occurred_at: AwareDatetime
    visibility: tuple[str, ...]
    independent_key: str
    start: int
    end: int
    text: str
    quote: str
    allowed_uses: tuple[str, ...]


class EvidenceManifestStore:
    def __init__(self, memory_store):
        self.memory = memory_store
        with closing(self.memory._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS pm_evidence_manifests(
                manifest_id TEXT PRIMARY KEY, command_id TEXT NOT NULL,
                audience TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(command_id,audience))""")

    def _check_database(self, db):
        databases = db.execute("PRAGMA database_list").fetchall()
        main = next((row[2] for row in databases if row[1] == "main"), None)
        if not main or Path(main).resolve() != self.memory.path.resolve():
            raise ValueError("evidence_manifest_database_identity_mismatch")
        row = db.execute("SELECT owner,preset FROM pm_identity WHERE singleton=1").fetchone()
        if row is None or tuple(row) != (self.memory.owner_id, self.memory.preset_id):
            raise ValueError("evidence_manifest_database_identity_mismatch")

    def _source(self, db, identifier, audience, *, now=None):
        row = db.execute("""SELECT s.payload,s.digest,s.revoked,r.revision FROM pm_sources s
            JOIN pm_records r ON r.id=s.id WHERE s.id=?""", (identifier,)).fetchone()
        if row is None or row["revoked"] or not self.memory._visible(db, identifier, audience, now=now):
            raise ValueError("evidence_source_unavailable")
        raw = json.loads(row["payload"])
        if _hash(raw) != row["digest"]:
            raise ValueError("evidence_source_integrity_failure")
        source = Experience.model_validate(raw)
        # Legacy episodes are mutable derived summaries, not original utterances.
        if source.kind == "legacy_episode":
            raise ValueError("evidence_original_source_required")
        return source, row["revision"], row["digest"]

    def _resolve(self, db, reference, audience, *, now=None):
        direct = db.execute("SELECT id FROM pm_sources WHERE id=?", (reference,)).fetchone()
        identifiers = [direct[0]] if direct else [row[0] for row in db.execute(
            "SELECT id FROM pm_source_alias WHERE alias=? ORDER BY id", (reference,))]
        visible = [identifier for identifier in identifiers if self.memory._visible(db, identifier, audience, now=now)]
        if not visible:
            raise ValueError("evidence_source_unavailable")
        candidates = [self._source(db, identifier, audience, now=now) for identifier in visible]
        # Multiple captures of the exact same immutable event are equivalent.
        # Similar names, text, or model beliefs never establish event identity.
        bindings = {(item.independent_key, item.author_id, item.text, item.kind) for item, _, _ in candidates}
        if len(bindings) != 1:
            raise ValueError("evidence_source_alias_ambiguous")
        return candidates[0]

    @staticmethod
    def _view(source, revision, source_hash, start=0, end=None):
        end = len(source.text) if end is None else end
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text):
            raise ValueError("evidence_view_range_invalid")
        authority = source.original.get("record", {}).get("authority") or "source_observation"
        uses = ["quote", "interpretation_evidence"]
        if source.kind == "self_expression":
            uses = ["self_expression_context"]
        elif authority == "speaker_utterance":
            uses.append("authored_utterance")
        value = dict(canonical_source_id=source.record_id, source_revision=revision, source_sha256=source_hash,
            author_id=source.author_id, source_kind=source.kind, authority=authority,
            occurred_at=source.occurred_at.isoformat(), visibility=sorted(source.scope.visibility),
            independent_key=source.independent_key, start=start, end=end, text=source.text[start:end], allowed_uses=uses)
        return EvidenceView(unit_id="evidence-unit:" + _hash(value), **value)

    def prepare_views(self, source_refs, *, audience, max_characters=12000, max_views=32, now=None):
        """Return whole-source views for explicit display; skip ineligible refs.

        Belief IDs, missing/private/revoked sources, ambiguous aliases, and legacy
        summaries are omitted. Their presence in a recall payload confers no raw
        quotation authority. Source integrity failures remain hard failures.
        """
        if type(max_characters) is not int or not 1 <= max_characters <= 100000:
            raise ValueError("evidence_view_budget_invalid")
        if type(max_views) is not int or not 1 <= max_views <= 128:
            raise ValueError("evidence_view_budget_invalid")
        source_refs = list(source_refs)
        if len(source_refs) > 512:
            raise ValueError("evidence_source_selection_exceeds_budget")
        result, seen = [], set()
        with closing(self.memory._connect()) as db:
            for reference in dict.fromkeys(source_refs):
                if not isinstance(reference, str) or not reference:
                    continue
                try:
                    source, revision, source_hash = self._resolve(db, reference, audience, now=now)
                except ValueError as exc:
                    if str(exc) in {"evidence_source_unavailable", "evidence_original_source_required", "evidence_source_alias_ambiguous"}:
                        continue
                    raise
                view = self._view(source, revision, source_hash)
                provenance = (source.independent_key, source.author_id, source.text)
                if provenance in seen:
                    continue
                candidate = [*result, view.model_dump(mode="json")]
                if len(_json(candidate)) <= max_characters:
                    result = candidate
                    seen.add(provenance)
                if len(result) >= max_views:
                    break
        return result

    @staticmethod
    def _shown_entries(payload):
        """Read only the two current production recall display shapes."""
        for segment in payload.get("memories", []):
            if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                continue
            try:
                shown = json.loads(segment["text"])
            except (ValueError, TypeError):
                continue
            if not isinstance(shown, dict):
                continue
            entries = ((shown.get("derived_memory") or {}).get("entries", [])
                       if isinstance(shown.get("derived_memory"), dict) else [shown])
            for entry in entries:
                if isinstance(entry, dict) and entry.get("record_id") in segment.get("source_ids", []):
                    yield entry

    def _targets(self, db, payload, audience, *, now=None):
        result = {}
        for entry in self._shown_entries(payload):
            identifier = entry["record_id"]
            row = db.execute("SELECT kind,subject,text,revision FROM pm_records WHERE id=?", (identifier,)).fetchone()
            if row is None or not self.memory._visible(db, identifier, audience, now=now):
                raise ValueError("evidence_target_unavailable")
            text = entry.get("current_impression") if entry.get("kind") == "person" else entry.get("summary")
            if (type(entry.get("revision")) is not int or entry["revision"] != row["revision"]
                    or text != row["text"]):
                raise ValueError("evidence_target_display_mismatch")
            binding = {"record_id": identifier, "revision": row["revision"], "kind": row["kind"],
                "subject_id": row["subject"], "text_sha256": _hash(row["text"])}
            if identifier in result and result[identifier] != binding:
                raise ValueError("evidence_target_display_mismatch")
            result[identifier] = binding
        return result

    def issue(self, final_payload, *, audience, command_id, runtime_epoch=None, subjects=(),
              runtime_instance_id=None, identity_revision=None, now=None):
        """Persist exactly the authenticated views retained in the final payload.

        Call this after prompt pruning and before the physical request. Supplying
        a source ID elsewhere in the payload does not grant it. This method does
        not mutate that payload or claim it was consumed by a provider.
        """
        if not isinstance(final_payload, dict) or not audience or not command_id:
            raise ValueError("evidence_manifest_context_invalid")
        if runtime_epoch is not None and (type(runtime_epoch) is not int or runtime_epoch < 0):
            raise ValueError("evidence_manifest_epoch_invalid")
        if (not isinstance(subjects, (list, tuple, set)) or len(subjects) > 128
                or any(not isinstance(item, str) or not item or len(item) > 160 for item in subjects)):
            raise ValueError("evidence_manifest_subjects_invalid")
        views = tuple(EvidenceView.model_validate(value) for value in final_payload.get("evidence_views", []))
        if len(views) > 128 or len({view.unit_id for view in views}) != len(views):
            raise ValueError("evidence_manifest_views_invalid")
        payload_hash = _hash(final_payload)
        moment = now or datetime.now(timezone.utc)
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            targets = self._targets(db, final_payload, audience, now=moment)
            allowed_subjects = sorted(set(subjects) | {view.author_id for view in views}
                | {target["subject_id"] for target in targets.values() if target["subject_id"]})
            identity = {"owner_id": self.memory.owner_id, "preset_id": self.memory.preset_id,
                "command_id": command_id, "audience": audience, "runtime_epoch": runtime_epoch,
                "runtime_instance_id": runtime_instance_id, "identity_revision": identity_revision,
                "subjects": allowed_subjects, "targets": targets, "payload_sha256": payload_hash,
                "views": [view.model_dump(mode="json") for view in views]}
            identifier = "evidence-manifest:" + _hash(identity)
            old = db.execute("SELECT manifest_id FROM pm_evidence_manifests WHERE command_id=? AND audience=?",
                (command_id, audience)).fetchone()
            if old:
                if old[0] != identifier:
                    raise ValueError("evidence_manifest_identity_conflict")
                return self.require_manifest(db, identifier, audience=audience, command_id=command_id, runtime_epoch=runtime_epoch)
            for view in views:
                source, revision, source_hash = self._source(db, view.canonical_source_id, audience, now=moment)
                expected = self._view(source, revision, source_hash, view.start, view.end)
                if expected != view:
                    raise ValueError("evidence_view_source_mismatch")
            manifest = EvidenceManifest(manifest_id=identifier, issued_at=moment, **identity)
            raw = manifest.model_dump(mode="json")
            db.execute("INSERT INTO pm_evidence_manifests VALUES(?,?,?,?,?)",
                (identifier, command_id, audience, _hash(raw), _json(raw)))
            return raw

    def require_manifest(self, db, manifest_id, *, audience, command_id=None, runtime_epoch=None,
                         runtime_instance_id=None, identity_revision=None):
        """Read trusted durable identity using the caller's commit transaction."""
        self._check_database(db)
        row = db.execute("SELECT payload,digest FROM pm_evidence_manifests WHERE manifest_id=?", (manifest_id,)).fetchone()
        if row is None:
            raise ValueError("evidence_manifest_missing")
        raw = json.loads(row["payload"])
        if _hash(raw) != row["digest"]:
            raise ValueError("evidence_manifest_integrity_failure")
        manifest = EvidenceManifest.model_validate(raw)
        if (manifest.manifest_id != manifest_id or manifest.owner_id != self.memory.owner_id
                or manifest.preset_id != self.memory.preset_id or manifest.audience != audience):
            raise ValueError("evidence_manifest_context_mismatch")
        if command_id is not None and manifest.command_id != command_id:
            raise ValueError("evidence_manifest_command_mismatch")
        if runtime_epoch is not None and manifest.runtime_epoch != runtime_epoch:
            raise ValueError("evidence_manifest_epoch_mismatch")
        if runtime_instance_id is not None and manifest.runtime_instance_id != runtime_instance_id:
            raise ValueError("evidence_manifest_runtime_mismatch")
        if identity_revision is not None and manifest.identity_revision != identity_revision:
            raise ValueError("evidence_manifest_identity_revision_mismatch")
        return manifest.model_dump(mode="json")

    def get(self, manifest_id, *, audience):
        with closing(self.memory._connect()) as db:
            return self.require_manifest(db, manifest_id, audience=audience)

    def validate_target(self, db, manifest_id, record_id, expected_revision, *, audience, now=None):
        manifest = self.require_manifest(db, manifest_id, audience=audience)
        target = manifest["targets"].get(record_id)
        if target is None:
            raise ValueError("evidence_target_not_displayed")
        if type(expected_revision) is not int or target["revision"] != expected_revision:
            raise ValueError("evidence_target_revision_mismatch")
        row = db.execute("SELECT kind,subject,text,revision FROM pm_records WHERE id=?", (record_id,)).fetchone()
        if row is None or not self.memory._visible(db, record_id, audience, now=now):
            raise ValueError("evidence_target_unavailable")
        if (row["revision"] != expected_revision or row["kind"] != target["kind"]
                or row["subject"] != target["subject_id"] or _hash(row["text"]) != target["text_sha256"]):
            raise ValueError("evidence_target_version_changed")
        return target

    def validate_subject(self, db, manifest_id, subject_id, *, audience):
        manifest = self.require_manifest(db, manifest_id, audience=audience)
        if subject_id not in manifest["subjects"]:
            raise ValueError("evidence_subject_not_bound")
        return subject_id

    def resolve_anchor(self, db, manifest_id, unit_id, start, end, quote, *, audience,
                       command_id=None, runtime_epoch=None, now=None):
        """Recheck exposure and live source authority atomically with v2 writes."""
        manifest = self.require_manifest(db, manifest_id, audience=audience,
            command_id=command_id, runtime_epoch=runtime_epoch)
        matches = [EvidenceView.model_validate(view) for view in manifest["views"] if view["unit_id"] == unit_id]
        if len(matches) != 1:
            raise ValueError("evidence_unit_not_displayed")
        view = matches[0]
        source, revision, source_hash = self._source(db, view.canonical_source_id, audience, now=now)
        if revision != view.source_revision or source_hash != view.source_sha256:
            raise ValueError("evidence_source_version_changed")
        if self._view(source, revision, source_hash, view.start, view.end) != view:
            raise ValueError("evidence_view_source_mismatch")
        if (type(start) is not int or type(end) is not int or not view.start <= start < end <= view.end
                or not isinstance(quote, str) or quote != source.text[start:end]):
            raise ValueError("evidence_quote_not_displayed")
        unit = EvidenceUnit(unit_id=unit_id,
            canonical_unit_key="evidence-anchor:" + _hash([source.independent_key, source.author_id,
                revision, _hash(source.text), start, end]),
            canonical_source_id=source.record_id, source_revision=revision, source_sha256=source_hash,
            author_id=source.author_id, source_kind=source.kind, authority=view.authority,
            occurred_at=source.occurred_at, visibility=tuple(sorted(source.scope.visibility)),
            independent_key=source.independent_key, start=start, end=end, text=source.text, quote=quote,
            allowed_uses=tuple(view.allowed_uses))
        result = unit.model_dump(mode="json")
        result.update(source_id=unit.canonical_source_id, revision=unit.source_revision, digest=unit.source_sha256,
            scope=source.scope.model_dump(mode="json"))
        return result
