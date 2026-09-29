"""Validated runtime connection settings for the MistyAI body provider."""

from __future__ import annotations

import os
import hmac
from dataclasses import dataclass, field
from ipaddress import ip_address
from math import isfinite
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


class MistyAIConfigurationError(ValueError):
    """Raised when the MistyAI provider is enabled without safe settings."""


_PLACEHOLDER_TOKENS = frozenset(
    {
        "changeme",
        "change-me",
        "development-misty-token",
        "dummy-token",
        "example-token",
        "mistyai-token",
        "placeholder",
        "replace-me",
        "replace-with-body-service-token",
        "test-token",
        "token",
        "your-token",
    }
)
_PLACEHOLDER_PREFIXES = (
    "development-",
    "dummy-",
    "example-",
    "placeholder-",
    "replace-",
    "test-",
    "your-",
)
_MAX_TOKEN_FILE_BYTES = 64 * 1024
_MIN_TOKEN_LENGTH = 16


@dataclass(frozen=True, slots=True)
class MistyAIConnectionSettings:
    """MistyAI settings with the credential excluded from representations."""

    base_url: str
    token: str = field(repr=False)
    timeout_seconds: float = 10.0
    poll_interval_seconds: float = 0.2
    token_source: str = "environment"
    accounting_token: str | None = field(default=None, repr=False)
    accounting_token_source: str | None = None

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> MistyAIConnectionSettings:
        values = os.environ if environ is None else environ
        token, token_source = _load_token(values)
        accounting_token, accounting_source = None, None
        if any(key in values for key in ("NC_MISTYAI_ACCOUNTING_TOKEN", "NC_MISTYAI_ACCOUNTING_TOKEN_FILE")):
            accounting_values = {
                key.replace("NC_MISTYAI_ACCOUNTING_", "NC_MISTYAI_"): value
                for key, value in values.items()
                if key.startswith("NC_MISTYAI_ACCOUNTING_")
            }
            try:
                accounting_token, accounting_source = _load_token(accounting_values)
            except MistyAIConfigurationError as exc:
                raise MistyAIConfigurationError(
                    str(exc).replace("NC_MISTYAI_TOKEN", "NC_MISTYAI_ACCOUNTING_TOKEN")
                ) from None
            if hmac.compare_digest(accounting_token, token):
                raise MistyAIConfigurationError("MistyAI accounting credential must differ from the body credential")
        base_url = _validate_base_url(
            values.get("NC_MISTYAI_BASE_URL") or values.get("NC_MISTYAI_URL") or "http://127.0.0.1:8787"
        )
        return cls(
            base_url=base_url,
            token=token,
            timeout_seconds=_positive_float(
                "NC_MISTYAI_TIMEOUT_SECONDS",
                values.get("NC_MISTYAI_TIMEOUT_SECONDS", "10"),
            ),
            poll_interval_seconds=_positive_float(
                "NC_MISTYAI_POLL_INTERVAL_SECONDS",
                values.get("NC_MISTYAI_POLL_INTERVAL_SECONDS", "0.2"),
            ),
            token_source=token_source,
            accounting_token=accounting_token,
            accounting_token_source=accounting_source,
        )


def _load_token(values: Mapping[str, str]) -> tuple[str, str]:
    token_file_value = values.get("NC_MISTYAI_TOKEN_FILE", "").strip()
    if token_file_value:
        token_path = Path(token_file_value).expanduser()
        try:
            if not token_path.is_file():
                raise MistyAIConfigurationError(f"NC_MISTYAI_TOKEN_FILE is not a readable file: {token_path}")
            if token_path.stat().st_size > _MAX_TOKEN_FILE_BYTES:
                raise MistyAIConfigurationError(f"NC_MISTYAI_TOKEN_FILE exceeds {_MAX_TOKEN_FILE_BYTES} bytes")
            token = token_path.read_text(encoding="utf-8-sig").strip()
        except MistyAIConfigurationError:
            raise
        except (OSError, UnicodeError) as exc:
            raise MistyAIConfigurationError(f"could not read NC_MISTYAI_TOKEN_FILE: {token_path}") from exc
        return _validate_token(token), "file"

    if "NC_MISTYAI_TOKEN" not in values:
        raise MistyAIConfigurationError("MistyAI requires NC_MISTYAI_TOKEN_FILE or NC_MISTYAI_TOKEN")
    return _validate_token(values.get("NC_MISTYAI_TOKEN", "")), "environment"


def _validate_token(value: str) -> str:
    token = value.strip()
    normalized = token.casefold()
    if not token:
        raise MistyAIConfigurationError("MistyAI token cannot be empty")
    if len(token) < _MIN_TOKEN_LENGTH:
        raise MistyAIConfigurationError(f"MistyAI token must contain at least {_MIN_TOKEN_LENGTH} characters")
    if any(character.isspace() for character in token):
        raise MistyAIConfigurationError("MistyAI token must be a single non-whitespace value")
    if (
        normalized in _PLACEHOLDER_TOKENS
        or normalized.startswith(_PLACEHOLDER_PREFIXES)
        or (normalized.startswith("${") and normalized.endswith("}"))
        or (normalized.startswith("<") and normalized.endswith(">"))
    ):
        raise MistyAIConfigurationError("MistyAI token is a development or placeholder value")
    return token


def _validate_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    try:
        parsed = urlsplit(base_url)
        _ = parsed.port
    except ValueError as exc:
        raise MistyAIConfigurationError("NC_MISTYAI_BASE_URL is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MistyAIConfigurationError("NC_MISTYAI_BASE_URL must be an HTTP(S) origin")
    if parsed.username is not None or parsed.password is not None:
        raise MistyAIConfigurationError("NC_MISTYAI_BASE_URL must not contain credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise MistyAIConfigurationError(
            "NC_MISTYAI_BASE_URL must be an HTTP(S) origin without a path, query, or fragment"
        )
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise MistyAIConfigurationError("NC_MISTYAI_BASE_URL must use HTTPS unless the host is loopback")
    return base_url


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").casefold()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _positive_float(name: str, value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise MistyAIConfigurationError(f"{name} must be a positive number") from exc
    if not isfinite(parsed) or parsed <= 0:
        raise MistyAIConfigurationError(f"{name} must be a positive number")
    return parsed
