"""Executable Runtime Supervisor control-plane service."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
import httpx

from harness.homeostasis.profiles import ProfileRevisionStore
from harness.research.manifest import load_experiment_manifest
from harness.runtime.control import ControlPlane, create_control_app
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
    supervisor = RuntimeSupervisor(
        manifest=manifest,
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=state_dir,
    )
    control = ControlPlane(supervisor, token=control_token)
    profile_store = ProfileRevisionStore(
        profile_dir=ROOT / "data" / "homeostasis" / "profiles",
        manifest_dir=ROOT / "data" / "runtime" / "manifests",
        pending_path=ROOT / "data" / "runtime" / "pending-start.json",
    )
    app = create_control_app(control, profile_store=profile_store)

    async def startup() -> None:
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
        task = getattr(app.state, "runtime_task", None)
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task

    app.state.supervisor = supervisor
    app.state.control = control
    app.router.add_event_handler("startup", startup)
    app.router.add_event_handler("shutdown", shutdown)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
