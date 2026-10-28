"""Read adapters. No writer, unrestricted history, or caller-chosen permissions."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from pathlib import Path
import json
import sqlite3
import subprocess
from zoneinfo import ZoneInfo

from .contracts import MemoryBundle, MemoryQuery, MemoryRecord, ReadContext, digest


def open_neuralcloud(path, *, owner_id, preset_id):
    from harness.memory.production import ProductionMemoryStore

    class ReadOnlyStore(ProductionMemoryStore):
        def __init__(self):
            self.path = Path(path).resolve(strict=True)
            self.owner_id, self.preset_id = owner_id, preset_id
            with closing(self._connect()) as db:
                row = db.execute("SELECT owner,preset FROM pm_identity WHERE singleton=1").fetchone()
                if row is None or tuple(row) != (owner_id, preset_id):
                    raise ValueError("memory store identity mismatch")

        def _connect(self):
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            return db

    return ReadOnlyStore()


class NeuralCloudSource:
    def __init__(self, store):
        self.store = store

    def read(self, query: MemoryQuery, context: ReadContext) -> MemoryBundle:
        if query.provider != "neuralcloud":
            raise ValueError("provider mismatch")
        payload = self.store.recall(audience=context.audience, query=query.query, limit=query.limit,
                                   max_characters=100000, now=context.now)
        records = []
        for entry in payload.get("entries", []):
            if not self.store.validate_references([entry], audience=context.audience, now=context.now):
                continue
            # An observed utterance's author is not necessarily the subject of its contents.
            subjects = tuple(entry.get("subject_refs", ())) if entry.get("kind") != "episode" else ()
            records.append(MemoryRecord(record_id=entry["record_id"], revision=str(entry["revision"]),
                subject_ids=subjects, subject_mention=entry.get("subject_mention"),
                kind=entry.get("semantic_status", entry.get("authority", "unknown")),
                content=entry.get("statement") or entry.get("current_impression") or entry.get("summary", ""),
                valid_from=entry.get("valid_from"), valid_until=entry.get("valid_until"),
                evidence_refs=tuple(entry.get("source_refs", ())), details=entry))
        return MemoryBundle(provider="neuralcloud", audience=context.audience, read_at=context.now, records=tuple(records))

    def validate(self, bundle, context):
        return bundle.provider == "neuralcloud" and bundle.audience == context.audience and all(
            r.valid_at(context.now) for r in bundle.records
        ) and self.store.validate_references(
            [{"record_id": r.record_id, "revision": int(r.revision)} for r in bundle.records],
            audience=context.audience, now=context.now)


def hy_time(value, timezone):
    if value is None or value == "":
        return None
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # Hy stores naive local timestamps; its deployment timezone must be configured.
    return result if result.tzinfo else result.replace(tzinfo=ZoneInfo(timezone))


class HyMemorySource:
    def __init__(self, transport, *, timezone: str):
        self.transport, self.timezone = transport, timezone
        ZoneInfo(timezone)

    def read(self, query: MemoryQuery, context: ReadContext) -> MemoryBundle:
        if query.provider != "hy_memory" or not context.hy_user_ids or not context.hy_agent_ids:
            raise ValueError("explicit Hy partition and agent scope required")
        rows = self.transport({"op": "search", "query": query.query, "limit": query.limit,
            "user_ids": list(context.hy_user_ids), "agent_ids": list(context.hy_agent_ids),
            "session_ids": list(context.hy_session_ids)})
        records, seen = [], set()
        for row in rows:
            identifier = row.get("node_id") or row.get("memory_id")
            if not identifier or identifier in seen or not self.allowed(row, context):
                continue
            seen.add(identifier)
            record = MemoryRecord(record_id=identifier, revision=digest(row), kind=str(row.get("layer", "unknown")),
                content=row["content"], valid_from=hy_time(row.get("valid_from"), self.timezone),
                valid_until=hy_time(row.get("valid_until"), self.timezone),
                evidence_refs=tuple(row.get("evidence_chain") or ()),
                details={"subject_status": "unknown", "source_record": row, "provenance_verified": False})
            if record.valid_at(context.now):
                records.append(record)
            if len(records) >= query.limit:
                break
        return MemoryBundle(provider="hy_memory", audience=context.audience, read_at=context.now, records=tuple(records))

    @staticmethod
    def allowed(row, context):
        return (row.get("user_id") in context.hy_user_ids and row.get("agent_id") in context.hy_agent_ids
                and (not context.hy_session_ids or row.get("session_id") in context.hy_session_ids)
                and row.get("is_latest", True) is True and row.get("status", "active") == "active")

    def validate(self, bundle, context):
        if bundle.provider != "hy_memory" or bundle.audience != context.audience:
            return False
        if not bundle.records:
            return True
        rows = self.transport({"op": "get_many", "record_ids": [record.record_id for record in bundle.records]})
        if len(rows) != len(bundle.records):
            return False
        for record, row in zip(bundle.records, rows):
            if not row or not self.allowed(row, context) or digest(row) != record.revision or not record.valid_at(context.now):
                return False
        return True


class HySubprocess:
    """One isolated SDK process per read or validation batch; read operations only."""
    def __init__(self, python, config, checkout, timeout=120):
        self.python, self.config, self.checkout, self.timeout = python, config, checkout, timeout

    def __call__(self, request):
        worker = Path(__file__).with_name("hy_worker.py")
        result = subprocess.run([str(self.python), str(worker), "--config", str(self.config),
            "--checkout", str(self.checkout)], input=json.dumps(request), capture_output=True,
            text=True, encoding="utf-8", timeout=self.timeout, check=False)
        if result.returncode:
            raise RuntimeError("Hy read worker failed; inspect its isolated configuration")
        return json.loads(result.stdout)
