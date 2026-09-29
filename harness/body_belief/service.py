"""Runtime-owned terminal decisions become experiences at the next observation."""

from __future__ import annotations

import asyncio
import time

from harness.body_belief.compiler import compile_body_memory
from harness.body_belief.consolidation import BodyBeliefConsolidator
from harness.body_belief.models import BodyExperienceEpisode, context_signature
from harness.body_belief.retrieval import retrieve_beliefs, retrieve_experiences
from harness.research.grounded_agency.hashes import content_hash
from harness.research.interoception.contracts import OpaqueInteroceptiveObservation


class BodyBeliefService:
    def __init__(self, store, *, appraiser=None, profile=None, memory_treatment="none", objective_history_visible=True):
        if memory_treatment not in {"none", "objective_events", "body_causal_beliefs"}:
            raise ValueError("unknown body memory treatment")
        if memory_treatment == "body_causal_beliefs" and appraiser is None:
            raise ValueError("body_causal_beliefs requires an explicitly configured model appraiser")
        self.store = store
        self.appraiser = appraiser
        self.memory_treatment = memory_treatment
        if type(objective_history_visible) is not bool:
            raise ValueError("objective_history_visible must be a boolean")
        self.objective_history_visible = objective_history_visible
        self.consolidator = BodyBeliefConsolidator(store, profile=profile)
        self._paused = False
        self._closed = False
        self._tasks = {}
        self._completed = {}
        self._retry_handle = None

    def record_decision(
        self, *, decision_id, episode_id, context_hash, context_key, observation_before,
        decision, action_outcome_refs=(), world_change_refs=(), outcome_success=True,
        outcome_verified=True, completed_at,
    ) -> bool:
        if self._closed:
            raise RuntimeError("body belief service is closed")
        if observation_before is None:
            return False
        pending = BodyExperienceEpisode(
            experience_id="experience-" + content_hash((episode_id, decision_id))[:32],
            episode_id=episode_id, decision_id=decision_id, context_hash=context_hash,
            context_signature=context_signature(context_key, observation_before),
            observation_before=observation_before, decision=decision,
            action_outcome_refs=tuple(action_outcome_refs), world_change_refs=tuple(world_change_refs),
            outcome_success=outcome_success, outcome_verified=outcome_verified, completed_at=completed_at,
        )
        old = next((item for item in self.store.experiences() if item.decision_id == decision_id), None)
        if old is not None:
            if old.model_dump(exclude={"observation_after"}) != pending.model_dump(exclude={"observation_after"}):
                raise ValueError("completed decision cannot be rebound to different evidence")
            return False
        existing = any(item["decision_id"] == decision_id for item in self.store.pending())
        self.store.put_pending(decision_id, pending.model_dump(mode="json"))
        return not existing

    async def observe(self, observation: OpaqueInteroceptiveObservation):
        if self._closed:
            raise RuntimeError("body belief service is closed")
        observation = OpaqueInteroceptiveObservation.model_validate(observation.model_dump(mode="python"))
        formed = []
        for raw in self.store.pending():
            pending = BodyExperienceEpisode.model_validate(raw)
            if (observation.sample_sequence <= pending.observation_before.sample_sequence
                    or observation.observed_at < pending.completed_at):
                continue
            episode = BodyExperienceEpisode.model_validate({**pending.model_dump(), "observation_after": observation})
            if self.store.finalize_pending(episode, appraise=self.memory_treatment == "body_causal_beliefs"):
                formed.append(episode)
        if not self._paused:
            self.consolidator.refresh_all()
            self._schedule()
        return tuple(formed)

    def _schedule(self):
        if self._paused or self._closed or self.appraiser is None or self.memory_treatment != "body_causal_beliefs":
            return
        if self._tasks:
            return
        row = self.store.db.execute(
            "SELECT w.id FROM appraisal_work w LEFT JOIN appraisal_retry r ON w.id=r.id "
            "WHERE r.retry_at IS NULL OR r.retry_at<=? ORDER BY COALESCE(r.retry_at,0),w.id LIMIT 1",
            (time.time(),),
        ).fetchone()
        if row is not None:
            identifier = row[0]
            self._tasks[identifier] = asyncio.create_task(self._appraise(identifier))
        elif self._retry_handle is None:
            due = self.store.db.execute("SELECT MIN(r.retry_at) FROM appraisal_retry r JOIN appraisal_work w ON r.id=w.id").fetchone()[0]
            if due is not None:
                self._retry_handle = asyncio.get_running_loop().call_later(max(0.1, due - time.time()), self._retry)

    def _retry(self):
        self._retry_handle = None
        self._schedule()

    def _failed(self, identifier, error_type):
        with self.store.db:
            old = self.store.db.execute("SELECT attempts FROM appraisal_retry WHERE id=?", (identifier,)).fetchone()
            attempts = (old[0] if old else 0) + 1
            self.store.db.execute("INSERT OR REPLACE INTO appraisal_retry VALUES (?,?,?,?)",
                                  (identifier, attempts, time.time() + min(300, 2 ** min(attempts, 8)), error_type))

    async def _appraise(self, identifier):
        try:
            if self._paused:
                return
            episode = next(item for item in self.store.experiences() if item.experience_id == identifier)
            experiences = retrieve_experiences(self.store.experiences(), context_signature=episode.context_signature,
                                              action_signature=episode.action_signature)
            existing = retrieve_beliefs(self.store.beliefs(), context_signature=episode.context_signature)
            result = self._completed.pop(identifier, None)
            if result is None:
                result = await asyncio.to_thread(self.appraiser.appraise, experiences, existing_beliefs=existing)
            if self._paused:
                self._completed[identifier] = result
                return
            self.store.record_audit({"event": "appraisal", "experience_id": identifier, "result": result})
            if result.success:
                for candidate in result.candidates:
                    self.consolidator.apply(candidate)
                with self.store.db:
                    self.store.db.execute("DELETE FROM appraisal_work WHERE id=?", (identifier,))
                    self.store.db.execute("DELETE FROM appraisal_retry WHERE id=?", (identifier,))
            else:
                self._failed(identifier, "appraisal_unsuccessful")
        except Exception as exc:
            self.store.record_audit({"event": "appraisal_failed", "experience_id": identifier, "error_type": type(exc).__name__})
            self._failed(identifier, type(exc).__name__)
        finally:
            self._tasks.pop(identifier, None)
            self._schedule()

    def visible_context(self, observation, *, context_hash=None, context_key, action_signatures=(), max_tokens=4096):
        if self.memory_treatment == "none" or observation is None:
            return None
        signature = context_signature(context_key, observation)
        if not self._paused:
            self.consolidator.refresh_all()
        experiences = retrieve_experiences(self.store.experiences(), context_signature=signature)
        beliefs = retrieve_beliefs(self.store.beliefs(), context_signature=signature, action_signatures=action_signatures)
        return compile_body_memory(treatment=self.memory_treatment, experiences=experiences, beliefs=beliefs,
                                   max_tokens=max_tokens, objective_history_visible=self.objective_history_visible)

    def snapshot(self):
        return self.store.snapshot()

    def restore(self, snapshot, *, include_beliefs=True):
        if self._tasks:
            raise RuntimeError("drain appraisal before restoring a body belief snapshot")
        return self.store.restore(snapshot, include_beliefs=include_beliefs)

    async def pause(self):
        self._paused = True
        if self._retry_handle is not None:
            self._retry_handle.cancel()
            self._retry_handle = None

    async def resume(self):
        if self._closed:
            raise RuntimeError("body belief service is closed")
        self._paused = False
        self.consolidator.refresh_all()
        self._schedule()

    async def drain(self, timeout=5.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while self._tasks:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            _, pending = await asyncio.wait(tuple(self._tasks.values()), timeout=remaining)
            if pending:
                return False
        return True

    async def close(self):
        if self._closed:
            return
        await self.pause()
        if not await self.drain(30.0):
            raise RuntimeError("body appraisal did not drain; store remains open")
        self._closed = True
        self.store.close()
