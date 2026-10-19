"""Shared owner/QQ queue integration with synthetic data and no network/model."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from harness.contracts.envelope import DomainCommandResult
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.homeostasis.models import QQFactLookup, QQHomeostaticFact
from harness.qq.decision import QQReplyDecision, record_qq_delivery
from harness.qq.homeostatic import QQHomeostaticDriver
from harness.qq.memory import QQVisualMemoryStore, QQVerifiedProfileInput, QQVisualObservationInput
from harness.qq.session import QQSessionSettings
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.watch_ledger import WatchEvent
from harness.research.manifest import load_experiment_manifest, ExperimentManifest
from harness.runtime.adapters import DomainSnapshot, ServiceHealth
from harness.runtime.control import ControlPlane, create_control_app
from harness.runtime.models import RuntimeEventType, RuntimeLifecycle
from harness.runtime.qq_homeostasis import QQFactIngress
from harness.runtime.qq_turns import QQTurnIngress, _source
from harness.runtime.rpc import create_domain_service_app
from harness.runtime.supervisor import RuntimeSupervisor


class Cognitive:
    def __init__(self):
        self.revision = 0
        self.commands = []

    async def health(self):
        return ServiceHealth(service="cognitive", revision=self.revision)

    async def get_snapshot(self):
        return DomainSnapshot(service="cognitive", revision=self.revision)

    async def prepare_qq_response(self, command):
        self.commands.append(command)
        self.revision += 1
        body = command.payload
        decision = QQReplyDecision(decision_id="qq-decision:" + uuid4().hex, input_hash="a" * 64,
            created_at=datetime.now(timezone.utc), group_id=body["group_id"], self_member_id=body["self_member_id"],
            trigger_member_id=body["trigger_member_id"], action="wait", reason="Synthetic wait.",
            decision_source="model", outcome="natural_wait", participants=[], source_record_ids=[],
            new_observation_record_ids=[], evidence_refs=[], delivery_refs=[], model="synthetic",
            persona_version="synthetic", preset_id="synthetic", trace_id=body["trace"]["trace_id"])
        return DomainCommandResult(command_id=command.command_id, command_type=command.command_type,
            status="applied", request_hash=command.effective_request_hash(), committed_revision=self.revision,
            acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
            completed_at=datetime.now(timezone.utc), result={"decision": decision.model_dump(mode="json"),
                "trace": {"trace_id": body["trace"]["trace_id"], "event_id": body["trace"]["event_id"],
                          "elapsed_ms": 0, "stages": [], "counters": {}}})


@pytest.fixture
def case(tmp_path):
    now = datetime.now(timezone.utc)
    store = QQVisualMemoryStore(tmp_path / "memory.sqlite3")
    profile = store.record_profile(QQVerifiedProfileInput(group_id="10001", member_id="20002",
        display_name="Synthetic person", display_name_source="qq_profile", verification="qq_ui_verified",
        capture_id="fixture-profile", observed_at=now, group_evidence=["fixture://group"],
        member_evidence=["fixture://profile"])).profile
    event_id = "qq-watch-event:" + uuid4().hex
    observation = store.record_observation(QQVisualObservationInput(group_id="10001", member_id="20002",
        profile_record_id=profile.record_id, capture_id="fixture-capture", capture_item_id=event_id,
        text="Synthetic question.", observed_at=now, evidence=["fixture://capture"],
        author_attribution="qq_ui_verified", attribution_evidence=["fixture://profile"])).observation
    event = WatchEvent(event_id=event_id, group_id="10001", run_id="synthetic-run", screen_id="fixture-capture",
        stream_revision=1, item_index=0, item={"kind":"text", "direction":"incoming", "text":observation.text},
        first_seen_at=now, observed_at=now, state="bound", source_record_id=observation.record_id)
    ledger = tmp_path / "watch.sqlite3"
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE qq_watch_events(event_id TEXT, run_id TEXT, group_id TEXT, state TEXT, record_json TEXT)")
        db.execute("INSERT INTO qq_watch_events VALUES(?,?,?,?,?)", (event_id, event.run_id, "10001", "bound", event.model_dump_json()))
        db.execute("CREATE TABLE qq_watch_state(singleton INTEGER, group_id TEXT,run_id TEXT,revision INTEGER,baseline_required INTEGER)")
        db.execute("INSERT INTO qq_watch_state VALUES(1,?,?,1,0)", (event.group_id,event.run_id))
        db.execute("CREATE TABLE qq_watch_sends(event_id TEXT)")
    watcher = tmp_path / "watcher.json"
    watcher.write_text(json.dumps({"group_id":"10001", "self_member_id":"20001", "state_db":str(ledger)}))
    token = tmp_path / "token"
    token.write_text("a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2")
    config = QQSessionSettings(memory_db=store.path, token_file=token, allowed_group_ids=["10001"],
        self_member_id="20001", watcher_status_file=tmp_path / "status.json")
    config_path = tmp_path / "session.json"
    config_path.write_text(config.model_dump_json())
    engine = HomeostasisEngine(load_profile("presets/homeostasis/qq-social-drive-v1.json"), state_dir=tmp_path / "domain")
    cognitive = Cognitive()
    manifest = load_experiment_manifest(Path("presets/runtime/shared-misty-v1.json"))
    runtime = RuntimeSupervisor(manifest=manifest, homeostasis=engine, cognitive=cognitive, state_dir=tmp_path / "runtime")
    runtime.lifecycle = RuntimeLifecycle.RUNNING
    runtime._homeostasis_snapshot = asyncio.run(engine.get_snapshot())
    control = ControlPlane(runtime, token="a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2")
    request = QQSharedPrepareRequest(group_id="10001", self_member_id="20001", trigger_member_id="20002",
        deadline_at=now+timedelta(seconds=60), trace={"trace_id":"qq-trace:"+uuid4().hex,
            "event_id":event_id, "first_seen_at":now, "first_seen_mono":100.0})
    fact = QQHomeostaticFact(fact_id=observation.record_id, group_id="10001", kind="verified_message",
        occurred_at=observation.stored_at, verification="qq_ui_verified", evidence_refs=observation.evidence)
    yield SimpleNamespace(**locals())
    asyncio.run(engine.close())


def test_shared_fact_is_queued_once_and_only_runtime_changes_domain(case):
    async def run():
        ingress = QQFactIngress(case.runtime, case.config)
        before = case.engine.snapshot
        queued = await ingress.submit(case.fact)
        assert queued["status"] == "queued" and case.engine.snapshot == before
        assert await ingress.submit(case.fact) == queued
        restarted = QQFactIngress(case.runtime, case.config)
        assert await restarted.submit(case.fact) == queued
        event = await case.runtime.queue.get(timeout=0)
        result = await case.runtime.dispatch(event)
        assert result.status == "completed"
        assert case.engine.snapshot.revision == before.revision+1
        assert not case.cognitive.commands
        assert case.engine.snapshot.dimensions["social_connection"].value == before.dimensions["social_connection"].value
        status = await case.engine.read_qq_fact(QQFactLookup(group_id="10001", fact_id=case.fact.fact_id))
        assert status.fact_hash == case.fact.fact_hash and status.committed_revision == 1
        assert (await restarted.submit(case.fact))["status"] == "applied"
        assert case.engine.snapshot.revision == 1
        with pytest.raises(ValueError, match="conflict"):
            await restarted.submit(case.fact.model_copy(update={"evidence_refs":["fixture://wrong"]}))
    asyncio.run(run())


def test_forged_fact_and_wrong_scope_are_rejected_before_queue(case):
    async def run():
        ingress = QQFactIngress(case.runtime, case.config)
        for changes in ({"group_id":"10002"}, {"fact_id":"not-a-record"}, {"evidence_refs":["fixture://wrong"]},
                        {"kind":"verified_novelty"}, {"occurred_at":case.now-timedelta(seconds=301)}):
            with pytest.raises(ValueError):
                await ingress.submit(case.fact.model_copy(update=changes))
        assert case.runtime.queue.empty() and case.engine.snapshot.revision == 0
    asyncio.run(run())


def test_actual_send_is_attempt_only_not_social_reward(case):
    receipt = record_qq_delivery(memory_db=case.store.path, group_id="10001", self_member_id="20001",
        delivered_text="Synthetic sent reply.", evidence_refs=["fixture://sent"], verification="qq_ui_verified")
    fact = QQHomeostaticFact(fact_id=receipt.receipt_id, group_id="10001", kind="send_attempt",
        occurred_at=receipt.delivered_at, verification="qq_ui_verified", evidence_refs=[receipt.receipt_id])
    async def run():
        before = {key:value.value for key,value in case.engine.snapshot.dimensions.items()}
        await QQFactIngress(case.runtime, case.config).submit(fact)
        assert (await case.runtime.dispatch(await case.runtime.queue.get(timeout=0))).status == "completed"
        assert before == {key:value.value for key,value in case.engine.snapshot.dimensions.items()}
    asyncio.run(run())


def test_queued_expired_fact_does_not_pause_shared_runtime(case):
    async def run():
        event = await case.runtime.submit_event(event_type=RuntimeEventType.QQ_FACT, priority=30,
            payload={"qq_fact":case.fact.model_copy(update={"occurred_at":case.now-timedelta(seconds=301)}).model_dump(mode="json")})
        result = await case.runtime.dispatch(event)
        assert result.status == "failed" and result.detail["error_type"] == "qq_fact_expired"
        assert case.runtime.lifecycle == RuntimeLifecycle.RUNNING and case.engine.snapshot.revision == 0
    asyncio.run(run())


def test_qq_turn_runs_once_in_shared_queue_and_never_appraises_or_delivers(case):
    async def run():
        ingress = QQTurnIngress(case.control, case.config, watcher_path=case.watcher)
        tasks = [asyncio.create_task(ingress.prepare(case.request)) for _ in range(2)]
        event = await case.runtime.queue.get(timeout=1)
        assert event is not None and event.event_type == RuntimeEventType.QQ_TURN
        result = await case.runtime.dispatch(event)
        assert result.status == "completed", result.detail
        values = await asyncio.gather(*tasks)
        assert values[0] == values[1] and set(values[0]) == {"decision", "trace"}
        assert len(case.cognitive.commands) == 1 and case.runtime._cognitive_snapshot.revision == 1
        assert case.engine.snapshot.revision == 0
        assert not any(r.record_type=="reply_delivery_intent" for r in case.runtime.journal.iter_records())
        replay = QQTurnIngress(case.control, case.config, watcher_path=case.watcher)
        assert await replay.prepare(case.request) == values[0]
        changed = case.request.model_copy(update={"query":"different"})
        with pytest.raises(ValueError, match="conflict"):
            await replay.prepare(changed)
    asyncio.run(run())


@pytest.mark.parametrize("background_commits", [1, 7])
def test_qq_prepare_survives_background_owner_revision_changes(case, monkeypatch, background_commits):
    from harness.runtime.adapters import PipelineCognitiveAdapter

    async def run():
        case.runtime._cognitive_snapshot = DomainSnapshot(service="cognitive", revision=14)
        case.cognitive.revision = 14 + background_commits
        prepare = case.cognitive.prepare_qq_response

        async def prepare_after_another_commit(command):
            # A fresh RPC snapshot before this call would still race with the
            # memory worker acquiring the owner lock first.
            case.cognitive.revision += 1
            adapter = object.__new__(PipelineCognitiveAdapter)
            adapter._revision = case.cognitive.revision
            rejected = adapter._preflight(command)
            if rejected is not None:
                return rejected
            return await prepare(command)

        monkeypatch.setattr(case.cognitive, "prepare_qq_response", prepare_after_another_commit)
        event = await case.runtime.submit_event(event_type=RuntimeEventType.QQ_TURN,
            priority=30, payload=case.request.model_dump(mode="json"))
        result = await case.runtime.dispatch(event)
        assert result.status == "completed", result.detail
        assert case.runtime.lifecycle == RuntimeLifecycle.RUNNING
        assert len(case.cognitive.commands) == 1
        command = case.cognitive.commands[0]
        assert command.expected_revision is None
        assert command.deadline <= case.request.deadline_at
        expected_payload = case.request.model_dump(mode="json")
        trusted_summary = case.engine.snapshot.interoceptive_summary.model_dump(mode="json")
        expected_payload["homeostatic_interoception"] = trusted_summary
        from harness.runtime.qq_pipeline import policy
        expected_payload["qq_frozen_policy"] = policy(case.runtime, case.request).model_dump()
        assert command.payload == expected_payload
        assert command.version_vector.homeostasis_state == trusted_summary["snapshot_revision"]
        assert case.runtime._cognitive_snapshot.revision == 16 + background_commits
        assert case.engine.snapshot.revision == 0
        assert not any(r.record_type == "reply_delivery_intent" for r in case.runtime.journal.iter_records())

    asyncio.run(run())


@pytest.mark.parametrize("change", [{"group_id":"10002"}, {"self_member_id":"20003"},
                                  {"trigger_member_id":"20003"}])
def test_original_author_scope_cannot_be_forged(case, change):
    with pytest.raises(ValueError):
        _source(case.config, case.watcher, case.request.model_copy(update=change))


def test_queued_deadline_cannot_be_renewed(case, monkeypatch):
    async def run():
        ingress = QQTurnIngress(case.control, case.config, watcher_path=case.watcher)
        request = case.request.model_copy(update={"deadline_at":datetime.now(timezone.utc)+timedelta(seconds=.1)})
        with pytest.raises(TimeoutError):
            await ingress.prepare(request)
        assert ingress._waiters == {}
        # Independent wall/QPC sampling can straddle a Windows clock tick.
        # Dispatch is explicitly after the immutable deadline in this test.
        monkeypatch.setattr("harness.runtime.supervisor.utc_now", lambda:request.deadline_at+timedelta(seconds=1))
        result = await case.runtime.dispatch(await case.runtime.queue.get(timeout=0))
        assert result.detail["error_type"] == "qq_turn_expired" and not case.cognitive.commands
        assert case.engine.snapshot.revision == 0
    asyncio.run(run())


def test_reopened_cooldown_uses_new_trace_but_original_source_and_deadline(case):
    async def run():
        ingress = QQTurnIngress(case.control, case.config, watcher_path=case.watcher)
        first = asyncio.create_task(ingress.prepare(case.request))
        await case.runtime.dispatch(await case.runtime.queue.get(timeout=1))
        original = await first
        # A completed response remains retrievable without reopening the event.
        terminal = case.event.model_copy(update={"state":"rate_limited", "decision_id":original["decision"]["decision_id"]})
        with sqlite3.connect(case.ledger) as db:
            db.execute("UPDATE qq_watch_events SET state=?,record_json=?", (terminal.state,terminal.model_dump_json()))
        assert await ingress.prepare(case.request) == original
        next_request = case.request.model_copy(update={"trace":case.request.trace.model_copy(update={"trace_id":"qq-trace:"+uuid4().hex})})
        with pytest.raises(ValueError, match="not_pending"):
            await ingress.prepare(next_request)
        reopened = terminal.model_copy(update={"state":"bound", "decision_id":None, "rate_limit_reopens":1,
            "reason":"rate_limit_reopened"})
        with sqlite3.connect(case.ledger) as db:
            db.execute("UPDATE qq_watch_events SET state=?,record_json=?", (reopened.state,reopened.model_dump_json()))
        second = asyncio.create_task(ingress.prepare(next_request))
        event = await case.runtime.queue.get(timeout=1)
        await case.runtime.dispatch(event)
        assert (await second)["decision"]["decision_id"] != original["decision"]["decision_id"]
        assert len(case.cognitive.commands)==2
        assert case.cognitive.commands[1].payload["deadline_at"]==case.cognitive.commands[0].payload["deadline_at"]
        assert case.engine.snapshot.revision==0
    asyncio.run(run())


@pytest.mark.parametrize("sql", [
    "UPDATE qq_watch_state SET run_id='another-run'",
    "UPDATE qq_watch_state SET revision=2",
    "UPDATE qq_watch_state SET baseline_required=1",
    "UPDATE qq_watch_events SET state='sent'",
    "INSERT INTO qq_watch_sends SELECT event_id FROM qq_watch_events",
])
def test_event_scope_and_pending_authority_are_current(case, sql):
    with sqlite3.connect(case.ledger) as db:
        db.execute(sql)
    with pytest.raises(ValueError):
        _source(case.config, case.watcher, case.request)


def test_retained_domain_cursor_sets_new_runtime_ingress_floor_without_reset(case):
    async def run():
        case.engine.snapshot=case.engine.snapshot.model_copy(update={"acknowledged_receive_seq":1003})
        before=case.engine.snapshot
        await case.runtime._check_services()
        assert case.runtime._receive_seq==1003 and case.engine.snapshot==before
        event=await case.runtime.submit_event(event_type="heartbeat",priority=50)
        assert event.receive_seq==1004
        assert (await case.runtime.dispatch(event)).status=="completed"
        assert case.engine.snapshot.revision==before.revision+1
        assert case.engine.snapshot.acknowledged_receive_seq==1004
        assert any(record.record_type=="ingress_cursor_floor" for record in case.runtime.journal.iter_records())
    asyncio.run(run())


def test_false_autonomous_flag_keeps_heartbeat_but_never_schedules_local_chat(case):
    async def run():
        assert case.runtime.manifest.heartbeat_seconds == 5
        assert await case.runtime.submit_event(event_type="idle", priority=60) is None
        event = await case.runtime.submit_event(event_type="heartbeat", priority=50)
        result = await case.runtime.dispatch(event)
        assert result.status == "completed", result.detail
        assert case.engine.snapshot.revision == 1 and not case.cognitive.commands
        assert not [r for r in case.runtime.journal.iter_records() if r.record_type=="event_accepted"
                    and r.data["event"]["event_type"]=="idle"]
    asyncio.run(run())


def test_control_and_domain_fact_routes_require_tokens_and_share_snapshot(case):
    app = create_control_app(case.control, qq_session_config_path=case.config_path)
    with TestClient(app) as client:
        assert client.get("/v1/homeostasis/snapshot").status_code == 401
        headers = {"Authorization":"Bearer a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2"}
        value = client.get("/v1/homeostasis/snapshot", headers=headers).json()
        assert value == asyncio.run(case.engine.get_snapshot()).model_dump(mode="json")
        bad = client.post("/v1/homeostasis/qq-facts:read", headers=headers,
            json={"group_id":"10002", "fact_id":"fixture"})
        assert bad.status_code == 403
        assert client.post("/v1/events", headers=headers, json={"event_type":"qq_fact", "priority":30}).status_code == 403
    app = create_domain_service_app(case.engine, service_token="a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2", service_name="homeostasis")
    with TestClient(app) as client:
        assert client.post("/v1/qq-facts:read", json={"group_id":"10001","fact_id":"missing"}).status_code==401
        response = client.post("/v1/qq-facts:read", headers=headers, json={"group_id":"10001","fact_id":"missing"})
        assert response.json()["status"] == "unknown" and case.engine.snapshot.revision == 0


def test_default_manifest_hash_unchanged_and_social_contract_explicit():
    manifest = load_experiment_manifest(Path("presets/runtime/development-demo.json"))
    payload = json.loads(manifest.canonical_json())
    assert "autonomous_chat_enabled" not in payload
    assert ExperimentManifest.model_validate(payload).content_hash() == manifest.content_hash()
    explicit = manifest.model_copy(update={"autonomous_chat_enabled":False})
    assert explicit.content_hash() != manifest.content_hash()
    assert json.loads(explicit.canonical_json())["autonomous_chat_enabled"] is False
    social = load_experiment_manifest(Path("presets/runtime/shared-misty-v1.json"))
    assert social.homeostatic_profile == "qq-social-drive-v1@1"
    for revision in (1, 2, 3):
        assert ExperimentManifest.model_validate({**social.model_dump(),
            "homeostatic_profile": f"qq-social-drive-v1@{revision}"})
    for ref in ("unrecognized@1", "qq-social-drive-v1@0"):
        with pytest.raises(ValueError):
            ExperimentManifest.model_validate({**social.model_dump(), "homeostatic_profile":ref})


def test_remote_driver_cannot_tick_domain_and_recovers_committed_fact(case, tmp_path):
    calls, known = [], {}
    def handler(request):
        assert request.headers["Authorization"] == "Bearer a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2"
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/homeostasis/snapshot":
            # Snapshot serialization is synchronous here; no nested asyncio.run.
            snapshot = case.engine.snapshot
            return httpx.Response(200, json=DomainSnapshot(service="homeostasis", revision=snapshot.revision,
                payload={**snapshot.model_dump(mode="json"), "profile":case.engine.profile.model_dump(mode="json")}).model_dump(mode="json"))
        payload = json.loads(request.content)
        if request.url.path.endswith(":read"):
            key = "qq:"+payload["group_id"]+":"+payload["fact_id"]
            return httpx.Response(200, json={"fact_key":key, "status":"applied" if key in known else "unknown",
                "fact_hash":known.get(key), "committed_revision":1 if key in known else None})
        fact = QQHomeostaticFact.model_validate(payload)
        return httpx.Response(200, json={"schema_version":"qq-homeostatic-submit-v1", "status":"queued",
            "fact_key":fact.fact_key, "fact_hash":fact.fact_hash})
    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    clock = SimpleNamespace(mono=0.0)
    options = dict(state_dir=tmp_path/"driver", group_id="10001", self_member_id="20001", run_id="fixture",
        run_async=asyncio.run, shared_runtime_url="http://127.0.0.1:8770", shared_control_token_file=case.token,
        shared_http_client=transport, monotonic=lambda:clock.mono)
    driver = QQHomeostaticDriver(**options)
    assert driver.engine is None and not (driver.state_dir/"domain").exists()
    occurred = datetime.now(timezone.utc)
    driver.enqueue_fact(fact_id="test", kind="verified_message", occurred_at=occurred, evidence_refs=["fixture://source"])
    driver.tick()
    assert driver.status()["feedback_pending"] == 1 and driver.snapshot.revision == 0
    with closing(driver._connect()) as db:
        raw = db.execute("SELECT record_json FROM facts").fetchone()[0]
    fact = QQHomeostaticFact.model_validate_json(raw)
    known[fact.fact_key] = fact.fact_hash
    driver.close()
    restarted = QQHomeostaticDriver(**options)
    restarted.tick()
    assert restarted.status()["feedback_applied"] == 1 and restarted.status()["feedback_pending"] == 0
    assert all("commands" not in path for _,path in calls)
    assert len([p for _,p in calls if p=="/v1/homeostasis/qq-facts"]) == 1
    restarted.close()
    asyncio.run(transport.aclose())


def test_local_owner_requires_explicit_development_and_normal_missing_service_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("NC_CONTROL_MODE", "study_runtime")
    args = dict(state_dir=tmp_path/"driver", profile_path=Path("presets/homeostasis/qq-social-drive-v1.json"),
        group_id="10001", self_member_id="20001", run_id="fixture", run_async=asyncio.run)
    with pytest.raises(ValueError, match="shared_runtime_required"):
        QQHomeostaticDriver(**args)
    with pytest.raises(PermissionError, match="development_mode_required"):
        QQHomeostaticDriver(**args, development_mode=True)
    assert not (tmp_path/"driver").exists()


def test_ingress_carries_late_error_notice_through_runtime_and_snapshot(case, monkeypatch):
    from harness.core.error_notice import ERROR_FALLBACK_REPLY
    from harness.runtime import qq_turns, supervisor

    async def run():
        ingress = QQTurnIngress(case.control, case.config, watcher_path=case.watcher)
        request = case.request.model_copy(update={"deadline_at": datetime.now(timezone.utc) + timedelta(seconds=1.2)})
        original = case.cognitive.prepare_qq_response
        snapshot = case.cognitive.get_snapshot
        monkeypatch.setattr(qq_turns, "ERROR_FALLBACK_SEND_SECONDS", .5)
        monkeypatch.setattr(supervisor, "ERROR_FALLBACK_SEND_SECONDS", .5)

        async def late(command):
            assert command.deadline <= request.deadline_at
            result = await original(command)
            await asyncio.sleep(max(0, (request.deadline_at - datetime.now(timezone.utc)).total_seconds()) + .025)
            decision = QQReplyDecision.model_validate({**result.result["decision"], "action": "speak",
                "reply": ERROR_FALLBACK_REPLY, "reply_segments": [ERROR_FALLBACK_REPLY],
                "decision_source": "expired", "outcome": "error_fallback", "error_type": "deadline_expired"})
            return result.model_copy(update={"result": {**result.result, "decision": decision.model_dump(mode="json")},
                                             "completed_at": datetime.now(timezone.utc)})

        async def slow_snapshot():
            await asyncio.sleep(.02)
            return await snapshot()

        monkeypatch.setattr(case.cognitive, "prepare_qq_response", late)
        monkeypatch.setattr(case.cognitive, "get_snapshot", slow_snapshot)
        task = asyncio.create_task(ingress.prepare(request))
        event = await case.runtime.queue.get(timeout=1)
        result = await case.runtime.dispatch(event)
        detail = await task
        assert result.status == "completed" and detail["decision"]["reply"] == ERROR_FALLBACK_REPLY
        assert detail["decision"]["outcome"] == "error_fallback" and ingress._waiters == {}
        # A new request/replay cannot acquire another grace interval after TTL.
        with pytest.raises(TimeoutError, match="qq_turn_expired"):
            await ingress.prepare(request)
        assert len(case.cognitive.commands) == 1

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["ready", "natural_wait", "forged_fallback", "wrong_scope", "replayed", "after_grace"])
def test_ingress_late_completion_grace_is_bound_and_notice_only(case, monkeypatch, outcome):
    from harness.core.error_notice import ERROR_FALLBACK_REPLY
    from harness.runtime import qq_turns

    async def run():
        ingress = QQTurnIngress(case.control, case.config, watcher_path=case.watcher)
        request = case.request
        task = asyncio.create_task(ingress.prepare(request))
        event = await case.runtime.queue.get(timeout=1)
        command = SimpleNamespace(payload=request.model_dump(mode="json"), command_id="fixture-command",
            command_type="PrepareQQResponse", effective_request_hash=lambda: "a" * 64, event_id=event.event_id, receive_seq=1)
        result = await case.cognitive.prepare_qq_response(command)
        raw = result.result["decision"]
        raw.update(action="speak", reply=ERROR_FALLBACK_REPLY, reply_segments=[ERROR_FALLBACK_REPLY],
                   outcome="error_fallback", decision_source="expired", error_type="deadline_expired")
        if outcome == "ready":
            raw.update(outcome="ready", decision_source="model", error_type=None, reply="ordinary", reply_segments=["ordinary"])
        elif outcome == "natural_wait":
            raw.update(action="wait", outcome="natural_wait", decision_source="model", error_type=None, reply=None, reply_segments=[])
        elif outcome == "forged_fallback":
            raw.update(reply="arbitrary late content", reply_segments=["arbitrary late content"])
        elif outcome == "wrong_scope":
            raw["group_id"] = "99999"
        elif outcome == "replayed":
            raw.update(replayed=True, new_decision=False)
        offset = qq_turns.ERROR_FALLBACK_SEND_SECONDS + .01 if outcome == "after_grace" else .01

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return request.deadline_at + timedelta(seconds=offset)

        monkeypatch.setattr(qq_turns, "datetime", Clock)
        await ingress._completed("event.completed", {"event_id": event.event_id, "status": "completed",
            "decision_kind": "qq_prepare", "detail": result.result})
        with pytest.raises((TimeoutError, ValueError)):
            await task
        assert ingress._waiters == {}

    asyncio.run(run())


@pytest.mark.parametrize("second_exit", ["timeout", "cancel"])
def test_retry_subscription_cannot_remove_original_waiters_fallback_grace(case, monkeypatch, second_exit):
    from harness.core.error_notice import ERROR_FALLBACK_REPLY
    from harness.runtime import qq_turns

    async def run():
        ingress = QQTurnIngress(case.control, case.config, watcher_path=case.watcher)
        monkeypatch.setattr(qq_turns, "ERROR_FALLBACK_SEND_SECONDS", .5)
        request = case.request.model_copy(update={"deadline_at": datetime.now(timezone.utc) + timedelta(seconds=1.25)})
        first = asyncio.create_task(ingress.prepare(request))
        second = None
        try:
            event = await case.runtime.queue.get(timeout=1)
            # A was admitted with >=1 second and has fallback grace. The exact
            # same idempotent request B arrives with <1 second and has none.
            await asyncio.sleep(max(0, (request.deadline_at - datetime.now(timezone.utc)).total_seconds() - .7))
            assert 0 < (request.deadline_at - datetime.now(timezone.utc)).total_seconds() < 1
            second = asyncio.create_task(ingress.prepare(request))

            async def subscribed():
                while len(ingress._waiters.get(event.event_id, ())) != 2:
                    await asyncio.sleep(0)

            await asyncio.wait_for(subscribed(), .5)
            if second_exit == "cancel":
                second.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await second
            else:
                with pytest.raises(TimeoutError):
                    await second
            assert not first.done() and len(ingress._waiters[event.event_id]) == 1
            await asyncio.sleep(max(0, (request.deadline_at - datetime.now(timezone.utc)).total_seconds()) + .025)
            command = SimpleNamespace(payload=request.model_dump(mode="json"), command_id="fixture-command",
                command_type="PrepareQQResponse", effective_request_hash=lambda: "a" * 64,
                event_id=event.event_id, receive_seq=1)
            result = await case.cognitive.prepare_qq_response(command)
            result.result["decision"].update(action="speak", reply=ERROR_FALLBACK_REPLY,
                reply_segments=[ERROR_FALLBACK_REPLY], outcome="error_fallback",
                decision_source="expired", error_type="deadline_expired")
            await ingress._completed("event.completed", {"event_id": event.event_id, "status": "completed",
                "decision_kind": "qq_prepare", "detail": result.result})
            detail = await asyncio.wait_for(first, .25)
            assert detail["decision"]["outcome"] == "error_fallback"
            assert ingress._waiters == {} and case.runtime.queue.empty()
            accepted = [record for record in case.runtime.journal.records()
                        if record.event_id == event.event_id and record.record_type == "event_accepted"]
            assert len(accepted) == 1
        finally:
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)

    asyncio.run(run())
