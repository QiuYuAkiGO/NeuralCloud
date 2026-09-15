"""Independent, credential-file-only configuration for daily memory revision."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DailyRevisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(default=False, strict=True)
    base_url: str = "https://api.moonshot.cn/v1"
    model: str = "kimi-k3"
    api_key_file: Path | None = None
    queue_path: Path | None = None
    reasoning_effort: Literal["low", "high", "max"] = "high"
    schedule_time: str = "03:00"
    timezone: str = "Asia/Shanghai"
    backfill_days: int = Field(default=7, ge=1, le=90, strict=True)
    request_timeout_seconds: float = Field(default=180, gt=0, le=900, allow_inf_nan=False)
    job_timeout_seconds: float = Field(default=900, gt=0, le=3600, allow_inf_nan=False)
    max_attempts: int = Field(default=3, ge=1, le=5, strict=True)
    max_completion_tokens: int = Field(default=32768, ge=512, le=65536, strict=True)
    chunk_input_tokens: int = Field(default=16000, ge=512, le=196608, strict=True)
    max_input_tokens: int = Field(default=96000, ge=2048, le=196608, strict=True)
    daily_token_budget: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def validate_binding(self):
        try:
            endpoint = urlsplit(self.base_url)
            _ = endpoint.port
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("daily_revision_configuration_invalid") from None
        if (self.base_url != self.base_url.strip() or endpoint.scheme != "https"
                or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment):
            raise ValueError("daily_revision_requires_https_endpoint")
        if not self.model.strip() or self.model != self.model.strip():
            raise ValueError("daily_revision_model_invalid")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.schedule_time):
            raise ValueError("daily_revision_schedule_invalid")
        for path in (self.api_key_file, self.queue_path):
            if path is not None and not path.is_absolute():
                raise ValueError("daily_revision_path_requires_absolute_path")
        if self.enabled and self.api_key_file is None:
            raise ValueError("daily_revision_api_key_file_required")
        if self.chunk_input_tokens > self.max_input_tokens:
            raise ValueError("daily_revision_chunk_exceeds_input_budget")
        return self

    def read_api_key(self) -> str:
        """Read a bounded private file; never include its contents in an error."""
        path = self.api_key_file
        descriptor = -1
        try:
            if path is None or any(item.is_symlink() for item in (path, *path.parents)):
                raise ValueError
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4096
                    or (os.name == "posix" and info.st_mode & 0o077)):
                raise ValueError
            with os.fdopen(descriptor, "r", encoding="utf-8-sig") as handle:
                descriptor = -1
                value = handle.read().strip()
            if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
                raise ValueError
            return value
        except (OSError, UnicodeError, ValueError):
            raise ValueError("daily_revision_credential_unavailable") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @property
    def configured(self) -> bool:
        try:
            self.read_api_key()
            return True
        except ValueError:
            return False

    def public(self) -> dict:
        result = self.model_dump(mode="json", exclude={"api_key_file", "queue_path"})
        result["api_key_configured"] = self.configured
        return result


def load_config(path: str | Path | None = None) -> DailyRevisionConfig:
    if path is None:
        return DailyRevisionConfig()
    try:
        target = Path(path)
        if (not target.is_absolute() or target.is_symlink() or not target.is_file()
                or target.stat().st_size > 65536):
            raise ValueError
        config = DailyRevisionConfig.model_validate_json(target.read_text(encoding="utf-8-sig"))
        if config.enabled:
            config.read_api_key()
        return config
    except (OSError, UnicodeError, ValueError):
        raise ValueError("daily_revision_configuration_invalid") from None
