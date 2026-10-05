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


@pytest.mark.parametrize("interrupt", [False, True])
def test_contribution_recovery_reuses_episode_and_commits_cursor_with_effects(tmp_path, monkeypatch, interrupt):
    from harness.contracts.envelope import DomainCommandEnvelope, DomainCommandResult
    from tests.test_runtime_recovery import open_journal

    goals, supervisor, _ = controller(tmp_path / "goals")
    journal, recovery = open_journal(tmp_path / "runtime")
    supervisor.recovery = recovery
    now = datetime.now(UTC)
    episode = {"id": "episode-one", "created_at": now.isoformat(), "status": "paused",
               "appraisals": [], "history": "large history " * 20000}
    goals.store.put("episode", episode["id"], episode)
    for index in range(4):
        command = DomainCommandEnvelope(command_id=f"command-{index}", command_type="ApplyAppraisal",
            event_id=f"event-{index}", receive_seq=index + 1, correlation_id="recovery-test",
            runtime_instance_id="fixture", manifest_revision=1, idempotency_key=f"appraisal-{index}",
            issued_at=now, deadline=now + timedelta(minutes=1), payload={"episode_id": episode["id"]})
        result = DomainCommandResult(command_id=command.command_id, command_type=command.command_type,
            status="applied", request_hash=command.effective_request_hash(), committed_revision=index + 1,
            acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
            completed_at=now, result={"trace": {"source": "psychological:self_report", "occurred_at": now.isoformat()},
                "appraisal": {"appraisal_id": f"appraisal-{index}", "appraisal_kind": "self_report"}})
        journal.append("domain_command_intent", command_id=command.command_id,
                       data={"service": "homeostasis", "envelope": command.model_dump(mode="json")})
        journal.append("domain_command_result", command_id=command.command_id,
                       data={"service": "homeostasis", "result": result.model_dump(mode="json")})
    recovery.sync()
    loads = []
    original = goals._episodes
    monkeypatch.setattr(goals, "_episodes", lambda: loads.append(1) or original())
    put = goals.store.put

    def interrupted_put(kind, key, value, **kwargs):
        if kind == "contribution" and value["command_id"] == "command-1":
            raise ValueError("synthetic interrupted replay")
        return put(kind, key, value, **kwargs)

    try:
        if interrupt:
            monkeypatch.setattr(goals.store, "put", interrupted_put)
            with pytest.raises(ValueError, match="interrupted replay"):
                goals.recover_contributions()
            assert not goals.store.all("contribution")
            assert goals.store.get("episode", episode["id"])["appraisals"] == []
            assert not goals.store.all("runtime_recovery")
            monkeypatch.setattr(goals.store, "put", put)
            loads.clear()
        goals.recover_contributions()
        assert len(loads) == 1
        assert len(goals.store.all("contribution")) == 4
        saved = goals.store.get("episode", episode["id"])
        assert saved["status"] == "paused" and len(saved["appraisals"]) == 4
        assert goals.store.all("runtime_recovery")
        # Replaying an already committed boundary preserves both effects and audit.
        audit_before = goals.store.objects._db.execute("SELECT count(*) FROM agency_audit").fetchone()[0]
        goals.recover_contributions()
        assert goals.store.objects._db.execute("SELECT count(*) FROM agency_audit").fetchone()[0] == audit_before
    finally:
        goals.close()
        recovery.close()


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("journal_keys", [False, True])
def test_short_command_id_collision_preserves_both_events_on_recovery(tmp_path, incremental, journal_keys):
    from harness.contracts.envelope import DomainCommandEnvelope, DomainCommandResult
    from harness.runtime.capabilities import digest
    from tests.test_runtime_recovery import open_journal

    goals, supervisor, _ = controller(tmp_path / "goals")
    journal, recovery = open_journal(tmp_path / "runtime")
    supervisor.recovery = recovery
    now = datetime.now(UTC)
    episode = {"id": "episode-one", "created_at": now.isoformat(), "status": "paused", "appraisals": []}
    goals.store.put("episode", episode["id"], episode)
    receipts = []
    try:
        for n in range(2):
            command = DomainCommandEnvelope(command_id="cmd_67936771", command_type="ApplyHeartbeat",
                event_id=f"event-{n}", receive_seq=n + 1, correlation_id=f"correlation-{n}",
                runtime_instance_id="fixture", manifest_revision=1, idempotency_key=f"heartbeat-{n}",
                issued_at=now + timedelta(days=n), deadline=now + timedelta(days=n, minutes=1), payload={})
            result = DomainCommandResult(command_id=command.command_id, command_type=command.command_type,
                status="applied", request_hash=command.effective_request_hash(), committed_revision=n + 1,
                acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
                completed_at=command.issued_at, result={"trace": {"source": "heartbeat",
                    "event_id": command.event_id, "receive_seq": command.receive_seq,
                    "occurred_at": command.issued_at.isoformat(), "revision": n + 1}})
            metadata = dict(event_id=command.event_id, command_id=command.command_id,
                            idempotency_key=command.idempotency_key if journal_keys else None)
            journal.append("domain_command_intent", **metadata,
                           data={"service": "homeostasis", "envelope": command.model_dump(mode="json")})
            journal.append("domain_command_result", **metadata,
                           data={"service": "homeostasis", "result": result.model_dump(mode="json")})
            receipts.append((command, result))
            if n == 0:
                # This object predates the fix and must never be rewritten.
                goals.record_homeostasis(command, result)
                legacy = goals.store.get("contribution", digest([episode["id"], command.command_id]))
                if incremental:
                    recovery.sync()
                    goals.recover_contributions()
        recovery.sync()
        goals.recover_contributions()
        assert len(goals.store.all("contribution")) == 2
        assert goals.store.get("contribution", digest([episode["id"], command.command_id])) == legacy
        assert {c["trace"]["event_id"] for c in goals.store.all("contribution")} == {"event-0", "event-1"}
        audit_before = goals.store.objects._db.execute("SELECT count(*) FROM agency_audit").fetchone()[0]
        goals.recover_contributions()
        for command, result in receipts:
            goals.record_homeostasis(command, result)
            changed = deepcopy(result.result)
            changed["trace"]["revision"] += 10
            with pytest.raises(ValueError, match="conflicting immutable"):
                goals.record_homeostasis(command, result.model_copy(update={"result": changed}))
        assert goals.store.objects._db.execute("SELECT count(*) FROM agency_audit").fetchone()[0] == audit_before
    finally:
        goals.close()
        recovery.close()
