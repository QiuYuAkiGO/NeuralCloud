"""Conservative visible-stream planning and durable QQ send reservations.

This module does not capture, infer identities, call models, or deliver messages.
Local event IDs identify ordered visual occurrences, never QQ message IDs.
Call ``start_run`` once per watcher process: its first screen is history only.
The caller must independently verify the configured group and every author.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from pydantic import model_validator

from harness.embodiment.capabilities import QQ_COMMUNICATION_READ, QQ_COMMUNICATION_SEND
from harness.qq.memory import QQID
from harness.qq.idle import QQIdleIntent, validated_idle_intent
from harness.qq.segment_ledger import SegmentLedgerMixin


class WatchLedgerError(ValueError):
    """An invalid scope or state transition; callers must not send on failure."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def no_blank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("blank identifiers or text are not allowed")
        return value


class VisibleItem(_Model):
    direction: Literal["incoming", "outgoing"]
    kind: Literal["text", "sticker"] = "text"
    text: str = Field(min_length=1, max_length=32768)
    # A stable visual discriminator, not a verified QQ identity or a nickname alias.
    author_key: str | None = Field(default=None, min_length=1, max_length=256)


class ScreenPlan(_Model):
    status: Literal["baseline", "unchanged", "new", "ambiguous"]
    overlap: int = 0
    new_indices: list[int] = Field(default_factory=list)
    reason: str


EventState = Literal["observed", "bound", "ready", "decided_wait", "ignored", "reserved", "retry_wait", "click_started", "sent", "uncertain", "not_sent", "rate_limited", "expired", "superseded", "failed", "partial"]


class AttributionAudit(_Model):
    action: Literal["deferred", "explicitly_recovered"]
    at: AwareDatetime
    reason: str
    previous_run_id: str


class DecisionAudit(_Model):
    decision_id: str
    at: AwareDatetime
    reason: Literal["rate_limit_reopened"] = "rate_limit_reopened"


class WatchEvent(_Model):
    event_id: str
    group_id: QQID
    run_id: str
    origin: Literal["message", "homeostasis"] = "message"
    idle_intent: QQIdleIntent | None = None
    screen_id: str | None = None
    stream_revision: int
    item_index: int | None = None
    item: VisibleItem | None = None
    observed_at: AwareDatetime
    state: EventState
    source_record_id: str | None = None
    decision_id: str | None = None
    reason: str | None = None
    attribution_attempts: int = Field(default=0, ge=0)
    attribution_recheck_required: bool = False
    attribution_audit: list[AttributionAudit] = Field(default_factory=list)
    first_seen_at: AwareDatetime | None = None
    rate_limit_reopens: int = Field(default=0, ge=0, le=1)
    decision_audit: list[DecisionAudit] = Field(default_factory=list)

    @model_validator(mode="after")
    def inherit_first_seen(self):
        if self.first_seen_at is None:
            object.__setattr__(self, "first_seen_at", self.observed_at)
        if self.first_seen_at > self.observed_at:
            raise ValueError("first_seen_at cannot be later than observation time")
        if self.origin == "message":
            if self.screen_id is None or self.item_index is None or self.item is None or self.idle_intent is not None:
                raise ValueError("message events require the original visible item")
        else:
            intent = validated_idle_intent(self.idle_intent)
            if (any(value is not None for value in (self.screen_id, self.item_index, self.item, self.source_record_id))
                    or (self.group_id, self.run_id) != (intent.group_id, intent.run_id)
                    or self.first_seen_at != intent.created_at or self.observed_at != intent.created_at):
                raise ValueError("homeostasis events cannot impersonate incoming observations")
            object.__setattr__(self, "idle_intent", intent)
        return self


def address_sources_for_event(event: WatchEvent) -> list[str]:
    """Empty attribution is permitted only for a fully typed internal intent."""
    event = WatchEvent.model_validate(event.model_dump(mode="python"))
    if event.origin == "homeostasis":
        return []
    if event.source_record_id is None:
        raise WatchLedgerError("the original message source must be bound")
    return [event.source_record_id]


def _check_idle_proof(event: WatchEvent, proof) -> None:
    if event.origin != "homeostasis":
        return
    intent = validated_idle_intent(event.idle_intent)
    if (proof.source_record_ids or proof.group_id != intent.group_id
            or proof.self_member_id != intent.self_member_id
            or proof.body_instance_id != intent.body_instance_id
            or proof.environment_revision != intent.environment_revision
            or proof.window_id != intent.window_id):
        raise WatchLedgerError("idle proof must preserve the original body, window and empty attribution")


class ObservationBatch(_Model):
    group_id: QQID
    run_id: str
    screen_id: str | None
    plan: ScreenPlan
    events: list[WatchEvent] = Field(default_factory=list)


class PreClickProof(_Model):
    reservation_id: str | None = Field(default=None, min_length=1, max_length=2048)
    group_id: QQID
    self_member_id: QQID
    reply_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frame_evidence_ref: str = Field(min_length=1, max_length=2048)
    context_signature: str = Field(min_length=1, max_length=2048)
    verified_at: AwareDatetime
    before_outgoing_count: int | None = Field(default=None, ge=0)
    ordered_baseline_signatures: list[str] = Field(default_factory=list, max_length=256)
    source_record_ids: list[str] = Field(default_factory=list, max_length=256)
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)
    window_id: str | None = Field(default=None, max_length=128)
    body_instance_id: str | None = Field(default=None, max_length=128)
    environment_revision: int | None = Field(default=None, ge=1)
    title_pixels_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DeliveryAttempt(_Model):
    attempt_id: str
    reservation_id: str
    attempt_number: int = Field(ge=1, le=3)
    phase: Literal["prechecking", "precheck_failed", "click_started", "sent", "uncertain", "cancelled"]
    started_at: AwareDatetime
    updated_at: AwareDatetime
    reason: str | None = None
    proof: PreClickProof | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)


class SendReservation(_Model):
    reservation_id: str
    group_id: QQID
    run_id: str
    event_id: str
    decision_id: str
    status: Literal["reserved", "retry_wait", "click_started", "sent", "uncertain", "not_sent", "expired", "superseded", "partial"]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    cooldown_seconds: float = Field(ge=0, le=86400)
    evidence_refs: list[str] = Field(default_factory=list)
    precheck_evidence_refs: list[str] = Field(default_factory=list, max_length=96)
    postclick_evidence_refs: list[str] = Field(default_factory=list, max_length=128)
    reason: str | None = None
    priority: Literal["direct", "ambient"] = "ambient"
    self_member_id: QQID | None = None
    reply_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    deadline_at: AwareDatetime | None = None
    attempt_count: int = Field(default=0, ge=0, le=3)
    next_retry_at: AwareDatetime | None = None
    click_started_at: AwareDatetime | None = None
    click_timing_required: bool = False
    click_acknowledged_at: AwareDatetime | None = None
    click_observed_at: AwareDatetime | None = None
    click_observed_evidence_ref: str | None = None
    preclick_proof: PreClickProof | None = None
    receipt_id: str | None = None
    reconciled_at: AwareDatetime | None = None
    # Zero selects the unchanged legacy single-send protocol.
    segment_count: int = Field(default=0, ge=0, le=3)


class SendSegment(_Model):
    child_id: str
    parent_id: str
    group_id: QQID
    run_id: str
    event_id: str
    decision_id: str
    segment_index: int = Field(ge=0, le=2, strict=True)
    segment_count: int = Field(ge=1, le=3, strict=True)
    text: str = Field(min_length=1, max_length=4000, strict=True)
    reply_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["reserved", "retry_wait", "click_started", "sent", "uncertain", "cancelled"] = "reserved"
    created_at: AwareDatetime
    updated_at: AwareDatetime
    deadline_at: AwareDatetime
    next_retry_at: AwareDatetime | None = None
    attempt_count: int = Field(default=0, ge=0, le=3)
    reason: str | None = None
    proof: PreClickProof | None = None
    click_started_at: AwareDatetime | None = None
    click_acknowledged_at: AwareDatetime | None = None
    click_observed_at: AwareDatetime | None = None
    click_observed_evidence_ref: str | None = None
    receipt_id: str | None = None
    receipt_fingerprint: str | None = None
    delivered_at: AwareDatetime | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    precheck_evidence_refs: list[str] = Field(default_factory=list, max_length=96)
    postclick_evidence_refs: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def bound_text(self):
        if (self.segment_index >= self.segment_count or any(char in self.text for char in "\r\n\x85\u2028\u2029")
                or hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.reply_sha256):
            raise ValueError("invalid frozen reply segment")
        if self.click_acknowledged_at is not None and self.click_observed_at is not None:
            raise ValueError("a segment has only one conservative click upper bound")
        return self

    @property
    def reservation_id(self) -> str:
        return self.child_id

    @property
    def phase(self) -> str:
        return self.status


class LegacyAgentStepResult(_Model):
    action_id: str = Field(min_length=1, max_length=2048, strict=True)
    step_id: Literal["send-once"]
    success: bool = Field(strict=True)


