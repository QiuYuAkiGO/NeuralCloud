"""Harness-backed chat service for the visualization GUI."""

from __future__ import annotations

import logging
import secrets
import threading
from datetime import UTC, datetime
from typing import Any, Protocol

from gui.debug import PersonaPatch, apply_persona, collect_debug_state
from gui.persist import (
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_USER_NAME,
    Mode,
    looks_like_ollama_url,
    normalize_speaker_name,
)
from gui.presets import (
    DEFAULT_PRESET_ID,
    DEFAULT_SESSION_ID,
    CharacterPreset,
    PresetLibrary,
    hydrate_memory,
    snapshot_memory,
)
from harness.config import Settings
from harness.llm.fallback import fallback_status
from harness.llm.structured import structured_status
from harness.core.error_notice import ERROR_FALLBACK_REPLY
from harness.core.inference import SessionTurn
from harness.llm.client import (
    LLMClient,
    MockLLM,
    aux_endpoint_distinct,
    aux_explicitly_configured,
    aux_model_distinct,
    build_llm_client,
    preload_aux_model,
    probe_endpoint,
    resolve_aux_endpoint,
    resolve_qq_memory_endpoint,
)
from harness.llm.embeddings import build_embedding_client, describe_embedding
from harness.memory.service import MemoryService, build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline

logger = logging.getLogger(__name__)


class BroadcastObserver(Protocol):
    def bind(self, run_id: str) -> None: ...

    def on_stage(self, kind: str, result: Any, payload: dict[str, Any]) -> None: ...

    def emit_state(self, snapshot: dict[str, Any], run_id: str | None = None) -> None: ...

    def emit_error(self, message: str, run_id: str | None = None) -> None: ...


def new_run_id() -> str:
    return f"run_{secrets.token_hex(4)}"


def probe_llm(settings: Settings, *, timeout: float = 0.8) -> bool:
    return probe_endpoint(settings.llm_base_url, settings.llm_api_key, timeout=timeout)


def probe_aux_llm(settings: Settings, *, timeout: float = 0.8) -> bool:
    base_url, _, api_key = resolve_aux_endpoint(settings)
    return probe_endpoint(base_url, api_key, timeout=timeout)


def _api_key_configured(settings: Settings) -> bool:
    key = (settings.llm_api_key or "").strip()
    return bool(key) and key != "not-needed"


