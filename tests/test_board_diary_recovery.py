import asyncio
import json
import time

from harness.runtime.goals import _episode_context
from harness.runtime.adapters import DomainSnapshot
from harness.runtime.supervisor import RuntimeSupervisor
from harness.runtime.emotion import attach_emotion_context
from tests.test_emotion_runtime import owner, manifest, event
from tests.test_diary_controller import rig  # noqa: F401


def test_episode_projection_does_not_grow_with_failed_attempts_or_mutate_ledger():
    episode = {"episode_id": "episode:test", "steps": [{"id": "step:1"}],
               "results": [{"call_id": str(i), "error": "x" * 700} for i in range(400)],
               "seen_calls": [str(i) for i in range(400)]}
    projected = _episode_context(episode)
    assert len(json.dumps(projected).encode()) < 20_000
    assert projected["results"] == episode["results"][-16:]
    assert projected["steps"] == episode["steps"]
    assert len(episode["results"]) == 400 and len(episode["seen_calls"]) == 400


def test_emotion_attachment_refreshes_stale_cas_without_extending_deadline(tmp_path):
    async def run():
        adapter, _, _ = owner(tmp_path)
        runtime = RuntimeSupervisor(manifest=manifest(), cognitive=adapter, homeostasis=object(),
            state_dir=tmp_path / "runtime", command_timeout_seconds=60)
        runtime._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=0, payload={})
        runtime._cognitive_snapshot = await adapter.get_snapshot()
        incoming = event()
        command = runtime._make_command(incoming, command_type="PrepareGoalDecision",
            idempotency_key="goal:refresh", expected_revision=0, payload={})
        adapter._revision = 5  # A separate channel committed after Runtime last read state.
        attached = await attach_emotion_context(runtime, incoming, command)
        assert attached.expected_revision == 5
        assert attached.deadline == command.deadline
        assert attached.version_vector.homeostasis_state == command.version_vector.homeostasis_state
        assert runtime._cognitive_snapshot.revision == 5
        adapter._revision = 6
        assert adapter._preflight(attached).status == "conflict"  # A later race still conflicts.
    asyncio.run(run())


def test_diary_legacy_owner_can_schedule_when_goals_are_enabled(rig):  # noqa: F811
    async def run():
        diary, supervisor = rig
        supervisor.goals.enabled = True
        diary.next_opportunity = 0
        diary.last_activity = time.monotonic() - 120
        await diary.heartbeat()
        assert len(supervisor.events) == 1
        await diary.heartbeat()
        assert len(supervisor.events) == 1
    asyncio.run(run())

