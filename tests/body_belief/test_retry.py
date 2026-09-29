import asyncio

from harness.body_belief import BodyBeliefService, BodyBeliefStore
from tests.body_belief.conftest import experience
from tests.body_belief.test_runtime_integration import record


def test_unsuccessful_appraisal_survives_restart_without_busy_retry(tmp_path):
    class Appraiser:
        def __init__(self, success):
            self.success, self.calls = success, 0

        def appraise(self, *args, **kwargs):
            self.calls += 1
            # Audit accepts JSON-serializable dict through the Pydantic model.
            from harness.body_belief.appraisal import AppraisalResult
            return AppraisalResult(success=self.success, candidates=(), messages=(), input_hash="0" * 64)

    async def run():
        path = tmp_path / "belief.sqlite3"
        failed = Appraiser(False)
        service = BodyBeliefService(BodyBeliefStore(path), appraiser=failed, memory_treatment="body_causal_beliefs")
        item = experience(0)
        record(service, item)
        await service.observe(item.observation_after)
        assert await service.drain()
        assert failed.calls == 1
        assert service.store.db.execute("SELECT count(*) FROM appraisal_work").fetchone()[0] == 1
        assert service.store.db.execute("SELECT attempts FROM appraisal_retry").fetchone()[0] == 1
        await service.close()
        recovered = Appraiser(True)
        service = BodyBeliefService(BodyBeliefStore(path), appraiser=recovered, memory_treatment="body_causal_beliefs")
        with service.store.db:
            service.store.db.execute("UPDATE appraisal_retry SET retry_at=0")
        await service.resume()
        assert await service.drain()
        assert recovered.calls == 1
        assert service.store.db.execute("SELECT count(*) FROM appraisal_work").fetchone()[0] == 0
        await service.close()
    asyncio.run(run())
