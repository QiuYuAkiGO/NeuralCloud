"""Long-gap invalidation cannot cross the durable single-click boundary."""
from types import SimpleNamespace as NS

import pytest

from harness.qq.desktop import PreClickIdentityInvalidated
from tests.test_qq_watcher import setup as setup  # noqa: F401 - scenario dependency
from tests.test_qq_watcher_unattended import scenario as scenario  # noqa: F401 - fixture


def install_delivery_gap(state, *, repeat=False, enabled=True, epoch_only=False):
    state.watcher.settings = state.watcher.settings.model_copy(update={"identity_cache_enabled": enabled})
    state.desktop._identity_epoch = "epoch-0"
    invalidations = []

    def invalidate():
        invalidations.append(True)
        state.desktop._identity_epoch = f"epoch-{len(invalidations)}"

    state.desktop.invalidate_identity_cache = invalidate
    state.callback = lambda _: state.answer("speak", reply="synthetic reply")
    original = state.desktop.deliver
    attempts = []

    def deliver(text, **kwargs):
        attempts.append(kwargs["reservation_id"])
        before_click = kwargs.pop("before_click")

        def just_before_click():
            if repeat or len(attempts) == 1:
                if epoch_only:
                    previous = state.watcher._newer_visible_context

                    def invalidate_inside_context_check():
                        invalidate()
                        state.watcher._newer_visible_context = previous
                        return previous()

                    state.watcher._newer_visible_context = invalidate_inside_context_check
                else:
                    state.observer._mark_gap(state.clock.wall(), state.clock.mono(), None)
                    state.clock.advance(10)
                    state.observer._close_gap(state.clock.wall(), state.clock.mono())
            before_click()

        try:
            return original(text, before_click=just_before_click, **kwargs)
        except PreClickIdentityInvalidated as exc:
            # Mirror the narrowly typed backend rejection; unrelated errors escape.
            assert str(exc) == "identity_cache_invalidated_before_click"
            return NS(status="not_sent", reason_code=str(exc), phase="precheck", retryable=True,
                      evidence_refs=[state.desktop.current.evidence_ref])

    state.desktop.deliver = deliver
    return attempts, invalidations


@pytest.mark.parametrize("epoch_only", [False, True])
def test_invalidation_inside_preclick_rejects_before_commit_then_can_reverify(scenario, epoch_only):
    state = scenario
    attempts, invalidations = install_delivery_gap(state, epoch_only=epoch_only)
    arrival = state.append("synthetic current question")
    state.watcher.cycle()
    turn = state.watcher._pending_turn
    assert turn is not None and len(attempts) == 1
    reserved = state.watcher.ledger.get_send(turn.reservation_id)
    assert reserved.status == "retry_wait"
    assert reserved.reason == "identity_cache_invalidated_before_click"
    assert reserved.preclick_proof is None and reserved.click_started_at is None
    assert state.desktop.clicks == 0 and not state.receipts and len(invalidations) == 1
    assert not state.watcher.ledger.has_uncertain_send()
    original_deadline, original_first_seen = turn.deadline, turn.trace.first_seen_mono
    state.clock.advance(2)
    state.watcher.cycle()
    assert len(set(attempts)) == 1 and len(attempts) == 2
    assert state.desktop.clicks == 1 and len(state.receipts) == 1
    assert turn.deadline is original_deadline and turn.trace.first_seen_mono == original_first_seen
    assert arrival.first_seen_mono == original_first_seen


def test_repeated_preclick_gap_does_not_extend_existing_three_attempt_limit(scenario):
    state = scenario
    attempts, _ = install_delivery_gap(state, repeat=True)
    state.append("synthetic current question")
    state.watcher.cycle()
    for _ in range(2):
        state.clock.advance(2)
        state.watcher.cycle()
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert state.watcher._pending_turn is None
    assert state.watcher.ledger.get_send(attempts[0]).status == "not_sent"
    assert state.desktop.clicks == 0 and not state.receipts
    assert not state.watcher.ledger.has_uncertain_send()


def test_default_disabled_cache_keeps_existing_click_boundary(scenario):
    state = scenario
    attempts, invalidations = install_delivery_gap(state, enabled=False)
    state.append("synthetic current question")
    state.watcher.cycle()
    assert len(attempts) == 1 and not invalidations
    assert state.desktop.clicks == 1 and len(state.receipts) == 1
