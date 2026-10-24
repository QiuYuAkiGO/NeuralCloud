"""Daily-report display projections; no inference and no state mutation."""
from __future__ import annotations

from datetime import datetime
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3


def operation_counts(capabilities, board, goals, start, end):
    """Read minimal operator summaries using independent, read-only handles."""
    def records(owner, attribute, table, fields, time_field):
        store = getattr(owner, attribute, None)
        if store is None:
            return False, []
        if not hasattr(store, "path"):  # In-memory adapters used by local callers.
            return True, store.all()
        projections = ",".join(f"'{key}',json_extract(record_json,'$.{source}')" for key, source in fields.items())
        query = f"""SELECT json_object({projections}) FROM {table}
            WHERE julianday(json_extract(record_json,'$.{time_field}'))>=julianday(?)
            AND julianday(json_extract(record_json,'$.{time_field}'))<julianday(?)"""
        with closing(sqlite3.connect(Path(store.path).resolve().as_uri()+"?mode=ro", uri=True)) as db:
            return True, [json.loads(row[0]) for row in db.execute(query, (start.isoformat(), end.isoformat()))]
    call_ok, calls = records(capabilities, "calls", "calls", {
        "created_at": "created_at", "status": "status", "error_code": "error_code",
        "channel": "offer.channel", "goal_binding": "goal_binding",
        "decision": "decision"}, "created_at")
    board_ok, turns = records(board, "turns", "turns", {
        "requested_at": "requested_at", "status": "status"}, "requested_at")
    episodes = None
    objects = getattr(getattr(goals, "store", None), "objects", None)
    if objects is not None:
        with closing(sqlite3.connect(Path(objects.path).resolve().as_uri()+"?mode=ro", uri=True)) as db:
            episodes = db.execute("SELECT count(*) FROM agency_objects WHERE kind='regulation_episode'").fetchone()[0]
    return {"capabilities_available": call_ok, "calls": calls, "board_available": board_ok,
            "turns": turns, "episode_count": episodes}


def moment(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def numeric(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def timeline(traces, affect, events):
    points, markers = [], {}
    for trace in sorted(traces, key=lambda row: row.get("occurred_at", "")):
        at = trace.get("occurred_at")
        if moment(at) is None:
            continue
        identifier = "body:" + str(trace.get("revision") or trace.get("event_id") or len(points))
        before = {key: value for key, value in (trace.get("values_before") or {}).items() if numeric(value)}
        after = {key: value for key, value in (trace.get("values_after") or {}).items() if numeric(value)}
        if before:
            points.append({"at": at, "values": before})
        if after:
            points.append({"at": at, "values": after, "event_id": identifier})
        changes = {key: round(value - before[key], 6) for key, value in after.items()
                   if key in before and abs(value - before[key]) > .000001}
        if changes and (trace.get("command_type") != "ApplyHeartbeat" or trace.get("salience") in {"marked", "critical"}):
            markers[identifier] = {
                "id": identifier, "at": at, "kind": "homeostasis",
                "title": trace.get("command_type") or "内稳态更新",
                "reason": trace.get("source") or "未记录来源",
                "source_refs": list(dict.fromkeys([ref for ref in
                    [trace.get("event_id"), *(trace.get("evidence_refs") or [])] if ref])),
                "changes": changes, "values": after, "authority": "recorded_transition",
            }
    for event in affect.get("events") or []:
        markers[event["id"]] = event
    # A Runtime receipt is useful context, but is never asserted to have caused
    # a nearby emotional change without an explicit evidence link.
    for event in events:
        if event.get("topic") != "event.completed":
            continue
        payload = event.get("payload") or {}
        if payload.get("event_type") in {None, "heartbeat", "idle"}:
            continue
        identifier = "runtime:" + str(event.get("event_id") or event["seq"])
        markers[identifier] = {
            "id": identifier, "at": event["observed_at"], "kind": "event",
            "title": str(payload.get("event_type")), "reason": str(payload.get("status") or "completed"),
            "source_refs": [event["event_id"]] if event.get("event_id") else [],
            "authority": "event_receipt_not_causal_inference",
        }
    return {"available": bool(points or affect.get("points")), "homeostasis": points,
            "affect": affect.get("points") or [], "mode": affect.get("mode"),
            "events": sorted(markers.values(), key=lambda row: row["at"]),
            "homeostasis_basis": "recorded_values_minute_extrema_and_event_transitions",
            "affect_basis": affect.get("basis")}


def failure_events(events):
    grouped = {}
    for event in events:
        payload = event.get("payload") or {}
        detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
        if not (event.get("severity") == "error" or payload.get("status") == "failed"
                or str(event.get("topic", "")).endswith("_failed")):
            continue
        error = detail.get("error_type") or detail.get("error_code") or payload.get("error_type") or payload.get("error_code")
        # Arbitrary exception messages can contain prompts or credentials.
        error = str(error or "failure_detail_not_recorded")[:160]
        event_id = event.get("event_id") or payload.get("event_id")
        identity = (event_id or event.get("run_id") or event["seq"], error)
        if identity in grouped:
            continue
        grouped[identity] = {
            "id": "monitor:" + str(event["seq"]), "at": event["observed_at"],
            "component": str(event.get("topic")), "reason": error,
            "status": "failed", "current_status": "unknown", "count": 1, "event_id": event_id,
        }
    return sorted(grouped.values(), key=lambda row: row["at"], reverse=True)


def run_health(samples, started_at, as_of, status):
    if started_at is None or started_at > as_of:
        return {"available": False}
    stable = last = None
    interruptions = 0
    for sample in samples:
        at = moment(sample.get("observed_at"))
        if at is None:
            continue
        if last is not None and at - last > 30:
            stable = None
            interruptions += 1
        if sample.get("lifecycle") != "RUNNING" or not sample.get("healthy"):
            stable = None
        elif stable is None:
            stable = at
        last = at
    lifecycle = getattr(getattr(status, "lifecycle", None), "value", getattr(status, "lifecycle", None))
    health = getattr(status, "health", None)
    if status is None and samples:
        lifecycle = samples[-1].get("lifecycle")
    if lifecycle != "RUNNING" or getattr(health, "degraded_reasons", []):
        stable = None
    fresh = last is not None and as_of.timestamp() - last <= 30
    return {
        "available": True, "instance_id": getattr(status, "runtime_instance_id", None),
        "started_at": started_at.isoformat(), "as_of": as_of.isoformat(), "lifecycle": lifecycle,
        "uptime_seconds": max(0, (as_of - started_at).total_seconds()),
        "stable_seconds": max(0, last - stable) if stable is not None and fresh else None,
        "stable_since": datetime.fromtimestamp(stable, started_at.tzinfo).isoformat() if stable is not None and fresh else None,
        "sample_gap_count": interruptions, "sample_count": len(samples), "fresh": fresh,
        "coverage_started_at": samples[0]["observed_at"] if samples else None,
        "stability_basis": "continuous_running_healthy_samples_max_gap_30_seconds",
    }
