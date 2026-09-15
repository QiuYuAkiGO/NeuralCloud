"""QQ request replay seeks durable facts without rescanning the event history."""

from types import SimpleNamespace

import pytest

from harness.runtime.models import RuntimeEventType
from harness.runtime.qq_turns import QQTurnIngress
from tests.test_runtime_command_index import _event, _forbid_history_scans, _journal, _noise, _runtime


def qq_event(sequence=1):
    return _event(sequence).model_copy(update={"event_id": f"qq-turn:test-{sequence}", "event_type": RuntimeEventType.QQ_TURN,
        "payload": {"transport": "onebot", "original_request": f"request-{sequence}"}})


def accept(journal, event):
    return journal.append("event_accepted", event_id=event.event_id, data={"event": event.model_dump(mode="json")})


def complete(journal, event, *, status="completed"):
    data = {"event_id": event.event_id, "status": status, "decision_kind": "qq_prepare",
            "detail": {"decision": {"decision_id": "original-decision"}, "trace": {"trace_id": "original-trace"}}}
    return journal.append("processing_completed", event_id=event.event_id, data=data)


def test_new_qq_identities_read_no_historical_pages_or_payloads(tmp_path, monkeypatch):
    journal = _journal(tmp_path / "journal")
    _noise(journal, 1000)
    runtime = _runtime(tmp_path, journal)
    _forbid_history_scans(monkeypatch, journal)
    monkeypatch.setattr(journal, "records_after", lambda *args, **kwargs: pytest.fail("new QQ identity scanned old journal"))
    ingress = QQTurnIngress(SimpleNamespace(supervisor=runtime), None)
    for index in range(100):
        assert ingress._historical(f"qq-turn:new-{index}") == (None, None)
    assert runtime._qq_turn_event_seqs == {} and runtime._qq_turn_completed_seqs == {}


def test_restart_replay_seeks_original_accepted_and_completed_records_after_tail_eviction(tmp_path, monkeypatch):
    journal = _journal(tmp_path / "journal")
    event = qq_event()
    first = accept(journal, event)
    result = complete(journal, event)
    _noise(journal)
    runtime = _runtime(tmp_path, _journal(tmp_path / "journal"))
    _forbid_history_scans(monkeypatch, runtime.journal)
    seek = runtime.journal.records_after
    calls = []

    def record_seek(sequence, *, limit):
        calls.append((sequence, limit))
        return seek(sequence, limit=limit)

    monkeypatch.setattr(runtime.journal, "records_after", record_seek)
    ingress = QQTurnIngress(SimpleNamespace(supervisor=runtime), None)
    assert ingress._historical(event.event_id) == (event, result.data)
    assert calls == [(first.record_seq - 1, 1), (result.record_seq - 1, 1)]
    assert runtime._qq_turn_event_seqs == {event.event_id: first.record_seq}
    assert runtime._qq_turn_completed_seqs == {event.event_id: result.record_seq}
    assert all(type(sequence) is int for sequence in runtime._qq_turn_event_seqs.values())


def test_direct_journal_appends_incrementally_update_pending_and_latest_completion(tmp_path, monkeypatch):
    journal = _journal(tmp_path / "journal")
    runtime = _runtime(tmp_path, journal)
    _forbid_history_scans(monkeypatch, journal)
    event = qq_event()
    accept(journal, event)
    assert runtime.qq_turn_history(event.event_id) == (event, None)
    complete(journal, event, status="deferred")
    final = complete(journal, event)
    assert runtime.qq_turn_history(event.event_id) == (event, final.data)
    assert runtime._command_index_cursor == journal.last_record_seq


def test_later_acceptance_never_inherits_an_older_completion_for_same_identity(tmp_path):
    journal = _journal(tmp_path / "journal")
    event = qq_event()
    accept(journal, event)
    complete(journal, event)
    replacement = event.model_copy(update={"payload": {"conflicting": "new request"}})
    accept(journal, replacement)
    runtime = _runtime(tmp_path, journal)
    assert runtime.qq_turn_history(event.event_id) == (replacement, None)


def test_index_cannot_substitute_another_event_or_completion(tmp_path):
    journal = _journal(tmp_path / "journal")
    first, other = qq_event(), qq_event(2)
    accept(journal, first)
    wrong = accept(journal, other)
    runtime = _runtime(tmp_path, journal)
    runtime._qq_turn_event_seqs[first.event_id] = wrong.record_seq
    with pytest.raises(RuntimeError, match="index binding"):
        runtime.qq_turn_history(first.event_id)
