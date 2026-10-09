"""Repeated public failure notices are throttled without suppressing diagnosis."""
import asyncio
from contextlib import closing
from datetime import timedelta
import json

from harness.qq import decision
from harness.qq.direct_store import utcnow
from tests.test_qq_direct_actor import actor_factory as actor_factory, consume, incoming


def test_verified_error_notice_throttles_repeated_failures_but_not_ready_reply(actor_factory):
    actor, cognition, _ = actor_factory()
    failing = True

    def transform(result):
        if not failing:
            return result
        result = decision.apply_error_fallback(result, error_type="model_http",
            decision_source="model_error", reason="synthetic provider failure")
        with closing(decision._connect(actor.session.memory_db)) as db, db:
            db.execute("UPDATE qq_reply_decisions SET record_json=? WHERE decision_id=?",
                       (result.model_dump_json(), result.decision_id))
        return result

    cognition.transform = transform

    async def run():
        nonlocal failing
        await actor.verify_connection()
        first = await consume(actor, incoming())
        # Model a confirmed notice two minutes ago: ordinary cooldown elapsed,
        # but the new failure-notice cooldown has not.
        with closing(actor.store.connect()) as db, db:
            row = db.execute("SELECT send_id,record_json FROM qq_direct_sends WHERE status='sent'").fetchone()
            value = json.loads(row["record_json"])
            value["delivered_at"] = (utcnow() - timedelta(seconds=120)).isoformat()
            db.execute("UPDATE qq_direct_sends SET record_json=? WHERE send_id=?",
                       (json.dumps(value), row["send_id"]))
        assert actor.store.cooldown(first["group_id"], 300, text=value["text"])
        assert not actor.store.cooldown(first["group_id"], 60, text=value["text"])
        assert not actor.store.cooldown(first["group_id"], 300, text="a different reply")
        assert not actor.store.cooldown("different-conversation", 300, text=value["text"])
        second = await consume(actor, incoming(2))
        failing = False
        third = await consume(actor, incoming(3))
        return first, second, third

    first, second, third = asyncio.run(run())
    assert first["state"] == "sent"
    assert second["state"] == "rate_limited"
    assert second["terminal_reason"] == "direct_error_notice_cooldown"
    assert third["state"] == "sent"
    assert len(actor.client.sent) == 2
    with closing(decision._connect(actor.session.memory_db)) as db:
        row = db.execute("SELECT record_json FROM qq_reply_decisions WHERE decision_id=?",
                         (second["decision_id"],)).fetchone()
    assert json.loads(row[0])["error_type"] == "model_http"
