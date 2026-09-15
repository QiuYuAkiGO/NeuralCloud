"""Production objects reuse the transactional, hash-chained GA-07 object store.

Separate object kinds preserve the experimental plan schema and its validators.
No short-lived capability offer or executable target is a durable plan step.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from harness.agency.store import GoalStore
from harness.runtime.capabilities import digest

PSYCHOLOGICAL_DIMENSIONS = (
    "cognitive_load", "safety_predictability", "competence", "autonomy", "social_connection", "novelty",
)
STANDING_ID = "psychological-balance"
FINISHED = {"completed", "abandoned", "cancelled"}


class GoalSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1, max_length=128)
    statement: str = Field(min_length=1, max_length=2000)
    success_criteria: list[str] = Field(min_length=1, max_length=16)

    @field_validator("statement", "request_id")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("goal text must not be blank")
        return value.strip()

    @field_validator("success_criteria")
    @classmethod
    def criteria(cls, values):
        if any(not v.strip() or len(v) > 600 for v in values):
            raise ValueError("invalid goal success criterion")
        return [v.strip() for v in values]


class GoalOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1, max_length=128)


class RegulationStore:
    def __init__(self, path):
        self.objects = GoalStore(path)
        if not self.objects.verify_integrity():
            raise ValueError("regulation store integrity check failed")
        if self.get("goal", STANDING_ID) is None:
            self.put("goal", STANDING_ID, dict(id=STANDING_ID, goal_id=STANDING_ID,
                statement="维持心理内稳态平衡", origin="maintenance", status="observing", revision=1,
                success_criteria=[], target_dimensions=list(PSYCHOLOGICAL_DIMENSIONS), blocked_reason=None))

    def get(self, kind, key):
        return self.objects.get("regulation_" + kind, key)

    def put(self, kind, key, value, *, immutable=False):
        return self.objects.put("regulation_" + kind, key, value, immutable=immutable)

    def all(self, kind):
        return list(self.objects.all("regulation_" + kind))

    def submit(self, request: GoalSubmission, now: datetime):
        identity = "goal:" + digest(request.request_id)[:32]
        fingerprint = digest(request.model_dump())
        with self.objects.atomic():
            old = self.get("goal", identity)
            if old:
                if old["request_hash"] != fingerprint:
                    raise ValueError("goal request id reused with different content")
                return old
            goal = dict(id=identity, goal_id=identity, statement=request.statement,
                success_criteria=request.success_criteria, origin="user_task", status="queued", revision=1,
                created_at=now.isoformat(), request_hash=fingerprint, blocked_reason=None, next_review_at=now.isoformat())
            self.put("goal", identity, goal)
            return goal

    def operation(self, identity: str, operation: Literal["pause", "resume", "cancel"], request_id: str, now: datetime):
        if identity == STANDING_ID and operation == "cancel":
            raise ValueError("the standing goal can be paused or resumed, not cancelled")
        key = digest([identity, request_id])
        with self.objects.atomic():
            previous = self.get("operation", key)
            if previous:
                if previous["operation"] != operation:
                    raise ValueError("goal operation id reused")
                return previous["result"]
            goal = self.get("goal", identity)
            if goal is None:
                raise ValueError("unknown goal")
            if goal["status"] in FINISHED:
                if not (operation == "cancel" and goal["status"] == "cancelled"):
                    raise ValueError("goal is already terminal")
            else:
                status = {"pause": "paused", "resume": "observing" if identity == STANDING_ID else "queued", "cancel": "cancelled"}[operation]
                goal.update(status=status, revision=goal["revision"] + 1,
                    blocked_reason="user_paused" if operation == "pause" else None, next_review_at=now.isoformat())
                if operation == "resume":
                    goal["no_progress"] = 0
                self.put("goal", identity, goal)
                for episode in self.all("episode"):
                    if episode["goal_id"] != identity or episode["status"] in FINISHED:
                        continue
                    episode.update(status={"pause": "paused", "resume": "waiting", "cancel": "cancelled"}[operation],
                        blocked_reason="user_paused" if operation == "pause" else None, next_review_at=now.isoformat())
                    self.put("episode", episode["id"], episode)
            self.put("operation", key, {"operation": operation, "result": goal}, immutable=True)
            return goal

    def close(self):
        self.objects.close()
