"""Reception permission, persistent policy fences and independent recovery."""
# ruff: noqa: F401,F811 -- imported pytest fixtures are resolved by name.
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from harness.qq.attention import AttentionStore, ATTENTION_SET
from harness.qq.direct_source import validate_direct_source
from harness.qq.direct_store import normalize_message
from tests.test_qq_direct_actor import actor_factory, incoming, MAIN, GUEST
from tests.test_qq_direct_capabilities import bridge, direct, dispatch
from tests import test_qq_direct_source as source_fixture


def change(store, group, mode="closed", *, identity="close", revision=0, origin="model"):
    return store.set({"group_id": group, "mode": mode, "reason": "需要自主调整注意力"},
        origin=origin, action_id=identity, expected_revision=revision, allowed_groups={group})


def test_persistent_closure_never_expires_and_user_override_fences_old_choice(tmp_path):
    now = [datetime(2026,10,6,tzinfo=UTC)]
    store = AttentionStore(tmp_path/"policy.sqlite3", clock=lambda: now[0])
    first = change(store, MAIN)
    assert change(store, MAIN) == first
    for index, minutes in enumerate((30,60,120,240,240), 1):
        now[0] += timedelta(days=1)
        assert store.check(MAIN) == "attention_closed"
        assert store.due()
        result = change(store, MAIN, identity=f"review-{index}", revision=index)
        assert datetime.fromisoformat(result["current"]["next_review_at"])-now[0] == timedelta(minutes=minutes)
    prior = store.get(MAIN)["revision"]
    now[0] += timedelta(seconds=1)
    change(store, MAIN, "open", identity="manual", revision=None, origin="user")
    with pytest.raises(ValueError, match="policy_changed"):
        change(store, MAIN, identity="old-model", revision=prior)
    restored = AttentionStore(store.path, clock=lambda: now[0])
    assert restored.check(MAIN, received_at=now[0]-timedelta(hours=1)) == "attention_backlog"
    assert restored.check(MAIN, received_at=now[0]+timedelta(seconds=1)) is None
    assert restored.check("private:29999:20001") is None


def test_budget_is_cross_group_and_mentions_cannot_monopolize(tmp_path):
    now = datetime.now(UTC)
    store = AttentionStore(tmp_path/"policy.sqlite3", clock=lambda: now)
    for group in (MAIN,GUEST):
        admitted = store.admit(group, group, received_at=now, units=2048, addressed=True,
                               budget_enabled=True, group_count=2)
        assert admitted["disposition"] == "admitted"
        assert store.admit(group+"extra", group, received_at=now, units=1, addressed=True,
                            budget_enabled=True, group_count=2)["disposition"] == "attention_deferred"
    change(store, GUEST)
    assert store.admit("at", GUEST, received_at=now, units=1, addressed=True)["disposition"] == "attention_closed"


@pytest.mark.parametrize("explicit", [False,True])
def test_portable_snapshot_keeps_attention_policy_path(tmp_path,explicit):
    from harness.deploy.snapshot import discover_shared_sources, build_sources_manifest, rewrite_config_paths
    state = tmp_path/"direct.sqlite3"
    policy = tmp_path/("chosen-policy.sqlite3" if explicit else "attention.sqlite3")
    config = tmp_path/"direct.json"
    payload = {"state_db":str(state)}
    if explicit:
        payload["attention_db"] = str(policy)
    config.write_text(json.dumps(payload),encoding="utf-8")
    change(AttentionStore(policy),MAIN)
    sources = discover_shared_sources(tmp_path,{"NC_QQ_DIRECT_CONFIG":str(config)})
    assert any(source.path == policy and source.target == "state/qq-direct/attention.sqlite3" for source in sources)
    if explicit:
        portable = build_sources_manifest(sources)
        rewritten = rewrite_config_paths(payload,portable["path_rewrites"])
        assert rewritten["attention_db"] == "/var/lib/neuralcloud/qq-direct/attention.sqlite3"


