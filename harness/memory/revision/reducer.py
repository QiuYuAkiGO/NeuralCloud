"""Pure validation and revision reducers over detached CharacterPreset values."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from harness.config import Settings
from harness.core.models import CandidateMemory, EpisodicEvent, SemanticFact, RelationshipState
from harness.memory.impressions import ImpressionMemory, impression_key
from harness.memory.revision.ledger import (append_effects, digest, integrate_external_snapshot,
    projection, replay, state_for, with_projection, _normalize_impression, independent_count)
from harness.memory.revision.models import RevisionProposal
from harness.memory.revision.sources import FrozenSource
from harness.memory.writer.confidence import update_confidence

VERSION = "daily-revision-v1"
DIMENSIONS = ("closeness", "trust", "attachment", "recent_tension")
VOLATILE = {"embedding", "strength", "strength_updated_at", "last_decay_review_at", "decay_demoted",
            "tier", "expire_at", "hit_count", "last_hit_at", "tier_updated_at", "tier_reasons", "updated_at"}


def _relation_scope(scope):
    return scope.model_copy(update={"conversation_id": None}) if scope.channel == "local_chat" else scope


def _same_subject(a, b, scope):
    return a == b or (scope.channel == "local_chat" and {a, b} <= {"user", "local:main"})


def _sources(values):
    return [s if isinstance(s, FrozenSource) else FrozenSource.model_validate(s) for s in values]


def _scope(sources):
    if not sources:
        raise ValueError("daily_revision_empty_partition")
    scope = sources[0].scope
    if any(s.scope != scope or s.owner_id != sources[0].owner_id or s.preset_id != sources[0].preset_id for s in sources):
        raise ValueError("daily_revision_mixed_scope")
    return scope


def revision_context(preset, sources):
    """Return only interpretations and historical evidence in this exact scope."""
    sources = _sources(sources)
    scope = _scope(sources)
    principals = {s.author_principal for s in sources} | {f"character:{scope.owner_id}"}
    targets = {s.relation_target for s in sources if s.relation_target}
    old = projection(preset)
    selected = {}
    for key, value in old.items():
        source_scope = value.get("scope")
        legacy_local = source_scope is None and scope.channel == "local_chat"
        normalized_relation = key.startswith("relationships/") and source_scope == _relation_scope(scope).model_dump(mode="json")
        if source_scope != scope.model_dump(mode="json") and not legacy_local and not normalized_relation:
            continue
        if key.startswith("relationships/") and value["target"] not in targets:
            continue
        # Embeddings are not source evidence and overwhelm structured prompts.
        selected[key] = {k: v for k, v in value.items() if k not in VOLATILE}
    ordered = sorted(selected, key=lambda key: (bool(principals.intersection(selected[key].get("subject_refs", []))),
                    selected[key].get("updated_at", selected[key].get("time", ""))), reverse=True)
    selected = {k: selected[k] for k in ordered[:128]}
    historical = []
    wanted = {ref for row in selected.values() for ref in row.get("revision_metadata", {}).get("source_ids", [])}
    for source_id in sorted(wanted):
        raw = preset.daily_revision_state.get("sources", {}).get(source_id)
        if raw and raw.get("scope") == scope.model_dump(mode="json"):
            historical.append(raw)
    return {"version": VERSION, "persona": preset.persona.model_dump(mode="json"),
            "scope": scope.model_dump(mode="json"), "allowed_subjects": sorted(principals),
            "allowed_relation_targets": sorted(targets), "existing": selected,
            "historical_sources": historical,
            "historical_classifications": {s["source_id"]: preset.daily_revision_state.get("classifications", {}).get(s["source_id"])
                                            for s in historical},
            "omitted_existing_count": max(0, len(ordered) - len(selected)),
            "active_revision_ids": sorted(k for k, r in preset.daily_revision_state.get("receipts", {}).items()
                                          if r["status"] == "applied" and any(
                                              e["run_id"] == k and e["key"] in selected
                                              for e in preset.daily_revision_state.get("entries", [])))}


def _key(prefix, *parts):
    return prefix + digest(parts)[:32]


def _bound_old(old, scope):
    if old is not None and old.scope != scope:
        if not (old.scope is None and scope.channel == "local_chat"):
            raise ValueError("daily_revision_target_scope_mismatch")


def _independent(source, classifications):
    return (not source.self_generated and getattr(source, "independent", True)
            and not getattr(source, "quoted", False)
            and classifications.get(source.source_id) in {"observation", "speaker_claim"})


def _evidence(item, known, classifications, *, independent=False):
    support_ids, counter_ids = set(item.evidence_ids), set(item.counterevidence_ids)
    if support_ids & counter_ids:
        raise ValueError("daily_revision_evidence_has_both_stances")
    if not support_ids and not counter_ids:
        raise ValueError("daily_revision_evidence_required")
    if not (support_ids | counter_ids) <= known.keys():
        raise ValueError("daily_revision_unknown_evidence")
    support = [known[k] for k in sorted(support_ids)]
    counter = [known[k] for k in sorted(counter_ids)]
    if independent and any(not _independent(s, classifications) for s in support + counter):
        raise ValueError("daily_revision_dependent_belief_evidence")
    return support, counter


def _metadata(old, support, counter, *, run_id, now, status="active"):
    meta = deepcopy(getattr(old, "revision_metadata", {}) if old else {})
    supports, counters = deepcopy(meta.get("support", {})), deepcopy(meta.get("counter", {}))
    for collection, other, selected in ((supports, counters, support), (counters, supports, counter)):
        for source in selected:
            for key in source.evidence_keys:
                other.pop(key, None)
                collection[key] = {"source_id": source.source_id, "time": source.occurred_at.isoformat()}
    meta.update(revision=int(meta.get("revision", 0)) + 1, run_id=run_id, status=status,
                support=supports, counter=counters, revised_at=now.isoformat(), processing_version=VERSION,
                source_ids=sorted({v["source_id"] for v in [*supports.values(), *counters.values()]}))
    meta["independence_groups"] = [list(group) for group in sorted(
        {tuple(group) for group in meta.get("independence_groups", [])} |
        {tuple(sorted(s.evidence_keys)) for s in support + counter})]
    return meta


def _confidence(old, meta, sources, settings):
    aliases = {alias: set(source.evidence_keys) for source in sources
               for alias in [source.source_id, *source.source_refs, *source.evidence_keys]}
    legacy = set()
    for ref in getattr(old, "source_refs", []):
        legacy.update(aliases.get(ref, {ref}))
    # Preserve the pre-feature evidence baseline, but never count an alias again.
    prior_meta = getattr(old, "revision_metadata", {}) if old else {}
    base = prior_meta.get("confidence_baseline", getattr(old, "confidence", settings.initial_candidate_confidence))
    baseline_keys = set(prior_meta.get("baseline_evidence_keys", sorted(legacy)))
    meta["confidence_baseline"] = base
    meta["baseline_evidence_keys"] = sorted(baseline_keys)
    keys = set(meta["support"]) - baseline_keys
    offset = prior_meta.get("initial_support_offset", 0 if old else 1)
    meta["initial_support_offset"] = offset
    n = max(0, independent_count(keys, meta.get("independence_groups", [])) - offset)
    result = update_confidence(base, n_new_evidence=n, settings=settings)
    return result * settings.qq_impression_counter_multiplier ** independent_count(set(meta["counter"]), meta.get("independence_groups", []))


def validate_proposal(preset, run, proposal):
    proposal = RevisionProposal.model_validate(proposal)
    sources = _sources(run["sources"])
    scope = _scope(sources)
    if any(s.owner_id != run["owner_id"] or s.preset_id != preset.preset_id or s.preset_id != run["preset_id"] for s in sources):
        raise ValueError("daily_revision_owner_mismatch")
    incoming = {s.source_id: s for s in sources}
    if len(incoming) != len(sources):
        raise ValueError("daily_revision_duplicate_source")
    context = revision_context(preset, sources)
    known = {s.source_id: s for s in _sources(context["historical_sources"])} | incoming
    covered = []
    classifications = dict(preset.daily_revision_state.get("classifications", {}))
    ignored = set()
    for part in proposal.cleaned:
        if not set(part.source_ids) <= incoming.keys() or len(set(part.source_ids)) != len(part.source_ids):
            raise ValueError("daily_revision_cleaning_unknown_source")
        covered.extend(part.source_ids)
        classifications.update({ref: part.evidence_kind for ref in part.source_ids})
        if part.disposition == "ignore":
            ignored.update(part.source_ids)
        if part.disposition != "ignore" and not part.summary.strip():
            raise ValueError("daily_revision_cleaning_empty_summary")
        for ref in part.source_ids:
            source = incoming[ref]
            if source.self_generated and part.evidence_kind != "self_expression":
                raise ValueError("daily_revision_self_expression_misattributed")
            if source.authority == "speaker_utterance" and part.evidence_kind == "observation":
                raise ValueError("daily_revision_utterance_promoted_to_observation")
    if len(covered) != len(set(covered)) or set(covered) != incoming.keys():
        raise ValueError("daily_revision_cleaning_coverage_incomplete")
    for part in proposal.cleaned:
        if part.disposition == "merge":
            # A merged conversational experience remains one independent support.
            merged_keys = tuple(sorted({k for ref in part.source_ids for k in known[ref].evidence_keys}))
            for ref in part.source_ids:
                known[ref] = known[ref].model_copy(update={"evidence_keys": merged_keys})
    for item in [*proposal.memories, *proposal.impressions, *proposal.relationships]:
        if ignored.intersection(item.evidence_ids + item.counterevidence_ids):
            raise ValueError("daily_revision_ignored_evidence_used")
        _evidence(item, known, classifications)
    allowed = set(context["allowed_subjects"]) | {scope.conversation_id, "user" if scope.channel == "local_chat" else None}
    for item in proposal.memories:
        if item.subject not in allowed:
            raise ValueError("daily_revision_subject_not_authenticated")
    for item in proposal.impressions:
        if not set(item.subject_refs) <= allowed:
            raise ValueError("daily_revision_subject_not_authenticated")
        support, contrary = _evidence(item, known, classifications, independent=True)
        if item.kind == "person" and any(not _same_subject(s.author_principal, item.subject_refs[0], scope) for s in support + contrary):
            raise ValueError("daily_revision_person_evidence_subject_mismatch")
    for item in proposal.relationships:
        if item.target not in context["allowed_relation_targets"]:
            raise ValueError("daily_revision_relationship_target_not_authenticated")
        evidence, contrary = _evidence(item, known, classifications, independent=True)
        # A member's behaviour cannot change a different member's relationship.
        if any(s.relation_target != item.target for s in evidence + contrary):
            raise ValueError("daily_revision_relationship_evidence_target_mismatch")
    if not set(proposal.depends_on_run_ids) <= set(context["active_revision_ids"]):
        raise ValueError("daily_revision_unknown_dependency")
    return proposal, sources, known, classifications, scope


def build_revision(preset, run, proposal, settings: Settings, *, now=None):
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("daily_revision_aware_time_required")
    preset = integrate_external_snapshot(preset)
    state = state_for(preset, initialize=True)
    if run["run_id"] in state["receipts"]:
        return preset
    proposal, sources, known, classifications, scope = validate_proposal(preset, run, proposal)
    if any(s.occurred_at > moment for s in sources):
        raise ValueError("daily_revision_future_source")
    before = projection(preset)
    working = deepcopy(before)
    run_id = run["run_id"]
    relation_effects = {}
    state["policy"] = state.get("policy", {k: getattr(settings, k) for k in (
        "initial_candidate_confidence", "confidence_increment", "consolidation_threshold",
        "qq_impression_initial_confidence", "qq_impression_support_increment", "qq_impression_counter_multiplier",
        "qq_impression_min_support_episodes", "qq_impression_min_distinct_days", "qq_impression_stable_confidence")})
    settings = settings.model_copy(update=state["policy"])
    state["classifications"] = dict(classifications)
    touched = set()

    def put(collection, identifier, value):
        key = f"{collection}/{identifier}"
        if key in touched:
            raise ValueError("daily_revision_duplicate_target")
        touched.add(key)
        working[key] = value.model_dump(mode="json") if hasattr(value, "model_dump") else deepcopy(value)

    # Sources remain separate immutable objects. Derived episodes reference them.
    for source in sources:
        previous = state["sources"].get(source.source_id)
        raw = source.model_dump(mode="json")
        if previous is not None and previous != raw:
            raise ValueError("daily_revision_source_changed")
        state["sources"][source.source_id] = raw
    for item in proposal.memories:
        supports, counters = _evidence(item, known, classifications, independent=item.kind == "semantic")
        selected = supports + counters
        meta = None
        if item.kind == "episodic":
            identifier = item.target_id or _key("depisode_", scope.model_dump(mode="json"), sorted({k for s in selected for k in s.evidence_keys}))
            previous_raw = working.get("episodic/" + identifier)
            old = EpisodicEvent.model_validate(previous_raw) if previous_raw else None
            if item.target_id and not old:
                raise ValueError("daily_revision_unknown_target")
            _bound_old(old, scope)
            meta = _metadata(old, supports, counters, run_id=run_id, now=moment,
                             status="disputed" if counters or item.operation == "dispute" else "active")
            event = EpisodicEvent(event_id=identifier, time=max(s.occurred_at for s in selected),
                participants=sorted({s.author_principal for s in selected}), summary=item.statement,
                objective_fact="\n".join(f"{s.author_principal}: {s.text}" for s in selected),
                importance=old.importance if old else 0.5, emotional_valence=old.emotional_valence if old else 0,
                evidence=sorted({s.source_id for s in selected}), source_refs=sorted({k for s in selected for k in s.evidence_keys}),
                channel=scope.channel, session_id=scope.conversation_id, scope=scope,
                event_type="daily_revision", tier=old.tier if old else "mid", revision_metadata=meta,
                processing_version=VERSION)
            if old:
                event = old.model_copy(update={k: v for k, v in event.__dict__.items() if k in {
                    "summary", "objective_fact", "evidence", "source_refs", "revision_metadata", "processing_version"}})
            put("episodic", identifier, event)
            continue
        # Reuse a prior canonical fact/candidate instead of making a daily copy.
        candidate_raw = next((v for k, v in working.items() if k.startswith("candidates/") and
            (v["candidate_id"] == item.target_id or (not item.target_id and v.get("scope") == scope.model_dump(mode="json")
            and _same_subject(v.get("subject"), item.subject, scope) and v.get("predicate") == item.predicate and v.get("value") == item.value))), None)
        fact_raw = next((v for k, v in working.items() if k.startswith("semantic/") and
            (v["fact_id"] == item.target_id or (not item.target_id and v.get("scope") == scope.model_dump(mode="json")
            and _same_subject(v.get("subject"), item.subject, scope) and v.get("predicate") == item.predicate and v.get("value") == item.value))), None)
        old = CandidateMemory.model_validate(candidate_raw) if candidate_raw else SemanticFact.model_validate(fact_raw) if fact_raw else None
        if item.target_id and old is None:
            raise ValueError("daily_revision_unknown_target")
        _bound_old(old, scope)
        if old and (not _same_subject(old.subject, item.subject, scope) or old.predicate != item.predicate):
            raise ValueError("daily_revision_changes_target_identity")
        if not supports and old is None:
            raise ValueError("daily_revision_new_fact_needs_support")
        meta = _metadata(old, supports, counters, run_id=run_id, now=moment,
                         status="disputed" if counters or item.operation == "dispute" else "active")
        confidence = _confidence(old, meta, list(known.values()), settings)
        identifier = candidate_raw["candidate_id"] if candidate_raw else _key("dcandidate_", scope.model_dump(mode="json"), item.subject, item.predicate, item.value)
        fact_id = fact_raw["fact_id"] if fact_raw else (candidate_raw or {}).get("consolidated_fact_id") or _key("dsemantic_", scope.model_dump(mode="json"), item.subject, item.predicate, item.value)
        source_refs = sorted(set(getattr(old, "source_refs", [])) | {k for s in selected for k in s.evidence_keys})
        evidence = sorted(set(getattr(old, "evidence", getattr(old, "supporting_events", []))) | {s.source_id for s in selected})
        stable = confidence >= settings.consolidation_threshold and meta["status"] == "active"
        candidate = CandidateMemory(candidate_id=identifier, claim=item.statement, source="daily_revision",
            confidence=confidence, status="consolidated" if stable else "candidate", evidence=evidence,
            subject=item.subject, predicate=item.predicate, value=item.value or item.statement,
            created_at=getattr(old, "created_at", moment), updated_at=moment, consolidated_fact_id=fact_id if stable else None,
            scope=scope, source_refs=source_refs, processing_version=VERSION, revision_metadata=meta)
        put("candidates", identifier, candidate)
        if stable or fact_raw:
            fact = SemanticFact(fact_id=fact_id, subject=item.subject, predicate=item.predicate,
                value=item.value or item.statement, confidence=confidence, supporting_events=evidence,
                formed_at=old.formed_at if isinstance(old, SemanticFact) else moment, scope=scope,
                source_refs=source_refs, processing_version=VERSION, revision_metadata=meta)
            put("semantic", fact_id, fact)
        for superseded_id in item.supersedes:
            if item.operation != "supersede" or not supports:
                raise ValueError("daily_revision_supersession_needs_new_support")
            raw = working.get("semantic/" + superseded_id)
            if not raw or superseded_id == fact_id:
                raise ValueError("daily_revision_invalid_superseded_target")
            old_fact = SemanticFact.model_validate(raw)
            _bound_old(old_fact, scope)
            if old_fact.subject != item.subject or old_fact.predicate != item.predicate:
                raise ValueError("daily_revision_supersedes_unrelated_fact")
            updated_meta = {**old_fact.revision_metadata, "status": "superseded" if stable else "disputed",
                            "superseded_by": fact_id if stable else identifier,
                            "superseded_by_kind": "semantic" if stable else "candidate",
                            "valid_until": max(s.occurred_at for s in supports).isoformat(), "run_id": run_id}
            put("semantic", superseded_id, old_fact.model_copy(update={"revision_metadata": updated_meta}))
    for item in proposal.impressions:
        supports, counters = _evidence(item, known, classifications, independent=True)
        identifier = impression_key(scope, item.kind, item.subject_refs, item.topic, item.context)
        if item.target_id and item.target_id != identifier:
            raise ValueError("daily_revision_impression_identity_changed")
        raw = working.get("impressions/" + identifier)
        old = ImpressionMemory.model_validate(raw) if raw else None
        if not supports and old is None:
            raise ValueError("daily_revision_new_impression_needs_support")
        evidence = deepcopy(raw["evidence"]) if raw else []
        continuations = deepcopy(raw.get("continuation_evidence", [])) if raw else []
        for stance, selected in (("support", supports), ("contradict", counters)):
            for source in selected:
                keys = set(source.evidence_keys)
                # QQ continuation boundaries are scheduling details, not support.
                prior = next((e for e in evidence if keys.intersection(e["source_ids"])), None)
                continuation = next((e for e in continuations if keys.intersection(e["source_ids"])), None)
                if continuation and not prior:
                    continue
                eid = prior["episode_id"] if prior else _key("depisode_", scope.model_dump(mode="json"), sorted(keys))
                if "episodic/" + eid not in working:
                    event = EpisodicEvent(event_id=eid, time=source.occurred_at,
                        participants=[source.author_principal], summary=source.text,
                        objective_fact=f"{source.author_principal}: {source.text}", importance=0.5,
                        emotional_valence=0, evidence=[source.source_id], source_refs=[source.source_id],
                        channel=scope.channel, session_id=scope.conversation_id, scope=scope,
                        event_type="daily_evidence", tier="mid", processing_version=VERSION)
                    put("episodic", eid, event)
                evidence = [e for e in evidence if not keys.intersection(e["source_ids"])]
                evidence.append({"episode_id": eid, "source_ids": sorted(keys | set(prior["source_ids"] if prior else [])),
                    "time": prior["time"] if prior else source.occurred_at.isoformat(), "stance": stance,
                    "rationale": "daily evidence revision", "statement": item.statement,
                    "independent": True, "quoted": False, "self_generated": False})
        evidence.sort(key=lambda e: (e["time"], e["episode_id"]))
        if not evidence:
            continue
        revision = old.revision + 1 if old else 1
        history = deepcopy(raw["history"]) if raw else []
        history.append({"revision": revision, "statement": item.statement, "confidence": 0,
            "status": "provisional", "evidence_episode_id": evidence[-1]["episode_id"],
            "evidence_stance": evidence[-1]["stance"], "revised_at": moment.isoformat()})
        value = {"impression_id": identifier, "scope": scope.model_dump(mode="json"), "kind": item.kind,
            "subject_refs": sorted(set(item.subject_refs)), "topic": item.topic, "context": item.context,
            "first_impression": old.first_impression if old else item.statement, "current_impression": item.statement,
            "first_episode_id": old.first_episode_id if old else evidence[0]["episode_id"],
            "first_observed_at": old.first_observed_at.isoformat() if old else evidence[0]["time"],
            "confidence": 0, "status": "provisional", "strength": old.strength if old else 1,
            "strength_updated_at": old.strength_updated_at.isoformat() if old else moment.isoformat(),
            "evidence": evidence, "continuation_evidence": continuations, "supporting_episode_ids": [],
            "contradicting_episode_ids": [], "revision": revision, "history": history, "updated_at": moment.isoformat()}
        normalized = _normalize_impression(value, settings)
        normalized["history"][-1].update(confidence=normalized["confidence"], status=normalized["status"])
        if raw and all(raw.get(k) == normalized.get(k) for k in ("current_impression", "evidence", "continuation_evidence")):
            continue
        put("impressions", identifier, ImpressionMemory.model_validate(normalized))
        # Update the existing QQ materialized candidate/fact, using its established IDs.
        suffix = identifier.removeprefix("impression_")
        candidate_id, fact_id = "qqcandidate_" + suffix, "qqsemantic_" + suffix
        refs = sorted({k for e in evidence for k in e["source_ids"]})
        candidate = CandidateMemory(candidate_id=candidate_id, claim=item.statement, source=identifier,
            confidence=normalized["confidence"], status="consolidated" if normalized["status"] == "stable" else "candidate",
            evidence=[e["episode_id"] for e in evidence], subject="|".join(item.subject_refs) or scope.conversation_id,
            predicate=item.topic or item.kind, value=item.statement, created_at=normalized["first_observed_at"],
            updated_at=moment, consolidated_fact_id=fact_id if normalized["status"] == "stable" else None,
            scope=scope, source_refs=refs, processing_version=VERSION,
            revision_metadata={"run_id": run_id, "revision": revision, "status": "active" if normalized["status"] == "stable" else "disputed"})
        put("candidates", candidate_id, candidate)
        if normalized["status"] == "stable":
            put("semantic", fact_id, SemanticFact(fact_id=fact_id, subject=candidate.subject, predicate=candidate.predicate,
                value=item.statement, confidence=candidate.confidence, supporting_events=candidate.evidence,
                formed_at=moment, scope=scope, source_refs=refs, processing_version=VERSION,
                revision_metadata=candidate.revision_metadata))
        else:
            working.pop("semantic/" + fact_id, None)
    for item in proposal.relationships:
        supports, counters = _evidence(item, known, classifications, independent=True)
        identifier = item.target
        raw = working.get("relationships/" + identifier)
        old = RelationshipState.model_validate(raw) if raw else RelationshipState(target=identifier, updated_at=moment)
        relationship_scope = _relation_scope(scope)
        _bound_old(old if raw else None, relationship_scope)
        meta = _metadata(old, supports, counters, run_id=run_id, now=moment)
        keys = sorted({key for s in supports + counters for key in s.evidence_keys})
        if not keys:
            raise ValueError("daily_revision_relationship_no_independent_keys")
        days = {datetime.fromisoformat(v["time"]).astimezone(ZoneInfo("Asia/Shanghai")).date()
                for v in meta["support"].values()}
        caps = {d: 0.15 if d == "recent_tension" else 0.05 for d in DIMENSIONS}
        deltas = item.deltas.model_dump()
        if len(days) < settings.qq_impression_min_distinct_days and deltas["attachment"] > 0:
            deltas["attachment"] = 0
        contributions = {d: {k: max(-caps[d], min(caps[d], deltas[d])) / len(keys) for k in keys} for d in DIMENSIONS}
        changed = old.model_copy(update={"scope": relationship_scope, "source_refs": sorted(set(old.source_refs) | set(keys)),
            "revision_metadata": meta, "shared_history_summary": item.statement,
            "last_major_event": (supports + counters)[-1].source_id, "updated_at": moment})
        put("relationships", identifier, changed)
        relation_effects["relationships/" + identifier] = {"contributions": contributions, "caps": caps, "day": run["day"],
            "evidence": {"support": {k: meta["support"][k] for s in supports for k in s.evidence_keys},
                         "counter": {k: meta["counter"][k] for s in counters for k in s.evidence_keys}}}
    referenced_ids = {i.target_id for i in [*proposal.memories, *proposal.impressions] if i.target_id}
    referenced_ids.update(ref for i in proposal.memories for ref in i.supersedes)
    dependencies = sorted(set(proposal.depends_on_run_ids) | {e["run_id"] for e in state["entries"]
        if e["run_id"] and e["key"].split("/", 1)[1] in referenced_ids
        and state["receipts"].get(e["run_id"], {}).get("status") == "applied"})
    effect_ids = append_effects(state, before, working, run_id=run_id, now=moment,
                               relation_effects=relation_effects, dependencies=dependencies)
    state["receipts"][run_id] = {"run_id": run_id, "commit_seq": state["seq"], "status": "applied",
        "effect_ids": effect_ids, "committed_at": moment.isoformat(), "proposal_digest": digest(proposal.model_dump(mode="json")),
        "source_ids": sorted(s.source_id for s in sources), "day": run["day"], "partition": run.get("partition"),
        "dependencies": dependencies, "invalidated_by": None}
    objects = replay(state, settings)
    return with_projection(preset, objects, state).model_copy(update={"updated_at": moment})


def rollback_revision(preset, run_id, *, now=None):
    preset = integrate_external_snapshot(preset)
    state = state_for(preset)
    if run_id not in state.get("receipts", {}):
        raise ValueError("daily_revision_unknown_receipt")
    if state["receipts"][run_id]["status"] == "rolled_back":
        return preset
    invalid = {run_id}
    state["receipts"][run_id]["status"] = "rolled_back"
    state["receipts"][run_id]["rolled_back_at"] = (now or datetime.now(timezone.utc)).isoformat()
    for key, receipt in sorted(state["receipts"].items(), key=lambda kv: kv[1]["commit_seq"]):
        if receipt["status"] == "applied" and invalid.intersection(receipt.get("dependencies", [])):
            receipt.update(status="invalidated", invalidated_by=run_id)
            invalid.add(key)
    state["seq"] += 1
    settings = Settings().model_copy(update=state.get("policy", {}))
    return with_projection(preset, replay(state, settings), state)
