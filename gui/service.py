"""Harness-backed chat service for the visualization GUI."""

from __future__ import annotations

import secrets
from typing import Any, Literal, Protocol

import httpx

from gui.demo_seed import DEFAULT_SESSION_ID, bootstrap_demo
from gui.debug import collect_debug_state
from harness.config import Settings
from harness.llm.client import LLMClient, MockLLM, OpenAICompatClient
from harness.memory.service import MemoryService, build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline

Mode = Literal["demo", "live"]


class BroadcastObserver(Protocol):
    def bind(self, run_id: str) -> None: ...

    def on_stage(self, kind: str, result: Any, payload: dict[str, Any]) -> None: ...

    def emit_state(self, snapshot: dict[str, Any], run_id: str | None = None) -> None: ...

    def emit_error(self, message: str, run_id: str | None = None) -> None: ...


def new_run_id() -> str:
    return f"run_{secrets.token_hex(4)}"


def probe_llm(settings: Settings, *, timeout: float = 0.8) -> bool:
    url = f"{settings.llm_base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers)
            return 200 <= response.status_code < 400
    except httpx.HTTPError:
        return False


def _api_key_configured(settings: Settings) -> bool:
    key = (settings.llm_api_key or "").strip()
    return bool(key) and key != "not-needed"


def _store_count(store: Any) -> int:
    if hasattr(store, "count") and callable(store.count):
        return int(store.count())
    if hasattr(store, "list_all") and callable(store.list_all):
        return len(store.list_all())
    rows = getattr(store, "_rows", None)
    if rows is None:
        return 0
    return len(rows)


class HarnessChatService:
    def __init__(self, observer: BroadcastObserver | None = None, *, mode: Mode = "demo") -> None:
        self.observer = observer
        self.mode: Mode = mode
        self.llm_reachable = False
        self.settings = Settings()
        self.memory: MemoryService
        self.pipeline: InferencePipeline
        self.llm: LLMClient
        self.rebuild(mode=mode)

    def attach_observer(self, observer: BroadcastObserver) -> None:
        self.observer = observer
        self.pipeline.observer = observer

    def rebuild(
        self,
        *,
        mode: Mode | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.mode = mode or self.mode
        updates: dict[str, Any] = {
            "complexity_threshold": 0.5,
            "validator_sample_rate": 1.0,
            "max_revisions": 2,
            "record_interaction": True,
            "session_window": 10,
            "continuation_enabled": True,
            "max_reply_segments": 3,
            "model_version": "mock" if self.mode == "demo" else "live",
        }
        if base_url:
            updates["llm_base_url"] = base_url
        if model:
            updates["llm_model"] = model
        if api_key:
            updates["llm_api_key"] = api_key
        self.settings = self.settings.model_copy(update=updates)

        if self.mode == "demo":
            self.llm = MockLLM()
        else:
            self.llm = OpenAICompatClient(self.settings)
        self.llm_reachable = probe_llm(self.settings)

        self.memory = build_inmemory_service(settings=self.settings, llm=self.llm)
        bootstrap_demo(self.memory)
        self.pipeline = InferencePipeline(
            self.memory,
            settings=self.settings,
            llm=self.llm,
            observer=self.observer,
        )

    def snapshot_state(self, session_id: str | None = None) -> dict[str, Any]:
        snap = self.memory.get_state()
        stores = self.memory.stores
        persona = snap.persona
        relationship = snap.relationship
        psych = snap.psych
        world = snap.world
        turns = self.pipeline.session_store.recent(session_id) if session_id else []
        return {
            "mode": self.mode,
            "llm": {
                "mode": self.mode,
                "base_url": self.settings.llm_base_url,
                "model": self.settings.llm_model,
                "reachable": self.llm_reachable,
                "api_key_configured": _api_key_configured(self.settings),
            },
            "persona": persona.model_dump(mode="json") if persona else None,
            "relationship": relationship.model_dump(mode="json") if relationship else None,
            "psych": psych.model_dump(mode="json") if psych else None,
            "world": world.model_dump(mode="json") if world else None,
            "counts": {
                "persona": 1 if persona else 0,
                "episodic": _store_count(stores.episodic),
                "semantic": _store_count(stores.semantic),
                "relationship": _store_count(stores.relationship),
                "candidates": _store_count(stores.candidates),
                "audit": _store_count(stores.audit),
            },
            "session": {
                "session_id": session_id,
                "turns": [turn.model_dump(mode="json") for turn in turns],
            },
        }

    def status(self, session_id: str | None = None) -> dict[str, Any]:
        payload = self.snapshot_state(session_id or DEFAULT_SESSION_ID)
        payload["default_session_id"] = DEFAULT_SESSION_ID
        return payload

    def debug_state(self) -> dict[str, Any]:
        return collect_debug_state(self.memory, self.settings)

    def respond(self, run_id: str, session_id: str, text: str) -> None:
        if self.observer is not None:
            self.observer.bind(run_id)
        try:
            self.pipeline.respond(text, session_id=session_id)
            if self.observer is not None:
                self.observer.emit_state(self.snapshot_state(session_id), run_id=run_id)
        except Exception as exc:
            if self.observer is not None:
                self.observer.emit_error(str(exc), run_id=run_id)
            raise
