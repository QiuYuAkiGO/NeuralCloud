"""Local evidence, exact revision joins, and reports for the Babel protocol.

The output directory is private: originals retain existing conversation data.
Monitor projections are never used to claim byte-level journal integrity.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from harness.homeostasis.models import HomeostaticSnapshot
from harness.runtime.journal import RuntimeJournalRecord
from harness.runtime.monitor import sanitize_payload


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex[:8] + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    for attempt in range(7):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 6:
                raise
            # Windows indexers can briefly open a report while it is replaced.
            time.sleep(.01 * (2 ** attempt))


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        handle.flush()


def homeostasis_state(composite: dict) -> dict:
    value = composite.get("homeostasis", {}).get("payload", {})
    state = value.get("snapshot") or value.get("state") or (value if "dimensions" in value else {})
    return {k: v for k, v in state.items() if k in HomeostaticSnapshot.model_fields}


class EvidenceStore:
    """A disk index keeps long runs bounded; durable monitor sequence is the key."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.db = sqlite3.connect(directory / "index.sqlite3")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY, correlation TEXT, event_id TEXT, topic TEXT,
                observed_at TEXT, client_monotonic REAL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_correlation ON events(correlation, seq);
            CREATE TABLE IF NOT EXISTS states(
                revision INTEGER, hash TEXT, payload TEXT, PRIMARY KEY(revision, hash));
        """)

    def add_state(self, state: dict) -> None:
        if not state or not state.get("interoceptive_summary"):
            return
        validated = HomeostaticSnapshot.model_validate(state).model_dump(mode="json")
        self.db.execute("INSERT OR IGNORE INTO states VALUES(?,?,?)", (
            validated["revision"], fingerprint(validated["interoceptive_summary"]),
            json.dumps(validated, ensure_ascii=False),
        ))
        self.db.commit()

    def add(self, item: dict, monotonic: float) -> bool:
        seq = item["seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise ValueError("invalid monitor sequence")
        encoded = json.dumps(item, ensure_ascii=False)
        existing = self.db.execute("SELECT payload FROM events WHERE seq=?", (seq,)).fetchone()
        if existing:
            if json.loads(existing[0]) != item:
                raise ValueError("monitor sequence reused with different payload")
            return False
        # Append before acknowledging the cursor. Index is local and rebuildable.
        append_jsonl(self.directory / "monitor.jsonl", {**item, "client_monotonic": monotonic})
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)", (
            seq, item.get("correlation_id"), item.get("event_id"), item.get("topic"),
            item.get("observed_at"), monotonic, encoded,
        ))
        data = item.get("payload", {}).get("data", {})
        if data.get("service") == "homeostasis":
            result = data.get("result", {}).get("result") or {}
            state = result.get("snapshot")
            if state:
                self.add_state(state)
        self.db.commit()
        return True

    def events(self, correlation: str, event_id: str | None = None) -> list[dict]:
        rows = self.db.execute("SELECT payload, client_monotonic FROM events WHERE correlation=? ORDER BY seq", (correlation,))
        values = [{**json.loads(row[0]), "client_monotonic": row[1]} for row in rows]
        return [v for v in values if event_id is None or v.get("event_id") in {event_id, None}]

    def exact_state(self, revision: int, summary_hash: str) -> dict | None:
        row = self.db.execute("SELECT payload FROM states WHERE revision=? AND hash=?", (revision, summary_hash)).fetchone()
        return json.loads(row[0]) if row else None

    def journal_bounds(self) -> tuple[int | None, int | None]:
        rows = self.db.execute("SELECT payload FROM events WHERE topic LIKE 'journal.%' ORDER BY seq")
        seqs = [json.loads(r[0]).get("payload", {}).get("record_seq") for r in rows]
        seqs = [s for s in seqs if isinstance(s, int)]
        return (min(seqs), max(seqs)) if seqs else (None, None)

    def export_background(self, correlations: set[str], event_ids: set[str]) -> set[str]:
        background_correlations = set()
        with (self.directory / "background.jsonl").open("w", encoding="utf-8") as output:
            for (raw,) in self.db.execute("SELECT payload FROM events ORDER BY seq"):
                value = json.loads(raw)
                if value.get("correlation_id") not in correlations or (value.get("event_id") is not None and value["event_id"] not in event_ids):
                    output.write(raw + "\n")
                    if value.get("correlation_id"):
                        background_correlations.add(value["correlation_id"])
        return background_correlations

    def close(self) -> None:
        self.db.close()


def _elapsed(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    seconds = (datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds()
    return seconds * 1000 if seconds >= 0 else None


def analyze_turn(events: list[dict], store: EvidenceStore, sent_monotonic: float) -> dict:
    journal = [item for item in events if item.get("topic", "").startswith("journal.")]
    intents = {i["payload"].get("command_id"): i for i in journal if i["topic"] == "journal.domain_command_intent"}
    receipts, commits, commands, routes = [], [], [], []
    terminal = None
    failed = None
    for item in journal:
        record = item["payload"]
        kind, data = record["record_type"], record.get("data", {})
        if kind == "processing_failed":
            failed = data
        if kind == "processing_completed":
            terminal = item
        if kind == "reply_delivery":
            receipts.append(item)
        if kind == "homeostatic_interoception_routed" and data.get("target") == "PrepareResponse":
            routes.append(item)
        if kind in {"domain_command_result", "domain_command_late_result"}:
            intent = intents.get(record.get("command_id"))
            if not intent:
                continue
            envelope = intent["payload"]["data"]["envelope"]
            result = data.get("result", {})
            commands.append({
                "command_id": record["command_id"], "service": data.get("service"),
                "command_type": envelope.get("command_type"), "status": result.get("status"),
                "late": kind.endswith("late_result"), "server_started_at": intent["payload"]["recorded_at"],
                "server_finished_at": record["recorded_at"],
                "wall_derived_ms": _elapsed(intent["payload"]["recorded_at"], record["recorded_at"]),
            })
            if envelope.get("command_type") == "CommitInteraction" and kind == "domain_command_result" and result.get("status") in {"applied", "idempotent_replay"}:
                commits.append((item, envelope))
    detail = terminal["payload"]["data"] if terminal else {}
    prepare_id = detail.get("detail", {}).get("prepare_id")
    delivery = next((i for i in reversed(receipts) if i["payload"]["data"].get("prepare_id") == prepare_id and i["payload"]["data"].get("result", {}).get("success") is True), None)
    commit = next((i for i, env in reversed(commits) if env.get("payload", {}).get("prepare_id") == prepare_id), None)
    completed = bool(terminal and detail.get("status") == "completed" and detail.get("decision_kind") == "reply" and detail.get("detail", {}).get("delivered") is True and delivery and commit)
    matched_routes = []
    for route in routes:
        data = route["payload"]["data"]
        revision, digest = data.get("homeostasis_revision"), data.get("homeostatic_summary_hash")
        state = store.exact_state(revision, digest)
        intent = intents.get(route["payload"].get("command_id"))
        env = intent["payload"]["data"]["envelope"] if intent else {}
        actual = env.get("payload", {}).get("homeostatic_interoception")
        valid = bool(state and actual and fingerprint(actual) == digest and env.get("version_vector", {}).get("homeostasis_state") == revision)
        matched_routes.append({"revision": revision, "summary_hash": digest, "command_id": route["payload"].get("command_id"), "exact": valid, "snapshot": state if valid else None, "actual_summary": actual})
    segments = delivery["payload"]["data"]["result"].get("delivered_segments", []) if delivery else []
    if not segments and receipts:
        segments = receipts[-1]["payload"]["data"].get("result", {}).get("delivered_segments", [])
    timestamps = {}
    for name, item in (("delivery_observed_ms", delivery), ("commit_observed_ms", commit), ("completed_observed_ms", terminal)):
        timestamps[name] = max(0, (item["client_monotonic"] - sent_monotonic) * 1000) if item else None
    accepted = next((i for i in journal if i["topic"] == "journal.event_accepted"), None)
    started = next((i for i in journal if i["topic"] == "journal.processing_started"), None)
    timestamps["queue_wall_derived_ms"] = _elapsed(accepted["payload"]["recorded_at"], started["payload"]["recorded_at"]) if accepted and started else None
    stages = [i["payload"] for i in events if i.get("topic", "").startswith("cognitive.stage.")]
    return {
        "durably_completed": completed, "terminal": detail or None, "failure": failed,
        "segments": segments, "reply": "\n\n".join(segments), "prepare_id": prepare_id,
        "commands": commands, "timing": timestamps, "ttft_ms": None,
        "ttft_unavailable_reason": "token streaming is not implemented",
        "used_for_response": matched_routes,
        "state_exact": bool(matched_routes) and all(r["exact"] for r in matched_routes),
        "stages": stages,
    }


def archive_journal(source: Path, destination: Path, first: int | None, last: int | None) -> dict:
    """Copy original complete lines in a continuous interval, including its anchor."""
    if first is None or last is None:
        return {"status": "UNAVAILABLE", "reason": "no journal interval observed"}
    destination.parent.mkdir(parents=True, exist_ok=True)
    paths = sorted(source.glob("runtime-*.jsonl")) if source.is_dir() else [source]
    previous = None
    selected = 0
    expected = first
    digest = hashlib.sha256()
    with destination.open("wb") as output:
        for path in paths:
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                for line in handle:
                    if not line.endswith(b"\n"):
                        break  # an active append is not a complete record
                    record = RuntimeJournalRecord.model_validate_json(line)
                    if record.record_seq < first:
                        previous = (record, line)
                        continue
                    if record.record_seq > last:
                        break
                    if record.record_seq != expected or not record.verify_checksum():
                        raise ValueError("raw journal sequence or checksum invalid")
                    if previous and record.prev_hash != previous[0].checksum:
                        raise ValueError("raw journal hash chain mismatch")
                    if selected == 0:
                        if first > 1 and (previous is None or previous[0].record_seq != first - 1 or not previous[0].verify_checksum()):
                            raise ValueError("raw journal preceding anchor missing")
                        if previous:
                            output.write(previous[1])
                            digest.update(previous[1])
                        elif record.prev_hash:
                            raise ValueError("first journal record has unexpected predecessor")
                    output.write(line)
                    digest.update(line)
                    previous = (record, line)
                    selected += 1
                    expected += 1
    if expected != last + 1:
        raise ValueError("raw journal interval incomplete")
    return {"status": "VERIFIED", "first": first, "last": last, "records": selected, "anchor_included": first > 1, "sha256": digest.hexdigest(), "file": str(destination.name)}


def copy_call_evidence(trace_dir: Path, calls_dir: Path, out: Path, correlations: set[str]) -> dict:
    """Re-scan terminally: future report invocations can collect late spans too."""
    spans = {}
    archived_spans = out / "model_calls.jsonl"
    if archived_spans.is_file():
        for line in archived_spans.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            spans[value["span_id"]] = value
    request_file = trace_dir / "model_requests.jsonl"
    if request_file.is_file():
        with request_file.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                correlation = value.get("correlation_id") or value.get("context", {}).get("correlation_id")
                if correlation in correlations:
                    previous = spans.get(value["span_id"], {})
                    if previous and previous.get("status") != "running" and value.get("status") == "running":
                        continue
                    spans[value["span_id"]] = value
    with (out / "model_calls.jsonl").open("w", encoding="utf-8") as handle:
        for value in spans.values():
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")
    files_by_path = {f["file"]: f for f in read_json(out / "call_files.json")} if (out / "call_files.json").is_file() else {}
    complete_traces = set()
    if calls_dir.exists():
        for path in calls_dir.rglob("*.json"):
            try:
                value = read_json(path)
            except (OSError, ValueError):
                continue
            correlation = value.get("correlation_id") or value.get("context", {}).get("correlation_id") or value.get("call_id")
            if correlation not in correlations:
                continue
            target = out / "calls" / path.relative_to(calls_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            files_by_path[str(target.relative_to(out))] = {"file": str(target.relative_to(out)), "correlation_id": correlation, "kind": value.get("kind"), "ok": value.get("ok"), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    files = list(files_by_path.values())
    for entry in files:
        value = read_json(out / entry["file"])
        stages = {step.get("kind") for step in value.get("steps", [])}
        if value.get("ok") and value.get("kind") == "prepare_response" and {"compile", "generate", "final"} <= stages:
            complete_traces.add(entry["correlation_id"])
    prepare_models = {s.get("correlation_id") for s in spans.values() if s.get("status") != "running" and s.get("phase") == "prepare" and s.get("role") == "main" and s.get("stage") in {"generate", "revise"}}
    commit_models = {s.get("correlation_id") for s in spans.values() if s.get("status") != "running" and s.get("phase") == "commit" and s.get("role") == "aux"}
    commit_logs = {entry["correlation_id"] for entry in files if read_json(out / entry["file"]).get("phase") == "commit"}
    write_json(out / "call_files.json", files)
    return {"model_spans": len(spans), "pending_model_spans": sum(s.get("status") == "running" for s in spans.values()), "call_files": len(files), "complete_trace_correlations": sorted(complete_traces & prepare_models & commit_models & commit_logs)}


def distribution(values: list[float]) -> dict:
    values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not values:
        return {"count": 0}
    def percentile(q):
        position = (len(values) - 1) * q
        low, high = math.floor(position), math.ceil(position)
        return values[low] + (values[high] - values[low]) * (position - low)
    return {"count": len(values), "mean_ms": sum(values) / len(values), "p50_ms": percentile(.5), "p95_ms": percentile(.95), "max_ms": values[-1]}


def render_report(directory: Path) -> dict:
    run = read_json(directory / "run.json")
    turns = [read_json(p) for p in sorted((directory / "turns").glob("*.json"))]
    completed = [t for t in turns if t.get("status") == "COMPLETED"]
    summary = {"schema_version": "babel-summary-v1", "run_id": run["run_id"], "status": run["status"],
               "requested_turns": 100, "attempted_turns": len(turns), "completed_turns": len(completed),
               "completed_messages": len(completed) * 2, "stop_reason": run.get("stop_reason"),
               "total_elapsed_seconds": run.get("total_elapsed_seconds"), "interval_seconds": run.get("interval_seconds"),
               "evidence": run.get("evidence", {}), "phases": {}}
    for phase in ("story", "reflection"):
        rows = [t for t in completed if t["phase"] == phase]
        summary["phases"][phase] = {"completed": len(rows), "client_completed_latency": distribution([t["analysis"]["timing"]["completed_observed_ms"] for t in rows]),
                                    "state_exact": sum(t["analysis"]["state_exact"] for t in rows)}
    summary["slowest_turns"] = [{"turn": t["turn"], "elapsed_ms": t["analysis"]["timing"]["completed_observed_ms"]} for t in sorted(completed, key=lambda t: t["analysis"]["timing"]["completed_observed_ms"], reverse=True)[:5]]
    model_groups = {}
    if (directory / "model_calls.jsonl").is_file():
        for line in (directory / "model_calls.jsonl").read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            key = f"{value.get('phase') or 'unknown'}/{value.get('role') or 'unknown'}"
            group = model_groups.setdefault(key, {"requests": 0, "errors": 0, "pending": 0, "token_counts": {}, "durations": []})
            group["requests"] += 1
            group["errors"] += value.get("status") == "error"
            group["pending"] += value.get("status") == "running"
            if value.get("latency_ms") is not None:
                group["durations"].append(value["latency_ms"])
            usage = value.get("usage")
            if usage:
                counts = group["token_counts"].setdefault(value.get("usage_source", "unavailable"), {"prompt": 0, "completion": 0, "total": 0})
                for name in counts:
                    counts[name] += usage.get(name + "_tokens", 0)
        for group in model_groups.values():
            group["latency"] = distribution(group.pop("durations"))
    summary["model_calls_by_phase_role"] = model_groups
    states = {}
    for t in turns:
        before = t.get("before_send", {})
        after = t.get("after_commit", {})
        for dimension, factor in after.get("dimensions", {}).items():
            group = states.setdefault(dimension, {"first_before_value": before.get("dimensions", {}).get(dimension, {}).get("value"), "last_after_value": None, "minimum": factor["value"], "maximum": factor["value"]})
            group["last_after_value"] = factor["value"]
            group["minimum"] = min(group["minimum"], factor["value"])
            group["maximum"] = max(group["maximum"], factor["value"])
    summary["homeostasis_observed_values"] = states
    write_json(directory / "summary.json", summary)
    with (directory / "turns.jsonl").open("w", encoding="utf-8") as handle:
        for turn in turns:
            handle.write(json.dumps(turn, ensure_ascii=False) + "\n")
    with (directory / "turns.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["turn", "phase", "status", "correlation_id", "accept_observed_ms", "delivery_observed_ms", "commit_observed_ms", "completed_observed_ms", "state_exact"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for t in turns:
            analysis = t.get("analysis", {})
            writer.writerow({**{f: t.get(f) for f in fields[:4]}, "accept_observed_ms": t.get("accept_observed_ms"), **{f: analysis.get("timing", {}).get(f) for f in fields[5:8]}, "state_exact": analysis.get("state_exact")})
    transcript = []
    for t in turns:
        transcript += [f"## 第 {t['turn']} 轮 · {t['status']}", "", "用户：" + t["prompt"], ""]
        if t.get("analysis", {}).get("reply"):
            transcript += ["Misty：" + t["analysis"]["reply"], ""]
    (directory / "transcript.md").write_text("\n".join(transcript), encoding="utf-8")
    text = ["# Misty 巴别塔对话实测", "", f"状态：{summary['status']}；完成 {len(completed)}/100 轮。", "",
            f"停止原因：{run.get('stop_reason') or '无'}。", "", "每轮等待交付和记忆提交完成后，再间隔 10 秒（非默认间隔在 run.json 单列）。", ""]
    for phase, label in (("story", "故事"), ("reflection", "感想")):
        value = summary["phases"][phase]
        text.append(f"- {label}阶段：完成 {value['completed']} 轮，实际输入状态精确关联 {value['state_exact']} 轮。")
        if value["client_completed_latency"]["count"]:
            stats = value["client_completed_latency"]
            text.append(f"  客户端观测到完整提交的耗时：平均 {stats['mean_ms']/1000:.2f} 秒，P50 {stats['p50_ms']/1000:.2f} 秒，P95 {stats['p95_ms']/1000:.2f} 秒；分位数采用线性插值。")
    if model_groups:
        text += ["", "## 模型调用", ""]
        for label, group in model_groups.items():
            text.append(f"- {label}：{group['requests']} 次，失败 {group['errors']} 次，结束时未完成 {group['pending']} 次；用量按 API 实报与估算分列在 summary.json。")
    if states:
        text += ["", "## 内稳态观测", ""]
        for label, values in states.items():
            text.append(f"- {label}：首轮发送前 {values['first_before_value']}，最后已提交轮后 {values['last_after_value']}；提交后样本范围 {values['minimum']:.4f}–{values['maximum']:.4f}。")
    text += ["", "## 解释与证据边界", "", "客户端耗时包含传输、轮询/流通知延迟。域命令及排队耗时由服务端 UTC 时间戳推算，不与跨进程单调时钟混算。阶段可并行，后台调用单列；不把它们相加作为总耗时。TTFT 不可用。", "", "七维状态是规则驱动的功能性代理量，故事词语及文本长度可能直接影响状态。Misty 的感想保留在 transcript.md；第 91 轮为首次整体感想，92–100 轮并非独立测量。现有记忆和上下文裁剪策略保持原样。", "", "逐轮完整状态、链路和时间见 turns/ 与 monitor.jsonl；原始连续 journal 及校验信息见 run.json 的 evidence；监控投影本身不提供原始 journal 的字节级证明。", "", "该目录含现有会话及状态，属于本地私有证据。需要分享时使用 shareable/ 的脱敏副本。", ""]
    (directory / "report.md").write_text("\n".join(text), encoding="utf-8")
    return summary


def export_shareable(directory: Path, secrets: list[str]) -> None:
    def safe_urls(value):
        if isinstance(value, dict):
            return {key: safe_urls(item) for key, item in value.items()}
        if isinstance(value, list):
            return [safe_urls(item) for item in value]
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            parts = urlsplit(value)
            return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
        return value

    target = directory / "shareable"
    for source in [directory / "summary.json", directory / "run.json", directory / "turns.jsonl", directory / "model_calls.jsonl", directory / "monitor.jsonl", directory / "background.jsonl"]:
        if not source.exists():
            continue
        target.mkdir(exist_ok=True)
        with source.open(encoding="utf-8") as incoming, (target / source.name).open("w", encoding="utf-8") as outgoing:
            values = (json.loads(line) for line in incoming if line.strip()) if source.suffix == ".jsonl" else [json.load(incoming)]
            for value in values:
                encoded = json.dumps(safe_urls(sanitize_payload(value)), ensure_ascii=False, indent=None if source.suffix == ".jsonl" else 2)
                for secret in sorted(set(filter(None, secrets)), key=len, reverse=True):
                    encoded = encoded.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "<redacted>")
                outgoing.write(encoded + "\n")
