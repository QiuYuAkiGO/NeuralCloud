"""Per-message rollout freeze and fair mixed raw/request scheduling."""
import asyncio
from contextlib import closing
import hashlib
import json
import sqlite3

import pytest

from harness.qq import direct
from harness.qq.direct_config import DirectQQSettings
from harness.qq.direct_images import DirectImageObservation
from harness.qq.direct_store import DirectStore, normalize_message
from harness.qq.telemetry import QQDeadline
from tests.test_qq_direct_actor import MAIN, GUEST, SELF, actor_factory, incoming  # noqa: F401
from tests.test_qq_private_actor import PRIVATE, private_actor, private_message  # noqa: F401


def configure(actor, selected):
    actor.settings = DirectQQSettings.model_validate(actor.settings.model_dump() | {
        "qq_latency_conversations": selected, "qq_reply_scheduler": "coalesced",
        "qq_media_cache_enabled": True, "qq_media_parallel_enabled": True})


async def add(actor, number, *, group=MAIN, explicit=False):
    raw = incoming(number, group=group)
    if explicit:
        raw["message"].insert(0, {"type": "at", "data": {"qq": SELF}})
    await actor.on_event(raw)
    event = normalize_message(raw, self_id=SELF, groups=actor.groups)
    return actor.store.get(event["event_id"])


def finish(actor, row):
    actor.store.update(row["event_id"], state="observed")
    actor.store.reply_requests.finish(row["event"], "observed")


@pytest.mark.parametrize("selected,enabled", [(None, True), ([], False), ([f"{SELF}/{MAIN}"], True),
    ([f"{SELF}/{GUEST}"], False), ([f"20000/{MAIN}"], False)])
def test_scope_defaults_account_and_allowlist(actor_factory, selected, enabled):  # noqa: F811
    actor, _, _ = actor_factory()
    configure(actor, selected)
    assert actor.scope_policy(MAIN) == {"scheduler": "coalesced" if enabled else "shadow",
        "media_cache": enabled, "media_parallel": enabled, "image_mode": "all"}


@pytest.mark.parametrize("selected", [[MAIN], [f"{SELF}/{MAIN}"] * 2,
    [f"{SELF}/private:20000:20001"], [f"{SELF}/{MAIN}/extra"]])
def test_scope_allowlist_rejects_invalid_or_duplicate_keys(actor_factory, selected):  # noqa: F811
    actor, _, _ = actor_factory()
    with pytest.raises(ValueError):
        configure(actor, selected)


def test_private_scope_is_independent_of_group_and_other_account(private_actor):  # noqa: F811
    actor, _, _ = private_actor
    configure(actor, [f"{SELF}/{PRIVATE}"])
    async def execute():
        await actor.verify_connection()
        await actor.on_event(private_message())
        await add(actor, 2)
    asyncio.run(execute())
    assert actor.scope_policy(PRIVATE)["scheduler"] == "coalesced"
    assert actor.scope_policy(MAIN)["scheduler"] == "shadow"
    with closing(actor.store.connect()) as db:
        groups = [row[0] for row in db.execute("SELECT group_id FROM qq_reply_requests")]
    assert groups == [PRIVATE]


def test_disabling_rollout_preserves_accepted_request_and_new_raw_lane(actor_factory):  # noqa: F811
    actor, _, _ = actor_factory(guest_send=True)
    configure(actor, [f"{SELF}/{MAIN}"])
    async def execute():
        first = await add(actor, 1, explicit=True)
        await add(actor, 2)
        configure(actor, [])
        other = await add(actor, 3, group=GUEST, explicit=True)
        chosen = actor.store.pending(actor.run_id, scheduler_mode="shadow")
        assert chosen["reply_request"]["authority_anchor"] == first["event_id"]
        assert len(chosen["reply_request"]["source_event_ids"]) == 2
        assert actor.event_policy(chosen["event"])["scheduler"] == "coalesced"
        finish(actor, chosen)
        raw = actor.store.pending(actor.run_id, scheduler_mode="shadow")
        assert raw["event_id"] == other["event_id"] and "reply_request" not in raw
        assert actor.event_policy(raw["event"])["scheduler"] == "shadow"
        finish(actor, raw)
        assert actor.store.pending(actor.run_id) is None
    asyncio.run(execute())


