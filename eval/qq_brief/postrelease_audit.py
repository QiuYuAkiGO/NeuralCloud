"""Read verified QQ deliveries after activation; never update the source database.

Run this file with the deployed release's Python and --release-root. The JSON
artifact is private audit data; stdout contains counts only, never message text.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time


class AuditError(ValueError):
    pass


MAX_CURRENT_REQUEST_CHARACTERS = 2000


def _aware(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise AuditError("timezone_required")
    return moment.astimezone(timezone.utc)


def _redact(text: str) -> tuple[str, bool]:
    safe = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED_KEY]", text)
    safe = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", safe)
    safe = re.sub(r"(?i)(\b(?:api[_-]?key|password|secret|access_token)\s*[:=]\s*)[^\s,;，；]+",
                  r"\1[REDACTED]", safe)
    safe = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", safe)
    return safe, safe != text


def _load_release(root: Path) -> None:
    root = root.resolve(strict=True)
    if not (root / "harness/qq/decision.py").is_file():
        raise AuditError("release_source_missing")
    sys.path.insert(0, str(root))
    from harness.qq import decision
    from harness.qq import reply_context
    if any(not Path(module.__file__).resolve().is_relative_to(root) for module in (decision, reply_context)):
        raise AuditError("wrong_runtime_import")


def collect_audit(database: Path, *, since: datetime, prompt_versions: list[str],
                  limit: int = 50, group_ids: list[str] | None = None,
                  as_of: datetime | None = None, max_candidates: int = 1000,
                  max_seconds: float = 10) -> dict:
    """Return a bounded snapshot of first delivered turns, ordered by delivery.

    A turn belongs to the activation cohort only when its first actually sent
    segment is at/after `since`. Subsequent segments never become another turn.
    Failed integrity checks remain explicit issues and never count as deliveries.
    """
    from harness.qq import decision as reader
    from harness.qq.reply_context import style_diagnostics

    as_of = as_of or datetime.now(timezone.utc)
    if (since.tzinfo is None or as_of.tzinfo is None or since > as_of or not prompt_versions
            or not 1 <= limit <= 50 or not limit <= max_candidates <= 10000
            or not 0 < max_seconds <= 30):
        raise AuditError("invalid_audit_bounds")
    database = database.absolute()
    if not database.is_file() or database.is_symlink():
        raise AuditError("existing_regular_database_required")
    deadline = time.monotonic() + max_seconds
    turns, issues = [], []
    scanned = 0
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=min(3, max_seconds))) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"qq_reply_decisions", "qq_visual_observations"}.issubset(tables):
            raise AuditError("decision_and_observation_tables_required")
        receipt_tables = [name for name in ("qq_reply_deliveries", "qq_reply_segment_deliveries") if name in tables]
        rows = []
        if receipt_tables:
            # Bad receipt JSON remains a candidate for an integrity issue. The
            # join never converts a ready-but-unsent draft into a sent reply.
            union = " UNION ALL ".join(
                "SELECT group_id,decision_id,CASE WHEN json_valid(record_json) "
                "THEN julianday(json_extract(record_json,'$.delivered_at')) END AS delivered_time FROM " + table
                for table in receipt_tables)
            version_slots = ",".join("?" for _ in prompt_versions)
            sql = ("WITH receipts AS (" + union + "), turns AS (SELECT group_id,decision_id,"
                   "MIN(delivered_time) AS first_time,MAX(delivered_time IS NULL) AS bad_time "
                   "FROM receipts GROUP BY group_id,decision_id) SELECT t.* FROM turns t "
                   "LEFT JOIN qq_reply_decisions d ON d.group_id=t.group_id AND d.decision_id=t.decision_id "
                   "WHERE ((t.first_time>=julianday(?) AND t.first_time<=julianday(?)) OR t.bad_time=1) "
                   "AND (d.decision_id IS NULL OR NOT json_valid(d.record_json) OR "
                   "CASE WHEN json_valid(d.record_json) THEN json_extract(d.record_json,'$.prompt_version') "
                   "END IN (" + version_slots + "))")
            parameters = [since.isoformat(), as_of.isoformat(), *prompt_versions]
            if group_ids:
                sql += " AND t.group_id IN (" + ",".join("?" for _ in group_ids) + ")"
                parameters.extend(group_ids)
            sql += " ORDER BY t.first_time,t.decision_id LIMIT ?"
            parameters.append(max_candidates + 1)
            rows = connection.execute(sql, parameters).fetchall()
        candidate_limit_reached = len(rows) > max_candidates
        for row in rows[:max_candidates]:
            if time.monotonic() > deadline:
                raise AuditError("audit_time_budget_exceeded")
            scanned += 1
            try:
                frozen = reader._frozen_decision(connection, row["group_id"], row["decision_id"])
                if frozen.prompt_version not in prompt_versions:
                    raise AuditError("decision_version_mismatch")
                segments = reader._read_segment_receipts(connection, group_id=frozen.group_id, decision=frozen)
                aggregates = connection.execute(
                    "SELECT * FROM qq_reply_deliveries WHERE group_id=? AND decision_id=?",
                    (frozen.group_id, frozen.decision_id)).fetchmany(2) if "qq_reply_deliveries" in tables else []
                if len(aggregates) > 1:
                    raise AuditError("duplicate_aggregate")
                expected = reader.effective_reply_segments(frozen)
                full = reader._checked_receipt(aggregates[0], reader.QQDeliveryReceipt) if aggregates else None
                if full is not None:
                    if (full.origin != "decision" or full.self_member_id != frozen.self_member_id
                            or full.delivered_text != frozen.reply):
                        raise AuditError("aggregate_frozen_mismatch")
                    reader._check_sources(connection, group_id=frozen.group_id,
                                          sources=full.source_record_ids, decision=frozen)
                    if full.delivered_segments:
                        reader._check_aggregate(full, segments, frozen)
                    elif segments or len(expected) != 1:
                        raise AuditError("aggregate_missing_segment_bindings")
                elif not segments:
                    raise AuditError("no_verified_receipt")
                times = [item.delivered_at for item in segments] + ([full.delivered_at] if full else [])
                if max(times) > as_of:
                    raise AuditError("delivery_after_audit_snapshot")
                if min(times) < since:
                    continue
                delivered = [item.delivered_text for item in segments] if segments else [full.delivered_text]
                text = "\n\n".join(delivered)
                safe, redacted = _redact(text)
                plan = getattr(frozen, "reply_plan", None)
                detail = (plan.get("detail_level", "brief") if isinstance(plan, dict)
                          else getattr(plan, "detail_level", "brief"))
                intent = plan.get("intent", "other") if isinstance(plan, dict) else getattr(plan, "intent", "other")
                raw_request = getattr(frozen, "effective_query", None)
                safe_request, request_redacted = _redact(raw_request) if raw_request is not None else (None, False)
                request_truncated = bool(getattr(frozen, "derived_query_truncated", False))
                if safe_request is not None:
                    request_truncated |= len(safe_request) > MAX_CURRENT_REQUEST_CHARACTERS
                    safe_request = safe_request[:MAX_CURRENT_REQUEST_CHARACTERS]
                coverage = ("aggregate_verified" if full else "partial_prefix" if len(segments) < len(expected)
                            else "all_segments_verified_without_aggregate")
                receipts = [*segments, *([full] if full else [])]
                turns.append({"decision_id": frozen.decision_id, "group_id": frozen.group_id,
                    "self_member_id": frozen.self_member_id, "prompt_version": frozen.prompt_version,
                    "first_delivered_at": min(times).astimezone(timezone.utc).isoformat(),
                    "last_delivered_at": max(times).astimezone(timezone.utc).isoformat(),
                    "coverage": coverage, "delivered_segment_count": len(delivered),
                    "planned_segment_count": len(expected), "receipt_ids": [item.receipt_id for item in receipts],
                    "evidence_refs": sorted({_redact(ref)[0] for item in receipts for ref in item.evidence_refs}),
                    "source_record_ids": sorted({ref for item in receipts for ref in item.source_record_ids}),
                    "new_observation_record_ids": list(frozen.new_observation_record_ids),
                    "current_request": safe_request,
                    "current_request_sha256": (hashlib.sha256(raw_request.encode("utf-8")).hexdigest()
                                               if raw_request is not None else None),
                    "current_request_redacted": request_redacted,
                    "current_request_truncated": request_truncated,
                    "query_source": frozen.query_source, "addressing": frozen.addressing,
                    "reply_plan": {"intent": intent, "detail_level": detail},
                    "reply_plan_available": plan is not None,
                    "content": safe, "content_redacted": redacted,
                    "delivered_content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "style": style_diagnostics(text, detail_level=detail, segments=len(delivered))})
            except (ValueError, TypeError, KeyError, IndexError):
                issues.append({"decision_id": row["decision_id"], "group_id": row["group_id"],
                               "code": "receipt_or_frozen_decision_integrity_failed"})
            if len(turns) >= limit:
                break
    turns.sort(key=lambda item: (item["first_delivered_at"], item["decision_id"]))
    metrics = ("brief_length_exceeded", "brief_sentence_count_exceeded", "hesitant_opening",
               "memory_disclaimer", "relational_cycle_proxy")
    aggregate = {"verified_turns": len(turns), "target_turns": limit,
                 "aggregate_verified": sum(item["coverage"] == "aggregate_verified" for item in turns),
                 "partial_prefix": sum(item["coverage"] == "partial_prefix" for item in turns),
                 "segments_without_aggregate": sum(item["coverage"] == "all_segments_verified_without_aggregate" for item in turns),
                 "integrity_issues": len(issues), "scanned_candidate_turns": scanned,
                 "candidate_limit_reached": candidate_limit_reached,
                 "first_cohort_complete": len(turns) == limit and not issues,
                 **{key: sum(item["style"][key] for item in turns) for key in metrics}}
    return {"schema_version": "qq-brief-postrelease-audit-v1", "since": since.isoformat(),
            "as_of": as_of.isoformat(), "prompt_versions": sorted(set(prompt_versions)),
            "database_readonly": True, "cohort_rule": "first_verified_segment_at_or_after_activation",
            "aggregate": aggregate, "turns": turns, "issues": issues}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--since", type=_aware, required=True)
    parser.add_argument("--prompt-version", action="append", required=True)
    parser.add_argument("--group-id", action="append")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-candidates", type=int, default=1000)
    parser.add_argument("--max-seconds", type=float, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        _load_release(args.release_root)
        report = collect_audit(args.database, since=args.since, prompt_versions=args.prompt_version,
            group_ids=args.group_id, limit=args.limit, max_candidates=args.max_candidates, max_seconds=args.max_seconds)
        # An exclusive artifact write never overwrites an earlier audit or its
        # source. No schema creation, model calls or sends are available here.
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        print(json.dumps(report["aggregate"], ensure_ascii=True, separators=(",", ":")))
        return 0 if not report["issues"] else 2
    except (OSError, ValueError, TypeError, sqlite3.Error, ImportError):
        print('{"audit_failed":true}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
