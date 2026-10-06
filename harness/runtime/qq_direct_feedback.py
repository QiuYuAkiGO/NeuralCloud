"""Read-only OneBot observation collector; feedback never creates send rights."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3

from harness.homeostasis.models import QQHomeostaticFact
from harness.qq.feedback import _observation, _readonly
from harness.runtime.models import RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle


class QQDirectFeedback:
    def __init__(self, supervisor, bridge):
        self.supervisor, self.bridge = supervisor, bridge
        self.last_error = None
        self.last_scan_at = None
        self.last_accepted_at = None
        self.accepted = 0
        self._seen = {}

    def collect(self):
        settings, status = self.bridge._active()
        now = self.bridge.clock()
        cutoff = now - timedelta(seconds=300)
        facts = []
        with closing(_readonly(settings.state_db)) as direct, closing(_readonly(self.bridge.config.memory_db)) as memory:
            for group in settings.group_roster:
                if not group.observe:
                    continue
                if self.bridge.attention_store().check(group.group_id):
                    continue
                rows = direct.execute("SELECT record_json,source_record_id FROM qq_direct_events WHERE group_id=? AND self_id=? AND received_at>=? ORDER BY received_at DESC LIMIT 64",
                    (group.group_id, settings.self_member_id, cutoff.isoformat())).fetchall()
                for row in rows:
                    event = json.loads(row["record_json"])
                    if (event.get("historical") or event.get("own") or event.get("_production_contact")
                            or event.get("attention_history_only")
                            or event.get("run_id") != status["run_id"] or not row["source_record_id"]):
                        continue
                    if not cutoff <= datetime.fromisoformat(event["message_time"]) <= now + timedelta(seconds=5):
                        continue
                    observation = _observation(memory, group.group_id, row["source_record_id"])
                    if observation is None or observation.member_id == settings.self_member_id:
                        continue
                    # Both frozen SQLite provenance and the content-addressed raw
                    # protocol event must agree. Model interpretations confer no credit.
                    self.bridge._verify(settings, observation)
                    fact = QQHomeostaticFact(fact_id=observation.record_id, group_id=group.group_id,
                        kind="verified_message", verification="qq_protocol_verified",
                        occurred_at=datetime.fromisoformat(event["received_at"]),
                        evidence_refs=observation.attribution_evidence)
                    if fact.occurred_at <= now + timedelta(seconds=5):
                        facts.append(fact)
        return facts

    async def heartbeat(self):
        s = self.supervisor
        if (s.lifecycle != RuntimeLifecycle.RUNNING or not s.goals.enabled
                or getattr(s, "production_runtime", None) is not None or not self.bridge.enabled()):
            return
        now = self.bridge.clock()
        if self.last_scan_at and (now - self.last_scan_at).total_seconds() < 5:
            return
        self.last_scan_at = now
        try:
            facts = await asyncio.to_thread(self.collect)
            indexed = s.recovery.mapping("qq_facts")
            self._seen = {k: v for k, v in self._seen.items() if (now - v).total_seconds() < 300}
            for fact in facts:
                if fact.fact_key in self._seen or indexed.get(fact.fact_key):
                    continue
                event_id = "qq-fact:" + hashlib.sha256(fact.fact_key.encode()).hexdigest()
                event = await s.submit_event(event_type=RuntimeEventType.QQ_FACT,
                    priority=RuntimeEventPriority.IMPORTANT_ENVIRONMENT, source="qq_protocol_collector",
                    source_time=fact.occurred_at, event_id=event_id, correlation_id=event_id,
                    idempotency_key=event_id, payload={"qq_fact": fact.model_dump(mode="json")})
                if event is not None:
                    self._seen[fact.fact_key] = fact.occurred_at
                    self.last_accepted_at = now.isoformat()
                    self.accepted += 1
            self.last_error = None
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
            self.last_error = "qq_protocol_feedback_unavailable"

    def status(self):
        return {"last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
                "last_accepted_at": self.last_accepted_at, "accepted_this_run": self.accepted,
                "error": self.last_error}