def test_mixed_scope_rotation_and_explicit_quota_do_not_starve_raw_queue(actor_factory):  # noqa: F811
    actor, _, _ = actor_factory(guest_send=True)
    configure(actor, [f"{SELF}/{MAIN}"])
    async def execute():
        for number in range(1, 5):
            await add(actor, number, explicit=True)
        for number in range(5, 9):
            await add(actor, number, group=GUEST, explicit=True)
        ambient = await add(actor, 9, group=GUEST)
        chosen = []
        for _ in range(9):
            row = actor.store.pending(actor.run_id)
            chosen.append(row)
            finish(actor, row)
            # Re-open only the store, preserving active run and durable cursor.
            actor.store = DirectStore(actor.settings.state_db, actor.settings.evidence_dir)
        return ambient, chosen
    ambient, rows = asyncio.run(execute())
    assert [row["group_id"] for row in rows[:3]] == [MAIN, GUEST, MAIN]
    assert rows[3]["event_id"] == ambient["event_id"]
    assert [row["event"]["message_id"] for row in rows if row["group_id"] == MAIN] == ["1", "2", "3", "4"]
    assert [row["event"]["message_id"] for row in rows if row["group_id"] == GUEST
            and row["event_id"] != ambient["event_id"]] == ["5", "6", "7", "8"]
    assert len({row["event_id"] for row in rows}) == 9


def test_old_scheduler_schema_migrates_cursor_without_changing_streak(tmp_path):
    path = tmp_path / "direct.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE qq_reply_scheduler(run_id TEXT PRIMARY KEY,explicit_streak INTEGER)")
        db.execute("INSERT INTO qq_reply_scheduler VALUES('old',2)")
    store = DirectStore(path, tmp_path / "evidence")
    with closing(store.connect()) as db:
        row = dict(db.execute("SELECT * FROM qq_reply_scheduler WHERE run_id='old'").fetchone())
    assert row == {"run_id": "old", "explicit_streak": 2, "last_explicit_scope": None, "last_ambient_scope": None}


def test_media_and_trace_use_received_policy_after_configuration_changes(actor_factory, monkeypatch):  # noqa: F811
    actor, _, _ = actor_factory(guest_send=True)
    configure(actor, [f"{SELF}/{MAIN}"])
    running, peak = 0, 0
    flags = []
    async def read_image(image, **kwargs):
        nonlocal running, peak
        flags.append(kwargs["cache_enabled"])
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        return DirectImageObservation(status="unknown", error="vision_unavailable")
    monkeypatch.setattr(direct, "load_and_describe_image", read_image)
    async def execute():
        nonlocal peak
        await actor.verify_connection()
        raws = []
        for number, group in [(1, MAIN), (2, GUEST)]:
            raw = incoming(number, group=group, images=True)
            raw["message"].append({"type": "image", "data": {"file": "second.png"}})
            await actor.on_event(raw)
            event = normalize_message(raw, self_id=SELF, groups=actor.groups)
            raws.append(actor.store.get(event["event_id"]))
        configure(actor, [])
        await actor.observe(raws[0]["event"], QQDeadline.after(10))
        assert peak == 2 and flags == [True, True]
        peak = 0
        await actor.observe(raws[1]["event"], QQDeadline.after(10))
        assert peak == 1 and flags == [True, True, False, False]
        row = actor.store.pending(actor.run_id)
        await actor.process(row)
    asyncio.run(execute())
    trace = json.loads(actor.settings.state_db.with_suffix(".trace.jsonl").read_text().splitlines()[-1])
    assert trace["counters"]["scheduler_mode_coalesced"] == 1
    assert trace["counters"]["media_cache_enabled"] == trace["counters"]["media_parallel_enabled"] == 1
    expected = "qq-scope:" + hashlib.sha256((SELF + "\0" + MAIN).encode()).hexdigest()
    assert trace["conversation_key"] == expected
