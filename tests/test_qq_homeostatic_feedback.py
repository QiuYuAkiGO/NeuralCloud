"""Local SQLite feedback recovery; no GUI, live model, or production state."""

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harness.qq import decision, feedback, memory
from harness.qq.draft import QQDraftError
from harness.qq.homeostatic import QQHomeostaticDriver
from harness.qq.watch_ledger import WatchEvent


START = datetime(2026, 9, 12, tzinfo=UTC)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
    clock = SimpleNamespace(wall=START, mono=100.0)

    class LocalDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.wall

    monkeypatch.setattr(memory, "datetime", LocalDateTime)
    driver = QQHomeostaticDriver(state_dir=tmp_path / "drive", development_mode=True,
        profile_path=Path("presets/homeostasis/qq-social-drive-v1.json"),
        group_id="10001", self_member_id="20001", run_id="test-run", run_async=asyncio.run,
        clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    store = memory.QQVisualMemoryStore(tmp_path / "memory.sqlite3")
    with closing(store._connect()):
        pass
    watch = tmp_path / "watch.sqlite3"
    with sqlite3.connect(watch) as db:
        db.execute("CREATE TABLE qq_watch_events (event_id TEXT PRIMARY KEY,group_id TEXT,record_json TEXT)")
    clock.wall += timedelta(seconds=120)
    clock.mono += 120
    fixture = SimpleNamespace(driver=driver, clock=clock, store=store, watch=watch)
    yield fixture
    fixture.driver.close()


def rows(case):
    with closing(case.driver._connect()) as db:
        return {row["fact_id"]: json.loads(row["record_json"]) for row in db.execute("SELECT * FROM facts")}


def source(case, *, text="A synthetic incoming question.", member="20002", group="10001",
           stored=None, observed=None, bind=True, item_changes=None):
    previous = case.clock.wall
    case.clock.wall = stored or previous
    identity = uuid4().hex
    profile = case.store.record_profile(memory.QQVerifiedProfileInput(
        group_id=group, member_id=member, display_name="Synthetic member", display_name_source="qq_profile",
        verification="qq_ui_verified", capture_id=f"profile:{identity}", observed_at=observed or case.clock.wall,
        group_evidence=["fixture://group"], member_evidence=["fixture://member"])).profile
    observation = case.store.record_observation(memory.QQVisualObservationInput(
        group_id=group, member_id=member, profile_record_id=profile.record_id,
        capture_id=f"frame:{identity}", capture_item_id=f"event:{identity}", observed_at=observed or case.clock.wall,
        text=text, evidence=[f"fixture://frame/{identity}"], author_attribution="qq_ui_verified",
        attribution_evidence=["fixture://attribution"])).observation
    case.clock.wall = previous
    if bind:
        event = WatchEvent(event_id=observation.capture_item_id, group_id=group, run_id="fixture-run",
            screen_id=observation.capture_id, stream_revision=1, item_index=0,
            item={"direction": "incoming", "kind": "text", "text": text, **(item_changes or {})},
            observed_at=observation.observed_at, state="bound", source_record_id=observation.record_id)
        with sqlite3.connect(case.watch) as db:
            db.execute("INSERT INTO qq_watch_events VALUES(?,?,?)", (event.event_id, group, event.model_dump_json()))
    return observation


def receipt(case, *, text="A complete synthetic reply.", member="20001", group="10001", moment=None, refs=None):
    return decision.record_qq_delivery(memory_db=case.store.path, group_id=group, self_member_id=member,
        delivered_text=text, evidence_refs=refs or [f"fixture://receipt/{uuid4().hex}"],
        verification="qq_ui_verified", delivered_at=moment or START + timedelta(seconds=30))


def model_decision(case, *, sources=(), receipts=(), **changes):
    identifiers = [item.record_id for item in sources]
    values = dict(decision_id=f"qq-decision:{uuid4().hex}", input_hash="a" * 64, created_at=case.clock.wall,
        group_id="10001", self_member_id="20001", participants=["20002"], action="wait", reason="Synthetic decision.",
        decision_source="model", outcome="natural_wait", source_record_ids=identifiers,
        new_observation_record_ids=identifiers, evidence_refs=["fixture://input"],
        delivery_refs=[item.receipt_id for item in receipts], addressing="direct",
        addressing_source="gate_direct_followup", addressing_source_record_ids=identifiers,
        addressing_evidence_refs=["fixture://addressing"], model="offline-fixture", persona_version="fixture",
        preset_id="fixture")
    values.update(changes)
    return decision.QQReplyDecision(**values)


def segments(case, *, count=2, complete=True, aggregate=True):
    texts = ["First complete fragment.", "Second complete fragment.", "Third complete fragment."][:count]
    plan = model_decision(case, action="speak", reply="\n\n".join(texts), reply_segments=texts, outcome="ready",
                          addressing="ambient", addressing_source="ambient", addressing_source_record_ids=[])
    with closing(decision._connect(case.store.path)) as db, db:
        db.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)",
            (plan.decision_id, plan.input_hash, plan.group_id, "completed", plan.model_dump_json()))
    prefix = []
    for index, text in enumerate(texts if complete else texts[:1]):
        prefix.append(decision.record_qq_segment_delivery(memory_db=case.store.path, group_id="10001",
            self_member_id="20001", decision_id=plan.decision_id, segment_index=index, delivered_text=text,
            verification="qq_ui_verified", evidence_refs=[f"fixture://segment/{index}/{n}" for n in range(32)],
            delivered_at=START + timedelta(seconds=30 + index)))
    final = None
    if aggregate:
        final = decision.record_qq_delivery(memory_db=case.store.path, group_id="10001", self_member_id="20001",
            decision_id=plan.decision_id, delivered_text=plan.reply, verification="qq_ui_verified",
            evidence_refs=sorted({ref for part in prefix for ref in part.evidence_refs}),
            delivered_at=START + timedelta(seconds=35))
    return prefix, final


