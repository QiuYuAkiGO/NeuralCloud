"""Durable effects over the existing preset; no model calls during replay.

The preset is the commit authority. A frozen SQLite proposal is not a receipt.
External writers are captured at snapshot/install boundaries, including the
field-level differences needed to preserve subsequent independent changes.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from harness.core.models import CandidateMemory, EpisodicEvent, RelationshipState, SemanticFact

VERSION = "daily-revision-ledger-v1"
COLLECTIONS = {"episodic": ("event_id", EpisodicEvent), "semantic": ("fact_id", SemanticFact),
               "candidates": ("candidate_id", CandidateMemory), "relationships": ("target", RelationshipState)}
SET_LISTS = {"evidence", "source_refs", "supporting_events", "supporting_evidence", "participants",
             "continuation_evidence", "supporting_episode_ids", "contradicting_episode_ids", "history"}
DERIVED_IMPRESSION = {"confidence", "status", "supporting_episode_ids", "contradicting_episode_ids"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def independent_count(keys, groups):
    """Count connected evidence groups; later re-chunking cannot split experience."""
    components = [{key} for key in keys]
    for group in groups:
        overlapping = [component for component in components if component.intersection(group)]
        if overlapping:
            components = [component for component in components if component not in overlapping]
            components.append(set.union(*overlapping))
    return len(components)


def projection(preset) -> dict:
    result = {}
    for collection, (identifier, _) in COLLECTIONS.items():
        for row in getattr(preset, collection):
            result[f"{collection}/{getattr(row, identifier)}"] = row.model_dump(mode="json")
    for key, value in preset.qq_memory_state.get("impressions", {}).items():
        result[f"impressions/{key}"] = deepcopy(value)
    return result


def state_for(preset, *, initialize=False) -> dict:
    state = deepcopy(preset.daily_revision_state)
    if not state and not initialize:
        return {}
    if state and state.get("schema_version") != VERSION:
        raise ValueError("daily_revision_ledger_version_unknown")
    if not state:
        current = projection(preset)
        state = {"schema_version": VERSION, "seq": 0, "baseline": deepcopy(current),
                 "projection": current, "entries": [], "receipts": {}, "sources": {}}
    return state


def _list_key(value):
    if isinstance(value, dict):
        if "episode_id" in value:
            return "episode:" + value["episode_id"]
        if "revision" in value and "evidence_episode_id" in value:
            return "history:" + digest(value)
    return digest(value)


def _patch(before: dict | None, after: dict | None) -> dict:
    if after is None:
        return {"delete": True}
    before = before or {}
    result = {"seed": deepcopy(after), "fields": {}, "lists": {}, "remove": []}
    for key in set(before) | set(after):
        if before.get(key) == after.get(key) and key in before:
            continue
        if key not in after:
            result["remove"].append(key)
        elif key in SET_LISTS and isinstance(after[key], list):
            old = {_list_key(item): item for item in before.get(key, [])}
            new = {_list_key(item): item for item in after[key]}
            result["lists"][key] = {"remove": [k for k in old if k not in new],
                                      "put": [v for k, v in new.items() if old.get(k) != v]}
        else:
            result["fields"][key] = deepcopy(after[key])
    return result


def _apply_patch(current, patch):
    if patch.get("delete"):
        return None
    if current is None:
        current = deepcopy(patch["seed"])
        # A later write can survive the deletion of the object it first saw.
        # Only its newly contributed evidence survives, not the deleted seed's.
        for key in patch["lists"]:
            current[key] = []
    else:
        current = deepcopy(current)
    current.update(deepcopy(patch["fields"]))
    for key in patch["remove"]:
        current.pop(key, None)
    for key, change in patch["lists"].items():
        values = {_list_key(item): item for item in current.get(key, [])}
        for identifier in change["remove"]:
            values.pop(identifier, None)
        for item in change["put"]:
            values[_list_key(item)] = deepcopy(item)
        current[key] = list(values.values())
    return current


def append_effects(state, before, after, *, run_id=None, now=None, relation_effects=None, dependencies=()):
    effect_ids = []
    moment = (now or datetime.now(timezone.utc)).isoformat()
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        state["seq"] += 1
        effect_id = f"effect_{state['seq']}"
        effect = {"id": effect_id, "seq": state["seq"], "key": key, "run_id": run_id,
                  "at": moment, "before": deepcopy(before.get(key)), "after": deepcopy(after.get(key)),
                  "patch": _patch(before.get(key), after.get(key)), "dependencies": list(dependencies)}
        if relation_effects and key in relation_effects:
            effect["relationship"] = deepcopy(relation_effects[key])
        state["entries"].append(effect)
        effect_ids.append(effect_id)
    state["projection"] = deepcopy(after)
    return effect_ids


def integrate_external_snapshot(preset):
    """Capture all online/GUI writes before a durable preset or revision commit."""
    state = state_for(preset)
    if not state:
        return preset
    current = projection(preset)
    append_effects(state, state["projection"], current)
    return preset.model_copy(update={"daily_revision_state": state})


def _normalize_impression(value, settings):
    """Recompute confidence from surviving unique evidence, never from replay count."""
    from harness.memory.impressions import ImpressionEvidence, _confidence
    from zoneinfo import ZoneInfo
    evidence, seen = [], set()
    for raw in sorted(value.get("evidence", []), key=lambda e: (e["time"], e["episode_id"])):
        item = ImpressionEvidence.model_validate(raw)
        if not item.independent or item.quoted or item.self_generated or seen.intersection(item.source_ids):
            continue
        seen.update(item.source_ids)
        evidence.append(item)
    if not evidence:
        return None
    supports = [e for e in evidence if e.stance == "support"]
    confidence = _confidence(evidence, settings)
    days = {e.time.astimezone(ZoneInfo("Asia/Shanghai")).date() for e in supports}
    stable = (len(supports) >= settings.qq_impression_min_support_episodes
              and len(days) >= settings.qq_impression_min_distinct_days
              and confidence >= settings.qq_impression_stable_confidence)
    last_counter = max((i for i, e in enumerate(evidence) if e.stance == "contradict"), default=-1)
    unresolved = last_counter >= 0 and (not stable or
        len([e for e in evidence[last_counter + 1:] if e.stance == "support"]) < settings.qq_impression_min_support_episodes)
    result = deepcopy(value)
    result.update(evidence=[e.model_dump(mode="json") for e in evidence], confidence=confidence,
                  status="dormant" if value.get("status") == "dormant" else "disputed" if unresolved else "stable" if stable else "provisional",
                  supporting_episode_ids=[e.episode_id for e in supports],
                  contradicting_episode_ids=[e.episode_id for e in evidence if e.stance == "contradict"])
    episode_ids = {e.episode_id for e in evidence}
    if result["first_episode_id"] not in episode_ids:
        first = evidence[0]
        result.update(first_episode_id=first.episode_id, first_observed_at=first.time.isoformat(),
                      first_impression=first.statement)
    result["history"] = [h for h in result.get("history", []) if h["evidence_episode_id"] in episode_ids]
    if not result["history"]:
        result["history"] = [{"revision": 1, "statement": evidence[-1].statement, "confidence": confidence,
                              "status": result["status"], "evidence_episode_id": evidence[-1].episode_id,
                              "evidence_stance": evidence[-1].stance, "revised_at": evidence[-1].time.isoformat()}]
    return result


def replay(state, settings):
    """Rebuild after arbitrary batch removal, preserving subsequent field deltas."""
    objects = deepcopy(state["baseline"])
    active = [entry for entry in state["entries"] if not entry["run_id"] or
              state["receipts"].get(entry["run_id"], {}).get("status") == "applied"]
    # For each canonical source/dimension only the latest surviving assessment counts.
    winners = {}
    for entry in active:
        for dimension, contributions in entry.get("relationship", {}).get("contributions", {}).items():
            for source_key in contributions:
                winners[(entry["key"], dimension, source_key)] = entry["id"]
    day_totals = {}
    for entry in active:
        key = entry["key"]
        previous = objects.get(key)
        if previous is None and entry["before"] is not None:
            patch = entry["patch"]
            has_new_evidence = any(patch.get("lists", {}).get(field, {}).get("put")
                                   for field in ("evidence", "supporting_events", "source_refs"))
            explicit_relation = key.startswith("relationships/") and (bool(entry.get("relationship")) or any(
                d in patch.get("fields", {}) for d in ("closeness", "trust", "attachment", "recent_tension")))
            if not has_new_evidence and not explicit_relation:
                continue
        updated = _apply_patch(previous, entry["patch"])
        relation = entry.get("relationship")
        if relation and updated:
            metadata = updated.setdefault("revision_metadata", {})
            prior_metadata = (previous or {}).get("revision_metadata", {})
            for stance in ("support", "counter"):
                metadata[stance] = deepcopy(prior_metadata.get(stance, {}))
            for stance, additions in relation.get("evidence", {}).items():
                for source_key, evidence in additions.items():
                    metadata["counter" if stance == "support" else "support"].pop(source_key, None)
                    metadata[stance][source_key] = deepcopy(evidence)
            for dimension, contributions in relation["contributions"].items():
                baseline = (previous or {}).get(dimension, 0.0 if dimension == "recent_tension" else 0.5)
                delta = sum(amount for source_key, amount in contributions.items()
                            if winners.get((key, dimension, source_key)) == entry["id"])
                cap = relation["caps"][dimension]
                bucket = (key, relation["day"], dimension)
                used = day_totals.get(bucket, 0.0)
                allowed = max(-cap, min(cap, used + delta)) - used
                day_totals[bucket] = used + allowed
                updated[dimension] = min(1.0, max(0.0, baseline + allowed))
        if updated is None:
            objects.pop(key, None)
        else:
            objects[key] = updated
    for key, value in list(objects.items()):
        if key.startswith("impressions/"):
            normalized = _normalize_impression(value, settings)
            if normalized:
                objects[key] = normalized
            else:
                objects.pop(key)
    for key, value in list(objects.items()):
        meta = value.get("revision_metadata", {})
        if not key.startswith(("semantic/", "candidates/")) or "confidence_baseline" not in meta:
            continue
        aliases = {alias: set(s["evidence_keys"]) for s in state.get("sources", {}).values()
                   for alias in (s["source_id"], *s["source_refs"], *s["evidence_keys"])}
        surviving = set()
        for ref in value.get("source_refs", []):
            surviving.update(aliases.get(ref, {ref}))
        for stance in ("support", "counter"):
            meta[stance] = {k: v for k, v in meta.get(stance, {}).items() if k in surviving}
        meta["source_ids"] = sorted({v["source_id"] for stance in ("support", "counter") for v in meta[stance].values()})
        groups = meta.get("independence_groups", [])
        support_keys = (set(meta["support"]) | (surviving - set(meta["counter"]))) - set(meta.get("baseline_evidence_keys", []))
        count = max(0, independent_count(support_keys, groups) - meta.get("initial_support_offset", 0))
        from harness.memory.writer.confidence import update_confidence
        confidence = update_confidence(meta["confidence_baseline"], n_new_evidence=count, settings=settings)
        confidence *= settings.qq_impression_counter_multiplier ** independent_count(set(meta["counter"]), groups)
        value["confidence"] = confidence
        if key.startswith("candidates/"):
            stable = confidence >= settings.consolidation_threshold and meta.get("status") == "active"
            value["status"] = "consolidated" if stable else "candidate"
            if not stable:
                linked = value.get("consolidated_fact_id")
                value["consolidated_fact_id"] = None
                if linked and "semantic/" + linked in objects:
                    objects["semantic/" + linked]["revision_metadata"]["status"] = "disputed"
    # QQ candidates/facts are materialized views of the single impression store.
    for key, value in list(objects.items()):
        if not key.startswith("candidates/qqcandidate_"):
            continue
        impression_id = value.get("source")
        impression = objects.get("impressions/" + str(impression_id))
        if impression is None:
            continue
        value.update(claim=impression["current_impression"], value=impression["current_impression"],
                     confidence=impression["confidence"], evidence=[e["episode_id"] for e in impression["evidence"]],
                     source_refs=sorted({ref for e in impression["evidence"] for ref in e["source_ids"]}))
        fact_key = "semantic/qqsemantic_" + str(impression_id).removeprefix("impression_")
        if impression["status"] != "stable":
            value.update(status="candidate", consolidated_fact_id=None)
            objects.pop(fact_key, None)
        elif fact_key in objects:
            objects[fact_key].update(confidence=value["confidence"], value=value["value"],
                                     supporting_events=value["evidence"], source_refs=value["source_refs"])
    return objects


def with_projection(preset, objects, state):
    updates = {collection: [] for collection in COLLECTIONS}
    qq = deepcopy(preset.qq_memory_state)
    qq["impressions"] = {}
    for key, value in objects.items():
        collection = key.split("/", 1)[0]
        if collection == "impressions":
            qq["impressions"][value["impression_id"]] = value
        else:
            updates[collection].append(COLLECTIONS[collection][1].model_validate(value))
    state["projection"] = deepcopy(objects)
    qq["revision"] = qq.get("revision", 0) + 1
    updates.update(qq_memory_state=qq, daily_revision_state=state)
    return preset.model_copy(update=updates)


def install_revision(host, preset):
    """Install only after atomic preset save, while Cognitive owner is locked."""
    memory = host.chat.memory
    for collection, (identifier, _) in COLLECTIONS.items():
        store = getattr(memory.stores, "relationship" if collection == "relationships" else collection)
        desired = {getattr(row, identifier): row for row in getattr(preset, collection)}
        for row in store.list_all():
            if getattr(row, identifier) not in desired:
                store.delete(getattr(row, identifier))
        for key, row in desired.items():
            if collection == "relationships":
                store.upsert(row)
            elif store.get(key) is None:
                store.insert(row)
            else:
                store.update(row)
    memory.qq_memory_state = deepcopy(preset.qq_memory_state)
    memory.daily_revision_state = deepcopy(preset.daily_revision_state)
    host.adapter._component_revisions["memory"] += 1
    host.adapter._component_revisions["maintenance"] += 1
    host.adapter._component_revisions["relationship"] += 1
    host.adapter._revision += 1
    host.adapter._qq_memory_publication_pending = True
    host.adapter._daily_revision_publication_pending = True
    host.adapter._persist_state()
    host.adapter._qq_memory_publication_pending = False
    host.adapter._daily_revision_publication_pending = False
