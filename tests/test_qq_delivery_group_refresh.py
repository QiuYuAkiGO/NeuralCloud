"""A sender must reacquire group evidence after attribution and model latency.

Synthetic coordinator tests pair with DesktopBackend's existing tests that
reject copied/stale group objects and revalidate context before any send click.
"""
from contextlib import nullcontext
from types import SimpleNamespace as NS

import pytest

from harness.qq.telemetry import QQDeadlineExpired
from tests.test_qq_segment_watcher import case as case, children, parent
from tests.test_qq_watcher import setup as setup
from tests.test_qq_watcher_unattended import scenario as scenario


def enforce_current_group(desktop, monkeypatch, *, after_refresh=lambda: None):
    """Model the backend's exact-object contract, including its None branch."""
    verify, attribute, deliver = desktop.verify_group, desktop.attribute, desktop.deliver
    evidence = NS(issued=[], replaced=[], supplied=[], refreshed=[])

    def current_group(*args, **kwargs):
        value = verify(*args, **kwargs)
        desktop._group = value
        evidence.issued.append(value)
        return value

    def attribution(*args, **kwargs):
        result = attribute(*args, **kwargs)
        previous = desktop._group
        current_group()
        evidence.replaced.append(previous)
        return result

    def current_delivery(*args, group_verification=None, **kwargs):
        evidence.supplied.append(group_verification)
        if group_verification is None:
            group_verification = current_group()
            evidence.refreshed.append(group_verification)
            after_refresh()
        if group_verification is not desktop._group:
            return NS(status="not_sent", reason_code="incoming_group_verification_expired",
                      phase="precheck", retryable=True, evidence_refs=[])
        return deliver(*args, group_verification=group_verification, **kwargs)

    monkeypatch.setattr(desktop, "verify_group", current_group)
    monkeypatch.setattr(desktop, "attribute", attribution)
    monkeypatch.setattr(desktop, "deliver", current_delivery)
    return evidence


def test_first_direct_send_refreshes_group_replaced_during_attribution(scenario, monkeypatch):
    state = scenario
    evidence = enforce_current_group(state.desktop, monkeypatch)
    arrival = state.append("虚构的直接提问")

    def decide(_):
        state.clock.advance(46)
        return state.answer("speak", priority="direct", reply="测试回复")

    state.callback = decide
    state.watcher.cycle()
    assert evidence.replaced and evidence.supplied == [None]
    assert len(evidence.refreshed) == 1
    assert state.desktop.clicks == len(state.receipts) == 1
    assert state.calls[0]["deadline"].deadline_mono == arrival.first_seen_mono + 60


def test_every_segment_refreshes_group_without_replaying_a_click(case, monkeypatch):
    evidence = enforce_current_group(case.desktop, monkeypatch)
    case.watcher.cycle()
    assert evidence.replaced and evidence.supplied == [None] * 3
    assert len(evidence.refreshed) == 3
    assert len(case.desktop.clicked) == 3
    assert all(item.status == "sent" and item.attempt_count == 1 for item in children(case))
    assert parent(case)["status"] == "sent"


@pytest.mark.parametrize("segmented", [False, True])
def test_group_refresh_does_not_renew_original_deadline(request, monkeypatch, segmented):
    state = request.getfixturevalue("case" if segmented else "scenario")
    advance = state.clock.sleep if segmented else state.clock.advance
    enforce_current_group(state.desktop, monkeypatch, after_refresh=lambda: advance(61))
    if not segmented:
        state.append("虚构的过期前提问")
        state.callback = lambda _: state.answer("speak", reply="不能迟发")
    # The legacy synthetic desktop propagates the callback exception; the
    # production DesktopBackend and segmented fake return a failed precheck.
    with nullcontext() if segmented else pytest.raises(QQDeadlineExpired):
        state.watcher.cycle()
    assert not (state.desktop.clicked if segmented else state.desktop.clicks)
    assert state.watcher._pending_turn is None
    if segmented:
        assert not any(item.status == "sent" for item in children(state))
