"""Settlement invalidates pre-cost body evidence even after a lost response."""

import asyncio
from types import SimpleNamespace

import pytest

from harness.production.runtime import ProductionRuntime
from harness.runtime.models import utc_now
from tests.test_production_runtime import FakeSupervisor


@pytest.mark.parametrize("receipt_lost", [False, True])
@pytest.mark.parametrize("snapshot_unavailable", [False, True])
def test_settlement_never_reuses_pre_cost_body(tmp_path, receipt_lost, snapshot_unavailable):
    class Body:
        def __init__(self):
            self.exhausted = False
            self.samples = 0

        async def snapshot(self):
            if self.exhausted and snapshot_unavailable:
                raise TimeoutError("body capture unavailable after settlement")
            self.samples += 1
            return {"body_instance_id": "body-test", "episode_id": "episode", "sample_sequence": self.samples,
                "captured_at": utc_now().isoformat(), "physiology_mode": "production_v1", "exhausted": self.exhausted,
                "resting": False, "energy": {"available_units": 0 if self.exhausted else 1}}

        async def record_usage(self, command):
            self.exhausted = True
            if receipt_lost:
                raise TimeoutError("applied cost but response lost")
            return SimpleNamespace(model_dump=lambda **_: {"inference_id": command.inference_id})

    async def run():
        runtime = ProductionRuntime(FakeSupervisor(tmp_path))
        body = Body()
        runtime.attach_body_provider(body)
        assert not (await runtime.context("owner"))["restricted_dialogue"]
        runtime.store.reserve_attempt("spent", purpose="review")
        runtime.store.record_usage("spent", {"source": "api", "raw_usage": {"total_tokens": 1000},
            "body_instance_id": "body-test", "episode_id": "episode"})
        await runtime.flush_usage()
        current = runtime.context_snapshot("owner")
        assert current["restricted_dialogue"]
        if snapshot_unavailable:
            assert not current["body"]["available"] and current["body"]["snapshot"] == {}
        else:
            assert current["body"]["snapshot"]["exhausted"]
            assert current["body"]["snapshot"]["energy"]["available_units"] == 0
        assert runtime.store.pending_usage_count() == int(receipt_lost)

    asyncio.run(run())


def test_context_capture_waits_until_inflight_settlement_finishes(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        class Body:
            exhausted = False

            async def snapshot(self):
                return {"body_instance_id": "body-test", "episode_id": "episode", "sample_sequence": 1,
                    "captured_at": utc_now().isoformat(), "physiology_mode": "production_v1",
                    "exhausted": self.exhausted, "resting": False, "energy": {}}

            async def record_usage(self, command):
                started.set()
                await release.wait()
                self.exhausted = True
                return SimpleNamespace(model_dump=lambda **_: {"inference_id": command.inference_id})

        runtime = ProductionRuntime(FakeSupervisor(tmp_path))
        runtime.attach_body_provider(Body())
        await runtime.refresh_body()
        runtime.store.reserve_attempt("spent", purpose="review")
        runtime.store.record_usage("spent", {"body_instance_id": "body-test", "episode_id": "episode"})
        flush = asyncio.create_task(runtime.flush_usage())
        await started.wait()
        # The nonblocking view is restricted, and a fresh capture waits for the
        # cost to commit instead of publishing the pre-settlement state again.
        assert runtime.context_snapshot("owner")["restricted_dialogue"]
        context = asyncio.create_task(runtime.context("owner"))
        await asyncio.sleep(0)
        assert not context.done()
        release.set()
        await flush
        assert (await context)["body"]["snapshot"]["exhausted"]

    asyncio.run(run())