class LegacyAgentStepJournal(_Model):
    schema_version: Literal["agent-step-journal-v1"]
    body_instance_id: str = Field(min_length=1, max_length=128, strict=True)
    environment_revision: int = Field(ge=1, strict=True)
    step_id: Literal["send-once"]
    state: Literal["completed"]
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: AwareDatetime
    finished_at: AwareDatetime
    result: LegacyAgentStepResult


class LegacyStepConflictRejection(_Model):
    """Locally validated read-only evidence, not a cryptographic attestation.

    Only an explicit operator maintenance action may consume this legacy proof.
    The old build/request binding must be independently supplied and audited.
    """
    schema_version: Literal["qq-legacy-step-conflict-rejection-v1"]
    kind: Literal["legacy_step_conflict_rejection"]
    reservation_id: str = Field(pattern=r"^qq-watch-send:[0-9a-f]{32}$")
    attempted_action_id: str = Field(pattern=r"^qq-send-[0-9a-f]{64}$")
    attempted_step_id: Literal["send-once"]
    queried_at: AwareDatetime
    journal: LegacyAgentStepJournal


_ITEMS = TypeAdapter(list[VisibleItem])


def _now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise WatchLedgerError("timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise WatchLedgerError("a nonempty identifier of at most 2048 characters is required")
    return value


def _items(value: list[VisibleItem]) -> list[VisibleItem]:
    parsed = _ITEMS.validate_python(value)
    if len(parsed) > 256:
        raise WatchLedgerError("a screen may contain at most 256 visible items")
    return parsed


def _occurrences(sequence: list[VisibleItem], anchor: list[VisibleItem]) -> int:
    return sum(sequence[i:i + len(anchor)] == anchor for i in range(len(sequence) - len(anchor) + 1))


def plan_visible_transition(
    previous_items: list[VisibleItem] | None,
    current_items: list[VisibleItem],
    *, frame_signature: str, previous_frame_signature: str | None = None,
) -> ScreenPlan:
    """Plan only visual continuity; an append is not a server message identity.

    Repeated text is allowed when a unique ordered anchor disambiguates it.
    Missing or repeating anchors reset the baseline, with no actionable items.
    """
    _identifier(frame_signature)
    current = _items(current_items)
    if previous_items is None:
        return ScreenPlan(status="baseline", reason="first_screen")
    previous = _items(previous_items)
    if frame_signature == previous_frame_signature:
        return ScreenPlan(status="unchanged", reason="same_frame")
    if current == previous:
        return ScreenPlan(status="unchanged", overlap=len(current), reason="same_ordered_items")
    if not previous or not current:
        return ScreenPlan(status="baseline", reason="empty_window")
    matches = [size for size in range(1, min(len(previous), len(current)) + 1) if previous[-size:] == current[:size]]
    if not matches:
        return ScreenPlan(status="baseline", reason="no_overlap")
    overlap = max(matches)
    anchor = current[:overlap]
    if len(matches) != 1 or _occurrences(previous, anchor) != 1 or _occurrences(current, anchor) != 1:
        return ScreenPlan(status="ambiguous", overlap=overlap, reason="repeated_overlap")
    indices = list(range(overlap, len(current)))
    if not indices:
        return ScreenPlan(status="baseline", overlap=overlap, reason="window_contracted")
    return ScreenPlan(status="new", overlap=overlap, new_indices=indices, reason="unique_ordered_append")


class WatchLedger(SegmentLedgerMixin):
    """Single-group SQLite ledger; every mutation belongs to one active run.

    The parent directory must already exist and be private. Old runs cannot
    resume work after a replacement run starts. Read methods expose uncertainty
    without resolving it; no automatic path clears an uncertain send.
    """

    def __init__(self, path: Path | str, *, group_id: str, allowed_read_group_ids: Sequence[str] | None = None):
        self.path = Path(path)
        self.group_id = TypeAdapter(QQID).validate_python(group_id)
        if isinstance(allowed_read_group_ids, (str, bytes)):
            raise WatchLedgerError("capability read scopes must be a group list")
        self.allowed_read_group_ids = frozenset(TypeAdapter(list[QQID]).validate_python(
            list(allowed_read_group_ids) if allowed_read_group_ids is not None else [self.group_id]))
        self.run_id: str | None = None
        if not self.path.parent.is_dir():
            raise WatchLedgerError("the existing private database directory is required")
        with closing(self._connect()) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS qq_watch_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), group_id TEXT NOT NULL,
                    run_id TEXT, previous_frame_signature TEXT, previous_items_json TEXT,
                    revision INTEGER NOT NULL DEFAULT 0, baseline_required INTEGER NOT NULL DEFAULT 1,
                    cooldown_until TEXT, last_screen_id TEXT);
                CREATE TABLE IF NOT EXISTS qq_watch_runs (
                    run_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, started_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS qq_watch_screens (
                    screen_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL, frame_signature TEXT NOT NULL,
                    items_json TEXT NOT NULL, plan_json TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS qq_watch_screens_frame ON qq_watch_screens(run_id,frame_signature);
                CREATE TABLE IF NOT EXISTS qq_watch_events (
                    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    state TEXT NOT NULL, record_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS qq_watch_idle_opportunities (
                    intent_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    episode_signature TEXT NOT NULL, registered_at TEXT NOT NULL, record_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS qq_watch_capability_owner (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), record_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS qq_watch_capability_claims (
                    opportunity_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, request_json TEXT NOT NULL,
                    event_id TEXT, status TEXT NOT NULL, result_json TEXT, acknowledged INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS qq_watch_sends (
                    reservation_id TEXT PRIMARY KEY, decision_id TEXT UNIQUE NOT NULL,
                    event_id TEXT UNIQUE NOT NULL, group_id TEXT NOT NULL,
                    status TEXT NOT NULL, record_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS qq_watch_delivery_attempts (
                    attempt_id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL, phase TEXT NOT NULL, record_json TEXT NOT NULL,
                    UNIQUE(reservation_id,attempt_number));
                CREATE TABLE IF NOT EXISTS qq_watch_send_segments (
                    child_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
                    group_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
                    status TEXT NOT NULL, record_json TEXT NOT NULL,
                    UNIQUE(parent_id,segment_index));
            """)
            connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(qq_watch_state)")}
            if "last_actual_activity_at" not in columns:
                connection.execute("ALTER TABLE qq_watch_state ADD COLUMN last_actual_activity_at TEXT")
            if "legacy_cooldown_until" not in columns:
                connection.execute("ALTER TABLE qq_watch_state ADD COLUMN legacy_cooldown_until TEXT")
                connection.execute("UPDATE qq_watch_state SET legacy_cooldown_until=cooldown_until")
            connection.execute("INSERT OR IGNORE INTO qq_watch_state(singleton,group_id) VALUES(1,?)", (self.group_id,))
            if connection.execute("SELECT group_id FROM qq_watch_state WHERE singleton=1").fetchone()[0] != self.group_id:
                raise WatchLedgerError("this watch database is bound to a different group")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute("SELECT * FROM qq_watch_state WHERE singleton=1").fetchone()
            if self.run_id is None or state["run_id"] != self.run_id:
                raise WatchLedgerError("start_run is required and must still be the active run")
            yield connection, state

    @staticmethod
    def _save_event(connection, event: WatchEvent):
        connection.execute("UPDATE qq_watch_events SET run_id=?,state=?,record_json=? WHERE event_id=?", (event.run_id, event.state, event.model_dump_json(), event.event_id))

    @staticmethod
    def _save_send(connection, reservation: SendReservation):
        connection.execute("UPDATE qq_watch_sends SET status=?,record_json=? WHERE reservation_id=?", (reservation.status, reservation.model_dump_json(), reservation.reservation_id))

    def _event(self, connection, event_id: str, *, current_run: bool = True) -> WatchEvent:
        row = connection.execute("SELECT record_json FROM qq_watch_events WHERE event_id=? AND group_id=?", (_identifier(event_id), self.group_id)).fetchone()
        if row is None:
            raise WatchLedgerError("unknown event in this group")
        event = WatchEvent.model_validate_json(row[0])
        if current_run and event.run_id != self.run_id:
            raise WatchLedgerError("an event from an earlier run cannot be resumed")
        return event

    def _reservation(self, connection, reservation_id: str) -> SendReservation:
        row = connection.execute("SELECT record_json FROM qq_watch_sends WHERE reservation_id=? AND group_id=?", (_identifier(reservation_id), self.group_id)).fetchone()
        if row is None:
            raise WatchLedgerError("unknown send reservation in this group")
        return SendReservation.model_validate_json(row[0])

    def next_idle_opportunity_at(self, *, minimum_interval_seconds: float = 1800) -> datetime | None:
        """Read the durable global opportunity boundary without GUI work.

        This advisory lookup cannot reserve an opportunity. Registration still
        checks the same boundary atomically, including after process restart.
        """
        if (isinstance(minimum_interval_seconds, bool) or not isinstance(minimum_interval_seconds, (int, float))
                or not 0 <= minimum_interval_seconds <= 86400):
            raise WatchLedgerError("idle opportunity interval must be finite and within one day")
        with closing(sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute("SELECT registered_at FROM qq_watch_idle_opportunities WHERE group_id=? ORDER BY rowid DESC LIMIT 1",
                                     (self.group_id,)).fetchone()
        return None if row is None else datetime.fromisoformat(row[0]) + timedelta(seconds=minimum_interval_seconds)

    def capability_owner(self):
        with closing(self._connect()) as db:
            row = db.execute("SELECT record_json FROM qq_watch_capability_owner WHERE singleton=1").fetchone()
        return json.loads(row[0]) if row else None

    def set_capability_owner(self, *, owner: str, generation: int):
        if owner not in {"legacy", "shared", "off"} or type(generation) is not int or generation < 0:
            raise WatchLedgerError("invalid capability owner")
        with self._transaction() as (db, _):
            db.execute("INSERT OR REPLACE INTO qq_watch_capability_owner VALUES(1,?)",
                (json.dumps({"owner": owner, "generation": generation}),))

    def register_capability_claim(self, request: dict):
        group_id = request.get("scope", {}).get("group_id")
        capability = request.get("capability_id")
        allowed = (capability == QQ_COMMUNICATION_READ and group_id in self.allowed_read_group_ids
            or capability == QQ_COMMUNICATION_SEND and group_id == self.group_id)
        if request.get("run_id") != self.run_id or not allowed:
            raise WatchLedgerError("capability claim scope mismatch")
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
        with self._transaction() as (db, _):
            old = db.execute("SELECT request_json FROM qq_watch_capability_claims WHERE opportunity_id=?",
                (request["opportunity_id"],)).fetchone()
            if old and old[0] != encoded:
                raise WatchLedgerError("capability opportunity cannot be rebound")
            db.execute("INSERT OR IGNORE INTO qq_watch_capability_claims(opportunity_id,run_id,request_json,status) VALUES(?,?,?,'claimed')",
                (request["opportunity_id"], self.run_id, encoded))

    def capability_claims(self):
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM qq_watch_capability_claims WHERE acknowledged=0 ORDER BY rowid LIMIT 32").fetchall()
        return [{**json.loads(row["request_json"]), "local_status": row["status"], "event_id": row["event_id"]} for row in rows]

    def finish_capability_claim(self, opportunity_id, *, status: str, result: dict | None = None):
        if status not in {"completed", "rejected", "expired", "failed", "superseded", "wait"}:
            raise WatchLedgerError("invalid capability terminal state")
        with self._transaction() as (db, _):
            db.execute("UPDATE qq_watch_capability_claims SET status=?,result_json=? WHERE opportunity_id=? AND run_id=? AND status='claimed'",
                (status, json.dumps(result) if result is not None else None, opportunity_id, self.run_id))

    def finish_capability_ack(self, opportunity_id):
        with self._transaction() as (db, _):
            db.execute("UPDATE qq_watch_capability_claims SET acknowledged=1 WHERE opportunity_id=?", (opportunity_id,))

    def expire_unstarted_capability_claims(self, *, now):
        with self._transaction() as (db, _):
            for row in db.execute("SELECT * FROM qq_watch_capability_claims WHERE status='claimed' AND event_id IS NULL").fetchall():
                request = json.loads(row["request_json"])
                if row["run_id"] != self.run_id or now >= datetime.fromisoformat(request["deadline_at"]):
                    db.execute("UPDATE qq_watch_capability_claims SET status='expired' WHERE opportunity_id=?", (row["opportunity_id"],))

    def register_idle_intent(
        self, intent: QQIdleIntent, *, self_member_id: str, body_instance_id: str,
        environment_revision: int, window_id: str, context_signature: str,
        minimum_interval_seconds: float = 1800, now: datetime | None = None,
    ) -> WatchEvent | None:
        """Consume one durable opportunity, including subsequent wait/failure.

        A new intent in the same episode is possible after the global interval.
        Reusing an intent ID never creates work, including after a restart.
        """
        try:
            intent = validated_idle_intent(intent)
        except (ValueError, TypeError):
            raise WatchLedgerError("a valid typed idle intent is required") from None
        moment = _now(now)
        if (isinstance(minimum_interval_seconds, bool) or not isinstance(minimum_interval_seconds, (int, float))
                or not 0 <= minimum_interval_seconds <= 86400):
            raise WatchLedgerError("idle opportunity interval must be finite and within one day")
        expected = (self.group_id, self.run_id, self_member_id, body_instance_id, environment_revision, window_id, context_signature)
        actual = (intent.group_id, intent.run_id, intent.self_member_id, intent.body_instance_id,
                  intent.environment_revision, intent.window_id, intent.context_signature)
        if actual != expected or type(environment_revision) is not int:
            raise WatchLedgerError("idle intent does not match the active scope")
        with self._transaction() as (connection, state):
            existing = connection.execute("SELECT record_json FROM qq_watch_idle_opportunities WHERE intent_id=?", (intent.intent_id,)).fetchone()
            if existing is not None:
                if QQIdleIntent.model_validate_json(existing[0]) != intent:
                    raise WatchLedgerError("an idle intent ID cannot be rebound")
                return None
            if state["baseline_required"]:
                raise WatchLedgerError("idle requires a completed current-run baseline")
            if not intent.created_at - timedelta(seconds=2) <= moment < intent.deadline_at:
                raise WatchLedgerError("idle opportunity is expired or in the future")
            last = connection.execute("SELECT registered_at FROM qq_watch_idle_opportunities WHERE group_id=? ORDER BY rowid DESC LIMIT 1", (self.group_id,)).fetchone()
            if last is not None and (moment - datetime.fromisoformat(last[0])).total_seconds() < minimum_interval_seconds:
                return None
            event = WatchEvent(event_id=f"qq-watch-event:{uuid4().hex}", group_id=self.group_id,
                run_id=self.run_id, origin="homeostasis", idle_intent=intent,
                stream_revision=state["revision"], observed_at=intent.created_at, state="bound")
            connection.execute("INSERT INTO qq_watch_idle_opportunities VALUES(?,?,?,?,?,?)", (
                intent.intent_id, self.group_id, self.run_id, intent.episode_signature, moment.isoformat(), intent.model_dump_json()))
            connection.execute("INSERT INTO qq_watch_events VALUES(?,?,?,?,?)", (
                event.event_id, self.run_id, self.group_id, event.state, event.model_dump_json()))
            if intent.opportunity_ref is not None:
                claim = connection.execute("SELECT * FROM qq_watch_capability_claims WHERE opportunity_id=?", (intent.opportunity_ref,)).fetchone()
                if claim is None or claim["run_id"] != self.run_id or claim["status"] != "claimed" or claim["event_id"] is not None:
                    raise WatchLedgerError("shared idle requires an unused durable capability claim")
                request = json.loads(claim["request_json"])
                if (request["selection_ref"] != intent.selection_ref or request["generation"] != intent.scheduler_generation
                        or datetime.fromisoformat(request["deadline_at"]) != intent.deadline_at):
                    raise WatchLedgerError("shared idle claim binding mismatch")
                connection.execute("UPDATE qq_watch_capability_claims SET event_id=?,status='bound' WHERE opportunity_id=?",
                    (event.event_id, intent.opportunity_ref))
            return event

    def start_run(self, *, now: datetime | None = None) -> str:
        """Start a new baseline, abandon old work, and quarantine pending sends."""
        moment = _now(now)
        run_id = f"qq-watch-run:{uuid4().hex}"
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute("SELECT record_json FROM qq_watch_sends WHERE status IN ('reserved','retry_wait','click_started')").fetchall()
            for row in pending:
                reservation = SendReservation.model_validate_json(row[0]).model_copy(update={"status": "uncertain", "updated_at": moment, "reason": "process_restarted_with_pending_send"})
                if reservation.segment_count:
                    status = self._quarantine_segments(connection, reservation, moment)
                    reservation = reservation.model_copy(update={"status": status})
                self._save_send(connection, reservation)
                self._finish_attempt(connection, reservation.reservation_id, "uncertain", moment, reservation.reason)
                event = self._event(connection, reservation.event_id, current_run=False).model_copy(update={"state": reservation.status, "reason": reservation.reason})
                self._save_event(connection, event)
            unfinished = connection.execute("SELECT record_json FROM qq_watch_events WHERE state IN ('observed','bound','ready')").fetchall()
            for row in unfinished:
                event = WatchEvent.model_validate_json(row[0])
                attribution_failure = (event.state == "observed" and event.source_record_id is None
                                       and event.decision_id is None and event.attribution_attempts > 0
                                       and (event.reason or "").startswith("attribution_"))
                # Keep the precise failure for an explicit operator repair;
                # ignored state still prevents all automatic restart retries.
                event = event.model_copy(update={"state": "ignored", "reason": event.reason if attribution_failure else "process_restarted_no_backlog"})
                self._save_event(connection, event)
            connection.execute("INSERT INTO qq_watch_runs VALUES(?,?,?)", (run_id, self.group_id, moment.isoformat()))
            connection.execute("UPDATE qq_watch_state SET run_id=?,baseline_required=1,revision=revision+1 WHERE singleton=1", (run_id,))
        self.run_id = run_id
        return run_id

    def observe(self, *, frame_signature: str, items: list[VisibleItem], observed_at: datetime | None = None, first_seen_at: datetime | None = None) -> ObservationBatch:
        moment = _now(observed_at)
        first_seen = _now(first_seen_at) if first_seen_at is not None else moment
        if first_seen > moment:
            raise WatchLedgerError("first_seen_at cannot be later than observed_at")
        parsed = _items(items)
        with self._transaction() as (connection, state):
            previous = None if state["baseline_required"] else _ITEMS.validate_json(state["previous_items_json"])
            plan = plan_visible_transition(previous, parsed, frame_signature=frame_signature, previous_frame_signature=state["previous_frame_signature"])
            if plan.reason == "same_frame":
                return ObservationBatch(group_id=self.group_id, run_id=self.run_id, screen_id=state["last_screen_id"], plan=plan)
            seen = connection.execute("SELECT items_json FROM qq_watch_screens WHERE run_id=? AND frame_signature=? LIMIT 1", (self.run_id, frame_signature)).fetchone()
            if seen is not None:
                # Returning to an old screenshot is not evidence of new arrivals.
                # Keep its first interpretation, rather than treating OCR drift
                # on identical pixels as an independent stream occurrence.
                parsed = _ITEMS.validate_json(seen["items_json"])
                plan = ScreenPlan(status="baseline", reason="previously_seen_frame")
            revision = state["revision"] + (plan.status != "unchanged")
            screen_id = f"qq-watch-screen:{uuid4().hex}"
            items_json = _ITEMS.dump_json(parsed).decode("utf-8")
            connection.execute("INSERT INTO qq_watch_screens VALUES(?,?,?,?,?,?,?,?)", (screen_id, self.run_id, self.group_id, moment.isoformat(), frame_signature, items_json, plan.model_dump_json(), revision))
            connection.execute("UPDATE qq_watch_state SET previous_frame_signature=?,previous_items_json=?,revision=?,baseline_required=0,last_screen_id=? WHERE singleton=1", (frame_signature, items_json, revision, screen_id))
            events = []
            for index in plan.new_indices:
                item = parsed[index]
                event = WatchEvent(event_id=f"qq-watch-event:{uuid4().hex}", group_id=self.group_id, run_id=self.run_id, screen_id=screen_id, stream_revision=revision, item_index=index, item=item, observed_at=moment, first_seen_at=first_seen, state="observed" if item.direction == "incoming" else "ignored", reason=None if item.direction == "incoming" else "outgoing_visual_item")
                connection.execute("INSERT INTO qq_watch_events VALUES(?,?,?,?,?)", (event.event_id, self.run_id, self.group_id, event.state, event.model_dump_json()))
                events.append(event)
            return ObservationBatch(group_id=self.group_id, run_id=self.run_id, screen_id=screen_id, plan=plan, events=events)

    def bind_event(self, event_id: str, source_record_id: str) -> WatchEvent:
        """Bind a caller-verified memory record, without guessing author identity."""
        _identifier(source_record_id)
        with self._transaction() as (connection, state):
            event = self._event(connection, event_id)
            if event.source_record_id == source_record_id:
                return event
            if event.origin != "message" or event.state != "observed" or event.source_record_id is not None or event.item.direction != "incoming":
                raise WatchLedgerError("only an unbound incoming event can be bound")
            if event.attribution_recheck_required or state["baseline_required"] or event.stream_revision != state["revision"]:
                raise WatchLedgerError("pending attribution requires a matching current screen before binding")
            event = event.model_copy(update={"state": "bound", "source_record_id": source_record_id})
            self._save_event(connection, event)
            return event

    def defer_attribution(self, event_id: str, reason: str, *, now: datetime | None = None) -> WatchEvent:
        """Keep a failed identity lookup retryable, without inventing an arrival."""
        _identifier(reason)
        moment = _now(now)
        with self._transaction() as (connection, _):
            event = self._event(connection, event_id)
            if (event.origin != "message" or event.state != "observed" or event.item.direction != "incoming"
                    or event.source_record_id is not None or event.decision_id is not None
                    or connection.execute("SELECT 1 FROM qq_watch_sends WHERE event_id=?", (event_id,)).fetchone()):
                raise WatchLedgerError("only an unbound incoming observation can defer attribution")
            audit = AttributionAudit(action="deferred", at=moment, reason=reason, previous_run_id=event.run_id)
            event = event.model_copy(update={
                "reason": reason, "attribution_attempts": event.attribution_attempts + 1,
                "attribution_recheck_required": True, "attribution_audit": [*event.attribution_audit, audit],
            })
            self._save_event(connection, event)
            return event

    def pending_attribution(
        self, *, items: list[VisibleItem], frame_signature: str,
        max_age_seconds: float = 900, now: datetime | None = None,
    ) -> list[WatchEvent]:
        """Revisit only an exact current window and a unique original item index.

        Call observe first for a changed frame. This never emits new events or
        retries a decision/send. Observation timestamps and IDs stay unchanged.
        """
        parsed = _items(items)
        _identifier(frame_signature)
        moment = _now(now)
        age_limit = TypeAdapter(float).validate_python(max_age_seconds)
        if not 0 <= age_limit <= 86400:
            raise WatchLedgerError("max_age_seconds must be between 0 and 86400")
        with self._transaction() as (connection, state):
            if (state["baseline_required"] or state["previous_frame_signature"] != frame_signature
                    or _ITEMS.validate_json(state["previous_items_json"]) != parsed):
                return []
            result = []
            rows = connection.execute("SELECT record_json FROM qq_watch_events WHERE run_id=? AND group_id=? AND state='observed'", (self.run_id, self.group_id)).fetchall()
            for row in rows:
                event = WatchEvent.model_validate_json(row[0])
                if (event.origin != "message" or event.item.direction != "incoming" or event.source_record_id is not None or event.decision_id is not None
                        or not 0 <= (moment - event.observed_at).total_seconds() <= age_limit
                        or connection.execute("SELECT 1 FROM qq_watch_sends WHERE event_id=?", (event.event_id,)).fetchone()):
                    continue
                original = connection.execute("SELECT items_json FROM qq_watch_screens WHERE screen_id=? AND group_id=?", (event.screen_id, self.group_id)).fetchone()
                if original is None or _ITEMS.validate_json(original["items_json"]) != parsed:
                    continue
                if [index for index, candidate in enumerate(parsed) if candidate == event.item] != [event.item_index]:
                    continue
                result.append(event)
            counts = {event.item_index: sum(candidate.item_index == event.item_index for candidate in result) for event in result}
            unique = []
            for event in result:
                if counts[event.item_index] == 1:
                    event = event.model_copy(update={"stream_revision": state["revision"], "attribution_recheck_required": False})
                    self._save_event(connection, event)
                    unique.append(event)
            return sorted(unique, key=lambda event: (event.item_index, event.observed_at, event.event_id))

    def recover_unbound_attribution_event(self, event_id: str, *, now: datetime | None = None) -> WatchEvent:
        """Explicit operator repair of a legacy ignored attribution failure.

        This method is never called by start_run. A deliberate cross-run repair
        preserves the old run in its audit and still requires a current baseline
        and exact pending_attribution check. It cannot recover any send attempt.
        """
        moment = _now(now)
        with self._transaction() as (connection, _):
            event = self._event(connection, event_id, current_run=False)
            if (event.origin != "message" or event.state != "ignored" or not (event.reason or "").startswith("attribution_")
                    or event.item.direction != "incoming" or event.source_record_id is not None or event.decision_id is not None
                    or not 0 <= (moment - event.observed_at).total_seconds() <= 900
                    or connection.execute("SELECT 1 FROM qq_watch_sends WHERE event_id=?", (event_id,)).fetchone()):
                raise WatchLedgerError("only an ignored, unbound attribution failure can be explicitly recovered")
            audit = AttributionAudit(action="explicitly_recovered", at=moment, reason=event.reason, previous_run_id=event.run_id)
            event = event.model_copy(update={
                "run_id": self.run_id, "state": "observed", "attribution_recheck_required": True,
                "attribution_audit": [*event.attribution_audit, audit],
            })
            self._save_event(connection, event)
            return event

    def mark_ignored(self, event_id: str, reason: str) -> WatchEvent:
        _identifier(reason)
        with self._transaction() as (connection, _):
            event = self._event(connection, event_id)
            if event.state == "ignored":
                return event
            if event.state not in {"observed", "bound", "ready", "decided_wait"}:
                raise WatchLedgerError("a reserved or completed send cannot be ignored")
            event = event.model_copy(update={"state": "ignored", "reason": reason})
            self._save_event(connection, event)
            return event

    def finish_event(self, event_id: str, *, outcome: Literal["expired", "failed", "rate_limited", "superseded"], reason: str) -> WatchEvent:
        """Finish unreserved work without pretending a failure was a model wait."""
        if outcome not in {"expired", "failed", "rate_limited", "superseded"}:
            raise WatchLedgerError("an explicit non-delivery outcome is required")
        _identifier(reason)
        with self._transaction() as (connection, _):
            event = self._event(connection, event_id)
            if connection.execute("SELECT 1 FROM qq_watch_sends WHERE event_id=?", (event_id,)).fetchone():
                raise WatchLedgerError("reserved events must use send-intent transitions")
            if event.state == outcome:
                return event
            if event.state not in {"observed", "bound", "ready", "rate_limited"}:
                raise WatchLedgerError("a completed event cannot change outcome")
            event = event.model_copy(update={"state": outcome, "reason": reason})
            self._save_event(connection, event)
            return event

    def reopen_rate_limited(self, event_id: str, *, now: datetime | None = None) -> WatchEvent | None:
        """Permit one fresh decision within the original TTL, never reuse its draft.

        The coordinator waits for its selected priority's next_allowed_at first;
        reserve_send independently rechecks that cooldown before any intention.
        """
        moment = _now(now)
        with self._transaction() as (connection, state):
            event = self._event(connection, event_id)
            if event.origin == "homeostasis" or event.state != "rate_limited" or event.rate_limit_reopens:
                return None
            if event.source_record_id is None or event.decision_id is None:
                raise WatchLedgerError("a rate-limited event needs its original source and decision")
            if connection.execute("SELECT 1 FROM qq_watch_sends WHERE event_id=?", (event_id,)).fetchone():
                raise WatchLedgerError("an existing send intent cannot be reopened")
            if moment < event.first_seen_at - timedelta(seconds=2):
                raise WatchLedgerError("first-seen time is in the future")
            if moment >= event.first_seen_at + timedelta(seconds=60):
                self._save_event(connection, event.model_copy(update={"state": "expired", "reason": "event_deadline_expired"}))
                return None
            if event.stream_revision != state["revision"]:
                self._save_event(connection, event.model_copy(update={"state": "superseded", "reason": "visible_stream_changed"}))
                return None
            audit = DecisionAudit(decision_id=event.decision_id, at=moment)
            event = event.model_copy(update={"state": "bound", "decision_id": None, "reason": "rate_limit_reopened", "rate_limit_reopens": 1, "decision_audit": [*event.decision_audit, audit]})
            self._save_event(connection, event)
            return event

    def attach_decision(self, event_id: str, decision_id: str, action: Literal["speak", "wait"], *, new_decision: bool, replayed: bool = False) -> WatchEvent:
        _identifier(decision_id)
        if action not in {"speak", "wait"} or type(new_decision) is not bool or type(replayed) is not bool:
            raise WatchLedgerError("an explicit speak/wait decision and freshness flags are required")
        with self._transaction() as (connection, _):
            event = self._event(connection, event_id)
            if event.decision_id == decision_id:
                if event.state == "ready" and (not new_decision or replayed):
                    event = event.model_copy(update={"state": "ignored", "reason": "cached_speak_not_queued"})
                    self._save_event(connection, event)
                return event
            if event.state != "bound":
                raise WatchLedgerError("only a bound, undecided event can receive a decision")
            address_sources_for_event(event)
            status, reason = "decided_wait", "decision_wait"
            if action == "speak":
                status, reason = ("ready", "fresh_speak") if new_decision and not replayed else ("ignored", "cached_speak_not_queued")
            event = event.model_copy(update={"state": status, "decision_id": decision_id, "reason": reason})
            self._save_event(connection, event)
            return event

    @staticmethod
    def _cooldown(priority: str, compatibility_seconds: float | None = None) -> float:
        if priority not in {"direct", "ambient"}:
            raise WatchLedgerError("priority must be direct or ambient")
        # Old callers may pass the matching value, but cannot shorten the policy.
        seconds = 10.0 if priority == "direct" else 60.0
        if compatibility_seconds is not None and compatibility_seconds != seconds:
            raise WatchLedgerError("cooldown_seconds must match the direct/ambient policy")
        return seconds

    @staticmethod
    def _allowed_at(state, seconds: float) -> datetime | None:
        candidates = []
        if state["last_actual_activity_at"]:
            candidates.append(datetime.fromisoformat(state["last_actual_activity_at"]) + timedelta(seconds=seconds))
        if state["legacy_cooldown_until"]:
            candidates.append(datetime.fromisoformat(state["legacy_cooldown_until"]))
        return max(candidates) if candidates else None

    def next_allowed_at(self, *, priority: Literal["direct", "ambient"] = "ambient") -> datetime | None:
        seconds = self._cooldown(priority)
        with closing(self._connect()) as connection:
            state = connection.execute("SELECT * FROM qq_watch_state WHERE singleton=1").fetchone()
            return self._allowed_at(state, seconds)

    @staticmethod
    def _finish_attempt(connection, reservation_id: str, phase: str, moment: datetime, reason: str | None = None, *, evidence_refs: list[str] | None = None):
        row = connection.execute("SELECT record_json FROM qq_watch_delivery_attempts WHERE reservation_id=? AND phase IN ('prechecking','click_started','uncertain') ORDER BY attempt_number DESC LIMIT 1", (reservation_id,)).fetchone()
        if row is not None:
            attempt = DeliveryAttempt.model_validate_json(row[0])
            attempt = attempt.model_copy(update={"phase": phase, "updated_at": moment, "reason": reason,
                "evidence_refs": sorted(set(attempt.evidence_refs + (evidence_refs or [])))})
            connection.execute("UPDATE qq_watch_delivery_attempts SET phase=?,record_json=? WHERE attempt_id=?", (phase, attempt.model_dump_json(), attempt.attempt_id))

    def _set_send(self, connection, reservation: SendReservation, status: str, moment: datetime, reason: str | None = None) -> SendReservation:
        reservation = reservation.model_copy(update={"status": status, "updated_at": moment, "reason": reason, "next_retry_at": None})
        self._save_send(connection, reservation)
        event = self._event(connection, reservation.event_id, current_run=False).model_copy(update={"state": status, "reason": reason})
        self._save_event(connection, event)
        return reservation

    @staticmethod
    def _record_actual_activity(connection, state, moment: datetime, seconds: float):
        latest = moment
        if state["last_actual_activity_at"]:
            latest = max(latest, datetime.fromisoformat(state["last_actual_activity_at"]))
        until = latest + timedelta(seconds=seconds)
        if state["legacy_cooldown_until"]:
            until = max(until, datetime.fromisoformat(state["legacy_cooldown_until"]))
        connection.execute("UPDATE qq_watch_state SET last_actual_activity_at=?,cooldown_until=? WHERE singleton=1", (latest.isoformat(), until.isoformat()))

    @classmethod
    def _preserve_click_activity(cls, connection, state, reservation: SendReservation):
        """Preserve the first conservative local upper bound on the click.

        Legacy records without a click timestamp retain the existing activity
        and migrated cooldown verbatim; receipt wall time is never a fallback.
        """
        clicked = reservation.click_acknowledged_at or reservation.click_observed_at
        if clicked is None and not reservation.click_timing_required:
            clicked = reservation.click_started_at
        if clicked is not None and (not state["last_actual_activity_at"]
                or clicked > datetime.fromisoformat(state["last_actual_activity_at"])):
            cls._record_actual_activity(connection, state, clicked, reservation.cooldown_seconds)

    def reserve_send(self, event_id: str, decision_id: str, *, now: datetime | None = None,
                     priority: Literal["direct", "ambient"] = "ambient", reply_sha256: str,
                     self_member_id: str, cooldown_seconds: float | None = None,
                     late_grace_seconds: float = 0) -> SendReservation | None:
        """Atomically reserve a fresh current event; blocked events are discarded.

        A returned reservation is not delivery authorization or success. Persist
        click_started immediately before the caller's first possible send click.
        """
        moment = _now(now)
        _identifier(decision_id)
        seconds = self._cooldown(priority, cooldown_seconds)
        self_id = TypeAdapter(QQID).validate_python(self_member_id)
        if not isinstance(reply_sha256, str) or len(reply_sha256) != 64 or any(char not in "0123456789abcdef" for char in reply_sha256):
            raise WatchLedgerError("reply_sha256 must bind the original reply before reservation")
        if type(late_grace_seconds) not in {int, float} or not 0 <= late_grace_seconds <= 30:
            raise WatchLedgerError("late send grace is invalid")
        with self._transaction() as (connection, state):
            event = self._event(connection, event_id)
            if event.state != "ready" or event.decision_id != decision_id:
                return None
            reason = None
            rejected_state = "ignored"
            deadline = event.idle_intent.deadline_at if event.origin == "homeostasis" else event.first_seen_at + timedelta(seconds=60)
            send_deadline = deadline + timedelta(seconds=late_grace_seconds)
            if event.origin == "homeostasis" and (priority != "ambient" or self_id != event.idle_intent.self_member_id):
                raise WatchLedgerError("idle send must retain its own account and ambient priority")
            if moment >= send_deadline:
                reason, rejected_state = "event_deadline_expired", "expired"
            elif moment < event.first_seen_at - timedelta(seconds=2):
                raise WatchLedgerError("first_seen_at is in the future")
            elif event.stream_revision != state["revision"]:
                reason = "newer_screen_discarded_old_speak"
                rejected_state = "superseded"
            elif connection.execute("SELECT 1 FROM qq_watch_sends WHERE status IN ('reserved','retry_wait','click_started','uncertain') LIMIT 1").fetchone():
                reason = "unresolved_send_blocks_delivery"
            elif connection.execute("SELECT 1 FROM qq_watch_sends WHERE decision_id=?", (decision_id,)).fetchone():
                reason = "decision_already_reserved"
            elif (allowed := self._allowed_at(state, seconds)) is not None and moment < allowed:
                reason, rejected_state = "cooldown_discarded_no_queue", "rate_limited"
            if reason:
                self._save_event(connection, event.model_copy(update={"state": rejected_state, "reason": reason}))
                return None
            reservation = SendReservation(reservation_id=f"qq-watch-send:{uuid4().hex}", group_id=self.group_id, run_id=self.run_id, event_id=event_id, decision_id=decision_id, status="reserved", created_at=moment, updated_at=moment, cooldown_seconds=seconds, priority=priority, self_member_id=self_id, reply_sha256=reply_sha256, deadline_at=send_deadline)
            connection.execute("INSERT INTO qq_watch_sends VALUES(?,?,?,?,?,?)", (reservation.reservation_id, decision_id, event_id, self.group_id, reservation.status, reservation.model_dump_json()))
            self._save_event(connection, event.model_copy(update={"state": "reserved", "reason": None}))
            return reservation

    def _begin_attempt(self, connection, reservation: SendReservation, moment: datetime) -> DeliveryAttempt:
        if reservation.segment_count:
            raise WatchLedgerError("a segmented parent must start a child attempt")
        if reservation.attempt_count >= 3:
            raise WatchLedgerError("at most two pre-click retries are permitted")
        if connection.execute("SELECT 1 FROM qq_watch_delivery_attempts WHERE reservation_id=? AND phase IN ('prechecking','click_started')", (reservation.reservation_id,)).fetchone():
            raise WatchLedgerError("a delivery attempt is already active")
        attempt = DeliveryAttempt(attempt_id=f"qq-watch-attempt:{uuid4().hex}", reservation_id=reservation.reservation_id, attempt_number=reservation.attempt_count + 1, phase="prechecking", started_at=moment, updated_at=moment)
        connection.execute("INSERT INTO qq_watch_delivery_attempts VALUES(?,?,?,?,?)", (attempt.attempt_id, attempt.reservation_id, attempt.attempt_number, attempt.phase, attempt.model_dump_json()))
        self._set_send(connection, reservation.model_copy(update={"attempt_count": attempt.attempt_number}), "reserved", moment)
        return attempt

    def start_send_attempt(self, reservation_id: str, *, now: datetime | None = None) -> DeliveryAttempt | None:
        moment = _now(now)
        with self._transaction() as (connection, state):
            reservation = self._reservation(connection, reservation_id)
            if reservation.run_id != self.run_id or reservation.status not in {"reserved", "retry_wait"} or reservation.preclick_proof is not None:
                raise WatchLedgerError("this intent cannot start a pre-click attempt")
            if reservation.deadline_at is None or moment >= reservation.deadline_at:
                self._set_send(connection, reservation, "expired", moment, "event_deadline_expired")
                self._finish_attempt(connection, reservation_id, "cancelled", moment, "event_deadline_expired")
                return None
            if self._event(connection, reservation.event_id).stream_revision != state["revision"]:
                self._set_send(connection, reservation, "superseded", moment, "visible_stream_changed")
                self._finish_attempt(connection, reservation_id, "cancelled", moment, "visible_stream_changed")
                return None
            if reservation.next_retry_at is not None and moment < reservation.next_retry_at:
                return None
            return self._begin_attempt(connection, reservation, moment)

    def defer_preclick(self, reservation_id: str, *, reason: str, retryable: bool = True,
                       evidence_refs: list[str] | None = None, now: datetime | None = None) -> SendReservation:
        _identifier(reason)
        if type(retryable) is not bool:
            raise WatchLedgerError("retryable must be an explicit boolean")
        if evidence_refs is not None and (not isinstance(evidence_refs, list) or len(evidence_refs) > 32):
            raise WatchLedgerError("pre-click evidence must be a list of at most 32 references")
        refs = sorted(set(_identifier(ref) for ref in (evidence_refs or [])))
        moment = _now(now)
        with self._transaction() as (connection, state):
            reservation = self._reservation(connection, reservation_id)
            active = connection.execute("SELECT 1 FROM qq_watch_delivery_attempts WHERE reservation_id=? AND phase='prechecking'", (reservation_id,)).fetchone()
            if reservation.run_id != self.run_id or reservation.status != "reserved" or reservation.preclick_proof is not None or not active:
                raise WatchLedgerError("only an active pre-click attempt can be deferred")
            reservation = reservation.model_copy(update={
                "evidence_refs": sorted(set(reservation.evidence_refs + refs)),
                # Final receipt evidence keeps its original exact semantics;
                # failed prechecks remain independently available after sent.
                "precheck_evidence_refs": sorted(set(reservation.precheck_evidence_refs + refs)),
            })
            self._finish_attempt(connection, reservation_id, "precheck_failed", moment, reason, evidence_refs=refs)
            if reservation.deadline_at is None or moment >= reservation.deadline_at:
                return self._set_send(connection, reservation, "expired", moment, "event_deadline_expired")
            if self._event(connection, reservation.event_id).stream_revision != state["revision"]:
                return self._set_send(connection, reservation, "superseded", moment, "visible_stream_changed")
            if not retryable or reservation.attempt_count >= 3:
                return self._set_send(connection, reservation, "not_sent", moment, reason)
            reservation = self._set_send(connection, reservation, "retry_wait", moment, reason)
            reservation = reservation.model_copy(update={"next_retry_at": moment + timedelta(seconds=2)})
            self._save_send(connection, reservation)
            return reservation

    def pending_preclick(self, *, now: datetime | None = None) -> list[SendReservation]:
        moment = _now(now)
        with self._transaction() as (connection, state):
            result = []
            for row in connection.execute("SELECT record_json FROM qq_watch_sends WHERE status IN ('reserved','retry_wait')").fetchall():
                reservation = SendReservation.model_validate_json(row[0])
                if reservation.segment_count:
                    continue
                if reservation.run_id != self.run_id or reservation.preclick_proof is not None:
                    continue
                status = None
                if reservation.deadline_at is None or moment >= reservation.deadline_at:
                    status = "expired"
                elif self._event(connection, reservation.event_id).stream_revision != state["revision"]:
                    status = "superseded"
                if status:
                    self._set_send(connection, reservation, status, moment, status)
                    self._finish_attempt(connection, reservation.reservation_id, "cancelled", moment, status)
                elif ((reservation.next_retry_at is None or moment >= reservation.next_retry_at)
                      and not connection.execute("SELECT 1 FROM qq_watch_delivery_attempts WHERE reservation_id=? AND phase='prechecking'", (reservation.reservation_id,)).fetchone()):
                    result.append(reservation)
            return result

    def supersede_send(self, reservation_id: str, *, reason: str = "newer_context", now: datetime | None = None) -> SendReservation:
        moment = _now(now)
        _identifier(reason)
        with self._transaction() as (connection, _):
            reservation = self._reservation(connection, reservation_id)
            if reservation.run_id != self.run_id or reservation.status not in {"reserved", "retry_wait"} or reservation.preclick_proof is not None:
                raise WatchLedgerError("only a pre-click intent can be superseded")
            self._finish_attempt(connection, reservation_id, "cancelled", moment, reason)
            return self._set_send(connection, reservation, "superseded", moment, reason)

    def _advance_send(self, reservation_id: str, status: str, *, now: datetime | None = None, evidence_refs: list[str] | None = None, reason: str | None = None, proof: PreClickProof | None = None, require_click_timing: bool = False) -> SendReservation:
        moment = _now(now)
        with self._transaction() as (connection, state):
            reservation = self._reservation(connection, reservation_id)
            if reservation.run_id != self.run_id:
                raise WatchLedgerError("a previous run's reservation cannot be resumed")
            if reservation.status == status:
                if status == "click_started":
                    raise WatchLedgerError("the send click already started; do not click again")
                if status == "sent" and reservation.evidence_refs != evidence_refs:
                    raise WatchLedgerError("sent evidence cannot be replaced")
                return reservation
            allowed = {"click_started": {"reserved"}, "sent": {"click_started"}, "uncertain": {"reserved", "retry_wait", "click_started"}, "not_sent": {"reserved", "retry_wait"}}
            if reservation.status not in allowed[status]:
                raise WatchLedgerError("invalid send state transition")
            if status == "sent":
                self._require_click_timing(reservation)
            if status == "click_started":
                event = self._event(connection, reservation.event_id)
                if proof is None or reservation.reply_sha256 is None or reservation.self_member_id is None:
                    raise WatchLedgerError("bound reply, self identity and a pre-click proof are required")
                _check_idle_proof(event, proof)
                if (proof.group_id != self.group_id or proof.self_member_id != reservation.self_member_id
                        or (proof.reservation_id is not None and proof.reservation_id != reservation.reservation_id)
                        or proof.reply_sha256 != reservation.reply_sha256
                        or not -2 <= (moment - proof.verified_at).total_seconds() <= 15
                        or (proof.source_record_ids and event.source_record_id not in proof.source_record_ids)):
                    raise WatchLedgerError("pre-click proof does not match the fresh intent")
                if reservation.deadline_at is None or moment >= reservation.deadline_at:
                    raise WatchLedgerError("event deadline expired; do not click")
                allowed_at = self._allowed_at(state, reservation.cooldown_seconds)
                if allowed_at is not None and moment < allowed_at:
                    raise WatchLedgerError("actual delivery cooldown is active; do not click")
                if event.stream_revision != state["revision"]:
                    raise WatchLedgerError("the visible stream changed after reservation; do not click")
                if reservation.attempt_count == 0:
                    self._begin_attempt(connection, reservation, moment)
                    reservation = self._reservation(connection, reservation_id)
                row = connection.execute("SELECT record_json FROM qq_watch_delivery_attempts WHERE reservation_id=? AND phase='prechecking'", (reservation_id,)).fetchone()
                if row is None:
                    raise WatchLedgerError("an active pre-click attempt is required")
                attempt = DeliveryAttempt.model_validate_json(row[0]).model_copy(update={"phase": "click_started", "updated_at": moment, "proof": proof})
                connection.execute("UPDATE qq_watch_delivery_attempts SET phase=?,record_json=? WHERE attempt_id=?", (attempt.phase, attempt.model_dump_json(), attempt.attempt_id))
            reservation = reservation.model_copy(update={"status": status, "updated_at": moment, "evidence_refs": evidence_refs or [], "reason": reason, "next_retry_at": None, "click_started_at": moment if status == "click_started" else reservation.click_started_at, "preclick_proof": proof or reservation.preclick_proof,
                "click_timing_required": require_click_timing if status == "click_started" else reservation.click_timing_required})
            self._save_send(connection, reservation)
            event = self._event(connection, reservation.event_id).model_copy(update={"state": status, "reason": reason})
            self._save_event(connection, event)
            if status in {"sent", "click_started"}:
                self._preserve_click_activity(connection, state, reservation)
            if status != "click_started":
                self._finish_attempt(connection, reservation_id, "cancelled" if status == "not_sent" else status, moment, reason)
            return reservation

    def mark_click_started(self, reservation_id: str, *, proof: PreClickProof | dict, now: datetime | None = None,
                           require_click_timing: bool = False) -> SendReservation:
        if type(require_click_timing) is not bool:
            raise WatchLedgerError("click timing policy must be explicit")
        return self._advance_send(reservation_id, "click_started", now=now, proof=PreClickProof.model_validate(proof),
                                  require_click_timing=require_click_timing)

    @staticmethod
    def _require_click_timing(reservation: SendReservation) -> None:
        if (reservation.click_timing_required and reservation.click_acknowledged_at is None
                and reservation.click_observed_at is None):
            raise WatchLedgerError("a conservative click upper bound is required")

    def _mark_click_bound(self, reservation_id: str, *, bound_at: datetime, observed: bool,
                         evidence_ref: str | None = None, now: datetime | None = None) -> SendReservation:
        if not isinstance(bound_at, datetime):
            raise WatchLedgerError("an explicit local click-bound timestamp is required")
        moment, bound = _now(now), _now(bound_at)
        if observed:
            evidence_ref = _identifier(evidence_ref)
        with self._transaction() as (connection, state):
            reservation = self._reservation(connection, reservation_id)
            if (reservation.click_started_at is None or bound < reservation.click_started_at
                    or bound > moment + timedelta(seconds=2)):
                raise WatchLedgerError("click upper bound must follow the attempt and not be in the future")
            if reservation.click_acknowledged_at is not None or reservation.click_observed_at is not None:
                return reservation  # Repeated acknowledgements/receipts cannot renew cadence.
            if reservation.run_id != self.run_id or reservation.status not in {"click_started", "uncertain"}:
                raise WatchLedgerError("only this run's possibly clicked intent can acquire a bound")
            updates = {"click_observed_at": bound, "click_observed_evidence_ref": evidence_ref} if observed else {"click_acknowledged_at": bound}
            reservation = reservation.model_copy(update=updates)
            self._save_send(connection, reservation)
            self._preserve_click_activity(connection, state, reservation)
            return reservation

    def mark_click_acknowledged(self, reservation_id: str, *, acknowledged_at: datetime,
                                now: datetime | None = None) -> SendReservation:
        """Persist the local successful action-response time, never receipt time."""
        return self._mark_click_bound(reservation_id, bound_at=acknowledged_at, observed=False, now=now)

    def mark_click_observed(self, reservation_id: str, *, observed_at: datetime, evidence_ref: str,
                           now: datetime | None = None) -> SendReservation:
        """Bind the local capture completion of a verified fresh outgoing bubble."""
        return self._mark_click_bound(reservation_id, bound_at=observed_at, observed=True,
                                     evidence_ref=evidence_ref, now=now)

    def record_postclick_evidence(self, reservation_id: str, *, evidence_refs: list[str]) -> SendReservation:
        """Retain current-run diagnostic reads without granting delivery authority.

        These references are separate from exact successful receipt evidence.
        In particular, recording a read cannot renew the uncertainty window or
        cooldown, establish an ACK, retry an action, or alter the pre-click proof.
        """
        if not isinstance(evidence_refs, list) or len(evidence_refs) > 32:
            raise WatchLedgerError("post-click diagnostics require at most 32 references per call")
        refs = sorted(set(_identifier(ref) for ref in evidence_refs))
        with self._transaction() as (connection, _state):
            reservation = self._reservation(connection, reservation_id)
            if reservation.segment_count:
                raise WatchLedgerError("a segmented parent must use its child delivery protocol")
            if (reservation.run_id != self.run_id or reservation.status not in {"click_started", "uncertain"}
                    or reservation.click_started_at is None):
                raise WatchLedgerError("only this run's possibly clicked intent can retain diagnostic evidence")
            merged = sorted(set(reservation.postclick_evidence_refs) | set(refs))
            if len(merged) > 128:
                raise WatchLedgerError("post-click diagnostic evidence limit exceeded")
            updated = reservation.model_copy(update={"postclick_evidence_refs": merged})
            if updated != reservation:
                self._save_send(connection, updated)
            return updated

    def mark_sent(self, reservation_id: str, *, evidence_refs: list[str], now: datetime | None = None) -> SendReservation:
        """Record caller-verified bubble evidence; never infer success from a click."""
        if not isinstance(evidence_refs, list) or not 1 <= len(evidence_refs) <= 32:
            raise WatchLedgerError("verified delivery requires 1 to 32 evidence references")
        refs = sorted(set(_identifier(ref) for ref in evidence_refs))
        return self._advance_send(reservation_id, "sent", now=now, evidence_refs=refs)

    def mark_uncertain(self, reservation_id: str, *, reason: str, now: datetime | None = None) -> SendReservation:
        return self._advance_send(reservation_id, "uncertain", now=now, reason=_identifier(reason))

    def mark_not_sent(self, reservation_id: str, *, reason: str, now: datetime | None = None) -> SendReservation:
        """Close a reservation only while no possible send click has started."""
        return self._advance_send(reservation_id, "not_sent", now=now, reason=_identifier(reason))

    def reconcile_sent(self, reservation_id: str, *, receipt, now: datetime | None = None) -> SendReservation:
        """Apply an already verified matching receipt, without delivery actions.

        The caller must load the receipt from the trusted delivery store. This
        method cannot upgrade missing/negative visual evidence into a receipt.
        Legacy reservations without reply/self bindings remain quarantined.
        """
        from harness.qq.decision import QQDeliveryReceipt

        receipt = QQDeliveryReceipt.model_validate(receipt)
        moment = _now(now)
        with self._transaction() as (connection, state):
            reservation = self._reservation(connection, reservation_id)
            event = self._event(connection, reservation.event_id, current_run=False)
            if reservation.status not in {"uncertain", "click_started", "sent"}:
                raise WatchLedgerError("only a possibly sent intent can be reconciled")
            expected_hash = hashlib.sha256(receipt.delivered_text.encode("utf-8")).hexdigest()
            if (reservation.reply_sha256 is None or reservation.self_member_id is None
                    or (reservation.preclick_proof is not None and reservation.preclick_proof.reservation_id is not None
                        and reservation.preclick_proof.reservation_id != reservation.reservation_id)
                    or receipt.group_id != self.group_id or receipt.decision_id != reservation.decision_id
                    or receipt.self_member_id != reservation.self_member_id or expected_hash != reservation.reply_sha256
                    or not set(address_sources_for_event(event)).issubset(receipt.source_record_ids)
                    or receipt.delivered_at < reservation.created_at or receipt.delivered_at > moment + timedelta(seconds=2)):
                raise WatchLedgerError("receipt does not match the original group, self, reply and source")
            allowed_sources = set(address_sources_for_event(event))
            if reservation.preclick_proof is not None:
                _check_idle_proof(event, reservation.preclick_proof)
                allowed_sources.update(reservation.preclick_proof.source_record_ids)
            elif event.origin == "homeostasis":
                raise WatchLedgerError("idle delivery requires the original scoped proof")
            if not set(receipt.source_record_ids).issubset(allowed_sources):
                raise WatchLedgerError("receipt addresses sources outside the original intent")
            self._require_click_timing(reservation)
            if reservation.receipt_id is not None:
                if reservation.receipt_id != receipt.receipt_id or reservation.evidence_refs != sorted(set(receipt.evidence_refs)):
                    raise WatchLedgerError("reconciled receipt cannot be replaced")
                return reservation
            if reservation.status == "sent" and reservation.evidence_refs != sorted(set(receipt.evidence_refs)):
                raise WatchLedgerError("sent evidence does not match the recorded receipt")
            reservation = reservation.model_copy(update={
                "status": "sent", "updated_at": receipt.delivered_at, "evidence_refs": sorted(set(receipt.evidence_refs)),
                "receipt_id": receipt.receipt_id, "reconciled_at": moment, "reason": "matched_verified_receipt",
                "next_retry_at": None,
            })
            self._save_send(connection, reservation)
            self._save_event(connection, event.model_copy(update={"state": "sent", "reason": "matched_verified_receipt"}))
            self._finish_attempt(connection, reservation_id, "sent", moment, "matched_verified_receipt")
            self._preserve_click_activity(connection, state, reservation)
            return reservation

    def get_send(self, reservation_id: str) -> SendReservation:
        return self.get_reservation(reservation_id)

    def maintain_legacy_step_conflict(self, reservation_id: str, *, proof: LegacyStepConflictRejection,
                                     legacy_build_hash: str, legacy_evidence_refs: list[str],
                                     now: datetime | None = None) -> SendReservation:
        """Explicit engineering maintenance of a proven rejected legacy request.

        Caller must hold the normal worker lock and verify absence of a delivery
        receipt in the trusted memory store. This never starts/resumes a run,
        retries a click, changes cooldown, clears drafts or rewrites attempts.
        The complete prior records are appended to the operator repair audit;
        acceptance rejects additions to that table during a formal run.
        """
        proof = LegacyStepConflictRejection.model_validate(proof)
        if (not isinstance(legacy_build_hash, str) or not legacy_build_hash.startswith("qq-unattended:")
                or len(legacy_build_hash) != 78
                or any(char not in "0123456789abcdef" for char in legacy_build_hash[14:])):
            raise WatchLedgerError("an explicit legacy build hash is required")
        if (not isinstance(legacy_evidence_refs, list) or not 1 <= len(legacy_evidence_refs) <= 16
                or any(not isinstance(ref, str) or not ref.startswith("evidence://sha256/")
                       or len(ref) != 82 or any(char not in "0123456789abcdef" for char in ref[18:])
                       for ref in legacy_evidence_refs)):
            raise WatchLedgerError("explicit legacy evidence digest references are required")
        moment = _now(now)
        expected_action = "qq-send-" + hashlib.sha256(reservation_id.encode("utf-8")).hexdigest()
        if proof.reservation_id != reservation_id or proof.attempted_action_id != expected_action:
            raise WatchLedgerError("legacy proof is not bound to this reservation")
        submitted = {"kind": proof.kind, "proof": proof.model_dump(mode="json"),
                     "legacy_build_hash": legacy_build_hash, "legacy_evidence_refs": sorted(set(legacy_evidence_refs))}
        repair_id = "qq-legacy-step-rejection:" + hashlib.sha256(reservation_id.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            reservation = self._reservation(connection, reservation_id)
            event = self._event(connection, reservation.event_id, current_run=False)
            if reservation.segment_count:
                raise WatchLedgerError("a segmented parent requires all segment receipts before completion")
            connection.execute("""CREATE TABLE IF NOT EXISTS qq_operator_delivery_repairs (
                repair_id TEXT PRIMARY KEY, original_reservation_id TEXT, state TEXT,
                updated_at TEXT, evidence_json TEXT)""")
            previous_repair = connection.execute("SELECT evidence_json FROM qq_operator_delivery_repairs WHERE repair_id=?", (repair_id,)).fetchone()
            if previous_repair is not None:
                saved = json.loads(previous_repair[0])
                if (saved.get("submission") != submitted or reservation.status != "not_sent"
                        or reservation.reason != "legacy_step_conflict_rejection"
                        or event.state != "failed" or reservation.receipt_id is not None):
                    raise WatchLedgerError("existing maintenance evidence cannot be replaced")
                return reservation
            if connection.execute("SELECT 1 FROM qq_operator_delivery_repairs WHERE original_reservation_id=?", (reservation_id,)).fetchone():
                raise WatchLedgerError("a reservation with prior operator repair cannot be maintained")
            before = reservation.preclick_proof
            journal = proof.journal
            if (reservation.status != "uncertain" or event.state != "uncertain"
                    or event.decision_id != reservation.decision_id or event.source_record_id is None
                    or reservation.click_started_at is None or before is None
                    or reservation.click_acknowledged_at is not None or reservation.click_observed_at is not None
                    or reservation.click_observed_evidence_ref is not None or reservation.receipt_id is not None
                    or reservation.self_member_id is None or reservation.reply_sha256 is None
                    or before.reservation_id != reservation_id or before.group_id != self.group_id
                    or before.self_member_id != reservation.self_member_id or before.reply_sha256 != reservation.reply_sha256
                    or before.body_instance_id != journal.body_instance_id
                    or before.environment_revision != journal.environment_revision
                    or journal.result.action_id == expected_action
                    or not journal.started_at <= journal.finished_at <= reservation.click_started_at - timedelta(seconds=10)
                    or not reservation.click_started_at <= proof.queried_at <= moment + timedelta(seconds=2)
                    or not -2 <= (moment - proof.queried_at).total_seconds() <= 30):
                raise WatchLedgerError("legacy step conflict does not prove this request was rejected")
            attempt_rows = connection.execute("SELECT record_json FROM qq_watch_delivery_attempts WHERE reservation_id=? ORDER BY attempt_number", (reservation_id,)).fetchall()
            attempts = [DeliveryAttempt.model_validate_json(row[0]) for row in attempt_rows]
            possibly_clicked = [attempt for attempt in attempts if attempt.phase == "uncertain"]
            if (len(possibly_clicked) != 1 or possibly_clicked[0].proof != before
                    or any(attempt.phase not in {"precheck_failed", "uncertain"} for attempt in attempts)
                    or len(attempts) != reservation.attempt_count):
                raise WatchLedgerError("legacy attempt history is incomplete or incompatible")
            old_send = connection.execute("SELECT record_json FROM qq_watch_sends WHERE reservation_id=?", (reservation_id,)).fetchone()[0]
            old_event = connection.execute("SELECT record_json FROM qq_watch_events WHERE event_id=?", (event.event_id,)).fetchone()[0]
            audit = {"schema_version": "qq-operator-maintenance-v1", "kind": proof.kind,
                     "submission": submitted, "previous_reservation_json": old_send,
                     "previous_event_json": old_event, "previous_attempt_json": [row[0] for row in attempt_rows],
                     "performed_at": moment.isoformat(), "outcome": "verified_not_sent",
                     "automatic_recovery": False, "delivery_receipt_created": False}
            connection.execute("INSERT INTO qq_operator_delivery_repairs(repair_id,original_reservation_id,state,updated_at,evidence_json) VALUES(?,?,?,?,?)", (
                repair_id, reservation_id, "legacy_step_conflict_not_sent", moment.isoformat(),
                json.dumps(audit, ensure_ascii=True, sort_keys=True)))
            ended = reservation.model_copy(update={"status": "not_sent", "updated_at": moment,
                "reason": "legacy_step_conflict_rejection", "next_retry_at": None})
            self._save_send(connection, ended)
            self._save_event(connection, event.model_copy(update={"state": "failed", "reason": "legacy_step_conflict_rejection"}))
            return ended

    def unsettled_sends(self) -> list[SendReservation]:
        with closing(self._connect()) as connection:
            return [SendReservation.model_validate_json(row[0]) for row in connection.execute(
                "SELECT record_json FROM qq_watch_sends WHERE group_id=? AND status IN ('reserved','retry_wait','click_started','uncertain')",
                (self.group_id,),
            )]

    def delivery_attempts(self, reservation_id: str) -> list[DeliveryAttempt]:
        with closing(self._connect()) as connection:
            self._reservation(connection, reservation_id)
            return [DeliveryAttempt.model_validate_json(row[0]) for row in connection.execute(
                "SELECT record_json FROM qq_watch_delivery_attempts WHERE reservation_id=? ORDER BY attempt_number", (reservation_id,),
            )]

    def has_uncertain_send(self) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute("SELECT 1 FROM qq_watch_sends WHERE group_id=? AND status='uncertain' LIMIT 1", (self.group_id,)).fetchone() is not None

    def get_event(self, event_id: str) -> WatchEvent:
        with closing(self._connect()) as connection:
            return self._event(connection, event_id, current_run=False)

    def get_reservation(self, reservation_id: str) -> SendReservation:
        with closing(self._connect()) as connection:
            return self._reservation(connection, reservation_id)

    def status(self) -> dict:
        """Metadata only: never expose visible text or secret configuration."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT run_id,baseline_required,cooldown_until,last_actual_activity_at FROM qq_watch_state WHERE singleton=1").fetchone()
            counts = {entry[0]: entry[1] for entry in connection.execute("SELECT status,COUNT(*) FROM qq_watch_sends GROUP BY status")}
            return {"group_id": self.group_id, "run_id": row["run_id"], "baseline_required": bool(row["baseline_required"]), "cooldown_until": row["cooldown_until"], "last_actual_activity_at": row["last_actual_activity_at"], "send_counts": counts, "has_uncertain_send": counts.get("uncertain", 0) > 0}
