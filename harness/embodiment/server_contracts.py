"""Provider-neutral Linux server observations. Command output is attributed, untrusted data."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ServerError(RuntimeError):
    """A fixed public error code, never host output, keys, or exception text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, revalidate_instances="always")


MAX_COMMAND_BYTES = 2000
MAX_OUTPUT_BYTES = 64 * 1024


def validate_cwd(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value or len(value) > 1024:
        raise ServerError("server_cwd_invalid")
    parts = [item for item in value.split("/") if item]
    if any(item in {".", ".."} for item in parts):
        raise ServerError("server_cwd_invalid")
    return value


class ServerCommandArguments(_Strict):
    command: str = Field(min_length=1, max_length=MAX_COMMAND_BYTES)
    cwd: str | None = Field(default=None, max_length=1024)
    timeout_seconds: int = Field(default=60, ge=1, le=300, strict=True)

    @field_validator("command")
    @classmethod
    def _command(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 and c not in "\t\n" for c in value) or "\0" in value:
            raise ValueError("server_command_invalid")
        return value

    @field_validator("cwd")
    @classmethod
    def _cwd(cls, value: str | None) -> str | None:
        return validate_cwd(value)


class ServerCommandResult(_Strict):
    status: Literal["completed", "pending_approval", "failed"] = "completed"
    command_id: str = Field(default="", max_length=256)
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    stdout: str = Field(default="", max_length=MAX_OUTPUT_BYTES)
    stderr: str = Field(default="", max_length=MAX_OUTPUT_BYTES)
    truncated: bool = False
    duration_ms: float = Field(default=0, ge=0, le=1_000_000, allow_inf_nan=False)
    risk_class: Literal["read_only", "routine", "high_risk"]
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    observation: dict[str, Any] | None = Field(default=None)