def _close_llm(llm: Any) -> None:
    closer = getattr(llm, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        logger.debug("llm client close failed", exc_info=True)


def _close_memory(memory: Any) -> None:
    closer = getattr(memory, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        logger.debug("memory service close failed", exc_info=True)


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
    def __init__(
        self,
        observer: BroadcastObserver | None = None,
        *,
        mode: Mode = "demo",
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        aux_base_url: str | None = None,
        aux_model: str | None = None,
        aux_api_key: str | None = None,
        validator_enabled: bool | None = None,
        user_name: str | None = None,
        character_name: str | None = None,
        library: PresetLibrary | None = None,
        active_preset_id: str | None = None,
        open_preset_ids: list[str] | None = None,
    ) -> None:
        self.observer = observer
        self.mode: Mode = mode
        self.llm_reachable = False
        self.aux_reachable = False
        self.aux_preloaded = False
        self.aux_active = False
        self._preload_generation = 0
        self.library = library or PresetLibrary()
        self.active_preset_id = active_preset_id or DEFAULT_PRESET_ID
        self.open_preset_ids = list(open_preset_ids or [self.active_preset_id])
        if self.active_preset_id not in self.open_preset_ids:
            self.open_preset_ids.insert(0, self.active_preset_id)
        self.user_name = normalize_speaker_name(user_name, default=DEFAULT_USER_NAME)
        self.character_name = normalize_speaker_name(character_name)
        self._session_id = DEFAULT_SESSION_ID
        self._active_builtin_id: str | None = DEFAULT_PRESET_ID
        self._active_preset_name = ""
        self.transcript: list[SessionTurn] = []
        self.settings = Settings()
        self.memory: MemoryService
        self.pipeline: InferencePipeline
        self.llm: LLMClient
        self.embedding_info: dict[str, Any] = {
            "backend": "mock",
            "reachable": False,
            "base_url": None,
            "model": None,
        }
        self.rebuild(
            mode=mode,
            base_url=base_url,
            model=model,
            api_key=api_key,
            aux_base_url=aux_base_url,
            aux_model=aux_model,
            aux_api_key=aux_api_key,
            validator_enabled=validator_enabled,
        )

    @property
    def active_session_id(self) -> str:
        return self._session_id

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
        aux_base_url: str | None = None,
        aux_model: str | None = None,
        aux_api_key: str | None = None,
        validator_enabled: bool | None = None,
        qq_latency=None,
    ) -> None:
        if validator_enabled is not None and type(validator_enabled) is not bool:
            raise ValueError("validator_enabled must be a boolean")
        had_runtime = getattr(self, "memory", None) is not None and getattr(self, "pipeline", None) is not None
        if had_runtime:
            self.flush_active()
        self.mode = mode or self.mode
        updates: dict[str, Any] = {
            "complexity_threshold": 0.5,
            "validator_sample_rate": 1.0,
            "max_revisions": 2,
            "record_interaction": True,
            "session_window": 10,
            "continuation_enabled": True,
            "max_reply_segments": 3,
            "call_log_enabled": True,
            "model_version": "mock" if self.mode == "demo" else "live",
        }
        if base_url:
            updates["llm_base_url"] = base_url
        elif self.mode == "ollama" and not looks_like_ollama_url(self.settings.llm_base_url):
            updates["llm_base_url"] = DEFAULT_OLLAMA_BASE_URL
        if model:
            updates["llm_model"] = model
        if api_key:
            updates["llm_api_key"] = api_key
        if aux_base_url:
            updates["aux_llm_base_url"] = aux_base_url
        if aux_model:
            updates["aux_llm_model"] = aux_model
        if aux_api_key:
            updates["aux_llm_api_key"] = aux_api_key
        if validator_enabled is not None:
            updates["validator_enabled"] = validator_enabled
        if qq_latency is not None:
            from harness.qq.policy import QQLatencyOptions
            updates.update(QQLatencyOptions.model_validate(qq_latency).model_dump())
        self.settings = self.settings.model_copy(update=updates)
        self.settings.mark_configuration_source(updates, "runtime")

        self._preload_generation += 1
        self.aux_preloaded = False
        if self.mode == "demo":
            new_llm: LLMClient = MockLLM()
            self.aux_active = False
            self.aux_reachable = False
        else:
            new_llm = build_llm_client(self.settings)
            self.aux_active = not (self.settings.agentic_enabled and self.settings.agentic_local_only) and (
                aux_explicitly_configured(self.settings) or aux_endpoint_distinct(self.settings))
        old_llm = getattr(self, "llm", None)
        self.llm = new_llm
        if old_llm is not None and old_llm is not new_llm:
            _close_llm(old_llm)
        self.llm_reachable = probe_llm(self.settings)
        if self.mode != "demo" and self.aux_active and aux_endpoint_distinct(self.settings):
            self.aux_reachable = probe_aux_llm(self.settings)
        elif self.mode != "demo" and self.aux_active:
            self.aux_reachable = self.llm_reachable
        else:
            self.aux_reachable = False

        self.hydrate(self.active_preset_id)
        self._maybe_preload_aux()

    def _swap_runtime(self) -> None:
        old_memory = getattr(self, "memory", None)
        embeddings = build_embedding_client(self.settings, mock=self.mode == "demo")
        self.embedding_info = describe_embedding(embeddings, settings=self.settings)
        self.memory = build_inmemory_service(settings=self.settings, llm=self.llm, embeddings=embeddings)
        self.pipeline = InferencePipeline(
            self.memory,
            settings=self.settings,
            llm=self.llm,
            observer=self.observer,
        )
        if old_memory is not None:
            _close_memory(old_memory)

    def flush_active(self) -> CharacterPreset | None:
        memory = getattr(self, "memory", None)
        pipeline = getattr(self, "pipeline", None)
        if memory is None or pipeline is None:
            return None
        persona = memory.get_persona()
        if persona is None:
            return None
        preset = snapshot_memory(
            memory,
            preset_id=self.active_preset_id,
            session_id=self.active_session_id,
            transcript=self.transcript,
            user_name=self.user_name,
            builtin_id=self._active_builtin_id,
            name=self._active_preset_name or persona.identity.name,
        )
        self.library.save(preset)
        return preset

    def hydrate(self, preset_id: str) -> CharacterPreset:
        preset = self.library.load(preset_id)
        self._swap_runtime()
        hydrate_memory(self.memory, preset)
        self.active_preset_id = preset.preset_id
        self._session_id = preset.session_id
        self._active_builtin_id = preset.builtin_id
        self._active_preset_name = preset.name
        self.transcript = list(preset.transcript)
        self.user_name = normalize_speaker_name(preset.user_name, default=DEFAULT_USER_NAME)
        self.character_name = normalize_speaker_name(preset.persona.identity.name)
        self.pipeline.session_store.replace(self._session_id, self.transcript)
        if self.active_preset_id not in self.open_preset_ids:
            self.open_preset_ids.append(self.active_preset_id)
        return preset

    def open_preset(self, preset_id: str) -> CharacterPreset:
        if preset_id == self.active_preset_id:
            return self.library.load(preset_id)
        self.flush_active()
        if preset_id not in self.open_preset_ids:
            self.open_preset_ids.append(preset_id)
        return self.hydrate(preset_id)

    def apply_preset(self, preset_id: str) -> CharacterPreset:
        if preset_id == self.active_preset_id:
            return self.library.load(preset_id)
        self.flush_active()
        current = self.active_preset_id
        if preset_id in self.open_preset_ids:
            return self.hydrate(preset_id)
        idx = self.open_preset_ids.index(current) if current in self.open_preset_ids else len(self.open_preset_ids)
        opened = [item for item in self.open_preset_ids if item != current]
        opened.insert(min(idx, len(opened)), preset_id)
        self.open_preset_ids = opened
        return self.hydrate(preset_id)

    def new_preset(self, from_id: str | None = None) -> CharacterPreset:
        self.flush_active()
        created = self.library.create_from(from_id or self.active_preset_id)
        self.open_preset_ids.append(created.preset_id)
        return self.hydrate(created.preset_id)

    def restore_active(self) -> CharacterPreset:
        restored = self.library.restore_factory(self.active_preset_id)
        return self.hydrate(restored.preset_id)

    def delete_preset(self, preset_id: str) -> CharacterPreset:
        if preset_id == self.active_preset_id:
            self.flush_active()
        self.library.delete(preset_id)
        self.open_preset_ids = [item for item in self.open_preset_ids if item != preset_id]
        if not self.open_preset_ids:
            self.open_preset_ids = [DEFAULT_PRESET_ID]
        if self.active_preset_id == preset_id:
            return self.hydrate(self.open_preset_ids[0])
        return self.library.load(self.active_preset_id)

    def _maybe_preload_aux(self) -> None:
        if self.mode != "live":
            return
        if not self.settings.aux_llm_preload:
            return
        if not self.aux_active or not aux_model_distinct(self.settings):
            return
        if not self.aux_reachable:
            logger.info("aux llm not reachable, skip preload")
            return
        generation = self._preload_generation
        settings = self.settings
        thread = threading.Thread(
            target=self._preload_worker,
            args=(generation, settings),
            name="nc-aux-preload",
            daemon=True,
        )
        thread.start()

    def _preload_worker(self, generation: int, settings: Settings) -> None:
        ok = preload_aux_model(settings)
        if generation != self._preload_generation:
            return
        self.aux_preloaded = ok
        base_url, model, _ = resolve_aux_endpoint(settings)
        if ok:
            logger.info("aux llm preloaded (%s @ %s)", model, base_url)
        else:
            logger.warning("aux llm preload did not complete (%s @ %s)", model, base_url)

    def set_speakers(self, *, user_name: str | None = None, character_name: str | None = None) -> None:
        if user_name is not None:
            self.user_name = normalize_speaker_name(user_name, default=DEFAULT_USER_NAME)
        if character_name is not None:
            self.character_name = normalize_speaker_name(character_name)
            self._apply_character_name()

    def _apply_character_name(self) -> None:
        name = self.character_name
        if not name:
            return
        persona = self.memory.get_persona()
        if persona is None or persona.identity.name == name:
            return
        apply_persona(self.memory, PersonaPatch(name=name))

    def speakers_payload(self) -> dict[str, str]:
        persona = self.memory.get_persona()
        character_name = persona.identity.name if persona is not None else (self.character_name or "character")
        return {
            "user_name": self.user_name,
            "character_name": character_name,
        }

    def presets_payload(self, *, summaries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if summaries is None:
            summaries = [item.model_dump(mode="json") for item in
                         self.library.list_summaries(open_ids=self.open_preset_ids, active_id=self.active_preset_id)]
        return {
            "active_id": self.active_preset_id,
            "open_ids": list(self.open_preset_ids),
            "items": [{**item, "open": item["preset_id"] in self.open_preset_ids,
                       "active": item["preset_id"] == self.active_preset_id} for item in summaries],
        }

    def snapshot_state(self, session_id: str | None = None, *, preset_summaries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        from harness.qq.policy import effective_configuration
        del session_id
        snap = self.memory.get_state()
        stores = self.memory.stores
        persona = snap.persona
        relationship = snap.relationship
        psych = snap.psych
        world = snap.world
        return {
            "mode": self.mode,
            "effective_configuration": effective_configuration(self.settings),
            "inference_config": {
                key: getattr(self.settings, key) for key in (
                    "context_token_budget", "session_window", "llm_timeout",
                    "parser_llm_enabled", "planner_llm_enabled", "tier_llm_enabled",
                    "validator_enabled", "validator_sample_rate", "call_log_enabled",
                    "complexity_threshold", "max_revisions", "continuation_enabled",
                    "max_reply_segments", "compiler_top_memories", "llm_json_max_tokens",
                    "retrieval_top_k", "retrieval_weights", "aux_llm_num_ctx",
                )
            },
            "llm": {
                "mode": self.mode,
                "base_url": self.settings.llm_base_url,
                "model": self.settings.llm_model,
                "reachable": self.llm_reachable,
                "api_key_configured": _api_key_configured(self.settings),
                "fallback": fallback_status(self.llm),
                "structured": structured_status(self.llm),
                "aux": self._aux_payload(),
                "qq_memory": self._qq_memory_llm_payload(),
                "embedding": dict(self.embedding_info),
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
                "session_id": self.active_session_id,
                "preset_id": self.active_preset_id,
                "turns": [turn.model_dump(mode="json") for turn in self.transcript],
            },
            "speakers": self.speakers_payload(),
            "presets": self.presets_payload(summaries=preset_summaries),
        }

    def _aux_payload(self) -> dict[str, Any]:
        base_url, model, _ = resolve_aux_endpoint(self.settings)
        return {
            "base_url": base_url,
            "model": model,
            "reachable": self.aux_reachable,
            "preloaded": self.aux_preloaded,
            "active": self.aux_active,
        }

    def _qq_memory_llm_payload(self) -> dict[str, Any]:
        base_url, model, _ = resolve_qq_memory_endpoint(self.settings)
        return {"base_url": base_url, "model": model}

    def status(self, session_id: str | None = None, *, preset_summaries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        payload = self.snapshot_state(session_id, preset_summaries=preset_summaries)
        payload["default_session_id"] = self.active_session_id
        return payload

    def debug_state(self) -> dict[str, Any]:
        return collect_debug_state(self.memory, self.settings)

    def respond(self, run_id: str, session_id: str, text: str) -> None:
        del session_id
        target = self.active_session_id
        if self.observer is not None:
            self.observer.bind(run_id)
        try:
            final = self.pipeline.respond(text, session_id=target, speaker=self.user_name, call_id=run_id)
            moment = datetime.now(UTC)
            self.transcript.append(
                SessionTurn(
                    role="user",
                    text=text,
                    speaker=self.user_name,
                    event_id=final.event_id,
                    timestamp=moment,
                )
            )
            character = self.speakers_payload()["character_name"]
            for index, segment in enumerate(final.segments, start=1):
                self.transcript.append(
                    SessionTurn(
                        role="character",
                        text=segment,
                        speaker=character,
                        event_id=f"{final.response_id}:s{index}",
                        timestamp=moment,
                    )
                )
            self.flush_active()
            if self.observer is not None:
                self.observer.emit_state(self.snapshot_state(target), run_id=run_id)
        except Exception as exc:
            moment = datetime.now(UTC)
            self.transcript.append(
                SessionTurn(
                    role="user",
                    text=text,
                    speaker=self.user_name,
                    event_id=run_id,
                    timestamp=moment,
                )
            )
            character = self.speakers_payload()["character_name"]
            self.transcript.append(
                SessionTurn(
                    role="character",
                    text=ERROR_FALLBACK_REPLY,
                    speaker=character,
                    event_id=f"{run_id}:error-fallback",
                    timestamp=moment,
                )
            )
            self.flush_active()
            if self.observer is not None:
                self.observer.emit_error(str(exc), run_id=run_id)
                self.observer.emit_state(self.snapshot_state(target), run_id=run_id)
