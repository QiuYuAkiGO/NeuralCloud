"""Real inbox/outbox and synthetic transport exercise the final send fences."""
import asyncio
from contextlib import closing

import pytest

from harness.interruption import InterruptionOptions
from harness.qq.interruption import QQTextTurns
from harness.qq.onebot import OneBotError
from tests.test_qq_direct_actor import actor_factory as actor_factory, MAIN
from tests.test_qq_private_actor import private_actor as private_actor, private_message, PRIVATE
from tests.test_qq_reply_requests import add, claim
from tests.test_text_interruption import result


def activate(actor):
    actor.settings = actor.settings.model_copy(update={"qq_reply_scheduler": "coalesced",
        "text_interruption": InterruptionOptions(mode="active", coalesce_seconds=0)})
    actor.text_turns = QQTextTurns(actor)


@pytest.mark.parametrize("outcome", ["continue", "regenerate", "wait", "invalid", "burst"])
def test_arrival_during_generation_keeps_frozen_binding(actor_factory, outcome):
    actor, cognition, _ = actor_factory()
    activate(actor)
    calls = []
    def judge(request):
        calls.append(request)
        if outcome == "invalid":
            return {}
        return result("continue" if outcome == "burst" else outcome, request.messages[-1]["id"])
    cognition.assess_text_interruption = judge
    async def run():
        await actor.verify_connection()
        first = await add(actor, 1, at=True)
        row = claim(actor)
        loop = asyncio.get_running_loop()
        async def arrive():
            for number in range(2, 8 if outcome == "burst" else 3):
                await add(actor, number)
        def during_generation():
            if len(cognition.calls) == 1:
                asyncio.run_coroutine_threadsafe(arrive(), loop).result(timeout=5)
        cognition.hook = during_generation
        await actor.process(row)
        current = actor.store.get(first["event_id"])
        if outcome in {"regenerate", "wait"}:
            assert not actor.client.sent
            assert current["terminal_reason"] == "text_interruption_" + outcome
            following = claim(actor)
            await actor.process(following)
            if outcome == "regenerate":
                assert len(actor.client.sent) == 1
                assert cognition.calls[-1]["text_interruption_context"]["original_request"] == first["text"]
                assert actor.text_turns.engine(first).store.get(following["event_id"])["checks"] == 1
            else:
                assert not actor.client.sent and len(cognition.calls) == 1
        else:
            assert current["state"] == "sent", current["terminal_reason"]
            assert len(actor.client.sent) == 1
        # No mutation of the already-running request, even for its own author.
        with closing(actor.store.connect()) as db:
            assert db.execute("SELECT revision FROM qq_reply_requests WHERE request_id=?", (row["reply_request"]["request_id"],)).fetchone()[0] == 1
        await actor.text_turns.close()
    asyncio.run(run())


def test_partial_reply_regenerates_only_remaining_and_shares_cooldown(actor_factory):
    actor, cognition, _ = actor_factory()
    activate(actor)
    cognition.texts = ["first visible", "old remainder"]
    cognition.assess_text_interruption = lambda request: result("regenerate", request.messages[-1]["id"])
    async def run():
        await actor.verify_connection()
        first = await add(actor, 1)
        async def after_send():
            actor.client.after_send = None
            await add(actor, 2)
        actor.client.after_send = after_send
        await actor.process(claim(actor))
        assert actor.store.get(first["event_id"])["state"] == "partial"
        assert len(actor.client.sent) == 1
        # Simulate a crash between verified native receipt and interruption ledger update.
        engine = actor.text_turns.engine(first)
        damaged = engine.store.get(first["event_id"])
        damaged.update(delivered=[], delivery_ids=[])
        engine.store.save(damaged)
        await actor.text_turns.close()
        actor.text_turns = QQTextTurns(actor)
        cognition.texts = ["updated remainder"]
        await actor.process(claim(actor))
        assert len(actor.client.sent) == 2
        assert cognition.calls[-1]["text_interruption_context"]["already_delivered"] == ["first visible"]
        sent_texts = [entry["message"][0]["data"]["text"] for entry in actor.client.sent.values()]
        assert sent_texts == ["first visible", "updated remainder"]
        assert actor.store.cooldown(MAIN, 60)  # Subsequent independent turns remain limited.
        await actor.text_turns.close()
    asyncio.run(run())


@pytest.mark.parametrize("fence", ["pause", "pause_resume", "attention", "permission", "unknown"])
def test_loose_mode_does_not_bypass_hard_send_checks(actor_factory, fence):
    actor, cognition, _ = actor_factory()
    activate(actor)
    async def run():
        await actor.verify_connection()
        first = await add(actor, 1)
        row = claim(actor)
        engine = actor.text_turns.engine(first)
        engine.begin("qq:" + MAIN, first["event_id"], first["text"], __import__("time").time() + 60)
        for i in range(5):
            engine.receive("qq:" + MAIN, str(i), "member", "busy")
        assert engine.store.get(first["event_id"])["loose"]
        if fence == "pause":
            actor.settings.control_file.write_text('{"paused":true}')
        elif fence == "pause_resume":
            # Bind the original control revision, then change it twice without a poll.
            saved = engine.store.get(first["event_id"])
            saved["control_fence"] = actor.text_turns.control_fence(first)
            engine.store.save(saved)
            actor.settings.control_file.write_text('{"paused":true}')
            actor.settings.control_file.write_text('{"paused":false,"stop":false}')
        elif fence == "attention":
            actor.check_attention = lambda event: (_ for _ in ()).throw(OneBotError("attention_closed"))
        elif fence == "permission":
            original = actor.current_conversations
            actor.current_conversations = lambda send=False: {} if send else original()
        else:
            actor.store.uncertain = lambda **kwargs: [{"status": "unknown"}]
        await actor.process(row)
        assert not actor.client.sent
        await actor.text_turns.close()
    asyncio.run(run())


def test_private_interruptions_are_independent_of_group_mode(private_actor):
    actor, cognition, _ = private_actor
    actor.settings = actor.settings.model_copy(update={"qq_reply_scheduler": "coalesced",
        "text_interruption_private": InterruptionOptions(mode="active", coalesce_seconds=0)})
    actor.text_turns = QQTextTurns(actor)
    cognition.assess_text_interruption = lambda request: result("regenerate", request.messages[-1]["id"])
    async def run():
        await actor.verify_connection()
        await actor.on_event(private_message())
        loop = asyncio.get_running_loop()
        def during_generation():
            if len(cognition.calls) == 1:
                asyncio.run_coroutine_threadsafe(actor.on_event(private_message(2)), loop).result(timeout=5)
        cognition.hook = during_generation
        await actor.process(claim(actor))
        assert not actor.client.sent
        await actor.process(claim(actor))
        assert len(actor.client.sent) == 1
        assert cognition.calls[-1]["group_id"] == PRIVATE
        assert cognition.calls[-1]["text_interruption_context"]
        assert actor.text_turns.engines["group"].options.mode == "off"
        assert not [entry for entry in actor.client.calls if entry[0] == "send_group_msg"]
        await actor.text_turns.close()
    asyncio.run(run())
