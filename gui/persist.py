"""Persist GUI runtime options (mode, LLM endpoint, debug tuning) across restarts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from gui.debug import TuningPatch, apply_tuning
from gui.presets import DEFAULT_PRESET_ID
from harness.config import Settings

Mode = Literal["demo", "live", "ollama"]
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"


def looks_like_ollama_url(url: str | None) -> bool:
    return ":11434" in (url or "")


ENV_CONFIG_PATH = "NC_GUI_CONFIG"
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = _REPO_ROOT / "data" / "gui_settings.json"
DEFAULT_USER_NAME = "用户"
MAX_SPEAKER_NAME = 32


def normalize_speaker_name(value: str | None, *, default: str = "") -> str:
    text = " ".join((value or "").split())
    if not text:
        return default
    return text[:MAX_SPEAKER_NAME]


class GuiPersistedSettings(BaseModel):
    mode: Mode = "demo"
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    aux_base_url: str | None = None
    aux_model: str | None = None
    aux_api_key: str | None = None
    user_name: str = DEFAULT_USER_NAME
    character_name: str = ""
    active_preset_id: str = DEFAULT_PRESET_ID
    open_preset_ids: list[str] = Field(default_factory=lambda: [DEFAULT_PRESET_ID])
    tuning: TuningPatch | None = None


def atomic_write_json(path: Path, payload: dict[str, Any] | list[Any]) -> Path:
    """Write JSON via a unique temp file. Falls back to direct write on Windows replace locks."""
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


def default_config_path() -> Path:
    override = os.environ.get(ENV_CONFIG_PATH)
    if override:
        return Path(override)
    return DEFAULT_CONFIG_PATH


def load_gui_settings(path: Path | None = None) -> GuiPersistedSettings:
    target = path if path is not None else default_config_path()
    if not target.is_file():
        return GuiPersistedSettings()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return GuiPersistedSettings.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError):
        return GuiPersistedSettings()


def save_gui_settings(settings: GuiPersistedSettings, path: Path | None = None) -> Path:
    target = path if path is not None else default_config_path()
    return atomic_write_json(target, settings.model_dump(mode="json"))


def snapshot_gui_settings(
    *,
    mode: Mode,
    settings: Settings,
    user_name: str = DEFAULT_USER_NAME,
    character_name: str = "",
    active_preset_id: str = DEFAULT_PRESET_ID,
    open_preset_ids: list[str] | None = None,
) -> GuiPersistedSettings:
    active = (active_preset_id or DEFAULT_PRESET_ID).strip() or DEFAULT_PRESET_ID
    opened = [item for item in (open_preset_ids or [active]) if item]
    if active not in opened:
        opened = [active, *[item for item in opened if item != active]]
    return GuiPersistedSettings(
        mode=mode,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        aux_base_url=settings.aux_llm_base_url,
        aux_model=settings.aux_llm_model,
        aux_api_key=settings.aux_llm_api_key,
        user_name=normalize_speaker_name(user_name, default=DEFAULT_USER_NAME),
        character_name=normalize_speaker_name(character_name),
        active_preset_id=active,
        open_preset_ids=opened,
        tuning=TuningPatch(
            retrieval_weights=dict(settings.retrieval_weights),
            consolidation_threshold=settings.consolidation_threshold,
            initial_candidate_confidence=settings.initial_candidate_confidence,
            confidence_increment=settings.confidence_increment,
            retrieval_pool_size=settings.retrieval_pool_size,
            retrieval_top_k=settings.retrieval_top_k,
            recency_half_life_hours=settings.recency_half_life_hours,
        ),
    )


def persist_gui_settings(
    *,
    mode: Mode,
    settings: Settings,
    path: Path | None = None,
    user_name: str | None = None,
    character_name: str | None = None,
    active_preset_id: str | None = None,
    open_preset_ids: list[str] | None = None,
) -> Path:
    previous = load_gui_settings(path)
    return save_gui_settings(
        snapshot_gui_settings(
            mode=mode,
            settings=settings,
            user_name=previous.user_name if user_name is None else user_name,
            character_name=previous.character_name if character_name is None else character_name,
            active_preset_id=previous.active_preset_id if active_preset_id is None else active_preset_id,
            open_preset_ids=previous.open_preset_ids if open_preset_ids is None else open_preset_ids,
        ),
        path,
    )


def apply_loaded_tuning(settings: Settings, persisted: GuiPersistedSettings) -> None:
    if persisted.tuning is None:
        return
    try:
        apply_tuning(settings, persisted.tuning)
    except ValueError:
        return
