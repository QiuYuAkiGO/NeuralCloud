"""Real Runtime plumbing with fake HTTP models and a neutral test profile.

This verifies 100-turn recording and correlation. The user/runtime precision
override is a test fixture, never evidence that the production profile can
complete a live 100-turn experiment without its watchdog pausing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import httpx

from eval.basic_runtime.babel import RunOptions, RuntimeClient, load_story, run_dialogue
from eval.basic_runtime.babel_evidence import read_json
from gui.presets import load_builtin
from harness.config import Settings
from harness.contracts.envelope import DomainCommandEnvelope
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.llm.client import OpenAICompatClient, RoutingLLMClient
from harness.memory.service import build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline
from harness.research.manifest import load_experiment_manifest
from harness.runtime import ControlPlane, PipelineCognitiveAdapter, RuntimeSupervisor, create_control_app
from harness.runtime.monitor import MonitorRecorder, MonitorStore
from infra.trace.serialize import serialize_stage


ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class _StageBridge:
    """Send the production observer wire shape into the real Monitor store."""

    def __init__(self, store):
        self.store = store
        self.run_id = None

    def bind(self, run_id):
        self.run_id = run_id

    def on_stage(self, kind, result, payload):
        self.store.append_event("cognitive.ui", {
            "type": "final" if kind == "final" else "stage",
            "run_id": self.run_id, "stage": kind,
            "latency_ms": payload.get("latency_ms", 0),
            "data": serialize_stage(kind, result),
            "meta": {key: value for key, value in payload.items() if key != "latency_ms"},
        })


class _PumpedRuntimeClient(RuntimeClient):
    def __init__(self, app, supervisor, recorder, clock):
        super().__init__("http://runtime.test", "fixture-control-token", transport=httpx.ASGITransport(app=app))
        self.supervisor, self.recorder, self.clock = supervisor, recorder, clock

    async def get(self, path, **params):
        # Run accepted work through actual dispatch, but do not wait for wall
        # clock heartbeat deadlines in this plumbing-only fixture.
        while self.supervisor.lifecycle.value == "RUNNING":
            event = await self.supervisor.queue.get(timeout=0)
            if event is None:
                break
            await self.supervisor.dispatch(event)
        await self.recorder.flush()
        self.clock.now += .01
        return await super().get(path, **params)


def test_babel_100_turns_real_runtime_http_journal_and_homeostasis(tmp_path):
    settings = Settings(
        trace_dir=tmp_path / "traces", call_log_enabled=True,
        llm_model="offline-http-fixture", llm_base_url="https://model.test/v1", llm_api_key="fixture-key",
        parser_llm_enabled=False, planner_llm_enabled=False, tier_llm_enabled=False,
        validator_sample_rate=0, continuation_enabled=False,
    )
    requests = []

    def model_response(request):
        assert request.url.host == "model.test"
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        text = "{}" if body.get("temperature") == .1 else "我在认真听。这个片段让我想到，人们也许还需要时间慢慢理解彼此。"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        })

    endpoint = OpenAICompatClient(settings)
    endpoint.close()
    endpoint._client = httpx.Client(transport=httpx.MockTransport(model_response), trust_env=False)
    llm = RoutingLLMClient(endpoint, endpoint)
    memory = build_inmemory_service(settings=settings, llm=llm)
    persona = load_builtin("misty").persona
    memory.bootstrap_persona(persona)
    pipeline = InferencePipeline(memory, settings=settings, llm=llm)
    adapter = PipelineCognitiveAdapter(pipeline, state_path=tmp_path / "cognitive/state.json", default_session_id="sess_fixture_misty")

    async def ui_snapshot():
        return {
            "mode": "live", "persona": persona.model_dump(mode="json"),
            "session": {"session_id": "sess_fixture_misty", "preset_id": "misty"},
            "llm": {"mode": "live", "base_url": settings.llm_base_url, "model": settings.llm_model, "reachable": True},
            "inference_config": {"call_log_enabled": True, "continuation_enabled": False},
            "speakers": {"user_name": "测试讲述者"},
        }

    adapter.ui_snapshot = ui_snapshot
    production_profile = load_profile(ROOT / "presets/homeostasis/functional-proxy-v1.json")
    neutral_profile = production_profile.model_copy(update={
        "source_precision": {**production_profile.source_precision, "user": 0, "runtime": 0},
    })
    homeostasis = HomeostasisEngine(neutral_profile, state_dir=tmp_path / "homeostasis")
    supervisor = RuntimeSupervisor(
        manifest=load_experiment_manifest(ROOT / "presets/runtime/development-demo.json"),
        cognitive=adapter, homeostasis=homeostasis, state_dir=tmp_path / "runtime",
    )
    monitor = MonitorStore(tmp_path / "monitor.sqlite3")
    recorder = MonitorRecorder(monitor, journal=supervisor.journal, status_provider=supervisor.status, trace_dir=settings.trace_dir)
    pipeline.observer = _StageBridge(monitor)
    control = ControlPlane(supervisor, token="fixture-control-token")
    control.event_stream.add_listener(recorder.enqueue)
    app = create_control_app(control, monitor=recorder)
    clock = _Clock()
    client = _PumpedRuntimeClient(app, supervisor, recorder, clock)
    output = tmp_path / "experiment"

    async def scenario():
        try:
            await supervisor.boot_and_recover()
            result = await run_dialogue(
                client, story=load_story(), directory=output, clock=clock,
                options=RunOptions(interval_seconds=10, turn_timeout_seconds=300, drain_seconds=0, use_stream=False),
                metadata={"test_fixture": "offline_http_and_neutral_appraisal_not_production_profile_acceptance"},
                artifact_paths=(tmp_path / "runtime/journal", settings.trace_dir, settings.trace_dir / "calls"),
                secrets=["fixture-key", "fixture-control-token"],
            )
            assert result["status"] == "COMPLETED", result
            assert (await adapter.get_snapshot()).revision == 100
            assert len(memory.stores.episodic.list_all()) == 200
            assert len(pipeline.session_store.recent("sess_fixture_misty")) == pipeline.session_store.window
            assert not homeostasis.snapshot.watchdog
        finally:
            await client.close()
            await supervisor.stop(reason="offline test finished")
            await recorder.close()
            llm.close()

    asyncio.run(scenario())
    turns = [read_json(path) for path in sorted((output / "turns").glob("*.json"))]
    assert len(turns) == 100
    assert all(turn["status"] == "COMPLETED" and turn["analysis"]["state_exact"] for turn in turns)
    assert len({turn["event_id"] for turn in turns}) == 100
    assert len({turn["correlation_id"] for turn in turns}) == 100
    assert all(turn["analysis"]["reply"] for turn in turns)
    assert turns[90]["first_overall_reflection"] is True
    assert all(turn["interval_actual_seconds"] >= 10 - 1e-9 for turn in turns[:-1])
    assert all({command["command_type"] for command in turn["analysis"]["commands"]} >= {"ApplyEvent", "PrepareResponse", "CommitInteraction"} for turn in turns)
    calls = [json.loads(line) for line in (output / "model_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len([call for call in calls if call["phase"] == "prepare" and call["role"] == "main"]) == 100
    assert any(call["phase"] == "commit" and call["role"] == "aux" for call in calls)
    assert all(call["status"] == "success" and call["command_id"] for call in calls)
    assert all(call["usage"]["total_tokens"] == 16 for call in calls)
    assert {call["correlation_id"] for call in calls} == {turn["correlation_id"] for turn in turns}
    assert len(requests) == len(calls)
    summary = read_json(output / "summary.json")
    assert summary["completed_turns"] == 100
    assert summary["completed_messages"] == 200
    assert (output / "raw/journal.jsonl").is_file()
    assert read_json(output / "call_files.json")


def test_existing_real_profile_watchdog_preserves_state_and_sends_no_story(tmp_path):
    """The unmodified profile can pause; the recorder must never force resume."""
    settings = Settings(parser_llm_enabled=False, planner_llm_enabled=False, tier_llm_enabled=False, call_log_enabled=False)
    memory = build_inmemory_service(settings=settings)
    memory.bootstrap_persona(load_builtin("misty").persona)
    adapter = PipelineCognitiveAdapter(InferencePipeline(memory, settings=settings), state_path=tmp_path / "cognitive.json")
    profile = load_profile(ROOT / "presets/homeostasis/functional-proxy-v1.json")
    now = datetime.now(UTC)
    homeostasis = HomeostasisEngine(profile, state_dir=tmp_path / "homeostasis", now=now)
    supervisor = RuntimeSupervisor(
        manifest=load_experiment_manifest(ROOT / "presets/runtime/development-demo.json"),
        cognitive=adapter, homeostasis=homeostasis, state_dir=tmp_path / "runtime",
    )
    monitor = MonitorStore(tmp_path / "monitor.sqlite3")
    recorder = MonitorRecorder(monitor, journal=supervisor.journal, status_provider=supervisor.status, trace_dir=tmp_path / "traces")
    control = ControlPlane(supervisor, token="fixture-control-token")
    app = create_control_app(control, monitor=recorder)
    client = RuntimeClient("http://runtime.test", "fixture-control-token", transport=httpx.ASGITransport(app=app))
    output = tmp_path / "paused-experiment"

    async def scenario():
        sequence = 0

        async def apply(kind):
            nonlocal sequence
            sequence += 1
            command = DomainCommandEnvelope[dict](
                command_type=kind, event_id=f"seed-{sequence}", receive_seq=sequence,
                correlation_id=f"seed-{sequence}", runtime_instance_id="paused-fixture",
                manifest_revision=3, idempotency_key=f"seed:{sequence}",
                issued_at=now, deadline=now + timedelta(minutes=1),
                expected_revision=homeostasis.snapshot.revision,
                payload={"event": {
                    "event_type": "heartbeat" if kind == "ApplyHeartbeat" else "user_message",
                    "source": "scheduler" if kind == "ApplyHeartbeat" else "user",
                    "source_time": now,
                    "payload": {} if kind == "ApplyHeartbeat" else {"text": "很长的一段故事。" * 30},
                }},
            )
            method = homeostasis.apply_heartbeat if kind == "ApplyHeartbeat" else homeostasis.apply_event
            assert (await method(command)).status == "applied"

        try:
            # Fixed-clock stress fixture uses the real appraisal and watchdog;
            # it makes no assertion about elapsed time to pause in production.
            for _ in range(30):
                await apply("ApplyEvent")
            for _ in range(3):
                await apply("ApplyHeartbeat")
            assert homeostasis.snapshot.watchdog
            saved = homeostasis.snapshot.model_dump(mode="json")
            await supervisor.boot_and_recover()
            await recorder.flush()
            result = await run_dialogue(
                client, story=load_story(), directory=output, clock=_Clock(),
                options=RunOptions(use_stream=False, drain_seconds=0),
                artifact_paths=(tmp_path / "runtime/journal", tmp_path / "traces", tmp_path / "traces/calls"),
            )
            assert result["status"] == "PAUSED"
            assert not list((output / "turns").glob("*.json"))
            assert homeostasis.snapshot.model_dump(mode="json") == saved
            assert not memory.stores.episodic.list_all()
            assert supervisor.lifecycle.value == "PAUSED"
        finally:
            await client.close()
            await supervisor.stop(reason="offline paused test finished")
            await recorder.close()

    asyncio.run(scenario())
    assert read_json(output / "summary.json")["attempted_turns"] == 0
