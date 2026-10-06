"""Real protocol provenance into idempotent Homeostasis, without replies."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from harness.contracts.envelope import DomainCommandEnvelope
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.runtime.models import RuntimeLifecycle
from harness.runtime.qq_direct_feedback import QQDirectFeedback
from tests.test_qq_direct_capabilities import bridge, direct  # noqa: F401
from tests.test_qq_direct_source import SELF, rewrite_event


def test_live_protocol_feedback_is_verified_and_changes_state_once(direct, bridge, tmp_path):  # noqa: F811
    async def run():
        source = direct.create(images=False)
        b, _ = bridge
        emitted = []
        async def submit(**kwargs):
            emitted.append(kwargs)
            return object()
        sup = SimpleNamespace(lifecycle=RuntimeLifecycle.RUNNING, goals=SimpleNamespace(enabled=True),
            production_runtime=None, recovery=SimpleNamespace(mapping=lambda _: {}), submit_event=submit)
        collector = QQDirectFeedback(sup, b)
        await collector.heartbeat()
        assert collector.last_error is None and len(emitted) == 1
        payload = emitted[0]["payload"]
        assert payload["qq_fact"]["verification"] == "qq_protocol_verified"
        assert payload["qq_fact"]["fact_id"] == source.observation.record_id
        assert "text" not in payload["qq_fact"]
        collector.last_scan_at = None
        await collector.heartbeat()
        assert len(emitted) == 1
        engine = HomeostasisEngine(load_profile("presets/homeostasis/qq-social-drive-v1.json"),
            state_dir=tmp_path / "homeostasis", now=direct.now, clock=lambda: direct.now)
        try:
            before = engine.snapshot.dimensions["novelty"].value
            command = DomainCommandEnvelope(command_type="ApplyEvent", event_id=emitted[0]["event_id"],
                receive_seq=1, correlation_id="test", runtime_instance_id="test", manifest_revision=1,
                idempotency_key="feedback:1", issued_at=direct.now, deadline=direct.now + timedelta(seconds=60),
                payload={"regulation_feedback": True, "event": {"payload": payload, "source_time": direct.now}})
            result = await engine.apply_event(command)
            assert result.status == "applied"
            after = engine.snapshot.dimensions["novelty"].value
            assert after > before
            assert result.result["trace"]["applied_impacts"]["social_connection"] == 0
            assert (await engine.apply_event(command)).status == "idempotent_replay"
            assert engine.snapshot.dimensions["novelty"].value == after
        finally:
            await engine.close()
        assert not direct.store.counts()["sends"]
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["history", "own", "old", "changed", "revoked"])
def test_protocol_history_echo_stale_and_forged_messages_cannot_earn_credit(direct, bridge, kind):  # noqa: F811
    b, _ = bridge
    source = direct.create(images=False, historical=kind == "history", member=SELF if kind == "own" else "2285027817",
        message_time=direct.now - timedelta(seconds=400) if kind == "old" else direct.now)
    if kind == "changed":
        rewrite_event(direct, source.event["event_id"], text="changed text")
    if kind == "revoked":
        b.config.allowed_group_ids = []
    collector = QQDirectFeedback(SimpleNamespace(), b)
    if kind in {"changed", "revoked"}:
        with pytest.raises(ValueError):
            collector.collect()
    else:
        assert collector.collect() == []
