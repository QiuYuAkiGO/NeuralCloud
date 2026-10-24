"""Persistent cross-channel memory with immutable sources and hard visibility.

This facade does not operate QQ, call a model, update message-handled state, or
replace any original store. Importers must supply authenticated source records.
Read scopes and visibility grants come from trusted application configuration,
never from model output or a display name.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import heapq
import json
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal
import unicodedata

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from harness.contracts.decisions import ActionOutcomeEvidence
from harness.qq.memory import QQGroupContext, QQImageAnalysis, QQPersonContext, QQVerifiedProfile, QQVisualObservation
from harness.qq.conversation import OFFICIAL_PRIVATE, conversation_audience, conversation_scope, private_scope_id
from harness.qq.telemetry import measure_stage
from harness.qq.official_identity import GROUP, PERSON, split_identity


NonBlank = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048, pattern=r"\S")]
Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Principal = Annotated[str, StringConstraints(strict=True, pattern=rf"^(?:local:[A-Za-z0-9][A-Za-z0-9_.-]*|character:[A-Za-z0-9][A-Za-z0-9_.-]*|qq:member:(?:[1-9][0-9]{{0,19}}|{PERSON}))$", max_length=256)]
Audience = Annotated[str, StringConstraints(strict=True, pattern=rf"^(?:local:[A-Za-z0-9][A-Za-z0-9_.-]*|qq:group:(?:[1-9][0-9]{{0,19}}|{GROUP})|qq:private:[1-9][0-9]{{0,19}}:[1-9][0-9]{{0,19}}|qq:{OFFICIAL_PRIVATE})$", max_length=256)]
Channel = Literal["local_chat", "qq_group", "qq_private", "private_board", "private_diary", "body"]
Kind = Literal["local_incoming", "local_outgoing", "qq_profile", "qq_observation", "qq_delivery", "qq_segment_delivery", "board_entry", "diary_entry", "action_outcome"]
Evidence = Annotated[tuple[NonBlank, ...], Field(min_length=1, max_length=128)]
Visibility = Annotated[tuple[Audience, ...], Field(min_length=1, max_length=64)]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SharedLocalEvent(_Frozen):
    """A real local user utterance or displayed assistant utterance, not QQ."""

    event_id: Identifier
    session_id: Identifier
    author_principal: Principal
    role: Literal["user", "assistant"]
    text: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=32768, pattern=r"\S")]
    occurred_at: AwareDatetime
    evidence_refs: Evidence
    display_name: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)] | None = None
    visibility: Visibility = ("local:main",)

    @model_validator(mode="after")
    def local_author(self):
        expected = "local:" if self.role == "user" else "character:"
        if not self.author_principal.startswith(expected):
            raise ValueError("local role requires its own namespaced principal")
        return self


class SharedReadScope(_Frozen):
    owner_id: Identifier = "misty"
    audience: Audience
    # None means all authorized records. Empty tuples explicitly select none.
    channels: tuple[Channel, ...] | None = None
    session_ids: tuple[Identifier, ...] | None = None
    principals: tuple[Principal, ...] | None = None
    kinds: tuple[Kind, ...] | None = None


class SharedActionOutcome(_Frozen):
    """A committed capability outcome bound by the authenticated Runtime adapter."""

    event_id: Identifier
    owner_id: Identifier = "misty"
    session_id: Identifier
    intent_id: Identifier
    evidence: ActionOutcomeEvidence

    @model_validator(mode="after")
    def verified_outcome(self):
        if (self.event_id != self.evidence.prepare_id + ":action_outcome"
                or not self.evidence.evidence_refs
                or any(not ref.strip() for ref in self.evidence.evidence_refs)):
            raise ValueError("action source requires the committed prepare identity and verifier evidence")
        return self


class SharedWindow(_Frozen):
    owner_id: Identifier
    preset_id: Identifier
    start: AwareDatetime
    end: AwareDatetime
    watermark: int = Field(ge=0)
    coverage_gaps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_window(self):
        if self.start >= self.end:
            raise ValueError("source window must be a nonempty half-open interval")
        return self


class SharedSourcePage(_Frozen):
    records: tuple["SharedMemoryRecord", ...]
    next_cursor: int = Field(ge=0)
    exhausted: bool


class SharedSource(_Frozen):
    kind: Kind
    original_record_id: Identifier
    original_sha256: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
    original_json: Annotated[str, StringConstraints(strict=True, min_length=2, max_length=1048576)]
    original_row_sha256: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_validator(mode="after")
    def canonical_source(self):
        value = json.loads(self.original_json)
        if not isinstance(value, dict) or _json(value) != self.original_json or _hash(self.original_json) != self.original_sha256:
            raise ValueError("original source must have a matching canonical JSON hash")
        return self


class SharedClaim(_Frozen):
    kind: Literal["unverified_author_assertion"] = "unverified_author_assertion"
    statement: str
    supporting_quote: str
    subject: Literal["self", "other", "account_relationship", "unspecified"]
    subject_label: str | None = None


class SharedMemoryRecord(_Frozen):
    schema_version: Literal["shared-memory-record-v1"] = "shared-memory-record-v1"
    record_id: Identifier
    owner_id: Identifier
    channel: Channel
    session_id: Identifier
    author_principal: Principal
    display_name: str | None = None
    kind: Kind
    authority: Literal["speaker_utterance", "verified_profile_observation", "verified_delivery", "verified_action_outcome"]
    text: str
    occurred_at: AwareDatetime
    stored_at: AwareDatetime
    evidence_refs: Evidence
    visibility: Visibility
    source: SharedSource
    profile_record_id: Identifier | None = None
    claims: tuple[SharedClaim, ...] = ()
    image_analysis: tuple[QQImageAnalysis, ...] = ()

    @model_validator(mode="after")
    def source_binding(self):
        if self.kind != "qq_observation" and self.image_analysis:
            raise ValueError("only QQ observations carry attached image interpretations")
        if self.kind != self.source.kind:
            raise ValueError("record and original source kind disagree")
        if self.record_id != _record_id(self.owner_id, self.kind, self.source.original_record_id):
            raise ValueError("record identifier does not bind its source")
        original = json.loads(self.source.original_json)
        source_id_key = "event_id" if self.kind.startswith("local_") or self.kind == "action_outcome" else "record_id" if self.kind in {"qq_profile", "qq_observation"} else "receipt_id"
        original_id = original.get("receipt", {}).get("entry_id") if self.kind == "board_entry" else original.get("entry_id") if self.kind == "diary_entry" else original.get(source_id_key)
        if original_id != self.source.original_record_id:
            raise ValueError("original source identifier disagrees with source reference")
        if self.kind == "action_outcome" and original.get("owner_id") != self.owner_id:
            raise ValueError("action original belongs to a different owner")
        expected = _project_original(self.kind, original)
        actual = self.model_dump(mode="json", include=set(expected))
        actual["occurred_at"] = self.occurred_at.isoformat()
        if actual != json.loads(_json(expected)):
            raise ValueError("projection disagrees with its immutable original")
        if self.kind.startswith("local_") and set(self.visibility) != set(original["visibility"]):
            raise ValueError("local visibility disagrees with original event")
        _validate_visibility(self.channel, self.session_id, self.visibility)
        return self

    def model_projection(self) -> dict:
        """Safe data projection: original payload remains available in storage."""
        value = self.model_dump(mode="json", exclude={"visibility", "source", "stored_at", "owner_id"})
        value["source"] = self.source.model_dump(mode="json", exclude={"original_json"})
        return value


class SharedWriteResult(_Frozen):
    created: bool
    record: SharedMemoryRecord


class SharedMigrationIssue(_Frozen):
    kind: str
    original_record_id: str | None = None
    code: Literal["invalid_source", "invalid_database", "source_conflict", "shared_storage_unavailable"]


class SharedMigrationSource(_Frozen):
    kind: Kind
    original_record_id: Identifier
    shared_record_id: Identifier
    original_sha256: str
    original_row_sha256: str


class SharedMigrationReport(_Frozen):
    success: bool
    selected_count: int
    created_count: int
    existing_count: int
    excluded_count: int
    counts_by_kind: dict[str, int]
    sources: tuple[SharedMigrationSource, ...]
    issues: tuple[SharedMigrationIssue, ...]
    source_readonly: Literal[True] = True
    partial_import: Literal[False] = False
    has_more: bool = False


class SharedMemoryContext(_Frozen):
    schema_version: Literal["shared-memory-context-v1"] = "shared-memory-context-v1"
    scope: SharedReadScope
    records: tuple[SharedMemoryRecord, ...]
    visible_count: int
    matched_count: int
    omitted_count: int
    query: str | None
    selection: Literal["relevance_then_recent", "recent"]
    authority: Literal["untrusted_source_attributed_memory"] = "untrusted_source_attributed_memory"

    def model_payload(self, *, max_bytes: int = 16000) -> dict:
        """Select whole records within a UTF-8 budget; never truncate quotations."""
        if type(max_bytes) is not int or not 256 <= max_bytes <= 1048576:
            raise ValueError("model payload budget must be an integer from 256 to 1048576")
        payload = {
            "schema_version": self.schema_version, "authority": self.authority,
            "coverage": "selected_visible_records_only", "entries": [],
            "visible_count": self.visible_count, "matched_count": self.matched_count,
            "selected_count": 0, "omitted_count": self.matched_count,
            "selection": self.selection,
        }
        if len(_json(payload).encode("utf-8")) > max_bytes:
            raise ValueError("model payload budget cannot hold coverage metadata")
        for record in self.records:
            candidate = {**payload, "entries": [*payload["entries"], record.model_projection()],
                         "selected_count": payload["selected_count"] + 1,
                         "omitted_count": self.matched_count - payload["selected_count"] - 1}
            if len(_json(candidate).encode("utf-8")) <= max_bytes:
                payload = candidate
        return payload


class SharedMemoryConflict(ValueError):
    """An immutable source or its visibility was supplied differently."""


class SharedMemoryStorageError(RuntimeError):
    """Persisted source, projection, or indexed scope is invalid."""


def _record_id(owner_id: str, kind: str, source_id: str) -> str:
    return "shared-memory:" + _hash(_json([owner_id, kind, source_id]))


def _validate_visibility(channel, session, visibility):
    if channel == "qq_group" and any(value.startswith("qq:") and value != session for value in visibility):
        raise ValueError("QQ originals cannot be exposed to another QQ group")
    if channel in {"private_board", "private_diary", "body"} and set(visibility) != {"local:main"}:
        raise ValueError("private board, diary and body originals are visible only to local:main")
    if channel == "qq_private" and (not session.startswith("qq:private:")
            or any(value not in {"local:main", session} for value in visibility)):
        raise ValueError("QQ private originals are visible only to their private peer and local:main")


def _qq_projection_scope(group_id: str) -> dict:
    scope = conversation_scope(group_id)
    return {"channel": "qq_private" if scope["kind"] == "private" else "qq_group",
            "session_id": conversation_audience(group_id)}


def _project_original(kind: Kind, value: dict) -> dict:
    """Derive every model-visible statement/identity from a typed original."""
    if kind == "action_outcome":
        event = SharedActionOutcome.model_validate(value)
        result = "通过" if event.evidence.success else "未通过"
        return dict(channel="body", session_id=event.session_id,
                    author_principal="character:" + event.owner_id, display_name=None, kind=kind,
                    authority="verified_action_outcome",
                    text=f"身体能力 {event.intent_id} 的独立核验结果：{result}。",
                    occurred_at=event.evidence.observed_at.isoformat(),
                    evidence_refs=list(dict.fromkeys(event.evidence.evidence_refs)),
                    profile_record_id=None, claims=[])
    if kind == "board_entry":
        from harness.embodiment.board_contracts import BoardEntry
        entry = BoardEntry.model_validate(value)
        receipt = entry.receipt
        if receipt.scope.owner_id != "misty" or _hash(entry.text) != receipt.text_sha256:
            raise ValueError("board original requires its owner's matching text digest")
        return dict(channel="private_board", session_id="board:" + _hash(_json(receipt.scope.model_dump(mode="json"))),
                    author_principal="character:misty", display_name="Misty", kind=kind,
                    authority="verified_delivery", text=entry.text, occurred_at=receipt.created_at.isoformat(),
                    evidence_refs=[receipt.evidence_ref], profile_record_id=None, claims=[])
    if kind == "diary_entry":
        from harness.diary.models import DiaryOriginal
        entry = DiaryOriginal.model_validate(value)
        return dict(channel="private_diary", session_id="diary:self",
                    author_principal="character:misty", display_name="Misty", kind=kind,
                    authority="verified_delivery", text=entry.text, occurred_at=entry.written_at.isoformat(),
                    evidence_refs=[entry.body_hash], profile_record_id=None, claims=[])
    if kind.startswith("local_"):
        event = SharedLocalEvent.model_validate(value)
        if kind != ("local_incoming" if event.role == "user" else "local_outgoing"):
            raise ValueError("local source role disagrees with source kind")
        return dict(channel="local_chat", session_id=event.session_id, author_principal=event.author_principal,
                    display_name=event.display_name, kind=kind, authority="speaker_utterance", text=event.text,
                    occurred_at=event.occurred_at.isoformat(), evidence_refs=list(dict.fromkeys(event.evidence_refs)),
                    profile_record_id=None, claims=[])
    if kind == "qq_profile":
        item = QQVerifiedProfile.model_validate(value)
        return dict(**_qq_projection_scope(item.group_id), author_principal=f"qq:member:{item.member_id}",
                    display_name=item.display_name, kind=kind, authority="verified_profile_observation",
                    text=_json({"observed_display_name": item.display_name, "display_name_source": item.display_name_source}),
                    occurred_at=item.observed_at.isoformat(), evidence_refs=list(dict.fromkeys([*item.group_evidence, *item.member_evidence])),
                    profile_record_id=item.record_id, claims=[])
    if kind == "qq_observation":
        item = QQVisualObservation.model_validate(value)
        return dict(**_qq_projection_scope(item.group_id), author_principal=f"qq:member:{item.member_id}",
                    display_name=None, kind=kind, authority="speaker_utterance", text=item.text,
                    occurred_at=item.observed_at.isoformat(), evidence_refs=list(dict.fromkeys([*item.evidence, *item.attribution_evidence])),
                    profile_record_id=item.profile_record_id, claims=[claim.model_dump(mode="json") for claim in item.claims],
                    image_analysis=[analysis.model_dump(mode="json") for analysis in item.image_analysis])
    from harness.qq.decision import QQDeliveryReceipt, QQSegmentDeliveryReceipt
    model = QQDeliveryReceipt if kind == "qq_delivery" else QQSegmentDeliveryReceipt
    item = model.model_validate(value)
    return dict(**_qq_projection_scope(item.group_id), author_principal=f"qq:member:{item.self_member_id}",
                display_name=None, kind=kind, authority="verified_delivery", text=item.delivered_text,
                occurred_at=item.delivered_at.isoformat(), evidence_refs=list(dict.fromkeys(item.evidence_refs)),
                profile_record_id=None, claims=[])


class SharedMemoryStore:
    def __init__(self, path: Path | str, *, owner_id: str = "misty", preset_id: str = "misty"):
        if str(path) == ":memory:":
            raise ValueError("shared memory requires a persistent database path")
        self.path = Path(path)
        self.owner_id = SharedReadScope(owner_id=owner_id, audience="local:main").owner_id
        self.preset_id = SharedReadScope(owner_id=preset_id, audience="local:main").owner_id

    def _connect(self, *, write: bool) -> sqlite3.Connection:
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5)
            # Readers must not be blocked by concurrent archive/queue commits.
            # Keep SQLite's default FULL durability and existing busy timeout.
            try:
                if connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    connection.execute("PRAGMA journal_mode=WAL")
            except Exception:
                connection.close()
                raise
        else:
            connection = sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        if write:
            connection.execute("""CREATE TABLE IF NOT EXISTS shared_memory_entries (
                record_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, source_kind TEXT NOT NULL,
                source_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, channel TEXT NOT NULL,
                session_id TEXT NOT NULL, author_principal TEXT NOT NULL, record_json TEXT NOT NULL,
                UNIQUE(owner_id, source_kind, source_id))""")
            connection.execute("CREATE INDEX IF NOT EXISTS shared_memory_scope ON shared_memory_entries(owner_id,channel,session_id,source_kind,author_principal)")
            connection.execute("CREATE TABLE IF NOT EXISTS shared_memory_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), preset_id TEXT NOT NULL)")
            # An unlabelled pre-upgrade database belongs to the historical Misty
            # preset. It must never be relabelled as another character's history.
            existing = connection.execute("SELECT 1 FROM shared_memory_entries LIMIT 1").fetchone()
            connection.execute("INSERT OR IGNORE INTO shared_memory_identity VALUES (1, ?)",
                               ("misty" if existing else self.preset_id,))
            self._check_preset(connection)
            has_index = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_memory_source_index'").fetchone()
            connection.execute("""CREATE TABLE IF NOT EXISTS shared_memory_source_index (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL UNIQUE,
                owner_id TEXT NOT NULL, preset_id TEXT NOT NULL, occurred_at TEXT NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS shared_memory_source_window ON shared_memory_source_index(owner_id,preset_id,occurred_at,sequence)")
            connection.execute("""CREATE TABLE IF NOT EXISTS shared_memory_archive_coverage (
                owner_id TEXT NOT NULL, preset_id TEXT NOT NULL, kind TEXT NOT NULL,
                started_at TEXT NOT NULL, PRIMARY KEY(owner_id,preset_id,kind))""")
            if not has_index:
                for row in connection.execute("SELECT * FROM shared_memory_entries ORDER BY rowid"):
                    self._index_source(connection, self._decode(row))
            connection.commit()
        else:
            connection.execute("PRAGMA query_only=ON")
            self._check_preset(connection)
        return connection

    def _check_preset(self, connection):
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_memory_identity'").fetchone()
        identity = connection.execute("SELECT preset_id FROM shared_memory_identity WHERE singleton=1").fetchone() if exists else None
        if (identity[0] if identity else "misty") != self.preset_id:
            connection.close()
            raise SharedMemoryStorageError("shared database belongs to a different preset")

    def _index_source(self, connection, record):
        connection.execute("INSERT OR IGNORE INTO shared_memory_source_index(record_id,owner_id,preset_id,occurred_at) VALUES (?,?,?,?)",
                           (record.record_id, record.owner_id, self.preset_id,
                            record.occurred_at.astimezone(timezone.utc).isoformat()))

    def _prepare(self, kind: Kind, source_id: str, original: BaseModel, visibility: tuple[str, ...], *, original_row_json: str | None = None) -> SharedMemoryRecord:
        # Database migration retains the original field set, including the
        # absence of newer DTO defaults. The exact row bytes get a second hash.
        raw = _json(json.loads(original_row_json)) if original_row_json is not None else _json(original.model_dump(mode="json"))
        projection = _project_original(kind, json.loads(raw))
        source = SharedSource(kind=kind, original_record_id=source_id, original_sha256=_hash(raw), original_json=raw,
                              original_row_sha256=_hash(original_row_json) if original_row_json is not None else None)
        return SharedMemoryRecord(record_id=_record_id(self.owner_id, kind, source_id), owner_id=self.owner_id,
                                  stored_at=_now(), source=source, visibility=tuple(sorted(set(visibility))), **projection)

    @staticmethod
    def _decode(row) -> SharedMemoryRecord:
        try:
            record = SharedMemoryRecord.model_validate_json(row["record_json"])
            expected = (record.record_id, record.owner_id, record.kind, record.source.original_record_id,
                        record.source.original_sha256, record.channel, record.session_id, record.author_principal)
            actual = tuple(row[key] for key in ("record_id", "owner_id", "source_kind", "source_id", "source_sha256", "channel", "session_id", "author_principal"))
            if expected != actual:
                raise ValueError("index and source scope disagree")
            return record
        except (ValueError, TypeError, KeyError) as exc:
            raise SharedMemoryStorageError("persisted shared memory binding is invalid") from exc

    def _write(self, records: list[SharedMemoryRecord], *, cursors=(), checkpoint=None) -> tuple[SharedWriteResult, ...]:
        results = []
        with closing(self._connect(write=True)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if checkpoint is not None:
                stream, previous, current = checkpoint
                connection.execute("CREATE TABLE IF NOT EXISTS shared_qq_checkpoints (stream TEXT PRIMARY KEY, state_json TEXT NOT NULL)")
                saved = connection.execute("SELECT state_json FROM shared_qq_checkpoints WHERE stream=?", (stream,)).fetchone()
                if (saved[0] if saved else None) != previous:
                    raise SharedMemoryConflict("QQ import checkpoint advanced concurrently; retry")
            for record in records:
                if record.owner_id != self.owner_id:
                    raise SharedMemoryConflict("source belongs to a different owner or preset")
                row = connection.execute("SELECT * FROM shared_memory_entries WHERE record_id=?", (record.record_id,)).fetchone()
                if row is not None:
                    previous = self._decode(row)
                    if previous.model_dump(exclude={"stored_at"}) != record.model_dump(exclude={"stored_at"}):
                        raise SharedMemoryConflict("source identity or visibility already has different immutable content")
                    results.append(SharedWriteResult(created=False, record=previous))
                else:
                    connection.execute("INSERT INTO shared_memory_entries VALUES (?,?,?,?,?,?,?,?,?)",
                                       (record.record_id, record.owner_id, record.kind, record.source.original_record_id,
                                        record.source.original_sha256, record.channel, record.session_id, record.author_principal, record.model_dump_json()))
                    self._index_source(connection, record)
                    results.append(SharedWriteResult(created=True, record=record))
            for stream, table, cursor in cursors:
                connection.execute("""INSERT INTO shared_memory_import_cursors VALUES(?,?,?)
                    ON CONFLICT(stream,source_table) DO UPDATE SET cursor=MAX(cursor,excluded.cursor)""", (stream, table, cursor))
            if checkpoint is not None:
                connection.execute("""INSERT INTO shared_qq_checkpoints VALUES(?,?)
                    ON CONFLICT(stream) DO UPDATE SET state_json=excluded.state_json""", (checkpoint[0], checkpoint[2]))
        return tuple(results)

    def record_local(self, event: SharedLocalEvent) -> SharedWriteResult:
        return self.record_local_batch([event])[0]

    def record_local_batch(self, events: list[SharedLocalEvent]) -> tuple[SharedWriteResult, ...]:
        """Validate every source before committing one immutable-source batch."""
        records = []
        for event in events:
            event = SharedLocalEvent.model_validate_json(event.model_dump_json())
            kind = "local_incoming" if event.role == "user" else "local_outgoing"
            records.append(self._prepare(kind, event.event_id, event, event.visibility))
        return self._write(records) if records else ()

    def record_action(self, event: SharedActionOutcome) -> SharedWriteResult:
        event = SharedActionOutcome.model_validate_json(event.model_dump_json())
        return self._write([self._prepare("action_outcome", event.event_id, event, ("local:main",))])[0]

    def mark_archive_started(self, kind: Literal["local_chat", "action_outcome"], *, at: datetime | None = None) -> None:
        moment = at or _now()
        if moment.utcoffset() is None:
            raise ValueError("archive boundary requires an aware time")
        with closing(self._connect(write=True)) as connection, connection:
            connection.execute("INSERT OR IGNORE INTO shared_memory_archive_coverage VALUES (?,?,?,?)",
                               (self.owner_id, self.preset_id, kind, moment.astimezone(timezone.utc).isoformat()))

    def freeze_window(self, start: datetime, end: datetime, *, watermark: int | None = None) -> SharedWindow:
        """Freeze an immutable insertion prefix; later arrivals require a new revision."""
        window = SharedWindow(owner_id=self.owner_id, preset_id=self.preset_id, start=start, end=end, watermark=0)
        with closing(self._connect(write=True)) as connection, connection:
            connection.execute("BEGIN")
            maximum = connection.execute("SELECT COALESCE(MAX(sequence),0) FROM shared_memory_source_index WHERE owner_id=? AND preset_id=?",
                                         (self.owner_id, self.preset_id)).fetchone()[0]
            if watermark is not None and (type(watermark) is not int or not 0 <= watermark <= maximum):
                raise ValueError("source watermark is outside this owner and preset snapshot")
            coverage = dict(connection.execute("SELECT kind,started_at FROM shared_memory_archive_coverage WHERE owner_id=? AND preset_id=?",
                                               (self.owner_id, self.preset_id)))
        gaps = tuple(kind + "_history_before_continuous_archive" for kind in ("local_chat", "action_outcome")
                     if kind not in coverage or start < datetime.fromisoformat(coverage[kind]))
        return window.model_copy(update={"watermark": maximum if watermark is None else watermark, "coverage_gaps": gaps})

    def read_window(self, window: SharedWindow, *, cursor: int = 0, limit: int = 200) -> SharedSourcePage:
        window = SharedWindow.model_validate(window.model_dump())
        if (window.owner_id, window.preset_id) != (self.owner_id, self.preset_id):
            raise ValueError("source window belongs to another owner or preset")
        if type(cursor) is not int or not 0 <= cursor <= window.watermark or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid source pagination cursor or limit")
        with closing(self._connect(write=False)) as connection:
            rows = connection.execute("""SELECT e.*,i.sequence AS source_sequence,i.occurred_at AS indexed_time FROM shared_memory_source_index i
                JOIN shared_memory_entries e ON e.record_id=i.record_id
                WHERE i.owner_id=? AND i.preset_id=? AND i.sequence>? AND i.sequence<=?
                  AND i.occurred_at>=? AND i.occurred_at<? ORDER BY i.sequence LIMIT ?""",
                (self.owner_id, self.preset_id, cursor, window.watermark,
                 window.start.astimezone(timezone.utc).isoformat(), window.end.astimezone(timezone.utc).isoformat(), limit + 1)).fetchall()
        selected = rows[:limit]
        records = []
        for row in selected:
            record = self._decode(row)
            if (record.owner_id != self.owner_id
                    or record.occurred_at.astimezone(timezone.utc).isoformat() != row["indexed_time"]):
                raise SharedMemoryStorageError("source time index disagrees with its original")
            records.append(record)
        exhausted = len(rows) <= limit
        return SharedSourcePage(records=tuple(records), next_cursor=window.watermark if exhausted else selected[-1]["source_sequence"], exhausted=exhausted)

    def read_after(self, *, cursor: int = 0, limit: int = 256) -> SharedSourcePage:
        """Read an insertion-ordered page without rescanning an entire archive."""
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid incremental source cursor or limit")
        if not self.path.is_file():
            return SharedSourcePage(records=(), next_cursor=cursor, exhausted=True)
        with closing(self._connect(write=False)) as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_memory_source_index'").fetchone()
            if not exists:
                # Old archives need explicit initialization; do not hide missing history.
                raise SharedMemoryStorageError("incremental source index requires migration")
            rows = connection.execute("""SELECT e.*,i.sequence AS source_sequence FROM shared_memory_source_index i
                JOIN shared_memory_entries e ON e.record_id=i.record_id
                WHERE i.owner_id=? AND i.preset_id=? AND i.sequence>? ORDER BY i.sequence LIMIT ?""",
                (self.owner_id, self.preset_id, cursor, limit + 1)).fetchall()
        selected = rows[:limit]
        return SharedSourcePage(records=tuple(self._decode(row) for row in selected),
            next_cursor=selected[-1]["source_sequence"] if selected else cursor, exhausted=len(rows) <= limit)

    def import_board_entry(self, entry) -> SharedWriteResult:
        """Project a body-read verified receipt; callers must verify its source first."""
        from harness.embodiment.board_contracts import BoardEntry
        entry = BoardEntry.model_validate_json(entry.model_dump_json())
        return self._write([self._prepare("board_entry", entry.receipt.entry_id, entry, ("local:main",))])[0]

    def import_diary_entry(self, entry) -> SharedWriteResult:
        from harness.diary.models import DiaryOriginal
        entry = DiaryOriginal.model_validate_json(entry.model_dump_json())
        return self._write([self._prepare("diary_entry", entry.entry_id, entry, ("local:main",))])[0]

    def import_qq_context(self, context: QQGroupContext | QQPersonContext, *, visibility: tuple[str, ...] | None = None) -> tuple[SharedWriteResult, ...]:
        """Import a detached verified snapshot atomically, preserving all originals."""
        model = QQGroupContext if isinstance(context, QQGroupContext) else QQPersonContext
        context = model.model_validate_json(context.model_dump_json())
        people = context.members if isinstance(context, QQGroupContext) else [context]
        if isinstance(context, QQGroupContext) and set(context.participants) != {person.member_id for person in people}:
            raise ValueError("QQ group participants disagree with member contexts")
        records = []
        for person in people:
            if person.group_id != context.group_id:
                raise ValueError("QQ member context belongs to another group")
            visible = visibility if visibility is not None else ("local:main", conversation_audience(person.group_id))
            profiles = {profile.record_id: profile for profile in person.profiles}
            if len(profiles) != len(person.profiles):
                raise ValueError("duplicate QQ profile identity")
            for profile in person.profiles:
                if (profile.group_id, profile.member_id) != (person.group_id, person.member_id):
                    raise ValueError("QQ profile identity is outside its context")
                records.append(self._prepare("qq_profile", profile.record_id, profile, visible))
            for observation in person.observations:
                if ((observation.group_id, observation.member_id) != (person.group_id, person.member_id)
                        or observation.profile_record_id not in profiles
                        or profiles[observation.profile_record_id].verification != observation.author_attribution):
                    raise ValueError("QQ observation requires its exact original profile snapshot")
                records.append(self._prepare("qq_observation", observation.record_id, observation, visible))
        return self._write(records)

    def import_qq_delivery(self, receipt, *, visibility: tuple[str, ...] | None = None) -> SharedWriteResult:
        from harness.qq.decision import QQDeliveryReceipt, QQSegmentDeliveryReceipt
        if isinstance(receipt, QQSegmentDeliveryReceipt):
            model, kind = QQSegmentDeliveryReceipt, "qq_segment_delivery"
        elif isinstance(receipt, QQDeliveryReceipt):
            model, kind = QQDeliveryReceipt, "qq_delivery"
        else:
            raise ValueError("a typed verified QQ receipt is required")
        receipt = model.model_validate_json(receipt.model_dump_json())
        visible = visibility if visibility is not None else ("local:main", conversation_audience(receipt.group_id))
        return self._write([self._prepare(kind, receipt.receipt_id, receipt, visible)])[0]

    def import_qq_database(self, path: Path | str, *, allowed_group_ids: tuple[str, ...] | list[str],
                           self_member_id: str,
                           allowed_private_peer_ids: tuple[str, ...] | list[str] = (),
                           incremental: bool = False, batch_size: int = 256,
                           track_changes: bool = False) -> SharedMigrationReport:
        """Mirror ALL selected historical originals, not a recent query window.

        The source is one read-only SQLite snapshot. Invalid selected rows abort
        the entire import; excluded groups/accounts and exact source hashes are
        reported separately. Nothing is submitted as a new QQ observation.

        track_changes reuses a guarded, transactionally committed append prefix;
        its report describes only rows visited in this call. First use, source
        replacement or mutation audits the full archive. Legacy sources without
        writer-installed guards retain the full read-only validation path.
        """
        from harness.qq.decision import (
            QQDeliveryReceipt, QQSegmentDeliveryReceipt, _check_aggregate, _check_sources,
            _checked_receipt, _frozen_decision, _read_segment_receipts,
        )
        official = isinstance(self_member_id, str) and self_member_id.startswith("qqbot:")
        if official:
            if (not isinstance(allowed_group_ids, (tuple, list)) or len(allowed_group_ids) > 100
                    or not isinstance(allowed_private_peer_ids, (tuple, list)) or len(allowed_private_peer_ids) > 100
                    or not allowed_group_ids and not allowed_private_peer_ids):
                raise ValueError("explicit official conversation scopes are required")
            app_id = self_member_id.split(":")[1]
            if split_identity(self_member_id, kind="bot", app_id=app_id) != app_id:
                raise ValueError("official bot account does not match its application")
            for group in allowed_group_ids:
                split_identity(group, kind="group", app_id=app_id)
            for peer in allowed_private_peer_ids:
                split_identity(peer, kind="user", app_id=app_id)
        elif (not isinstance(allowed_group_ids, (tuple, list))
                or len(allowed_group_ids) > 100 or any(not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value) for value in allowed_group_ids)
                or not isinstance(self_member_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", self_member_id)):
            raise ValueError("explicit numeric group and self-account scopes are required")
        if not official and (not isinstance(allowed_private_peer_ids, (tuple, list)) or len(allowed_private_peer_ids) > 100
                or any(not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value)
                       or value == self_member_id for value in allowed_private_peer_ids)
                or not allowed_group_ids and not allowed_private_peer_ids):
            raise ValueError("explicit numeric group or private peer scopes are required")
        source_path = Path(path)
        if incremental and track_changes:
            raise ValueError("choose bounded backfill or transactional change tracking")
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError("invalid QQ ingestion batch size")
        if source_path.absolute() == self.path.absolute() or (source_path.exists() and self.path.exists() and source_path.samefile(self.path)):
            raise ValueError("the mirror must not write into the original QQ database")
        kinds = {"qq_visual_profiles": "qq_profile", "qq_visual_observations": "qq_observation",
                 "qq_reply_deliveries": "qq_delivery", "qq_reply_segment_deliveries": "qq_segment_delivery"}
        records, issues, excluded = [], [], 0
        counts = {kind: 0 for kind in kinds.values()}
        allowed = {*allowed_group_ids,
                   *(private_scope_id(self_member_id, peer) for peer in allowed_private_peer_ids)}
        stream = _hash(_json([str(source_path.absolute()), sorted(allowed), self_member_id]))
        cursors, next_cursors, has_more = {}, [], False
        checkpoint, previous = None, None
        if incremental:
            with closing(self._connect(write=True)) as target:
                target.execute("CREATE TABLE IF NOT EXISTS shared_memory_import_cursors (stream TEXT, source_table TEXT, cursor INTEGER, PRIMARY KEY(stream,source_table))")
                target.commit()
                cursors = dict(target.execute("SELECT source_table,cursor FROM shared_memory_import_cursors WHERE stream=?", (stream,)))
        if track_changes and self.path.is_file():
            with closing(self._connect(write=False)) as target:
                if target.execute("SELECT 1 FROM sqlite_master WHERE name='shared_qq_checkpoints'").fetchone():
                    row = target.execute("SELECT state_json FROM shared_qq_checkpoints WHERE stream=?", (stream,)).fetchone()
                    previous = row[0] if row else None
        try:
            if not source_path.is_file() or source_path.is_symlink():
                raise ValueError("original database is unavailable")
            with closing(sqlite3.connect(source_path.absolute().as_uri()+"?mode=ro", uri=True, timeout=5)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"qq_visual_profiles", "qq_visual_observations"}.issubset(tables):
                    raise ValueError("original QQ tables are missing")
                if track_changes:
                    from harness.qq.source_index import append_cursors, read_source_index
                    state = read_source_index(connection, tables)
                    if state is not None:
                        stat = source_path.stat()
                        state['file_identity'] = [stat.st_dev, stat.st_ino]
                        cursors = append_cursors(previous, state)
                        checkpoint = (stream, previous, _json(state))
                profiles = {}
                for table, kind in kinds.items():
                    if table not in tables:
                        continue
                    if checkpoint is not None and cursors:
                        rows = connection.execute(f"SELECT * FROM {table} WHERE rowid>? AND rowid<=? ORDER BY rowid",
                            (cursors.get(table, 0), state['watermarks'][table]))
                    elif incremental:
                        batch = connection.execute(f"SELECT rowid AS source_cursor,* FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?",
                            (cursors.get(table, 0), batch_size + 1)).fetchall()
                        has_more = has_more or len(batch) > batch_size
                        rows = batch[:batch_size]
                        if rows:
                            next_cursors.append((stream, table, rows[-1]["source_cursor"]))
                    else:
                        rows = connection.execute(f"SELECT * FROM {table}")
                    for row in rows:
                        source_id = row["record_id" if kind in {"qq_profile", "qq_observation"} else "receipt_id"]
                        if row["group_id"] not in allowed:
                            excluded += 1
                            continue
                        counts[kind] += 1
                        try:
                            if kind == "qq_profile":
                                item = QQVerifiedProfile.model_validate_json(row["record_json"])
                                if (item.record_id, item.group_id, item.member_id, item.capture_id, item.observed_at) != (
                                    row["record_id"], row["group_id"], row["member_id"], row["capture_id"], datetime.fromisoformat(row["observed_at"])):
                                    raise ValueError("profile index mismatch")
                                profiles[item.record_id] = item
                            elif kind == "qq_observation":
                                item = QQVisualObservation.model_validate_json(row["record_json"])
                                if (item.record_id, item.group_id, item.member_id, item.profile_record_id, item.capture_id, item.capture_item_id, item.observed_at) != (
                                    row["record_id"], row["group_id"], row["member_id"], row["profile_record_id"], row["capture_id"], row["capture_item_id"], datetime.fromisoformat(row["observed_at"])):
                                    raise ValueError("observation index mismatch")
                                profile = profiles.get(item.profile_record_id)
                                if profile is None and (incremental or checkpoint is not None):
                                    profile_row = connection.execute("SELECT * FROM qq_visual_profiles WHERE record_id=?", (item.profile_record_id,)).fetchone()
                                    if profile_row is not None:
                                        profile = QQVerifiedProfile.model_validate_json(profile_row["record_json"])
                                        if (profile.record_id, profile.group_id, profile.member_id, profile.capture_id, profile.observed_at) != (
                                                profile_row["record_id"], profile_row["group_id"], profile_row["member_id"], profile_row["capture_id"], datetime.fromisoformat(profile_row["observed_at"])):
                                            raise ValueError("profile index mismatch")
                                if (profile is None or (profile.group_id, profile.member_id) != (item.group_id, item.member_id)
                                        or profile.verification != item.author_attribution):
                                    raise ValueError("observation profile mismatch")
                            else:
                                model = QQDeliveryReceipt if kind == "qq_delivery" else QQSegmentDeliveryReceipt
                                item = _checked_receipt(row, model)
                                if item.self_member_id != self_member_id:
                                    excluded += 1
                                    counts[kind] -= 1
                                    continue
                                frozen = _frozen_decision(connection, item.group_id, item.decision_id) if item.decision_id is not None else None
                                _check_sources(connection, group_id=item.group_id, sources=item.source_record_ids, decision=frozen)
                                if frozen is not None and frozen.self_member_id not in {None, self_member_id}:
                                    raise ValueError("receipt self identity mismatch")
                                if kind == "qq_segment_delivery":
                                    prefix = _read_segment_receipts(connection, group_id=item.group_id, decision=frozen)
                                    if item not in prefix:
                                        raise ValueError("segment receipt is outside the validated prefix")
                                elif item.delivered_segments:
                                    _check_aggregate(item, _read_segment_receipts(connection, group_id=item.group_id, decision=frozen), frozen)
                                elif frozen is not None and item.delivered_text != frozen.reply:
                                    raise ValueError("receipt does not match frozen reply")
                            records.append(self._prepare(kind, source_id, item,
                                ("local:main", conversation_audience(item.group_id)), original_row_json=row["record_json"]))
                        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                            issues.append(SharedMigrationIssue(kind=kind, original_record_id=source_id, code="invalid_source"))
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError):
            issues.append(SharedMigrationIssue(kind="database", code="invalid_database"))
        writes = ()
        if not issues and (records or next_cursors or checkpoint is not None and checkpoint[1] != checkpoint[2]):
            try:
                writes = self._write(records, cursors=next_cursors, checkpoint=checkpoint)
            except SharedMemoryConflict:
                issues.append(SharedMigrationIssue(kind="database", code="source_conflict"))
            except (sqlite3.Error, OSError, SharedMemoryStorageError):
                issues.append(SharedMigrationIssue(kind="database", code="shared_storage_unavailable"))
        return SharedMigrationReport(success=not issues, selected_count=sum(counts.values()),
            created_count=sum(item.created for item in writes), existing_count=sum(not item.created for item in writes),
            excluded_count=excluded, counts_by_kind=counts,
            sources=tuple(SharedMigrationSource(kind=item.kind, original_record_id=item.source.original_record_id,
                shared_record_id=item.record_id, original_sha256=item.source.original_sha256,
                original_row_sha256=item.source.original_row_sha256) for item in records), issues=tuple(issues), has_more=has_more)

    def read_exact(self, scope: SharedReadScope, record_ids: list[str]) -> tuple[SharedMemoryRecord, ...]:
        """Read only named records through the same immutable-source/scope guard."""
        from harness.qq.attention import attention_visible
        scope = SharedReadScope.model_validate_json(scope.model_dump_json())
        if scope.owner_id != self.owner_id or len(record_ids) > 256 or len(set(record_ids)) != len(record_ids):
            raise ValueError("invalid exact shared-memory read")
        if any(not isinstance(item, str) or not item for item in record_ids):
            raise ValueError("invalid exact shared-memory identifier")
        records = []
        if not record_ids or not self.path.is_file():
            return ()
        with closing(self._connect(write=False)) as connection, connection:
            connection.execute("BEGIN")
            for record_id in record_ids:
                row = connection.execute("SELECT * FROM shared_memory_entries WHERE record_id=? AND owner_id=?",
                                         (record_id, self.owner_id)).fetchone()
                if row is None:
                    continue
                record = self._decode(row)
                if record.channel == "qq_group" and not attention_visible(record.session_id):
                    continue
                if (scope.audience not in record.visibility
                        or scope.channels is not None and record.channel not in scope.channels
                        or scope.session_ids is not None and record.session_id not in scope.session_ids
                        or scope.principals is not None and record.author_principal not in scope.principals
                        or scope.kinds is not None and record.kind not in scope.kinds):
                    continue
                records.append(record)
        return tuple(records)

    def operator_recent(self, *, limit: int = 50) -> SharedMemoryContext:
        """Bounded local UI projection; model retrieval still uses query().

        Scan ordering/visibility metadata in SQLite, then fully validate the
        selected originals in the same read transaction. Archive counts are
        metadata counts, not an assertion that every old body was audited.
        """
        from harness.qq.attention import attention_policy_token, attention_visible
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("query limit must be an integer from 1 to 100")
        policy = attention_policy_token()
        selected, count = (), 0
        if self.path.is_file():
            with closing(self._connect(write=False)) as db, db:
                db.execute("BEGIN")
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_memory_entries'").fetchone()
                if exists:
                    # Old text, original JSON and image data stay in SQLite.
                    # Python datetime preserves timezone and microsecond order.
                    rows = db.execute("""SELECT record_id,channel,session_id,
                        json_extract(record_json,'$.occurred_at') AS occurred_at,
                        json_extract(record_json,'$.stored_at') AS stored_at
                        FROM shared_memory_entries WHERE owner_id=? AND EXISTS(
                            SELECT 1 FROM json_each(record_json,'$.visibility') WHERE value='local:main')""",
                        (self.owner_id,))
                    groups = {}

                    def metadata():
                        nonlocal count
                        for row in rows:
                            if row["channel"] == "qq_group":
                                group = row["session_id"]
                                if group not in groups:
                                    groups[group] = attention_visible(group)
                                if not groups[group]:
                                    continue
                            count += 1
                            yield (datetime.fromisoformat(row["occurred_at"]),
                                   datetime.fromisoformat(row["stored_at"]), row["record_id"])

                    try:
                        recent = heapq.nlargest(limit, metadata())
                        selected = tuple(self._decode(db.execute(
                            "SELECT * FROM shared_memory_entries WHERE record_id=?", (item[2],)).fetchone())
                            for item in recent)
                    except (TypeError, ValueError) as exc:
                        raise SharedMemoryStorageError("persisted shared UI metadata is invalid") from exc
                    if any(r.owner_id != self.owner_id or "local:main" not in r.visibility for r in selected):
                        raise SharedMemoryStorageError("shared UI scope disagrees with original")
        if attention_policy_token() != policy:
            raise ValueError("attention_policy_changed")
        return SharedMemoryContext(scope=SharedReadScope(owner_id=self.owner_id, audience="local:main"),
            records=selected, visible_count=count, matched_count=count, omitted_count=count-len(selected),
            query=None, selection="recent")

    @measure_stage("memory.shared_query")
    def query(self, scope: SharedReadScope, *, text: str | None = None, limit: int = 20) -> SharedMemoryContext:
        from harness.qq.attention import attention_visible
        scope = SharedReadScope.model_validate_json(scope.model_dump_json())
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("query limit must be an integer from 1 to 100")
        if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 512):
            raise ValueError("query text must be nonblank and at most 512 characters")
        if scope.owner_id != self.owner_id:
            raise ValueError("read scope belongs to another owner")
        records, visible_count, matched_count = [], 0, 0
        normalized = unicodedata.normalize("NFKC", text).casefold() if text is not None else None
        terms_list = []
        for term in re.findall(r"[\w]+", normalized or ""):
            terms_list.append(term)
            # Chinese has no whitespace boundaries. Deterministic bigrams
            # make a conversational question retrieve its topic without an
            # extra model call or weakening the author/audience filter.
            for run in re.findall(r"[\u3400-\u9fff]{3,}", term):
                terms_list.extend(run[index:index+2] for index in range(len(run)-1))
        terms = tuple(dict.fromkeys(terms_list))
        if normalized and not terms:
            terms = (normalized,)
        if self.path.is_file():
            with closing(self._connect(write=False)) as connection, connection:
                connection.execute("BEGIN")
                exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_memory_entries'").fetchone()
                # Reject nonmatching indexed scope dimensions before decoding
                # large immutable source JSON. Every admitted row still passes
                # the original integrity and visibility checks below.
                filters, parameters = ["owner_id=?"], [self.owner_id]
                for column, values in (("channel", scope.channels), ("session_id", scope.session_ids),
                                       ("author_principal", scope.principals), ("source_kind", scope.kinds)):
                    if values is not None:
                        filters.append(column + " IN (" + ",".join("?" for _ in values) + ")" if values else "0")
                        parameters.extend(values)
                sql = "SELECT * FROM shared_memory_entries WHERE " + " AND ".join(filters)
                rows = connection.execute(sql, parameters) if exists else ()
                for row in rows:
                    record = self._decode(row)
                    if record.channel == "qq_group" and not attention_visible(record.session_id):
                        continue
                    if (scope.audience not in record.visibility
                            or scope.channels is not None and record.channel not in scope.channels
                            or scope.session_ids is not None and record.session_id not in scope.session_ids
                            or scope.principals is not None and record.author_principal not in scope.principals
                            or scope.kinds is not None and record.kind not in scope.kinds):
                        continue
                    visible_count += 1
                    searchable = unicodedata.normalize("NFKC", record.text).casefold()
                    score = sum(term in searchable for term in terms)
                    if terms and not score:
                        continue
                    matched_count += 1
                    records.append((score, record.occurred_at, record.stored_at, record.record_id, record))
                    records.sort(key=lambda item: item[:4], reverse=True)
                    del records[limit:]
        selected = tuple(item[-1] for item in records)
        return SharedMemoryContext(scope=scope, records=selected, visible_count=visible_count,
                                   matched_count=matched_count, omitted_count=matched_count-len(selected),
                                   query=text, selection="relevance_then_recent" if text is not None else "recent")
