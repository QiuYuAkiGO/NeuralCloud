"""Read verified QQ records into factual homeostatic feedback, without text copies."""
from contextlib import closing
from datetime import timedelta
import hashlib
from pathlib import Path
import sqlite3

from harness.qq.decision import (
    QQDeliveryReceipt, QQReplyDecision, QQSegmentDeliveryReceipt, _check_aggregate, _checked_receipt,
    _frozen_decision, _read_segment_receipts,
)
from harness.qq.memory import QQVerifiedProfile, QQVisualObservation
from harness.qq.watch_ledger import WatchEvent


def _readonly(path):
    db = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def _observation(db, group_id, source_id):
    row = db.execute("SELECT * FROM qq_visual_observations WHERE record_id=? AND group_id=?",
                     (source_id, group_id)).fetchone()
    if row is None:
        return None
    observation = QQVisualObservation.model_validate_json(row["record_json"])
    if any(getattr(observation, key) != row[key] for key in (
            "record_id", "group_id", "member_id", "profile_record_id", "capture_id", "capture_item_id")):
        return None
    row = db.execute("SELECT * FROM qq_visual_profiles WHERE record_id=? AND group_id=?",
                     (observation.profile_record_id, group_id)).fetchone()
    if row is None:
        return None
    profile = QQVerifiedProfile.model_validate_json(row["record_json"])
    if (profile.record_id != row["record_id"] or profile.group_id != row["group_id"]
            or profile.member_id != row["member_id"] or profile.member_id != observation.member_id):
        return None
    return observation


def _receipts(db, group_id, cutoff, *, limit=96):
    """Read real receipts, including only a validated contiguous segment prefix."""
    prefixes = {}
    for table, model in (("qq_reply_deliveries", QQDeliveryReceipt),
                         ("qq_reply_segment_deliveries", QQSegmentDeliveryReceipt)):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            continue
        for row in db.execute(f"SELECT * FROM {table} WHERE group_id=? ORDER BY rowid DESC LIMIT ?", (group_id, limit)):
            receipt = _checked_receipt(row, model)
            if receipt.delivered_at < cutoff:
                continue
            if isinstance(receipt, QQSegmentDeliveryReceipt) or receipt.segment_receipt_ids:
                if receipt.decision_id not in prefixes:
                    frozen = _frozen_decision(db, group_id, receipt.decision_id)
                    prefixes[receipt.decision_id] = (frozen, _read_segment_receipts(db, group_id=group_id, decision=frozen))
                frozen, prefix = prefixes[receipt.decision_id]
                if isinstance(receipt, QQDeliveryReceipt):
                    _check_aggregate(receipt, prefix, frozen)
            yield receipt


def recover_recent_feedback(driver, *, memory_db, watch_db, clock):
    """Repair the cross-store enqueue gap. Expired history never earns new credit."""
    moment = clock()
    cutoff = max(driver.armed_at, moment - timedelta(seconds=300))
    count = 0
    with closing(_readonly(watch_db)) as ledger, closing(_readonly(memory_db)) as memory:
        events = ledger.execute("SELECT record_json FROM qq_watch_events WHERE group_id=? ORDER BY rowid DESC LIMIT 256",
                                (driver.group_id,)).fetchall()
        for row in events:
            event = WatchEvent.model_validate_json(row[0])
            if (event.group_id != driver.group_id or event.origin != "message" or event.source_record_id is None
                    or event.item is None or event.item.direction != "incoming" or event.item.kind != "text"):
                continue
            observation = _observation(memory, driver.group_id, event.source_record_id)
            if (observation is None or observation.member_id == driver.self_member_id
                    or observation.capture_item_id != event.event_id or observation.text != event.item.text
                    or not cutoff <= observation.stored_at <= moment + timedelta(seconds=5)):
                continue
            count += bool(driver.enqueue_fact(fact_id=observation.record_id, kind="verified_message",
                occurred_at=observation.stored_at, evidence_refs=observation.evidence))
        for receipt in _receipts(memory, driver.group_id, cutoff):
            if (receipt.self_member_id != driver.self_member_id or receipt.delivered_at > moment + timedelta(seconds=5)
                    or getattr(receipt, "segment_receipt_ids", None)):
                continue
            count += bool(driver.enqueue_fact(fact_id=receipt.receipt_id, kind="send_attempt",
                occurred_at=receipt.delivered_at, evidence_refs=[receipt.receipt_id]))
        if memory.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='qq_reply_decisions'").fetchone():
            for row in memory.execute(
                    "SELECT decision_id,record_json FROM qq_reply_decisions WHERE group_id=? AND status='completed' ORDER BY rowid DESC LIMIT 32",
                    (driver.group_id,)):
                outcome = QQReplyDecision.model_validate_json(row["record_json"])
                if (outcome.decision_id != row["decision_id"] or outcome.group_id != driver.group_id
                        or not cutoff <= (outcome.completed_at or outcome.created_at) <= moment + timedelta(seconds=5)):
                    continue
                count += feedback_for_direct_response(driver, memory_db=memory_db, decision=outcome, clock=lambda: moment)
    return count


def feedback_for_direct_response(driver, *, memory_db, decision, clock):
    """Require a supported direct response quoting one recent own send exactly.

    A bare mention or the gate's opinion alone cannot claim a response to an
    actual send. Unlinked conversation still contributes the weaker seen-message
    fact. Ambiguous or very short quoted text earns no interaction credit.
    """
    if (decision.group_id != driver.group_id or decision.self_member_id != driver.self_member_id
            or decision.decision_source != "model" or decision.initiated_by != "message" or decision.addressing != "direct"
            or not decision.addressing_source_record_ids or not decision.delivery_refs
            or not set(decision.addressing_source_record_ids).issubset(
                set(decision.source_record_ids) & set(decision.new_observation_record_ids))
            or decision.outcome not in {"ready", "natural_wait"}):
        return 0
    moment = clock()
    cutoff = max(driver.armed_at, moment - timedelta(seconds=300))
    count = 0
    with closing(_readonly(memory_db)) as db:
        receipts = []
        for receipt in _receipts(db, driver.group_id, cutoff, limit=20):
            if (isinstance(receipt, QQDeliveryReceipt) and receipt.receipt_id in decision.delivery_refs
                    and receipt.self_member_id == driver.self_member_id and receipt.delivered_at <= moment):
                receipts.append(receipt)
        for source_id in decision.addressing_source_record_ids:
            observation = _observation(db, driver.group_id, source_id)
            if (observation is None or observation.member_id == driver.self_member_id
                    or not cutoff <= observation.stored_at <= moment + timedelta(seconds=5)):
                continue
            matched = [receipt for receipt in receipts if receipt.delivered_at < observation.observed_at
                       and any(len(text.strip()) >= 4 and text in observation.text
                               for text in (receipt.delivered_segments or [receipt.delivered_text]))]
            if len(matched) != 1:
                continue
            receipt = matched[0]
            key = hashlib.sha256((source_id + ":" + receipt.receipt_id).encode()).hexdigest()
            count += bool(driver.enqueue_fact(fact_id=f"qq-interaction:{key}", kind="verified_interaction",
                occurred_at=observation.stored_at, related_attempt_id=receipt.receipt_id,
                evidence_refs=[observation.record_id, receipt.receipt_id]))
    return count
