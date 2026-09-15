"""FastAPI visualization server: chat REST + stage WebSocket + static UI."""

from __future__ import annotations

import asyncio
import os
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
from gui.observer import QueueObserver
from gui.persist import (
    apply_loaded_tuning,
    default_config_path,
    load_gui_settings,
    persist_gui_settings,
)
from gui.presets import DEFAULT_PRESET_ID, PresetLibrary
from gui.service import HarnessChatService, Mode, new_run_id
from harness.core.exceptions import EvidenceRequiredError, MemoryNotFoundError, PersonaLockedError
from harness.llm.client import list_endpoint_models
from harness.runtime.ownership import ControlMode, SharedDomainLease, require_development

STATIC_DIR = Path(__file__).resolve().parent / "static"
HOST = "127.0.0.1"
PORT = int(os.environ.get("NC_GUI_PORT", "8790"))


class ChatRequest(BaseModel):
    text: str = Field(min_length=1)
    session_id: str | None = None


class ConfigRequest(BaseModel):
    mode: Mode
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    aux_base_url: str | None = None
    aux_model: str | None = None
    aux_api_key: str | None = None
    user_name: str | None = None
    character_name: str | None = None


class SpeakersRequest(BaseModel):
    user_name: str | None = None
    character_name: str | None = None


class PresetIdRequest(BaseModel):
    preset_id: str = Field(min_length=1, max_length=64)


class PresetNewRequest(BaseModel):
    from_id: str | None = Field(default=None, max_length=64)


def _preset_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _resolve_open_presets(library: PresetLibrary, persisted: Any) -> tuple[str, list[str]]:
    requested = list(getattr(persisted, "open_preset_ids", None) or [])
    active = getattr(persisted, "active_preset_id", None) or DEFAULT_PRESET_ID
    if active not in requested:
        requested.insert(0, active)
    valid: list[str] = []
    for preset_id in requested:
        try:
            if library.exists(preset_id) and preset_id not in valid:
                valid.append(preset_id)
        except ValueError:
            continue
    if not valid:
        valid = [DEFAULT_PRESET_ID]
    if active not in valid:
        active = valid[0]
    return active, valid


async def _run_debug(app: FastAPI, apply_fn: Any, *, persist_settings: bool = False) -> dict[str, Any]:
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
        service.flush_active()
        if persist_settings:
            _persist(app, service)
        snapshot = service.snapshot_state()
        if service.observer is not None:
            service.observer.emit_state(snapshot)
        return service.debug_state()


def _persist(app: FastAPI, service: HarnessChatService) -> None:
    persist_gui_settings(
        mode=service.mode,
        settings=service.settings,
        path=app.state.config_path,
        user_name=service.user_name,
        character_name=service.character_name,
        active_preset_id=service.active_preset_id,
        open_preset_ids=service.open_preset_ids,
    )


