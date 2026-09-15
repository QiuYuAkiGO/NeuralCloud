"""Trusted launch configuration for the private diary channel."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DiarySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    autonomous_enabled: bool = False
    window_local: str = "21:00-02:00"
    max_entries_per_day: int = Field(default=1, ge=1, le=8)
    quiet_seconds: int = Field(default=60, ge=30, le=3600)
    opportunity_seconds: int = Field(default=1800, ge=300, le=86400)
    autonomous_model_seconds: float = Field(default=12, ge=1, le=25)
    user_model_seconds: float = Field(default=25, ge=1, le=25)
    model_max_tokens: int = Field(default=2048, ge=128, le=8192)
    timezone: str = "Asia/Shanghai"

    @field_validator("window_local")
    @classmethod
    def valid_window(cls, value: str) -> str:
        parts = value.split("-")
        if len(parts) != 2:
            raise ValueError("diary_window_invalid")
        for part in parts:
            hour, _, minute = part.partition(":")
            if not (hour.isdigit() and minute.isdigit()):
                raise ValueError("diary_window_invalid")
            if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                raise ValueError("diary_window_invalid")
        return value

    @classmethod
    def from_environment(cls) -> "DiarySettings":
        value = os.environ.get("NC_DIARY_CONFIG")
        if not value:
            return cls()
        path = Path(value)
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.stat().st_size > 65536):
            raise ValueError("diary_launch_configuration_invalid")
        return cls.model_validate_json(path.read_text(encoding="utf-8-sig"))