def recover(case):
    return feedback.recover_recent_feedback(case.driver, memory_db=case.store.path, watch_db=case.watch,
                                             clock=lambda: case.clock.wall)


def interaction(case, outcome):
    return feedback.feedback_for_direct_response(case.driver, memory_db=case.store.path,
                                                  decision=outcome, clock=lambda: case.clock.wall)


def test_bound_message_recovery_retains_original_local_storage_time_and_is_idempotent(case):
    # Remote screenshot time need not agree with local wall time. Facts use the
    # immutable local storage timestamp, never the recovery pass's timestamp.
    item = source(case, observed=START - timedelta(hours=1))
    assert recover(case) == 1
    assert datetime.fromisoformat(rows(case)[item.record_id]["occurred_at"].replace("Z", "+00:00")) == item.stored_at
    case.clock.wall += timedelta(seconds=10)
    assert recover(case) == 0
    case.driver.tick()
    assert case.driver.status()["feedback_applied"] == 1
    assert case.driver.engine.snapshot.dimensions["social_connection"].value == pytest.approx(0.62)


@pytest.mark.parametrize("changes", [{"direction": "outgoing"}, {"kind": "sticker"}, {"text": "Different bubble."}])
def test_incoming_event_binding_cannot_be_replaced_by_another_visible_item(case, changes):
    source(case, item_changes=changes)
    assert recover(case) == 0
    assert rows(case) == {}


def test_unbound_own_other_group_and_old_history_cannot_earn_credit(case):
    source(case, bind=False)
    source(case, member="20001")
    source(case, group="10002")
    source(case, stored=START - timedelta(seconds=1))
    case.clock.wall = START + timedelta(seconds=600)
    source(case, stored=case.clock.wall - timedelta(seconds=301))
    assert recover(case) == 0
    assert rows(case) == {}


