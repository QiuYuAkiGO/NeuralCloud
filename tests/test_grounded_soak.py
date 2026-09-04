from __future__ import annotations

import asyncio
import os
import time
from collections import deque

import pytest

from harness.body.mistyai import MistyBodyClient, MistyBodyMonitor
from harness.contracts.body import BodyStateSnapshot


@pytest.mark.soak
def test_live_mistyai_grounded_polling_soak() -> None:
    """Opt-in two-hour merge-gate soak against a real MistyAI API."""

    url = os.environ.get("NC_SOAK_MISTYAI_URL")
    token = os.environ.get("NC_SOAK_MISTYAI_TOKEN")
    body_instance_id = os.environ.get("NC_SOAK_BODY_INSTANCE_ID", "misty-body-01")
    if not url or not token:
        pytest.skip("set NC_SOAK_MISTYAI_URL and NC_SOAK_MISTYAI_TOKEN")
    duration = float(os.environ.get("NC_SOAK_DURATION_SECONDS", "7200"))
    interval = float(os.environ.get("NC_SOAK_POLL_SECONDS", "5"))

    async def scenario() -> None:
        revisions: deque[int] = deque(maxlen=max(4, int(duration / interval) + 2))
        episodes: set[str] = set()

        async def accept(snapshot: BodyStateSnapshot) -> None:
            if revisions:
                assert snapshot.revision > revisions[-1]
            revisions.append(snapshot.revision)
            episodes.add(snapshot.episode_id)

        async def no_op(*_args: object) -> None:
            return None

        monitor = MistyBodyMonitor(
            client=MistyBodyClient(url, token=token, timeout_seconds=max(10, interval)),
            body_instance_id=body_instance_id,
            poll_interval_seconds=interval,
            max_state_age_seconds=max(10, interval * 2),
            future_clock_tolerance_seconds=2,
            on_snapshot=accept,
            on_degraded=no_op,
            on_recovered=no_op,
        )
        await monitor.client.validate_contract(body_instance_id=body_instance_id)
        deadline = time.monotonic() + duration
        try:
            while time.monotonic() < deadline:
                await monitor.poll_once()
                await asyncio.sleep(min(interval, max(0, deadline - time.monotonic())))
        finally:
            await monitor.stop()
        assert revisions
        assert list(revisions) == sorted(set(revisions))
        assert episodes

    asyncio.run(scenario())