def test_closed_mentions_and_images_are_archived_without_cognitive_work(actor_factory):
    actor, cognition, vision = actor_factory()
    async def run():
        await actor.verify_connection()
        change(actor.attention, GUEST)
        payload = incoming(group=GUEST, images=True)
        payload["message"].append({"type":"at", "data":{"qq":actor.settings.self_member_id}})
        await actor.on_event(payload)
        event = normalize_message(payload, self_id=actor.settings.self_member_id, groups={GUEST})
        row = actor.store.get(event["event_id"])
        assert row["state"] == "archived" and row["source_record_id"] is None
        assert row["terminal_reason"] == "attention_closed"
        assert actor.store.pending(actor.run_id) is None
    asyncio.run(run())
    assert not cognition.calls and not vision.calls


def test_closed_period_history_requires_explicit_bounded_read_and_never_replays_feedback(direct, bridge, tmp_path):
    from harness.qq.direct_capabilities import QQDirectReadArguments, QQDirectReadScope
    instance, _ = bridge
    source = direct.create(images=False)
    settings = direct.settings.model_copy(update={"attention_budget_enabled":True})
    (tmp_path/"direct.json").write_text(settings.model_dump_json(),encoding="utf-8")
    store = instance.attention_store()
    change(store, source_fixture.HOME)
    raw = source.raw | {"message_id":789,"message":[{"type":"text","data":{"text":"关闭期间的一条新事实"}}]}
    event = normalize_message(raw,self_id=source_fixture.SELF,groups={source_fixture.HOME},received_at=direct.now)
    direct.store.receive(event,run_id=direct.run,evidence_ref=direct.store.evidence(raw),historical=False)
    direct.store.update(event["event_id"],state="archived",terminal_reason="attention_closed",terminal_stage="protocol.attention")
    scope = QQDirectReadScope(group_id=source_fixture.HOME,self_member_id=source_fixture.SELF)
    with pytest.raises(ValueError,match="attention_closed"):
        instance._read(settings,scope,QQDirectReadArguments(),direct.now+timedelta(seconds=30))
    change(store, source_fixture.HOME,"open",identity="reopen",revision=1)
    result = instance.enqueue(dispatch(instance))
    assert result["receipt"]["archived_messages"][0]["text"] == "关闭期间的一条新事实"
    assert result["receipt"]["input_units"] > 0
    assert direct.store.get(event["event_id"])["source_record_id"] is None


def test_closing_group_immediately_releases_queued_shared_reply(direct, monkeypatch):
    from harness.runtime.qq_turns import QQTurnIngress
    source = direct.create(images=False)
    owner = SimpleNamespace(add_listener=lambda _:None, journal=SimpleNamespace(iter_records=lambda **_:[]))
    async def submit(**kwargs):
        return object()
    owner.submit_event = submit
    ingress = QQTurnIngress(SimpleNamespace(supervisor=owner),direct.config)
    monkeypatch.setattr("harness.runtime.qq_turns._source",lambda *args,**kwargs:None)
    async def run():
        task = asyncio.create_task(ingress.prepare(source.request))
        while not ingress._waiters:
            await asyncio.sleep(.001)
        ingress.close_group(source_fixture.HOME)
        with pytest.raises(ValueError,match="attention_closed"):
            await asyncio.wait_for(task,1)
        assert not ingress._waiters
    asyncio.run(run())


