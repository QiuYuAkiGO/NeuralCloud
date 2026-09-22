"""Checkpointed historical QQ memory replay into an isolated output directory.

Never opens a live preset or source database for writing, starts a service, or
sends a QQ message. Re-run the same command to resume its frozen input snapshot.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from gui.persist import DEFAULT_OLLAMA_BASE_URL, GuiPersistedSettings, apply_loaded_tuning
from gui.presets import CharacterPreset, atomic_write_json
from harness.cognitive.qq_memory import _hash, build_commit, memory_state, proposal_context
from harness.config import Settings
from harness.memory.impressions import ImpressionMemory
from harness.memory.qq_assimilation import QQAssimilationQueue, QQMemoryProposer, qq_reasoning_options
from harness.memory.shared import SharedMemoryStore
from harness.memory.tier import maintain_qq_events, qq_event_strength
from harness.qq.conversation import conversation_audience, private_scope_id
from harness.files import replace_path


def _now():
    return datetime.now(timezone.utc)


def _sha(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _atomic_bytes(path, data):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        replace_path(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_readonly(source, destination):
    temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            original.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(temporary)) as frozen:
                original.backup(frozen)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        replace_path(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _output_lock(path):
    # OS locks disappear on a crashed process, unlike a stale PID-file lock.
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _settings(path):
    persisted = GuiPersistedSettings.model_validate_json(path.read_text(encoding="utf-8"))
    settings = Settings()
    updates = {"model_version": "mock" if persisted.mode == "demo" else "live",
               "aux_llm_preload": False, "record_interaction": False, "call_log_enabled": False,
               "llm_json_max_tokens": 8192}
    for field in ("base_url", "model", "api_key"):
        if getattr(persisted, field):
            updates["llm_" + field] = getattr(persisted, field)
        if getattr(persisted, "aux_" + field):
            updates["aux_llm_" + field] = getattr(persisted, "aux_" + field)
    if persisted.mode == "ollama" and not persisted.base_url:
        updates["llm_base_url"] = DEFAULT_OLLAMA_BASE_URL
    settings = settings.model_copy(update=updates)
    apply_loaded_tuning(settings, persisted)
    return settings


def _policy(settings):
    # The replay policy is frozen; credentials/endpoints never enter artifacts.
    return {key: value for key, value in settings.model_dump(mode="json").items()
            if key.startswith(("qq_memory_", "qq_impression_", "uniqueness_", "tier_promotion_"))
            or key in {"tier_hit_min_relevance", "short_term_ttl_days"}}


def _queue(path, preset_id, self_id, groups, settings, private_peers=()):
    return QQAssimilationQueue(path, owner_id="misty", preset_id=preset_id, self_id=self_id,
        allowed_group_ids=groups, allowed_private_peer_ids=private_peers,
        idle_seconds=settings.qq_memory_window_idle_seconds,
        max_seconds=settings.qq_memory_window_max_seconds, max_messages=settings.qq_memory_window_max_messages)


def _source_counts(path, groups, self_id=None, private_peers=()):
    audiences = [conversation_audience(group) for group in groups]
    audiences.extend(conversation_audience(private_scope_id(self_id, peer)) for peer in private_peers)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute("SELECT source_kind, COUNT(*) FROM shared_memory_entries WHERE owner_id=? "
            "AND channel IN ('qq_group','qq_private') "
            f"AND session_id IN ({','.join('?' for _ in audiences)}) GROUP BY source_kind",
            ("misty", *audiences)).fetchall()
    counts = dict(rows)
    return {"raw_observations": counts.get("qq_observation", 0), "source_records": sum(counts.values()),
            "source_kinds": counts}


def _manifest_sources(sources):
    return [{"source_id": item.source_id, "source_sha256": item.source_sha256,
             "original_record_id": item.record.source.original_record_id,
             "original_sha256": item.record.source.original_sha256} for item in sources]


def _freeze(source_db, preset_path, output, self_id, groups, settings, now, private_peers=()):
    manifest_path = output / "source_manifest.json"
    if not manifest_path.exists():
        preset_bytes = preset_path.read_bytes()
        preset = CharacterPreset.model_validate_json(preset_bytes)
        if preset.preset_id != "misty":
            raise ValueError("qq_backfill_requires_misty_preset")
        if (output / "preset.json").exists():
            raise ValueError("qq_backfill_missing_manifest_for_existing_checkpoint")
        _backup_readonly(source_db, output / "source.sqlite3")
        _atomic_bytes(output / "input-preset.json", preset_bytes)
        source_path = output / "source.sqlite3"
        with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "shared_memory_entries" not in tables:
            source_path = output / "shared.sqlite3"
            SharedMemoryStore(source_path).import_qq_database(output / "source.sqlite3",
                allowed_group_ids=groups, self_member_id=self_id, allowed_private_peer_ids=private_peers)
        sources = _queue(source_path, preset.preset_id, self_id, groups, settings, private_peers).snapshot()
        frozen = tuple(source for source in sources if source.occurred_at <= now)
        manifest = {"schema_version": "qq-backfill-manifest-v1", "frozen_at": now.isoformat(),
            "owner_id": "misty", "processing_version": "qq-memory-v1",
            "source_path": str(source_db), "preset_path": str(preset_path), "preset_id": preset.preset_id,
            "self_id": self_id, "group_ids": list(groups), "private_peer_ids": list(private_peers),
            "policy": _policy(settings),
            "source_snapshot": source_path.name, "snapshot_sha256": _sha(source_path),
            "source_backup_sha256": _sha(output / "source.sqlite3"),
            "input_preset_sha256": hashlib.sha256(preset_bytes).hexdigest(),
            "sources": _manifest_sources(frozen), "excluded_future_observations": len(sources) - len(frozen),
            **_source_counts(source_path, groups, self_id, private_peers)}
        atomic_write_json(manifest_path, manifest)
        atomic_write_json(output / "preset.json", preset.model_dump(mode="json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != "qq-backfill-manifest-v1" or manifest["self_id"] != self_id
            or manifest["group_ids"] != list(groups) or manifest["source_path"] != str(source_db)
            or manifest.get("private_peer_ids", []) != list(private_peers)
            or manifest["preset_path"] != str(preset_path)):
        raise ValueError("qq_backfill_resume_scope_changed")
    if manifest["source_snapshot"] not in {"source.sqlite3", "shared.sqlite3"}:
        raise ValueError("qq_backfill_invalid_snapshot_path")
    source_path = output / manifest["source_snapshot"]
    if (_sha(source_path) != manifest["snapshot_sha256"]
            or _sha(output / "source.sqlite3") != manifest["source_backup_sha256"]
            or _sha(output / "input-preset.json") != manifest["input_preset_sha256"]):
        raise ValueError("qq_backfill_frozen_input_changed")
    settings = settings.model_copy(update=manifest["policy"])
    frozen_at = datetime.fromisoformat(manifest["frozen_at"])
    sources = tuple(source for source in _queue(source_path, manifest["preset_id"], self_id, groups,
                                               settings, private_peers).snapshot()
                    if source.occurred_at <= frozen_at)
    if _manifest_sources(sources) != manifest["sources"]:
        raise ValueError("qq_backfill_source_manifest_changed")
    if not (output / "preset.json").exists():
        initial = CharacterPreset.model_validate_json((output / "input-preset.json").read_bytes())
        atomic_write_json(output / "preset.json", initial.model_dump(mode="json"))
    return manifest, sources, settings


def _make_proposal(job, old, persona, settings):
    from harness.llm.client import build_llm_client, qq_memory_proposal_settings
    from harness.llm.deadline import model_deadline
    if settings.model_version == "mock":
        raise ValueError("qq_backfill_requires_configured_live_model")
    settings = qq_memory_proposal_settings(settings)
    llm = build_llm_client(settings)
    try:
        with model_deadline(_now() + timedelta(seconds=settings.qq_memory_model_timeout_seconds)):
            return QQMemoryProposer(llm, timeout_seconds=settings.qq_memory_model_timeout_seconds,
                reasoning_options=qq_reasoning_options(settings)).propose(
                job, old_impressions=old, persona=persona)
    finally:
        llm.close()


def _maintain(preset, settings, now):
    events = maintain_qq_events(preset.episodic, settings, now=now)
    by_id = {event.event_id: event for event in events}
    state = memory_state(preset.qq_memory_state)
    for key, raw in state["impressions"].items():
        impression = ImpressionMemory.model_validate(raw)
        active = [by_id[evidence.episode_id] for evidence in impression.evidence
                  if evidence.episode_id in by_id and by_id[evidence.episode_id].tier != "forgotten"]
        updates = {"strength": max((qq_event_strength(event, settings, now=now) for event in active), default=0.0),
                   "strength_updated_at": now}
        if not active:
            updates["status"] = "dormant"
        state["impressions"][key] = impression.model_copy(update=updates).model_dump(mode="json")
    state["last_review_at"] = now.isoformat()
    facts = [fact for fact in preset.semantic if not fact.fact_id.startswith("qqsemantic_")
             or state["impressions"].get("impression_" + fact.fact_id.removeprefix("qqsemantic_"), {}).get("status") == "stable"]
    candidates = [candidate.model_copy(update={"status": "dropped", "consolidated_fact_id": None})
        if candidate.candidate_id.startswith("qqcandidate_") and state["impressions"].get(
            "impression_" + candidate.candidate_id.removeprefix("qqcandidate_"), {}).get("status") == "dormant"
        else candidate for candidate in preset.candidates]
    return preset.model_copy(update={"episodic": events, "semantic": facts, "candidates": candidates,
                                      "qq_memory_state": state})


def _manifest_audiences(manifest):
    return {conversation_audience(group) for group in manifest["group_ids"]} | {
        conversation_audience(private_scope_id(manifest["self_id"], peer))
        for peer in manifest.get("private_peer_ids", [])}


def _report(preset, manifest, queue, *, failures, completed, elapsed, processed):
    state = memory_state(preset.qq_memory_state)
    audiences = _manifest_audiences(manifest)
    episodes = [event for event in preset.episodic if event.scope and event.scope.owner_id == "misty"
                and event.scope.conversation_id in audiences]
    episode_ids = {event.event_id for event in episodes}
    impressions = [item for item in state["impressions"].values()
                   if item.get("scope", {}).get("owner_id") == "misty"
                   and item.get("scope", {}).get("conversation_id") in audiences]
    statuses = Counter(item["status"] for item in impressions)
    receipts = [receipt for receipt in state["receipts"].values()
                if receipt["episode_ids"] and set(receipt["episode_ids"]) <= episode_ids]
    return {"schema_version": "qq-backfill-report-v1", "completed": completed,
        "frozen_at": manifest["frozen_at"], "updated_at": _now().isoformat(),
        "raw_observations": manifest["raw_observations"], "source_records": manifest["source_records"],
        "source_kinds": manifest["source_kinds"], "frozen_observations": len(manifest["sources"]),
        "excluded_future_observations": manifest["excluded_future_observations"],
        "episodes": len(episodes), "tiers": dict(Counter(event.tier for event in episodes)),
        "impressions": len(impressions), "impression_states": dict(statuses),
        "stable_judgments": statuses["stable"], "disputed_judgments": statuses["disputed"],
        "committed_windows": len(receipts), "queue": queue.status(),
        "processed_this_run": processed, "failures_this_run": failures, "duration_ms": round(elapsed * 1000, 3)}


def run_backfill(*, source_db, preset, settings, output, self_id, group_ids,
                 private_peer_ids=(), proposer=None, max_jobs=None, now=None, progress=print):
    """Return a count-only report; injected proposer follows QQMemoryProposer.propose."""
    source_db, preset_path, settings_path = (Path(path).resolve() for path in (source_db, preset, settings))
    output = Path(output).resolve()
    if not all(path.is_file() for path in (source_db, preset_path, settings_path)):
        raise ValueError("qq_backfill_input_unavailable")
    if output in {source_db.parent, preset_path.parent, settings_path.parent}:
        raise ValueError("qq_backfill_requires_isolated_output_directory")
    if max_jobs is not None and (type(max_jobs) is not int or max_jobs < 1):
        raise ValueError("qq_backfill_max_jobs_invalid")
    groups = tuple(sorted(set(group_ids)))
    private_peers = tuple(sorted(set(private_peer_ids)))
    if not groups and not private_peers:
        raise ValueError("qq_backfill_conversation_scope_required")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("source.sqlite3", "shared.sqlite3", "queue.sqlite3", "input-preset.json", "preset.json",
                 "source_manifest.json", "report.json", "backfill.lock"):
        target = output / name
        if (target.is_symlink() or target.resolve() in {source_db, preset_path, settings_path}
                or target.is_file() and any(target.samefile(path) for path in (source_db, preset_path, settings_path))):
            raise ValueError("qq_backfill_output_aliases_live_input")
    started, processed, failures = time.perf_counter(), 0, 0
    moment = now or _now()
    with _output_lock(output / "backfill.lock"):
        config = _settings(settings_path)
        manifest, sources, config = _freeze(source_db, preset_path, output, self_id, groups,
                                            config, moment, private_peers)
        current = CharacterPreset.model_validate_json((output / "preset.json").read_bytes())
        queue = _queue(output / "queue.sqlite3", manifest["preset_id"], self_id, groups, config, private_peers)
        queue.enqueue(now=datetime.fromisoformat(manifest["frozen_at"]), backfill=True, sources=sources)
        queue.recover(set(memory_state(current.qq_memory_state)["receipts"]))
        # A new explicit offline invocation retries its previous failed checkpoint.
        if queue.path.exists():
            with sqlite3.connect(queue.path) as connection:
                connection.execute("UPDATE qq_memory_jobs SET next_attempt_at=NULL WHERE status='retry'")
        while max_jobs is None or processed < max_jobs:
            job = queue.next_job()
            if job is None:
                break
            batch_started = time.perf_counter()
            try:
                state = memory_state(current.qq_memory_state)
                old = proposal_context(state, job)
                proposal = job.proposal
                if proposal is not None and job.proposal_basis_digest != _hash(old):
                    queue.reset_proposal(job.job_id, expected_digest=job.proposal_basis_digest)
                    proposal = None
                if proposal is None:
                    proposal = (proposer.propose(job, old_impressions=old, persona=current.persona) if proposer is not None
                                else _make_proposal(job, old, current.persona, config))
                    queue.store_proposal(job.job_id, proposal, old_impressions=old, basis_digest=_hash(old))
                committed = build_commit(current, job, proposal, config, now=now or _now())
                atomic_write_json(output / "preset.json", committed.model_dump(mode="json"))
                current = committed
                queue.acknowledge(job.job_id, set(memory_state(current.qq_memory_state)["receipts"]))
                processed += 1
            except Exception as exc:
                failures += 1
                error_code = getattr(exc, "error_code", type(exc).__name__)
                queue.fail(job.job_id, error_code)
                details = (exc.errors(include_input=False, include_context=False) if hasattr(exc, "errors")
                           else str(exc)[:2000] if isinstance(exc, ValueError) else type(exc).__name__)
                atomic_write_json(output / "last_failure.json", {"job_id": job.job_id,
                    "error_type": type(exc).__name__, "error_code": error_code, "details": details})
            progress(json.dumps({"processed": processed, "failed": failures,
                "pending": queue.status()["pending"], "retry": queue.status()["retry"],
                "duration_ms": round((time.perf_counter() - batch_started) * 1000, 3)}))
            if failures:
                break
        counts = queue.status()
        completed = not any(counts[key] for key in ("pending", "preparing", "retry"))
        if completed:
            current = _maintain(current, config, now or _now())
            atomic_write_json(output / "preset.json", current.model_dump(mode="json"))
        report = _report(current, manifest, queue, failures=failures, completed=completed,
                         elapsed=time.perf_counter() - started, processed=processed)
        atomic_write_json(output / "report.json", report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("source-db", "preset", "settings", "output", "self-id"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--group-id", action="append", default=[], dest="group_ids")
    parser.add_argument("--private-peer-id", action="append", default=[], dest="private_peer_ids")
    parser.add_argument("--max-jobs", type=int, help="Stop after this many committed windows; resume with the same output")
    parser.add_argument("--retry-failures", type=int, default=0, choices=range(0, 11),
                        help="Resume failed model checkpoints at most this many times, without changing frozen input")
    args = parser.parse_args(argv)
    retries = vars(args).pop("retry_failures")
    try:
        for attempt in range(retries + 1):
            report = run_backfill(**vars(args))
            if report["completed"] or not report["failures_this_run"]:
                break
        print(json.dumps({key: report[key] for key in ("completed", "episodes", "impressions", "stable_judgments",
                                                       "processed_this_run", "failures_this_run", "duration_ms")}))
        return 0 if report["completed"] else 2
    except Exception as exc:
        # Exception messages can contain source text, URLs, keys or model output.
        print(json.dumps({"failed": 1, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
