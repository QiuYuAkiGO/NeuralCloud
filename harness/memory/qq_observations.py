"""Durable QQ observations, isolated from inference, control, and delivery.

Authenticated collectors must supply identifiers actually obtained from QQ.
This module never derives a QQ identifier from a nickname, text, or screenshot.
It validates the collector's assertion; it does not independently contact QQ.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator


QQIdentifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[1-9][0-9]{0,19}$")]
EvidenceRef = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)]


class QQMessageSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    platform: Literal["qq"] = "qq"
    group_id: QQIdentifier
    member_id: QQIdentifier
    message_id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
    identity_source: Literal["qq_metadata", "qq_visible_identifiers"]

    @field_validator("message_id")
    @classmethod
    def actual_message_id(cls, value: str) -> str:
        if any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("message_id must be the QQ identifier without whitespace")
        if value.casefold() in {"unknown", "none", "null", "missing", "unavailable", "n/a", "?"}:
            raise ValueError("a known QQ message_id is required")
        if value.casefold().startswith(("screenshot:", "capture:", "sha256:", "ocr:")):
            raise ValueError("observation fingerprints are not QQ message identifiers")
        return value


class QQObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: QQMessageSource
    author_name: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)] | None = None
    text: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=32768)]
    observed_at: AwareDatetime
    message_time: AwareDatetime | None = None
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=16)

    @field_validator("text", "author_name")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("observed text and author names cannot be blank")
        return value

    @field_validator("evidence")
    @classmethod
    def nonblank_evidence(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("evidence references cannot be blank")
        return list(dict.fromkeys(value))

    @field_validator("observed_at", "message_time")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value is not None else None


class QQObservation(QQObservationInput):
    observation_id: str
    stored_at: AwareDatetime


class QQObservationResult(BaseModel):
    created: bool
    observation: QQObservation


class QQObservationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: QQIdentifier
    member_id: QQIdentifier | None = None
    limit: int = Field(default=50, ge=1, le=100)


class QQObservationConflict(ValueError):
    """An existing QQ message identity was submitted with conflicting content."""


class QQObservationStorageError(RuntimeError):
    """Persisted observation data cannot be read safely."""


class QQObservationStore:
    """SQLite is the sole authority, including after restart and across writers.

    No process-local cache, embeddings, global memory fallback, or supervisor
    reference is used. Connections live for one bounded operation.
    """

    def __init__(self, path: Path | str) -> None:
        if str(path) == ":memory:":
            raise ValueError("QQ observations require a persistent database path")
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        # Initialize lazily: an unavailable observation database must not cause
        # the independent Runtime controller to fail during service startup.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """CREATE TABLE IF NOT EXISTS qq_observations (
                    group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (group_id, message_id)
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS qq_observations_by_member
                   ON qq_observations (group_id, member_id, observed_at DESC)"""
            )
            return connection
        except BaseException:
            connection.close()
            raise

    def observe(self, incoming: QQObservationInput) -> QQObservationResult:
        # Validate again at the storage boundary, even for direct Python callers.
        incoming = QQObservationInput.model_validate(incoming.model_dump())
        source = incoming.source
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT record_json FROM qq_observations WHERE group_id = ? AND message_id = ?",
                (source.group_id, source.message_id),
            ).fetchone()
            if existing is not None:
                record = self._decode(existing["record_json"])
                different_time = (
                    record.message_time is not None
                    and incoming.message_time is not None
                    and record.message_time != incoming.message_time
                )
                if record.source.member_id != source.member_id or record.text != incoming.text or different_time:
                    raise QQObservationConflict(
                        "QQ source message already exists with different author, text, or message time"
                    )
                # A new screenshot, timestamp, or mutable display name is not a
                # new logical message. Keep the original attribution/evidence.
                return QQObservationResult(created=False, observation=record)
            record = QQObservation(
                **incoming.model_dump(),
                observation_id=f"qq-observation:{uuid4().hex}",
                stored_at=datetime.now(timezone.utc),
            )
            connection.execute(
                """INSERT INTO qq_observations
                   (group_id, message_id, member_id, observed_at, record_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    source.group_id,
                    source.message_id,
                    source.member_id,
                    record.observed_at.isoformat(),
                    record.model_dump_json(),
                ),
            )
            return QQObservationResult(created=True, observation=record)

    def find(self, *, group_id: str, member_id: str | None = None, limit: int = 50) -> list[QQObservation]:
        query = QQObservationQuery(group_id=group_id, member_id=member_id, limit=limit)
        sql = "SELECT record_json FROM qq_observations WHERE group_id = ?"
        parameters: list[str | int] = [query.group_id]
        if query.member_id is not None:
            sql += " AND member_id = ?"
            parameters.append(query.member_id)
        sql += " ORDER BY observed_at DESC, message_id DESC LIMIT ?"
        parameters.append(query.limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [self._decode(row["record_json"]) for row in rows]

    @staticmethod
    def _decode(record_json: str) -> QQObservation:
        try:
            return QQObservation.model_validate_json(record_json)
        except ValidationError as exc:
            raise QQObservationStorageError("persisted QQ observation is invalid") from exc
