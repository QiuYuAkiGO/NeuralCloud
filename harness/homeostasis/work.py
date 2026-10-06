"""Content-free work receipts shared by model workers and the state advancer."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from harness.homeostasis.dynamics import AttentionAssessment, CognitiveExposure


class WorkLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS work_receipts(id TEXT PRIMARY KEY, record TEXT NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS work_prefixes(context TEXT, hash TEXT, PRIMARY KEY(context,hash));
            """)

    def connect(self):
        return closing_transaction(self.path)

    def pending(self, limit=128):
        with self.connect() as db:
            return [CognitiveExposure.model_validate_json(r[0]) for r in db.execute(
                "SELECT record FROM work_receipts WHERE applied=0 ORDER BY rowid LIMIT ?", (limit,))]

    def acknowledge(self, receipt_id):
        with self.connect() as db:
            db.execute("UPDATE work_receipts SET applied=1 WHERE id=?", (receipt_id,))

    def record(self, *, span_id, context, messages, prefix_count, usage, assessment=None, evidence_ref=None,
               evidence_refs=None, reread=False, at=None, attention_revision=None):
        from harness.qq.attention import text_units
        text_parts, images = [], 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT record FROM work_receipts WHERE id=?", (span_id,)).fetchone()
            if previous:
                return CognitiveExposure.model_validate_json(previous[0])
            for index, message in enumerate(messages[prefix_count:], prefix_count):
                content = message.get("content", "")
                key = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                # A newly attended event can repeat the exact same words. Only
                # automatically carried history/prefixes are free, not new readings.
                occurrence = key
                if index == len(messages)-1 and evidence_ref and message.get("role") != "assistant":
                    occurrence = hashlib.sha256((str(evidence_ref)+":"+key).encode()).hexdigest()
                if not reread and db.execute("SELECT 1 FROM work_prefixes WHERE context=? AND hash=?", (context,occurrence)).fetchone():
                    continue
                db.execute("INSERT OR IGNORE INTO work_prefixes VALUES (?,?)", (context,key))
                db.execute("INSERT OR IGNORE INTO work_prefixes VALUES (?,?)", (context,occurrence))
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}:
                            images += 1
                        elif isinstance(part, dict) and isinstance(part.get("text"), str):
                            text_parts.append(part["text"])
            text = "\n".join(text_parts)
            interpretation = None
            if assessment is not None:
                try:
                    interpretation = AttentionAssessment.model_validate(assessment)
                except (ValueError, TypeError):
                    pass  # Optional interpretation never changes reply success.
            receipt = CognitiveExposure(exposure_id=span_id, context_id=context[:256],
                attended_at=at or datetime.now(UTC), input_tokens=min(2_000_000, text_units(text)),
                output_tokens=min(2_000_000, max(0, int((usage or {}).get("completion_tokens", 0) or 0))),
                input_count_basis="text_units_estimate",
                output_count_basis=("unavailable" if not usage else "estimate" if usage.get("source") == "estimate" else "api"),
                images=min(images, 1000), content_hash=hashlib.sha256(text.encode()).hexdigest(),
                evidence_refs=list(dict.fromkeys([*(evidence_refs or []), *([evidence_ref] if evidence_ref else [])]))[:128],
                assessment=interpretation, attention_revision=attention_revision)
            db.execute("INSERT OR IGNORE INTO work_receipts(id,record) VALUES (?,?)", (span_id, receipt.model_dump_json()))
            return receipt


@contextmanager
def closing_transaction(path):
    with closing(sqlite3.connect(path, timeout=30)) as db:
        with db:
            yield db


def configured_work_ledger():
    path = os.environ.get("NC_COGNITIVE_WORK_DB")
    return WorkLedger(path) if path else None