def test_cognitive_cached_turns_and_model_calls_are_fenced_after_close_and_reopen(direct):
    from harness.cognitive.service import CognitiveServiceHost
    from harness.cognitive.qq_turn import run as run_turn
    from harness.contracts.envelope import DomainCommandEnvelope
    from tests.test_runtime_qq_deadline import _domain_result
    from harness.config import Settings
    from infra.trace.request_spans import model_request_span, trace_scope
    from harness.qq.attention import attention_path
    source = direct.create(images=False)
    store = AttentionStore(attention_path(direct.settings))
    cmd = DomainCommandEnvelope(command_type="PrepareQQTurn",event_id="qq-test",receive_seq=1,
        correlation_id="qq-test",runtime_instance_id="runtime",manifest_revision=1,idempotency_key="same",
        issued_at=direct.now,deadline=direct.now+timedelta(seconds=60),payload=source.request.model_dump(mode="json"))
    owner = SimpleNamespace(adapter=SimpleNamespace(_failure=lambda c,e,**kw:
        _domain_result(c,{}).model_copy(update={"error":e,"status":"rejected"})))
    async def check(code):
        for result in (await run_turn(owner,cmd,resume=False), await run_turn(owner,cmd,resume=True),
                       await CognitiveServiceHost.prepare_qq_response(owner,cmd)):
            assert result.error == code and result.result["failure_category"] == "cancelled"
        from harness.runtime.qq_turns import QQTurnIngress
        with pytest.raises(ValueError,match=code):
            QQTurnIngress._result({"status":"ignored","decision_kind":"qq_prepare","detail":result.result})
    change(store, source_fixture.HOME)
    asyncio.run(check("attention_closed"))
    with trace_scope(attention_context="qq:"+source_fixture.HOME,attention_revision=0):
        with pytest.raises(ValueError,match="attention_closed"):
            with model_request_span(Settings(),role="main",messages=[]):
                pytest.fail("closed content cannot reach another model call")
    change(store,source_fixture.HOME,"open",identity="manual-open",revision=None,origin="user")
    asyncio.run(check("attention_backlog"))


def test_background_assimilation_and_owner_history_respect_closure(tmp_path,monkeypatch):
    from tests.test_qq_memory_worker import setup_host
    from harness.memory.shared import SharedReadScope
    from harness.qq.direct_config import DirectQQSettings
    from harness.qq.attention import attention_path
    host, _ = setup_host(tmp_path,monkeypatch)
    paths = {name: tmp_path/name for name in ("token_file","session_config_path","status_file","control_file",
        "image_cache_dir","desktop_status_file","shared_control_token_file")}
    settings = DirectQQSettings(enabled=True,self_member_id="29999",group_roster=[{"group_id":"10001","observe":True,"send":False}],
        state_db=tmp_path/"direct.sqlite3",evidence_dir=tmp_path/"evidence",**paths)
    config = tmp_path/"direct.json"
    config.write_text(settings.model_dump_json(),encoding="utf-8")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG",str(config))
    host.shared.sync_qq()
    scope = SharedReadScope(audience="local:main",channels=("qq_group",))
    records = host.shared.store.query(scope).records
    assert records
    change(AttentionStore(attention_path(settings)),"10001")
    async def check():
        try:
            await host.qq_memory.tick()
            assert host.qq_memory.proposer.calls == 0
            assert not host.shared.store.query(scope).records
            assert not host.shared.store.read_exact(scope,[r.record_id for r in records])
            with pytest.raises(ValueError,match="attention_closed"):
                host.shared.qq_binding(group_id="10001")
        finally:
            await host.close()
    asyncio.run(check())


def test_inflight_reply_is_fenced_before_send(actor_factory):
    actor, cognition, _ = actor_factory()
    async def run():
        await actor.verify_connection()
        cognition.hook = lambda: change(actor.attention, MAIN)
        await actor.on_event(incoming())
        row = actor.store.pending(actor.run_id)
        await actor.process(row)
        assert not [c for c in actor.client.calls if c[0] == "send_group_msg"]
        assert actor.store.get(row["event_id"])["terminal_reason"] == "attention_closed"
    asyncio.run(run())


def test_readonly_group_can_be_closed_without_send_authority_and_no_read_leak(direct, bridge):
    instance, _ = bridge
    source = direct.create(group=source_fixture.GUEST)
    offer = next(o for o in instance.attention_catalog() if o["scope"]["group_id"] == source_fixture.GUEST)
    row = {"opportunity_id":"attention-close", "generation":2, "capability_id":ATTENTION_SET,
           "scope":offer["scope"], "activation_revision":offer["activation_revision"],
           "deadline_at":offer["valid_until"], "arguments":{"group_id":source_fixture.GUEST,"mode":"closed","reason":"减载"}}
    assert instance.enqueue(row)["status"] == "completed"
    assert not direct.store.counts()["sends"]
    assert all(o["scope"]["group_id"] != source_fixture.GUEST for o in instance.catalog())
    with pytest.raises(ValueError, match="attention_closed"):
        direct.memory.group_context(group_id=source_fixture.GUEST)
    with pytest.raises(ValueError, match="attention_closed"):
        validate_direct_source(direct.config, source.request)


