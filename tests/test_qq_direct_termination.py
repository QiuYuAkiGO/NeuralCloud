"""Direct terminal diagnostics retain fixed guard reasons without reply content."""
from __future__ import annotations

import asyncio
import builtins
from contextlib import closing
import json
import sqlite3

import pytest

from harness.qq.direct_store import DirectStore
from harness.qq.onebot import OneBotError
from harness.qq.shared import SharedQQUnavailable
from harness.qq.telemetry import QQDeadlineExpired, QQTurnTrace, classify_error
from tests import test_qq_direct_actor as fixtures
from tests.test_qq_direct_actor import consume, incoming


actor_factory = fixtures.actor_factory


def _last_trace(actor):
    return json.loads(actor.settings.state_db.with_suffix(".trace.jsonl").read_text().splitlines()[-1])


@pytest.mark.parametrize("error,stage,reason", [
    (OneBotError("direct_newer_context"), "protocol.precheck", "direct_newer_context"),
    (OneBotError("direct_owner_unavailable"), "protocol.precheck", "direct_owner_unavailable"),
    (OneBotError("direct_send_budget_insufficient"), "protocol.delivery", "direct_send_budget_insufficient"),
    (OneBotError("secret-token https://private.invalid/request"), "protocol.precheck", "qq_transport_error"),
    (QQDeadlineExpired("private deadline details"), "protocol.precheck", "deadline_expired"),
])
def test_guard_reason_and_stage_survive_in_event_and_trace(actor_factory, monkeypatch, error, stage, reason):
    actor, _, _ = actor_factory()
    original = actor.check_send
    calls = 0

    def guard(*args, **kwargs):
        nonlocal calls
        calls += 1
        if stage == "protocol.precheck" or calls == 2:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(actor, "check_send", guard)

    async def run():
        await actor.verify_connection()
        return await consume(actor, incoming())

    row = asyncio.run(run())
    assert row["state"] == "failed"  # Existing lifecycle states remain compatible.
    assert row["terminal_reason"] == reason and row["terminal_stage"] == stage
    assert row["terminal_at"] is not None and not actor.client.sent
    trace = _last_trace(actor)
    assert trace["termination"] == {"reason": reason, "stage": stage}
    assert next(item for item in trace["stages"] if item["name"] == stage)["error_type"] == reason
    assert "secret-token" not in json.dumps(trace) + actor.settings.status_file.read_text()
    reopened = DirectStore(actor.store.path, actor.store.evidence_dir)
    assert reopened.get(row["event_id"])["terminal_reason"] == reason


def test_cooldown_and_delivery_success_have_distinct_persisted_outcomes(actor_factory):
    actor, _, _ = actor_factory()

    async def run():
        await actor.verify_connection()
        first = await consume(actor, incoming())
        second = await consume(actor, incoming(2))
        return first, second

    first, second = asyncio.run(run())
    assert first["state"] == "sent" and first["terminal_reason"] == "sent"
    assert second["state"] == "rate_limited" and second["terminal_reason"] == "direct_cooldown"
    assert second["terminal_stage"] == "protocol.delivery"
    assert _last_trace(actor)["termination"] == {"reason": "direct_cooldown", "stage": "protocol.delivery"}
    assert len(actor.client.sent) == 1


def test_uncertain_send_and_real_cognitive_failure_are_not_both_model_errors(actor_factory):
    actor, _, _ = actor_factory()
    actor.client.send_error = RuntimeError("private protocol reply")

    async def run():
        await actor.verify_connection()
        return await consume(actor, incoming())

    row = asyncio.run(run())
    assert row["state"] == "unknown" and row["terminal_reason"] == "direct_delivery_unknown"
    assert row["terminal_stage"] == "protocol.delivery"
    assert _last_trace(actor)["termination"]["reason"] == "direct_delivery_unknown"

    other, _, _ = actor_factory()

    def broken(**kwargs):
        raise RuntimeError("private cognitive message")

    other.cognition = broken

    async def fail():
        await other.verify_connection()
        return await consume(other, incoming())

    row = asyncio.run(fail())
    assert row["terminal_reason"] == "model_error" and row["terminal_stage"] == "protocol.shared_prepare"
    assert "private cognitive message" not in json.dumps(_last_trace(other))


