"""Read-only source monitoring for one uninterrupted eight-hour QQ acceptance run.

``start --config PRIVATE.json`` samples in the foreground. It writes only its
new private run directory; it never starts, controls or delivers through QQ.
``status --run-dir ...`` and ``report --run-dir ...`` export content-free results.
This is local evidence correlation, not protection against forged logs or
unrecorded human interaction with the desktop.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


DURATION_SECONDS = 8 * 60 * 60
TARGETS = {"warm_speak": 40, "cold_speak": 5, "natural_wait": 10}
_EXCLUDED_TERMINALS = {"turn_batched", "turn_guard_wait", "turn_superseded", "turn_rate_limited"}
_TERMINALS = {"turn_sent", "turn_natural_wait", "turn_failed", "turn_send_disabled"} | _EXCLUDED_TERMINALS


class AcceptanceError(ValueError):
    """A fixed, content-free input/evidence error."""


class WorkerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pid: int = Field(gt=0)
    started_at: AwareDatetime


class AcceptanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_dir: Path
    watcher_status_path: Path
    trace_file: Path
    ledger_db: Path
    memory_db: Path
    worker_identity: WorkerIdentity
    build_hash: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{6,159}$")
    sample_interval_seconds: float = Field(default=15, gt=0, le=15)
    max_heartbeat_age_seconds: float = Field(default=30, gt=0, le=30)
    max_observation_gap_seconds: float = Field(default=10, gt=0, le=10)

    @field_validator("run_dir", "watcher_status_path", "trace_file", "ledger_db", "memory_db")
    @classmethod
    def absolute_path(cls, value):
        if not value.is_absolute() or value.is_symlink():
            raise ValueError("Choose absolute private paths without symlink roots.")
        return value


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise AcceptanceError("timestamp_not_aware")
    return value.astimezone(timezone.utc)


def _date(value):
    try:
        return _utc(datetime.fromisoformat(value))
    except (ValueError, TypeError, AttributeError):
        raise AcceptanceError("invalid_evidence_timestamp") from None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _opaque(value):
    return "id:" + _digest(str(value))[:24]


def _json(path):
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, ValueError):
        raise AcceptanceError("json_source_unavailable") from None


def _write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _connect(path):
    if not path.is_file() or path.is_symlink():
        raise AcceptanceError("sqlite_source_unavailable")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _records(connection, table, identifier, group, *, state=None):
    # SQL identifiers are internal constants, never supplied by the configuration.
    result = {}
    for row in connection.execute(f"SELECT * FROM {table} WHERE group_id=?", (group,)):
        value = json.loads(row["record_json"])
        if (value.get(identifier) != row[identifier] or value.get("group_id") != group
                or (state and value.get(state) != row[state])):
            raise AcceptanceError("sqlite_index_record_mismatch")
        if table == "qq_reply_decisions":
            value["_stored_status"] = row["status"]
        result[row[identifier]] = value
    return result


def _sources(config):
    try:
        with closing(_connect(config.ledger_db)) as ledger, closing(_connect(config.memory_db)) as memory:
            group = ledger.execute("SELECT group_id FROM qq_watch_state WHERE singleton=1").fetchone()[0]
            result = {"group": group,
                      "events": _records(ledger, "qq_watch_events", "event_id", group, state="state"),
                      "sends": _records(ledger, "qq_watch_sends", "reservation_id", group, state="status"),
                      "receipts": _records(memory, "qq_reply_deliveries", "receipt_id", group),
                      "decisions": _records(memory, "qq_reply_decisions", "decision_id", group),
                      "repairs": {}, "attempts": {}}
            for row in ledger.execute("SELECT a.record_json FROM qq_watch_delivery_attempts a JOIN qq_watch_sends s ON s.reservation_id=a.reservation_id WHERE s.group_id=?", (group,)):
                attempt = json.loads(row[0])
                result["attempts"].setdefault(attempt["reservation_id"], []).append(attempt)
            for prefix, connection in (("ledger", ledger), ("memory", memory)):
                present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='qq_operator_delivery_repairs'").fetchone()
                if present:
                    for row in connection.execute("SELECT * FROM qq_operator_delivery_repairs"):
                        result["repairs"][prefix + ":" + row["repair_id"]] = _digest(dict(row))
            return result
    except (sqlite3.Error, ValueError, TypeError, IndexError, KeyError):
        raise AcceptanceError("sqlite_evidence_unavailable") from None


def _traces(path):
    if not path.exists():
        return {}, []
    if path.is_symlink() or path.stat().st_size > 128 * 1024 * 1024:
        raise AcceptanceError("trace_source_invalid")
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    result, problems = {}, []
    for line in lines:
        if not line.endswith(b"\n"):
            continue  # An append in progress is reconsidered on the next sample.
        try:
            trace = json.loads(line)
            identifier = trace["trace_id"]
            if trace["schema_version"] != "qq-turn-trace-v1" or not isinstance(identifier, str):
                raise ValueError
            if not isinstance(trace.get("preparation_counters", {}), dict):
                raise ValueError
            previous = result.get(identifier)
            if previous:
                stable = ("event_id", "first_seen_at", "first_seen_mono")
                if (any(previous[key] != trace[key] for key in stable)
                        or previous.get("observation_sequence") != trace.get("observation_sequence")
                        or previous["elapsed_ms"] > trace["elapsed_ms"]
                        or trace["stages"][:len(previous["stages"])] != previous["stages"]
                        or (previous.get("decision_id") and previous["decision_id"] != trace.get("decision_id"))
                        or any(trace.get("preparation_counters", {}).get(key) != value
                               for key, value in previous.get("preparation_counters", {}).items())
                        or any(trace["counters"].get(key, 0) < count for key, count in previous["counters"].items())):
                    problems.append("trace_identity_or_time_regressed")
            result[identifier] = trace
        except (ValueError, KeyError, TypeError):
            problems.append("trace_record_invalid")
    return result, problems


def _inventory(sources):
    return {key: {identifier: _digest(record) for identifier, record in sources[key].items()}
            for key in ("events", "sends", "receipts")} | {"repairs": sources["repairs"]}


def _preparation_records(config, manifest, *, moment, complete=False):
    """Classify independent attempts, without inventing a candidate or a retry link."""
    traces, problems = _traces(config.trace_file.with_suffix(".observation.jsonl"))
    reasons = {"preparation_" + problem for problem in problems}
    baseline = set(manifest.get("baseline_preparation_trace_ids", []))
    if not baseline.issubset(traces):
        reasons.add("preparation_baseline_removed")
    sample_path = config.run_dir / "samples.jsonl"
    if sample_path.exists():
        try:
            previous = json.loads(sample_path.read_text(encoding="utf-8").splitlines()[-1])
            known = previous.get("metrics", {}).get("preparation_trace_ids", [])
            if not isinstance(known, list) or any(not isinstance(value, str) for value in known):
                raise ValueError
            if not set(known).issubset({_opaque(identifier) for identifier in traces}):
                reasons.add("preparation_records_removed")
        except (ValueError, KeyError, IndexError, TypeError):
            reasons.add("preparation_inventory_unavailable")
    entries = []
    for identifier, trace in traces.items():
        if identifier in baseline:
            continue
        try:
            if (trace.get("event_id") is not None or trace.get("decision_id") is not None
                    or trace.get("preparation_counters")
                    or any(name.startswith("turn_") for name in trace.get("counters", {}))):
                raise AcceptanceError("preparation_trace_invalid")
            sequence = trace.get("observation_sequence")
            if sequence is not None and (type(sequence) is not int or sequence < 0):
                raise AcceptanceError("preparation_observation_identity_invalid")
            first = _date(trace["first_seen_at"])
            first_offset = (first - _date(manifest["started_at"])).total_seconds()
            if (not math.isfinite(trace["first_seen_mono"])
                    or abs(trace["first_seen_mono"] - manifest["started_mono"] - first_offset) > 5):
                raise AcceptanceError("preparation_monotonic_mismatch")
            stages = trace.get("stages", [])
            if not isinstance(stages, list) or any(not isinstance(stage, dict) for stage in stages):
                raise AcceptanceError("preparation_stage_invalid")
            _add_workload(_independent_workload(trace), {}, {}, {})
            if trace["counters"].get("events_dropped", 0):
                raise AcceptanceError("preparation_stages_dropped")
            terminal = [stage for stage in stages if stage.get("name") == "observation_completed"]
            if len(terminal) > 1:
                raise AcceptanceError("preparation_terminal_invalid")
            finish = terminal[0]["end_offset_ms"] / 1000 if terminal else trace["elapsed_ms"] / 1000
            start = min((stage["start_offset_ms"] for stage in stages), default=0) / 1000
            if (not all(math.isfinite(number) and number >= 0 for number in (start, finish)) or start > finish
                    or finish > trace["elapsed_ms"] / 1000
                    or (terminal and terminal[0] is not stages[-1])):
                raise AcceptanceError("preparation_timing_invalid")
            if first_offset + finish < 0 or first_offset + start > DURATION_SECONDS:
                continue
            outcome = terminal[0].get("outcome") if terminal else "pending"
            if outcome not in {"ok", "error", "expired", "cancelled", "late_result", "pending"}:
                raise AcceptanceError("preparation_terminal_invalid")
            entries.append({"trace": trace, "key": (sequence, first.isoformat(), trace["first_seen_mono"]) if sequence is not None else None,
                            "first": first, "finish": finish, "finish_offset": first_offset + finish,
                            "outcome": outcome, "complete": bool(terminal)})
        except (AcceptanceError, KeyError, ValueError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, AcceptanceError) else "preparation_evidence_invalid"
            reasons.add(reason)
    successful = {}
    for entry in entries:
        if entry["outcome"] == "ok" and entry["finish"] <= 60 and entry["finish_offset"] <= DURATION_SECONDS:
            successful.setdefault(entry["key"], []).append(entry)
    for entry in entries:
        trace, key, outcome = entry["trace"], entry["key"], entry["outcome"]
        recovery = next((later for later in successful.get(key, []) if key is not None and later["finish"] >= entry["finish"]), None)
        counters = trace["counters"]
        expired = counters.get("expired_observations", 0) > 0 or counters.get("expired_carry_origins", 0) > 0
        if counters.get("unresolved_observation_baselines", 0) > 0:
            classification = "lost_observation"
            reasons.add("preparation_observation_lost")
        elif expired:
            classification = "expired"
            reasons.add("preparation_expired")
        elif outcome == "ok":
            classification = "successful"
        elif outcome == "pending":
            classification = "pending"
            if complete or (moment - entry["first"]).total_seconds() > 60:
                reasons.add("preparation_unterminated")
        elif recovery is not None:
            classification = "recovered_within_deadline"
        elif key is None:
            classification = "unlinked_failure"
        else:
            classification = "unrecovered_failure"
            if complete or (moment - entry["first"]).total_seconds() > 60:
                reasons.add("preparation_unrecovered")
        if complete and entry["finish_offset"] > DURATION_SECONDS:
            reasons.add("preparation_completed_outside_run_window")
        entry["public"] = {"trace_id": _opaque(trace["trace_id"]), "outcome": outcome,
                           "classification": classification, "elapsed_seconds": entry["finish"],
                           "observation_link_available": key is not None}
        if classification == "recovered_within_deadline":
            entry["public"]["recovery_trace_id"] = _opaque(recovery["trace"]["trace_id"])
    return entries, reasons


def _gap_inventory(observer):
    """Validate the bounded closed-gap stream independently of current health."""
    try:
        sequence, count = observer["gap_sequence"], observer["completed_gap_count"]
        total, maximum = observer["total_gap_seconds"], observer["max_gap_seconds"]
        recent = observer["recent_gaps"]
        if (type(sequence) is not int or type(count) is not int or sequence != count or sequence < 0
                or any(isinstance(number, bool) or not isinstance(number, (int, float))
                       or not math.isfinite(number) or number < 0 for number in (total, maximum))
                or not isinstance(recent, list) or len(recent) > min(32, sequence)):
            raise ValueError
        expected = max(1, sequence - len(recent) + 1)
        if (sequence == 0) != (len(recent) == 0):
            raise ValueError
        normalized = []
        for gap in recent:
            duration = gap["duration_seconds"]
            if (type(gap["sequence"]) is not int or gap["sequence"] != expected
                    or isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or not math.isfinite(duration) or duration < 0 or duration > maximum + 1e-6
                    or not isinstance(gap["reason"], str) or not re.fullmatch(r"[a-z_]{1,80}", gap["reason"])):
                raise ValueError
            start, finish = _date(gap["started_at"]), _date(gap["finished_at"])
            if finish < start:
                raise ValueError
            normalized.append({"sequence": expected, "started_at": start.isoformat(),
                               "finished_at": finish.isoformat(), "duration_seconds": duration,
                               "reason": gap["reason"]})
            expected += 1
        if sum(gap["duration_seconds"] for gap in normalized) > total + 1e-5:
            raise ValueError
        return {"sequence": sequence, "count": count, "total_seconds": total, "max_seconds": maximum}, normalized
    except (ValueError, TypeError, KeyError):
        raise AcceptanceError("observer_gap_evidence_invalid") from None


def _consume_gaps(config, manifest, observer):
    current, recent = _gap_inventory(observer)
    path = config.run_dir / "samples.jsonl"
    previous = manifest["observer_gap_baseline"]
    if path.exists():
        try:
            previous_sample = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
            previous = previous_sample["metrics"]["gap_cursor"]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            raise AcceptanceError("observer_gap_cursor_unavailable") from None
    reasons = []
    if any(current[key] + 1e-6 < previous[key] for key in current):
        reasons.append("observer_gap_counter_regressed")
    new = [gap for gap in recent if gap["sequence"] > previous["sequence"]]
    if current["sequence"] > previous["sequence"] and (not new or new[0]["sequence"] != previous["sequence"] + 1):
        reasons.append("observer_gap_history_overflow")
    maximum = max((gap["duration_seconds"] for gap in new), default=0)
    if maximum > config.max_observation_gap_seconds:
        reasons.append("completed_observation_gap")
    return reasons, {"gap_cursor": current, "closed_gaps": new,
                     "max_completed_gap_seconds": maximum}


def _health(config, value, moment, group, *, allow_transient=False, preparation_recovery=False):
    reasons = []
    try:
        if (value["pid"] != config.worker_identity.pid
                or _date(value["started_at"]) != config.worker_identity.started_at):
            reasons.append("worker_identity_changed")
        if value.get("build_hash") != config.build_hash:
            reasons.append("worker_build_changed")
        if value.get("group_id") != group:
            reasons.append("worker_group_changed")
        observer = value["observer"]
        poll_age = (moment - _date(observer["last_polled_at"])).total_seconds()
        gap = float(observer.get("gap_seconds", 0))
        if not math.isfinite(gap) or gap < 0:
            raise ValueError
        bounded_gap = (allow_transient and observer.get("running") is True
                       and -2 <= poll_age <= config.max_observation_gap_seconds
                       and observer.get("gap_started_at") is not None
                       and gap <= config.max_observation_gap_seconds)
        observation_state = value.get("state") in {"waiting_for_observer", "observer_unavailable"}
        transient_flags = (bounded_gap and value.get("state") not in {"paused", "stopped", "stopping"}
                           and (observation_state or observer.get("evidence_in_progress") is True
                                or observer.get("pending_digest") is not None))
        preparation_recovery = (allow_transient and preparation_recovery
                                and value.get("state") == "retrying_capture"
                                and observer.get("running") is True
                                and not observer.get("last_error_type")
                                and -2 <= poll_age <= config.max_observation_gap_seconds
                                and gap <= config.max_observation_gap_seconds)
        recovery_state = value.get("state") in {"retrying_precheck", "reconciling_delivery"} or preparation_recovery
        for key in ("running", "automatic_screen_capture", "automatic_send"):
            if value.get(key) is not True:
                transient = key != "running" and (transient_flags or preparation_recovery) and value.get(key) is False
                reconcile = key == "automatic_send" and value.get("state") == "reconciling_delivery" and value.get(key) is False
                if not transient and not reconcile:
                    reasons.append("worker_flag_disabled")
        age = (moment - _date(value["heartbeat_at"])).total_seconds()
        if not -2 <= age <= config.max_heartbeat_age_seconds:
            reasons.append("heartbeat_gap")
        if value.get("last_error_type") and not recovery_state and not (observation_state and bounded_gap):
            reasons.append("worker_error")
        if value.get("human_intervention"):
            reasons.append("human_intervention")
        if value.get("preparation_trace_error"):
            reasons.append("preparation_trace_unavailable")
        if observer.get("running") is not True or not -2 <= poll_age <= config.max_observation_gap_seconds:
            reasons.append("observation_gap")
        if observer.get("last_error_type") and not bounded_gap:
            reasons.append("observer_error")
        if observer.get("overflows", 0):
            reasons.append("observer_overflow")
        if gap > config.max_observation_gap_seconds:
            reasons.append("occlusion_observation_gap")
        return reasons, {"heartbeat_age_seconds": max(0, age), "poll_age_seconds": max(0, poll_age),
                         "occlusion_gap_seconds": gap, "polls": int(observer["polls"])}
    except (ValueError, KeyError, TypeError):
        return [*reasons, "status_evidence_invalid"], {}


def initialize_run(config: AcceptanceConfig, *, now=None, monotonic=None):
    """Create one private manifest; no prior run directory may be reused."""
    moment, mono = _utc(now), time.monotonic() if monotonic is None else monotonic
    sources = _sources(config)
    status = _json(config.watcher_status_path)
    reasons, _ = _health(config, status, moment, sources["group"])
    gap_baseline, _ = _gap_inventory(status["observer"])
    if any(send["status"] in {"reserved", "retry_wait", "click_started", "uncertain"} for send in sources["sends"].values()):
        reasons.append("preexisting_unsettled_send")
    traces, errors = _traces(config.trace_file)
    preparations, preparation_errors = _traces(config.trace_file.with_suffix(".observation.jsonl"))
    if reasons or errors or preparation_errors:
        raise AcceptanceError("preflight_evidence_not_ready")
    if not config.run_dir.parent.is_dir() or config.run_dir.exists():
        raise AcceptanceError("new_private_run_directory_required")
    config.run_dir.mkdir(mode=0o700)
    manifest = {"schema_version": "qq-acceptance-manifest-v1", "run_id": "qq-acceptance:" + uuid4().hex,
                "started_at": moment.isoformat(), "started_mono": mono, "monitor_pid": os.getpid(),
                "duration_seconds": DURATION_SECONDS, "targets": TARGETS, "config": config.model_dump(mode="json"),
                "observer_gap_baseline": gap_baseline,
                "baseline": _inventory(sources), "baseline_trace_ids": list(traces),
                "baseline_preparation_trace_ids": list(preparations)}
    _write(config.run_dir / "manifest.json", manifest)
    sample_run(config.run_dir, now=moment, monotonic=mono)
    return manifest


def _load(run_dir):
    directory = Path(run_dir)
    manifest = _json(directory / "manifest.json")
    config = AcceptanceConfig.model_validate(manifest["config"])
    if config.run_dir != directory or manifest["duration_seconds"] != DURATION_SECONDS or manifest["targets"] != TARGETS:
        raise AcceptanceError("manifest_contract_changed")
    return manifest, config


def sample_run(run_dir, *, now=None, monotonic=None):
    """Append a redacted observation; only the original monitor process samples."""
    manifest, config = _load(run_dir)
    if manifest["monitor_pid"] != os.getpid():
        raise AcceptanceError("monitor_process_changed")
    moment, mono = _utc(now), time.monotonic() if monotonic is None else monotonic
    reasons, metrics = [], {}
    sample = {"sampled_at": moment.isoformat(), "elapsed_seconds": mono - manifest["started_mono"]}
    try:
        sources = _sources(config)
        status = _json(config.watcher_status_path)
        preparations, preparation_errors = _preparation_records(config, manifest, moment=moment)
        active_preparation = status.get("preparation") or {}
        preparation_recovery = any(
            entry["key"] is not None and entry["trace"]["trace_id"] == active_preparation.get("trace_id")
            and entry["public"]["classification"] in {"unrecovered_failure", "recovered_within_deadline"}
            and 0 <= (moment - entry["first"]).total_seconds() <= 60
            for entry in preparations)
        reasons, metrics = _health(config, status, moment, sources["group"], allow_transient=True,
                                   preparation_recovery=preparation_recovery and not preparation_errors)
        reasons.extend(preparation_errors)
        gap_reasons, gap_metrics = _consume_gaps(config, manifest, status["observer"])
        reasons.extend(gap_reasons)
        metrics.update(gap_metrics)
        current = _inventory(sources)
        if current["repairs"] != manifest["baseline"]["repairs"]:
            reasons.append("manual_repair_changed")
        for key in ("events", "sends", "receipts"):
            if not set(manifest["baseline"][key]).issubset(current[key]):
                reasons.append("baseline_records_removed")
        uncertain = [send for send in sources["sends"].values() if send["status"] == "uncertain"]
        metrics["reconciling_sends"] = len(uncertain)
        if uncertain and (status.get("state") != "reconciling_delivery" or any(
                not 0 <= (moment - _date(send["updated_at"])).total_seconds() <= 30 for send in uncertain)):
            reasons.append("unresolved_uncertain_send")
        new_events = [event for key, event in sources["events"].items() if key not in manifest["baseline"]["events"]]
        if any(event["state"] in {"failed", "expired"} for event in new_events):
            reasons.append("event_failed_or_expired")
        sample["counts"] = {key: len(value) for key, value in current.items()}
        _, trace_errors = _traces(config.trace_file)
        reasons.extend(trace_errors)
        metrics["preparation_outcomes"] = {kind: sum(entry["public"]["classification"] == kind for entry in preparations)
                                            for kind in sorted({entry["public"]["classification"] for entry in preparations})}
        metrics["preparation_trace_ids"] = sorted(entry["public"]["trace_id"] for entry in preparations)
    except (AcceptanceError, OSError):
        reasons.append("source_read_failed")
    sample.update(metrics=metrics, reasons=sorted(set(reasons)))
    path = config.run_dir / "samples.jsonl"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(sample, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return sample


def _percentile(values):
    return sorted(values)[math.ceil(len(values) * 0.95) - 1] if values else None


def _distribution(values):
    ordered = sorted(values)
    return {"count": len(values), "p50": ordered[math.ceil(len(values) * .5) - 1] if values else None,
            "p95": _percentile(values), "max": max(values) if values else None}


_WORK_COUNTERS = {"model_calls", "gate_model_calls", "persona_model_calls", "failed_model_calls",
                  "precheck_retries", "attribution_retries", "receipt_recoveries",
                  "static_parse_cache_hit", "static_parse_cache_miss", "body_capture", "vision_calls",
                  "accessibility_projection", "accessibility_pair_retry", "accessibility_fallback",
                  "fresh_frame_reuse", "profile_transition_retries", "preparation_attempts", "expired_observations",
                  "expired_carry_origins", "unresolved_observation_baselines"}
_WORK_STAGES = {"gate", "persona", "gate.model_call", "persona.model_call", "decision", "vision",
                "body_capture", "accessibility_projection", "capture_and_group_verification", "attribution",
                "memory_storage", "delivery", "receipt_storage", "retry_relocation", "reconciliation",
                "owned_draft_cleanup", "capture", "group_verification", "self_verification",
                "initial_capture", "initial_self_verification", "observer_gap_recovery"}


def _add_workload(trace, stages, totals, preparations):
    """Count shared physical preparation once, retaining each event's own work."""
    imported = trace.get("preparation_counters", {})
    if not isinstance(imported, dict) or len(imported) > 128:
        raise AcceptanceError("trace_preparation_invalid")
    for source, counts in imported.items():
        if (not isinstance(source, str) or not re.fullmatch(r"qq-trace:[0-9a-f]{32}", source)
                or not isinstance(counts, dict) or len(counts) > 128
                or any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name)
                       or type(count) is not int or count < 0 for name, count in counts.items())):
            raise AcceptanceError("trace_preparation_invalid")
    source_stages = {source: [] for source in imported}
    event_stages = []
    for stage in trace.get("stages", []):
        source = stage.get("source_preparation_id")
        if source is None:
            event_stages.append(stage)
            continue
        if source not in imported:
            raise AcceptanceError("trace_preparation_unlinked")
        index = stage.get("source_stage_index")
        start, end, duration = (stage.get(key) for key in ("source_start_offset_ms", "source_end_offset_ms", "duration_ms"))
        if (type(index) is not int or index != len(source_stages[source])
                or any(isinstance(number, bool) or not isinstance(number, (int, float))
                       or not math.isfinite(number) or number < 0 for number in (start, end, duration))
                or start > end or abs(end - start - duration) > .002):
            raise AcceptanceError("trace_preparation_stage_invalid")
        source_stages[source].append({key: stage.get(key) for key in (
            "source_stage_index", "name", "outcome", "error_type", "duration_ms",
            "source_start_offset_ms", "source_end_offset_ms")})
    additions, source_fingerprints = {}, {}
    for source, counts in imported.items():
        fingerprint = _digest({"counters": counts, "stages": source_stages[source]})
        if source in preparations and preparations[source] != fingerprint:
            raise AcceptanceError("trace_preparation_changed")
        source_fingerprints[source] = fingerprint
        if source not in preparations:
            event_stages.extend(source_stages[source])
    for name in _WORK_COUNTERS:
        count = trace.get("counters", {}).get(name, 0)
        if type(count) is not int or count < 0:
            raise AcceptanceError("trace_work_counter_invalid")
        own_count = count - sum(counts.get(name, 0) for counts in imported.values())
        if own_count < 0:
            raise AcceptanceError("trace_preparation_counter_exceeds_total")
        additions[name] = own_count + sum(counts.get(name, 0) for source, counts in imported.items()
                                         if source not in preparations)
    durations = []
    for stage in event_stages:
        if stage.get("name") not in _WORK_STAGES or "duration_ms" not in stage:
            continue
        duration = stage["duration_ms"]
        if isinstance(duration, bool) or not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration < 0:
            raise AcceptanceError("trace_stage_duration_invalid")
        durations.append((stage["name"], duration / 1000))
    # Validation is complete before any global aggregate changes.
    preparations.update(source_fingerprints)
    for name, count in additions.items():
        totals[name] = totals.get(name, 0) + count
    for name, duration in durations:
        stages.setdefault(name, []).append(duration)


