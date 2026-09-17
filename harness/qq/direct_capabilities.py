"""Read-only protocol memory offers for the existing Runtime capability owner.

The actor continuously retains roster messages. These offers inspect that
retained evidence; they never pretend to perform a new QQ API or GUI capture.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from harness.qq.conversation import QQConversationID, conversation_scope
from harness.qq.direct_config import DirectQQSettings
from harness.qq.direct_store import canonical, normalize_evidence
from harness.qq.feedback import _readonly
from harness.qq.memory import QQID, QQVerifiedProfile, QQVisualObservation, _history_match, _normalized_text, _query_terms


READ = "communication.qq.read.v1"


class QQDirectReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    query: Annotated[str, StringConstraints(min_length=1, max_length=512, pattern=r"\S")] | None = None
    limit: int = Field(default=20, ge=1, le=50)


class QQDirectReadScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    transport: Literal["onebot"] = "onebot"
    group_id: QQConversationID
    self_member_id: QQID

    @model_validator(mode="after")
    def account_binding(self):
        scope = conversation_scope(self.group_id)
        if scope["kind"] == "private" and scope["self_member_id"] != self.self_member_id:
            raise ValueError("direct_read_private_account_invalid")
        return self


def _time(value):
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("direct_read_aware_time_required")
    return moment


class QQDirectCapabilityBridge:
    def __init__(self, supervisor, config, config_path, state_path, *, clock=None):
        self.supervisor, self.config = supervisor, config
        self.config_path, self.path = Path(config_path), Path(state_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS qq_direct_capability_results (opportunity_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, result_json TEXT NOT NULL)")

    def _db(self):
        return sqlite3.connect(self.path, timeout=3)

    def enabled(self):
        # A malformed configured bridge must not fall through to an old GUI
        # sender. Only an explicit disabled setting permits the legacy owner.
        try:
            return DirectQQSettings.read(self.config_path).enabled
        except (ValueError, OSError):
            return True

    def _ownership(self):
        owner = getattr(self.supervisor, "capabilities", None)
        return (owner.owner("qq") if owner is not None else "legacy", getattr(owner, "generation", 0))

    def _active(self):
        settings = DirectQQSettings.read(self.config_path)
        if (not settings.enabled or settings.self_member_id != self.config.self_member_id
                or any(group.group_id not in self.config.allowed_group_ids for group in settings.group_roster)
                or any(contact.peer_id not in getattr(self.config, "allowed_private_peer_ids", [])
                       for contact in settings.private_contacts)):
            raise ValueError("direct_read_scope_invalid")
        path = settings.status_file
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError("direct_read_status_invalid")
        status = json.loads(path.read_text(encoding="utf-8"))
        age = (self.clock() - _time(status["heartbeat_at"])).total_seconds()
        if (status.get("service") != "qq-direct-onebot-v1" or not -2 <= age <= 15
                or status.get("running") is not True or status.get("connected") is not True
                or status.get("paused") is not False or status.get("desktop_owner_active") is not False
                or status.get("observation_gap") is True or status.get("self_member_id") != settings.self_member_id
                or status.get("group_roster") != [group.model_dump(mode="json") for group in settings.group_roster]
                or status.get("private_contacts", []) != [contact.model_dump(mode="json") for contact in settings.private_contacts]):
            raise ValueError("direct_read_not_online")
        with closing(_readonly(settings.state_db)) as db:
            runs = db.execute("SELECT run_id,self_id FROM qq_direct_runs WHERE active=1").fetchall()
        if len(runs) != 1 or runs[0]["run_id"] != status.get("run_id") or runs[0]["self_id"] != settings.self_member_id:
            raise ValueError("direct_read_run_invalid")
        return settings, status

    def _scope_authorized(self, settings, scope):
        conversation = conversation_scope(scope.group_id)
        if scope.self_member_id != settings.self_member_id:
            raise ValueError("direct_read_scope_invalid")
        if conversation["kind"] == "private":
            if (conversation["self_member_id"] != settings.self_member_id
                    or conversation["peer_id"] not in getattr(self.config, "allowed_private_peer_ids", [])
                    or not any(contact.observe and contact.peer_id == conversation["peer_id"]
                               for contact in settings.private_contacts)):
                raise ValueError("direct_read_scope_invalid")
        elif (scope.group_id not in self.config.allowed_group_ids
                or not any(group.observe and group.group_id == scope.group_id for group in settings.group_roster)):
            raise ValueError("direct_read_scope_invalid")
        return conversation

    def _metadata(self, settings, group_id):
        with closing(_readonly(settings.state_db)) as db:
            row = db.execute("SELECT MAX(received_at) received_at,MAX(json_extract(record_json,'$.message_time')) message_time,COUNT(*) count FROM qq_direct_events WHERE group_id=? AND self_id=?",
                             (group_id, settings.self_member_id)).fetchone()
        return {"last_received_at": row["received_at"], "last_message_at": row["message_time"], "retained_event_count": row["count"]}

    def catalog(self):
        owner, generation = self._ownership()
        if owner != "shared":
            return []
        try:
            settings, status = self._active()
            offers = []
            for group in settings.group_roster:
                if not group.observe:
                    continue
                scope = QQDirectReadScope(group_id=group.group_id, self_member_id=settings.self_member_id)
                key = hashlib.sha256(canonical(scope.model_dump(mode="json")).encode()).hexdigest()
                offers.append({"offer_id": "qq-onebot:" + key + ":read", "channel": "qq", "capability_id": READ,
                    "provider_id": "qq-onebot", "body_instance_id": "qq-onebot:" + settings.self_member_id,
                    "scope": scope.model_dump(mode="json"), "activation_revision": f"{status['run_id']}:{generation}",
                    "input_schema": QQDirectReadArguments.model_json_schema(), "observation_refs": [],
                    "supported": True, "available": True, "ready": True,
                    "valid_until": (self.clock() + timedelta(seconds=10)).isoformat(),
                    "observation": {"group_id": group.group_id, "group_label": group.label, "read_only": True,
                        "transport": "onebot", "current_live_observation": False,
                        "effect": "read_or_search_retained_protocol_messages_and_image_analysis",
                        **self._metadata(settings, group.group_id)}})
            # Runtime capability results feed generic goal review. Until that
            # path carries an enforced audience, expose only group offers.
            # Private history belongs to its scoped QQ reply preparation.
            return offers
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
            return []

    def _verify(self, settings, observation):
        scope = QQDirectReadScope(group_id=observation.group_id, self_member_id=settings.self_member_id)
        conversation = self._scope_authorized(settings, scope)
        with closing(_readonly(settings.state_db)) as db:
            row = db.execute("SELECT * FROM qq_direct_events WHERE source_record_id=? AND group_id=? AND self_id=?",
                             (observation.record_id, observation.group_id, settings.self_member_id)).fetchone()
        if row is None:
            raise ValueError("direct_read_source_missing")
        event = json.loads(row["record_json"])
        ref = event["event_evidence_ref"]
        if not isinstance(ref, str) or re.fullmatch(r"qq-protocol://sha256/[0-9a-f]{64}", ref) is None:
            raise ValueError("direct_read_evidence_invalid")
        path = settings.evidence_dir / (ref.rsplit("/", 1)[1] + ".json")
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("direct_read_evidence_invalid")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref.rsplit("/", 1)[1]:
            raise ValueError("direct_read_evidence_changed")
        original = normalize_evidence(json.loads(raw), self_id=settings.self_member_id,
            groups={observation.group_id} if conversation["kind"] == "group" else set(),
            private_peers={conversation["peer_id"]} if conversation["kind"] == "private" else set(),
            received_at=_time(row["received_at"]))
        try:
            frozen_observation = type(observation).model_validate(event.get("bound_observation"))
        except ValueError:
            raise ValueError("direct_read_source_invalid") from None
        if (original is None or observation != frozen_observation
                or original["group_id"] != observation.group_id or original["self_member_id"] != row["self_id"]
                or observation.member_id != row["member_id"] or observation.message_id != row["message_id"]
                or observation.capture_item_id != row["event_id"] or observation.capture_id != row["event_id"]
                or ref not in observation.attribution_evidence or observation.text != original["text"]
                or observation.observed_at != _time(original["message_time"])
                or any(original[key] != event[key] for key in ("event_id", "group_id", "self_member_id", "member_id", "message_id", "text", "message_time", "images", "own"))
                or [item.image_index for item in observation.image_analysis] != list(range(1, len(original["images"]) + 1))):
            raise ValueError("direct_read_source_invalid")

    def _read(self, settings, scope, arguments, deadline):
        conversation = self._scope_authorized(settings, scope)
        terms, _ = _query_terms(arguments.query)

        def score(observation):
            original = _history_match(observation, terms)
            images = [_normalized_text(value) for item in observation.image_analysis
                      for value in (item.description, item.ocr_text) if value]
            return original.score + sum(min(len(term), 8) for term in terms if term not in original.matched_terms
                                        and any(term in value for value in images))

        # Native image search is bounded and channel-specific. The original
        # shared QQ text/history retrieval semantics remain unchanged.
        candidates = []
        with closing(_readonly(self.config.memory_db)) as db:
            rows = db.execute("SELECT record_json FROM qq_visual_observations WHERE group_id=? AND json_extract(record_json,'$.author_attribution')='qq_protocol_verified' ORDER BY observed_at DESC,record_id DESC LIMIT 501",
                              (scope.group_id,)).fetchall()
            for row in rows[:500]:
                observation = QQVisualObservation.model_validate_json(row["record_json"])
                if observation.group_id != scope.group_id:
                    raise ValueError("direct_read_scope_invalid")
                profile_row = db.execute("SELECT record_json FROM qq_visual_profiles WHERE record_id=? AND group_id=? AND member_id=?",
                    (observation.profile_record_id, scope.group_id, observation.member_id)).fetchone()
                if profile_row is None:
                    raise ValueError("direct_read_profile_invalid")
                profile = QQVerifiedProfile.model_validate_json(profile_row["record_json"])
                candidates.append((observation, profile))
        if terms:
            candidates = [(observation, profile) for observation, profile in candidates if score(observation) > 0]
        candidates.sort(key=lambda pair: (score(pair[0]) if terms else 0, pair[0].observed_at, pair[0].stored_at), reverse=True)
        messages, refs = [], []
        for observation, profile in candidates[:arguments.limit]:
            if self.clock() >= deadline:
                raise ValueError("direct_read_expired")
            self._verify(settings, observation)
            if (profile.verification != "qq_protocol_verified" or profile.member_id != observation.member_id
                    or profile.group_id != scope.group_id or profile.record_id != observation.profile_record_id):
                raise ValueError("direct_read_profile_invalid")
            item = {"observation": observation.model_dump(mode="json"), "profile": profile.model_dump(mode="json")}
            if len(canonical(messages + [item]).encode("utf-8")) > 64000:
                break
            messages.append(item)
            refs.extend(observation.evidence + observation.attribution_evidence)
        if self.clock() >= deadline:
            raise ValueError("direct_read_expired")
        private_metadata = {"conversation": conversation, "privacy": "private_conversation_only"} if conversation["kind"] == "private" else {}
        return {"status": "completed", "receipt": {"kind": "retained_protocol_memory_read", "scope": scope.model_dump(mode="json"),
            "read_at": self.clock().isoformat(), "current_live_observation": False, "coverage": "partial_retained_messages",
            "query": arguments.query, "messages": messages, "matched_candidates": len(candidates),
            "omitted_candidates": len(candidates) - len(messages), "searched_latest_records": min(len(rows), 500),
            "scan_limit": 500, "scan_truncated": len(rows) > 500, **self._metadata(settings, scope.group_id), **private_metadata},
            "evidence_refs": list(dict.fromkeys(refs))[:128]}

    def enqueue(self, row):
        owner, generation = self._ownership()
        scope = QQDirectReadScope.model_validate(row["scope"])
        if conversation_scope(scope.group_id)["kind"] == "private":
            raise ValueError("direct_read_private_audience_required")
        arguments = QQDirectReadArguments.model_validate(row.get("arguments", {}))
        deadline = _time(row["deadline_at"])
        if owner != "shared" or row["generation"] != generation or row["capability_id"] != READ:
            raise ValueError("direct_read_owner_or_capability_invalid")
        request = {key: row[key] for key in ("opportunity_id", "generation", "capability_id", "offer_id", "body_instance_id", "selection_ref")}
        request.update(scope=scope.model_dump(mode="json"), arguments=arguments.model_dump(mode="json"), deadline_at=deadline.isoformat())
        request["activation_revision"] = row.get("activation_revision") or row.get("offer", {}).get("activation_revision")
        with closing(self._db()) as db:
            existing = db.execute("SELECT request_json,result_json FROM qq_direct_capability_results WHERE opportunity_id=?", (row["opportunity_id"],)).fetchone()
        if existing:
            if existing[0] != canonical(request):
                raise ValueError("direct_read_request_conflict")
            return {"opportunity_id": row["opportunity_id"], **json.loads(existing[1])}
        if not 0 < (deadline - self.clock()).total_seconds() <= 60:
            raise ValueError("direct_read_expired")
        offers = [offer for offer in self.catalog() if offer["offer_id"] == row["offer_id"]]
        if len(offers) != 1 or any(request[key] != offers[0][key] for key in ("scope", "capability_id", "body_instance_id", "activation_revision")):
            raise ValueError("direct_read_offer_changed")
        settings, _ = self._active()
        result = self._read(settings, scope, arguments, deadline)
        with closing(self._db()) as db, db:
            db.execute("INSERT INTO qq_direct_capability_results VALUES(?,?,?)", (row["opportunity_id"], canonical(request), canonical(result)))
        return {"opportunity_id": row["opportunity_id"], **result}

    def result(self, opportunity_id):
        with closing(self._db()) as db:
            row = db.execute("SELECT request_json,result_json FROM qq_direct_capability_results WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        if row is None:
            return None
        request = json.loads(row[0])
        scope = QQDirectReadScope.model_validate(request["scope"])
        if conversation_scope(scope.group_id)["kind"] == "private":
            return None
        return {"opportunity_id": opportunity_id, **json.loads(row[1])}

    get = result


class QQCapabilityTransports:
    """Select one configured read owner; native mode exposes no GUI send offer."""

    def __init__(self, desktop, direct):
        self.desktop, self.direct = desktop, direct

    def catalog(self):
        return self.direct.catalog() if self.direct.enabled() else self.desktop.catalog() if self.desktop else []

    def enqueue(self, row):
        if self.direct.enabled():
            return self.direct.enqueue(row)
        if self.desktop is None:
            raise ValueError("qq_capability_not_configured")
        return self.desktop.enqueue(row)

    def result(self, opportunity_id):
        return self.direct.result(opportunity_id) or (self.desktop.result(opportunity_id) if self.desktop else None)

    get = result

    def __getattr__(self, name):
        if name not in {"report", "claim", "acknowledge"} or self.desktop is None or self.direct.enabled():
            raise ValueError("qq_desktop_capability_inactive")
        return getattr(self.desktop, name)
