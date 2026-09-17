"""Exact used-fact guards; no recall, ranking, model call, or global revision."""
from __future__ import annotations

from datetime import datetime, timezone

from harness.core.models import MemoryScope
from harness.memory.shared import SharedReadScope
from harness.qq.conversation import conversation_audience, conversation_scope
from harness.qq.turn_checkpoints import QQTurnStateError, digest


_BOOKKEEPING = {"embedding", "hit_count", "last_hit_at", "strength", "strength_updated_at",
                "last_decay_review_at", "tier_updated_at", "tier_reasons"}
_DAILY_COLLECTIONS = {"episodic": "episodic", "semantic": "semantic", "candidate": "candidates",
                      "relationship": "relationships", "impression": "impressions"}


def _reject():
    raise QQTurnStateError("qq_turn_used_source_changed")


def _raw(memory, collection, record_id):
    if collection == "impressions":
        value = getattr(memory, "qq_memory_state", {}).get("impressions", {}).get(record_id)
    else:
        name = "relationship" if collection == "relationships" else collection
        value = getattr(memory.stores, name).get(record_id)
    if value is None:
        _reject()
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _content_hash(raw):
    return digest({key: value for key, value in raw.items() if key not in _BOOKKEEPING})


def _active(raw):
    if (raw.get("tier") == "forgotten" or raw.get("status") in {"dormant", "forgotten", "dropped", "consolidated"}
            or raw.get("revision_metadata", {}).get("status") in {"superseded", "retracted", "dropped", "invalidated"}):
        return False
    expiry = raw.get("expire_at")
    return not (raw.get("tier") == "short" and expiry
                and datetime.fromisoformat(expiry) <= datetime.now(timezone.utc))


def _visible(host, group_id, raw, *, daily):
    from harness.cognitive.qq_memory import _scope_matches, _owner_local_recall_scope

    scope = MemoryScope.model_validate(raw["scope"]) if raw.get("scope") else None
    if daily:
        audience = conversation_audience(group_id)
        return bool(scope and scope.owner_id == host.shared.store.owner_id
                    and scope.conversation_id == audience and audience in scope.visibility)
    coordinator = host.qq_memory
    if not coordinator.control.recall_enabled or host.chat.active_preset_id != "misty":
        return False
    if not coordinator._conversation_authorized(group_id, for_recall=True):
        return False
    if _scope_matches(scope, group_id):
        return True
    if not coordinator._owner_private_access(group_id):
        return False
    if any(_scope_matches(scope, allowed) for allowed in coordinator._authorized_conversations()):
        return True
    return (_owner_local_recall_scope(scope) and not str(raw.get("channel", "")).startswith("qq_")
            and not str(raw.get("session_id", "")).startswith(("qq:", "private:")))


def _shared_scope(host, group_id, *, operator):
    if not operator:
        return SharedReadScope(owner_id=host.shared.store.owner_id, audience=conversation_audience(group_id),
                               channels=("local_chat",))
    from harness.cognitive.shared_memory import OPERATOR_QQ_MEMBER_ID

    scope = conversation_scope(group_id)
    qq = host.shared.qq_settings
    if not (qq and scope["kind"] == "private" and scope["self_member_id"] == qq.self_member_id
            and scope["peer_id"] == OPERATOR_QQ_MEMBER_ID and scope["peer_id"] in qq.allowed_private_peer_ids):
        _reject()
    return SharedReadScope(owner_id=host.shared.store.owner_id, audience="local:main", channels=("local_chat",),
        session_ids=("operator-confirmed-facts",), principals=("local:main",), kinds=("local_incoming",))


def _daily_dependencies(memory, collection, record_id, entry, *, scope, owner_id):
    state = getattr(memory, "daily_revision_state", {})
    key = collection + "/" + record_id
    runs = sorted({item["run_id"] for item in state.get("entries", []) if item.get("key") == key
                   and state.get("receipts", {}).get(item.get("run_id"), {}).get("status") == "applied"})
    if not runs:
        _reject()
    sources = {}
    for source_id in entry.get("source_refs", []):
        source = state.get("sources", {}).get(source_id)
        if source is None or source.get("scope") != scope or source.get("owner_id") != owner_id:
            _reject()
        sources[source_id] = digest(source)
    if not sources:
        _reject()
    return {"runs": {run_id: digest(state["receipts"][run_id]) for run_id in runs}, "sources": sources}


