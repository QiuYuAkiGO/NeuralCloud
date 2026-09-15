"""Trusted launch configuration for the private Linux expression board."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BoardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    autonomous_enabled: bool = True
    base_url: str = "http://127.0.0.1:8793"
    token_file: Path
    body_instance_id: str = "misty-linux-board-01"
    board_id: str = "misty-thoughts"
    opportunity_seconds: int = Field(default=1800, ge=300, le=86400)
    quiet_seconds: int = Field(default=60, ge=30, le=3600)
    user_model_seconds: float = Field(default=25, ge=1, le=25)
    autonomous_model_seconds: float = Field(default=12, ge=1, le=25)
    model_max_tokens: int = Field(default=2048, ge=128, le=8192)

    @model_validator(mode="after")
    def validate_binding(self):
        url = urlsplit(self.base_url)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError("board_requires_local_origin")
        if not self.token_file.is_absolute():
            raise ValueError("board_token_requires_absolute_path")
        # Import lazily: configuration parsing must not construct a body or brain.
        self.scope
        return self

    @property
    def scope(self):
        from harness.embodiment.board_contracts import BoardScope
        return BoardScope(body_instance_id=self.body_instance_id, board_id=self.board_id)

    def client(self):
        from harness.embodiment.providers.board import BoardClient
        return BoardClient(self.base_url, token_file=self.token_file)

    @classmethod
    def from_environment(cls):
        value = os.environ.get("NC_BOARD_CONFIG")
        if not value:
            return None
        path = Path(value)
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.stat().st_size > 65536):
            raise ValueError("board_launch_configuration_invalid")
        return cls.model_validate_json(path.read_text(encoding="utf-8-sig"))
