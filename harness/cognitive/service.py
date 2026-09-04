"""Private Cognitive service and diagnostic event stream."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, cast

from fastapi import Header, HTTPException
from fastapi.responses import StreamingResponse
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
from gui.persist import Mode, apply_loaded_tuning, default_config_path, load_gui_settings, persist_gui_settings
from gui.presets import DEFAULT_PRESET_ID, PresetLibrary
from gui.service import HarnessChatService
from harness.llm.client import list_endpoint_models
from harness.runtime.adapters import PipelineCognitiveAdapter
from harness.runtime.rpc import create_domain_service_app
from infra.trace.serialize import serialize_stage

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("NC_COGNITIVE_HOST", "127.0.0.1")
PORT = int(os.environ.get("NC_COGNITIVE_PORT", "8771"))


class CognitiveEventObserver:
    def __init__(self) -> None:
        self.run_id: str | None = None
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = set()

    def bind(self, run_id: str) -> None:
        self.run_id = run_id

    def on_stage(self, kind: str, result: Any, payload: dict[str, Any]) -> None:
        self._broadcast(
            {
                "type": "final" if kind == "final" else "stage",
                "run_id": self.run_id,
                "stage": kind,
                "latency_ms": payload.get("latency_ms", 0.0),
                "data": serialize_stage(kind, result),
                "meta": {key: value for key, value in payload.items() if key != "latency_ms"},
            }
        )

    def emit_state(self, snapshot: dict[str, Any], run_id: str | None = None) -> None:
        self._broadcast({"type": "state", "run_id": run_id or self.run_id, "data": snapshot})

    def emit_error(self, message: str, run_id: str | None = None) -> None:
        self._broadcast({"type": "error", "run_id": run_id or self.run_id, "data": {"message": message}})

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]:
        item: tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]] = (
            asyncio.get_running_loop(),
            asyncio.Queue(maxsize=256),
        )
        self._subscribers.add(item)
        return item

    def unsubscribe(self, item: tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]) -> None:
        self._subscribers.discard(item)

    def _broadcast(self, event: dict[str, Any]) -> None:
        for loop, queue in list(self._subscribers):
            def put(target: asyncio.Queue[dict[str, Any]] = queue) -> None:
                if target.full():
                    try:
                        target.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                target.put_nowait(event)

            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:
                self._subscribers.discard((loop, queue))


class CognitiveServiceHost:
    def __init__(self, *, config_path: Path, presets_dir: Path, state_path: Path) -> None:
        self.config_path = config_path
        self.observer = CognitiveEventObserver()
        persisted = load_gui_settings(config_path)
        library = PresetLibrary(working_dir=presets_dir)
        open_ids = [item for item in persisted.open_preset_ids if library.exists(item)]
        active = persisted.active_preset_id if library.exists(persisted.active_preset_id) else DEFAULT_PRESET_ID
        if active not in open_ids:
            open_ids.insert(0, active)
        configured_mode = os.environ.get("NC_MODE", persisted.mode)
        if configured_mode not in {"demo", "live", "ollama"}:
            raise ValueError(f"unsupported NC_MODE: {configured_mode}")
        self.chat = HarnessChatService(
            observer=self.observer,
            mode=cast(Mode, configured_mode),
            base_url=persisted.base_url,
            model=persisted.model,
            api_key=persisted.api_key,
            aux_base_url=persisted.aux_base_url,
            aux_model=persisted.aux_model,
            aux_api_key=persisted.aux_api_key,
            user_name=persisted.user_name,
            character_name=persisted.character_name,
            library=library,
            active_preset_id=active,
            open_preset_ids=open_ids or [active],
        )
        apply_loaded_tuning(self.chat.settings, persisted)
        self.adapter = PipelineCognitiveAdapter(
            self.chat.pipeline,
            state_path=state_path,
            autonomous_log_dir=Path(
                os.environ.get(
                    "NC_COGNITIVE_AUTONOMOUS_LOG_DIR",
                    str(state_path.parent / "autonomous"),
                )
            ),
            default_session_id=self.chat.active_session_id,
        )
        self._control_lock = asyncio.Lock()

    async def health(self):
        return await self.adapter.health()

    async def get_snapshot(self):
        return await self.adapter.get_snapshot()

    async def prepare_response(self, command):
        return await self.adapter.prepare_response(command)

    async def assess_autonomous(self, command):
        result = await self.adapter.assess_autonomous(command)
        if result.result is not None and result.result.internal_memory_ref is not None:
            self.chat.flush_active()
        return result

    async def commit_interaction(self, command):
        result = await self.adapter.commit_interaction(command)
        self._sync_transcript()
        self.chat.flush_active()
        return result

    async def pause_maintenance(self, *, timeout: float) -> bool:
        return await self.adapter.pause_maintenance(timeout=timeout)

    async def resume_maintenance(self) -> None:
        await self.adapter.resume_maintenance()

    async def close(self) -> None:
        self._sync_transcript()
        self.chat.flush_active()
        await self.adapter.close()

    async def ui_snapshot(self) -> dict[str, Any]:
        self._sync_transcript()
        return self.chat.status()

    async def control(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = payload or {}
        async with self._control_lock:
            if operation == "snapshot":
                return await self.ui_snapshot()
            if operation == "presets.list":
                return self.chat.presets_payload()
            if operation == "debug.state":
                return self.chat.debug_state()
            if operation == "models":
                url = str(body.get("base_url") or self.chat.settings.llm_base_url)
                names = await asyncio.to_thread(
                    list_endpoint_models, url, self.chat.settings.llm_api_key or "not-needed"
                )
                return {"base_url": url, "models": names}
            self._apply_mutation(operation, body)
            self.adapter.pipeline = self.chat.pipeline
            self.adapter.default_session_id = self.chat.active_session_id
            self.chat.flush_active()
            self._persist()
            snapshot = await self.ui_snapshot()
            self.observer.emit_state(snapshot)
            return snapshot if not operation.startswith("debug.") else self.chat.debug_state()

    def _apply_mutation(self, operation: str, body: dict[str, Any]) -> None:
        if operation == "presets.open":
            self.chat.open_preset(str(body["preset_id"]))
        elif operation == "presets.apply":
            self.chat.apply_preset(str(body["preset_id"]))
        elif operation == "presets.new":
            self.chat.new_preset(body.get("from_id"))
        elif operation == "presets.restore" or operation == "reset":
            self.chat.restore_active()
        elif operation == "presets.delete":
            self.chat.delete_preset(str(body["preset_id"]))
        elif operation == "speakers":
            self.chat.set_speakers(user_name=body.get("user_name"), character_name=body.get("character_name"))
        elif operation == "config":
            self.chat.set_speakers(user_name=body.get("user_name"), character_name=body.get("character_name"))
            self.chat.rebuild(
                mode=body.get("mode"),
                base_url=body.get("base_url"),
                model=body.get("model"),
                api_key=body.get("api_key"),
                aux_base_url=body.get("aux_base_url"),
                aux_model=body.get("aux_model"),
                aux_api_key=body.get("aux_api_key"),
            )
            self.adapter.pipeline = self.chat.pipeline
        elif operation == "debug.relationship":
            apply_relationship(self.chat.memory, RelationshipPatch.model_validate(body))
        elif operation == "debug.psych":
            apply_psych(self.chat.memory, PsychPatch.model_validate(body))
        elif operation == "debug.persona":
            patch = PersonaPatch.model_validate(body)
            apply_persona(self.chat.memory, patch)
            if patch.name is not None:
                self.chat.set_speakers(character_name=patch.name)
        elif operation == "debug.episodic":
            apply_episodic(self.chat.memory, EpisodicPatch.model_validate(body))
        elif operation == "debug.semantic":
            apply_semantic(self.chat.memory, SemanticPatch.model_validate(body))
        elif operation == "debug.candidate":
            apply_candidate(self.chat.memory, CandidatePatch.model_validate(body))
        elif operation == "debug.tuning":
            apply_tuning(self.chat.settings, TuningPatch.model_validate(body), memory=self.chat.memory)
        else:
            raise ValueError(f"unsupported cognitive control operation: {operation}")

    def _sync_transcript(self) -> None:
        self.chat.transcript = self.chat.pipeline.session_store.recent(self.chat.active_session_id)

    def _persist(self) -> None:
        persist_gui_settings(
            mode=self.chat.mode,
            settings=self.chat.settings,
            path=self.config_path,
            user_name=self.chat.user_name,
            character_name=self.chat.character_name,
            active_preset_id=self.chat.active_preset_id,
            open_preset_ids=self.chat.open_preset_ids,
        )


def create_app():
    token = os.environ.get("NC_SERVICE_TOKEN", "development-service-token")
    config_path = default_config_path()
    presets_dir = Path(os.environ.get("NC_GUI_PRESETS", str(ROOT / "data" / "presets")))
    state_path = Path(os.environ.get("NC_COGNITIVE_STATE_PATH", str(ROOT / "data" / "cognitive" / "state.json")))
    host = CognitiveServiceHost(config_path=config_path, presets_dir=presets_dir, state_path=state_path)
    app = create_domain_service_app(host, service_token=token, service_name="cognitive")

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="invalid service token")

    @app.get("/v1/ui/snapshot")
    async def ui_snapshot(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        await authenticate(authorization)
        return await host.ui_snapshot()

    @app.post("/v1/ui/control")
    async def ui_control(payload: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        await authenticate(authorization)
        try:
            return await host.control(str(payload.get("operation") or ""), dict(payload.get("payload") or {}))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/events/stream")
    async def events(authorization: str | None = Header(default=None)) -> StreamingResponse:
        await authenticate(authorization)
        subscription = host.observer.subscribe()

        async def generate():
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(subscription[1].get(), timeout=15.0)
                        yield f"event: cognitive.ui\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                host.observer.unsubscribe(subscription)

        return StreamingResponse(generate(), media_type="text/event-stream")

    app.state.host = host
    app.router.add_event_handler("shutdown", host.close)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
