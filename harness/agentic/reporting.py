"""Read-only operator projections. Never included in a model or QQ payload."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import base64
import json
import math

from harness.agentic.affect_store import AffectStore
from harness.memory.production import BeliefRevision


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def timestamp(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.utcoffset() is None:
        raise ValueError("report_time_requires_timezone")
    return value.timestamp()


def number(value):
    return float(value) if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


def decode_memory_cursor(value, start, end):
    """A sort boundary, never an authorization or a cached private record."""
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 4096:
            raise ValueError
        raw = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if not isinstance(raw, dict) or set(raw) != {"v", "window", "after"}:
            raise ValueError
        if type(raw["v"]) is not int or raw["v"] != 1 or raw["window"] != [iso(start), iso(end)]:
            raise ValueError
        if not isinstance(raw["after"], list) or len(raw["after"]) != 3:
            raise ValueError
        strength, at, identifier = raw["after"]
        if strength is not None and (number(strength) is None or strength < 0):
            raise ValueError
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 512:
            raise ValueError
        stamp = timestamp(at)
        if not start <= stamp < end:
            raise ValueError
        return (-(strength if strength is not None else -1), -stamp, identifier)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid_report_memory_cursor") from exc


def encode_memory_cursor(entry, start, end):
    raw = {"v": 1, "window": [iso(start), iso(end)],
           "after": [entry["strength"], entry["at"], entry["id"]]}
    return base64.urlsafe_b64encode(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode()).decode()


class AgentReport:
    def __init__(self, service):
        self.service = service
        self.memory = service.store.memory

    def read(self, *, since, until, run_since=None, section="daily", offset=0, limit=100, after=None):
        start, end = timestamp(since), timestamp(until)
        if not 0 < end - start <= 90000 or section not in {"daily", "memories"}:
            raise ValueError("invalid_report_window")
        if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 200:
            raise ValueError("invalid_report_page")
        boundary = decode_memory_cursor(after, start, end)
        with closing(self.memory._connect()) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            memories = self.memories(db, start, end, offset, limit, boundary=boundary)
            if section == "memories":
                return memories
            begun = db.execute("SELECT value FROM agent_report_meta_v1 WHERE key='started'").fetchone()
            return {
                "available": True, "authority": "operator_only", "audience": "local:main",
                "mode": self.service.affect.settings.mode,
                "memories": memories,
                "affect": self.affect(db, start, min(end, self.service.affect.clock())),
                "today": self.work(db, start, end),
                "run": self.work(db, timestamp(run_since), min(end, self.service.affect.clock())) if run_since else {"available": False},
                "telemetry_started_at": iso(float(begun[0])) if begun else None,
            }

    def memories(self, db, start, end, offset, limit, *, boundary=None):
        # Claims have an actual commit timestamp; original experiences retain
        # their occurrence date. Never substitute enqueue time for completion.
        rows = db.execute("""
            SELECT r.*, s.payload AS source_payload, l.payload AS legacy_payload,
                v.payload AS claim_payload,
                COALESCE(json_extract(v.payload,'$.recorded_at'),r.time) AS report_time,
                COALESCE(json_extract(l.payload,'$.strength'),
                    json_extract(s.payload,'$.original.record.strength')) AS strength
            FROM pm_records r
            LEFT JOIN pm_sources s ON s.id=r.id
            LEFT JOIN pm_legacy l ON l.id=r.id
            LEFT JOIN pm_v2_versions v ON v.id=r.id AND v.revision=r.revision
            WHERE r.active=1 AND COALESCE(json_extract(v.payload,'$.recorded_at'),r.time)>=?
                AND COALESCE(json_extract(v.payload,'$.recorded_at'),r.time)<?
            ORDER BY CASE WHEN typeof(strength) IN ('real','integer') AND strength>=0 THEN strength END DESC,
                report_time DESC,r.id
            """, (iso(start), iso(end)))
        entries, visible = [], 0
        for row in rows:
            strength = number(row["strength"])
            strength = strength if strength is not None and strength >= 0 else None
            key = (-(strength if strength is not None else -1), -timestamp(row["report_time"]), row["id"])
            if boundary is not None and key <= boundary:
                continue
            if not self.memory._visible(db, row["id"], "local:main"):
                continue
            if row["kind"] == "belief":
                raw = db.execute("SELECT payload FROM pm_revisions WHERE id=? AND revision=?",
                    (row["id"], row["revision"])).fetchone()
                belief = BeliefRevision.model_validate_json(raw[0])
                now = datetime.now(timezone.utc)
                if belief.valid_from > now or (belief.valid_until and belief.valid_until <= now):
                    continue
            visible += 1
            if boundary is None and visible <= offset:
                continue
            if len(entries) >= limit:
                break
            entry = {
                "id": row["id"], "kind": "impression" if row["kind"] in {"belief", "claim_v2"} else "memory",
                "text": row["text"][:4000], "text_truncated": len(row["text"]) > 4000,
                "at": row["report_time"], "occurred_at": row["time"],
                "time_basis": "recorded" if row["claim_payload"] else "occurred",
                "strength": strength if strength is not None and strength >= 0 else None,
                "strength_basis": "stored_retention_strength" if strength is not None and strength >= 0 else "not_recorded",
                "pinned": bool(row["pinned"]), "important": bool(row["important"]),
                "revision": row["revision"], "source_refs": [],
            }
            if row["source_payload"]:
                source = json.loads(row["source_payload"])
                entry.update(author_id=source["author_id"], source_refs=source["source_refs"], authority=source["kind"])
            else:
                entry["source_refs"] = [r[0] for r in db.execute(
                    "SELECT source FROM pm_evidence WHERE id=? ORDER BY source", (row["id"],))]
                entry["authority"] = "source_anchored_interpretation"
            entries.append(entry)
        has_more = visible > (offset if boundary is None else 0) + len(entries)
        return {"available": True, "items": entries, "offset": offset, "limit": limit,
                "has_more": has_more,
                "next_cursor": encode_memory_cursor(entries[-1], start, end) if entries and has_more else None,
                "sort": "strength_desc_nulls_last,time_desc,id",
                "time_basis": "claims_recorded_at;experiences_occurred_at",
                "scope": "local:main", "strength_semantics": "retention_not_confidence"}

    def affect(self, db, start, end):
        settings = self.service.affect.settings
        # Match the existing mood projection's eight-half-life history. Keep
        # subjective appraisals distinct from Homeostasis sensor/usage traces.
        rows = db.execute("SELECT e.* FROM agent_affect_events_v1 e WHERE e.occurred>=? AND e.occurred<? "
            "AND e.committed<=? AND " + AffectStore.valid_clause() + " ORDER BY e.occurred,e.revision",
            (start - 8 * settings.mood_half_life_seconds, end, end)).fetchall()
        mood_v = mood_a = 0.0
        mood_at = None
        labels, points, events = {}, [], []

        def point(at, event_id=None):
            tail = math.exp2(-(at - mood_at) / settings.mood_half_life_seconds) if mood_at is not None else 1.0
            values = {"mood_valence": round(mood_v * tail, 4), "mood_arousal": round(mood_a * tail, 4)}
            for label, impulses in labels.items():
                values["feeling_" + label] = round(max(
                    intensity * math.exp2(-(at - when) / settings.feeling_half_life_seconds)
                    for when, intensity in impulses), 4)
            return {"at": iso(at), "values": values, "event_id": event_id}

        cursor = start
        for row in rows:
            event = json.loads(row["payload"])
            at = row["occurred"]
            while cursor < at and cursor <= end:
                if mood_at is not None:
                    points.append(point(cursor))
                cursor += 300
            if at >= start and mood_at is not None:
                points.append(point(at))
            previous_values = point(at)["values"]
            decay = math.exp2(-(at - mood_at) / settings.mood_half_life_seconds) if mood_at is not None else 1.0
            alpha = event["confidence"]
            mood_v = (1 - alpha) * mood_v * decay + alpha * event["valence"]
            mood_a = (1 - alpha) * mood_a * decay + alpha * event["arousal"]
            mood_at = at
            for label, intensity in event["feelings"].items():
                labels.setdefault(label, []).append((at, intensity * alpha))
            if at < start:
                continue
            visible = self.service.affect._cause_visible(db, row, event, "local:main")
            identifier = "affect:" + str(row["revision"])
            points.append(point(at, identifier))
            changes = {key: round(value - previous_values.get(key, 0), 4)
                       for key, value in points[-1]["values"].items()
                       if abs(value - previous_values.get(key, 0)) > .00001}
            events.append({
                "id": identifier, "at": iso(at), "recorded_at": iso(row["committed"]),
                "kind": "appraisal", "title": event["topic"] if visible else "其他范围的情绪事件",
                "reason": event["interpretation"] if visible else "原因不在当前可见范围内",
                "source_refs": [row["source"], *event["context_source_ids"]] if visible else [],
                "confidence": event["confidence"], "authority": "subjective_interpretation",
                "values": points[-1]["values"], "changes": changes, "mode": settings.mode,
            })
        while cursor <= end:
            if mood_at is not None:
                points.append(point(cursor))
            cursor += 300
        if mood_at is not None and end >= start:
            points.append(point(end))
        return {"available": bool(points), "points": points, "events": events,
                "mode": settings.mode, "basis": "reconstructed_from_current_valid_appraisals"}

    def work(self, db, start, end):
        receipts = db.execute("""SELECT count(*) FROM agent_compaction_receipts_v1
            WHERE committed>=? AND committed<?""", (start, end)).fetchone()[0]
        counts = dict(db.execute("""SELECT role,count(*) FROM (
            SELECT DISTINCT e.conversation,e.seq,e.role FROM agent_compaction_receipts_v1 c
            JOIN agent_events_v2 e ON e.conversation=c.conversation AND e.seq BETWEEN c.first_seq AND c.last_seq
            WHERE c.committed>=? AND c.committed<?) GROUP BY role""", (start, end)))
        unknown = db.execute("""SELECT count(*) FROM agent_compactions_v2 c WHERE NOT EXISTS(
            SELECT 1 FROM agent_compaction_receipts_v1 r WHERE r.conversation=c.conversation
              AND r.first_seq=c.first_seq AND r.last_seq=c.last_seq)""").fetchone()[0]
        failures = []
        rows = db.execute("""SELECT t.seq,t.job,t.at,t.status,t.detail,j.kind,j.status AS current_status
            FROM agent_job_transitions_v1 t JOIN agent_jobs_v2 j ON j.id=t.job
            WHERE t.at>=? AND t.at<? AND t.status IN ('failed','retrying','rejected') ORDER BY t.at DESC,t.seq DESC""",
            (start, end))
        for row in rows:
            detail = json.loads(row["detail"])
            failures.append({
                "id": "job-transition:" + str(row["seq"]), "job_id": row["job"], "at": iso(row["at"]),
                "component": row["kind"], "status": row["status"], "current_status": row["current_status"],
                "reason": str(detail.get("error") or detail.get("reason") or row["status"])[:160],
                "validation": detail.get("validation"), "count": 1,
            })
        legacy = []
        for row in db.execute("""SELECT j.id,j.kind,j.result FROM agent_jobs_v2 j
            WHERE j.status='failed' AND NOT EXISTS(SELECT 1 FROM agent_job_transitions_v1 t WHERE t.job=j.id)"""):
            detail = json.loads(row["result"] or "{}")
            legacy.append({"id": row["id"], "at": None, "component": row["kind"], "status": "failed",
                "reason": str(detail.get("error") or "unknown")[:160], "time_basis": "not_recorded"})
        return {"available": True, "since": iso(start), "until": iso(end),
                "compaction": {"operations": receipts, "turns": counts.get("user", 0), "messages": sum(counts.values()),
                               "turn_definition": "distinct_user_events", "legacy_undated_operations": unknown},
                "failures": failures, "legacy_undated_failures": legacy}
