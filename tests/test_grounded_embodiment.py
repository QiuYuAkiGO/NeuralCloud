from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import httpx

from harness.body.mistyai import BodyMonitorError, MistyBodyClient, MistyBodyMonitor
from harness.compiler.compiler import ContextCompiler
from harness.config import Settings
from harness.contracts.body import BodyStateSnapshot
from harness.contracts.envelope import DomainCommandEnvelope
from harness.core.inference import (
    InteroceptiveState,
    SituationActor,
    SituationState,
    SituationTrigger,
)
from harness.embodiment.capabilities import (
    ACTION_EXECUTE,
    ACTION_VERIFY,
    EVIDENCE_REFERENCE,
    OBSERVATION_SNAPSHOT,
    PHYSIOLOGY_BODY_STATE,
)
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.research.manifest import ExperimentManifest, load_experiment_manifest
from harness.runtime.models import (
    AgencyState,
    RuntimeEvent,
    RuntimeEventPriority,
    RuntimeEventType,
)
from harness.runtime.supervisor import RuntimeStateError, RuntimeSupervisor

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
EVIDENCE = "evidence://sha256/" + "a" * 64


def _canonical_contract_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def _body_state(
    *,
    revision: int = 1,
    episode_id: str = "episode-1",
    ratio: float = 0.2,
    captured_at: datetime = NOW,
    terminal: bool = False,
) -> BodyStateSnapshot:
    return BodyStateSnapshot.model_validate(
        {
            "body_instance_id": "misty-body-01",
            "episode_id": episode_id,
            "revision": revision,
            "captured_at": captured_at,
            "energy": {
                "capacity_units": 100,
                "available_units": ratio * 100,
                "available_ratio": ratio,
                "delta_units_per_minute": -1,
                "charging": False,
                "estimated_time_to_terminal_seconds": 0 if terminal else 1200,
                "terminal": terminal,
            },
            "evidence_refs": [EVIDENCE],
        }
    )


def _manifest_payload() -> dict[str, Any]:
    return {
        "experiment_id": "grounded-test",
        "revision": 1,
        "control_mode": "development_debug",
        "condition": "argmin_efe",
        "persona_version": "v3",
        "homeostatic_profile": "functional-proxy-v1",
        "random_seed": 1,
        "initial_snapshot_ref": "snapshot://base",
        "model_version": "mock",
        "prompt_version": "v1",
        "schema_version": "v1",
        "a_version": "A-v1",
        "b_version": "B-v1",
        "c_version": "C-v1",
        "precision_version": "precision-v1",
        "coupling_version": "coupling-v1",
        "priority_policy": "runtime-v1",
        "environment": "test",
        "retention": "test",
        "watchdog_action": "pause_preserve_state",
    }


def test_manifest_defaults_off_and_rejects_active_or_unsafe_flags() -> None:
    manifest = ExperimentManifest.model_validate(_manifest_payload())
    assert manifest.embodiment.mode == "off"
    assert manifest.embodiment.body_provider == "none"

    payload = _manifest_payload()
    payload["embodiment"] = {
        "mode": "grounded",
        "body_provider": "mistyai",
        "body_instance_id": "misty-body-01",
    }
    assert ExperimentManifest.model_validate(payload).embodiment.mode == "grounded"

    payload["embodiment"] = {
        "mode": "active",
        "body_provider": "mistyai",
        "body_instance_id": "misty-body-01",
    }
    with pytest.raises(ValueError, match="not implemented"):
        ExperimentManifest.model_validate(payload)

    payload["embodiment"] = {
        "mode": "observe",
        "body_provider": "mistyai",
        "body_instance_id": "misty-body-01",
        "body_memory": True,
    }
    with pytest.raises(ValueError, match="must be false"):
        ExperimentManifest.model_validate(payload)


def test_grounded_mistyai_preset_requires_core_provider_capabilities() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = load_experiment_manifest(root / "presets/runtime/grounded-mistyai-v1.json")
    requirements = {
        item.capability: item.required
        for item in manifest.embodiment.capability_requirements
    }

    assert manifest.embodiment.required_for_runtime is True
    assert requirements[ACTION_EXECUTE] is True
    assert requirements[ACTION_VERIFY] is True
    assert requirements[OBSERVATION_SNAPSHOT] is True
    assert requirements[EVIDENCE_REFERENCE] is True
    assert requirements[PHYSIOLOGY_BODY_STATE] is False


def test_pinned_mistyai_body_schema_hash_matches_fixture() -> None:
    root = Path(__file__).resolve().parents[1]
    schema_path = root / "tests" / "fixtures" / "mistyai" / "body-state.schema.json"
    expected = (
        root / "tests" / "fixtures" / "mistyai" / "body-state.schema.sha256"
    ).read_text(encoding="utf-8").strip()
    content = _canonical_contract_bytes(schema_path)
    assert hashlib.sha256(content).hexdigest() == expected
    schema = json.loads(content)
    assert schema["properties"]["schema_version"]["const"] == "body-state-v1"


