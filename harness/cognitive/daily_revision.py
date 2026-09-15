"""Daily memory maintenance owned by Cognitive; model work never holds its lock."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gui.presets import atomic_write_json
from harness.cognitive.qq_memory import snapshot_owner
from harness.memory.revision.config import DailyRevisionConfig, load_config
from harness.memory.revision.queue import DailyRevisionQueue


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class DailyRevisionWorker:
    def __init__(self, host, *, config_path: Path, queue_path: Path, provider=None):
        self.host = host
        self.config_path = config_path
        self.default_queue_path = queue_path
        configuration_error = None
        try:
            self.config = load_config(config_path if config_path.exists() else None)
        except ValueError:
            # A broken optional maintenance configuration must leave normal
            # conversations available. It never falls back to another model.
            self.config = DailyRevisionConfig()
            configuration_error = "daily_revision_configuration_invalid"
        self.queue = DailyRevisionQueue(self.config.queue_path or queue_path)
        self.provider = provider
        self.stopping = asyncio.Event()
        self.wake = asyncio.Event()
        self._loop_task = None
        self._work_task = None
        self._tick_lock = asyncio.Lock()
        self._enqueue_lock = asyncio.Lock()
        self._scheduled_day = None
        self.current_run_id = None
        self.last_error = configuration_error

    def _receipts(self):
        return getattr(self.host.chat.memory, "daily_revision_state", {}).get("receipts", {})

    def status(self, *, details=False):
        result = {"config": self.config.public(), "configured": self.config.configured,
                  "processing": bool(self._work_task and not self._work_task.done()),
                  "current_run_id": self.current_run_id, "last_error": self.last_error,
                  "maintenance_paused": self.host.adapter._maintenance_paused,
                  "queue": self.queue.status()}
        if details:
            result["history"] = self.history()
        return result

    def history(self, limit=50):
        owner = self.host.chat.active_preset_id
        # Queue records contain private frozen inputs; the UI only receives
        # selected metadata and the evidence-bound result for this preset.
        rows = self.queue.history(limit=min(200, max(1, limit)), preset_id=owner)
        return [self._public_job(row) for row in rows if row.get("preset_id") == owner]

    def _public_job(self, row):
        fields = ("run_id", "owner_id", "preset_id", "day", "partition", "status", "dry_run",
                  "created_at", "updated_at", "attempts", "last_error", "receipt", "usage")
        value = {key: row[key] for key in fields if key in row}
        receipt = self._receipts().get(row["run_id"])
        if receipt:
            value["receipt"] = receipt
            value["status"] = receipt["status"]
        value["source_count"] = len(row.get("sources", []))
        return value

    def diff(self, run_id):
        row = self.queue.get(run_id)
        if row is None or row.get("preset_id") != self.host.chat.active_preset_id:
            raise ValueError("daily_revision_run_not_found")
        value = self._public_job(row)
        value["proposal"] = self.queue.stage(run_id, "proposal")
        value["diff"] = self.queue.stage(run_id, "diff")
        value["coverage_gaps"] = row.get("basis", {}).get("coverage_gaps", [])
        value["evidence"] = [{key: source.get(key) for key in
            ("source_id", "occurred_at", "author_principal", "kind", "text", "evidence_keys", "source_refs")}
            for source in row.get("sources", [])]
        return value

    def configure(self, payload):
        if self._work_task is not None and not self._work_task.done():
            raise ValueError("daily_revision_configuration_busy")
        values = {**self.config.model_dump(mode="json"), **payload}
        try:
            updated = DailyRevisionConfig.model_validate(values)
        except ValueError:
            raise ValueError("daily_revision_configuration_invalid") from None
        if updated.enabled:
            updated.read_api_key()
        atomic_write_json(self.config_path, updated.model_dump(mode="json"))
        self.config = updated
        self.last_error = None
        self.queue = DailyRevisionQueue(updated.queue_path or self.default_queue_path)
        self._scheduled_day = None
        self.wake.set()
        return self.status()

    async def enqueue(self, *, day=None, dry_run=False):
        if self.stopping.is_set() or self.host.adapter._maintenance_paused:
            raise ValueError("daily_revision_maintenance_paused")
        if not self.config.configured:
            raise ValueError("daily_revision_provider_not_configured")
        if type(dry_run) is not bool:
            raise ValueError("daily_revision_dry_run_invalid")
        local_now = datetime.now(ZoneInfo(self.config.timezone))
        target = date.fromisoformat(day) if day is not None else local_now.date() - timedelta(days=1)
        if target >= local_now.date():
            raise ValueError("daily_revision_requires_completed_day")
        from harness.memory.revision.sources import collect_source_window
        from harness.memory.revision.reducer import revision_context
        async with self._enqueue_lock:
            preset_id = self.host.chat.active_preset_id
            owner = self.host.shared.store.owner_id
            memory = self.host.chat.memory
            await asyncio.to_thread(self.host.shared.sync_qq)
            await asyncio.to_thread(self.host.shared.sync_local)
            sync_actions = getattr(self.host.shared, "sync_actions", None)
            if callable(sync_actions):
                await asyncio.to_thread(sync_actions)
            store = self.host.shared.store
            zone = ZoneInfo(self.config.timezone)
            start = datetime.combine(target, time.min, zone).astimezone(UTC)
            end = datetime.combine(target + timedelta(days=1), time.min, zone).astimezone(UTC)
            window = await asyncio.to_thread(collect_source_window, store, owner_id=owner,
                preset_id=preset_id, start=start, end=end,
                self_member_id=getattr(self.host.shared.qq_settings, "self_member_id", None))
            groups = defaultdict(list)
            for source in window.records:
                groups[source.scope.model_dump_json()].append(source)
            async with self.host.adapter._lock:
                if self.host.chat.memory is not memory or self.host.chat.active_preset_id != preset_id:
                    raise ValueError("daily_revision_owner_changed")
                preset = snapshot_owner(self.host)
            jobs = []
            for partition, sources in groups.items():
                context = revision_context(preset, sources)
                basis = {"context": context, "context_digest": _digest(context),
                         "watermark": window.watermark, "coverage_gaps": list(window.coverage_gaps),
                         "start": start.isoformat(), "end": end.isoformat()}
                jobs.append(await asyncio.to_thread(self.queue.enqueue,
                    owner_id=owner, preset_id=preset_id, day=target.isoformat(),
                    partition=_digest(partition), sources=[row.model_dump(mode="json") for row in sources],
                    basis=basis, dry_run=dry_run, config_digest=_digest(self.config.model_dump(mode="json"))))
            return {"accepted": True, "run_ids": [row["run_id"] for row in jobs],
                    "source_count": len(window.records), "coverage_gaps": list(window.coverage_gaps)}

    async def run_now(self, **payload):
        result = await self.enqueue(**payload)
        self.poke()
        return result

    def poke(self):
        if self.stopping.is_set() or self.host.adapter._maintenance_paused:
            return
        if self._work_task is not None and not self._work_task.done():
            return
        task = asyncio.create_task(self.tick(), name="daily-memory-revision")
        self._work_task = task
        self.host.adapter.research_tasks.add(task)
        def done(completed):
            self.host.adapter.research_tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is None and completed.result():
                self.wake.set()
        task.add_done_callback(done)

    async def retry(self, run_id):
        self.diff(run_id)  # Enforce active-preset ownership before queue mutation.
        receipt = self._receipts().get(run_id)
        if receipt:
            raise ValueError("daily_revision_committed_run_cannot_retry")
        await asyncio.to_thread(self.queue.retry, run_id)
        self.poke()
        return {"accepted": True, "run_id": run_id}

    async def rollback(self, run_id):
        from harness.memory.revision.reducer import rollback_revision
        from harness.memory.revision.ledger import install_revision
        self.diff(run_id)
        async with self.host._control_lock:
            async with self.host.adapter._lock:
                current = snapshot_owner(self.host)
                committed = rollback_revision(current, run_id)
                await asyncio.to_thread(self.host.chat.library.save, committed)
                install_revision(self.host, committed)
        await asyncio.to_thread(self.queue.tombstone, run_id)
        for key, receipt in self._receipts().items():
            if self.queue.get(key) is not None:
                if receipt["status"] == "rolled_back":
                    await asyncio.to_thread(self.queue.tombstone, key)
                elif receipt["status"] == "invalidated":
                    await asyncio.to_thread(self.queue.requeue_invalidated, key)
        self.poke()
        return self.diff(run_id)

    def _recover_queue(self):
        receipts = self._receipts()
        self.queue.recover({key for key, receipt in receipts.items() if receipt["status"] == "applied"})
        for key, receipt in receipts.items():
            if self.queue.get(key) is not None:
                if receipt["status"] == "rolled_back":
                    self.queue.tombstone(key)
                elif receipt["status"] == "invalidated":
                    self.queue.requeue_invalidated(key)

    async def _heartbeat(self, run_id, finished):
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=30)
            except TimeoutError:
                await asyncio.to_thread(self.queue.heartbeat, run_id, seconds=300)

    async def tick(self):
        from harness.memory.revision.models import RevisionProposal
        from harness.memory.revision.sources import FrozenSource
        from harness.memory.revision.reducer import build_revision, revision_context
        from harness.memory.revision.ledger import install_revision
        from harness.memory.revision.provider import DailyRevisionProvider
        async with self._tick_lock:
            if self.stopping.is_set() or self.host.adapter._maintenance_paused:
                return
            # Publication failure leaves the already saved preset installed.
            if getattr(self.host.adapter, "_daily_revision_publication_pending", False):
                async with self.host._control_lock:
                    async with self.host.adapter._lock:
                        self.host.adapter._persist_state()
                        self.host.adapter._daily_revision_publication_pending = False
                        self.host.adapter._qq_memory_publication_pending = False
                self._recover_queue()
            job = await asyncio.to_thread(self.queue.claim, owner_id=self.host.shared.store.owner_id,
                                          preset_id=self.host.chat.active_preset_id)
            if job is None:
                return False
            run_id = job["run_id"]
            self.current_run_id = run_id
            finished = asyncio.Event()
            heartbeat = asyncio.create_task(self._heartbeat(run_id, finished))
            phase = "prepare"
            try:
                if job["preset_id"] != self.host.chat.active_preset_id:
                    raise ValueError("daily_revision_owner_changed")
                receipt = self._receipts().get(run_id)
                if receipt:
                    if receipt["status"] == "applied":
                        await asyncio.to_thread(self.queue.finish, run_id, "applied", receipt=receipt)
                    else:
                        await asyncio.to_thread(self.queue.tombstone, run_id)
                    self.last_error = None
                    return
                memory = self.host.chat.memory
                sources = [FrozenSource.model_validate(row) for row in job["sources"]]
                async with self.host.adapter._lock:
                    before = snapshot_owner(self.host)
                context = revision_context(before, sources)
                frozen = await asyncio.to_thread(self.queue.stage, run_id, "proposal")
                if _digest(context) != job["basis"]["context_digest"]:
                    job["basis"] = {**job["basis"], "context": context, "context_digest": _digest(context)}
                    await asyncio.to_thread(self.queue.rebase, run_id, job["basis"])
                    frozen = None
                if frozen is None:
                    provider = self.provider or DailyRevisionProvider(self.config, self.queue)
                    try:
                        proposal = await asyncio.to_thread(provider.propose, run_id, sources, context)
                    finally:
                        if self.provider is None:
                            await asyncio.to_thread(provider.close)
                    await asyncio.to_thread(self.queue.checkpoint, run_id, "proposal", proposal.model_dump(mode="json"))
                else:
                    proposal = RevisionProposal.model_validate(frozen)
                if self.stopping.is_set() or self.host.adapter._maintenance_paused:
                    raise ValueError("daily_revision_maintenance_paused")
                async with self.host._control_lock:
                    async with self.host.adapter._lock:
                        if self.host.chat.memory is not memory or self.host.chat.active_preset_id != job["preset_id"]:
                            raise ValueError("daily_revision_owner_changed")
                        current = snapshot_owner(self.host)
                        latest = revision_context(current, sources)
                        if _digest(latest) != job["basis"]["context_digest"]:
                            await asyncio.to_thread(self.queue.rebase, run_id,
                                {**job["basis"], "context": latest, "context_digest": _digest(latest)})
                            raise ValueError("daily_revision_basis_changed")
                        phase = "validate"
                        committed = build_revision(current, job, proposal, self.host.chat.settings)
                        phase = "commit"
                        diff = {}
                        # Store only serializable changed memory collections, so
                        # dry runs are inspectable without touching live memory.
                        for field in ("episodic", "candidates", "semantic", "relationships", "qq_memory_state"):
                            prior = current.model_dump(mode="json")[field]
                            after = committed.model_dump(mode="json")[field]
                            if prior != after:
                                diff[field] = {"before": prior, "after": after}
                        await asyncio.to_thread(self.queue.checkpoint, run_id, "diff", diff)
                        if job.get("dry_run"):
                            await asyncio.to_thread(self.queue.finish, run_id, "dry_run")
                        else:
                            await asyncio.to_thread(self.host.chat.library.save, committed)
                            install_revision(self.host, committed)
                if not job.get("dry_run"):
                    await asyncio.to_thread(self.queue.finish, run_id, "applied", receipt=self._receipts()[run_id])
                self.last_error = None
            except Exception as exc:
                code = getattr(exc, "error_code", None) or (str(exc) if isinstance(exc, ValueError)
                    and str(exc).startswith("daily_revision_") else type(exc).__name__)
                self.last_error = code
                rejected = phase == "validate" and isinstance(exc, ValueError)
                if rejected:
                    await asyncio.to_thread(self.queue.reject_proposal, run_id, code)
                if getattr(exc, "blocked", False):
                    await asyncio.to_thread(self.queue.finish, run_id, "blocked", receipt={"error_code": code})
                else:
                    await asyncio.to_thread(self.queue.fail, run_id, code,
                        max_attempts=self.config.max_attempts,
                        retry_after_seconds=getattr(exc, "retry_after_seconds", None),
                        retryable=not rejected and getattr(exc, "retryable", code != "daily_revision_owner_changed"))
            finally:
                finished.set()
                await heartbeat
                self.current_run_id = None
            return True

    async def start(self):
        if self._loop_task is None or self._loop_task.done():
            self.stopping.clear()
            self._recover_queue()
            self._loop_task = asyncio.create_task(self._loop(), name="daily-memory-schedule")

    async def _loop(self):
        while not self.stopping.is_set():
            try:
                if not self.host.adapter._maintenance_paused:
                    now = datetime.now(ZoneInfo(self.config.timezone))
                    if (self.config.enabled and self.config.configured
                            and now.strftime("%H:%M") >= self.config.schedule_time
                            and self._scheduled_day != now.date()):
                        for offset in range(self.config.backfill_days, 0, -1):
                            await self.enqueue(day=(now.date() - timedelta(days=offset)).isoformat())
                        self._scheduled_day = now.date()
                    # enabled governs the daily schedule. Explicitly requested
                    # jobs and interrupted/retrying jobs still finish normally.
                    if self.config.configured:
                        self.poke()
            except Exception as exc:
                self.last_error = type(exc).__name__
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=30)
            except TimeoutError:
                pass

    async def close(self):
        self.stopping.set()
        self.wake.set()
        if self._loop_task is not None:
            await asyncio.shield(self._loop_task)
        if self._work_task is not None:
            # asyncio.to_thread cancellation does not stop the underlying HTTP
            # call. Keep the task and owner lease until that call has settled.
            await asyncio.shield(self._work_task)
