"""Transactional goal state and append-only evidence, independent of transcripts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from harness.agency.models import AgencyOutcome, GoalCheckpoint, GoalPlan, GoalReview, PersistentGoal
from harness.research.grounded_agency.hashes import canonical_json, content_hash

TRANSITIONS = {
    "candidate": {"active", "blocked", "abandoned"},
    "active": {"paused", "completed", "abandoned", "blocked"},
    "paused": {"active", "blocked", "abandoned"},
    "blocked": {"candidate", "active", "paused", "abandoned"},
    "completed": set(), "abandoned": set(),
}


class GoalStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = RLock()
        self._transaction_depth = 0
        self._db.execute("CREATE TABLE IF NOT EXISTS agency_objects (kind TEXT, key TEXT, payload TEXT NOT NULL, PRIMARY KEY(kind,key))")
        self._db.execute("CREATE TABLE IF NOT EXISTS agency_audit (sequence INTEGER PRIMARY KEY, kind TEXT, object_key TEXT, payload TEXT, previous_hash TEXT, hash TEXT)")
        self._db.commit()
        self.closed = False

    @contextmanager
    def atomic(self):
        """Commit goal, plan and arbitration changes as one durable unit."""
        with self._lock:
            outer = self._transaction_depth == 0
            if outer:
                self._db.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
                if outer:
                    self._db.commit()
            except BaseException:
                if outer:
                    self._db.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT payload FROM agency_objects WHERE kind=? AND key=?", (kind, key)).fetchone()
            return json.loads(row[0]) if row else None

    def all(self, kind: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(json.loads(row[0]) for row in self._db.execute(
                "SELECT payload FROM agency_objects WHERE kind=? ORDER BY key", (kind,)).fetchall())

    def put(self, kind: str, key: str, payload: Any, *, immutable: bool = False) -> bool:
        encoded = canonical_json(payload)
        with self.atomic():
            prior = self.get(kind, key)
            if prior is not None:
                if canonical_json(prior) == encoded:
                    return False
                if immutable:
                    raise ValueError(f"conflicting immutable {kind} identity")
            previous = self._db.execute("SELECT hash FROM agency_audit ORDER BY sequence DESC LIMIT 1").fetchone()
            previous_hash = previous[0] if previous else "0" * 64
            sha = content_hash([kind, key, json.loads(encoded), previous_hash])
            self._db.execute("INSERT INTO agency_objects VALUES(?,?,?) ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload",
                             (kind, key, encoded))
            self._db.execute("INSERT INTO agency_audit(kind,object_key,payload,previous_hash,hash) VALUES(?,?,?,?,?)",
                             (kind, key, encoded, previous_hash, sha))
            return True

    def goal(self, goal_id: str) -> PersistentGoal:
        payload = self.get("goal", goal_id)
        if payload is None:
            raise ValueError("unknown goal")
        return PersistentGoal.model_validate(payload)

    def goals(self) -> tuple[PersistentGoal, ...]:
        return tuple(PersistentGoal.model_validate(item) for item in self.all("goal"))

    def create_goal(self, goal: PersistentGoal) -> None:
        goal = PersistentGoal.model_validate(goal.model_dump(mode="json"))
        if goal.status != "candidate" or goal.revision != 1:
            raise ValueError("new goals must be revision-1 candidates")
        if goal.parent_goal_id is not None:
            self.goal(goal.parent_goal_id)
        self.put("goal", goal.goal_id, goal, immutable=True)

    def transition(self, goal_id: str, status: str, *, evidence_refs: tuple[str, ...] = ()) -> PersistentGoal:
        with self._lock:
            goal = self.goal(goal_id)
            if status == goal.status:
                return goal
            if status not in TRANSITIONS[goal.status]:
                raise ValueError(f"illegal goal transition {goal.status}->{status}")
            if status in {"completed", "abandoned"} and not evidence_refs:
                raise ValueError("completion and abandonment require evidence")
            if status == "paused" and self.get("checkpoint", goal_id) is None:
                raise ValueError("pause requires a saved safe-point checkpoint")
            updated = PersistentGoal.model_validate({**goal.model_dump(mode="json"), "status": status,
                "revision": goal.revision + 1, "evidence_refs": tuple(dict.fromkeys((*goal.evidence_refs, *evidence_refs)))})
            self.put("goal", goal_id, updated)
            return updated

    def plan(self, goal_id: str) -> GoalPlan | None:
        raw = self.get("plan", goal_id)
        return GoalPlan.model_validate(raw) if raw is not None else None

    def save_plan(self, plan: GoalPlan) -> None:
        plan = GoalPlan.model_validate(plan.model_dump(mode="json"))
        goal = self.goal(plan.goal_id)
        if goal.status in {"completed", "abandoned"}:
            raise ValueError("cannot plan a terminal goal")
        prior = self.plan(plan.goal_id)
        if plan.revision != (prior.revision + 1 if prior else 1):
            raise ValueError("plan revision must increase exactly once")
        self.put("plan", plan.goal_id, plan)

    def checkpoint(self, goal_id: str) -> GoalCheckpoint | None:
        raw = self.get("checkpoint", goal_id)
        return GoalCheckpoint.model_validate(raw) if raw else None

    def save_checkpoint(self, checkpoint: GoalCheckpoint) -> None:
        checkpoint = GoalCheckpoint.model_validate(checkpoint.model_dump(mode="json"))
        plan = self.plan(checkpoint.goal_id)
        if plan is None or plan.revision != checkpoint.plan_revision:
            raise ValueError("checkpoint must match the current plan revision")
        identifiers = {step.step_id for step in plan.steps}
        if not set(checkpoint.completed_step_ids) <= identifiers or checkpoint.next_step_id not in identifiers | {None}:
            raise ValueError("checkpoint contains unknown plan steps")
        if checkpoint.next_step_id in checkpoint.completed_step_ids:
            raise ValueError("checkpoint cannot replay a completed step")
        self.put("checkpoint", checkpoint.goal_id, checkpoint)

    def outcome(self, outcome: AgencyOutcome) -> bool:
        outcome = AgencyOutcome.model_validate(outcome.model_dump(mode="json"))
        plan = self.plan(outcome.goal_id)
        if plan is None or outcome.step_id not in {step.step_id for step in plan.steps}:
            raise ValueError("outcome does not bind a planned step")
        existing = self.get("action_outcome", outcome.action_id)
        if existing is not None and existing.get("outcome_id") != outcome.outcome_id:
            if existing.get("state") != "unknown":
                raise ValueError("action already has a terminal outcome; repeated side effects are forbidden")
        added = self.put("outcome", outcome.outcome_id, outcome, immutable=True)
        self.put("action_outcome", outcome.action_id, outcome)
        return added

    def save_review(self, review: GoalReview) -> None:
        self.goal(review.goal_id)
        self.put("review", content_hash(review), review, immutable=True)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"schema_version": "agency-store-v1", "objects": {
                kind: {key: json.loads(payload) for _, key, payload in self._db.execute(
                    "SELECT kind,key,payload FROM agency_objects WHERE kind=? ORDER BY key", (kind,)).fetchall()}
                for (kind,) in self._db.execute("SELECT DISTINCT kind FROM agency_objects ORDER BY kind").fetchall()
            }}

    def verify_integrity(self) -> bool:
        previous = "0" * 64
        objects = {}
        for kind, key, encoded, prior, sha in self._db.execute(
            "SELECT kind,object_key,payload,previous_hash,hash FROM agency_audit ORDER BY sequence"
        ).fetchall():
            if prior != previous or sha != content_hash([kind, key, json.loads(encoded), prior]):
                return False
            previous = sha
            objects[kind, key] = encoded
        return objects == {(kind, key): payload for kind, key, payload in self._db.execute("SELECT kind,key,payload FROM agency_objects")}

    def close(self) -> None:
        if not self.closed:
            self._db.commit()
            self._db.close()
            self.closed = True