def create_app(
    *, config_path: Path | None = None, presets_dir: Path | None = None,
    development: bool = False, control_mode: ControlMode | None = None,
    runtime_url: str | None = None, control_token: str | None = None,
) -> FastAPI:
    if not development:
        if config_path is not None or presets_dir is not None:
            raise ValueError("independent_gui_state_requires_development")
        from gui.runtime_server import create_app as create_runtime_app

        return create_runtime_app(runtime_url=runtime_url, control_token=control_token)
    require_development("gui.independent_instance", control_mode=control_mode)
    if config_path is None:
        raise ValueError("independent_gui_requires_explicit_config_path")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        require_development("gui.independent_instance", control_mode=control_mode)
        loop = asyncio.get_running_loop()
        subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        observer = QueueObserver(loop, subscribers)
        resolved = config_path if config_path is not None else default_config_path()
        with SharedDomainLease("cognition", resolved, control_mode=control_mode, isolated=True):
            persisted = load_gui_settings(resolved)
            library = PresetLibrary(working_dir=presets_dir if presets_dir is not None else resolved.parent / "presets")
            active_id, open_ids = _resolve_open_presets(library, persisted)
            service = HarnessChatService(
                observer=observer,
                mode=persisted.mode,
                base_url=persisted.base_url,
                model=persisted.model,
                api_key=persisted.api_key,
                aux_base_url=persisted.aux_base_url,
                aux_model=persisted.aux_model,
                aux_api_key=persisted.aux_api_key,
                library=library,
                active_preset_id=active_id,
                open_preset_ids=open_ids,
            )
            apply_loaded_tuning(service.settings, persisted)
            app.state.service = service
            app.state.config_path = resolved
            app.state.subscribers = subscribers
            app.state.observer = observer
            app.state.run_lock = asyncio.Lock()
            yield

    app = FastAPI(title="NeuralCloud Harness GUI", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/ops")
    async def operations() -> FileResponse:
        return FileResponse(STATIC_DIR / "ops.html")

    @app.get("/api/ops/availability")
    async def operations_availability() -> dict[str, Any]:
        return {
            "available": False,
            "reason": "运维面板需要通过 start.ps1 启动完整 Runtime 四进程拓扑。",
        }

    @app.get("/api/status")
    async def status(session_id: str | None = None) -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        payload = service.status(session_id)
        payload["authority"] = {"instance_mode": "development_isolated", "development": True}
        return payload

    @app.get("/api/presets")
    async def list_presets() -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        return service.presets_payload()

    @app.post("/api/presets/open")
    async def open_preset(body: PresetIdRequest) -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        async with app.state.run_lock:
            try:
                service.open_preset(body.preset_id)
            except (FileNotFoundError, ValueError) as exc:
                raise _preset_error(exc) from exc
            _persist(app, service)
            snapshot = service.status()
            if service.observer is not None:
                service.observer.emit_state(snapshot)
        return snapshot

    @app.post("/api/presets/apply")
    async def apply_preset(body: PresetIdRequest) -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        async with app.state.run_lock:
            try:
                service.apply_preset(body.preset_id)
            except (FileNotFoundError, ValueError) as exc:
                raise _preset_error(exc) from exc
            _persist(app, service)
            snapshot = service.status()
            if service.observer is not None:
                service.observer.emit_state(snapshot)
        return snapshot

    @app.post("/api/presets/new")
    async def new_preset(body: PresetNewRequest) -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        async with app.state.run_lock:
            try:
                service.new_preset(body.from_id)
            except (FileNotFoundError, ValueError) as exc:
                raise _preset_error(exc) from exc
            _persist(app, service)
            snapshot = service.status()
            if service.observer is not None:
                service.observer.emit_state(snapshot)
        return snapshot

    @app.post("/api/presets/restore")
    async def restore_preset() -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        async with app.state.run_lock:
            try:
                service.restore_active()
            except (FileNotFoundError, ValueError) as exc:
                raise _preset_error(exc) from exc
            _persist(app, service)
            snapshot = service.status()
            if service.observer is not None:
                service.observer.emit_state(snapshot)
        return snapshot

    @app.delete("/api/presets/{preset_id}")
    async def delete_preset(preset_id: str) -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        async with app.state.run_lock:
            try:
                service.delete_preset(preset_id)
            except (FileNotFoundError, ValueError) as exc:
                raise _preset_error(exc) from exc
            _persist(app, service)
            snapshot = service.status()
            if service.observer is not None:
                service.observer.emit_state(snapshot)
        return snapshot

    @app.post("/api/chat")
    async def chat(body: ChatRequest) -> dict[str, Any]:
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="empty text")
        service: HarnessChatService = app.state.service
        run_id = new_run_id()
        async with app.state.run_lock:
            try:
                await asyncio.to_thread(service.respond, run_id, body.session_id or "", text)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "run_id": run_id}

    @app.post("/api/config")
    async def config(body: ConfigRequest) -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        async with app.state.run_lock:
            try:
                service.set_speakers(user_name=body.user_name, character_name=body.character_name)
                service.rebuild(
                    mode=body.mode,
                    base_url=body.base_url,
                    model=body.model,
                    api_key=body.api_key,
                    aux_base_url=body.aux_base_url,
                    aux_model=body.aux_model,
                    aux_api_key=body.aux_api_key,
                )
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            _persist(app, service)
        return service.status()

    @app.get("/api/models")
    async def models(base_url: str | None = None) -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        url = (base_url or "").strip() or service.settings.llm_base_url
        key = service.settings.llm_api_key or "not-needed"
        names = await asyncio.to_thread(list_endpoint_models, url, key)
        return {"base_url": url, "models": names}

    @app.post("/api/reset")
    async def reset() -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        async with app.state.run_lock:
            try:
                service.restore_active()
            except (FileNotFoundError, ValueError) as exc:
                raise _preset_error(exc) from exc
            _persist(app, service)
        return service.status()

    @app.post("/api/speakers")
    async def speakers(body: SpeakersRequest) -> dict[str, Any]:
        service: HarnessChatService = app.state.service
        async with app.state.run_lock:
            service.set_speakers(user_name=body.user_name, character_name=body.character_name)
            service.flush_active()
            _persist(app, service)
            snapshot = service.snapshot_state()
            if service.observer is not None:
                service.observer.emit_state(snapshot)
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
        def apply(service: HarnessChatService) -> None:
            apply_persona(service.memory, body)
            if body.name is not None:
                service.set_speakers(character_name=body.name)

        return await _run_debug(app, apply, persist_settings=body.name is not None)

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
        def apply(service: HarnessChatService) -> None:
            apply_tuning(service.settings, body, memory=service.memory)

        return await _run_debug(app, apply, persist_settings=True)

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
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Shared Runtime GUI; independent cognition requires development authority.")
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--config-path", type=Path)
    args = parser.parse_args()
    selected = create_app(development=args.development, config_path=args.config_path)
    uvicorn.run(selected, host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