@pytest.mark.parametrize("target", ["profile", "observation", "capture_item"])
def test_recovery_requires_exact_historical_profile_and_row_binding(case, target):
    item = source(case)
    with sqlite3.connect(case.store.path) as db:
        if target == "profile":
            db.execute("UPDATE qq_visual_profiles SET member_id='20003' WHERE record_id=?", (item.profile_record_id,))
        elif target == "observation":
            db.execute("UPDATE qq_visual_observations SET member_id='20003' WHERE record_id=?", (item.record_id,))
        else:
            replacement = item.model_copy(update={"capture_item_id": "another-event"})
            db.execute("UPDATE qq_visual_observations SET capture_item_id=?,record_json=? WHERE record_id=?",
                       (replacement.capture_item_id, replacement.model_dump_json(), item.record_id))
    assert recover(case) == 0


@pytest.mark.parametrize("count", [1, 2, 3])
def test_segment_aggregate_recovery_counts_each_real_bubble_once_and_no_social_reward(case, count):
    prefix, final = segments(case, count=count)
    assert recover(case) == count
    assert set(rows(case)) == {item.receipt_id for item in prefix}
    assert final.receipt_id not in rows(case)
    assert all(value["evidence_refs"] == [key] for key, value in rows(case).items())
    # Same identity and proof projection as the live enqueue path.
    assert not case.driver.enqueue_fact(fact_id=prefix[0].receipt_id, kind="send_attempt",
        occurred_at=prefix[0].delivered_at, evidence_refs=[prefix[0].receipt_id])
    assert recover(case) == 0
    before = {key: value.value for key, value in case.driver.engine.snapshot.dimensions.items()}
    case.driver.tick(allow_heartbeat=False)
    assert {key: value.value for key, value in case.driver.engine.snapshot.dimensions.items()} == before


def test_partial_segment_prefix_is_a_real_attempt_without_aggregate(case):
    prefix, final = segments(case, complete=False, aggregate=False)
    assert final is None
    assert recover(case) == 1
    assert set(rows(case)) == {prefix[0].receipt_id}


def test_legacy_single_receipt_uses_durable_ref_and_cutoff(case):
    valid = receipt(case, refs=[f"fixture://proof/{index}" for index in range(32)])
    receipt(case, member="20003")
    receipt(case, group="10002")
    receipt(case, moment=START - timedelta(seconds=1))
    assert recover(case) == 1
    assert rows(case)[valid.receipt_id]["evidence_refs"] == [valid.receipt_id]
    case.clock.wall += timedelta(seconds=400)
    assert recover(case) == 0


@pytest.mark.parametrize("damage", ["hash", "prefix"])
def test_corrupt_receipt_or_missing_prefix_cannot_supply_feedback(case, damage):
    prefix, _ = segments(case)
    with sqlite3.connect(case.store.path) as db:
        if damage == "hash":
            db.execute("UPDATE qq_reply_deliveries SET receipt_hash='broken'")
        else:
            db.execute("DELETE FROM qq_reply_segment_deliveries WHERE receipt_id=?", (prefix[0].receipt_id,))
    with pytest.raises(QQDraftError):
        recover(case)
    assert rows(case) == {}


def test_exact_unique_seen_receipt_quote_records_one_real_interaction(case):
    sent = receipt(case)
    incoming = source(case, text=f'About "{sent.delivered_text}", a synthetic follow-up.')
    outcome = model_decision(case, sources=[incoming], receipts=[sent])
    assert interaction(case, outcome) == 1
    fact = next(iter(rows(case).values()))
    assert fact["kind"] == "verified_interaction"
    assert fact["related_attempt_id"] == sent.receipt_id
    assert set(fact["evidence_refs"]) == {sent.receipt_id, incoming.record_id}
    assert interaction(case, outcome) == 0
    case.driver.tick(allow_heartbeat=False)
    assert case.driver.engine.snapshot.dimensions["social_connection"].value > 0.62