def _independent_workload(trace):
    """Use the identical source fingerprint for raw and event-imported work."""
    source = trace["trace_id"]
    return {"counters": trace["counters"], "preparation_counters": {source: trace["counters"]},
            "stages": [dict(stage, source_preparation_id=source, source_stage_index=index,
                            source_start_offset_ms=stage["start_offset_ms"],
                            source_end_offset_ms=stage["end_offset_ms"])
                       for index, stage in enumerate(trace["stages"])]}


def _link_delivery(trace, event, sources):
    """Require exact automatic intent / body / receipt / proof correspondence."""
    decision = sources["decisions"].get(trace.get("decision_id"))
    sends = [send for send in sources["sends"].values() if send["event_id"] == event["event_id"]]
    receipts = [receipt for receipt in sources["receipts"].values() if receipt.get("decision_id") == trace.get("decision_id")]
    if not decision or len(sends) != 1 or len(receipts) != 1:
        raise AcceptanceError("delivery_chain_not_unique")
    send, receipt = sends[0], receipts[0]
    proof = send.get("preclick_proof") or {}
    attempts = sources["attempts"].get(send["reservation_id"], [])
    reply_hash = hashlib.sha256(str(decision.get("reply", "")).encode("utf-8")).hexdigest()
    if (send["status"] != "sent" or event["state"] != "sent"
            or send["decision_id"] != decision["decision_id"] or event.get("decision_id") != decision["decision_id"]
            or decision.get("action") != "speak" or decision.get("decision_source") != "model"
            or decision.get("outcome") != "ready" or decision.get("error_type")
            or decision.get("_stored_status") != "completed"
            or receipt.get("origin") != "decision" or receipt.get("verification") != "qq_ui_verified"
            or send.get("receipt_id") not in (None, receipt["receipt_id"])
            or decision.get("reply") != receipt.get("delivered_text")
            or reply_hash != send.get("reply_sha256") or reply_hash != proof.get("reply_sha256")
            or send.get("self_member_id") != receipt.get("self_member_id")
            or proof.get("self_member_id") != receipt.get("self_member_id")
            or proof.get("group_id") != event["group_id"]
            or proof.get("reservation_id") not in (None, send["reservation_id"])
            or not event.get("source_record_id") or event["source_record_id"] not in receipt.get("source_record_ids", [])
            or not set(receipt.get("source_record_ids", [])).issubset(decision.get("source_record_ids", []))
            or not receipt.get("evidence_refs") or sorted(set(receipt["evidence_refs"])) != sorted(set(send.get("evidence_refs", [])))
            or not proof.get("frame_evidence_ref") or not proof.get("context_signature")
            or proof.get("before_outgoing_count") is None
            or not proof.get("window_id") or not proof.get("body_instance_id")
            or not proof.get("environment_revision") or not proof.get("title_pixels_sha256")
            or not 1 <= send.get("attempt_count", 0) <= 3
            or len(attempts) != send.get("attempt_count")
            or len([attempt for attempt in attempts if attempt["phase"] == "sent" and attempt.get("proof") == proof]) != 1
            or any(attempt["phase"] not in {"sent", "precheck_failed"} for attempt in attempts)):
        raise AcceptanceError("delivery_chain_mismatch")
    acknowledged, observed = send.get("click_acknowledged_at"), send.get("click_observed_at")
    if send.get("click_timing_required") is not True or (acknowledged is None) == (observed is None):
        raise AcceptanceError("actual_click_timing_missing")
    if observed is not None and (not send.get("click_observed_evidence_ref")
                                or send["click_observed_evidence_ref"] not in receipt["evidence_refs"]):
        raise AcceptanceError("actual_click_observation_unlinked")
    if not (_date(proof["verified_at"]) <= _date(send.get("click_started_at"))
            <= _date(acknowledged or observed) <= _date(receipt["delivered_at"])):
        raise AcceptanceError("actual_click_timing_order_invalid")
    stages = [stage for stage in trace.get("stages", []) if stage.get("name") == "receipt_persisted" and stage.get("outcome") == "ok"]
    if len(stages) != 1:
        raise AcceptanceError("durable_receipt_timing_missing")
    latency = stages[0]["end_offset_ms"] / 1000
    if not isinstance(latency, (int, float)) or not math.isfinite(latency) or not 0 <= latency <= trace["elapsed_ms"] / 1000:
        raise AcceptanceError("durable_receipt_timing_invalid")
    if (_date(send["created_at"]) > _date(proof["verified_at"])
            or _date(proof["verified_at"]) > _date(receipt["delivered_at"])
            or latency + 2 < (_date(receipt["delivered_at"]) - _date(trace["first_seen_at"])).total_seconds()):
        raise AcceptanceError("delivery_timestamp_order_invalid")
    return send, receipt, latency


