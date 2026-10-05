"""Dedicated, file-configured DeepSeek routing for the two emotion stages."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from harness.llm.client import OpenAICompatClient


class EmotionEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="deepseek-v4-flash", min_length=1)
    api_key_file: Path


class DeepSeekEmotionClient(OpenAICompatClient):
    def complete_json(self, *, system, user, response_format=None, **kwargs):
        if self.settings.structured_llm_config is not None:
            return super().complete_json(system=system, user=user, response_format=response_format, **kwargs)
        # Chat Completions JSON mode does not enforce a JSON Schema. Supply it
        # explicitly; EmotionService validates the resulting typed payload.
        if response_format and response_format.get("type") == "json_schema":
            schema = response_format["json_schema"]["schema"]
            system += "\nReturn only a JSON object conforming to this JSON Schema:\n" + json.dumps(
                schema, ensure_ascii=False, separators=(",", ":"))
        return super().complete_json(system=system, user=user,
            response_format={"type": "json_object"}, **kwargs)


def build_emotion_client(settings):
    if getattr(settings, "structured_llm_config", None) is not None:
        from harness.llm.structured import build_structured_client
        return build_structured_client(settings)
    path = getattr(settings, "emotion_llm_config", None)
    if not path:
        return None
    config = EmotionEndpoint.model_validate_json(Path(path).read_bytes())
    if not config.api_key_file.is_absolute():
        raise ValueError("emotion API key file must be absolute")
    key = config.api_key_file.read_text(encoding="utf-8-sig").strip()
    if not key or any(char.isspace() for char in key):
        raise ValueError("emotion API key file must contain one nonempty key")
    dedicated = settings.model_copy(update={"llm_base_url": "https://api.deepseek.com",
        "llm_model": config.model, "llm_api_key": key})
    return DeepSeekEmotionClient(dedicated, extra_payload={"thinking": {"type": "disabled"}})
