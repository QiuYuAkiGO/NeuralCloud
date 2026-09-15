"""Continuous psychological review is distinct from successful body execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from harness.regulation.store import STANDING_ID
from harness.runtime.goals import GoalController
from tests.test_goal_runtime_review import controller


class ReviewClock:
    def __init__(self):
        self.start = datetime.now(UTC)
        self.value = self.start

    def __call__(self):
        return self.value

    def at(self, seconds):
        self.value = self.start + timedelta(seconds=seconds)


def snapshot(clock, value, revision, *, age=0):
    return {
        "revision": revision,
        "updated_at": (clock() - timedelta(seconds=age)).isoformat(),
        "dimensions": {"competence": {"value": value, "target_band": [0.4, 0.7]}},
    }


@pytest.fixture
def review(tmp_path):
    goals, supervisor, _ = controller(tmp_path)
    supervisor.manifest.heartbeat_seconds = 5
    supervisor.capabilities.settings = SimpleNamespace(opportunity_seconds=1800)
    clock = ReviewClock()
    goals.clock = clock
    episode = {
        "id": "episode:review", "episode_id": "episode:review", "goal_id": STANDING_ID,
        "created_at": clock().isoformat(), "status": "waiting", "revision": 1,
        "baseline": snapshot(clock, 0.2, 0), "target_dimensions": ["competence"],
        "stable_since": None, "results": [], "appraisals": [],
    }
    goals.store.put("episode", episode["id"], episode)
    yield goals, supervisor, clock, episode
    goals.close()


def current(goals, episode):
    return goals.store.get("episode", episode["id"])


def evaluation(goals, episode):
    return goals.store.get("evaluation", current(goals, episode)["last_evaluation"])


def contribution(goals, episode, name, amount):
    goals.store.put("contribution", "contribution:" + name, {
        "episode_id": episode["id"], "source": name,
        "trace": {"applied_impacts": {"competence": amount}, "coupling_impacts": {}},
    })


def test_stable_requires_180_seconds_of_fresh_continuous_observations(review):
    goals, _, clock, episode = review
    for index, seconds in enumerate((0, 60, 120)):
        clock.at(seconds)
        goals.observe(snapshot(clock, 0.5, index + 1))
        assert current(goals, episode)["status"] == "waiting"
        assert evaluation(goals, episode)["outcome"] == "improved"
    clock.at(179)
    goals.observe(snapshot(clock, 0.5, 4))
    assert current(goals, episode)["status"] == "waiting"
    clock.at(180)
    goals.observe(snapshot(clock, 0.5, 5))
    final = current(goals, episode)
    assert final["status"] == "completed" and final["end_reason"] == "stability_observed"
    assert evaluation(goals, episode)["outcome"] == "stable"
    assert final["causal_effect_verified"] is False
    assert goals.store.get("goal", STANDING_ID)["status"] == "observing"


def test_observation_gap_resets_stability_hold(review):
    goals, _, clock, episode = review
    for index, seconds in enumerate((0, 60, 151, 211, 271)):
        clock.at(seconds)
        goals.observe(snapshot(clock, 0.5, index + 1))
        assert current(goals, episode)["status"] == "waiting"
        if seconds >= 151:
            assert current(goals, episode)["stable_since"] == (clock.start + timedelta(seconds=151)).isoformat()
    clock.at(331)
    goals.observe(snapshot(clock, 0.5, 6))
    assert evaluation(goals, episode)["outcome"] == "stable"


@pytest.mark.parametrize("invalid_snapshot", ["repeated_revision", "stale_timestamp", "missing_timestamp"])
def test_stale_or_repeated_snapshot_cannot_satisfy_stability(review, invalid_snapshot):
    goals, _, clock, episode = review
    goals.observe(snapshot(clock, 0.5, 1))
    before = current(goals, episode)
    for seconds in (60, 120, 180):
        clock.at(seconds)
        value = snapshot(clock, 0.5, 1 if invalid_snapshot == "repeated_revision" else seconds, age=91 if invalid_snapshot == "stale_timestamp" else 0)
        if invalid_snapshot == "missing_timestamp":
            value.pop("updated_at")
        goals.observe(value)
    assert current(goals, episode) == before
    assert len(goals.store.all("evaluation")) == 1
    clock.at(240)
    goals.observe(snapshot(clock, 0.5, 1000))
    assert current(goals, episode)["stable_since"] == clock().isoformat()
    assert current(goals, episode)["status"] == "waiting"


def test_departure_from_target_band_restarts_hold(review):
    goals, _, clock, episode = review
    for revision, seconds, value in ((1, 0, 0.5), (2, 60, 0.5), (3, 120, 0.39), (4, 180, 0.5), (5, 240, 0.5), (6, 300, 0.5)):
        clock.at(seconds)
        goals.observe(snapshot(clock, value, revision))
        assert current(goals, episode)["status"] == "waiting"
        if seconds == 120:
            assert current(goals, episode)["stable_since"] is None
    clock.at(360)
    goals.observe(snapshot(clock, 0.5, 7))
    assert evaluation(goals, episode)["outcome"] == "stable"


def completed_episode(goals, clock, episode):
    episode.update(goal_id="goal:user", status="completed", ended_at=clock().isoformat(),
        effect_status="pending", effect_review_until=(clock() + timedelta(seconds=600)).isoformat(),
        results=[{"status": "completed", "evidence_refs": ["board:independent-readback"]}])
    goals.store.put("episode", episode["id"], episode)


def test_completed_board_action_without_state_change_is_not_improvement(review):
    goals, _, clock, episode = review
    completed_episode(goals, clock, episode)
    goals.observe(snapshot(clock, 0.2, 1))
    first = evaluation(goals, episode)
    assert first["outcome"] == "unchanged"
    assert first["evidence_refs"] == ["board:independent-readback"]
    assert first["causal_effect_verified"] is False
    assert current(goals, episode)["effect_status"] == "pending"
    clock.at(600)
    goals.observe(snapshot(clock, 0.2, 2))
    final = current(goals, episode)
    assert final["status"] == "completed" and final["effect_status"] == "reviewed"
    assert final["regulation_effect"] == "unchanged"
    assert final["causal_effect_verified"] is False


def test_completed_activity_keeps_pending_effect_review_after_store_reopen(review, tmp_path):
    goals, supervisor, clock, episode = review
    completed_episode(goals, clock, episode)
    goals.observe(snapshot(clock, 0.5, 1))
    goals.close()
    recovered = GoalController(supervisor, tmp_path, clock=clock)
    try:
        for revision, seconds in ((2, 60), (3, 120), (4, 180)):
            clock.at(seconds)
            recovered.observe(snapshot(clock, 0.5, revision))
        final = current(recovered, episode)
        assert final["status"] == "completed" and final["effect_status"] == "reviewed"
        assert final["ended_at"] == episode["ended_at"]
        assert final["regulation_effect"] == "stable" and final["causal_effect_verified"] is False
        assert len(recovered.store.all("evaluation")) == 4
    finally:
        recovered.close()


def test_zero_appraisal_with_natural_recovery_is_not_self_report_only(review):
    goals, _, clock, episode = review
    episode["appraisals"] = [{"id": "appraisal:neutral"}]
    goals.store.put("episode", episode["id"], episode)
    contribution(goals, episode, "self_report", 0.0)
    contribution(goals, episode, "drift", 0.3)
    goals.observe(snapshot(clock, 0.5, 1))
    value = evaluation(goals, episode)
    assert value["outcome"] == "improved" and value["contributing_sources"] == ["drift"]
    assert value["sources"]["self_report"] == ["appraisal:neutral"]
    assert value["contributions"]["self_report"]["competence"] == 0.0
    assert value["contributions"]["drift"]["competence"] == 0.3
    assert value["causal_effect_verified"] is False


def test_self_report_only_improvement_is_labelled_even_with_successful_action_receipt(review):
    goals, _, clock, episode = review
    completed_episode(goals, clock, episode)
    episode["appraisals"] = [{"id": "appraisal:positive"}]
    goals.store.put("episode", episode["id"], episode)
    contribution(goals, episode, "self_report", 0.3)
    for revision, seconds in ((1, 0), (2, 60), (3, 120), (4, 180)):
        clock.at(seconds)
        goals.observe(snapshot(clock, 0.5, revision))
        value = evaluation(goals, episode)
        assert value["outcome"] == "self_report_only"
        assert value["contributing_sources"] == ["self_report"]
        assert value["evidence_refs"] == ["board:independent-readback"]
        assert value["causal_effect_verified"] is False
    assert current(goals, episode)["effect_status"] == "reviewed"
    assert current(goals, episode)["regulation_effect"] == "self_report_only"