def capture_used_sources(host, selected_payload) -> dict:
    group_id = selected_payload["group_context"]["group_id"]
    shared = selected_payload.get("shared_memory") or {}
    manifest = {"version": "qq-used-sources-v1", "group_id": group_id,
                "owner_id": host.shared.store.owner_id, "shared": [], "derived": []}
    for operator, entries in ((False, shared.get("entries", [])),
                              (True, shared.get("operator_confirmed_facts", {}).get("entries", []))):
        scope = _shared_scope(host, group_id, operator=operator) if entries else None
        records = host.shared.store.read_exact(scope, [item["record_id"] for item in entries]) if entries else ()
        if len(records) != len(entries):
            _reject()
        for item in records:
            manifest["shared"].append({"record_id": item.record_id, "operator": operator,
                                       "hash": digest(item.model_dump(mode="json"))})
    memory = host.chat.memory
    for entry in (selected_payload.get("derived_memory") or {}).get("entries", []):
        daily = entry.get("authority") == "daily_evidence_bound_interpretation"
        collection = (_DAILY_COLLECTIONS.get(entry["kind"]) if daily else
                      "episodic" if entry["kind"] == "episode" else
                      "semantic" if entry["kind"] == "semantic" else "impressions")
        if collection is None:
            _reject()
        record_id = entry["record_id"]
        raw = _raw(memory, collection, record_id)
        if not _active(raw) or not _visible(host, group_id, raw, daily=daily):
            _reject()
        value = {"record_id": record_id, "collection": collection, "daily": daily,
                 "hash": _content_hash(raw), "dependencies": {}}
        if daily:
            value["daily_dependencies"] = _daily_dependencies(memory, collection, record_id, entry,
                scope=raw.get("scope"), owner_id=host.shared.store.owner_id)
        else:
            dependencies = set(entry.get("supporting_episode_ids", []))
            dependencies.update(item["episode_id"] for item in entry.get("counterevidence", []))
            if entry.get("first_episode_id"):
                dependencies.add(entry["first_episode_id"])
            if collection == "impressions":
                dependencies.update(item["episode_id"] for item in raw.get("evidence", []))
            for episode_id in sorted(dependencies):
                evidence = _raw(memory, "episodic", episode_id)
                if (not _visible(host, group_id, evidence, daily=False)
                        or collection == "impressions" and evidence.get("scope") != raw.get("scope")):
                    _reject()
                value["dependencies"][episode_id] = _content_hash(evidence)
            if collection == "impressions" and not any(
                _active(_raw(memory, "episodic", item["episode_id"])) for item in raw.get("evidence", [])
            ):
                _reject()
        manifest["derived"].append(value)
    return manifest


def validate_used_sources(host, manifest) -> None:
    if manifest.get("version") != "qq-used-sources-v1" or manifest["owner_id"] != host.shared.store.owner_id:
        _reject()
    group_id = manifest["group_id"]
    for entry in manifest["shared"]:
        scope = _shared_scope(host, group_id, operator=entry["operator"])
        records = host.shared.store.read_exact(scope, [entry["record_id"]])
        if len(records) != 1 or digest(records[0].model_dump(mode="json")) != entry["hash"]:
            _reject()
    memory = host.chat.memory
    for entry in manifest["derived"]:
        raw = _raw(memory, entry["collection"], entry["record_id"])
        if (_content_hash(raw) != entry["hash"] or not _active(raw)
                or not _visible(host, group_id, raw, daily=entry["daily"])):
            _reject()
        for episode_id, expected in entry["dependencies"].items():
            evidence = _raw(memory, "episodic", episode_id)
            if _content_hash(evidence) != expected or not _visible(host, group_id, evidence, daily=False):
                _reject()
        if entry["collection"] == "impressions" and not entry["daily"] and not any(
            _active(_raw(memory, "episodic", item["episode_id"])) for item in raw.get("evidence", [])
        ):
            _reject()
        if entry["daily"]:
            state = getattr(memory, "daily_revision_state", {})
            dependencies = entry["daily_dependencies"]
            key = entry["collection"] + "/" + entry["record_id"]
            for run_id, expected in dependencies["runs"].items():
                receipt = state.get("receipts", {}).get(run_id)
                if (not receipt or receipt.get("status") != "applied" or digest(receipt) != expected
                        or not any(item.get("key") == key and item.get("run_id") == run_id for item in state.get("entries", []))):
                    _reject()
            for source_id, expected in dependencies["sources"].items():
                source = state.get("sources", {}).get(source_id)
                if source is None or digest(source) != expected:
                    _reject()
