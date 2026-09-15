"""Frozen daily inputs from authenticated originals, with shared QQ evidence identity.

The collector makes no model calls and never treats an earlier summary as a new
observation. Its watermark binds an insertion prefix independently of event time.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from harness.core.models import MemoryScope
from harness.memory.qq_assimilation import QQAssimilationQueue, QQSource
from harness.memory.shared import (
    SharedMemoryConflict, SharedMemoryRecord, SharedMemoryStore,
)
from harness.qq.conversation import conversation_scope


class FrozenSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    owner_id: str = Field(min_length=1)
    preset_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    occurred_at: AwareDatetime
    stored_at: AwareDatetime
    text: str
    author_principal: str = Field(min_length=1)
    scope: MemoryScope
    evidence_keys: tuple[str, ...] = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority: str = Field(min_length=1)
    relation_target: str | None = None
    self_generated: bool = False
    quoted: bool = False
    independent: bool = True

    @model_validator(mode="after")
    def bound_scope(self):
        if self.scope.owner_id != self.owner_id:
            raise ValueError("daily source scope belongs to another owner")
        if any(not value.strip() for value in (*self.evidence_keys, *self.source_refs)):
            raise ValueError("daily evidence identities must not be blank")
        if self.self_generated and self.independent:
            raise ValueError("self-generated text cannot independently support a judgment")
        return self


class SourceWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    owner_id: str
    preset_id: str
    start: AwareDatetime
    end: AwareDatetime
    watermark: int = Field(ge=0)
    records: tuple[FrozenSource, ...] = ()
    coverage_gaps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def records_belong_to_window(self):
        if self.start >= self.end:
            raise ValueError("daily source window must be a nonempty half-open interval")
        if len({source.source_id for source in self.records}) != len(self.records):
            raise ValueError("daily source window has duplicate source identifiers")
        if any((source.owner_id, source.preset_id) != (self.owner_id, self.preset_id)
               or not self.start <= source.occurred_at < self.end for source in self.records):
            raise ValueError("daily source is outside the frozen owner, preset or time window")
        return self


def _assigned_qq_sources(store: SharedMemoryStore, *, self_id: str, watermark: int,
                         groups: set[str]) -> tuple[QQSource, ...]:
    """Reuse online identities, including pre-window aliases, without opening a queue writer."""
    if not groups:
        return ()
    with closing(store._connect(write=False)) as connection:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='qq_memory_job_sources'").fetchone()
        if not exists:
            return ()
        # An assignment can have several processing versions. Their immutable
        # source and canonical identity must agree before a daily job may use it.
        rows = connection.execute("""SELECT s.frozen_json FROM qq_memory_job_sources s
            JOIN shared_memory_source_index i ON i.record_id=s.source_id
            WHERE s.owner_id=? AND s.preset_id=? AND s.self_id=? AND i.sequence<=?
            ORDER BY s.rowid""", (store.owner_id, store.preset_id, self_id, watermark))
        by_id: dict[str, QQSource] = {}
        for row in rows:
            source = QQSource.model_validate_json(row[0])
            if source.group_id not in groups:
                continue
            previous = by_id.get(source.source_id)
            if previous and (previous.source_sha256, previous.independent_key) != (source.source_sha256, source.independent_key):
                raise SharedMemoryConflict("online QQ versions disagree on a source evidence identity")
            if source.record.owner_id != store.owner_id or source.self_id != self_id:
                raise SharedMemoryConflict("online QQ source belongs to another owner or account")
            by_id[source.source_id] = source
    return tuple(sorted(by_id.values(), key=lambda item: (item.occurred_at, item.source_id)))


def _normalized_source(record: SharedMemoryRecord, *, preset_id: str,
                       qq_source: QQSource | None, self_member_id: str | None) -> FrozenSource:
    original = json.loads(record.source.original_json)
    scope = MemoryScope(owner_id=record.owner_id, channel=record.channel,
                        conversation_id=record.session_id, visibility=list(record.visibility))
    quoted = False
    independent = record.kind == "local_incoming"
    self_generated = record.kind in {"local_outgoing", "qq_delivery", "qq_segment_delivery", "board_entry", "diary_entry", "action_outcome"}
    relation_target = "user" if record.channel == "local_chat" else None
    evidence_key = ":".join((record.kind, record.owner_id, preset_id, record.source.original_record_id))
    if qq_source is not None:
        # Exactly the key used by the existing live QQ assimilation queue.
        evidence_key = qq_source.independent_key
        self_generated = qq_source.is_self
        quoted = qq_source.contains_quote
        independent = qq_source.independent_eligible
    if record.channel in {"qq_group", "qq_private"}:
        conversation = conversation_scope(original["group_id"])
        if conversation["kind"] == "private":
            relation_target = record.session_id
        else:
            member = original.get("member_id")
            relation_target = f"qq:{original['group_id']}:{member if member and member != self_member_id else 'group'}"
        if record.kind in {"qq_delivery", "qq_segment_delivery"}:
            # Aggregate and segment receipts describe one actual expression;
            # neither may count as new evidence of somebody else's behavior.
            evidence_key = ":".join(("qq-delivery", original["self_member_id"], original["group_id"],
                                      original.get("decision_id") or original["receipt_id"]))
    return FrozenSource(owner_id=record.owner_id, preset_id=preset_id,
        source_id=record.record_id, kind=record.kind, occurred_at=record.occurred_at,
        stored_at=record.stored_at, text=record.text, author_principal=record.author_principal,
        scope=scope, evidence_keys=(evidence_key,),
        source_refs=tuple(dict.fromkeys((record.source.original_record_id, *record.evidence_refs))),
        source_hash=record.source.original_sha256, authority=record.authority,
        relation_target=relation_target, self_generated=self_generated,
        quoted=quoted, independent=independent)


def collect_source_window(store: SharedMemoryStore, *, owner_id: str, preset_id: str,
                          start: datetime, end: datetime, self_member_id: str | None = None,
                          watermark: int | None = None, page_size: int = 200) -> SourceWindow:
    """Collect all available originals for a day; no recent-history truncation.

    Call the trusted sync adapters before freezing. A reused watermark excludes
    late imports even if their occurrence time is inside the requested day.
    """
    if (store.owner_id, store.preset_id) != (owner_id, preset_id):
        raise ValueError("daily collector requires the matching owner and preset archive")
    window = store.freeze_window(start, end, watermark=watermark)
    cursor = 0
    records: list[SharedMemoryRecord] = []
    while True:
        page = store.read_window(window, cursor=cursor, limit=page_size)
        records.extend(page.records)
        if page.exhausted:
            break
        cursor = page.next_cursor
    records.sort(key=lambda item: (item.occurred_at, item.record_id))
    qq_records = [record for record in records if record.kind == "qq_observation"]
    gaps = list(window.coverage_gaps)
    assignments: dict[str, QQSource] = {}
    previous: list[QQSource] = []
    if qq_records and self_member_id is not None:
        groups = {json.loads(record.source.original_json)["group_id"] for record in qq_records}
        previous = list(_assigned_qq_sources(store, self_id=self_member_id, watermark=window.watermark, groups=groups))
        assignments = {source.source_id: source for source in previous}
    elif qq_records:
        gaps.append("qq_observations_excluded_without_authenticated_self_account")
    frozen: list[FrozenSource] = []
    for record in records:
        source = None
        if record.kind == "qq_observation":
            if self_member_id is None:
                continue
            source = QQSource.from_record(record, self_id=self_member_id)
            if record.record_id in assignments:
                prior = assignments[record.record_id]
                if prior.source_sha256 != source.source_sha256:
                    raise SharedMemoryConflict("daily QQ original differs from online frozen source")
                source = prior
            else:
                source = QQAssimilationQueue._link_duplicates(source, previous)
                previous.append(source)
        frozen.append(_normalized_source(record, preset_id=preset_id, qq_source=source,
                                         self_member_id=self_member_id))
    return SourceWindow(owner_id=owner_id, preset_id=preset_id, start=start, end=end,
                        watermark=window.watermark, records=tuple(frozen), coverage_gaps=tuple(gaps))


def partition_sources(sources: tuple[FrozenSource, ...]) -> tuple[tuple[FrozenSource, ...], ...]:
    """Separate exact trusted visibility scopes before any model invocation."""
    partitions: dict[str, list[FrozenSource]] = {}
    for source in sources:
        key = json.dumps([source.owner_id, source.preset_id, source.scope.model_dump(mode="json")],
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        partitions.setdefault(key, []).append(source)
    return tuple(tuple(sorted(partitions[key], key=lambda item: (item.occurred_at, item.source_id)))
                 for key in sorted(partitions))
