"""Frozen Runtime task notices delivered by DirectQQActor's existing sender.

These are service facts, not forged model decisions. They share the protocol
send ledger/uncertainty barrier and require independently verified private receipts.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json

from harness.qq.direct_store import canonical, normalize_evidence, utcnow
from harness.qq.conversation import conversation_scope
from harness.qq.onebot import OneBotError
from harness.qq.telemetry import QQDeadline


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS qq_task_notifications(
        id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, send_id TEXT UNIQUE,
        result_json TEXT NOT NULL)""")


class TaskNotifications:
    def __init__(self, store):
        self.store = store
        with closing(store.connect()) as db, db:
            initialize(db)

    def publish(self, task, notice):
        source = task["request"]["source"]
        expected_task = "user-task:" + hashlib.sha256(source["event_id"].encode()).hexdigest()[:32]
        purpose = notice["purpose"]
        if (task["id"] != expected_task or purpose not in {"task_accepted", "task_result"}
                or notice["task_id"] != task["id"] or notice["id"] != task["id"] + ":" + purpose
                or not isinstance(notice["text"], str) or not 0 < len(notice["text"]) <= 3000):
            raise ValueError("task_notice_binding_invalid")
        if purpose == "task_result" and task["status"] not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("task_notice_result_not_ready")
        scope = conversation_scope(source["group_id"])
        if scope["kind"] != "private" or scope["peer_id"] != source["member_id"] or scope["self_member_id"] != source["self_member_id"]:
            raise ValueError("task_notice_private_source_required")
        original = self.store.get(source["event_id"])
        if any(original["event"][key] != value for key, value in source.items()):
            raise ValueError("task_notice_source_changed")
        if original["event"]["own"] or original["event"]["historical"] or not original["source_record_id"]:
            raise ValueError("task_notice_source_not_bound")
        payload = canonical({"id": notice["id"], "task_id": task["id"], "purpose": purpose,
                             "text": notice["text"], "source": source, "source_record_id": original["source_record_id"]})
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM qq_task_notifications WHERE id=?", (notice["id"],)).fetchone()
            if old:
                if old["payload_json"] != payload:
                    raise ValueError("task_notice_immutable")
                return json.loads(old["result_json"])
            db.execute("INSERT INTO qq_task_notifications VALUES(?,?,NULL,?)",
                       (notice["id"], payload, canonical({"status": "pending"})))
        return {"status": "pending"}

    def pending(self):
        with closing(self.store.connect()) as db:
            row = db.execute("SELECT * FROM qq_task_notifications WHERE send_id IS NULL ORDER BY rowid LIMIT 1").fetchone()
        return json.loads(row["payload_json"]) if row else None

    def reserve(self, notice):
        source = notice["source"]
        send = {"send_id": "task-send:" + hashlib.sha256(notice["id"].encode()).hexdigest(),
                "event_id": source["event_id"], "decision_id": notice["id"], "group_id": source["group_id"],
                "self_member_id": source["self_member_id"], "segment_index": 0, "text": notice["text"],
                "source_record_id": notice["source_record_id"], "status": "reserved", "message_id": None,
                "receipt_id": None, "received_at": utcnow().isoformat(), "task_notice_id": notice["id"]}
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_task_notifications WHERE id=?", (notice["id"],)).fetchone()
            if row is None or row["send_id"] is not None or row["payload_json"] != canonical(notice):
                raise ValueError("task_notice_already_claimed")
            if db.execute("SELECT 1 FROM qq_direct_sends WHERE status IN ('attempted','unknown')").fetchone():
                raise ValueError("task_notice_delivery_unknown")
            db.execute("INSERT INTO qq_direct_sends VALUES(?,?,?,?,?,'reserved',NULL,NULL,?)",
                       (send["send_id"], send["event_id"], send["decision_id"], send["group_id"], 0, canonical(send)))
            db.execute("UPDATE qq_task_notifications SET send_id=?,result_json=? WHERE id=?",
                       (send["send_id"], canonical({"status": "inflight"}), notice["id"]))
        return send

    def reconcile_rows(self):
        with closing(self.store.connect()) as db:
            return [(json.loads(row["payload_json"]), json.loads(row["record_json"])) for row in db.execute("""
                SELECT n.payload_json,s.record_json FROM qq_task_notifications n
                JOIN qq_direct_sends s ON n.send_id=s.send_id
                WHERE json_extract(n.result_json,'$.status') IN ('inflight','unknown') ORDER BY n.rowid LIMIT 4""")]

    def result(self, notice, status, **extra):
        if status not in {"cancelled", "unknown"}:
            raise ValueError("task_notice_result_invalid")
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE qq_task_notifications SET result_json=? WHERE id=?",
                       (canonical({"status": status, **extra}), notice["id"]))

    def confirm(self, notice, send, proof_ref):
        proof = self.store._protocol_evidence(proof_ref)
        scope = conversation_scope(send["group_id"])
        verified = normalize_evidence(proof, self_id=send["self_member_id"], groups=(), private_peers=[scope["peer_id"]])
        if (proof.get("action") != "get_friend_msg_history" or verified is None or not verified["own"]
                or verified["group_id"] != send["group_id"] or verified["message_id"] != send["message_id"]
                or verified["text"] != send["text"] or verified["images"]):
            raise ValueError("task_notice_receipt_invalid")
        receipt_id = "task-delivery:" + hashlib.sha256(canonical([send["send_id"], proof_ref]).encode()).hexdigest()
        result = {"status": "delivered", "receipt_id": receipt_id, "proof_ref": proof_ref,
                  "message_id": send["message_id"], "delivered_at": utcnow().isoformat()}
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_task_notifications WHERE id=?", (notice["id"],)).fetchone()
            stored = db.execute("SELECT record_json FROM qq_direct_sends WHERE send_id=?", (send["send_id"],)).fetchone()
            if (row is None or row["payload_json"] != canonical(notice) or row["send_id"] != send["send_id"]
                    or stored is None or stored[0] != canonical(send) or send["status"] not in {"attempted", "unknown"}
                    or send.get("task_notice_id") != notice["id"] or send["text"] != notice["text"]):
                raise ValueError("task_notice_receipt_binding_changed")
            updated = send | {"status": "sent", "receipt_id": receipt_id, "proof_ref": proof_ref,
                              "delivered_at": result["delivered_at"]}
            db.execute("UPDATE qq_direct_sends SET status='sent',receipt_id=?,record_json=? WHERE send_id=?",
                       (receipt_id, canonical(updated), send["send_id"]))
            db.execute("UPDATE qq_task_notifications SET result_json=? WHERE id=?", (canonical(result), notice["id"]))
        return result