def test_quote_of_complete_segment_matches_checked_aggregate_without_double_credit(case):
    _, sent = segments(case)
    incoming = source(case, text=f"A direct follow-up: {sent.delivered_segments[1]}")
    outcome = model_decision(case, sources=[incoming], receipts=[sent])
    assert interaction(case, outcome) == 1
    assert len(rows(case)) == 1
    assert len(next(iter(rows(case).values()))["evidence_refs"]) == 2


@pytest.mark.parametrize("text,reply", [("@Synthetic please respond", "A complete synthetic reply."),
    ("I saw ABC again", "ABC"), ("Only a complete fragment", "a complete fragment plus more")])
def test_bare_mention_short_or_partial_quote_never_earns_interaction(case, text, reply):
    sent = receipt(case, text=reply)
    incoming = source(case, text=text)
    outcome = model_decision(case, sources=[incoming], receipts=[sent], addressing_source="verified_self_mention")
    assert interaction(case, outcome) == 0


@pytest.mark.parametrize("change", ["not_seen", "not_new", "not_considered", "other_group", "other_self", "ambient"])
def test_direct_feedback_requires_scoped_successful_model_and_actual_new_source(case, change):
    sent = receipt(case)
    incoming = source(case, text=sent.delivered_text)
    changes = {"not_seen": {"delivery_refs": []}, "not_new": {"new_observation_record_ids": []},
        "not_considered": {"source_record_ids": []}, "other_group": {"group_id": "10002"},
        "other_self": {"self_member_id": "20003"}, "ambient": {"addressing": "ambient"}}[change]
    assert interaction(case, model_decision(case, sources=[incoming], receipts=[sent], **changes)) == 0


def test_ambiguous_matching_receipts_and_own_or_stale_sources_receive_no_credit(case):
    one, two = receipt(case), receipt(case)
    incoming = source(case, text=one.delivered_text)
    assert interaction(case, model_decision(case, sources=[incoming], receipts=[one, two])) == 0
    own = source(case, text=one.delivered_text, member="20001")
    assert interaction(case, model_decision(case, sources=[own], receipts=[one])) == 0
    case.clock.wall += timedelta(seconds=301)
    assert interaction(case, model_decision(case, sources=[incoming], receipts=[one])) == 0


def test_source_predating_actual_receipt_cannot_be_called_its_response(case):
    sent = receipt(case)
    incoming = source(case, text=sent.delivered_text, observed=sent.delivered_at - timedelta(seconds=1))
    assert interaction(case, model_decision(case, sources=[incoming], receipts=[sent])) == 0


def test_feedback_connections_cannot_write_even_accidentally(case):
    with closing(feedback._readonly(case.watch)) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM qq_watch_events")


def test_completed_direct_decision_enqueue_failure_recovers_once_after_restart(case, monkeypatch):
    sent = receipt(case)
    incoming = source(case, text=f"A synthetic reply to {sent.delivered_text}")
    outcome = model_decision(case, sources=[incoming], receipts=[sent], completed_at=case.clock.wall)
    with closing(decision._connect(case.store.path)) as db, db:
        db.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)", (
            outcome.decision_id, outcome.input_hash, outcome.group_id, "completed", outcome.model_dump_json()))

    def unavailable(**kwargs):
        raise OSError("synthetic queue unavailable")

    monkeypatch.setattr(case.driver, "enqueue_fact", unavailable)
    with pytest.raises(OSError):
        interaction(case, outcome)
    state_dir = case.driver.state_dir
    case.driver.close()
    case.driver = QQHomeostaticDriver(state_dir=state_dir, development_mode=True,
        profile_path=Path("presets/homeostasis/qq-social-drive-v1.json"), group_id="10001", self_member_id="20001",
        run_id="restarted", run_async=asyncio.run, clock=lambda: case.clock.wall, monotonic=lambda: case.clock.mono)
    assert recover(case) == 3  # One received message, actual attempt, and linked interaction.
    assert sum(fact["kind"] == "verified_interaction" for fact in rows(case).values()) == 1
    assert recover(case) == 0
    case.driver.tick(allow_heartbeat=False)
    assert case.driver.status()["feedback_applied"] == 3
