"""Executable Runtime Supervisor control-plane service."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
import httpx
from fastapi import FastAPI

from harness.homeostasis.profiles import ProfileRevisionStore
from harness.memory.qq_observations import QQObservationStore
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
from harness.runtime.ownership import SharedDomainLease, resolve_control_mode, require_service_token

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


def _startup_settings():
    manifest_path = Path(
        os.environ.get(
            "NC_RUNTIME_MANIFEST_PATH",
            str(ROOT / "presets" / "runtime" / "shared-misty-v1.json"),
        )
    )
    state_dir = Path(os.environ.get("NC_RUNTIME_STATE_DIR", str(ROOT / "data" / "runtime")))
    manifest = load_experiment_manifest(manifest_path)
    mode = resolve_control_mode(manifest=manifest)
    require_service_token(os.environ.get("NC_SERVICE_TOKEN"), name="NC_SERVICE_TOKEN", control_mode=mode)
    require_service_token(os.environ.get("NC_CONTROL_TOKEN"), name="NC_CONTROL_TOKEN", control_mode=mode)
    isolation = os.environ.get("NC_ISOLATED_DEVELOPMENT", "0")
    if isolation not in {"0", "1"}:
        raise ValueError("NC_ISOLATED_DEVELOPMENT must be 0 or 1")
    return manifest, state_dir, SharedDomainLease("runtime", state_dir, control_mode=mode, isolated=isolation == "1")


def create_app():
    @asynccontextmanager
    async def lifespan(app):
        manifest, state_dir, lease = _startup_settings()
        with lease:
            inner = _create_owned_app(manifest, state_dir)
            app.state.owner_lease = lease
            for name in ("supervisor", "control", "monitor"):
                setattr(app.state, name, getattr(inner.state, name))
            app.mount("/", inner)
            mount = app.routes[-1]
            try:
                async with inner.router.lifespan_context(inner):
                    yield
            finally:
                app.routes.remove(mount)
    return FastAPI(title="NeuralCloud Runtime service", lifespan=lifespan)


def _create_owned_app(manifest, state_dir):
    from harness.runtime.web_config import WebConfigStore
    # Lock one revision after acquiring ownership; authorization and credentials
    # must come from the same immutable startup bundle.
    web_store = WebConfigStore(state_dir)
    manifest = web_store.apply_manifest(manifest)
    from harness.runtime.server_config import ServerConfigStore
    server_store = ServerConfigStore(state_dir)
    manifest = server_store.apply_manifest(manifest)
    cognitive_url = os.environ.get("NC_COGNITIVE_URL", "http://127.0.0.1:8771")
    homeostasis_url = os.environ.get("NC_HOMEOSTASIS_URL", "http://127.0.0.1:8772")
    service_token = require_service_token(os.environ.get("NC_SERVICE_TOKEN"), name="NC_SERVICE_TOKEN", control_mode=manifest.control_mode)
    control_token = require_service_token(os.environ.get("NC_CONTROL_TOKEN"), name="NC_CONTROL_TOKEN", control_mode=manifest.control_mode)
    cognitive = HttpCognitiveClient(cognitive_url, service_token=service_token, timeout=manifest.command_timeout_seconds)
    homeostasis = HttpHomeostasisClient(homeostasis_url, service_token=service_token, timeout=manifest.command_timeout_seconds)
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
    from harness.board.config import BoardSettings
    from harness.runtime.board import BoardController
    board_settings = BoardSettings.from_environment()
    supervisor.board = None
    if board_settings and board_settings.enabled:
        from harness.embodiment.providers.board import BoardProvider, BOARD_CAPABILITIES
        from harness.embodiment.capabilities import CapabilityRequirement
        from harness.research.manifest import EmbodimentCondition
        board_provider = BoardProvider(condition=EmbodimentCondition(mode="grounded",
            body_instance_id=board_settings.body_instance_id, capability_requirements=[
                CapabilityRequirement(capability=c, required=True) for c in BOARD_CAPABILITIES]),
            client=board_settings.client(), scope=board_settings.scope)
        supervisor.board = BoardController(supervisor, board_settings, state_dir, provider=board_provider)
    from harness.diary.config import DiarySettings
    from harness.runtime.diary import DiaryController
    supervisor.diary = DiaryController(supervisor, DiarySettings.from_environment(), state_dir)
    from harness.runtime.voice_config import VoiceSettings
    voice_settings = VoiceSettings.from_environment()
    supervisor.voice = None
    if voice_settings and voice_settings.enabled:
        from harness.embodiment.capabilities import CapabilityRequirement
        from harness.embodiment.providers.voice import VOICE_CAPABILITIES, VoiceClient, VoiceProvider
        from harness.research.manifest import EmbodimentCondition
        from harness.runtime.voice import VoiceController
        voice_provider = VoiceProvider(
            condition=EmbodimentCondition(
                mode=voice_settings.mode,
                body_instance_id=voice_settings.body_instance_id,
                capability_requirements=[
                    CapabilityRequirement(capability=capability, required=True)
                    for capability in VOICE_CAPABILITIES
                ],
            ),
            client=VoiceClient(voice_settings.base_url, token_file=voice_settings.token_file,
                               timeout_seconds=voice_settings.timeout_seconds),
            scope=voice_settings.scope,
        )
        supervisor.voice = VoiceController(supervisor, voice_settings, state_dir, provider=voice_provider)
    from harness.runtime.web import WebController
    from harness.embodiment.providers.web import TavilyWebProvider
    from harness.research.manifest import EmbodimentCondition
    from harness.embodiment.capabilities import CapabilityRequirement
    web_settings = web_store.load_settings()

    def web_test_provider(settings, key):
        return TavilyWebProvider(EmbodimentCondition(mode="observe", body_instance_id=settings.body_instance_id,
            capability_requirements=[CapabilityRequirement(capability=c, required=False) for c in ("web.search.v1", "web.read.v1")]),
            settings.base_url, key, settings.body_instance_id, settings.timeout_seconds)

    web_provider = None
    if web_settings is not None and manifest.web_access.enabled:
        web_key = web_store.load_api_key()
        if web_key:
            web_provider = TavilyWebProvider(EmbodimentCondition(mode="observe", body_instance_id=web_settings.body_instance_id,
                capability_requirements=manifest.web_access.capability_requirements), web_settings.base_url,
                web_key, web_settings.body_instance_id, web_settings.timeout_seconds)
    supervisor.web_config_store = web_store
    supervisor.web = WebController(supervisor, web_settings, state_dir, provider=web_provider,
        config_store=web_store, test_provider_factory=web_test_provider)
    from harness.runtime.server import ServerController
    from harness.embodiment.providers.server import SSHServerProvider
    server_settings = server_store.load_settings()

    def server_test_provider(settings, key):
        return SSHServerProvider(EmbodimentCondition(mode="observe", body_instance_id=settings.body_instance_id,
            capability_requirements=[CapabilityRequirement(capability=c, required=False) for c in ("server.observe.v1", "server.execute.v1")]),
            host=settings.host, port=settings.port, username=settings.username, host_key=settings.host_key,
            private_key=key, body_instance_id=settings.body_instance_id,
            connect_timeout_seconds=settings.connect_timeout_seconds,
            default_timeout_seconds=settings.default_timeout_seconds)

    server_provider = None
    if server_settings is not None and manifest.server_access.enabled:
        server_key = server_store.load_private_key()
        if server_key and server_settings.host_key:
            server_provider = SSHServerProvider(EmbodimentCondition(mode="observe", body_instance_id=server_settings.body_instance_id,
                capability_requirements=manifest.server_access.capability_requirements),
                host=server_settings.host, port=server_settings.port, username=server_settings.username,
                host_key=server_settings.host_key, private_key=server_key,
                body_instance_id=server_settings.body_instance_id,
                connect_timeout_seconds=server_settings.connect_timeout_seconds,
                default_timeout_seconds=server_settings.default_timeout_seconds)
    supervisor.server_config_store = server_store
    supervisor.server = ServerController(supervisor, server_settings, state_dir, provider=server_provider,
        config_store=server_store, test_provider_factory=server_test_provider)
    if perception_backend is not None:
        supervisor.attach_perception_guard(
            PerceptionGuard(
                perception_backend,
                supervisor.submit_event,
                interval_seconds=manifest.embodiment.poll_interval_seconds,
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
    from harness.runtime.resources import ResourceSampler
    from harness.runtime.daily_report import DailyReportBuilder, DailyReportController, DailyReportSettings, DailyReportStore
    monitor = MonitorRecorder(
        monitor_store,
        journal=supervisor.journal,
        status_provider=supervisor.status,
        trace_dir=Path(os.environ.get("NC_TRACE_DIR", str(ROOT / "traces"))),
        sample_seconds=float(os.environ.get("NC_MONITOR_SAMPLE_SECONDS", "10")),
        resource_sampler=ResourceSampler(state_dir=state_dir),
    )
    report_settings = DailyReportSettings.from_environment()
    supervisor.daily_report = DailyReportController(
        DailyReportBuilder(
            monitor=monitor_store,
            state_dir=state_dir,
            settings=report_settings,
            homeostasis=supervisor.homeostasis,
            diary_store=supervisor.diary.store,
            capabilities=supervisor.capabilities,
            board=supervisor.board,
            goals=getattr(supervisor, "goals", None),
            cognitive=supervisor.cognitive,
            runtime_status=supervisor.status,
        ),
        DailyReportStore(state_dir / "reports", retention_days=report_settings.retention_days),
        report_settings,
    )
    from harness.runtime.self_check import SelfCheckController, SelfCheckSettings
    supervisor.self_check = SelfCheckController(
        supervisor, state_dir, SelfCheckSettings.from_environment(),
    )
    control.event_stream.add_listener(monitor.enqueue)
    profile_store = ProfileRevisionStore(
        profile_dir=Path(os.environ.get("NC_PROFILE_REVISION_DIR", str(ROOT / "data" / "homeostasis" / "profiles"))),
        manifest_dir=Path(os.environ.get("NC_MANIFEST_REVISION_DIR", str(ROOT / "data" / "runtime" / "manifests"))),
        pending_path=Path(os.environ.get("NC_PENDING_START_PATH", str(ROOT / "data" / "runtime" / "pending-start.json"))),
    )
    app = create_control_app(
        control,
        profile_store=profile_store,
        monitor=monitor,
        qq_observation_store=QQObservationStore(state_dir / "qq-observations.sqlite3"),
    )

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
        if getattr(supervisor, "self_check", None) is not None:
            await supervisor.self_check.close()
        await supervisor.stop(reason="runtime service shutdown")
        if supervisor.board is not None:
            await supervisor.board.close()
        if getattr(supervisor, "diary", None) is not None:
            supervisor.diary.close()
        if supervisor.voice is not None:
            await supervisor.voice.close()
        await supervisor.web.close()
        if getattr(supervisor, "server", None) is not None:
            await supervisor.server.close()
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
