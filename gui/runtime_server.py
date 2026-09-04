"""Thin GUI facade for the authenticated Runtime control plane."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gui.presets import DEFAULT_SESSION_ID

STATIC_DIR = Path(__file__).resolve().parent / "static"
HOST = os.environ.get("NC_GUI_HOST", "127.0.0.1")
PORT = int(os.environ.get("NC_GUI_PORT", "8766"))


class RuntimeGateway:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=65.0)
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.ops_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.stream_task: asyncio.Task[None] | None = None
        self.monitor_stream_task: asyncio.Task[None] | None = None
        self.monitor_cursor = 0

    async def start(self) -> None:
        self.stream_task = asyncio.create_task(self._stream(), name="gui-runtime-events")
        self.monitor_stream_task = asyncio.create_task(self._monitor_stream(), name="gui-monitor-events")

    async def close(self) -> None:
        if self.stream_task is not None:
            self.stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.stream_task
        if self.monitor_stream_task is not None:
            self.monitor_stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.monitor_stream_task
        await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.client.request(method, path, **kwargs)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=f"Runtime unavailable: {exc}") from exc
        if response.is_error:
            detail = payload.get("detail") if isinstance(payload, dict) else str(payload)
            raise HTTPException(status_code=response.status_code, detail=detail)
        return payload

    async def status(self) -> dict[str, Any]:
        composite = await self.request("GET", "/v1/snapshot")
        ui = dict(composite.get("cognitive_ui") or {})
        ui["runtime"] = composite.get("runtime")
        ui["homeostasis"] = (composite.get("homeostasis") or {}).get("payload")
        ui["manifest"] = composite.get("manifest")
        ui["pending_start"] = composite.get("pending_start")
        ui["services"] = {
            "gui": {"healthy": True},
            "runtime": (composite.get("runtime") or {}).get("health") or {},
            "cognitive": composite.get("cognitive") or {},
            "homeostasis": composite.get("homeostasis") or {},
        }
        return ui

    async def cognitive(self, operation: str, payload: dict[str, Any] | None = None) -> Any:
        return await self.request(
            "POST",
            "/v1/cognitive/control",
            json={"operation": operation, "payload": payload or {}},
        )

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(queue)

    def subscribe_ops(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        self.ops_subscribers.add(queue)
        return queue

    def unsubscribe_ops(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.ops_subscribers.discard(queue)

    async def broadcast(self, event: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def broadcast_ops(self, event: dict[str, Any]) -> None:
        for queue in list(self.ops_subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def _stream(self) -> None:
        while True:
            try:
                async with httpx.AsyncClient(headers=self.headers, timeout=None) as client:
                    async with client.stream("GET", f"{self.base_url}/v1/events/stream") as response:
                        response.raise_for_status()
                        topic = "runtime"
                        async for line in response.aiter_lines():
                            if line.startswith("event:"):
                                topic = line[6:].strip()
                            elif line.startswith("data:"):
                                payload = json.loads(line[5:].strip())
                                if topic == "cognitive.ui" and isinstance(payload, dict):
                                    await self.broadcast(payload)
                                elif topic in {"event.completed", "lifecycle", "watchdog"}:
                                    try:
                                        await self.broadcast({"type": "state", "data": await self.status()})
                                    except HTTPException:
                                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.broadcast({"type": "error", "data": {"message": f"Runtime event stream: {exc}"}})
                await asyncio.sleep(1.0)

    async def _monitor_stream(self) -> None:
        while True:
            try:
                async with httpx.AsyncClient(headers=self.headers, timeout=None) as client:
                    params = {"after_seq": self.monitor_cursor}
                    async with client.stream("GET", f"{self.base_url}/v1/monitor/stream", params=params) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            payload = json.loads(line[5:].strip())
                            if not isinstance(payload, dict):
                                continue
                            self.monitor_cursor = max(self.monitor_cursor, int(payload.get("seq") or 0))
                            await self.broadcast_ops({"type": "ops.event", "data": payload})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.broadcast_ops({"type": "ops.error", "data": {"message": str(exc)}})
                await asyncio.sleep(1.0)


def create_app(*, runtime_url: str | None = None, control_token: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        gateway = RuntimeGateway(
            runtime_url or os.environ.get("NC_RUNTIME_URL", "http://127.0.0.1:8770"),
            control_token or os.environ.get("NC_CONTROL_TOKEN", "development-control-token"),
        )
        app.state.gateway = gateway
        await gateway.start()
        try:
            yield
        finally:
            await gateway.close()

    app = FastAPI(title="NeuralCloud Runtime GUI", lifespan=lifespan)

    def gateway(request: Request) -> RuntimeGateway:
        return request.app.state.gateway

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/ops")
    async def operations() -> FileResponse:
        return FileResponse(STATIC_DIR / "ops.html")

    @app.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        return await gateway(request).status()

    @app.get("/api/ops/availability")
    async def ops_availability(request: Request) -> dict[str, Any]:
        status_payload = await gateway(request).request("GET", "/v1/status")
        return {"available": True, "runtime": status_payload}

    @app.get("/api/ops/overview")
    async def ops_overview(request: Request) -> Any:
        bridge = gateway(request)
        monitor_payload, composite = await asyncio.gather(
            bridge.request("GET", "/v1/monitor/overview", params=dict(request.query_params)),
            bridge.status(),
        )
        monitor_payload["services"] = composite.get("services") or {}
        monitor_payload["homeostasis"] = composite.get("homeostasis") or {}
        return monitor_payload

    @app.get("/api/ops/events")
    async def ops_events(request: Request) -> Any:
        return await gateway(request).request(
            "GET", "/v1/monitor/events", params=dict(request.query_params)
        )

    @app.get("/api/ops/journal")
    async def ops_journal(request: Request) -> Any:
        return await gateway(request).request(
            "GET", "/v1/monitor/journal", params=dict(request.query_params)
        )

    @app.get("/api/ops/runs")
    async def ops_runs(request: Request) -> Any:
        return await gateway(request).request(
            "GET", "/v1/monitor/runs", params=dict(request.query_params)
        )

    @app.get("/api/ops/runs/{run_id}")
    async def ops_run(run_id: str, request: Request) -> Any:
        return await gateway(request).request("GET", f"/v1/monitor/runs/{run_id}")

    @app.get("/api/ops/actions")
    async def ops_actions(request: Request) -> Any:
        return await gateway(request).request(
            "GET", "/v1/monitor/actions", params=dict(request.query_params)
        )

    @app.get("/api/ops/export/{correlation_id}")
    async def ops_export(correlation_id: str, request: Request) -> Any:
        return await gateway(request).request("GET", f"/v1/monitor/export/{correlation_id}")

    @app.post("/api/ops/runtime/{operation}")
    async def ops_runtime_control(operation: str, request: Request) -> Any:
        mapping = {
            "pause": "/v1/pause",
            "resume": "/v1/resume",
            "recover": "/v1/recover",
            "emergency-stop": "/v1/emergency-stop",
            "stop": "/v1/stop",
            "checkpoint": "/v1/checkpoint",
        }
        path = mapping.get(operation)
        if path is None:
            raise HTTPException(status_code=404, detail="unknown runtime operation")
        return await gateway(request).request("POST", path, json=await _json_body(request))

    @app.post("/api/ops/events")
    async def ops_inject_event(request: Request) -> Any:
        return await gateway(request).request(
            "POST", "/v1/debug/events", json=await _json_body(request)
        )

    @app.post("/api/ops/actions/{action_id}/{operation}")
    async def ops_action(action_id: str, operation: str, request: Request) -> Any:
        if operation not in {"approve", "reject", "resolve"}:
            raise HTTPException(status_code=404, detail="unknown action operation")
        return await gateway(request).request(
            "POST",
            f"/v1/actions/{action_id}/{operation}",
            json=await _json_body(request),
        )

    @app.post("/api/ops/deliveries/{prepare_id}/resolve")
    async def ops_delivery(prepare_id: str, request: Request) -> Any:
        return await gateway(request).request(
            "POST",
            f"/v1/deliveries/{prepare_id}/resolve",
            json=await _json_body(request),
        )

    @app.post("/api/chat")
    async def chat(request: Request) -> dict[str, Any]:
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="empty text")
        run_id = f"run_{secrets.token_hex(4)}"
        event = await gateway(request).request(
            "POST",
            "/v1/messages",
            json={
                "text": text,
                "session_id": body.get("session_id") or DEFAULT_SESSION_ID,
                "correlation_id": run_id,
            },
        )
        if event.get("accepted") is False:
            raise HTTPException(status_code=409, detail="Runtime did not accept the message")
        return {"ok": True, "run_id": run_id, "event_id": event.get("event_id")}

    @app.get("/api/presets")
    async def presets(request: Request) -> Any:
        return await gateway(request).cognitive("presets.list")

    @app.post("/api/presets/{operation}")
    async def preset_operation(operation: str, request: Request) -> Any:
        if operation not in {"open", "apply", "new", "restore"}:
            raise HTTPException(status_code=404, detail="unknown preset operation")
        body = await _json_body(request)
        return await gateway(request).cognitive(f"presets.{operation}", body)

    @app.delete("/api/presets/{preset_id}")
    async def delete_preset(preset_id: str, request: Request) -> Any:
        return await gateway(request).cognitive("presets.delete", {"preset_id": preset_id})

    @app.post("/api/config")
    async def config(request: Request) -> Any:
        return await gateway(request).cognitive("config", await _json_body(request))

    @app.get("/api/models")
    async def models(request: Request, base_url: str | None = None) -> Any:
        return await gateway(request).cognitive("models", {"base_url": base_url})

    @app.post("/api/reset")
    async def reset(request: Request) -> Any:
        return await gateway(request).cognitive("reset")

    @app.post("/api/speakers")
    async def speakers(request: Request) -> Any:
        return await gateway(request).cognitive("speakers", await _json_body(request))

    @app.get("/api/debug/state")
    async def debug_state(request: Request) -> Any:
        return await gateway(request).cognitive("debug.state")

    @app.post("/api/debug/{operation}")
    async def debug_operation(operation: str, request: Request) -> Any:
        if operation not in {"relationship", "psych", "persona", "episodic", "semantic", "candidate", "tuning"}:
            raise HTTPException(status_code=404, detail="unknown debug operation")
        return await gateway(request).cognitive(f"debug.{operation}", await _json_body(request))

    @app.get("/api/homeostasis/profile")
    async def homeostasis_profile(request: Request) -> Any:
        return await gateway(request).request("GET", "/v1/homeostasis/profile")

    @app.post("/api/homeostasis/profile/validate")
    async def validate_profile(request: Request) -> Any:
        return await gateway(request).request(
            "POST", "/v1/homeostasis/profile/validate", json=await _json_body(request)
        )

    @app.post("/api/homeostasis/profile/revisions")
    async def revise_profile(request: Request) -> Any:
        return await gateway(request).request(
            "POST", "/v1/homeostasis/profile/revisions", json=await _json_body(request)
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        bridge: RuntimeGateway = websocket.app.state.gateway
        queue = bridge.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                    await websocket.send_json(event)
                except TimeoutError:
                    await websocket.send_json({"type": "ping"})
        except WebSocketDisconnect:
            pass
        finally:
            bridge.unsubscribe(queue)

    @app.websocket("/ops/ws")
    async def operations_websocket(websocket: WebSocket) -> None:
        await websocket.accept()
        bridge: RuntimeGateway = websocket.app.state.gateway
        queue = bridge.subscribe_ops()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                    await websocket.send_json(event)
                except TimeoutError:
                    await websocket.send_json({"type": "ping"})
        except WebSocketDisconnect:
            pass
        finally:
            bridge.unsubscribe_ops(queue)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception:
        return {}
    return dict(value) if isinstance(value, dict) else {}


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