def test_live_mistyai_contract_matches_fixture_when_configured() -> None:
    source = os.environ.get("MISTYAI_CONTRACT_DIR")
    if not source:
        pytest.skip("set MISTYAI_CONTRACT_DIR for the cross-repository merge gate")
    root = Path(__file__).resolve().parents[1]
    fixtures = root / "tests" / "fixtures" / "mistyai"
    generated = Path(source) / "schemas"
    for name in ("body-state.schema.json", "body-state-capture-response.schema.json"):
        assert _canonical_contract_bytes(generated / name) == _canonical_contract_bytes(
            fixtures / name
        )

def test_absolute_observation_uses_frozen_gain_without_coupling(tmp_path) -> None:
    async def scenario() -> None:
        profile = load_profile("presets/homeostasis/functional-proxy-v1.json")
        engine = HomeostasisEngine(profile, state_dir=tmp_path, now=NOW)
        before = {key: value.value for key, value in engine.snapshot.dimensions.items()}
        snapshot = _body_state(ratio=0.1)
        observation = {
            "source": "system_sensor",
            "observed_values": {"energy_reserve": snapshot.energy.available_ratio},
            "body_instance_id": snapshot.body_instance_id,
            "episode_id": snapshot.episode_id,
            "body_revision": snapshot.revision,
            "captured_at": snapshot.captured_at,
            "evidence_refs": snapshot.evidence_refs,
            "experiment_mode": "grounded",
        }
        command = DomainCommandEnvelope[dict[str, Any]](
            command_type="ApplyObservation",
            event_id="body-event-1",
            receive_seq=1,
            correlation_id="body-correlation",
            runtime_instance_id="runtime-test",
            manifest_revision=1,
            idempotency_key="body:r1",
            issued_at=NOW,
            deadline=NOW + timedelta(days=1),
            expected_revision=0,
            payload={"observation": observation},
        )
        result = await engine.apply_observation(command)
        gain = profile.source_precision["system_sensor"]
        expected = before["energy_reserve"] + gain * (0.1 - before["energy_reserve"])
        assert engine.snapshot.dimensions["energy_reserve"].value == pytest.approx(expected)
        for dimension, value in before.items():
            if dimension != "energy_reserve":
                assert engine.snapshot.dimensions[dimension].value == value
        trace = result.result["trace"]
        assert trace["coupling_impacts"] == {}
        assert trace["assimilation_gain"] == gain
        assert trace["body_revision"] == 1

    asyncio.run(scenario())


def test_context_compiler_uses_neutral_or_blinded_resource_labels() -> None:
    for label in ("internal_resource", "resource_A"):
        interoception = InteroceptiveState(
            resource_label=label,
            level="low",
            trend=-0.01,
            time_to_action_limit_seconds=1200,
            confidence=1,
            captured_at=NOW,
            evidence_refs=[EVIDENCE],
        )
        situation = SituationState(
            trigger=SituationTrigger(ref="evt-1"),
            utterance="你好",
            actor=SituationActor(),
            interoception=interoception,
        )
        context = ContextCompiler(Settings(context_token_budget=500)).compile(situation)
        segment = next(item for item in context.segments if item.kind == "interoception")
        assert label in segment.text
        assert "emotion" not in segment.text
        assert "recommend" not in segment.text
        assert "threshold" not in segment.text


def test_monitor_rejects_stale_and_revision_regression_without_updating_belief() -> None:
    class FakeClient:
        def __init__(self, snapshots: list[BodyStateSnapshot]) -> None:
            self.snapshots = snapshots

        async def validate_contract(self, *, body_instance_id: str) -> None:
            assert body_instance_id == "misty-body-01"

        async def capture(self) -> BodyStateSnapshot:
            return self.snapshots.pop(0)

        async def verify_evidence(self, snapshot: BodyStateSnapshot) -> None:
            assert snapshot.evidence_refs

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        accepted: list[int] = []

        async def on_snapshot(snapshot: BodyStateSnapshot) -> None:
            accepted.append(snapshot.revision)

        async def no_op(*_args: object) -> None:
            return None

        client = FakeClient(
            [
                _body_state(revision=2),
                _body_state(revision=1),
                _body_state(revision=3, captured_at=NOW - timedelta(seconds=11)),
            ]
        )
        monitor = MistyBodyMonitor(
            client=client,  # type: ignore[arg-type]
            body_instance_id="misty-body-01",
            poll_interval_seconds=5,
            max_state_age_seconds=10,
            future_clock_tolerance_seconds=2,
            on_snapshot=on_snapshot,
            on_degraded=no_op,
            on_recovered=no_op,
        )
        await monitor.poll_once(now=NOW)
        with pytest.raises(BodyMonitorError, match="backwards"):
            await monitor.poll_once(now=NOW)
        with pytest.raises(BodyMonitorError, match="stale"):
            await monitor.poll_once(now=NOW)
        assert accepted == [2]
        assert monitor.last_snapshot is not None and monitor.last_snapshot.revision == 2

    asyncio.run(scenario())