def test_closed_cached_read_cannot_replay_and_reopen_does_not_revive_old_turn(direct, bridge):
    instance, _ = bridge
    source = direct.create()
    instance.enqueue(dispatch(instance))
    change(instance.attention_store(), source_fixture.HOME)
    assert instance.result("opportunity:read")["status"] == "rejected"
    store = instance.attention_store()
    store.clock = lambda: direct.now+timedelta(seconds=1)
    change(store, source_fixture.HOME, "open", identity="open", revision=1)
    assert instance.result("opportunity:read")["status"] == "rejected"
    with pytest.raises(ValueError, match="attention_backlog|attention_policy_changed"):
        validate_direct_source(direct.config, source.request)


def test_control_review_is_independent_bounded_and_cannot_overwrite_user(direct, bridge, monkeypatch):
    from harness.runtime.attention import AttentionController
    from harness.runtime.models import RuntimeLifecycle
    from harness.cognitive.capabilities import catalog_hash
    from harness.contracts.capability_calls import CapabilityOffer
    instance, _ = bridge
    owner = instance.supervisor
    now = [direct.now]
    instance.clock = lambda: now[0]
    monkeypatch.setattr("harness.runtime.attention.utc_now", lambda: now[0])
    owner.lifecycle = RuntimeLifecycle.RUNNING
    owner._homeostasis_snapshot = SimpleNamespace(payload={"dynamics":{"mode":"active"},
        "regulation_pressure":{"suggest_reduce_input":True}})
    owner.goals = SimpleNamespace(enabled=True,budget=lambda **_: {"calls_last_hour":8},
                                  config=SimpleNamespace(maximum_model_calls_per_hour=12))
    owner.cognitive = SimpleNamespace(prepare_capability_call=None)
    owner._make_command = lambda event, **values: SimpleNamespace(payload=values["payload"])
    owner._append_journal = lambda *args,**kwargs: None
    calls = []
    async def invoke(**values):
        request = values["envelope"].payload
        calls.append(request)
        offer = request["offers"][0]
        if len(calls) == 1:
            # User changes the policy while inference is running.
            change(instance.attention_store(), offer["scope"]["group_id"], "open", identity="user-open",revision=None,origin="user")
        choice = {"kind":"act", "offer_id":offer["offer_id"], "arguments":{
            "group_id":offer["scope"]["group_id"], "mode":"closed", "reason":"reduce"}}
        return SimpleNamespace(result={"decision":{"decision_id":"review-decision", "opportunity_id":request["opportunity_id"],
            "choice":choice, "prepared_at":now[0].isoformat(), "deadline_at":request["deadline_at"],
            "catalog_hash":catalog_hash([CapabilityOffer.model_validate(o) for o in request["offers"]])}})
    owner._invoke_domain = invoke
    control = AttentionController(owner,instance)
    async def run():
        control.schedule(None)
        await control.task
        assert instance.attention_store().get(request_group())["mode"] == "open"
        assert control.last_error == "ValueError"
        for _ in range(3):
            now[0] += timedelta(minutes=15)
            control.schedule(None)
            await control.task
        control.schedule(None)
        assert len(calls) == 4
        owner.lifecycle = RuntimeLifecycle.PAUSED
        now[0] += timedelta(hours=1)
        control.schedule(None)
        assert len(calls) == 4
        assert all(r["homeostatic_summary"]["attention_control_only"] for r in calls)
        assert all(o["capability_id"] == ATTENTION_SET for r in calls for o in r["offers"])
    def request_group():
        return calls[0]["offers"][0]["scope"]["group_id"]
    asyncio.run(run())
