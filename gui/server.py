"""FastAPI visualization server: chat REST + stage WebSocket + static UI."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from gui.debug import (
    CandidatePatch,
    EpisodicPatch,
    PersonaPatch,
    PsychPatch,
    RelationshipPatch,
    SemanticPatch,
    TuningPatch,
    apply_candidate,
    apply_episodic,
    apply_persona,
    apply_psych,
    apply_relationship,
    apply_semantic,
    apply_tuning,
)
from gui.demo_seed import DEFAULT_SESSION_ID
from gui.observer import QueueObserver
from gui.service import HarnessChatService, Mode, new_run_id
from harness.core.exceptions import EvidenceRequiredError, MemoryNotFoundError, PersonaLockedError

STATIC_DIR = Path(__file__).resolve().parent / "static"
HOST = "127.0.0.1"
PORT = 8765


class ChatRequest(BaseModel):
    text: str = Field(min_length=1)
    session_id: str = DEFAULT_SESSION_ID


class ConfigRequest(BaseModel):
    mode: Mode
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


async def _run_debug(app: FastAPI, apply_fn: Any) -> dict[str, Any]:
    service: HarnessChatService = app.state.service
    async with app.state.run_lock:
        try:
            apply_fn(service)
        except MemoryNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (EvidenceRequiredError, ValueError, NotImplementedError, PersonaLockedError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snapshot = service.snapshot_state()
        if service.observer is not None:
            service.observer.emit_state(snapshot)
        return service.debug_state()


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        observer = QueueObserver(loop, subscribers)
        service = HarnessChatService(observer=observer, mode="demo")
        app.state.service = service
        app.state.subscribers = subscribers
        app.state.observer = observer
        app.state.run_lock = asyncio.Lock()
        yield

    app = FastAPI(title="NeuralCloud Harness GUI", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    async def status(session_id: str | None = None) -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        return service.status(session_id)

    @app.post("/api/chat")
    async def chat(body: ChatRequest) -> dict[str, Any]:
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="empty text")
        service: HarnessChatService = app.state.service
        run_id = new_run_id()
        async with app.state.run_lock:
            try:
                await asyncio.to_thread(service.respond, run_id, body.session_id, text)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "run_id": run_id}

    @app.post("/api/config")
    async def config(body: ConfigRequest) -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        async with app.state.run_lock:
            try:
                service.rebuild(
                    mode=body.mode,
                    base_url=body.base_url,
                    model=body.model,
                    api_key=body.api_key,
                )
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return service.status()

    @app.post("/api/reset")
    async def reset() -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        async with app.state.run_lock:
            service.rebuild(mode=service.mode)
        return service.status()

    @app.get("/api/debug/state")
    async def debug_state() -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        async with app.state.run_lock:
            return service.debug_state()

    @app.post("/api/debug/relationship")
    async def debug_relationship(body: RelationshipPatch) -> dict[str, Any]:
        return await _run_debug(app, lambda service: apply_relationship(service.memory, body))

    @app.post("/api/debug/psych")
    async def debug_psych(body: PsychPatch) -> dict[str, Any]:
        return await _run_debug(app, lambda service: apply_psych(service.memory, body))

    @app.post("/api/debug/persona")
    async def debug_persona(body: PersonaPatch) -> dict[str, Any]:
        return await _run_debug(app, lambda service: apply_persona(service.memory, body))

    @app.post("/api/debug/episodic")
    async def debug_episodic(body: EpisodicPatch) -> dict[str, Any]:
        return await _run_debug(app, lambda service: apply_episodic(service.memory, body))

    @app.post("/api/debug/semantic")
    async def debug_semantic(body: SemanticPatch) -> dict[str, Any]:
        return await _run_debug(app, lambda service: apply_semantic(service.memory, body))

    @app.post("/api/debug/candidate")
    async def debug_candidate(body: CandidatePatch) -> dict[str, Any]:
        return await _run_debug(app, lambda service: apply_candidate(service.memory, body))

    @app.post("/api/debug/tuning")
    async def debug_tuning(body: TuningPatch) -> dict[str, Any]:
        return await _run_debug(
            app,
            lambda service: apply_tuning(service.settings, body, memory=service.memory),
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        subscribers: set[asyncio.Queue[dict[str, Any]]] = app.state.subscribers
        subscribers.add(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                except TimeoutError:
                    await websocket.send_json({"type": "ping"})
                    continue
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            subscribers.discard(queue)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("gui.server:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
