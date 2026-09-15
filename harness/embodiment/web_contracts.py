"""Provider-neutral web observations. Remote content is attributed, untrusted data."""

from __future__ import annotations

from datetime import datetime
import ipaddress
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, field_validator


class WebError(RuntimeError):
    """A fixed public error code, never provider text or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _moment(value: Any) -> Any:
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


Moment = Annotated[AwareDatetime, BeforeValidator(_moment)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, revalidate_instances="always")


def validate_public_url(value: str) -> str:
    """Validate and normalize an HTTP(S) source URL without fetching or resolving it.

    Only the configured search service fetches sources. Reject literal private
    addresses, local hostnames and alternate numeric IP spellings at this boundary.
    """
    try:
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) <= 32 for c in value) or "\\" in value:
            raise ValueError
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or parts.username is not None or parts.password is not None:
            raise ValueError
        hostname = parts.hostname
        if not hostname or parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError
        hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if ("." not in hostname or re.fullmatch(r"[0-9.]+", hostname)
                    or hostname.endswith((".localhost", ".local", ".internal", ".invalid", ".test"))
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                           for label in hostname.split("."))
                    or any(label.startswith("0x") and re.fullmatch(r"0x[0-9a-f]+", label)
                           for label in hostname.split("."))):
                raise ValueError
        else:
            if not address.is_global or address.is_multicast or address.is_unspecified:
                raise ValueError
            hostname = f"[{address.compressed}]" if address.version == 6 else address.compressed
        netloc = hostname + (f":{parts.port}" if parts.port is not None else "")
        return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    except (TypeError, ValueError, UnicodeError):
        raise WebError("web_source_url_invalid") from None


class WebSearchArguments(_Strict):
    query: str = Field(min_length=1, max_length=1000)
    max_results: int = Field(default=5, ge=1, le=10)
    time_range: Literal["day", "week", "month", "year"] | None = None

    @field_validator("query")
    @classmethod
    def _query(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("web_query_invalid")
        return value


class WebReadArguments(_Strict):
    source_refs: list[str] = Field(min_length=1, max_length=3)

    @field_validator("source_refs")
    @classmethod
    def _refs(cls, value: list[str]) -> list[str]:
        if any(not ref or len(ref) > 256 or any(c.isspace() for c in ref) for ref in value) or len(set(value)) != len(value):
            raise ValueError("web_source_refs_invalid")
        return value


class WebSource(_Strict):
    source_ref: str = Field(default="", max_length=256)
    origin: Literal["user_link", "search", "read"] = "search"
    title: str = Field(max_length=1000)
    url: str = Field(max_length=4096)
    snippet: str = Field(default="", max_length=2000)
    content: str = Field(default="", max_length=12000)
    retrieved_at: Moment
    published_at: str | None = Field(default=None, max_length=128)
    truncated: bool = False

    _url = field_validator("url")(validate_public_url)


class WebResult(_Strict):
    status: Literal["completed", "partial"] = "completed"
    sources: list[WebSource] = Field(default_factory=list, max_length=10)
    failures: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=10)
    provider_request_id: str | None = Field(default=None, max_length=256)
    usage: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
