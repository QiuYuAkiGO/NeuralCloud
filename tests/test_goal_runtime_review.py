"""Independent regressions for production goal control and late receipts."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from harness.regulation.config import RegulationConfig
from harness.runtime.goals import GoalController


class Calls:
    def __init__(self):
        self.rows = {}

    def put(self, value):
        self.rows[value["opportunity_id"]] = deepcopy(value)

    def all(self):
        return deepcopy(list(self.rows.values()))


def controller(tmp_path):
    preemptions = []

    async def preempt(*, reason):
        preemptions.append(reason)

    supervisor = SimpleNamespace(
        manifest=SimpleNamespace(goal_regulation=RegulationConfig(enabled=True)),
        capabilities=SimpleNamespace(activity_revision=0, calls=Calls()),
        action_coordinator=SimpleNamespace(current=None, request_preemption=preempt),
        add_listener=lambda listener: None,
        _append_journal=lambda *args, **kwargs: None,
    )
    return GoalController(supervisor, tmp_path), supervisor, preemptions


def test_replaying_old_pause_after_resume_cannot_preempt_new_work(tmp_path):
    async def scenario():
        goals, supervisor, preemptions = controller(tmp_path)
        try:
            goal = await goals.submit({"request_id": "goal-one", "statement": "Complete the requested task",
                "success_criteria": ["The requested artifact exists"]})
            first_pause = await goals.operate(goal["id"], "pause", "pause-one")
            await goals.operate(goal["id"], "resume", "resume-one")
            supervisor.action_coordinator.current = SimpleNamespace(action_id="new-action")
            supervisor.capabilities.calls.put({"opportunity_id": "new-call", "action_id": "new-action",
                "goal_binding": {"goal_id": goal["id"]}})
            before_revision = supervisor.capabilities.activity_revision
            replay = await goals.operate(goal["id"], "pause", "pause-one")
            assert replay == first_pause
            assert goals.store.get("goal", goal["id"])["status"] == "queued"
            assert preemptions == []
            assert supervisor.capabilities.activity_revision == before_revision
        finally:
            goals.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("control_operation", ["pause", "cancel"])
def test_late_appraisal_receipt_preserves_current_control_state_and_replays_once(tmp_path, control_operation):
    async def scenario():
        goals, _, _ = controller(tmp_path)
        try:
            now = datetime.now(UTC)
            goal = await goals.submit({"request_id": "goal-one", "statement": "Review one experience",
                "success_criteria": ["An evidence review exists"]})
            episode = {"id": "episode-one", "episode_id": "episode-one", "goal_id": goal["id"],
                "created_at": (now - timedelta(seconds=1)).isoformat(), "status": "acting", "appraisals": []}
            goals.store.put("episode", episode["id"], episode)
            await goals.operate(goal["id"], control_operation, "control-one")
            command = SimpleNamespace(command_type="ApplyAppraisal", command_id="appraisal-command",
                issued_at=now, payload={"episode_id": episode["id"]})
            receipt = SimpleNamespace(status="applied", committed_revision=1, completed_at=now,
                result={"trace": {"source": "psychological:self_report", "occurred_at": now.isoformat(),
                                  "evidence_refs": ["event:one"], "applied_impacts": {"competence": 0.01}},
                        "appraisal": {"appraisal_id": "appraisal-one", "appraisal_kind": "self_report"}})
            goals.record_homeostasis(command, receipt)
            receipt.status = "idempotent_replay"
            goals.record_homeostasis(command, receipt)
            current = goals.store.get("episode", episode["id"])
            assert current["status"] == {"pause": "paused", "cancel": "cancelled"}[control_operation]
            assert len(current["appraisals"]) == 1
            assert len(goals.store.all("contribution")) == 1
        finally:
            goals.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["paused", "cancelled"])
def test_unknown_call_keeps_terminal_or_paused_episode_state(tmp_path, status):
    goals, supervisor, _ = controller(tmp_path)
    try:
        episode = {"id": "episode-one", "episode_id": "episode-one", "goal_id": "goal-one",
            "created_at": datetime.now(UTC).isoformat(), "status": status, "seen_calls": []}
        goals.store.put("episode", episode["id"], episode)
        supervisor.capabilities.calls.put({"opportunity_id": "unknown-call", "status": "uncertain",
            "goal_binding": {"goal_id": "goal-one", "episode_id": episode["id"]}})
        goals.reconcile()
        current = goals.store.get("episode", episode["id"])
        assert current["status"] == status
        assert current["unresolved_call"] == "unknown-call"
    finally:
        goals.close()