def test_observation_only_keeps_its_reason_without_calling_cognition(actor_factory):
    actor, cognition, _ = actor_factory()

    async def run():
        await actor.verify_connection()
        return await consume(actor, incoming(age=90))

    row = asyncio.run(run())
    assert row["state"] == "observed" and row["terminal_reason"] == "observed_history"
    assert row["terminal_stage"] == "protocol.read" and not cognition.calls


def test_shared_service_failure_is_not_a_model_or_storage_failure(actor_factory):
    actor, _, _ = actor_factory()

    def unavailable(**kwargs):
        raise SharedQQUnavailable("private service details")

    actor.cognition = unavailable

    async def run():
        await actor.verify_connection()
        return await consume(actor, incoming())

    row = asyncio.run(run())
    assert row["terminal_reason"] == "shared_cognitive_unavailable"
    assert row["terminal_stage"] == "protocol.shared_prepare"
    trace = _last_trace(actor)
    assert next(item for item in trace["stages"] if item["name"] == row["terminal_stage"])["error_type"] == row["terminal_reason"]
    assert "private service details" not in json.dumps(trace)


def test_old_database_migrates_without_inventing_historical_failure_reasons(tmp_path):
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE qq_direct_events(event_id TEXT PRIMARY KEY,run_id TEXT,group_id TEXT,
            self_id TEXT,member_id TEXT,message_id TEXT,received_at TEXT,state TEXT,
            source_record_id TEXT,decision_id TEXT,record_json TEXT,UNIQUE(self_id,group_id,message_id))""")
        db.execute("""INSERT INTO qq_direct_events VALUES('old','run','10001','29999','20001','1',
            '2026-09-16T00:00:00+00:00','failed',NULL,NULL,'{}')""")
    store = DirectStore(path, tmp_path / "evidence")
    old = store.get("old")
    assert old["state"] == "failed"
    assert old["terminal_reason"] is old["terminal_stage"] is old["terminal_at"] is None
    store.update("old", terminal_reason="direct_newer_context", terminal_stage="protocol.precheck")
    store.update("old", state="failed")
    assert store.get("old")["terminal_reason"] == "direct_newer_context"
    with pytest.raises(ValueError):
        store.update("old", terminal_reason="private text", terminal_stage="protocol.precheck")
    with pytest.raises(ValueError):
        store.update("old", terminal_reason="direct_newer_context", terminal_stage="secret path")


def test_termination_is_additive_to_cache_telemetry_and_rejects_untrusted_labels():
    trace = QQTurnTrace()
    trace.add_counter("persona_cache_hit_tokens", 100)
    assert "termination" not in trace.snapshot()
    trace.record_termination(reason="direct_newer_context", stage="protocol.precheck")
    value = trace.snapshot()
    assert value["counters"]["persona_cache_hit_tokens"] == 100
    assert value["termination"]["reason"] == "direct_newer_context"
    assert classify_error(OneBotError("secret://value")) == "qq_transport_error"
    with pytest.raises(ValueError):
        trace.record_termination(reason="unknown secret", stage="protocol.delivery")


def test_error_classification_does_not_import_optional_onebot_transport(monkeypatch):
    original = builtins.__import__

    def without_onebot(name, *args, **kwargs):
        if name == "harness.qq.onebot":
            raise AssertionError("Generic telemetry must not import the optional transport.")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_onebot)
    assert classify_error(RuntimeError("private details")) == "model_error"
    assert classify_error(OneBotError("direct_newer_context")) == "direct_newer_context"
