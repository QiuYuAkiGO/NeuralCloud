"""Runtime-owned QQ capability dispatch, with watcher-ledger evidence checks.

Dispatch never waits for the GUI actor: that actor may itself enqueue QQ_TURN
on this Runtime while preparing a reply. Acknowledgements contain references,
not claims of successful delivery.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from harness.embodiment.capabilities import QQ_COMMUNICATION_READ, QQ_COMMUNICATION_SEND
from harness.embodiment.qq_contracts import QQReadResult, QQScope
from harness.qq.capability_bridge import QQCapabilityReport, QQCapabilityClaim, QQCapabilityAck
from harness.qq.decision import get_qq_delivery, get_qq_segment_deliveries, _frozen_decision
from harness.qq.feedback import _readonly
from harness.qq.roster import configured_groups
from harness.qq.session import QQSessionSettings
from harness.qq.watch_ledger import WatchEvent, SendReservation


def _now():
    return datetime.now(timezone.utc)


def _time(value):
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("qq_capability_aware_time_required")
    return moment


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class QQCapabilityBridge:
    def __init__(self, supervisor, config: QQSessionSettings, watcher_config_path: Path, state_path: Path, *, clock=None):
        self.supervisor, self.config = supervisor, config
        self.watcher_path, self.path = Path(watcher_config_path), Path(state_path)
        self.clock = clock or _now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS qq_capability_reports (
                    scope_key TEXT PRIMARY KEY, report_json TEXT NOT NULL, valid_until TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS qq_capability_dispatches (
                    opportunity_id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                    status TEXT NOT NULL, claim_json TEXT, result_json TEXT);
            """)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _ownership(self):
        controller = getattr(self.supervisor, "capabilities", None)
        owner = controller.owner("qq") if controller is not None else "legacy"
        generation = getattr(controller, "generation", 0)
        options = getattr(getattr(self.supervisor, "manifest", None), "autonomous_capabilities", None)
        send = bool(getattr(options, "qq_send_enabled", False))
        return owner, generation, send

    def _ineligible(self, call):
        controller = getattr(self.supervisor, "capabilities", None)
        calls = getattr(controller, "calls", None)
        original = calls.get(call["opportunity_id"]) if calls is not None else None
        return controller.invalid_reason(original) if original is not None else None

    def _scope(self, scope: QQScope, run_id=None):
        path = self.watcher_path
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError("qq_capability_watcher_configuration_invalid")
        settings = json.loads(path.read_text(encoding="utf-8"))
        expected = QQScope(group_id=scope.group_id, self_member_id=settings["self_member_id"],
            body_instance_id=settings["expected_instance_id"], environment_revision=settings["expected_revision"],
            window_id=settings["window_id"])
        entry = next((item for item in configured_groups(settings) if item.group_id == scope.group_id), None)
        if (scope != expected or entry is None or not entry.observe or scope.group_id not in self.config.allowed_group_ids
                or scope.self_member_id != self.config.self_member_id or not settings.get("qq_body_enabled")):
            raise ValueError("qq_capability_scope_invalid")
        ledger = Path(settings["state_db"])
        if not ledger.is_absolute() or ledger.is_symlink() or not ledger.is_file():
            raise ValueError("qq_capability_ledger_unavailable")
        with closing(_readonly(ledger)) as db:
            state = db.execute("SELECT * FROM qq_watch_state WHERE singleton=1").fetchone()
        # One watcher owns the GUI and the home ledger. Roster reads retain
        # their own QQScope while sharing that watcher's run and dispatch queue.
        if state is None or state["group_id"] != settings["group_id"] or (run_id is not None and state["run_id"] != run_id):
            raise ValueError("qq_capability_run_invalid")
        return ledger, settings, dict(state)

    @staticmethod
    def _send_allowed(settings, scope):
        return scope.group_id == settings["group_id"] and any(
            item.group_id == scope.group_id and item.observe and item.send for item in configured_groups(settings))

    @staticmethod
    def _key(scope):
        return hashlib.sha256(_json(scope.model_dump(mode="json")).encode()).hexdigest()

    def report(self, report: QQCapabilityReport):
        _, settings, state = self._scope(report.scope, report.run_id)
        now = self.clock()
        if not -2 <= (now - report.reported_at).total_seconds() <= 15:
            raise ValueError("qq_capability_report_expired")
        report = report.model_copy(update={
            "read_ready": report.read_ready and not state["baseline_required"],
            "send_ready": report.send_ready and self._send_allowed(settings, report.scope)
                and bool(settings.get("automatic_send")) and not state["baseline_required"],
        })
        expiry = now + timedelta(seconds=15)
        with closing(self._db()) as db, db:
            db.execute("INSERT OR REPLACE INTO qq_capability_reports VALUES(?,?,?)",
                (self._key(report.scope), report.model_dump_json(), expiry.isoformat()))
        owner, generation, send = self._ownership()
        return {"owner": owner, "generation": generation, "qq_send_enabled": send, "valid_until": expiry.isoformat()}

    def catalog(self):
        now = self.clock()
        owner, generation, send = self._ownership()
        if owner != "shared":
            return []
        with closing(self._db()) as db:
            rows = db.execute("SELECT * FROM qq_capability_reports").fetchall()
        offers = []
        for row in rows:
            if now >= _time(row["valid_until"]):
                continue
            report = QQCapabilityReport.model_validate_json(row["report_json"])
            try:
                _, settings, _ = self._scope(report.scope, report.run_id)
            except (ValueError, OSError, KeyError):
                continue
            entry = next(item for item in configured_groups(settings) if item.group_id == report.scope.group_id)
            allow_send = self._send_allowed(settings, report.scope)
            for capability, ready in ((QQ_COMMUNICATION_READ, report.read_ready), (QQ_COMMUNICATION_SEND, report.send_ready and send)):
                if capability == QQ_COMMUNICATION_SEND and not allow_send:
                    continue
                offers.append({"offer_id": f"qq:{row['scope_key']}:{capability}", "channel": "qq",
                    "capability_id": capability, "provider_id": "qq-watcher", "body_instance_id": report.scope.body_instance_id,
                    "scope": report.scope.model_dump(mode="json"), "activation_revision": f"{report.run_id}:{generation}",
                    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                    "observation_refs": report.observation_refs,
                    "observation": {"group_id": entry.group_id, "group_label": entry.label,
                        "read_only": not allow_send, "home_group_id": settings["group_id"]}, "supported": True,
                    "available": not report.paused and (capability != QQ_COMMUNICATION_SEND or send),
                    "ready": ready and not report.paused and not report.uncertain,
                    "valid_until": row["valid_until"]})
        return offers

    def enqueue(self, row: dict):
        """Persist one dispatch and return immediately; never call the QQ actor."""
        scope = QQScope.model_validate(row["scope"])
        deadline = _time(row["deadline_at"])
        owner, generation, send = self._ownership()
        if owner != "shared" or row["generation"] != generation:
            raise ValueError("qq_capability_owner_changed")
        if row["capability_id"] not in {QQ_COMMUNICATION_READ, QQ_COMMUNICATION_SEND}:
            raise ValueError("qq_capability_unknown")
        if row["capability_id"] == QQ_COMMUNICATION_SEND and not send:
            raise ValueError("qq_capability_send_disabled")
        _, settings, _ = self._scope(scope)
        if row["capability_id"] == QQ_COMMUNICATION_SEND and not self._send_allowed(settings, scope):
            raise ValueError("qq_capability_send_disabled")
        request = {key: row[key] for key in ("opportunity_id", "generation", "capability_id", "selection_ref", "offer_id", "body_instance_id")}
        request.update(scope=scope.model_dump(mode="json"), deadline_at=deadline.isoformat())
        summary = row.get("homeostatic_summary") or {}
        if summary:
            request.update(source_homeostasis_revision=summary["snapshot_revision"], source_episode_signature=summary["episode_signature"])
        if row.get("offer", {}).get("activation_revision"):
            request["activation_revision"] = row["offer"]["activation_revision"]
        for key in ("source_homeostasis_revision", "source_episode_signature", "activation_revision"):
            if key in row:
                request[key] = row[key]
        if request["body_instance_id"] != scope.body_instance_id or not request["selection_ref"]:
            raise ValueError("qq_capability_body_binding_invalid")
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM qq_capability_dispatches WHERE opportunity_id=?", (request["opportunity_id"],)).fetchone()
            if existing:
                if existing["request_json"] != _json(request):
                    raise ValueError("qq_capability_request_conflict")
                return self._result_row(existing)
            if not 0 < (deadline - self.clock()).total_seconds() <= 60:
                raise ValueError("qq_capability_dispatch_expired")
            offers = [item for item in self.catalog() if item["offer_id"] == row["offer_id"] and item["ready"]]
            if not offers or (request.get("activation_revision") is not None
                    and request["activation_revision"] != offers[0]["activation_revision"]):
                raise ValueError("qq_capability_unavailable")
            if any(request[key] != offers[0][key] for key in ("capability_id", "scope", "body_instance_id")):
                raise ValueError("qq_capability_offer_binding_invalid")
            db.execute("INSERT INTO qq_capability_dispatches VALUES(?,?,'pending',NULL,NULL)", (request["opportunity_id"], _json(request)))
        return {"opportunity_id": request["opportunity_id"], "status": "pending"}

    def claim(self, request: QQCapabilityClaim):
        _, settings, _ = self._scope(request.scope, request.run_id)
        owner, generation, send = self._ownership()
        if owner != "shared" or request.generation != generation:
            return {"opportunity": None}
        ready = {item["offer_id"] for item in self.catalog() if item["ready"]}
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM qq_capability_dispatches WHERE status IN ('pending','claimed') ORDER BY rowid").fetchall():
                call = json.loads(row["request_json"])
                if request.opportunity_id is not None and call["opportunity_id"] != request.opportunity_id:
                    continue
                if call["scope"] != request.scope.model_dump(mode="json"):
                    continue
                if call["capability_id"] == QQ_COMMUNICATION_SEND and (
                        not send or not self._send_allowed(settings, request.scope)):
                    continue
                invalid = self._ineligible(call)
                if call["generation"] != generation or self.clock() >= _time(call["deadline_at"]) or invalid:
                    # A prior claim may already have effects: never reassign it.
                    state = "uncertain" if row["claim_json"] else "superseded" if invalid else "expired"
                    db.execute("UPDATE qq_capability_dispatches SET status=? WHERE opportunity_id=?", (state, call["opportunity_id"]))
                    continue
                if row["claim_json"]:
                    bound = json.loads(row["claim_json"])
                    return {"opportunity": bound if bound["run_id"] == request.run_id else None}
                if call["offer_id"] not in ready or (call["capability_id"] == QQ_COMMUNICATION_SEND and not send):
                    continue
                bound = {**call, "run_id": request.run_id, "claim_token": uuid4().hex, "claimed_at": self.clock().isoformat()}
                db.execute("UPDATE qq_capability_dispatches SET status='claimed',claim_json=? WHERE opportunity_id=?",
                    (_json(bound), call["opportunity_id"]))
                return {"opportunity": bound}
        return {"opportunity": None}

    def _verified_result(self, bound):
        ledger, _, _ = self._scope(QQScope.model_validate(bound["scope"]))
        with closing(_readonly(ledger)) as db:
            local = db.execute("SELECT * FROM qq_watch_capability_claims WHERE opportunity_id=?", (bound["opportunity_id"],)).fetchone()
            if local is None or json.loads(local["request_json"]) != bound or local["run_id"] != bound["run_id"]:
                raise ValueError("qq_capability_local_claim_missing")
            if local["event_id"] is None:
                if local["status"] in {"rejected", "expired", "failed", "superseded", "wait"}:
                    return {"status": local["status"], "evidence_refs": []}
                if bound["capability_id"] != QQ_COMMUNICATION_READ or local["status"] != "completed":
                    return {"status": "pending"}
                read = QQReadResult.model_validate_json(local["result_json"])
                if (read.scope.model_dump(mode="json") != bound["scope"]
                        or not _time(bound["claimed_at"]) <= read.observed_at <= _time(bound["deadline_at"])):
                    raise ValueError("qq_capability_read_evidence_mismatch")
                return {"status": "completed", "receipt": read.model_dump(mode="json"), "evidence_refs": list(read.evidence_refs)}
            record = db.execute("SELECT record_json FROM qq_watch_events WHERE event_id=?", (local["event_id"],)).fetchone()
            event = WatchEvent.model_validate_json(record[0]) if record else None
            if event is None or event.origin != "homeostasis" or event.run_id != bound["run_id"]:
                raise ValueError("qq_capability_event_missing")
            intent = event.idle_intent
            if (intent.opportunity_ref != bound["opportunity_id"] or intent.selection_ref != bound["selection_ref"]
                    or intent.scheduler_generation != bound["generation"] or intent.deadline_at != _time(bound["deadline_at"])):
                raise ValueError("qq_capability_event_binding_invalid")
            send_row = db.execute("SELECT record_json FROM qq_watch_sends WHERE event_id=?", (event.event_id,)).fetchone()
            if send_row is None:
                status = "wait" if event.state == "decided_wait" else event.state if event.state in {"failed", "expired", "superseded"} else "pending"
                return {"status": status, "event_id": event.event_id, "evidence_refs": []}
            reservation = SendReservation.model_validate_json(send_row[0])
            if (reservation.run_id != bound["run_id"] or reservation.decision_id != event.decision_id
                    or reservation.deadline_at != intent.deadline_at):
                raise ValueError("qq_capability_send_binding_invalid")
            if reservation.status != "sent":
                status = "uncertain" if reservation.status in {"click_started", "uncertain", "partial"} else "failed" if reservation.status == "not_sent" else "pending"
                return {"status": status, "event_id": event.event_id, "evidence_refs": []}
            segments = [json.loads(item[0]) for item in db.execute(
                "SELECT record_json FROM qq_watch_send_segments WHERE parent_id=? ORDER BY segment_index", (reservation.reservation_id,))]
        with closing(_readonly(self.config.memory_db)) as memory:
            decision = _frozen_decision(memory, event.group_id, reservation.decision_id)
        if (decision.initiated_by != "homeostasis" or decision.idle_intent_id != intent.intent_id
                or decision.self_member_id != intent.self_member_id
                or decision.outcome != "ready"):
            raise ValueError("qq_capability_decision_binding_invalid")
        receipts = (get_qq_segment_deliveries(memory_db=self.config.memory_db, group_id=event.group_id, decision_id=reservation.decision_id)
                    if segments else [get_qq_delivery(memory_db=self.config.memory_db, group_id=event.group_id, decision_id=reservation.decision_id)])
        records = segments or [reservation.model_dump(mode="json")]
        if len(receipts) != len(records) or not receipts:
            raise ValueError("qq_capability_receipt_missing")
        for receipt, record in zip(receipts, records):
            proof = record.get("proof") if segments else record.get("preclick_proof")
            click = record.get("click_started_at")
            click_upper = record.get("click_acknowledged_at") or record.get("click_observed_at")
            if (receipt is None or record.get("receipt_id") != receipt.receipt_id or record["status"] != "sent"
                    or receipt.decision_id != reservation.decision_id or receipt.self_member_id != bound["scope"]["self_member_id"]
                    or receipt.source_record_ids or proof is None
                    or any(proof[key] != bound["scope"][key] for key in bound["scope"])
                    or proof["reply_sha256"] != hashlib.sha256(receipt.delivered_text.encode()).hexdigest()
                    or not click or not click_upper
                    or not _time(proof["verified_at"]) <= _time(click) <= _time(click_upper) <= receipt.delivered_at
                    or receipt.delivered_at > intent.deadline_at):
                raise ValueError("qq_capability_receipt_binding_invalid")
        return {"status": "completed", "event_id": event.event_id, "decision_id": reservation.decision_id,
                "receipt": {"receipt_ids": [item.receipt_id for item in receipts]},
                "evidence_refs": [ref for item in receipts for ref in item.evidence_refs]}

    def acknowledge(self, request: QQCapabilityAck):
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_capability_dispatches WHERE opportunity_id=?", (request.opportunity_id,)).fetchone()
            if row is None or row["claim_json"] is None:
                raise ValueError("qq_capability_claim_missing")
            bound = json.loads(row["claim_json"])
            if (bound["claim_token"] != request.claim_token or bound["run_id"] != request.run_id
                    or bound["generation"] != request.generation or bound["scope"] != request.scope.model_dump(mode="json")):
                raise ValueError("qq_capability_ack_binding_invalid")
            if row["result_json"] and row["status"] != "uncertain":
                return self._result_row(row)
            result = self._verified_result(bound)
            if result["status"] != "pending":
                db.execute("UPDATE qq_capability_dispatches SET status=?,result_json=? WHERE opportunity_id=?",
                    (result["status"], _json(result), request.opportunity_id))
            return {"opportunity_id": request.opportunity_id, **result}

    @staticmethod
    def _result_row(row):
        return {"opportunity_id": row["opportunity_id"], "status": row["status"], **json.loads(row["result_json"] or "{}")}

    def result(self, opportunity_id):
        with closing(self._db()) as db, db:
            row = db.execute("SELECT * FROM qq_capability_dispatches WHERE opportunity_id=?", (opportunity_id,)).fetchone()
            if row and row["status"] in {"pending", "claimed"}:
                call = json.loads(row["request_json"])
                owner, generation, _ = self._ownership()
                invalid = self._ineligible(call) or ("superseded" if owner != "shared" or generation != call["generation"] else None)
                expired = self.clock() >= _time(call["deadline_at"])
                if invalid or expired:
                    status = "uncertain" if row["claim_json"] else "expired" if expired else "superseded"
                    db.execute("UPDATE qq_capability_dispatches SET status=? WHERE opportunity_id=?", (status, opportunity_id))
                    row = db.execute("SELECT * FROM qq_capability_dispatches WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        return self._result_row(row) if row else None

    get = result


def create_qq_capability_router(supervisor, *, authenticate, config_path=None, state_path=None):
    from fastapi import APIRouter, Depends, HTTPException
    supervisor = getattr(supervisor, "supervisor", supervisor)
    router = APIRouter(dependencies=[Depends(authenticate)])
    configured = config_path or os.environ.get("NC_QQ_SESSION_CONFIG")
    bridge = None
    if configured:
        path = Path(configured)
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError("qq_capability_configuration_invalid")
        config = QQSessionSettings.model_validate_json(path.read_text(encoding="utf-8"))
        watcher = os.environ.get("NC_QQ_WATCHER_CONFIG")
        watcher_path = Path(watcher) if watcher else config.watcher_status_file.parent / "watcher.json" if config.watcher_status_file else None
        if watcher_path is not None:
            bridge = QQCapabilityBridge(supervisor, config, watcher_path,
                Path(state_path) if state_path else supervisor.state_dir / "qq-capabilities.sqlite3")
        direct = os.environ.get("NC_QQ_DIRECT_CONFIG")
        if direct:
            from harness.qq.direct_capabilities import QQCapabilityTransports, QQDirectCapabilityBridge

            direct_bridge = QQDirectCapabilityBridge(supervisor, config, Path(direct),
                (Path(state_path) if state_path else supervisor.state_dir / "qq-capabilities.sqlite3").with_name("qq-direct-capabilities.sqlite3"))
            bridge = QQCapabilityTransports(bridge, direct_bridge)
    supervisor.qq_capability_bridge = bridge

    def invoke(method, schema, payload):
        if bridge is None:
            raise HTTPException(status_code=503, detail="qq_capability_not_configured")
        try:
            return getattr(bridge, method)(schema.model_validate(payload))
        except (ValueError, OSError, KeyError, sqlite3.Error):
            raise HTTPException(status_code=409, detail="qq_capability_binding_or_state_invalid") from None

    @router.post("/v1/qq/capabilities/report")
    async def report(payload: dict):
        return invoke("report", QQCapabilityReport, payload)

    @router.post("/v1/qq/opportunities/claim")
    async def claim(payload: dict):
        return invoke("claim", QQCapabilityClaim, payload)

    @router.post("/v1/qq/opportunities/ack")
    async def acknowledge(payload: dict):
        return invoke("acknowledge", QQCapabilityAck, payload)

    return router