def report_run(run_dir, *, write=False, now=None):
    """Build a redacted report; slow/failing turns are never relabeled as waits."""
    manifest, config = _load(run_dir)
    final_path = config.run_dir / "report.json"
    if final_path.is_file():
        final = _json(final_path)
        if (final.get("run_id") == manifest["run_id"] and final.get("duration_seconds", 0) >= DURATION_SECONDS
                and final.get("status") in {"PASS", "FAIL", "INSUFFICIENT_SAMPLES"}):
            return final  # A completed run does not absorb later worker activity.
    try:
        samples = [json.loads(line) for line in (config.run_dir / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
    except (ValueError, OSError):
        raise AcceptanceError("samples_unavailable") from None
    reasons = {reason for sample in samples for reason in sample["reasons"]}
    elapsed = samples[-1]["elapsed_seconds"] if samples else 0
    complete = elapsed >= DURATION_SECONDS
    failure_path = config.run_dir / "monitor_failure.json"
    if failure_path.exists():
        failure = _json(failure_path)
        reasons.add("monitor_failed" if failure.get("run_id") == manifest["run_id"]
                    and failure.get("monitor_pid") == manifest["monitor_pid"] else "monitor_failure_marker_invalid")
    if not complete and samples and (_utc(now) - _date(samples[-1]["sampled_at"])).total_seconds() > config.sample_interval_seconds + 5:
        reasons.add("monitor_sampling_stale")
    if not samples or samples[0]["elapsed_seconds"] > 1:
        reasons.add("initial_sample_missing")
    for before, after in zip(samples, samples[1:]):
        delta = after["elapsed_seconds"] - before["elapsed_seconds"]
        wall_delta = (_date(after["sampled_at"]) - _date(before["sampled_at"])).total_seconds()
        if not 0 < delta <= config.sample_interval_seconds + 5:
            reasons.add("monitor_sampling_gap")
        if abs(wall_delta - delta) > 5:
            reasons.add("wall_monotonic_discontinuity")
    metrics = {"warm_speak": [], "cold_speak": [], "natural_wait": []}
    turns, pending, observation_attempts = [], 0, []
    stage_durations, work_totals, preparations = {}, {}, {}
    try:
        sources = _sources(config)
        if sources["repairs"] != manifest["baseline"]["repairs"]:
            reasons.add("manual_repair_changed")
        traces, errors = _traces(config.trace_file)
        reasons.update(errors)
        independent, preparation_errors = _preparation_records(config, manifest, moment=_utc(now), complete=complete)
        reasons.update(preparation_errors)
        for entry in independent:
            observation_attempts.append(entry["public"])
            if not entry["complete"]:
                continue  # A later complete source must retain its full fingerprint.
            try:
                _add_workload(_independent_workload(entry["trace"]), stage_durations, work_totals, preparations)
            except (AcceptanceError, KeyError, TypeError, ValueError) as exc:
                reasons.add(str(exc) if isinstance(exc, AcceptanceError) else "preparation_work_evidence_invalid")
        new_events = {key: value for key, value in sources["events"].items() if key not in manifest["baseline"]["events"]}
        trace_events, trace_decisions = set(), set()
        linked_sends = set()
        for identifier, trace in traces.items():
            if identifier in manifest["baseline_trace_ids"]:
                continue
            if _date(trace["first_seen_at"]) < _date(manifest["started_at"]):
                continue
            if (_date(trace["first_seen_at"]) - _date(manifest["started_at"])).total_seconds() > DURATION_SECONDS:
                continue
            event = new_events.get(trace.get("event_id"))
            counters = trace.get("counters", {})
            terminal = [key for key in _TERMINALS if counters.get(key, 0)]
            if not terminal:
                pending += 1
                continue
            try:
                if (len(terminal) != 1 or counters[terminal[0]] != 1 or counters.get("events_dropped", 0)
                        or (terminal[0] == "turn_sent" and type(counters.get("cold_path")) is not int)
                        or counters.get("cold_path", 0) not in (0, 1)):
                    raise AcceptanceError("invalid_terminal_trace")
                if event is None or _date(event.get("first_seen_at") or event["observed_at"]) != _date(trace["first_seen_at"]):
                    raise AcceptanceError("event_first_seen_mismatch")
                wall_offset = (_date(trace["first_seen_at"]) - _date(manifest["started_at"])).total_seconds()
                if abs(trace["first_seen_mono"] - manifest["started_mono"] - wall_offset) > 5:
                    raise AcceptanceError("first_seen_monotonic_mismatch")
                _add_workload(trace, stage_durations, work_totals, preparations)
                if terminal[0] in _EXCLUDED_TERMINALS:
                    turns.append({"trace_id": _opaque(identifier), "event_id": _opaque(event["event_id"]),
                                  "kind": "excluded", "reason": terminal[0]})
                    continue
                if trace["event_id"] in trace_events or (trace.get("decision_id") and trace["decision_id"] in trace_decisions):
                    raise AcceptanceError("duplicate_terminal_identity")
                trace_events.add(trace["event_id"])
                if trace.get("decision_id"):
                    trace_decisions.add(trace["decision_id"])
                if terminal[0] in {"turn_failed", "turn_send_disabled"}:
                    raise AcceptanceError("turn_failed")
                if counters.get("decision_cache_hits", 0):
                    raise AcceptanceError("cached_decision_not_natural_sample")
                entry = {"event_id": _opaque(event["event_id"]), "trace_id": _opaque(identifier), "decision_id": _opaque(trace.get("decision_id"))}
                if terminal[0] == "turn_natural_wait":
                    decision = sources["decisions"].get(trace.get("decision_id"), {})
                    if (event["state"] != "decided_wait" or decision.get("outcome") != "natural_wait"
                            or decision.get("_stored_status") != "completed"
                            or event.get("decision_id") != trace.get("decision_id")
                            or not event.get("source_record_id")
                            or event["source_record_id"] not in decision.get("source_record_ids", [])
                            or decision.get("decision_source") != "model" or decision.get("action") != "wait"
                            or decision.get("error_type") or decision.get("reply") is not None):
                        raise AcceptanceError("wait_not_natural")
                    completions = [stage for stage in trace.get("stages", []) if stage.get("name") == "turn_completed" and stage.get("outcome") == "ok"]
                    if len(completions) != 1:
                        raise AcceptanceError("wait_completion_timing_missing")
                    wait_seconds = completions[0]["end_offset_ms"] / 1000
                    if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 60:
                        raise AcceptanceError("wait_completion_timing_invalid")
                    if complete and wall_offset + wait_seconds > DURATION_SECONDS:
                        raise AcceptanceError("wait_completed_outside_run_window")
                    kind = "natural_wait"
                    metrics[kind].append(wait_seconds)
                else:
                    send, receipt, latency = _link_delivery(trace, event, sources)
                    if complete and wall_offset + latency > DURATION_SECONDS:
                        raise AcceptanceError("receipt_completed_outside_run_window")
                    if receipt["receipt_id"] in manifest["baseline"]["receipts"] or send["reservation_id"] in manifest["baseline"]["sends"]:
                        raise AcceptanceError("baseline_delivery_cannot_count")
                    linked_sends.add(send["reservation_id"])
                    kind = "cold_speak" if counters.get("cold_path", 0) == 1 else "warm_speak"
                    metrics[kind].append(latency)
                    entry.update(reservation_id=_opaque(send["reservation_id"]), receipt_id=_opaque(receipt["receipt_id"]), latency_seconds=latency)
                    entry["click_timing_basis"] = "acknowledged" if send.get("click_acknowledged_at") is not None else "observed"
                    refs = list(receipt.get("evidence_refs", [])) + list(send.get("evidence_refs", []))
                    entry["evidence_reference_hashes"] = sorted({_digest(ref) for ref in refs})
                turns.append(entry | {"kind": kind})
            except (AcceptanceError, KeyError, TypeError, ValueError) as exc:
                reason = str(exc) if isinstance(exc, AcceptanceError) else "turn_evidence_invalid"
                reasons.add(reason)
                turns.append({"trace_id": _opaque(identifier), "kind": "failed", "reason": reason})
        for identifier, send in sources["sends"].items():
            if identifier in manifest["baseline"]["sends"]:
                continue
            send_event = new_events.get(send["event_id"])
            if send_event and (_date(send_event.get("first_seen_at") or send_event["observed_at"]) - _date(manifest["started_at"])).total_seconds() > DURATION_SECONDS:
                continue
            if complete and send["status"] == "uncertain":
                reasons.add("uncertain_send")
            if complete and send["status"] == "sent" and identifier not in linked_sends:
                reasons.add("sent_without_terminal_trace")
            if complete and send["status"] in {"reserved", "retry_wait", "click_started", "not_sent", "expired"}:
                reasons.add("uncompleted_send")
        if complete and pending:
            reasons.add("unterminated_turn")
        if complete:
            traced_events = {trace.get("event_id") for trace in traces.values()
                             if any(trace.get("counters", {}).get(key, 0) for key in _TERMINALS)}
            for event in new_events.values():
                first_seen = _date(event.get("first_seen_at") or event["observed_at"])
                if not 0 <= (first_seen - _date(manifest["started_at"])).total_seconds() <= DURATION_SECONDS:
                    continue
                if event["state"] in {"failed", "expired"}:
                    reasons.add("event_failed_or_expired")
                item = event.get("item", {})
                if item.get("direction") != "incoming" or item.get("kind", "text") != "text":
                    continue
                explicit_exclusion = event["state"] == "superseded" or (
                    event["state"] == "ignored" and event.get("reason") in
                    {"non_text_or_self", "self_message", "considered_in_batch", "cached_speak_not_queued"})
                if event["event_id"] not in traced_events and not explicit_exclusion:
                    reasons.add("event_without_terminal_trace")
    except (AcceptanceError, OSError, ValueError, TypeError, KeyError):
        reasons.add("report_evidence_unavailable")
    p95 = {key: _percentile(metrics[key]) for key in ("warm_speak", "cold_speak")}
    if p95["warm_speak"] is not None and p95["warm_speak"] > 30:
        reasons.add("warm_p95_over_30_seconds")
    if p95["cold_speak"] is not None and p95["cold_speak"] > 60:
        reasons.add("cold_p95_over_60_seconds")
    counts = {key: len(value) for key, value in metrics.items()}
    enough = all(counts[key] >= target for key, target in TARGETS.items())
    status: Literal["RUNNING", "PASS", "FAIL", "INSUFFICIENT_SAMPLES"]
    status = "FAIL" if reasons else "RUNNING" if not complete else "PASS" if enough else "INSUFFICIENT_SAMPLES"
    report = {"schema_version": "qq-acceptance-report-v1", "run_id": manifest["run_id"], "status": status,
              "fully_automatic_e2e_pass": status == "PASS", "duration_seconds": elapsed, "required_duration_seconds": DURATION_SECONDS,
              "started_at": manifest["started_at"], "build_hash": config.build_hash, "worker_pid": config.worker_identity.pid,
              "sample_count": len(samples), "targets": TARGETS, "counts": counts, "p95_seconds": p95,
              "latency_seconds": {key: _distribution(values) for key, values in metrics.items()},
              "stage_seconds": {key: _distribution(values) for key, values in sorted(stage_durations.items())},
              "work_counters": dict(sorted(work_totals.items())),
              "physical_work_preparation_count": len(preparations),
              "preparation_attempts": observation_attempts,
              "preparation_outcomes": {kind: sum(entry["classification"] == kind for entry in observation_attempts)
                                       for kind in sorted({entry["classification"] for entry in observation_attempts})},
              "preparation_association_scope": "same_observation_sequence_and_original_wall_monotonic_origin_within_60_seconds; unlinked_failures_are_diagnostic_only",
              "failure_counts": {reason: sum(turn.get("reason") == reason and turn["kind"] == "failed" for turn in turns)
                                 for reason in sorted({turn["reason"] for turn in turns if turn["kind"] == "failed"})},
              "max_occlusion_gap_seconds": max((max(sample.get("metrics", {}).get("occlusion_gap_seconds", 0),
                    sample.get("metrics", {}).get("max_completed_gap_seconds", 0)) for sample in samples), default=0),
              "pending_turns": pending, "reasons": sorted(reasons), "turns": turns,
              "evidence_scope": "local_status_trace_ledger_receipt_correlation",
              "human_intervention_observability": "recorded_repairs_and_status_only",
              "server_acknowledgement_verified": False}
    if write:
        _write(config.run_dir / "report.json", report)
    return report


def run_monitor(config):
    # No failure marker may be written until this invocation has created its run.
    manifest = initialize_run(config)
    try:
        while True:
            time.sleep(config.sample_interval_seconds)
            sample = sample_run(config.run_dir)
            if sample["elapsed_seconds"] >= DURATION_SECONDS:
                return report_run(config.run_dir, write=True)
    except (Exception, KeyboardInterrupt):
        try:
            _write(config.run_dir / "monitor_failure.json", {
                "schema_version": "qq-acceptance-monitor-failure-v1", "run_id": manifest["run_id"],
                "monitor_pid": manifest["monitor_pid"], "failed_at": _utc().isoformat(),
                "reason": "acceptance_monitor_interrupted"})
        except OSError:
            pass  # A stale final sample still becomes FAIL without this marker.
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start").add_argument("--config", type=Path, required=True)
    for command in ("status", "report"):
        commands.add_parser(command).add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_monitor(AcceptanceConfig.model_validate(_json(args.config))) if args.command == "start" else report_run(args.run_dir, write=args.command == "report")
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result["status"] in {"PASS", "RUNNING"} else 2
    except (AcceptanceError, ValueError, OSError, KeyboardInterrupt):
        print(json.dumps({"status": "FAIL", "reason": "acceptance_input_or_monitor_failure"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
