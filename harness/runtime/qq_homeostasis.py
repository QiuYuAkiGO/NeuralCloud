"""Authenticated factual QQ ingress to the Runtime's single domain queue.

The request carries references, never authoritative appraisals or dialogue.
All evidence is checked against the configured private QQ memory database.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

from harness.homeostasis.models import QQFactLookup, QQHomeostaticFact
from harness.qq.decision import QQReplyDecision
from harness.qq.feedback import _observation, _readonly, _receipts, feedback_for_direct_response
from harness.qq.session import QQSessionSettings
from harness.runtime.models import RuntimeEvent, RuntimeEventPriority, RuntimeEventType


class _FactVerifier:
    def __init__(self, config: QQSessionSettings, requested: QQHomeostaticFact):
        self.group_id, self.self_member_id = requested.group_id, config.self_member_id
        self.armed_at = requested.occurred_at - timedelta(seconds=300)
        self.requested = requested
        self.matched = False

    def enqueue_fact(self, **fields):
        candidate = QQHomeostaticFact(group_id=self.group_id, verification="qq_ui_verified", **fields)
        self.matched |= candidate.fact_hash == self.requested.fact_hash
        return False


def _validate_evidence(config: QQSessionSettings, fact: QQHomeostaticFact, now: datetime) -> None:
    if not -5 <= (now - fact.occurred_at).total_seconds() <= 300:
        raise ValueError("qq_fact_not_recent")
    if fact.group_id not in config.allowed_group_ids:
        raise ValueError("qq_fact_scope_mismatch")
    with closing(_readonly(config.memory_db)) as db:
        if fact.kind == "verified_message":
            observation = _observation(db, fact.group_id, fact.fact_id)
            if (observation is not None and observation.member_id != config.self_member_id
                    and fact.occurred_at == observation.stored_at and fact.related_attempt_id is None
                    and set(fact.evidence_refs) == set(observation.evidence)):
                return
        elif fact.kind == "send_attempt":
            for receipt in _receipts(db, fact.group_id, now - timedelta(seconds=300)):
                if (receipt.receipt_id == fact.fact_id and receipt.self_member_id == config.self_member_id
                        and fact.occurred_at == receipt.delivered_at and fact.evidence_refs == [receipt.receipt_id]
                        and fact.related_attempt_id is None and not getattr(receipt, "segment_receipt_ids", None)):
                    return
        elif fact.kind == "verified_interaction":
            verifier = _FactVerifier(config, fact)
            for row in db.execute(
                    "SELECT decision_id,record_json FROM qq_reply_decisions WHERE group_id=? AND status='completed' ORDER BY rowid DESC LIMIT 32",
                    (fact.group_id,)):
                decision = QQReplyDecision.model_validate_json(row["record_json"])
                if decision.decision_id != row["decision_id"]:
                    raise ValueError("qq_fact_decision_binding_invalid")
                feedback_for_direct_response(verifier, memory_db=config.memory_db, decision=decision, clock=lambda: now)
                if verifier.matched:
                    return
    # No observation or confirmed send is manufactured for no-response or
    # novelty. Those future collectors need their own verifiable source type.
    raise ValueError("qq_fact_evidence_invalid")


class QQFactIngress:
    def __init__(self, supervisor, config: QQSessionSettings):
        self.supervisor, self.config = supervisor, config
        self._lock = asyncio.Lock()
        self._accepted: dict[str, RuntimeEvent] | None = None

    def _restore(self):
        if self._accepted is not None:
            return
        restored = {}
        for record in self.supervisor.journal.iter_records():
            if record.record_type != "event_accepted":
                continue
            raw = record.data.get("event") or {}
            if raw.get("event_type") != RuntimeEventType.QQ_FACT:
                continue
            event = RuntimeEvent.model_validate(raw)
            fact = QQHomeostaticFact.model_validate(event.payload["qq_fact"])
            old = restored.get(fact.fact_key)
            if old is not None and QQHomeostaticFact.model_validate(old.payload["qq_fact"]).fact_hash != fact.fact_hash:
                raise ValueError("qq_fact_journal_conflict")
            restored[fact.fact_key] = event
        self._accepted = restored

    async def submit(self, fact: QQHomeostaticFact):
        if fact.group_id not in self.config.allowed_group_ids:
            raise ValueError("qq_fact_scope_mismatch")
        async with self._lock:
            lookup = QQFactLookup(group_id=fact.group_id, fact_id=fact.fact_id)
            known = await self.supervisor.homeostasis.read_qq_fact(lookup)
            base = {"schema_version": "qq-homeostatic-submit-v1", "fact_key": fact.fact_key, "fact_hash": fact.fact_hash}
            if known.status == "applied":
                if known.fact_hash != fact.fact_hash:
                    raise ValueError("qq_fact_conflict")
                return base | {"status": "applied", "committed_revision": known.committed_revision}
            await asyncio.to_thread(_validate_evidence, self.config, fact, datetime.now(timezone.utc))
            self._restore()
            assert self._accepted is not None
            old = self._accepted.get(fact.fact_key)
            if old is not None:
                if QQHomeostaticFact.model_validate(old.payload["qq_fact"]).fact_hash != fact.fact_hash:
                    raise ValueError("qq_fact_conflict")
                return base | {"status": "queued", "event_id": old.event_id, "receive_seq": old.receive_seq}
            event_id = "qq-fact:" + hashlib.sha256(fact.fact_key.encode()).hexdigest()
            try:
                event = await self.supervisor.submit_event(event_type=RuntimeEventType.QQ_FACT,
                    priority=RuntimeEventPriority.IMPORTANT_ENVIRONMENT, source="qq_verified_collector",
                    source_time=fact.occurred_at, event_id=event_id, correlation_id=event_id,
                    idempotency_key=event_id, payload={"qq_fact": fact.model_dump(mode="json")})
            except Exception:
                # An accepted event may be durable before queue insertion fails.
                # Re-read the journal before retrying; never allocate it again.
                self._accepted = None
                raise
            if event is None:
                self._accepted = None
                raise ValueError("qq_fact_not_queued")
            self._accepted[fact.fact_key] = event
            return base | {"status": "queued", "event_id": event.event_id, "receive_seq": event.receive_seq}


def create_qq_homeostasis_router(control, *, authenticate, config_path: Path | None = None):
    from fastapi import APIRouter, Depends, HTTPException

    config = None
    configured = config_path or os.environ.get("NC_QQ_SESSION_CONFIG")
    if configured:
        path = Path(configured)
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError("qq_homeostasis_configuration_invalid")
        config = QQSessionSettings.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if not config.memory_db.is_absolute() or config.memory_db.is_symlink() or not config.memory_db.is_file():
            raise ValueError("qq_homeostasis_memory_unavailable")
    ingress = QQFactIngress(control.supervisor, config) if config is not None else None
    router = APIRouter(dependencies=[Depends(authenticate)])
    from harness.runtime.qq_turns import add_qq_turn_route
    add_qq_turn_route(router, control, config)

    def require_scope(group_id):
        if config is None:
            raise HTTPException(status_code=503, detail="qq_homeostasis_not_configured")
        if group_id not in config.allowed_group_ids:
            raise HTTPException(status_code=403, detail="qq_fact_scope_mismatch")

    @router.get("/v1/homeostasis/snapshot")
    async def snapshot():
        return (await control.supervisor.homeostasis.get_snapshot()).model_dump(mode="json")

    @router.post("/v1/homeostasis/qq-facts:read")
    async def read_fact(payload: dict):
        try:
            lookup = QQFactLookup.model_validate(payload)
        except ValueError:
            raise HTTPException(status_code=422, detail="qq_fact_lookup_invalid") from None
        require_scope(lookup.group_id)
        return (await control.supervisor.homeostasis.read_qq_fact(lookup)).model_dump(mode="json")

    @router.post("/v1/homeostasis/qq-facts")
    async def submit(payload: dict):
        try:
            fact = QQHomeostaticFact.model_validate(payload)
        except ValueError:
            raise HTTPException(status_code=422, detail="qq_fact_invalid") from None
        require_scope(fact.group_id)
        assert ingress is not None
        try:
            return await ingress.submit(fact)
        except (ValueError, OSError, KeyError):
            raise HTTPException(status_code=409, detail="qq_fact_evidence_or_state_invalid") from None

    return router
