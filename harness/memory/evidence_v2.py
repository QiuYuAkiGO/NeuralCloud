"""Transactional, source-anchored judgments and audience-scoped projections.

The archive is authoritative. These relations describe model interpretations,
not probabilities or verified natural-language entailments.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import unicodedata

from pydantic import ValidationError

from harness.memory.evidence_models import MemoryUpdateGroup


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.utcoffset() is None:
        raise ValueError("evidence_time_requires_timezone")
    return value.astimezone(timezone.utc).isoformat()


def _normalized(value):
    return " ".join(unicodedata.normalize("NFC", value).split())


def _claim_key(claim, original_statements=None):
    if not claim.get("subject_id"):
        # An unbound textual mention is not a stable identity to merge by name.
        return None
    if original_statements is None and claim.get("proposition_key"):
        return claim["proposition_key"]
    statement = claim["statement"]
    if claim["kind"] == "self_report" and original_statements is not None:
        # Authoritative displayed meaning, not a free paraphrase. Exclude the
        # occurrence timestamp so repeated captures/reports do not evade dedup.
        statement = _json(sorted({(_normalized(author), _normalized(text)) for author, text in original_statements}))
    return _hash([*(_normalized(claim[key]) for key in ("subject_id", "topic", "context", "facet")),
                  _normalized(statement)])


def ensure_schema(db):
    """Initialize only additive v2 tables; never rewrite old revisions or sources."""
    had_keys = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pm_v2_claim_keys'").fetchone()
    statements = (
        "CREATE TABLE IF NOT EXISTS pm_v2_versions(id TEXT,revision INTEGER,payload TEXT NOT NULL,event_id TEXT NOT NULL,PRIMARY KEY(id,revision))",
        "CREATE TABLE IF NOT EXISTS pm_v2_relations(id TEXT,revision INTEGER,unit_key TEXT,source TEXT NOT NULL,source_revision INTEGER NOT NULL,role TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(id,revision,unit_key))",
        "CREATE INDEX IF NOT EXISTS pm_v2_relation_source ON pm_v2_relations(source,id,revision)",
        "CREATE TABLE IF NOT EXISTS pm_v2_timeline(id TEXT PRIMARY KEY,subject TEXT,topic TEXT,context TEXT,facet TEXT,valid_from TEXT,utterance_at TEXT)",
        "CREATE TABLE IF NOT EXISTS pm_v2_claim_keys(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,valid_from TEXT NOT NULL,valid_until TEXT)",
        "CREATE INDEX IF NOT EXISTS pm_v2_claim_key_lookup ON pm_v2_claim_keys(fingerprint,valid_from,valid_until)",
        "CREATE INDEX IF NOT EXISTS pm_v2_timeline_family ON pm_v2_timeline(subject,topic,context,facet,valid_from,utterance_at)",
        "CREATE TABLE IF NOT EXISTS pm_v2_supersessions(prior_id TEXT,prior_revision INTEGER,replacement_id TEXT,replacement_revision INTEGER,effective_from TEXT,PRIMARY KEY(prior_id,prior_revision,replacement_id,replacement_revision))",
        "CREATE TABLE IF NOT EXISTS pm_v2_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS pm_v2_receipts(event_id TEXT PRIMARY KEY,digest TEXT NOT NULL,payload TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS pm_v2_task_resolutions(event_id TEXT PRIMARY KEY,digest TEXT NOT NULL,payload TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS pm_v2_tasks(task_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,group_id TEXT NOT NULL,audience TEXT NOT NULL,reason TEXT NOT NULL,payload TEXT NOT NULL,manifest_id TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending')",
        "CREATE INDEX IF NOT EXISTS pm_v2_task_pending ON pm_v2_tasks(audience,status,created_at)",
        "CREATE TABLE IF NOT EXISTS pm_v2_projection(singleton INTEGER PRIMARY KEY CHECK(singleton=1),event_seq INTEGER NOT NULL)",
        "INSERT OR IGNORE INTO pm_v2_projection VALUES(1,0)",
    )
    # Do not use executescript here: it implicitly commits the caller's transaction.
    for statement in statements:
        db.execute(statement)
    # Only backfill missing v2 key rows. No model call, old source mutation or
    # rewriting of previously accepted version payloads is involved.
    old_heads = [] if had_keys else db.execute("""SELECT v.id,v.payload FROM pm_v2_versions v JOIN pm_records r
        ON r.id=v.id AND r.revision=v.revision LEFT JOIN pm_v2_claim_keys k ON k.id=v.id WHERE k.id IS NULL""").fetchall()
    for identifier, raw in old_heads:
        claim = json.loads(raw)
        originals = None
        if claim["kind"] == "self_report":
            originals = [(row[0], json.loads(row[1])["text"]) for row in db.execute("""SELECT s.author,s.payload
                FROM pm_v2_relations e JOIN pm_sources s ON s.id=e.source WHERE e.id=? AND e.revision=?""",
                (identifier, claim["revision"]))]
        key = _claim_key(claim, originals)
        if key:
            db.execute("INSERT INTO pm_v2_claim_keys VALUES(?,?,?,?)", (identifier, key, claim["valid_from"], claim.get("valid_until")))


def _head(db, identifier):
    row = db.execute("""SELECT v.payload FROM pm_v2_versions v JOIN pm_records r
        ON r.id=v.id AND r.revision=v.revision WHERE v.id=? AND r.kind='claim_v2'""", (identifier,)).fetchone()
    return json.loads(row[0]) if row else None


def _raw_visible(db, identifier, audience):
    from harness.memory.production import ProductionMemoryStore
    # Relations only bind pm_sources, so dispatch cannot recurse to a v2 claim.
    if not db.execute("SELECT 1 FROM pm_sources WHERE id=?", (identifier,)).fetchone():
        return False
    return ProductionMemoryStore._visible(db, identifier, audience)


def _timeline_current(db, identifier, audience, moment):
    item = db.execute("SELECT * FROM pm_v2_timeline WHERE id=?", (identifier,)).fetchone()
    if item is None:
        return True
    latest = db.execute("""SELECT t.valid_from,t.utterance_at FROM pm_v2_timeline t
        JOIN pm_visibility v ON v.id=t.id WHERE t.subject=? AND t.topic=? AND t.context=? AND t.facet=?
        AND v.audience=? AND t.valid_from<=? ORDER BY t.valid_from DESC,t.utterance_at DESC LIMIT 1""",
        (item["subject"], item["topic"], item["context"], item["facet"], audience, moment)).fetchone()
    # Deliberately include revoked/inactive latest entries: withdrawing one is
    # not an assertion that an older preference has become current again.
    return latest is not None and (item["valid_from"], item["utterance_at"]) == tuple(latest)


def _authorized_claim(db, identifier, audience):
    row = db.execute("""SELECT 1 FROM pm_records r JOIN pm_visibility v ON v.id=r.id
        WHERE r.id=? AND r.kind='claim_v2' AND r.active=1 AND v.audience=?""", (identifier, audience)).fetchone()
    claim = _head(db, identifier) if row else None
    if not claim:
        return None
    relations = db.execute("SELECT source,source_revision FROM pm_v2_relations WHERE id=? AND revision=?",
                           (identifier, claim["revision"])).fetchall()
    if not relations:
        return None
    for source, revision in relations:
        if not _raw_visible(db, source, audience):
            return None
        current = db.execute("SELECT revision FROM pm_records WHERE id=?", (source,)).fetchone()
        if not current or current[0] != revision:
            return None
    return claim


def is_visible(db, identifier, audience, now=None):
    moment = _time(now)
    claim = _authorized_claim(db, identifier, audience)
    if not claim or claim["valid_from"] > moment or (claim.get("valid_until") and claim["valid_until"] <= moment):
        return False
    if not _timeline_current(db, identifier, audience, moment):
        return False
    superseding = db.execute("""SELECT s.replacement_id FROM pm_v2_supersessions s JOIN pm_visibility v
        ON v.id=s.replacement_id WHERE s.prior_id=? AND s.prior_revision=? AND s.effective_from<=? AND v.audience=?""",
        (identifier, claim["revision"], moment, audience)).fetchone()
    return not superseding


def _relations(db, identifier, revision):
    return [json.loads(row[0]) for row in db.execute(
        "SELECT payload FROM pm_v2_relations WHERE id=? AND revision=? ORDER BY unit_key", (identifier, revision))]


def _utterance_identities(db, relations):
    """Identify full original utterances, independently of selected spans/captures."""
    identities = set()
    for relation in relations:
        raw = db.execute("SELECT payload FROM pm_sources WHERE id=?", (relation["source_id"],)).fetchone()
        if raw is None:
            raise ValueError("evidence_source_unavailable")
        # The manifest/current-target checks authenticate each capture separately.
        # Its whole payload digest includes record_id and would reject aliases of
        # the same utterance; bind full text and provenance rather than a span.
        text = json.loads(raw[0])["text"]
        identities.add((relation["independent_key"], relation["author_id"], _hash(text),
                        relation["occurred_at"], relation["source_revision"],
                        relation["source_kind"], relation["authority"]))
    return identities


def _quotation(db, relation):
    from harness.memory.production import Experience
    raw = db.execute("SELECT payload FROM pm_sources WHERE id=?", (relation["source_id"],)).fetchone()
    source = Experience.model_validate_json(raw[0])
    return {"source_id": relation["source_id"], "source_revision": relation["source_revision"],
        "author_id": relation["author_id"], "quote": relation["quote"], "source_text": source.text,
        "start": relation["start"], "end": relation["end"], "role": relation["role"],
        "rationale": relation["rationale"], "occurred_at": relation["occurred_at"],
        "source_kind": relation["source_kind"], "authority": relation["authority"], "quote_verified": True}


def _primary_text(claim, quotations):
    if claim["kind"] != "self_report":
        return claim["statement"]
    originals = sorted({(item["author_id"], item["occurred_at"], item["source_text"]) for item in quotations})
    return "\n".join(f"{author} 在 {at} 说：{text}" for author, at, text in originals)


def project_entry(db, identifier, audience, now=None):
    if not is_visible(db, identifier, audience, now):
        return None
    claim = _head(db, identifier)
    relations = _relations(db, identifier, claim["revision"])
    quotations = [_quotation(db, item) for item in relations]
    roles = {item["role"] for item in relations}
    pair = ("supports" in roles, "challenges" in roles)
    state = {(False, False): "neither", (True, False): "support_only", (False, True): "challenge_only",
             (True, True): "contested"}[pair]
    # Do not display a model paraphrase or a decontextualized quote as self-report.
    primary = _primary_text(claim, quotations)
    return {"record_id": identifier, "kind": "person", "subject_refs": [claim["subject_id"]] if claim.get("subject_id") else [],
        "subject_mention": claim.get("subject_mention"), "topic": claim["topic"], "context": claim["context"],
        "current_impression": primary, "statement": primary, "model_interpretation": claim["statement"],
        "authority": "source_anchored_interpretation", "semantic_status": "model_interpretation",
        "quote_verified": True, "quotations": quotations, "source_refs": sorted({item["source_id"] for item in relations}),
        "revision": claim["revision"], "occurred_at": claim["valid_from"], "valid_from": claim["valid_from"],
        "valid_until": claim.get("valid_until"), "facet": claim["facet"], "report_mode": claim["report_mode"],
        "evidence_state": state, "needs_review": "mixed" in roles or claim["report_mode"] == "ambiguous",
        "confidence": None, "status": state, "source_channel": claim["source_channel"],
        "source_conversation_id": claim["source_conversation_id"], "supporting_episode_ids": sorted({
            item["source_id"] for item in relations if item["role"] == "supports"}),
        "counterevidence": [{"episode_id": item["source_id"], "statement": item["quote"], "rationale": item["rationale"]}
                            for item in relations if item["role"] == "challenges"]}


def history(db, identifier):
    result = []
    for revision, payload in db.execute("SELECT revision,payload FROM pm_v2_versions WHERE id=? ORDER BY revision", (identifier,)):
        claim = json.loads(payload)
        relations = _relations(db, identifier, revision)
        available = all(db.execute("SELECT 1 FROM pm_sources WHERE id=? AND revoked=0", (item["source_id"],)).fetchone()
                        for item in relations)
        if not available:
            result.append({"record_id": identifier, "revision": revision, "status": "source_withdrawn"})
        else:
            result.append({**claim, "relations": relations, "semantic_status": "model_interpretation"})
    return tuple(result)


def operator_entry(db, identifier, audience="local:main", now=None):
    entry = project_entry(db, identifier, audience, now)
    if entry is None:
        return None
    claim = _head(db, identifier)
    row = db.execute("SELECT pinned,important FROM pm_records WHERE id=?", (identifier,)).fetchone()
    retention = db.execute("SELECT reason FROM pm_retention WHERE id=?", (identifier,)).fetchone()
    return {**entry, "subject_id": claim.get("subject_id"), "kind": "claim_v2", "claim_kind": claim["kind"],
        "pinned": bool(row[0]), "important": bool(row[1]), "important_reason": retention[0] if retention else None,
        "evidence": [{**item, "record_id": item["source_id"], "stance": item["role"], "text": item["quote"],
                      "source_refs": [item["source_id"]]} for item in entry["quotations"]]}


class EvidenceV2Store:
    def __init__(self, memory_store):
        self.memory = memory_store
        with closing(memory_store._connect()) as db, db:
            ensure_schema(db)

    def _manifest_store(self):
        from harness.memory.evidence_manifest import EvidenceManifestStore
        return EvidenceManifestStore(self.memory)

    def apply_groups(self, groups, *, manifest_id, event_id, audience, now=None):
        if not isinstance(event_id, str) or not event_id or len(event_id) > 256:
            raise ValueError("evidence_event_id_invalid")
        if len(groups) > 8:
            raise ValueError("too_many_evidence_groups")
        moment = _time(now)
        raw = [item.model_dump(mode="json") if isinstance(item, MemoryUpdateGroup) else item for item in groups]
        binding = {"groups": raw, "manifest_id": manifest_id, "audience": audience}
        digest = _hash(binding)
        manifests = self._manifest_store()
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT digest,payload FROM pm_v2_receipts WHERE event_id=?", (event_id,)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise ValueError("evidence_receipt_identity_conflict")
                return json.loads(prior[1])
            parsed, errors, normalized = {}, {}, []
            for index, item in enumerate(raw):
                key = item.get("group_id", f"invalid:{index}") if isinstance(item, dict) else f"invalid:{index}"
                if not isinstance(key, str) or not key or len(key) > 160:
                    key = f"invalid:{index}"
                if key in parsed or key in errors:
                    raise ValueError("evidence_group_id_duplicate")
                normalized.append((key, item))
                try:
                    parsed[key] = MemoryUpdateGroup.model_validate(item)
                except ValidationError:
                    errors[key] = "evidence_group_schema_invalid"
            # A model cannot split changes to the same target across groups to
            # turn a conflict into sequential successful writes.
            touched = {}
            for key, group in parsed.items():
                ids = [item.record_id for item in group.claims if item.record_id]
                ids.extend(item.record_id for item in group.important)
                ids.extend(old.record_id for item in group.claims for old in item.supersedes)
                for identifier in set(ids):
                    touched.setdefault(identifier, set()).add(key)
            for owners in touched.values():
                if len(owners) > 1:
                    errors.update({key: "evidence_target_shared_across_groups" for key in owners})
            manifest_error = None
            try:
                manifest = manifests.require_manifest(db, manifest_id, audience=audience)
            except ValueError as exc:
                manifest, manifest_error = {}, str(exc)
            pending = list(parsed)
            results = {}
            # Keep invalid groups as durable tasks, too. Their dependents can
            # then be rejected without silently losing unrelated valid work.
            all_keys = [key for key, _ in normalized]
            raw_by_key = dict(normalized)
            for key in list(errors):
                results[key] = self._reject(db, event_id, key, manifest_id, audience, raw_by_key[key], errors[key], moment)
                if key in pending:
                    pending.remove(key)
            while pending:
                ready = [key for key in pending if not any(dep in pending for dep in parsed[key].depends_on)]
                if not ready:
                    ready = list(pending)
                    errors.update({key: "evidence_dependency_cycle" for key in ready})
                for key in ready:
                    group = parsed[key]
                    reason = errors.get(key) or manifest_error
                    if reason is None and any(dep not in results or results[dep]["status"] != "applied" for dep in group.depends_on):
                        reason = "evidence_dependency_rejected"
                    if reason is None:
                        db.execute("SAVEPOINT evidence_group")
                        try:
                            record_ids = self._apply_group(db, group, manifests, manifest, manifest_id,
                                                           event_id, audience, moment)
                            db.execute("RELEASE evidence_group")
                            results[key] = {"group_id": key, "status": "applied", "record_ids": record_ids}
                        except ValueError as exc:
                            db.execute("ROLLBACK TO evidence_group")
                            db.execute("RELEASE evidence_group")
                            reason = str(exc)
                    if reason is not None:
                        results[key] = self._reject(db, event_id, key, manifest_id, audience,
                                                   group.model_dump(mode="json"), reason, moment)
                    pending.remove(key)
            ordered = [results[key] for key in all_keys if key in results]
            receipt = {"schema_version": "memory-evidence-result-v2", "groups": ordered,
                "accepted_count": sum(item["status"] == "applied" for item in ordered),
                "rejected_count": sum(item["status"] == "rejected" for item in ordered)}
            db.execute("INSERT INTO pm_v2_receipts VALUES(?,?,?)", (event_id, digest, _json(receipt)))
            db.execute("UPDATE pm_v2_projection SET event_seq=(SELECT COALESCE(MAX(seq),0) FROM pm_v2_events) WHERE singleton=1")
            return receipt

    def _reject(self, db, event_id, key, manifest_id, audience, proposal, reason, moment):
        task = "memory-v2-task:" + _hash([event_id, key])[:32]
        db.execute("INSERT INTO pm_v2_tasks(task_id,event_id,group_id,audience,reason,payload,manifest_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (task, event_id, key, audience, reason, _json(proposal), manifest_id, moment))
        db.execute("INSERT INTO pm_v2_events(event_id,payload) VALUES(?,?)", ("rejected:" + _hash([event_id, key]),
            _json({"kind": "group_rejected", "group_id": key, "task_id": task, "reason": reason, "audience": audience})))
        return {"group_id": key, "status": "rejected", "reason": reason, "record_ids": [], "task_id": task}

    def _resolve(self, db, anchors, manifests, manifest_id, audience, moment):
        resolved = {}
        for anchor in anchors:
            unit = manifests.resolve_anchor(db, manifest_id, anchor.unit_id, anchor.start, anchor.end, anchor.quote,
                                            audience=audience, now=moment)
            identifier = unit.get("canonical_source_id", unit.get("source_id"))
            revision = unit.get("source_revision", unit.get("revision"))
            key = unit["canonical_unit_key"]
            value = {"unit_key": key, "source_id": identifier, "source_revision": revision,
                "source_digest": unit.get("source_sha256", unit.get("digest")), "author_id": unit["author_id"],
                "source_kind": unit["source_kind"], "authority": unit["authority"],
                "occurred_at": _time(unit["occurred_at"]), "independent_key": unit["independent_key"],
                "allowed_uses": unit["allowed_uses"],
                "start": anchor.start, "end": anchor.end, "quote": anchor.quote, "role": anchor.role,
                "rationale": anchor.rationale}
            if key in resolved and resolved[key]["role"] != anchor.role:
                raise ValueError("evidence_same_span_conflicting_roles")
            if key not in resolved:
                resolved[key] = value
        return list(resolved.values())

    def _apply_group(self, db, group, manifests, manifest, manifest_id, event_id, audience, moment):
        records = []
        declared = [item.record_id for item in group.claims if item.record_id]
        if len(declared) != len(set(declared)):
            raise ValueError("evidence_duplicate_claim_in_group")
        for index, proposal in enumerate(group.claims):
            identifier = proposal.record_id or "claim_v2_" + _hash([event_id, group.group_id, index])[:32]
            current = db.execute("SELECT kind,revision,subject FROM pm_records WHERE id=?", (identifier,)).fetchone()
            if current is not None:
                if current["kind"] != "claim_v2":
                    raise ValueError("evidence_cannot_rewrite_legacy_record")
                manifests.validate_target(db, manifest_id, identifier, proposal.expected_revision, audience=audience, now=moment)
                if current["revision"] != proposal.expected_revision:
                    raise ValueError("evidence_claim_revision_conflict")
            elif proposal.expected_revision:
                raise ValueError("evidence_claim_revision_conflict")
            if current is not None and not proposal.expected_revision:
                raise ValueError("evidence_claim_revision_conflict")
            if proposal.subject_id and proposal.subject_id not in manifest.get("subjects", []):
                raise ValueError("evidence_subject_not_authorized")
            if proposal.reported_speaker_id and proposal.reported_speaker_id not in manifest.get("subjects", []):
                raise ValueError("evidence_speaker_not_authorized")
            if current and current["subject"] != (proposal.subject_id or "unbound:" + identifier):
                raise ValueError("evidence_claim_subject_changed")
            previous_head = _head(db, identifier) if current else None
            if previous_head and any(previous_head.get(key) != getattr(proposal, key)
                                     for key in ("subject_id", "subject_mention", "topic", "context", "facet", "kind", "report_mode")):
                raise ValueError("evidence_revision_identity_context_changed")
            if previous_head and (
                (proposal.valid_from is not None and _time(proposal.valid_from) != previous_head["valid_from"])
                or (proposal.valid_until is not None and _time(proposal.valid_until) != previous_head.get("valid_until"))
            ):
                raise ValueError("evidence_revision_temporal_interval_changed")
            relations = self._resolve(db, proposal.anchors, manifests, manifest_id, audience, moment)
            sources = []
            from harness.memory.production import Experience
            for relation in relations:
                source = Experience.model_validate_json(db.execute("SELECT payload FROM pm_sources WHERE id=?",
                    (relation["source_id"],)).fetchone()[0])
                sources.append(source)
                if source.kind == "legacy_episode":
                    raise ValueError("evidence_legacy_is_not_new_source")
                if "interpretation_evidence" not in relation["allowed_uses"] and not (
                    (proposal.subject_id or "").startswith("character:")
                    and proposal.subject_id == source.author_id and "self_expression_context" in relation["allowed_uses"]
                ):
                    raise ValueError("evidence_source_use_not_authorized")
                if not (proposal.subject_id or "").startswith("character:") and (
                    source.author_id.startswith("character:") or relation["source_kind"] in {"body_measurement", "assistant_intent", "body_state"}
                ):
                    raise ValueError("evidence_self_state_cannot_prove_person_claim")
                if proposal.kind == "self_report" and proposal.report_mode == "direct" and (
                    not proposal.subject_id or source.author_id != proposal.subject_id or relation["authority"] != "speaker_utterance"
                    or (proposal.reported_speaker_id is not None and proposal.reported_speaker_id != proposal.subject_id)
                ):
                    raise ValueError("evidence_direct_report_author_mismatch")
            if previous_head and proposal.kind == "self_report" and proposal.report_mode == "direct":
                previous_relations = _relations(db, identifier, previous_head["revision"])
                if _utterance_identities(db, previous_relations) != _utterance_identities(db, relations):
                    raise ValueError("evidence_direct_report_utterances_changed")
            if proposal.subject_mention and not any(proposal.subject_mention in source.text for source in sources):
                raise ValueError("evidence_unbound_mention_not_in_source")
            visibility = sorted(set.intersection(*(set(source.scope.visibility) for source in sources)))
            if audience not in visibility:
                raise ValueError("evidence_sources_no_common_audience")
            utterance = max(item["occurred_at"] for item in relations)
            start = previous_head["valid_from"] if previous_head else _time(proposal.valid_from or utterance)
            end = previous_head.get("valid_until") if previous_head else (_time(proposal.valid_until) if proposal.valid_until else None)
            if end and end <= start:
                raise ValueError("claim_effective_interval_invalid")
            revision = proposal.expected_revision + 1
            claim = {**proposal.model_dump(mode="json", exclude={"anchors"}), "record_id": identifier,
                "revision": revision, "valid_from": start, "valid_until": end, "utterance_at": utterance,
                "recorded_at": moment, "source_channel": sources[0].scope.channel,
                "source_conversation_id": sources[0].scope.conversation_id, "schema_version": "memory-claim-v2"}
            key = _claim_key(claim, [(item.author_id, item.text) for item in sources])
            claim["proposition_key"] = key
            if key and current is None:
                candidates = db.execute("""SELECT id FROM pm_v2_claim_keys WHERE fingerprint=? AND id<>?
                    AND (valid_until IS NULL OR valid_until>?) AND (? IS NULL OR valid_from<?)""",
                    (key, identifier, start, end, end)).fetchall()
                for candidate in candidates:
                    # A validated preference supersession closes the prior
                    # effective interval at this new start. It is not an
                    # independent duplicate interpretation dropping challenges.
                    if candidate[0] in {item.record_id for item in proposal.supersedes}:
                        continue
                    existing_claim = _authorized_claim(db, candidate[0], audience)
                    prior_end = None
                    if existing_claim:
                        closed = db.execute("""SELECT MIN(s.effective_from) FROM pm_v2_supersessions s
                            JOIN pm_visibility v ON v.id=s.replacement_id WHERE s.prior_id=? AND s.prior_revision=? AND v.audience=?""",
                            (candidate[0], existing_claim["revision"], audience)).fetchone()[0]
                        endings = [value for value in (existing_claim.get("valid_until"), closed) if value]
                        prior_end = min(endings) if endings else None
                    if existing_claim and (prior_end is None or prior_end > start):
                        raise ValueError("existing_claim_requires_revision")
            for prior in proposal.supersedes:
                if prior.record_id == identifier:
                    raise ValueError("evidence_supersession_self_reference")
                manifests.validate_target(db, manifest_id, prior.record_id, prior.expected_revision, audience=audience, now=moment)
                previous = _head(db, prior.record_id)
                if not previous or previous["revision"] != prior.expected_revision:
                    raise ValueError("evidence_supersession_revision_conflict")
                identity = ("subject_id", "topic", "context", "facet")
                if (any(previous.get(key) != claim.get(key) for key in identity)
                        or previous["kind"] != "self_report" or previous["report_mode"] != "direct"
                        or start < previous["valid_from"] or utterance < previous["utterance_at"]):
                    raise ValueError("evidence_supersession_identity_or_time_mismatch")
                old_visibility = sorted(row[0] for row in db.execute("SELECT audience FROM pm_visibility WHERE id=?", (prior.record_id,)))
                if old_visibility != visibility:
                    raise ValueError("evidence_supersession_scope_mismatch")
                db.execute("INSERT INTO pm_v2_supersessions VALUES(?,?,?,?,?)",
                           (prior.record_id, prior.expected_revision, identifier, revision, start))
            db.execute("INSERT INTO pm_v2_versions VALUES(?,?,?,?)", (identifier, revision, _json(claim), event_id))
            for relation in relations:
                db.execute("INSERT INTO pm_v2_relations VALUES(?,?,?,?,?,?,?)", (identifier, revision, relation["unit_key"],
                    relation["source_id"], relation["source_revision"], relation["role"], _json(relation)))
            source_by_id = {item.record_id: item for item in sources}
            primary = _primary_text(claim, [{**item, "source_text": source_by_id[item["source_id"]].text} for item in relations])
            db.execute("""INSERT INTO pm_records(id,kind,subject,text,time,revision,confidence) VALUES(?,'claim_v2',?,?,?,?,NULL)
                ON CONFLICT(id) DO UPDATE SET text=excluded.text,time=excluded.time,revision=excluded.revision,active=1,confidence=NULL""",
                (identifier, proposal.subject_id or "unbound:" + identifier, primary, start, revision))
            db.execute("DELETE FROM pm_evidence WHERE id=?", (identifier,))
            db.execute("DELETE FROM pm_evidence_revision WHERE id=?", (identifier,))
            for source in {item["source_id"]: item["source_revision"] for item in relations}.items():
                db.execute("INSERT INTO pm_evidence VALUES(?,?,'dependency')", (identifier, source[0]))
                db.execute("INSERT INTO pm_evidence_revision VALUES(?,?,?)", (identifier, *source))
            self.memory._index(db, identifier, proposal.statement + " " + proposal.topic + " " + " ".join(item["quote"] for item in relations), visibility)
            if key:
                db.execute("INSERT OR REPLACE INTO pm_v2_claim_keys VALUES(?,?,?,?)", (identifier, key, start, end))
            if proposal.kind == "self_report" and proposal.report_mode == "direct" and proposal.facet in {"preference", "intention"}:
                db.execute("INSERT OR REPLACE INTO pm_v2_timeline VALUES(?,?,?,?,?,?,?)", (identifier, proposal.subject_id,
                    proposal.topic, proposal.context, proposal.facet, start, utterance))
            records.append(identifier)
        for importance in group.important:
            manifests.validate_target(db, manifest_id, importance.record_id, importance.expected_revision, audience=audience, now=moment)
            relations = self._resolve(db, importance.anchors, manifests, manifest_id, audience, moment)
            bound = {row[0] for row in db.execute("SELECT source FROM pm_evidence WHERE id=?", (importance.record_id,))} | {importance.record_id}
            if not {item["source_id"] for item in relations}.issubset(bound):
                raise ValueError("evidence_importance_unbound_source")
            db.execute("UPDATE pm_records SET important=1 WHERE id=?", (importance.record_id,))
            db.execute("UPDATE pm_postings SET priority=(SELECT pinned+important FROM pm_records WHERE id=?) WHERE id=?",
                       (importance.record_id, importance.record_id))
            db.execute("INSERT OR REPLACE INTO pm_retention VALUES(?,?,?)", (importance.record_id, importance.reason,
                       _json(sorted({item["source_id"] for item in relations}))))
            records.append(importance.record_id)
        db.execute("INSERT INTO pm_v2_events(event_id,payload) VALUES(?,?)", ("applied:" + _hash([event_id, group.group_id]),
            _json({"kind": "group_applied", "group": group.model_dump(mode="json"), "record_ids": records,
                   "manifest_id": manifest_id, "audience": audience, "recorded_at": moment})))
        return records

    def pending_tasks(self, *, audience, limit=50):
        if not 1 <= limit <= 100:
            raise ValueError("evidence_task_limit_invalid")
        with closing(self.memory._connect()) as db:
            return [dict(row) for row in db.execute("""SELECT task_id,event_id,group_id,reason,manifest_id,created_at,status
                FROM pm_v2_tasks WHERE audience=? AND status='pending' ORDER BY created_at,task_id LIMIT ?""", (audience, limit))]

    def resolve_task(self, task_id, *, audience, event_id, resolution, reason,
                     replacement_event_id=None, replacement_group_id=None):
        """Record an explicit disposition, without re-running or repairing a model.

        ``acknowledged`` records a deliberate closure, not a successful repair.
        ``replaced`` must point to a durably accepted group in the same audience.
        Both leave the original rejected proposal and its receipt intact.
        """
        if resolution not in {"acknowledged", "replaced"} or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("evidence_task_resolution_invalid")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 256:
            raise ValueError("evidence_event_id_invalid")
        replacement = bool(replacement_event_id and replacement_group_id)
        if (resolution == "replaced") != replacement or (
            resolution == "acknowledged" and (replacement_event_id is not None or replacement_group_id is not None)
        ):
            raise ValueError("evidence_task_replacement_required")
        payload = {"task_id": task_id, "audience": audience, "resolution": resolution, "reason": reason,
                   "replacement_event_id": replacement_event_id, "replacement_group_id": replacement_group_id}
        digest = _hash(payload)
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT digest,payload FROM pm_v2_task_resolutions WHERE event_id=?", (event_id,)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise ValueError("evidence_task_resolution_identity_conflict")
                return json.loads(prior[1])
            task = db.execute("SELECT audience,status FROM pm_v2_tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None or task["audience"] != audience:
                raise ValueError("evidence_task_unavailable")
            if task["status"] != "pending":
                raise ValueError("evidence_task_already_disposed")
            if replacement:
                receipt = db.execute("SELECT payload FROM pm_v2_receipts WHERE event_id=?", (replacement_event_id,)).fetchone()
                accepted = receipt and any(item["group_id"] == replacement_group_id and item["status"] == "applied"
                    for item in json.loads(receipt[0])["groups"])
                event = db.execute("SELECT payload FROM pm_v2_events WHERE event_id=?",
                    ("applied:" + _hash([replacement_event_id, replacement_group_id]),)).fetchone()
                if not accepted or not event or json.loads(event[0])["audience"] != audience:
                    raise ValueError("evidence_task_replacement_not_accepted")
            result = {**payload, "status": resolution}
            db.execute("UPDATE pm_v2_tasks SET status=? WHERE task_id=?", (resolution, task_id))
            db.execute("INSERT INTO pm_v2_task_resolutions VALUES(?,?,?)", (event_id, digest, _json(result)))
            db.execute("INSERT INTO pm_v2_events(event_id,payload) VALUES(?,?)",
                ("task-resolution:" + _hash(event_id), _json({"kind": "task_resolution", **result})))
            db.execute("UPDATE pm_v2_projection SET event_seq=(SELECT MAX(seq) FROM pm_v2_events) WHERE singleton=1")
            return result
