"""Production feedback HTTP under the provider loop, with synthetic transport."""
import asyncio
from datetime import timedelta
import threading
import time
from types import SimpleNamespace as NS

import httpx
import pytest

from harness.homeostasis import HomeostasisEngine, load_profile
from harness.qq import homeostatic
from harness.qq.desktop import DeliveryResult
from tests.test_qq_embodied_driver import action, case

embodied_case = case


@pytest.fixture
def remote(tmp_path, monkeypatch):
    engine = HomeostasisEngine(load_profile("presets/homeostasis/qq-social-drive-v1.json"),
                              state_dir=tmp_path / "domain")
    snapshot = asyncio.run(engine.get_snapshot()).model_dump(mode="json")
    asyncio.run(engine.close())
    token = tmp_path / "token"
    token.write_text("a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2")
    evidence = NS(created=[], calls=[], closed=[], cancelled=False, block=False,
                  snapshot=snapshot, after_request=None, drivers=[])

    async def handler(request):
        evidence.calls.append((request.url.path, threading.get_ident(), id(asyncio.get_running_loop())))
        if evidence.block:
            try:
                await asyncio.sleep(60)
            finally:
                evidence.cancelled = True
        await asyncio.sleep(0)
        if evidence.after_request is not None:
            evidence.after_request()
        return httpx.Response(200, json=evidence.snapshot)

    original_client = httpx.AsyncClient

    class Client(original_client):
        def __init__(self, **kwargs):
            evidence.created.append(threading.get_ident())
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

        async def aclose(self):
            evidence.closed.append((threading.get_ident(), id(asyncio.get_running_loop())))
            await super().aclose()

    monkeypatch.setattr(homeostatic.httpx, "AsyncClient", Client)

    def make(**extra):
        driver = homeostatic.QQHomeostaticDriver(state_dir=tmp_path / "driver",
            group_id="10001", self_member_id="20001", run_id="synthetic",
            run_async=lambda _: pytest.fail("production HTTP entered the GUI provider loop"),
            shared_runtime_url="http://127.0.0.1:8770", shared_control_token_file=token, **extra)
        evidence.drivers.append(driver)
        return driver

    yield make, evidence
    for driver in evidence.drivers:
        driver.close()


def test_shared_refresh_inside_negotiated_provider_keeps_actor_and_one_io_loop(remote, embodied_case):
    make, evidence = remote
    driver = make()
    actor_thread = threading.get_ident()

    def before_click_check():
        assert asyncio.get_running_loop() is embodied_case.actor._qq_loop
        assert threading.get_ident() == actor_thread
        driver.refresh()
        assert driver.summary.snapshot_revision == evidence.snapshot["revision"]
        return DeliveryResult("not_sent", [], "synthetic_preclick_checked")

    result = action(embodied_case, execute_fn=before_click_check)
    assert result.status == "not_sent" and result.reason_code == "synthetic_preclick_checked"
    driver.refresh()
    asyncio.run(close_in_loop(driver))
    assert not embodied_case.clicks and not embodied_case.persistence
    assert len(evidence.created) == 1 and evidence.created[0] != actor_thread
    assert {item[1] for item in evidence.calls} == set(evidence.created)
    assert len({item[2] for item in evidence.calls}) == 1
    assert evidence.closed == [(evidence.created[0], evidence.calls[0][2])]
    assert not any(item.ident in evidence.created and item.is_alive() for item in threading.enumerate())
    driver.close()
    assert len(evidence.closed) == 1


async def close_in_loop(driver):
    driver.close()


def test_initial_snapshot_can_be_created_and_closed_inside_running_loop(remote):
    make, evidence = remote

    async def provider():
        driver = make()
        driver.refresh()
        driver.close()

    asyncio.run(provider())
    assert len(evidence.calls) == 2 and len(evidence.closed) == 1


def test_shared_preclick_refresh_cannot_extend_original_intent(remote, embodied_case, monkeypatch):
    make, evidence = remote
    driver = make()
    actor = embodied_case.actor
    actor._homeostasis = driver
    deadline = embodied_case.moment[0] + timedelta(seconds=10)
    intent = NS(opportunity_ref="synthetic-opportunity", scheduler_generation=1,
        homeostasis_revision=driver.summary.snapshot_revision,
        episode_signature=driver.summary.episode_signature, deadline_at=deadline)
    embodied_case.turn.event = NS(idle_intent=intent)
    actor._capability_bridge = NS(owner="shared", send_enabled=True, generation=1,
        valid_until=deadline + timedelta(seconds=120), check_claim=lambda *args, **kwargs: None)
    monkeypatch.setattr(actor.ledger, "capability_owner", lambda: None)
    monkeypatch.setattr(actor.ledger, "capability_claims", lambda: [{"opportunity_id": intent.opportunity_ref}])

    def request_finished():
        if len(evidence.calls) >= 3:  # Initial snapshot, outer check, actual callback.
            embodied_case.moment[0] = deadline + timedelta(seconds=1)

    evidence.after_request = request_finished

    def before_click_check():
        assert asyncio.get_running_loop() is actor._qq_loop
        with pytest.raises(RuntimeError, match="homeostatic_intent_stale"):
            actor._check_homeostatic_turn(embodied_case.turn)
        return DeliveryResult("not_sent", [], "synthetic_expired_intent")

    result = action(embodied_case, execute_fn=before_click_check)
    assert result.status == "not_sent" and result.reason_code == "synthetic_expired_intent"
    assert intent.deadline_at == deadline
    assert not embodied_case.clicks and not embodied_case.persistence


@pytest.mark.parametrize("tick_remaining", [None, 0.04])
def test_shared_request_preserves_three_second_and_shorter_tick_budgets(remote, tick_remaining):
    make, evidence = remote
    driver = make()
    original_snapshot = driver.snapshot
    driver._tick_deadline = None if tick_remaining is None else time.monotonic() + tick_remaining
    original_deadline = driver._tick_deadline
    evidence.block = True

    async def provider():
        with pytest.raises(TimeoutError):
            driver.refresh()

    started = time.monotonic()
    asyncio.run(provider())
    elapsed = time.monotonic() - started
    limit = 3.0 if tick_remaining is None else tick_remaining
    assert elapsed < limit + 1.0
    assert evidence.cancelled and driver.snapshot is original_snapshot
    assert driver._tick_deadline == original_deadline
    driver.close()
    assert len(evidence.closed) == 1
    assert not any(item.ident in evidence.created and item.is_alive() for item in threading.enumerate())


def test_queued_http_cannot_renew_budget_or_start_after_expiry(remote):
    make, evidence = remote
    driver = make()
    entered, release = threading.Event(), threading.Event()

    def blocker():
        entered.set()
        release.wait(1)

    pending = driver._shared._io.submit(blocker)
    assert entered.wait(1)
    calls_before = len(evidence.calls)
    timer = threading.Timer(0.08, release.set)
    timer.start()
    try:
        with pytest.raises(TimeoutError, match="request_expired"):
            driver._shared.request("GET", "/v1/homeostasis/snapshot", timeout_seconds=0.02)
        assert len(evidence.calls) == calls_before
    finally:
        release.set()
        timer.join()
        pending.result()


def test_failed_initial_snapshot_closes_owned_client_and_thread(remote):
    make, evidence = remote
    evidence.snapshot = {"invalid": True}
    with pytest.raises(ValueError):
        make()
    assert len(evidence.closed) == 1
    assert not any(item.ident in evidence.created and item.is_alive() for item in threading.enumerate())