async def process_notice(actor):
    notifications = TaskNotifications(actor.store)
    notice = notifications.pending()
    if notice is None or actor.store.uncertain():
        return False
    event = notice["source"]
    # Leave revoked/unavailable notices pending for Web inspection; no new peer
    # or model-supplied purpose can turn them into a permitted send.
    if event["group_id"] not in actor.current_conversations(send=True):
        return False
    async with actor._delivery_lock:
        epoch = actor.epoch

        def guard():
            if (not actor.active() or actor.stop.is_set() or epoch != actor.epoch
                    or event["self_member_id"] != actor.settings.self_member_id
                    or event["group_id"] not in actor.current_conversations(send=True)):
                raise OneBotError("direct_owner_unavailable")
        guard()
        send = notifications.reserve(notice)
        try:
            guard()
        except Exception:
            actor.store.update_send(send, "cancelled")
            notifications.result(notice, "cancelled")
            return True
        send = actor.store.update_send(send, "attempted", attempted_at=utcnow().isoformat())
        try:
            guard()
        except Exception:
            actor.store.cancel_untransmitted(send, reason="direct_owner_unavailable")
            notifications.result(notice, "cancelled")
            return True
        try:
            from harness.qq.direct_store import numeric
            result = await actor.transmit_text(event, notice["text"], timeout=10)
            send = actor.store.update_send(send, "attempted", message_id=numeric(result.get("message_id"), signed=True))
            proof = await actor.verify_sent(event, send["message_id"], notice["text"])
            notifications.confirm(notice, send, proof)
        except Exception:
            actor.store.update_send(send, "unknown")
            notifications.result(notice, "unknown")
        return True


async def reconcile_notices(actor):
    if not actor.active() or actor._delivery_lock.locked():
        return
    notifications = TaskNotifications(actor.store)
    async with actor._delivery_lock:
        budget = QQDeadline.after(10)
        for notice, send in notifications.reconcile_rows():
            if budget.expired:
                break
            if send["status"] == "cancelled":
                notifications.result(notice, "cancelled")
            elif send["message_id"] is not None:
                try:
                    proof = await actor.verify_sent(notice["source"], send["message_id"], send["text"],
                                                    timeout=min(3, budget.check()), history_count=200)
                    notifications.confirm(notice, send, proof)
                except (ValueError, OneBotError, TimeoutError):
                    notifications.result(notice, "unknown")
            else:
                notifications.result(notice, "unknown")
