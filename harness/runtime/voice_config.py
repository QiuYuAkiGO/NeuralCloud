"""Trusted connection and fixed character profile for the independent voice body."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VoiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(default=True, strict=True)
    mode: Literal["observe", "grounded"] = "grounded"
    base_url: str = "http://127.0.0.1:8794"
    token_file: Path
    body_instance_id: str = "misty-voice-01"
    profile_id: str
    profile_revision: int = Field(ge=1, strict=True)
    timeout_seconds: float = Field(default=5, gt=0, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_binding(self) -> VoiceSettings:
        try:
            url = urlsplit(self.base_url)
            _ = url.port
        except ValueError as exc:
            raise ValueError("voice_requires_local_origin") from exc
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or url.username is not None or url.password is not None
                or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError("voice_requires_local_origin")
        if not self.token_file.is_absolute():
            raise ValueError("voice_token_requires_absolute_path")
        self.scope
        return self

    @property
    def scope(self):
        from harness.embodiment.voice_contracts import VoiceScope

        return VoiceScope(body_instance_id=self.body_instance_id, profile_id=self.profile_id,
                          profile_revision=self.profile_revision)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> VoiceSettings | None:
        values = os.environ if environ is None else environ
        value = values.get("NC_VOICE_CONFIG")
        if not value:
            return None
        path = Path(value)
        try:
            if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or path.stat().st_size > 65536):
                raise ValueError("voice_launch_configuration_invalid")
            return cls.model_validate_json(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            # A configuration may contain misplaced credentials; do not echo it.
            raise ValueError("voice_launch_configuration_invalid") from None
