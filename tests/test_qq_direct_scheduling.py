"""Reply scheduling spends the original lifetime on explicit live requests."""
from datetime import datetime, timedelta, timezone

from harness.qq import direct_store
from harness.qq.direct_store import DirectStore, normalize_message


NOW = datetime(2026, 9, 16, 7, 0, tzinfo=timezone.utc)
SELF, MEMBER, GROUP = "29999", "20001", "10001"


def enqueue(store, run, ident, *, age=0, kind="ambient", historical=False):
    member = SELF if kind == "own" else MEMBER
    message = [{"type": "text", "data": {"text": "@29999 test request"}}]
    if kind == "self_mention":
        message.insert(0, {"type": "at", "data": {"qq": SELF}})
    received = NOW - timedelta(seconds=age)
    payload = {"post_type": "message", "message_type": "group", "self_id": SELF,
               "group_id": GROUP, "user_id": member, "message_id": ident,
               "time": received.timestamp(), "message": message,
               "sender": {"user_id": member, "nickname": "test"}}
    if kind == "private":
        payload.pop("group_id")
        payload.update(message_type="private", sub_type="friend")
    event = normalize_message(payload, self_id=SELF, groups=[GROUP],
                              private_peers=[MEMBER], received_at=received)
    assert store.receive(event, run_id=run, evidence_ref=store.evidence(payload), historical=historical)
    return event


def drain(store, run):
    selected = []
    while (row := store.pending(run)) is not None:
        selected.append(row["event"])
        store.update(row["event_id"], state="observed")
    return selected


def test_fresh_explicit_first_then_ambient_expired_history_without_dropping(tmp_path, monkeypatch):
    monkeypatch.setattr(direct_store, "utcnow", lambda: NOW)
    store = DirectStore(tmp_path / "direct.sqlite3", tmp_path / "evidence")
    run = store.start_run(SELF)
    enqueue(store, run, 1, age=80, kind="self_mention")
    enqueue(store, run, 2, age=120, historical=True)
    enqueue(store, run, 3, age=50)
    enqueue(store, run, 4, age=40, kind="own")
    enqueue(store, run, 5, age=30, kind="self_mention")
    enqueue(store, run, 6, age=20, kind="private")
    enqueue(store, run, 7, age=10, kind="self_mention")
    result = drain(store, run)
    assert [item["message_id"] for item in result] == ["5", "6", "7", "3", "1", "2", "4"]
    assert result[0]["received_at"] == (NOW - timedelta(seconds=30)).isoformat()
    assert len({item["event_evidence_ref"] for item in result}) == 7


def test_explicit_request_loses_priority_at_original_deadline(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(direct_store, "utcnow", lambda: clock[0])
    store = DirectStore(tmp_path / "direct.sqlite3", tmp_path / "evidence")
    run = store.start_run(SELF)
    explicit = enqueue(store, run, 1, age=59, kind="self_mention")
    ambient = enqueue(store, run, 2, age=1)
    assert store.pending(run)["event_id"] == explicit["event_id"]
    clock[0] += timedelta(seconds=1)
    assert store.pending(run)["event_id"] == ambient["event_id"]
    assert store.get(explicit["event_id"])["state"] == "received"


def test_legacy_and_text_mentions_remain_fifo_ambient(tmp_path, monkeypatch):
    monkeypatch.setattr(direct_store, "utcnow", lambda: NOW)
    store = DirectStore(tmp_path / "direct.sqlite3", tmp_path / "evidence")
    run = store.start_run(SELF)
    legacy = enqueue(store, run, 1, age=10)
    with store.connect() as db:
        db.execute("UPDATE qq_direct_events SET record_json=json_remove(record_json,'$.protocol_addressing') "
                   "WHERE event_id=?", (legacy["event_id"],))
    enqueue(store, run, 2, age=5)
    result = drain(store, run)
    assert [item["message_id"] for item in result] == ["1", "2"]
    assert result[1]["protocol_addressing"] == "ambient"
