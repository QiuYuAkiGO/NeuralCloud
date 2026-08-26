"""Behavioral watchdog that only emits structured decisions."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from harness.runtime.models import (
    ActionExecutionState,
    QueueStatistics,
    WatchdogAction,
    WatchdogDecision,
)


class RuntimeWatchdog:
    def __init__(
        self,
        *,
        queue_high_samples: int = 3,
        consecutive_failure_limit: int = 3,
        reconciliation_limit: int = 8,
        repeated_goal_limit: int = 3,
    ) -> None:
        self.queue_high_samples = queue_high_samples
        self.consecutive_failure_limit = consecutive_failure_limit
        self.reconciliation_limit = reconciliation_limit
        self.repeated_goal_limit = repeated_goal_limit
        self._queue_high_count = 0
        self._failures: dict[str, int] = defaultdict(int)
        self._goal_evidence: dict[str, tuple[str, int]] = {}

    def record_success(self, dependency: str) -> None:
        self._failures[dependency] = 0

    def record_failure(self, dependency: str) -> None:
        self._failures[dependency] += 1

    def record_goal_attempt(self, goal_id: str, *, evidence_fingerprint: str) -> None:
        previous = self._goal_evidence.get(goal_id)
        count = previous[1] + 1 if previous and previous[0] == evidence_fingerprint else 1
        self._goal_evidence[goal_id] = (evidence_fingerprint, count)

    def evaluate(
        self,
        *,
        queue: QueueStatistics,
        current_action: dict[str, Any] | None = None,
        reconciliation_backlog: int = 0,
        now: datetime | None = None,
    ) -> WatchdogDecision | None:
        moment = now or datetime.now(UTC)
        if queue.size >= queue.high_watermark:
            self._queue_high_count += 1
        else:
            self._queue_high_count = 0
        if self._queue_high_count >= self.queue_high_samples:
            return WatchdogDecision(
                action=WatchdogAction.PAUSE,
                reason="event queue remained above high watermark",
                evidence=queue.model_dump(mode="json"),
                observed_at=moment,
            )

        unhealthy = {
            name: count
            for name, count in self._failures.items()
            if count >= self.consecutive_failure_limit
        }
        if unhealthy:
            return WatchdogDecision(
                action=WatchdogAction.PAUSE,
                reason="critical dependency repeatedly failed",
                evidence={"consecutive_failures": unhealthy},
                observed_at=moment,
            )

        if reconciliation_backlog >= self.reconciliation_limit:
            return WatchdogDecision(
                action=WatchdogAction.PAUSE,
                reason="reconciliation backlog exceeded limit",
                evidence={"backlog": reconciliation_backlog},
                observed_at=moment,
            )

        repeated = {
            goal_id: count
            for goal_id, (_, count) in self._goal_evidence.items()
            if count >= self.repeated_goal_limit
        }
        if repeated:
            return WatchdogDecision(
                action=WatchdogAction.PAUSE,
                reason="goal repeated without new evidence",
                evidence={"goals": repeated},
                observed_at=moment,
            )

        if current_action:
            state = current_action.get("state")
            deadline = current_action.get("deadline")
            if isinstance(deadline, str):
                deadline = datetime.fromisoformat(deadline)
            active = state in {
                ActionExecutionState.RUNNING,
                ActionExecutionState.VERIFYING,
                "RUNNING",
                "VERIFYING",
            }
            if active and isinstance(deadline, datetime) and moment >= deadline:
                return WatchdogDecision(
                    action=WatchdogAction.PAUSE,
                    reason="foreground action exceeded deadline",
                    evidence={"action_id": current_action.get("action_id")},
                    observed_at=moment,
                )
        return None
