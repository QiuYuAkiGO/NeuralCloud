"""Executable Runtime Supervisor control-plane service."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
import httpx

from harness.homeostasis.profiles import ProfileRevisionStore
from harness.embodiment.guards import BodyStateGuard
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.providers.mistyai import (
    MistyAIActionBackend,
    MistyAIBodyStateProvider,
    MistyAICapabilityProvider,
    MistyAIClient,
    MistyAIPerceptionBackend,
)
from harness.embodiment.ports import BodyStateProvider, PerceptionBackend
from harness.embodiment.providers.simulator import (
    DeterministicEmbodimentSimulator,
    SimulatorConfig,
)
from harness.research.manifest import load_experiment_manifest
from harness.runtime.action import ActionCoordinator
from harness.runtime.control import ControlPlane, create_control_app
from harness.runtime.mistyai_config import MistyAIConnectionSettings
from harness.runtime.monitor import MonitorRecorder, MonitorStore
from harness.runtime.perception import PerceptionGuard
from harness.runtime.rpc import HttpCognitiveClient, HttpHomeostasisClient
from harness.runtime.supervisor import RuntimeSupervisor

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("NC_RUNTIME_HOST", "127.0.0.1")
PORT = int(os.environ.get("NC_RUNTIME_PORT", "8770"))


async def _bridge_cognitive_events(
    *,
    base_url: str,
    token: str,
    control: ControlPlane,
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    while True:
        try:
            async with httpx.AsyncClient(timeout=None, headers=headers) as client:
                async with client.stream("GET", f"{base_url.rstrip('/')}/v1/events/stream") as response:
                    response.raise_for_status()
                    topic = "cognitive.ui"
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            topic = line[6:].strip() or "cognitive.ui"
                        elif line.startswith("data:"):
                            payload = json.loads(line[5:].strip())
                            if isinstance(payload, dict):
                                await control.event_stream.publish(topic, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await control.event_stream.publish(
                "cognitive.stream.degraded", {"error": str(exc)}
            )
            await asyncio.sleep(1.0)


def create_app():
    manifest_path = Path(
        os.environ.get(
            "NC_RUNTIME_MANIFEST_PATH",
            str(ROOT / "presets" / "runtime" / "development-demo.json"),
        )
    )
    state_dir = Path(os.environ.get("NC_RUNTIME_STATE_DIR", str(ROOT / "data" / "runtime")))
    cognitive_url = os.environ.get("NC_COGNITIVE_URL", "http://127.0.0.1:8771")
    homeostasis_url = os.environ.get("NC_HOMEOSTASIS_URL", "http://127.0.0.1:8772")
    service_token = os.environ.get("NC_SERVICE_TOKEN", "development-service-token")
    control_token = os.environ.get("NC_CONTROL_TOKEN", "development-control-token")
    manifest = load_experiment_manifest(manifest_path)
    cognitive = HttpCognitiveClient(cognitive_url, service_token=service_token)
    homeostasis = HttpHomeostasisClient(homeostasis_url, service_token=service_token)
    provider_default = getattr(manifest.embodiment, "body_provider", "none")
    provider_name = os.environ.get("NC_EMBODIMENT_PROVIDER", provider_default).strip().lower()
    embodiment_client: MistyAIClient | None = None
    embodiment_manager: EmbodimentManager | None = None
    action_coordinator: ActionCoordinator | None = None
    perception_backend: PerceptionBackend | None = None
    body_state_provider: BodyStateProvider | None = None
    provider_poll_seconds = 0.2
    if provider_name == "mistyai":
        mistyai_settings = MistyAIConnectionSettings.from_environment()
        provider_poll_seconds = mistyai_settings.poll_interval_seconds
        embodiment_client = MistyAIClient(
            mistyai_settings.base_url,
            token=mistyai_settings.token,
            timeout_seconds=mistyai_settings.timeout_seconds,
        )
        embodiment_manager = EmbodimentManager(
            manifest.embodiment,
            MistyAICapabilityProvider(embodiment_client),
        )
        action_coordinator = ActionCoordinator(
            MistyAIActionBackend(
                embodiment_client,
                poll_interval_seconds=provider_poll_seconds,
            ),
            capability_check=embodiment_manager.supports,
        )
        perception_backend = MistyAIPerceptionBackend(embodiment_client)
        body_state_provider = MistyAIBodyStateProvider(embodiment_client)
    elif provider_name == "simulator":
        simulator = DeterministicEmbodimentSimulator(
            SimulatorConfig(
                body_instance_id=manifest.embodiment.body_instance_id or "sim-body-01",
                seed=int(os.environ.get("NC_SIMULATOR_SEED", "0")),
                observation_only=os.environ.get("NC_SIMULATOR_OBSERVATION_ONLY", "false").lower()
                in {"1", "true", "yes"},
                offline=os.environ.get("NC_SIMULATOR_OFFLINE", "false").lower()
                in {"1", "true", "yes"},
                physiology=os.environ.get("NC_SIMULATOR_PHYSIOLOGY", "false").lower()
                in {"1", "true", "yes"},
            )
        )
        embodiment_manager = EmbodimentManager(manifest.embodiment, simulator)
        action_coordinator = ActionCoordinator(
            simulator,
            capability_check=embodiment_manager.supports,
        )
        perception_backend = simulator
        body_state_provider = simulator
    elif provider_name not in {"none", "simulator"}:
        raise ValueError("NC_EMBODIMENT_PROVIDER must be none, mistyai, or simulator")
    supervisor = RuntimeSupervisor(
        manifest=manifest,
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=state_dir,
        embodiment_manager=embodiment_manager,
        action_coordinator=action_coordinator,
    )
    if perception_backend is not None:
        supervisor.attach_perception_guard(
            PerceptionGuard(
                perception_backend,
                supervisor.submit_event,
                interval_seconds=provider_poll_seconds,
                max_state_age_seconds=manifest.embodiment.max_state_age_seconds,
                source="embodiment",
            )
        )
    if body_state_provider is not None:
        supervisor.attach_body_state_guard(
            BodyStateGuard(
                body_state_provider,
                supervisor.submit_event,
                interval_seconds=manifest.embodiment.body_state_interval_seconds,
                max_age_seconds=manifest.embodiment.body_state_max_age_seconds,
                expected_body_instance_id=manifest.embodiment.body_instance_id,
                source="embodiment",
            )
        )
    control = ControlPlane(supervisor, token=control_token)
    retention_days = int(os.environ.get("NC_MONITOR_RETENTION_DAYS", "7"))
    try:
        monitor_store = MonitorStore(
            Path(os.environ.get("NC_MONITOR_DB", str(state_dir / "observability.sqlite3"))),
            retention_days=retention_days,
        )
    except Exception as exc:
        monitor_store = MonitorStore(":memory:", retention_days=retention_days)
        monitor_store.startup_error = f"persistent monitor store unavailable: {exc}"
    monitor = MonitorRecorder(
        monitor_store,
        journal=supervisor.journal,
        status_provider=supervisor.status,
        trace_dir=Path(os.environ.get("NC_TRACE_DIR", str(ROOT / "traces"))),
        sample_seconds=float(os.environ.get("NC_MONITOR_SAMPLE_SECONDS", "10")),
    )
    control.event_stream.add_listener(monitor.enqueue)
    profile_store = ProfileRevisionStore(
        profile_dir=ROOT / "data" / "homeostasis" / "profiles",
        manifest_dir=ROOT / "data" / "runtime" / "manifests",
        pending_path=ROOT / "data" / "runtime" / "pending-start.json",
    )
    app = create_control_app(control, profile_store=profile_store, monitor=monitor)

    async def startup() -> None:
        await monitor.start()
        app.state.runtime_task = asyncio.create_task(supervisor.run(), name="runtime-supervisor")
        app.state.cognitive_bridge = asyncio.create_task(
            _bridge_cognitive_events(base_url=cognitive_url, token=service_token, control=control),
            name="cognitive-event-bridge",
        )

    async def shutdown() -> None:
        bridge = getattr(app.state, "cognitive_bridge", None)
        if bridge is not None:
            bridge.cancel()
            with suppress(asyncio.CancelledError):
                await bridge
        await supervisor.stop(reason="runtime service shutdown")
        if embodiment_client is not None:
            await embodiment_client.close()
        task = getattr(app.state, "runtime_task", None)
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
        await monitor.close()

    app.state.supervisor = supervisor
    app.state.control = control
    app.state.monitor = monitor
    app.router.add_event_handler("startup", startup)
    app.router.add_event_handler("shutdown", shutdown)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
