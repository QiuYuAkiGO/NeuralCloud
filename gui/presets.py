"""Character presets: factory templates + writable working copies."""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from harness.core.inference import SessionTurn
from harness.core.models import (
    CandidateMemory,
    EpisodicEvent,
    PersonaCore,
    PsychState,
    RelationshipState,
    SemanticFact,
    WorldState,
)
from harness.memory.service import MemoryService

TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_PRESET_ID = "vaelith-daily"
DEFAULT_SESSION_ID = "sess_vaelith_daily"
DEFAULT_USER_NAME = "用户"
ENV_PRESETS_DIR = "NC_GUI_PRESETS"
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUILTIN_DIR = _REPO_ROOT / "presets" / "builtin"
DEFAULT_WORKING_DIR = _REPO_ROOT / "data" / "presets"
_PRESET_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _now() -> datetime:
    return datetime.now(TZ)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(3).hex()}.tmp")
    tmp.write_text(data, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        path.write_text(data, encoding="utf-8")
        tmp.unlink(missing_ok=True)
    return path


def default_working_dir() -> Path:
    override = os.environ.get(ENV_PRESETS_DIR)
    if override:
        return Path(override)
    return DEFAULT_WORKING_DIR


def default_builtin_dir() -> Path:
    return DEFAULT_BUILTIN_DIR


class CharacterPreset(BaseModel):
    schema_version: str = "preset-v1"
    preset_id: str
    builtin_id: str | None = None
    name: str
    session_id: str
    user_name: str = DEFAULT_USER_NAME
    persona: PersonaCore
    relationships: list[RelationshipState] = Field(default_factory=list)
    psych: PsychState | None = None
    world: WorldState | None = None
    episodic: list[EpisodicEvent] = Field(default_factory=list)
    semantic: list[SemanticFact] = Field(default_factory=list)
    candidates: list[CandidateMemory] = Field(default_factory=list)
    transcript: list[SessionTurn] = Field(default_factory=list)
    updated_at: datetime

    def is_protected(self) -> bool:
        return bool(self.builtin_id) and self.preset_id == self.builtin_id

    def display_name(self) -> str:
        ident = self.persona.identity.name if self.persona is not None else ""
        return self.name or ident or self.preset_id


class PresetSummary(BaseModel):
    preset_id: str
    name: str
    builtin: bool
    builtin_id: str | None = None
    protected: bool = False
    updated_at: datetime | None = None
    open: bool = False
    active: bool = False
    turn_count: int = 0


def snapshot_memory(
    memory: MemoryService,
    *,
    preset_id: str,
    session_id: str,
    transcript: list[SessionTurn],
    user_name: str = DEFAULT_USER_NAME,
    builtin_id: str | None = None,
    name: str | None = None,
    updated_at: datetime | None = None,
) -> CharacterPreset:
    persona = memory.get_persona()
    if persona is None:
        raise ValueError("cannot snapshot a memory service without a persona")
    stores = memory.stores
    return CharacterPreset(
        preset_id=preset_id,
        builtin_id=builtin_id,
        name=name or persona.identity.name,
        session_id=session_id,
        user_name=user_name or DEFAULT_USER_NAME,
        persona=persona,
        relationships=list(stores.relationship.list_all()),
        psych=stores.psych.get(),
        world=stores.world.latest(),
        episodic=list(stores.episodic.list_all()),
        semantic=list(stores.semantic.list_all()),
        candidates=list(stores.candidates.list_all()),
        transcript=list(transcript),
        updated_at=updated_at or _now(),
    )


def hydrate_memory(memory: MemoryService, preset: CharacterPreset) -> None:
    """Load a preset into an empty MemoryService. Does not call record_event."""
    memory.bootstrap_persona(preset.persona)
    for row in preset.relationships:
        memory.stores.relationship.upsert(row)
    if preset.psych is not None:
        memory.stores.psych.upsert(preset.psych)
    if preset.world is not None:
        memory.stores.world.insert(preset.world)
    events, facts = _backfill_embeddings(memory, preset.episodic, preset.semantic)
    for event in events:
        memory.stores.episodic.insert(event)
    for fact in facts:
        memory.stores.semantic.insert(fact)
    for candidate in preset.candidates:
        memory.stores.candidates.insert(candidate)


def _backfill_embeddings(
    memory: MemoryService,
    events: list[EpisodicEvent],
    facts: list[SemanticFact],
) -> tuple[list[EpisodicEvent], list[SemanticFact]]:
    """Preset files ship embedding=None, which silently zeroes the semantic
    retrieval signal for every seed (cosine returns 0 on null input). Texts
    mirror the record_event / add_semantic_fact embedding formats."""
    event_idx = [i for i, event in enumerate(events) if event.embedding is None]
    fact_idx = [i for i, fact in enumerate(facts) if fact.embedding is None]
    if not event_idx and not fact_idx:
        return list(events), list(facts)
    texts = [f"{events[i].summary} {events[i].objective_fact}" for i in event_idx]
    texts.extend(f"{facts[i].subject} {facts[i].predicate} {facts[i].value}" for i in fact_idx)
    vectors = memory.embeddings.embed(texts)
    patched_events = list(events)
    for i, vector in zip(event_idx, vectors[: len(event_idx)]):
        if vector:
            patched_events[i] = events[i].model_copy(update={"embedding": vector})
    patched_facts = list(facts)
    for i, vector in zip(fact_idx, vectors[len(event_idx) :]):
        if vector:
            patched_facts[i] = facts[i].model_copy(update={"embedding": vector})
    return patched_events, patched_facts


def load_preset_file(path: Path) -> CharacterPreset:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return CharacterPreset.model_validate(raw)


def save_preset_file(preset: CharacterPreset, path: Path) -> Path:
    return atomic_write_json(path, preset.model_dump(mode="json"))


def load_builtin(preset_id: str, *, builtin_dir: Path | None = None) -> CharacterPreset:
    target = (builtin_dir or default_builtin_dir()) / f"{preset_id}.json"
    if not target.is_file():
        raise FileNotFoundError(f"builtin preset not found: {preset_id}")
    return load_preset_file(target)


def list_builtin_ids(builtin_dir: Path | None = None) -> list[str]:
    folder = builtin_dir or default_builtin_dir()
    if not folder.is_dir():
        return []
    return sorted(path.stem for path in folder.glob("*.json"))


def _validate_preset_id(preset_id: str) -> str:
    text = (preset_id or "").strip()
    if not _PRESET_ID_RE.match(text):
        raise ValueError("invalid preset id")
    return text


def new_preset_id(stem: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", stem).strip("-").lower() or "preset"
    base = base[:40]
    return f"{base}-{secrets.token_hex(3)}"


class PresetLibrary:
    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        builtin_dir: Path | None = None,
    ) -> None:
        self.working_dir = working_dir if working_dir is not None else default_working_dir()
        self.builtin_dir = builtin_dir if builtin_dir is not None else default_builtin_dir()
        self.working_dir.mkdir(parents=True, exist_ok=True)

    def working_path(self, preset_id: str) -> Path:
        return self.working_dir / f"{_validate_preset_id(preset_id)}.json"

    def builtin_path(self, preset_id: str) -> Path:
        return self.builtin_dir / f"{_validate_preset_id(preset_id)}.json"

    def is_builtin_id(self, preset_id: str) -> bool:
        return self.builtin_path(preset_id).is_file()

    def exists(self, preset_id: str) -> bool:
        return self.working_path(preset_id).is_file() or self.is_builtin_id(preset_id)

    def load(self, preset_id: str) -> CharacterPreset:
        preset_id = _validate_preset_id(preset_id)
        working = self.working_path(preset_id)
        if working.is_file():
            try:
                return load_preset_file(working)
            except (OSError, json.JSONDecodeError, ValidationError):
                if self.is_builtin_id(preset_id):
                    return self.ensure_working(preset_id)
                raise
        if self.is_builtin_id(preset_id):
            return self.ensure_working(preset_id)
        raise FileNotFoundError(f"preset not found: {preset_id}")

    def ensure_working(self, preset_id: str) -> CharacterPreset:
        preset_id = _validate_preset_id(preset_id)
        working = self.working_path(preset_id)
        if working.is_file():
            try:
                return load_preset_file(working)
            except (OSError, json.JSONDecodeError, ValidationError):
                pass
        factory = load_builtin(preset_id, builtin_dir=self.builtin_dir)
        copy = factory.model_copy(update={"updated_at": _now()})
        save_preset_file(copy, working)
        return copy

    def save(self, preset: CharacterPreset) -> Path:
        _validate_preset_id(preset.preset_id)
        updated = preset.model_copy(update={"updated_at": _now()})
        return save_preset_file(updated, self.working_path(updated.preset_id))

    def create_from(self, from_id: str) -> CharacterPreset:
        source = self.load(from_id)
        preset_id = new_preset_id(source.builtin_id or source.preset_id or source.name)
        cloned = source.model_copy(
            deep=True,
            update={
                "preset_id": preset_id,
                "session_id": f"sess_{preset_id}",
                "transcript": [],
                "updated_at": _now(),
                "builtin_id": source.builtin_id or (from_id if self.is_builtin_id(from_id) else None),
            },
        )
        self.save(cloned)
        return cloned

    def restore_factory(self, preset_id: str) -> CharacterPreset:
        current = self.load(preset_id)
        source_id = current.builtin_id or (preset_id if self.is_builtin_id(preset_id) else None)
        if not source_id or not self.is_builtin_id(source_id):
            raise ValueError("preset has no factory template to restore")
        factory = load_builtin(source_id, builtin_dir=self.builtin_dir)
        restored = factory.model_copy(
            deep=True,
            update={
                "preset_id": current.preset_id,
                "session_id": current.session_id,
                "transcript": [],
                "updated_at": _now(),
                "builtin_id": source_id,
            },
        )
        self.save(restored)
        return restored

    def delete(self, preset_id: str) -> None:
        preset_id = _validate_preset_id(preset_id)
        if self.is_builtin_id(preset_id):
            raise ValueError("cannot delete a builtin preset")
        path = self.working_path(preset_id)
        if path.is_file():
            path.unlink()
            return
        raise FileNotFoundError(f"preset not found: {preset_id}")

    def list_ids(self) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for preset_id in list_builtin_ids(self.builtin_dir):
            ids.append(preset_id)
            seen.add(preset_id)
        if self.working_dir.is_dir():
            for path in sorted(self.working_dir.glob("*.json")):
                if path.stem not in seen:
                    ids.append(path.stem)
                    seen.add(path.stem)
        return ids

    def summarize(
        self,
        preset_id: str,
        *,
        open_ids: list[str] | None = None,
        active_id: str | None = None,
    ) -> PresetSummary:
        preset = self.load(preset_id)
        protected = preset.is_protected() or self.is_builtin_id(preset_id)
        opened = open_ids or []
        return PresetSummary(
            preset_id=preset.preset_id,
            name=preset.display_name(),
            builtin=self.is_builtin_id(preset.preset_id),
            builtin_id=preset.builtin_id,
            protected=protected,
            updated_at=preset.updated_at,
            open=preset.preset_id in opened,
            active=preset.preset_id == active_id,
            turn_count=len(preset.transcript),
        )

    def list_summaries(
        self,
        *,
        open_ids: list[str] | None = None,
        active_id: str | None = None,
    ) -> list[PresetSummary]:
        items: list[PresetSummary] = []
        for preset_id in self.list_ids():
            try:
                items.append(self.summarize(preset_id, open_ids=open_ids, active_id=active_id))
            except (OSError, json.JSONDecodeError, ValidationError, FileNotFoundError, ValueError):
                continue
        return items
