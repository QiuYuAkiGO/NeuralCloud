"""Semantic freshness for admitted Direct messages; protocol authority stays in Direct."""
from __future__ import annotations

import asyncio
from datetime import datetime
import os
from pathlib import Path
import time

from harness.interruption import InterruptedReply, TextInterruption
from harness.qq.conversation import conversation_scope
from harness.qq.telemetry import QQDeadline
from harness.qq.onebot import OneBotError


class QQTextTurns:
    def __init__(self, actor):
        self.actor = actor
        settings = actor.settings
        self.engines = {
            "group": TextInterruption(settings.state_db.with_suffix(".interruption.sqlite3"),
                settings.text_interruption, self.assess),
            "private": TextInterruption(settings.state_db.with_suffix(".private-interruption.sqlite3"),
                settings.text_interruption_private, self.assess),
        }

    def engine(self, event):
        return self.engines[conversation_scope(event["group_id"])["kind"]]

    async def assess(self, request):
        if not self.actor.active() or self.actor.stop.is_set():
            raise ValueError("interruption_owner_paused")
        return await asyncio.to_thread(self.actor.cognition.assess_text_interruption, request)

    def eligible(self, event):
        return not event["historical"] and not event["own"] and self.actor.conversations[event["group_id"]].send

    def receive(self, event):
        if self.eligible(event):
            self.engine(event).receive("qq:" + event["group_id"], event["event_id"], event["member_id"], event["text"])

    def begin(self, event, deadline):
        if not self.eligible(event):
            return deadline
        engine = self.engine(event)
        engine.begin("qq:" + event["group_id"], event["event_id"], event["text"],
                     datetime.fromisoformat(event["received_at"]).timestamp() + 60, author=event["member_id"])
        if not engine.active:
            return deadline
        turn = engine.store.get(event["event_id"])
        if "control_fence" not in turn:
            turn["control_fence"] = self.control_fence(event)
            engine.store.save(turn)
        self.restore_prefix(event)
        remaining = max(0, engine.deadline(event["event_id"], time.time() + deadline.remaining()) - time.time())
        return QQDeadline(min(deadline.deadline_mono, deadline.clock() + remaining), deadline.clock)

    async def gate(self, event, candidate):
        action = await self.engine(event).before_send(event["event_id"], candidate)
        if action != "continue":
            raise InterruptedReply(action)

    def context(self, event):
        return self.engine(event).continuation(event["event_id"])

    def control_fence(self, event):
        files = [self.actor.settings.control_file, self.actor.settings.session_config_path]
        if os.environ.get("NC_QQ_DIRECT_CONFIG"):
            files.append(Path(os.environ["NC_QQ_DIRECT_CONFIG"]))
        # File versions also detect pause then resume between polling ticks.
        versions = [[str(path), path.stat().st_mtime_ns, path.stat().st_size] for path in files]
        return {"files": versions, "attention": self.actor.attention.get(event["group_id"])["revision"],
                "run": self.actor.run_id, "connection_epoch": self.actor.epoch}

    def check_control(self, event):
        engine = self.engine(event)
        row = engine.store.get(event["event_id"]) if engine.active else None
        if row and row.get("control_fence") != self.control_fence(event):
            raise OneBotError("direct_owner_unavailable")

    def restore_prefix(self, event):
        # The protocol receipt may have committed just before an owner crash.
        # Recover from verified receipts, never from a planned/attempted send.
        from harness.qq.decision import get_qq_segment_deliveries
        engine = self.engine(event)
        turn = engine.store.get(event["event_id"])
        if not turn:
            return
        for ident in dict.fromkeys((turn["root_id"], turn["current_event"])):
            source = self.actor.store.get(ident)
            if not source or not source.get("decision_id"):
                continue
            for receipt in get_qq_segment_deliveries(memory_db=self.actor.session.memory_db,
                    group_id=event["group_id"], decision_id=source["decision_id"]):
                if receipt.verification == "qq_protocol_verified":
                    engine.delivered(event["event_id"], [receipt.delivered_text], receipt_id=receipt.receipt_id)

    def cooldown_exclusions(self, event):
        engine = self.engine(event)
        row = engine.store.get(event["event_id"]) if engine.active else None
        # Verified prefix belongs to the same reply, not a second speaking turn.
        if row and row["current_event"] == event["event_id"] and row["regenerations"] and row["delivered"]:
            return (row["root_id"],)
        return ()

    async def close(self):
        await asyncio.gather(*(engine.close() for engine in self.engines.values()))
