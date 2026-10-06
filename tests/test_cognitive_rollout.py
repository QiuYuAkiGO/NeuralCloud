import asyncio
from datetime import timedelta
import json

import pytest

from harness.homeostasis.engine import HomeostasisEngine
from harness.homeostasis.rollout import shadow_report
from tests.test_cognitive_dynamics import active_profile, command, NOW, receipt
from harness.homeostasis.dynamics import AttentionAssessment
from tests.test_capability_cognition import host, FakeLLM, offer, command as model_command, run


def test_shadow_ticks_do_not_change_legacy_values_evidence_clocks_or_watchdog(tmp_path):
    async def scenario():
        clock = [NOW]
        engine = HomeostasisEngine(active_profile("shadow"), state_dir=tmp_path, clock=lambda:clock[0])
        before = engine.snapshot
        for index in range(1,5):
            clock[0] = NOW+timedelta(seconds=index*5)
            cmd = command(engine,index,clock[0],{"passive_only":True},heartbeat=True)
            assert (await engine.apply_heartbeat(cmd)).status == "applied"
        assert engine.snapshot.dimensions == before.dimensions
        assert engine.snapshot.watchdog == before.watchdog
        assert engine.snapshot.emotion == before.emotion
        assert engine.snapshot.last_heartbeat_at == before.last_heartbeat_at
        assert engine._dynamics.load < before.dimensions["cognitive_load"].value
        assert engine._dynamics.shadow_observed_seconds == 15
        clock[0] += timedelta(hours=24)
        await engine.apply_heartbeat(command(engine,5,clock[0],{"passive_only":True},heartbeat=True))
        assert engine._dynamics.shadow_observed_seconds == 0
        assert not shadow_report(engine.snapshot.model_dump(mode="json"),now=clock[0])["ready"]
        await engine.close()
    asyncio.run(scenario())


def test_activation_preserves_seven_values_and_restarts_passive_recovery(tmp_path):
    async def scenario():
        first = HomeostasisEngine(active_profile("shadow"),state_dir=tmp_path,clock=lambda:NOW)
        await first.apply_event(command(first,1,NOW,{"cognitive_exposure":receipt().model_dump(mode="json")}))
        before = {k:v.value for k,v in first.snapshot.dimensions.items()}
        await first.close()
        active = HomeostasisEngine(active_profile(),state_dir=tmp_path,clock=lambda:NOW)
        assert active.snapshot.dynamics["mode"] == "active"
        assert {k:v.value for k,v in active.snapshot.dimensions.items()} == before
        assert active.snapshot.interoceptive_summary.schema_version == "homeostatic-interoception-v2"
        await active.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [1,0,-1])
def test_semantic_evidence_is_signed_optional_and_deduplicated_across_receipt_ids(tmp_path,value):
    async def scenario():
        engine = HomeostasisEngine(active_profile(),state_dir=tmp_path,clock=lambda:NOW)
        initial = engine._dynamics.responses["social_connection"]
        observed = receipt(assessment=AttentionAssessment(topic="relation",unexpectedness=.5,meaning=.8,
            confidence=1,impacts={"social_connection":value}),evidence_refs=["real-occasion"])
        for index in range(1,4):
            fresh = observed.model_copy(update={"exposure_id":str(index)})
            assert (await engine.apply_event(command(engine,index,NOW,{"cognitive_exposure":fresh.model_dump(mode="json")}))).status == "applied"
        assert engine._dynamics.responses["social_connection"] == pytest.approx(initial+.05*value)
        assert engine._dynamics.baselines["social_connection"] == active_profile().preferences["social_connection"]
        assert engine._dynamics.exposures == 3  # Actual rereading still costs work.
        assert len(engine._dynamics.habituation_fragments) == 1
        await engine.close()
    asyncio.run(scenario())


def test_attention_review_model_sees_control_schema_without_dialogue_or_memory():
    target = offer(offer_id="attention:1",channel="qq",capability_id="communication.qq.attention.set.v1",
        provider_id="qq-onebot",scope={"group_id":"123","self_member_id":"456"})
    owner = host(FakeLLM({"kind":"act","offer_id":target.offer_id,
        "arguments":{"group_id":"123","mode":"closed","reason":"选择休息"}}))
    result = run(owner,model_command([target],homeostatic_summary={"attention_control_only":True}))
    assert result["decision"]["choice"]["arguments"]["mode"] == "closed"
    assert not owner.queries
    messages = json.dumps(owner.chat.llm.calls[0][0],ensure_ascii=False)
    assert "review_after_seconds" in messages and "verified-shared-recall" not in messages


def test_shadow_gate_requires_real_recent_work_and_valid_candidate_state(tmp_path):
    async def scenario():
        engine = HomeostasisEngine(active_profile("shadow"),state_dir=tmp_path,clock=lambda:NOW)
        data = engine.snapshot.model_dump(mode="json")
        observed = data["dynamics"]
        observed["shadow_observation"].update(continuous_seconds=86400,last_sample_at=NOW.isoformat())
        assert "no_measured_work_within_shadow_window" in shadow_report(data,now=NOW)["reasons"]
        observed["assessment_coverage"].update(exposures=1,assessed=0,unassessed=1,
            work_units=512,last_work_at=NOW.isoformat())
        assert shadow_report(data,now=NOW)["ready"]  # Missing optional meaning is observable, not a deadlock.
        observed["fatigue"] = float("nan")
        assert "invalid_shadow_fatigue" in shadow_report(data,now=NOW)["reasons"]
        observed["shadow_observation"]["continuous_seconds"] = float("nan")
        assert not shadow_report(data,now=NOW)["ready"]
        await engine.close()
    asyncio.run(scenario())
