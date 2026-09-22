"""One Cognitive owner's asynchronous QQ memory maintenance and scoped recall.

Model work produces proposals outside the owner lock. Only an atomically saved
working preset commits memory effects; the inbox acknowledgement is recoverable.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import time

from pydantic import BaseModel, ConfigDict

from gui.presets import atomic_write_json, snapshot_memory
from harness.config import DEFAULT_QQ_INPUT_TOKEN_BUDGET, DEFAULT_QQ_MEMORY_RECALL_CHARACTERS
from harness.core.models import CandidateMemory, EpisodicEvent, MemoryScope, SemanticFact
from harness.qq.conversation import conversation_audience, conversation_scope, private_scope_id


VERSION = "qq-memory-v1"


def foreground_pending(host) -> bool:
    """Include commands waiting for the owner lock and shielded RPC work."""
    return any(not task.done() for task in getattr(host.adapter, "command_tasks", ()))


async def wait_foreground_idle(host, stopping: asyncio.Event) -> bool:
    """Wait outside owner locks without polling or cancelling foreground work."""
    while not stopping.is_set():
        commands = tuple(task for task in getattr(host.adapter, "command_tasks", ()) if not task.done())
        if not commands:
            return True
        changed = asyncio.Event()
        def completed(_):
            changed.set()
        for task in commands:
            task.add_done_callback(completed)
        wake = asyncio.create_task(changed.wait())
        shutdown = asyncio.create_task(stopping.wait())
        try:
            await asyncio.wait((wake, shutdown), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in commands:
                task.remove_done_callback(completed)
            wake.cancel()
            shutdown.cancel()
            await asyncio.gather(wake, shutdown, return_exceptions=True)
    return False


def now_utc():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def memory_state(value):
    state = deepcopy(value or {})
    if state.get("schema_version", VERSION) != VERSION:
        raise ValueError("qq_memory_state_version_unknown")
    state.setdefault("schema_version", VERSION)
    for key in ("impressions", "receipts", "recalls"):
        state.setdefault(key, {})
    state.setdefault("revision", 0)
    return state


def proposal_context(state, job):
    """Bound old interpretations separately from immutable raw evidence."""
    members = {f"qq:member:{source.member_id}" for source in job.sources}
    terms = _tokens(" ".join(source.text for source in job.sources))
    rows = [entry for entry in state["impressions"].values()
            if entry.get("scope", {}).get("conversation_id") == conversation_audience(job.group_id)]
    rows.sort(key=lambda row: (bool(members.intersection(row["subject_refs"])),
                               len(terms & _tokens(row["current_impression"])),
                               max([row["updated_at"], *[entry["time"] for entry in row.get("continuation_evidence", [])]])),
              reverse=True)
    selected = []
    for row in rows:
        item = {key: row[key] for key in ("impression_id", "scope", "kind", "subject_refs", "topic", "context",
                                           "first_impression", "current_impression", "confidence", "status", "revision")}
        for field in ("evidence", "continuation_evidence"):
            item[field] = [{key: evidence[key] for key in ("episode_id", "time", "stance", "statement")}
                           for evidence in row.get(field, [])[-3:]]
        if len(selected) < 24 and len(json.dumps([*selected, item], ensure_ascii=False)) <= 12000:
            selected.append(item)
    return selected


def _scope_matches(scope, group_id):
    try:
        conversation = conversation_scope(group_id)
        audience = conversation_audience(group_id)
    except ValueError:
        return False
    channel = "qq_private" if conversation["kind"] == "private" else "qq_group"
    return bool(scope and scope.owner_id == "misty" and scope.channel == channel
                and scope.conversation_id == audience and audience in scope.visibility)


def _tokens(text):
    text = str(text or "").casefold()
    words = set(re.findall(r"[a-z0-9_]{2,}", text))
    for part in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(part[i:i + 2] for i in range(len(part) - 1))
    return words


def _owner_local_recall_scope(scope):
    """Legacy local memories stay local; a QQ scope needs explicit authorization."""
    return scope is None or (scope.owner_id == "misty" and "local:main" in scope.visibility
        and not scope.channel.startswith("qq_") and not str(scope.conversation_id or "").startswith("qq:"))


def _recall_source_scope(scope, *, channel="local_chat", conversation_id=None):
    return {"source_channel": scope.channel if scope else channel,
            "source_conversation_id": scope.conversation_id if scope else conversation_id}


def _recall_person_key(entry):
    if entry.get("kind") != "person":
        return None
    return (entry.get("source_channel"), entry.get("source_conversation_id"), tuple(entry["subject_refs"]))


def _people_retrospective(query):
    """Recognize a broad people-impression request, not a named factual topic."""
    text = str(query or "")
    if re.search(r"[a-zA-Z0-9]", text):
        return False
    return bool(re.search(
        r"(?:(?:遇见|遇到|接触|认识)过?的?(?:人|群友|朋友)|群友|朋友们|大家)[，,\s]*"
        r"(?:给你|让你|带给你|留给你|对你|在你看来|在你心里)?"
        r"(?:有|是|的|什么|哪些|怎样|总体|整体|最初|目前|比较|深刻|留下|了|最|更)*"
        r"(?:感受|印象|看法|感觉)", text))


def select_recall(memory, *, group_id, member_id=None, query=None,
                  max_items=8, max_characters=None, now=None, settings=None,
                  allowed_conversation_ids=(), include_owner_local=False):
    """Bounded, side-effect-free recall from explicit caller-granted capabilities.

    The authenticated owner-private facade may supply additional authorized QQ
    conversations and local access. Query text and model output never grant it.
    """
    from harness.memory.impressions import ImpressionMemory
    from harness.memory.tier import qq_event_strength
    from harness.config import Settings

    moment = now or now_utc()
    conversation = conversation_scope(group_id)
    allowed = tuple(dict.fromkeys((group_id, *allowed_conversation_ids)))
    for key in allowed:
        conversation_scope(key)
    expanded = bool(allowed_conversation_ids or include_owner_local)

    def visible(scope):
        return any(_scope_matches(scope, key) for key in allowed)

    def visible_event(event):
        # Old QQ records without a trustworthy scope are never promoted into
        # owner-local access simply because they lack an audience.
        return visible(event.scope) or (include_owner_local and _owner_local_recall_scope(event.scope)
            and not event.channel.startswith("qq_")
            and not str(event.session_id or "").startswith(("qq:", "private:")))

    settings = settings or getattr(memory, "settings", None) or Settings()
    if max_characters is None:
        max_characters = settings.qq_memory_recall_max_characters
    terms = _tokens(query)
    member = f"qq:member:{member_id}" if member_id else None
    state = memory_state(getattr(memory, "qq_memory_state", {}))
    visible_events = {event.event_id: event for event in memory.stores.episodic.list_all() if visible_event(event)}
    active_events = {event.event_id: event for event in visible_events.values()
                     if event.tier != "forgotten"
                     and not (event.tier == "short" and event.expire_at and event.expire_at <= moment)}
    rows = []
    people_priority = set()
    # A repeated broad question and our old answer can otherwise outscore all
    # people impressions through exact wording alone. Reserve at most two
    # whole entries from other conversations, without changing factual queries.
    people_slots = (2 if include_owner_local and expanded and conversation["kind"] == "private"
                    and _people_retrospective(query) else 0)
    for raw in state["impressions"].values():
        item = ImpressionMemory.model_validate(raw)
        if not (visible(item.scope) or (include_owner_local and _owner_local_recall_scope(item.scope))):
            continue
        value = item.model_dump(mode="json")
        # Historical variants are inspectable, but only the current conclusion
        # and its counterevidence enter the small response context.
        text = str(value.get("current_impression", ""))
        evidence = value.get("evidence", [])
        if expanded:
            evidence = [entry for entry in evidence if entry.get("episode_id") in visible_events
                        and visible_events[entry["episode_id"]].scope == item.scope]
        if not any(entry.get("episode_id") in active_events for entry in evidence):
            continue
        support = [x for x in evidence if x.get("stance") == "support"]
        counter = [x for x in evidence if x.get("stance") == "contradict"]
        if value.get("status") in {"dormant", "forgotten"}:
            continue
        entry = {"record_id": value["impression_id"], "kind": value["kind"],
                 "subject_refs": value["subject_refs"], "topic": value.get("topic", ""),
                 "context": value.get("context", ""), "first_impression": value["first_impression"],
                 "first_episode_id": value["first_episode_id"],
                 "current_impression": text, "status": value["status"],
                 "confidence": value["confidence"], "authority": "misty_tentative_interpretation",
                 "supporting_episode_ids": [x["episode_id"] for x in support[-2:]],
                 "counterevidence": [{"episode_id": x["episode_id"], "statement": x["statement"],
                                      "rationale": x["rationale"]} for x in counter[-2:]]}
        if expanded:
            entry.update(_recall_source_scope(item.scope))
        score = 2 + (3 if member and member in value["subject_refs"] else 0)
        score += len(terms & _tokens(text + value.get("topic", "")))
        score *= max(0.05, max((qq_event_strength(active_events[e["episode_id"]], settings, now=moment)
                     for e in evidence if e.get("episode_id") in active_events), default=0.0))
        if (people_slots and item.kind == "person"
                and item.scope.conversation_id != conversation_audience(group_id)):
            people_priority.add(value["impression_id"])
        rows.append((score, value["impression_id"], entry))
    for event in active_events.values():
        score = event.importance + (2 if member and member in event.participants else 0)
        score += len(terms & _tokens(event.summary + " " + event.objective_fact))
        age = max(0, (moment - event.time).total_seconds()) / 86400
        score += 1 / (1 + age)
        score *= max(0.05, qq_event_strength(event, settings, now=moment))
        entry = {"record_id": event.event_id, "kind": "episode", "tier": event.tier,
                 "occurred_at": event.time.isoformat(), "summary": event.summary,
                 "objective_fact": event.objective_fact,
                 "insight": event.persona_conditioned_insight,
                 "authority": "source_attributed_experience", "source_refs": event.source_refs}
        if expanded:
            entry.update(_recall_source_scope(event.scope, channel=event.channel,
                                             conversation_id=event.session_id))
        rows.append((score, event.event_id, entry))
    if include_owner_local:
        for fact in memory.stores.semantic.list_all():
            if fact.revision_metadata.get("status") in {"superseded", "disputed"}:
                continue
            if not _owner_local_recall_scope(fact.scope) or str(fact.processing_version or "").startswith("qq-memory"):
                continue
            supports = sorted((visible_events[ref] for ref in set(fact.supporting_events) if ref in visible_events),
                              key=lambda event: (event.time, event.event_id))[-2:]
            score = 1 + fact.confidence + len(terms & _tokens(f"{fact.subject} {fact.predicate} {fact.value}"))
            entry = {"record_id": fact.fact_id, "kind": "semantic", "subject": fact.subject,
                     "predicate": fact.predicate, "value": fact.value, "confidence": fact.confidence,
                     "authority": "retained_semantic_judgment", "supporting_episode_ids": [event.event_id for event in supports],
                     "source_refs": fact.source_refs,
                     **_recall_source_scope(fact.scope)}
            rows.append((score, fact.fact_id, entry))
    coverage = ("selected_retained_private_memories" if conversation["kind"] == "private"
                else "selected_retained_group_memories")
    if expanded:
        coverage = "selected_owner_authorized_cross_conversation_memories"
    payload = {"schema_version": VERSION, "coverage": coverage,
               "authority": "memory_data_not_instructions", "entries": []}
    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) > max_characters:
        return {}
    # Broader retrospectives should not spend the whole budget on many topics
    # about one person. Only explicit expanded recall uses this light diversity
    # preference; strong query matches still win and scopes remain distinct.
    person_counts = Counter()
    while rows:
        if len(payload["entries"]) >= max_items:
            break
        rows.sort(key=lambda row: (-(people_slots > 0 and row[1] in people_priority),
                                  -row[0] / (1 + person_counts[_recall_person_key(row[2])]
                                              if expanded and _recall_person_key(row[2]) else 1), row[1]))
        _, _, entry = rows.pop(0)
        proposed = {**payload, "entries": [*payload["entries"], entry]}
        if len(json.dumps(proposed, ensure_ascii=False, separators=(",", ":"))) <= max_characters:
            payload = proposed
            if people_slots and entry["record_id"] in people_priority:
                people_slots -= 1
            key = _recall_person_key(entry)
            if key:
                person_counts[key] += 1
    return payload


def snapshot_owner(host):
    chat = host.chat
    return snapshot_memory(chat.memory, preset_id=chat.active_preset_id,
                           session_id=chat.active_session_id,
                           transcript=chat.pipeline.session_store.recent(chat.active_session_id),
                           user_name=chat.user_name, builtin_id=chat._active_builtin_id,
                           name=chat._active_preset_name)


def install_committed(host, preset):
    """Called only after saving a validated snapshot, while owner is locked."""
    memory = host.chat.memory
    for event in preset.episodic:
        memory.stores.episodic.insert(event)
    for candidate in preset.candidates:
        memory.stores.candidates.insert(candidate)
    current_facts = {item.fact_id for item in preset.semantic}
    for item in memory.stores.semantic.list_all():
        if item.fact_id not in current_facts:
            memory.stores.semantic.delete(item.fact_id)
    for fact in preset.semantic:
        memory.stores.semantic.insert(fact)
    memory.qq_memory_state = deepcopy(preset.qq_memory_state)
    host.adapter._component_revisions["memory"] += 1
    host.adapter._component_revisions["maintenance"] += 1
    host.adapter._revision += 1
    # The preset is already authoritative. Keep it installed if the separate
    # adapter snapshot fails, so a later local turn cannot overwrite its receipt
    # with stale memory. Publish this revision only after it is durable, and
    # retry publication without installing or strengthening the memory again.
    host.adapter._qq_memory_publication_pending = True
    host.adapter._persist_state()
    host.adapter._qq_memory_publication_pending = False


def build_commit(preset, job, proposal, settings, *, now=None, embeddings=None):
    """Validate and apply a frozen proposal to a detached working-preset DTO."""
    from harness.memory.impressions import (ImpressionEvidence, ImpressionMemory,
        ImpressionProposal, apply_impression, impression_key)
    from harness.memory.qq_assimilation import validate_proposal
    from harness.memory.tier import maintain_qq_events, reinforce_qq_event, stamp_retention

    moment = now or now_utc()
    state = memory_state(preset.qq_memory_state)
    if job.preset_id != preset.preset_id or job.owner_id != "misty":
        raise ValueError("qq_memory_job_owner_mismatch")
    if job.job_id in state["receipts"]:
        if state["receipts"][job.job_id]["source_digest"] != job.source_digest:
            raise ValueError("qq_memory_receipt_source_conflict")
        return preset
    validate_proposal(job, proposal, old_impressions=tuple(state["impressions"].values()))
    sources = {item.source_id: item for item in job.sources}
    visibility = set.intersection(*(set(item.record.visibility) for item in job.sources))
    conversation = conversation_scope(job.group_id)
    channel = "qq_private" if conversation["kind"] == "private" else "qq_group"
    audience = conversation_audience(job.group_id)
    expected = {"local:main", audience}
    if not expected.issubset(visibility):
        raise ValueError("qq_memory_source_visibility_invalid")
    scope = MemoryScope(owner_id=job.owner_id, channel=channel,
                        conversation_id=audience, visibility=sorted(expected))
    events = {item.event_id: item.model_copy(deep=True) for item in preset.episodic}
    candidates = {item.candidate_id: item.model_copy(deep=True) for item in preset.candidates}
    facts = {item.fact_id: item.model_copy(deep=True) for item in preset.semantic}
    event_for_source = {}
    new_ids = []
    for part in proposal.episodes:
        selected = [sources[ref] for ref in part.source_ids]
        event_id = "qqepisode_" + _hash([job.preset_id, job.processing_version,
                                        job.group_id, sorted(part.source_ids)])[:32]
        for source in selected:
            event_for_source[source.source_id] = event_id
        objective = []
        for source in selected:
            objective.append(f"{source.member_id}: {source.text}")
            for analysis in source.record.image_analysis:
                if analysis.status == "described":
                    if analysis.description:
                        objective.append("[图片模型解读，非作者原话] " + analysis.description)
                    if analysis.ocr_text:
                        objective.append("[图片OCR模型识别，非作者原话] " + analysis.ocr_text)
        occurred = max(source.occurred_at for source in selected)
        event = EpisodicEvent(event_id=event_id, time=occurred,
            participants=sorted({f"qq:member:{source.member_id}" for source in selected}),
            summary=part.summary, objective_fact="\n".join(objective),
            persona_conditioned_insight=part.insight,
            insight_confidence=0.15 if part.insight else None,
            importance=part.importance, emotional_valence=part.valence,
            evidence=sorted({ref for source in selected for ref in source.record.evidence_refs}),
            supporting_evidence=sorted(part.source_ids), channel=channel,
            session_id=scope.conversation_id, scope=scope, source_refs=sorted(part.source_ids),
            processing_version=job.processing_version, strength_updated_at=occurred,
            embedding=(embeddings or {}).get(event_id))
        event = stamp_retention(event, now=occurred, settings=settings)
        if event_id in events and events[event_id].source_refs != event.source_refs:
            raise ValueError("qq_memory_episode_identity_conflict")
        events[event_id] = event
        new_ids.append(event_id)
    updated_impressions = set()
    for item in proposal.impressions:
        if item.stance == "uncertain":
            continue
        selected = [sources[ref] for ref in item.source_ids]
        eligible = [source for source in selected if source.independent_eligible and not source.is_self]
        if item.kind == "person":
            eligible = [source for source in eligible
                        if f"qq:member:{source.member_id}" in item.subject_refs]
        if not eligible:
            continue
        subject_refs = sorted({ref if str(ref).startswith("qq:") else f"qq:member:{ref}"
                               for ref in item.subject_refs})
        previous = None
        if item.target_impression_id and item.stance != "context":
            previous = ImpressionMemory.model_validate(state["impressions"][item.target_impression_id])
        topic = previous.topic if previous else item.topic
        context = previous.context if previous else item.context
        key = impression_key(scope, item.kind, subject_refs, topic, context)
        if key in updated_impressions:
            continue
        if previous is None and key in state["impressions"]:
            previous = ImpressionMemory.model_validate(state["impressions"][key])
        # One judgement in a window is one independent experience even if the
        # model split that window into several related paragraphs.
        anchor = max(eligible, key=lambda source: (source.occurred_at, source.source_id))
        evidence = ImpressionEvidence(episode_id=event_for_source[anchor.source_id],
            source_ids=sorted({source.independent_key for source in eligible}),
            time=max(source.occurred_at for source in eligible),
            stance="contradict" if item.stance == "counter" else "support",
            rationale=("来自所列私聊原始观察的暂定解释；保留其作者归属。" if channel == "qq_private"
                       else "来自所列群聊原始观察的暂定解释；保留其作者归属。"), statement=item.statement)
        change = ImpressionProposal(kind=item.kind, subject_refs=subject_refs,
            topic=topic, context=context, statement=item.statement, evidence=evidence,
            contrast=item.stance == "counter", continues_episode_id=item.continues_episode_id)
        update = apply_impression(previous, change, scope=scope, now=evidence.time, settings=settings)
        if not update.changed or update.memory is None:
            continue
        impression = update.memory
        if previous is not None:
            # A delayed source keeps its observation clock for belief history,
            # while retained strength may already have been aged to wall time.
            impression = impression.model_copy(update={"strength_updated_at": max(
                impression.strength_updated_at, previous.strength_updated_at)})
        state["impressions"][impression.impression_id] = impression.model_dump(mode="json")
        updated_impressions.add(key)
        for ref in update.reinforce_episode_ids:
            if ref in events:
                event = events[ref]
                anchor = event.strength_updated_at or event.tier_updated_at or event.time
                strengthened = reinforce_qq_event(event, now=max(evidence.time, anchor),
                                                  adopted=False, settings=settings)
                events[ref] = strengthened.model_copy(update={"decay_demoted": False})
        suffix = impression.impression_id.removeprefix("impression_")
        candidate_id, fact_id = "qqcandidate_" + suffix, "qqsemantic_" + suffix
        evidence_ids = sorted({entry.episode_id for entry in impression.evidence})
        source_refs = sorted({ref for event_id in evidence_ids for ref in events[event_id].source_refs})
        candidate = CandidateMemory(candidate_id=candidate_id, claim=impression.current_impression,
            source=impression.impression_id, confidence=impression.confidence,
            status="consolidated" if impression.status == "stable" else "candidate",
            evidence=evidence_ids, subject="|".join(subject_refs) or scope.conversation_id,
            predicate=item.topic or item.kind, value=impression.current_impression,
            created_at=impression.first_observed_at, updated_at=impression.updated_at,
            consolidated_fact_id=fact_id if impression.status == "stable" else None,
            scope=scope, source_refs=source_refs, processing_version=job.processing_version)
        candidates[candidate_id] = candidate
        if impression.status == "stable":
            facts[fact_id] = SemanticFact(fact_id=fact_id, subject=candidate.subject,
                predicate=candidate.predicate, value=candidate.value,
                confidence=candidate.confidence, supporting_events=evidence_ids,
                formed_at=impression.updated_at, scope=scope, source_refs=source_refs,
                processing_version=job.processing_version)
        else:
            facts.pop(fact_id, None)
    # Use source time during chronological replay; age everything to wall time
    # after the frozen historical queue has drained, not before each old window.
    review_at = max(source.occurred_at for source in job.sources)
    maintained = maintain_qq_events(list(events.values()), settings, now=review_at)
    state["receipts"][job.job_id] = {"job_id": job.job_id, "source_digest": job.source_digest,
        "source_ids": sorted(sources), "episode_ids": new_ids,
        "proposal_digest": _hash(proposal.model_dump(mode="json")),
        "processing_version": job.processing_version, "committed_at": moment.isoformat()}
    state["ruleset_version"] = "qq-memory-continuation-v1"
    state["revision"] += 1
    return preset.model_copy(update={"episodic": maintained, "candidates": list(candidates.values()),
                                     "semantic": list(facts.values()), "qq_memory_state": state})


class QQMemoryControl(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    recall_enabled: bool = False
    private_enabled: bool = False
    private_recall_enabled: bool = False
    owner_private_full_recall_enabled: bool = False


class QQMemoryWorker:
    def __init__(self, host, *, control_path: Path, proposer=None):
        self.host = host
        self.control_path = Path(control_path)
        settings = host.chat.settings
        self.control = QQMemoryControl(enabled=settings.qq_memory_enabled,
                                       recall_enabled=settings.qq_memory_recall_enabled)
        if self.control_path.exists():
            self.control = QQMemoryControl.model_validate_json(self.control_path.read_text(encoding="utf-8"))
        self.proposer = proposer
        self.task = None
        self.stopping = asyncio.Event()
        self.last_error = None
        self.last_completed_at = None
        self.last_duration_ms = None
        self.last_recall_ms = 0.0
        self.processing = False
        self.queue = None
        self._queue_owner = None
        self._queue_scope = None
        self._recall_pending = {}
        self._queue_counts = {}

    def configure(self, payload):
        control = QQMemoryControl.model_validate({**self.control.model_dump(), **payload})
        atomic_write_json(self.control_path, control.model_dump())
        self.control = control
        return self.status()

    def status(self, *, details=False):
        state = memory_state(getattr(self.host.chat.memory, "qq_memory_state", {}))
        settings = getattr(self.host.chat, "settings", None)
        episodes = [x for x in self.host.chat.memory.stores.episodic.list_all()
                    if x.scope and x.scope.channel in {"qq_group", "qq_private"}]
        result = {**self.control.model_dump(), "processing": self.processing,
                  "input_budget": {
                      "estimated_tokens": getattr(settings, "qq_input_token_budget", DEFAULT_QQ_INPUT_TOKEN_BUDGET),
                      "characters": getattr(settings, "qq_input_token_budget", DEFAULT_QQ_INPUT_TOKEN_BUDGET) * 2,
                      "recall_characters": getattr(settings, "qq_memory_recall_max_characters", DEFAULT_QQ_MEMORY_RECALL_CHARACTERS),
                      "recall_items": getattr(settings, "qq_memory_recall_max_items", 8)},
                  "queue": dict(self._queue_counts), "tiers": dict(Counter(x.tier for x in episodes)),
                  "episodes": len(episodes), "impressions": len(state["impressions"]),
                  "impression_states": dict(Counter(x["status"] for x in state["impressions"].values())),
                  "stable_judgments": sum(x["status"] == "stable" for x in state["impressions"].values()),
                  "processed_observations": len({ref for receipt in state["receipts"].values()
                                                  for ref in receipt["source_ids"]}),
                  "committed_windows": len(state["receipts"]),
                  "last_error_type": self.last_error, "last_completed_at": self.last_completed_at,
                  "last_duration_ms": self.last_duration_ms, "last_recall_ms": self.last_recall_ms,
                  "channels": {channel: {
                      "episodes": sum(x.scope.channel == channel for x in episodes),
                      "impressions": sum(x.get("scope", {}).get("channel") == channel for x in state["impressions"].values()),
                      "tiers": dict(Counter(x.tier for x in episodes if x.scope.channel == channel))}
                      for channel in ("qq_group", "qq_private")}}
        if details:
            result["impression_records"] = deepcopy(list(state["impressions"].values()))
            by_id = {event.event_id: event for event in episodes}
            for impression in result["impression_records"]:
                for evidence in [*impression["evidence"], *impression.get("continuation_evidence", [])]:
                    event = by_id.get(evidence["episode_id"])
                    if event is not None:
                        evidence["source_refs"] = list(event.source_refs)
                        evidence["observed_text"] = event.objective_fact
                        evidence["summary"] = event.summary
            result["receipts"] = list(state["receipts"].values())
        return result

    def recall(self, *, group_id, member_id=None, query=None):
        if not self.control.recall_enabled or self.host.chat.active_preset_id != "misty":
            return {}
        qq = self.host.shared.qq_settings
        if not qq:
            return {}
        try:
            conversation = conversation_scope(group_id)
        except ValueError:
            return {}
        if conversation["kind"] == "private":
            if (not self.control.private_recall_enabled
                    or conversation["self_member_id"] != qq.self_member_id
                    or conversation["peer_id"] not in getattr(qq, "allowed_private_peer_ids", ())
                    or member_id not in (None, conversation["peer_id"], conversation["self_member_id"])):
                return {}
            member_id = member_id or conversation["peer_id"]
        elif group_id not in qq.allowed_group_ids:
            return {}
        start = time.perf_counter()
        try:
            full_owner = self._owner_private_access(group_id)
            authorized = self._authorized_conversations() if full_owner else ()
            return select_recall(self.host.chat.memory, group_id=group_id,
                                 member_id=None if full_owner else member_id,
                                 query=query, max_items=self.host.chat.settings.qq_memory_recall_max_items,
                                 max_characters=self.host.chat.settings.qq_memory_recall_max_characters,
                                 settings=self.host.chat.settings, allowed_conversation_ids=authorized,
                                 include_owner_local=full_owner)
        finally:
            self.last_recall_ms = round((time.perf_counter() - start) * 1000, 3)

    def _authorized_conversations(self):
        qq = self.host.shared.qq_settings
        if qq is None:
            return ()
        return (*qq.allowed_group_ids, *(private_scope_id(qq.self_member_id, peer)
                for peer in getattr(qq, "allowed_private_peer_ids", ())))

    def _conversation_authorized(self, conversation_id, *, for_recall=False):
        qq = self.host.shared.qq_settings
        if qq is None:
            return False
        try:
            scope = conversation_scope(conversation_id)
        except ValueError:
            return False
        if scope["kind"] == "group":
            return conversation_id in qq.allowed_group_ids
        enabled = self.control.private_recall_enabled if for_recall else self.control.private_enabled
        return (enabled and scope["self_member_id"] == qq.self_member_id
                and scope["peer_id"] in getattr(qq, "allowed_private_peer_ids", ()))

    def _owner_private_access(self, group_id):
        from harness.cognitive.shared_memory import OPERATOR_QQ_MEMBER_ID

        if not (self.control.private_recall_enabled and self.control.owner_private_full_recall_enabled):
            return False
        qq = self.host.shared.qq_settings
        try:
            scope = conversation_scope(group_id)
        except ValueError:
            return False
        return bool(qq and scope["kind"] == "private" and scope["self_member_id"] == qq.self_member_id
            and scope["peer_id"] == OPERATOR_QQ_MEMBER_ID
            and scope["peer_id"] in getattr(qq, "allowed_private_peer_ids", ()))

    def _adoption_allowed(self, event, conversation_id):
        if not self._conversation_authorized(conversation_id, for_recall=True):
            return False
        if _scope_matches(event.scope, conversation_id):
            return True
        if not self._owner_private_access(conversation_id):
            return False
        if event.scope is None:
            return not str(event.channel or "").startswith("qq_")
        if event.scope.channel in {"qq_group", "qq_private"}:
            return any(_scope_matches(event.scope, allowed) for allowed in self._authorized_conversations())
        return event.scope.owner_id == "misty" and "local:main" in event.scope.visibility

    def note_recall(self, decision):
        if (self.control.recall_enabled and decision.decision_source == "model"
                and decision.outcome in {"ready", "natural_wait"}
                and decision.completed_at is not None
                and getattr(decision, "recalled_memory_ids", None)):
            self._recall_pending[decision.decision_id] = {
                "ids": list(decision.recalled_memory_ids), "group_id": decision.group_id,
                "episode_ids": list(getattr(decision, "recalled_episode_ids", [])),
                "at": decision.completed_at.isoformat(), "preset_id": self.host.chat.active_preset_id}

    async def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="qq-memory-assimilation")

    async def close(self):
        self.stopping.set()
        if self.task is not None:
            await self.task
            self.task = None

    async def run(self):
        while not self.stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=self.host.chat.settings.qq_memory_poll_seconds)
            except TimeoutError:
                pass

    async def tick(self):
        return await self._tick()

    async def _recover_publication(self):
        from harness.memory.qq_assimilation import QQAssimilationQueue

        adapter = self.host.adapter
        if not getattr(adapter, "_qq_memory_publication_pending", False):
            return
        async with self.host._control_lock:
            async with adapter._lock:
                if not getattr(adapter, "_qq_memory_publication_pending", False):
                    return
                # Reuse the already installed revision and preset receipt. This
                # also covers an import whose queue has never been opened by
                # this worker, and runs while both feature switches are off.
                adapter._persist_state()
                qq = self.host.shared.qq_settings
                if qq is not None and self.host.chat.active_preset_id == "misty":
                    queue = QQAssimilationQueue(self.host.shared.store.path,
                        owner_id="misty", preset_id="misty", self_id=qq.self_member_id,
                        allowed_group_ids=qq.allowed_group_ids,
                        allowed_private_peer_ids=getattr(qq, "allowed_private_peer_ids", ()))
                    receipts = memory_state(self.host.chat.memory.qq_memory_state)["receipts"]
                    await asyncio.to_thread(queue.recover, set(receipts))
                    self._queue_counts = await asyncio.to_thread(queue.status)
                adapter._qq_memory_publication_pending = False
                self.last_error = None

    async def _tick(self):
        from harness.memory.qq_assimilation import QQAssimilationQueue

        if self.stopping.is_set():
            return
        await self._recover_publication()
        if self.host.adapter._maintenance_paused:
            return
        if foreground_pending(self.host):
            return
        if not (self.control.enabled or self.control.recall_enabled):
            return
        qq = self.host.shared.qq_settings
        if qq is None or self.host.chat.active_preset_id != "misty":
            return
        private_peers = tuple(sorted(getattr(qq, "allowed_private_peer_ids", ()))) if self.control.private_enabled else ()
        queue_scope = (self.host.chat.active_preset_id, str(qq.self_member_id),
                       tuple(sorted(qq.allowed_group_ids)), private_peers)
        if (self.queue is None or self._queue_owner is not self.host.chat.memory
                or self._queue_scope != queue_scope):
            self.queue = QQAssimilationQueue(self.host.shared.store.path, owner_id="misty",
                preset_id=self.host.chat.active_preset_id, self_id=qq.self_member_id,
                allowed_group_ids=qq.allowed_group_ids,
                allowed_private_peer_ids=queue_scope[3],
                idle_seconds=self.host.chat.settings.qq_memory_window_idle_seconds,
                max_seconds=self.host.chat.settings.qq_memory_window_max_seconds,
                max_messages=self.host.chat.settings.qq_memory_window_max_messages)
            state = memory_state(self.host.chat.memory.qq_memory_state)
            await asyncio.to_thread(self.queue.recover, set(state["receipts"]))
            self._queue_owner = self.host.chat.memory
            self._queue_scope = queue_scope
            self._recall_pending.clear()
        if self.control.enabled:
            await asyncio.to_thread(self.host.shared.sync_qq)
            await asyncio.to_thread(self.queue.enqueue, now=now_utc())
        self._queue_counts = await asyncio.to_thread(self.queue.status)
        # This is the live owner, not the offline historical replay. A busy or
        # failed source queue must not indefinitely postpone decay or durable
        # adoption of memories that have already been committed.
        await self.maintain()
        if foreground_pending(self.host) or self.stopping.is_set():
            return
        job = await asyncio.to_thread(self.queue.next_job) if self.control.enabled else None
        if job is None:
            return
        started = time.perf_counter()
        self.processing = True
        try:
            if not await wait_foreground_idle(self.host, self.stopping):
                raise ValueError("qq_memory_owner_changed")
            async with self.host.adapter._lock:
                memory_identity = self.host.chat.memory
                preset = snapshot_owner(self.host)
            state = memory_state(preset.qq_memory_state)
            if job.job_id in state["receipts"]:
                await asyncio.to_thread(self.queue.acknowledge, job.job_id, set(state["receipts"]))
                return
            old = proposal_context(state, job)
            proposal = job.proposal
            if proposal is not None and job.proposal_basis_digest != _hash(old):
                # A restored preset can have a different interpretation history.
                # Keep its frozen source window, discard only the uncommitted
                # proposal, and regenerate against that restored history.
                await asyncio.to_thread(self.queue.reset_proposal, job.job_id,
                                        expected_digest=job.proposal_basis_digest)
                return
            if proposal is None:
                if not await wait_foreground_idle(self.host, self.stopping):
                    raise ValueError("qq_memory_owner_changed")
                proposal = await asyncio.to_thread(self.make_proposal, job, old, preset.persona)
                await asyncio.to_thread(self.queue.store_proposal, job.job_id, proposal,
                                        old_impressions=old, basis_digest=_hash(old))
            # Validate every proposal again at commit, including recovered output.
            committed = build_commit(preset, job, proposal, self.host.chat.settings)
            new_event_ids = set(memory_state(committed.qq_memory_state)["receipts"][job.job_id]["episode_ids"])
            # Embedding work is optional for QQ's bounded lexical recall, and is
            # performed off the foreground path using the existing embedding client.
            if self.stopping.is_set() or self.host.chat.memory is not memory_identity:
                raise ValueError("qq_memory_owner_changed")
            new_events = [event for event in committed.episodic if event.event_id in new_event_ids]
            if not await wait_foreground_idle(self.host, self.stopping):
                raise ValueError("qq_memory_owner_changed")
            try:
                vectors = await asyncio.wait_for(asyncio.to_thread(memory_identity.embeddings.embed,
                    [event.summary + " " + event.objective_fact for event in new_events]), timeout=5)
                for event, vector in zip(new_events, vectors, strict=True):
                    event.embedding = vector
            except Exception:
                for event in new_events:
                    event.embedding = None
            if not await wait_foreground_idle(self.host, self.stopping):
                raise ValueError("qq_memory_owner_changed")
            async with self.host._control_lock:
                async with self.host.adapter._lock:
                    if (self.stopping.is_set() or not self.control.enabled
                            or self.host.adapter._maintenance_paused
                            or self.host.chat.memory is not memory_identity
                            or self.host.chat.active_preset_id != job.preset_id
                            or self.host.shared.qq_settings is None
                            or self.host.shared.qq_settings.self_member_id != job.self_id
                            or not self._conversation_authorized(job.group_id)):
                        raise ValueError("qq_memory_owner_changed")
                    current = snapshot_owner(self.host)
                    if memory_state(current.qq_memory_state)["revision"] != state["revision"]:
                        raise ValueError("qq_memory_basis_changed")
                    # Local turns may have committed while the model was running.
                    # Apply against the current snapshot so those turns survive.
                    embeddings = {e.event_id: e.embedding for e in committed.episodic if e.event_id in new_event_ids}
                    committed = build_commit(current, job, proposal, self.host.chat.settings,
                                             embeddings=embeddings)
                    await asyncio.to_thread(self.host.chat.library.save, committed)
                    install_committed(self.host, committed)
            await asyncio.to_thread(self.queue.acknowledge, job.job_id,
                                    set(memory_state(committed.qq_memory_state)["receipts"]))
            self.last_completed_at = now_utc().isoformat()
            self.last_error = None
        except Exception as exc:
            self.last_error = getattr(exc, "error_code", type(exc).__name__)
            await asyncio.to_thread(self.queue.fail, job.job_id, self.last_error)
        finally:
            self.processing = False
            self.last_duration_ms = round((time.perf_counter() - started) * 1000, 3)
            self._queue_counts = await asyncio.to_thread(self.queue.status)

    def make_proposal(self, job, old, persona):
        if self.proposer is not None:
            return self.proposer.propose(job, old_impressions=old, persona=persona)
        from harness.llm.client import build_llm_client, qq_memory_proposal_settings
        from harness.llm.deadline import model_deadline
        from harness.memory.qq_assimilation import QQMemoryProposer, qq_reasoning_options
        settings = qq_memory_proposal_settings(self.host.chat.settings)
        llm = build_llm_client(settings)
        try:
            with model_deadline(now_utc() + timedelta(seconds=settings.qq_memory_model_timeout_seconds)):
                return QQMemoryProposer(llm, timeout_seconds=settings.qq_memory_model_timeout_seconds,
                    reasoning_options=qq_reasoning_options(settings)).propose(
                    job, old_impressions=old, persona=persona)
        finally:
            llm.close()

    async def maintain(self, *, now=None):
        from harness.memory.impressions import ImpressionMemory
        from harness.memory.tier import (
            apply_local_event_hit, maintain_qq_events, reinforce_qq_event, qq_event_strength,
        )

        if foreground_pending(self.host):
            return
        moment = now or now_utc()
        async with self.host._control_lock:
            async with self.host.adapter._lock:
                if foreground_pending(self.host):
                    return
                current = snapshot_owner(self.host)
                state = memory_state(current.qq_memory_state)
                # Recover adoption events from Cognitive's durable decision ledger.
                pending = dict(self._recall_pending)
                for value in self.host.adapter._ledger.values():
                    result = value.result if isinstance(value.result, dict) else {}
                    decision = result.get("decision", {})
                    if (decision.get("schema_version") == "qq-reply-decision-v1"
                            and decision.get("decision_source") == "model"
                            and decision.get("outcome") in {"natural_wait", "ready"}
                            and decision.get("recalled_memory_ids")):
                        pending.setdefault(decision["decision_id"], {
                            "ids": decision["recalled_memory_ids"], "group_id": decision["group_id"],
                            "episode_ids": decision.get("recalled_episode_ids", []),
                            "at": decision["completed_at"], "preset_id": "misty"})
                last_review = state.get("last_review_at")
                if (not pending and not state["impressions"]
                        and not any(e.scope and e.scope.channel in {"qq_group", "qq_private"} for e in current.episodic)):
                    return
                if (not any(ref not in state["recalls"] for ref in pending)
                        and last_review is not None
                        and moment - datetime.fromisoformat(last_review) < timedelta(
                            hours=self.host.chat.settings.qq_memory_review_hours)
                        and not any(e.scope and e.scope.channel in {"qq_group", "qq_private"} and e.tier == "short"
                                    and e.expire_at and e.expire_at <= moment for e in current.episodic)):
                    return
                events = {entry.event_id: entry for entry in current.episodic}
                for decision_id, adoption in pending.items():
                    if decision_id in state["recalls"] or adoption["preset_id"] != current.preset_id:
                        continue
                    at = datetime.fromisoformat(adoption["at"])
                    episode_ids = set()
                    # Freeze the evidence actually shown to this decision.
                    # Expanding today's impression could reinforce unseen or
                    # subsequently added history and create an immortal loop.
                    for ref in set(adoption.get("episode_ids", [])) | set(adoption["ids"]):
                        if ref in events and self._adoption_allowed(events[ref], adoption["group_id"]):
                            episode_ids.add(ref)
                    for ref in episode_ids:
                        if ref in events:
                            event = events[ref]
                            anchor = max(event.strength_updated_at or event.time, event.last_hit_at or event.time)
                            if _owner_local_recall_scope(event.scope) and not event.channel.startswith("qq_"):
                                events[ref] = apply_local_event_hit(event, self.host.chat.settings, now=max(at, anchor))
                            else:
                                events[ref] = reinforce_qq_event(event, now=max(at, anchor),
                                                               settings=self.host.chat.settings)
                    state["recalls"][decision_id] = sorted(episode_ids)
                maintained = maintain_qq_events(list(events.values()), self.host.chat.settings, now=moment)
                state["last_review_at"] = moment.isoformat()
                events = {entry.event_id: entry for entry in maintained}
                for key, raw in list(state["impressions"].items()):
                    item = ImpressionMemory.model_validate(raw)
                    related = [events[e.episode_id] for e in item.evidence if e.episode_id in events]
                    active = [event for event in related if event.tier != "forgotten"]
                    strength = max((qq_event_strength(e, self.host.chat.settings, now=moment)
                                    for e in active), default=0.0)
                    updates = {"strength": strength, "strength_updated_at": moment}
                    if not active:
                        updates["status"] = "dormant"
                    state["impressions"][key] = item.model_copy(update=updates).model_dump(mode="json")
                retained_facts = [fact for fact in current.semantic
                    if not fact.fact_id.startswith("qqsemantic_") or state["impressions"].get(
                        "impression_" + fact.fact_id.removeprefix("qqsemantic_"), {}).get("status") == "stable"]
                candidates = [candidate.model_copy(update={"status": "dropped", "consolidated_fact_id": None})
                    if candidate.candidate_id.startswith("qqcandidate_") and state["impressions"].get(
                        "impression_" + candidate.candidate_id.removeprefix("qqcandidate_"), {}).get("status") == "dormant"
                    else candidate for candidate in current.candidates]
                if state == current.qq_memory_state and maintained == current.episodic:
                    return
                committed = current.model_copy(update={"episodic": maintained, "semantic": retained_facts,
                                                       "candidates": candidates, "qq_memory_state": state})
                await asyncio.to_thread(self.host.chat.library.save, committed)
                install_committed(self.host, committed)
                self._recall_pending.clear()
