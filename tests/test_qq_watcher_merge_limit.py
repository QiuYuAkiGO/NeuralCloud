"""A second context change terminates the bounded replan, without old sends."""
import sqlite3

from tests.test_qq_watcher import setup as setup  # noqa: F401 - fixture dependency
from tests.test_qq_watcher_unattended import events, scenario as scenario, traces


def test_second_context_change_discards_both_results_without_renewing_original_deadline(scenario):
    state = scenario
    first = state.append("fictional first direct question")
    arrivals = []

    def question_changes_during_each_model_call(_kwargs):
        arrivals.append(state.append(f"fictional supplement {len(state.calls)}", after=10))
        return state.answer("speak", priority="direct", reply=f"stale reply {len(state.calls)}")

    state.callback = question_changes_during_each_model_call
    state.watcher.cycle()
    assert len(state.calls) == 1 and state.desktop.sends == 0
    state.clock.advance(7)
    state.watcher.cycle()

    # First call and the one permitted replan share the initial observation TTL,
    # despite two newer arrivals and time spent between the model requests.
    assert len(state.calls) == 2
    assert first.first_seen_mono < arrivals[0].first_seen_mono < arrivals[1].first_seen_mono
    for call in state.calls:
        assert call["trace"].first_seen_at == first.first_seen_at
        assert call["trace"].first_seen_mono == first.first_seen_mono
        assert call["deadline"].deadline_mono == first.first_seen_mono + 60
    records = events(state)
    assert len(records) == 2 and {event["state"] for event in records} == {"superseded"}
    assert {event["first_seen_at"] for event in records} == {first.first_seen_at.isoformat().replace("+00:00", "Z")}
    assert state.watcher._pending_turn is None and state.watcher._carry_first_seen is None
    assert state.desktop.sends == state.desktop.clicks == 0 and not state.receipts
    with sqlite3.connect(state.watcher.settings.state_db) as connection:
        assert connection.execute("SELECT count(*) FROM qq_watch_sends").fetchone()[0] == 0
    recorded = traces(state)
    assert len(recorded) == 2
    assert all(trace["counters"].get("turn_superseded") == 1 for trace in recorded)
    assert not any(trace["counters"].get("turn_sent") or trace["counters"].get("turn_natural_wait") for trace in recorded)