def test_http_client_rejects_corrupt_body_evidence() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/capabilities":
                return httpx.Response(
                    200,
                    json={
                        "schema_version": "body-contract-v1",
                        "body_instance_id": "misty-body-01",
                        "contract_versions": ["body-contract-v1"],
                        "desktop_backend": "xvfb-x11",
                        "display": {"name": ":99", "width": 1280, "height": 720, "scale": 1},
                        "actuators": ["gui"],
                        "skills": [],
                        "network_phase": 0,
                        "policy_profile": "m0-default-deny",
                        "extensions": {
                            "body_state_v1": True,
                            "virtual_resource_ledger": True,
                            "social_observation_v1": False,
                            "experiment_control": False,
                        },
                    },
                )
            assert request.url.path.endswith("/" + "a" * 64)
            return httpx.Response(200, content=b"corrupt")

        transport = httpx.MockTransport(handler)
        raw_client = httpx.AsyncClient(base_url="http://misty", transport=transport)
        client = MistyBodyClient("http://misty", token="test-token", client=raw_client)
        capabilities = await client.validate_contract(body_instance_id="misty-body-01")
        assert capabilities.extensions["body_state_v1"] is True
        with pytest.raises(BodyMonitorError, match="digest mismatch"):
            await client.verify_evidence(_body_state())
        await raw_client.aclose()

    asyncio.run(scenario())


def test_runtime_observe_grounded_and_terminal_reset_are_isolated(tmp_path) -> None:
    class UnusedBodyClient:
        async def close(self) -> None:
            return None

    def manifest(mode: str) -> ExperimentManifest:
        payload = _manifest_payload()
        payload["embodiment"] = {
            "mode": mode,
            "body_provider": "mistyai",
            "body_instance_id": "misty-body-01",
        }
        return ExperimentManifest.model_validate(payload)

    def event(snapshot: BodyStateSnapshot, event_type: RuntimeEventType) -> RuntimeEvent:
        return RuntimeEvent(
            event_id=f"body-{snapshot.revision}",
            receive_seq=snapshot.revision,
            correlation_id="body-test",
            event_type=event_type,
            priority=(
                RuntimeEventPriority.CONTROL
                if event_type == RuntimeEventType.AGENCY_TERMINAL
                else RuntimeEventPriority.PERCEPTION
            ),
            source="mistyai",
            source_time=snapshot.captured_at,
            received_at=snapshot.captured_at,
            idempotency_key=f"body:r{snapshot.revision}",
            manifest_revision=1,
            payload=snapshot.model_dump(mode="json"),
        )

    async def scenario() -> None:
        profile = load_profile("presets/homeostasis/functional-proxy-v1.json")
        observe_engine = HomeostasisEngine(profile, state_dir=tmp_path / "observe-homeo", now=NOW)
        observe = RuntimeSupervisor(
            manifest=manifest("observe"),
            cognitive=object(),  # type: ignore[arg-type]
            homeostasis=observe_engine,
            body_client=UnusedBodyClient(),  # type: ignore[arg-type]
            state_dir=tmp_path / "observe-runtime",
        )
        observed = _body_state(revision=1, ratio=0.2)
        result = await observe._apply_body_state(
            event(observed, RuntimeEventType.BODY_STATE), 1
        )
        assert result.detail["homeostasis_updated"] is False
        assert observe_engine.snapshot.revision == 0
        assert (tmp_path / "observe-runtime" / "body-analysis.jsonl").is_file()

        grounded_engine = HomeostasisEngine(
            profile, state_dir=tmp_path / "grounded-homeo", now=NOW
        )
        grounded = RuntimeSupervisor(
            manifest=manifest("grounded"),
            cognitive=object(),  # type: ignore[arg-type]
            homeostasis=grounded_engine,
            body_client=UnusedBodyClient(),  # type: ignore[arg-type]
            state_dir=tmp_path / "grounded-runtime",
        )
        grounded_result = await grounded._apply_body_state(
            event(observed, RuntimeEventType.BODY_STATE), 1
        )
        assert grounded_result.detail["homeostasis_updated"] is True
        assert grounded_engine.snapshot.revision == 1

        terminal = _body_state(revision=2, ratio=0, terminal=True)
        await grounded._dispatch_control(
            event(terminal, RuntimeEventType.AGENCY_TERMINAL), 2
        )
        assert grounded.status().agency_state == AgencyState.TERMINAL
        assert grounded.body_monitor is not None
        grounded.body_monitor._last_snapshot = terminal
        with pytest.raises(RuntimeStateError, match="new body episode"):
            await grounded.reset_agency()

        reset = _body_state(revision=3, episode_id="episode-2", ratio=0.5)
        grounded.body_monitor._last_snapshot = reset
        await grounded._apply_body_state(
            event(reset, RuntimeEventType.BODY_STATE), 3
        )
        await grounded.reset_agency()
        assert grounded.status().agency_state == AgencyState.AVAILABLE

    asyncio.run(scenario())
