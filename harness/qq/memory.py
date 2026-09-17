"""Evidence-backed QQ visual person context, without control or model calls.

Profiles record what an authenticated collector verified in QQ's UI. Claims
record what an attributed author said, never an independently verified fact.
Capture item identity is deliberately separate from QQ message identity.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal, TypeVar
import unicodedata
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator

from harness.qq.conversation import QQConversationID, conversation_scope
from harness.qq.context_window import WINDOW_CANDIDATE_LIMIT


QQID = Annotated[str, StringConstraints(strict=True, pattern=r"^[1-9][0-9]{0,19}$")]
Reference = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)]
Label = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
ObservedText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=32768)]
Evidence = Annotated[list[Reference], Field(min_length=1, max_length=16)]
ProtocolMessageID = Annotated[str, StringConstraints(strict=True, pattern=r"^-?[0-9]{1,20}$")]


def protocol_message_evidence_ref(group_id: str, member_id: str, message_id: str) -> str:
    """A protocol identity reference, separate from the retained JSON hash."""
    from pydantic import TypeAdapter

    scope = conversation_scope(group_id)
    member_id = TypeAdapter(QQID).validate_python(member_id)
    message_id = TypeAdapter(ProtocolMessageID).validate_python(message_id)
    if scope["kind"] == "private":
        if member_id not in {scope["self_member_id"], scope["peer_id"]}:
            raise ValueError("private message author is outside its conversation")
        return "qq-protocol-private-message:" + ":".join((
            scope["self_member_id"], scope["peer_id"], member_id, message_id))
    return "qq-protocol-message:" + ":".join((group_id, member_id, message_id))


def has_protocol_evidence(references) -> bool:
    return any(re.fullmatch(r"qq-protocol://sha256/[0-9a-f]{64}", reference) for reference in references)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def private_conversation_members(self):
        group_id = getattr(self, "group_id", None)
        if group_id is None:
            return self
        scope = conversation_scope(group_id)
        if scope["kind"] == "private":
            allowed = {scope["self_member_id"], scope["peer_id"]}
            members = [getattr(self, "member_id", None)]
            members += list(getattr(self, "participants", None) or [])
            members += list(getattr(self, "member_ids", None) or [])
            if any(member is not None and member not in allowed for member in members):
                raise ValueError("private conversation has only its self account and peer")
            for field in ("verification", "author_attribution"):
                value = getattr(self, field, None)
                if value is not None and value != "qq_protocol_verified":
                    raise ValueError("private conversation requires protocol verification")
        return self

    @field_validator("*", mode="before")
    @classmethod
    def no_blank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("blank strings are not evidence or identifiers")
        if isinstance(value, list) and any(isinstance(item, str) and not item.strip() for item in value):
            raise ValueError("blank evidence references are not allowed")
        return value


class QQVerifiedProfileInput(_Model):
    group_id: QQConversationID
    member_id: QQID
    display_name: Label
    display_name_source: Literal["qq_profile", "qq_group_card", "qq_member_list", "qq_metadata"]
    verification: Literal["qq_ui_verified", "qq_protocol_verified"]
    capture_id: Reference
    observed_at: AwareDatetime
    # These may refer to separate screenshots of the group and member profile.
    group_evidence: Evidence
    member_evidence: Evidence

    @model_validator(mode="after")
    def verification_source(self):
        if self.verification == "qq_protocol_verified":
            if (self.display_name_source != "qq_metadata" or not has_protocol_evidence(self.group_evidence)
                    or not has_protocol_evidence(self.member_evidence)):
                raise ValueError("protocol profile requires retained QQ metadata evidence")
        elif self.display_name_source == "qq_metadata":
            raise ValueError("QQ protocol metadata is not UI verification")
        return self

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class QQVerifiedProfile(QQVerifiedProfileInput):
    record_id: str
    stored_at: AwareDatetime


class QQAuthorClaim(_Model):
    kind: Literal["unverified_author_assertion"] = "unverified_author_assertion"
    statement: ObservedText
    supporting_quote: ObservedText
    subject: Literal["self", "other", "account_relationship", "unspecified"] = "unspecified"
    subject_label: Label | None = None


class QQImageAnalysis(_Model):
    """A bounded model interpretation of attached pixels, never author speech."""

    authority: Literal["model_inference"] = "model_inference"
    image_index: int = Field(ge=1, le=8, strict=True)
    status: Literal["described", "unknown"]
    evidence_ref: Annotated[str, StringConstraints(pattern=r"^qq-direct-image:sha256:[0-9a-f]{64}$")] | None = None
    analysis_ref: Annotated[str, StringConstraints(pattern=r"^qq-image-analysis:sha256:[0-9a-f]{64}$")] | None = None
    description: Annotated[str, StringConstraints(max_length=6000)] | None = None
    ocr_text: Annotated[str, StringConstraints(max_length=6000)] | None = None
    uncertainties: list[Annotated[str, StringConstraints(max_length=512)]] = Field(default_factory=list, max_length=16)
    error: Literal["download_failed", "unsupported_media", "vision_failed", "deadline_expired",
                   "invalid_image", "image_too_large", "unavailable"] | None = None

    @model_validator(mode="after")
    def inference_evidence(self):
        if self.status == "described":
            if self.evidence_ref is None or not (self.description or self.ocr_text) or self.error is not None:
                raise ValueError("described image requires retained pixels and an interpretation")
        elif self.description is not None or self.ocr_text is not None:
            raise ValueError("unknown image cannot claim a model description")
        return self


class QQVisualObservationInput(_Model):
    group_id: QQConversationID
    member_id: QQID
    profile_record_id: Reference
    capture_id: Reference
    capture_item_id: Label
    observed_at: AwareDatetime
    text: ObservedText
    evidence: Evidence
    # Verifying a profile alone does not prove who authored a visible bubble.
    author_attribution: Literal["qq_ui_verified", "qq_protocol_verified"]
    attribution_evidence: Evidence
    claims: list[QQAuthorClaim] = Field(default_factory=list, max_length=16)
    extraction_method: Label = "manual"
    message_id: Label | None = None
    message_id_source: Literal["qq_metadata", "qq_visible_identifiers"] | None = None
    image_analysis: list[QQImageAnalysis] = Field(default_factory=list, max_length=8)

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def check_sources(self):
        if (self.message_id is None) != (self.message_id_source is None):
            raise ValueError("a real QQ message_id and its source must be supplied together, or both omitted")
        if self.message_id is not None:
            candidate = self.message_id.casefold()
            if candidate in {"unknown", "none", "null", "missing", "unavailable", "n/a", "?"}:
                raise ValueError("unknown message_id must be null")
            if candidate.startswith(("capture:", "screenshot:", "sha256:", "ocr:")):
                raise ValueError("capture fingerprints cannot be used as QQ message identifiers")
            if any(char.isspace() or not char.isprintable() for char in self.message_id):
                raise ValueError("message_id cannot contain whitespace or control characters")
        if any(claim.supporting_quote not in self.text for claim in self.claims):
            raise ValueError("every claim must quote the observed text; interpretation is still unverified")
        if len({item.image_index for item in self.image_analysis}) != len(self.image_analysis):
            raise ValueError("each attached image has one interpretation slot")
        if any(item.evidence_ref is not None and item.evidence_ref not in self.evidence for item in self.image_analysis):
            raise ValueError("image interpretation must retain its original pixel evidence")
        if self.author_attribution == "qq_protocol_verified":
            if (self.message_id_source != "qq_metadata" or self.message_id is None
                    or not has_protocol_evidence(self.evidence) or not has_protocol_evidence(self.attribution_evidence)
                    or protocol_message_evidence_ref(self.group_id, self.member_id, self.message_id)
                    not in self.attribution_evidence):
                raise ValueError("protocol author attribution requires bound message metadata evidence")
        return self


class QQVisualObservation(QQVisualObservationInput):
    record_id: str
    stored_at: AwareDatetime
    deduplication_scope: Literal["capture_item_only"] = "capture_item_only"


class QQProfileWriteResult(_Model):
    created: bool
    profile: QQVerifiedProfile


class QQVisualWriteResult(_Model):
    created: bool
    observation: QQVisualObservation


class QQPersonContext(_Model):
    group_id: QQConversationID
    member_id: QQID
    authority: Literal["untrusted_qq_observations"] = "untrusted_qq_observations"
    profiles: list[QQVerifiedProfile]
    observations: list[QQVisualObservation]


class _ContextQuery(_Model):
    group_id: QQConversationID
    member_id: QQID
    limit: int = Field(default=20, ge=1, le=100, strict=True)


class QQHistoryMatch(_Model):
    record_id: str
    reason: Literal["query_substring", "earliest_history"]
    score: int
    matched_terms: list[str]
    matched_fields: list[str]


class QQMemberRetrieval(_Model):
    member_id: QQID
    total_observations: int
    recent_record_ids: list[str]
    history_record_ids: list[str]
    history_matches: list[QQHistoryMatch]
    history_candidate_count: int
    history_match_count: int
    history_candidates_scanned: int
    candidate_scan_truncated: Literal[False] = False
    recent_window_truncated: bool
    history_truncated: bool
    observations_omitted_count: int


class QQGroupRetrieval(_Model):
    query: str | None
    terms: list[str]
    terms_truncated: bool
    history_selection: Literal["query_substring_rank", "earliest_outside_recent", "chronological_append"]
    recent_selection: Literal["observed_at_then_stored_at_desc"] = "observed_at_then_stored_at_desc"
    member_selection: Literal["verified_numeric_member_id_order", "explicit_member_id_order"]
    group_known_member_count: int
    candidate_member_count: int
    missing_member_ids: list[QQID]
    members_truncated: bool
    omitted_member_count: int
    max_members: int
    recent_limit_per_member: int
    history_limit_per_member: int
    window_mode: Literal["balanced_recent_history", "chronological_append"] = "balanced_recent_history"


class QQGroupContext(_Model):
    group_id: QQConversationID
    authority: Literal["untrusted_qq_observations"] = "untrusted_qq_observations"
    participants: list[QQID]
    members: list[QQPersonContext]
    retrieval: QQGroupRetrieval
    member_retrieval: list[QQMemberRetrieval]


class _GroupContextQuery(_Model):
    group_id: QQConversationID
    member_ids: list[QQID] | None = Field(default=None, max_length=100)
    query: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)] | None = None
    max_members: int = Field(default=20, ge=1, le=100, strict=True)
    recent_limit_per_member: int = Field(default=5, ge=0, le=50, strict=True)
    history_limit_per_member: int = Field(default=5, ge=0, le=50, strict=True)
    window_mode: Literal["balanced_recent_history", "chronological_append"] = "balanced_recent_history"

    @field_validator("member_ids")
    @classmethod
    def distinct_members(cls, value: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(value)) if value is not None else None

    @model_validator(mode="after")
    def nonempty_budget(self):
        if self.window_mode == "chronological_append":
            return self
        if self.recent_limit_per_member + self.history_limit_per_member == 0:
            raise ValueError("at least one observation budget must be positive")
        return self


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _query_terms(query: str | None) -> tuple[list[str], bool]:
    if query is None:
        return [], False
    terms: list[str] = []
    # Exact Chinese runs plus overlapping two-character terms need no tokenizer
    # model; Latin words and numbers are literal terms. This is OR matching.
    for token in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+", _normalized_text(query)):
        terms.append(token)
        if re.fullmatch(r"[\u3400-\u9fff]+", token) and len(token) > 2:
            terms.extend(token[index:index + 2] for index in range(len(token) - 1))
    terms = list(dict.fromkeys(terms))
    if not terms:
        raise ValueError("query must contain a searchable Chinese character, word, or number")
    return terms[:64], len(terms) > 64


def _history_match(observation: QQVisualObservation, terms: list[str]) -> QQHistoryMatch:
    fields = {
        "text": _normalized_text(observation.text),
        "claims.statement": [_normalized_text(claim.statement) for claim in observation.claims],
        "claims.supporting_quote": [_normalized_text(claim.supporting_quote) for claim in observation.claims],
        "claims.subject_label": [_normalized_text(claim.subject_label) for claim in observation.claims if claim.subject_label],
    }
    matched: list[str] = []
    locations: set[str] = set()
    for term in terms:
        found = [name for name, text in fields.items()
                 if (term in text if isinstance(text, str) else any(term in item for item in text))]
        if found:
            matched.append(term)
            locations.update(found)
    return QQHistoryMatch(
        record_id=observation.record_id, reason="query_substring", score=sum(min(len(term), 8) for term in matched),
        matched_terms=matched, matched_fields=sorted(locations),
    )


class QQVisualMemoryConflict(ValueError):
    """A capture item was resubmitted with inconsistent evidence or attribution."""


class QQVisualMemoryStorageError(RuntimeError):
    """Persisted visual memory cannot be decoded safely."""


Record = TypeVar("Record", bound=BaseModel)


class QQVisualMemoryStore:
    """Durable capture-level observations; no alias resolution or message dedup.

    Callers explicitly fetch reply_context and decide how to use its untrusted
    data. Nothing is automatically sent to Runtime, Cognitive, or a QQ window.
    """

    def __init__(self, path: Path | str):
        if str(path) == ":memory:":
            raise ValueError("visual person memory requires a persistent SQLite path")
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS qq_visual_profiles (
                    record_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL, member_id TEXT NOT NULL,
                    capture_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    UNIQUE (group_id, member_id, capture_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS qq_visual_observations (
                    record_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL, member_id TEXT NOT NULL,
                    capture_id TEXT NOT NULL, capture_item_id TEXT NOT NULL,
                    profile_record_id TEXT NOT NULL REFERENCES qq_visual_profiles(record_id),
                    observed_at TEXT NOT NULL, record_json TEXT NOT NULL,
                    UNIQUE (group_id, capture_id, capture_item_id)
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS qq_visual_person_context
                   ON qq_visual_observations (group_id, member_id, observed_at DESC)"""
            )
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _decode(model: type[Record], value: str) -> Record:
        try:
            return model.model_validate_json(value)
        except ValidationError as exc:
            raise QQVisualMemoryStorageError("persisted QQ visual memory is invalid") from exc

    @staticmethod
    def _same_capture_input(original: BaseModel, incoming: BaseModel) -> bool:
        # Reprocessing time is not a new observation. All source evidence,
        # attribution, and extracted claims must remain consistent for this key.
        expected = original.model_dump(include=set(type(incoming).model_fields), exclude={"observed_at"})
        actual = incoming.model_dump(exclude={"observed_at"})
        for key in ("group_evidence", "member_evidence", "evidence", "attribution_evidence"):
            if key in expected:
                expected[key] = sorted(set(expected[key]))
                actual[key] = sorted(set(actual[key]))
        return expected == actual

    def record_profile(self, incoming: QQVerifiedProfileInput) -> QQProfileWriteResult:
        if incoming.verification != "qq_ui_verified":
            raise ValueError("UI collection cannot assert protocol verification")
        return self._record_profile(incoming)

    def record_protocol_profile(self, incoming: QQVerifiedProfileInput) -> QQProfileWriteResult:
        if incoming.verification != "qq_protocol_verified":
            raise ValueError("protocol collection requires protocol verification")
        return self._record_profile(incoming)

    def _record_profile(self, incoming: QQVerifiedProfileInput) -> QQProfileWriteResult:
        incoming = QQVerifiedProfileInput.model_validate(incoming.model_dump())
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._write_profile(incoming, connection)

    def _write_profile(self, incoming: QQVerifiedProfileInput, connection) -> QQProfileWriteResult:
        row = connection.execute(
            "SELECT record_json FROM qq_visual_profiles WHERE group_id = ? AND member_id = ? AND capture_id = ?",
            (incoming.group_id, incoming.member_id, incoming.capture_id),
        ).fetchone()
        if row is not None:
            profile = self._decode(QQVerifiedProfile, row["record_json"])
            if not self._same_capture_input(profile, incoming):
                raise QQVisualMemoryConflict("the profile capture already has different content or evidence")
            return QQProfileWriteResult(created=False, profile=profile)
        profile = QQVerifiedProfile(
            **incoming.model_dump(), record_id=f"qq-profile:{uuid4().hex}", stored_at=datetime.now(timezone.utc)
        )
        connection.execute(
            "INSERT INTO qq_visual_profiles VALUES (?, ?, ?, ?, ?, ?)",
            (profile.record_id, profile.group_id, profile.member_id, profile.capture_id,
             profile.observed_at.isoformat(), profile.model_dump_json()),
        )
        return QQProfileWriteResult(created=True, profile=profile)

    def record_observation(self, incoming: QQVisualObservationInput) -> QQVisualWriteResult:
        if incoming.author_attribution != "qq_ui_verified":
            raise ValueError("UI collection cannot assert protocol attribution")
        return self._record_observation(incoming)

    def record_protocol_observation(self, incoming: QQVisualObservationInput) -> QQVisualWriteResult:
        if incoming.author_attribution != "qq_protocol_verified":
            raise ValueError("protocol collection requires protocol attribution")
        return self._record_observation(incoming)

    def record_protocol_message(self, profile_input, observation_factory):
        """Commit one message's fresh identity proof and observation together."""
        profile_input = QQVerifiedProfileInput.model_validate(profile_input.model_dump())
        if profile_input.verification != "qq_protocol_verified":
            raise ValueError("protocol collection requires protocol verification")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            profile = self._write_profile(profile_input, connection)
            incoming = observation_factory(profile.profile)
            incoming = QQVisualObservationInput.model_validate(incoming.model_dump())
            if (incoming.author_attribution != "qq_protocol_verified"
                    or incoming.group_id != profile.profile.group_id
                    or incoming.member_id != profile.profile.member_id
                    or incoming.profile_record_id != profile.profile.record_id):
                raise ValueError("protocol message requires its own exact profile")
            observation = self._write_observation(incoming, connection)
            return profile, observation

    def _record_observation(self, incoming: QQVisualObservationInput) -> QQVisualWriteResult:
        incoming = QQVisualObservationInput.model_validate(incoming.model_dump())
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._write_observation(incoming, connection)

    def _write_observation(self, incoming: QQVisualObservationInput, connection) -> QQVisualWriteResult:
        row = connection.execute(
            "SELECT record_json FROM qq_visual_observations WHERE group_id = ? AND capture_id = ? AND capture_item_id = ?",
            (incoming.group_id, incoming.capture_id, incoming.capture_item_id),
        ).fetchone()
        if row is not None:
            observation = self._decode(QQVisualObservation, row["record_json"])
            if not self._same_capture_input(observation, incoming):
                raise QQVisualMemoryConflict("the capture item already has different content, attribution, or evidence")
            return QQVisualWriteResult(created=False, observation=observation)
        profile_row = connection.execute(
            "SELECT record_json FROM qq_visual_profiles WHERE record_id = ? AND group_id = ? AND member_id = ?",
            (incoming.profile_record_id, incoming.group_id, incoming.member_id),
        ).fetchone()
        if profile_row is None:
            raise ValueError("observation requires a stored verified profile for this exact group and member")
        profile = self._decode(QQVerifiedProfile, profile_row["record_json"])
        if profile.verification != incoming.author_attribution:
            raise ValueError("observation and profile verification transports must agree")
        observation = QQVisualObservation(
            **incoming.model_dump(), record_id=f"qq-visual:{uuid4().hex}", stored_at=datetime.now(timezone.utc)
        )
        connection.execute(
            "INSERT INTO qq_visual_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (observation.record_id, observation.group_id, observation.member_id,
             observation.capture_id, observation.capture_item_id, observation.profile_record_id,
             observation.observed_at.isoformat(), observation.model_dump_json()),
        )
        return QQVisualWriteResult(created=True, observation=observation)

    def reply_context(self, *, group_id: str, member_id: str, limit: int = 20) -> QQPersonContext:
        query = _ContextQuery(group_id=group_id, member_id=member_id, limit=limit)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            profiles = connection.execute(
                """SELECT record_json FROM qq_visual_profiles WHERE group_id = ? AND member_id = ?
                   ORDER BY observed_at DESC,
                     CASE WHEN json_valid(record_json) THEN json_extract(record_json, '$.stored_at') ELSE '' END DESC,
                     record_id DESC LIMIT ?""",
                (query.group_id, query.member_id, query.limit),
            ).fetchall()
            observations = connection.execute(
                """SELECT record_json FROM qq_visual_observations WHERE group_id = ? AND member_id = ?
                   ORDER BY observed_at DESC,
                     CASE WHEN json_valid(record_json) THEN json_extract(record_json, '$.stored_at') ELSE '' END DESC,
                     record_id DESC LIMIT ?""",
                (query.group_id, query.member_id, query.limit),
            ).fetchall()
            decoded_profiles = [self._decode(QQVerifiedProfile, row["record_json"]) for row in profiles]
            decoded_observations = [self._decode(QQVisualObservation, row["record_json"]) for row in observations]
            present = {profile.record_id for profile in decoded_profiles}
            # Keep the identity evidence referenced by every returned observation,
            # even when later profile snapshots pushed it outside the recent limit.
            missing = {item.profile_record_id for item in decoded_observations} - present
            for record_id in sorted(missing):
                row = connection.execute(
                    "SELECT record_json FROM qq_visual_profiles WHERE record_id = ? AND group_id = ? AND member_id = ?",
                    (record_id, query.group_id, query.member_id),
                ).fetchone()
                if row is None:
                    raise QQVisualMemoryStorageError("observation identity evidence is missing or outside its scope")
                decoded_profiles.append(self._decode(QQVerifiedProfile, row["record_json"]))
        for record in [*decoded_profiles, *decoded_observations]:
            if record.group_id != query.group_id or record.member_id != query.member_id:
                raise QQVisualMemoryStorageError("persisted QQ visual memory disagrees with its identity scope")
        return QQPersonContext(
            group_id=query.group_id, member_id=query.member_id,
            profiles=decoded_profiles,
            observations=decoded_observations,
        )

    def group_context(
        self,
        *,
        group_id: str,
        member_ids: list[str] | None = None,
        query: str | None = None,
        max_members: int = 20,
        recent_limit_per_member: int = 5,
        history_limit_per_member: int = 5,
        window_mode: str = "balanced_recent_history",
    ) -> QQGroupContext:
        """Read balanced recent/history context from one consistent SQLite snapshot.

        Each selected member's entire stored history is scanned. Result budgets
        never restrict the candidates searched; scans are streamed and only the
        best bounded history results are retained. No query means earliest
        observations outside the recent window, not semantic relevance.
        """
        request = _GroupContextQuery(
            group_id=group_id, member_ids=member_ids, query=query, max_members=max_members,
            recent_limit_per_member=recent_limit_per_member,
            history_limit_per_member=history_limit_per_member,
            window_mode=window_mode,
        )
        if request.window_mode == "chronological_append":
            return self._chronological_group_context(request)
        terms, terms_truncated = _query_terms(request.query)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            group_count = connection.execute(
                "SELECT COUNT(DISTINCT member_id) FROM qq_visual_profiles WHERE group_id = ?",
                (request.group_id,),
            ).fetchone()[0]
            missing: list[str] = []
            if request.member_ids is None:
                # Decimal-string ordering works beyond SQLite's signed int64.
                selected = [row[0] for row in connection.execute(
                    """SELECT DISTINCT member_id FROM qq_visual_profiles WHERE group_id = ?
                       ORDER BY LENGTH(member_id), member_id LIMIT ?""",
                    (request.group_id, request.max_members),
                )]
                candidate_count = group_count
            elif not request.member_ids:
                selected, candidate_count = [], 0
            else:
                placeholders = ",".join("?" for _ in request.member_ids)
                known = {row[0] for row in connection.execute(
                    f"SELECT DISTINCT member_id FROM qq_visual_profiles WHERE group_id = ? AND member_id IN ({placeholders})",
                    (request.group_id, *request.member_ids),
                )}
                missing = [member for member in request.member_ids if member not in known]
                eligible = [member for member in request.member_ids if member in known]
                selected, candidate_count = eligible[:request.max_members], len(eligible)
            members: list[QQPersonContext] = []
            member_retrieval: list[QQMemberRetrieval] = []
            for member_id in selected:
                person, retrieval = self._group_member_context(connection, request, member_id, terms)
                members.append(person)
                member_retrieval.append(retrieval)
        return QQGroupContext(
            group_id=request.group_id, participants=selected, members=members,
            retrieval=QQGroupRetrieval(
                query=request.query, terms=terms, terms_truncated=terms_truncated,
                history_selection="query_substring_rank" if request.query is not None else "earliest_outside_recent",
                member_selection="verified_numeric_member_id_order" if request.member_ids is None else "explicit_member_id_order",
                group_known_member_count=group_count, candidate_member_count=candidate_count,
                missing_member_ids=missing, members_truncated=candidate_count > len(selected),
                omitted_member_count=candidate_count - len(selected), max_members=request.max_members,
                recent_limit_per_member=request.recent_limit_per_member,
                history_limit_per_member=request.history_limit_per_member,
                window_mode="balanced_recent_history",
            ),
            member_retrieval=member_retrieval,
        )

    def _chronological_group_context(self, request: _GroupContextQuery) -> QQGroupContext:
        terms, terms_truncated = ([], False) if request.query is None else _query_terms(request.query)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            group_count = connection.execute(
                "SELECT COUNT(DISTINCT member_id) FROM qq_visual_profiles WHERE group_id = ?",
                (request.group_id,),
            ).fetchone()[0]
            rows = connection.execute(
                """SELECT record_json FROM qq_visual_observations WHERE group_id = ?
                   ORDER BY observed_at DESC,
                   CASE WHEN json_valid(record_json)
                        THEN REPLACE(json_extract(record_json, '$.stored_at'), 'Z', '+00:00') ELSE '' END DESC,
                   record_id DESC LIMIT ?""",
                (request.group_id, WINDOW_CANDIDATE_LIMIT),
            )
            newest_first = [self._decode(QQVisualObservation, row["record_json"]) for row in rows]
            observations = list(reversed(newest_first))
            present_ids = {item.record_id for item in observations}
            extra_matches: list[tuple[QQVisualObservation, QQHistoryMatch]] = []
            if terms:
                scanned = connection.execute(
                    """SELECT record_json FROM qq_visual_observations WHERE group_id = ?
                       ORDER BY observed_at DESC,
                       CASE WHEN json_valid(record_json)
                            THEN REPLACE(json_extract(record_json, '$.stored_at'), 'Z', '+00:00') ELSE '' END DESC,
                       record_id DESC""",
                    (request.group_id,),
                )
                for row in scanned:
                    observation = self._decode(QQVisualObservation, row["record_json"])
                    if observation.record_id in present_ids:
                        continue
                    match = _history_match(observation, terms)
                    if not match.matched_terms:
                        continue
                    extra_matches.append((observation, match))
                    extra_matches.sort(key=lambda item: (
                        item[1].score, item[0].observed_at, item[0].stored_at, item[0].record_id
                    ), reverse=True)
                    del extra_matches[request.history_limit_per_member:]
            hint_observations = [item[0] for item in extra_matches]
            by_member: dict[str, list[QQVisualObservation]] = {}
            for observation in [*observations, *hint_observations]:
                by_member.setdefault(observation.member_id, []).append(observation)
            selected = sorted(by_member, key=lambda member_id: (len(member_id), member_id))[:request.max_members]
            if request.member_ids is not None:
                requested = list(dict.fromkeys(request.member_ids))
                selected = [member for member in requested if member in by_member][:request.max_members]
            members: list[QQPersonContext] = []
            member_retrieval: list[QQMemberRetrieval] = []
            window_ids = {item.record_id for item in observations}
            for member_id in selected:
                person_obs = by_member[member_id]
                matches = []
                if terms:
                    matches = [match for observation, match in (
                        (item, _history_match(item, terms)) for item in person_obs
                    ) if match.matched_terms]
                person, retrieval = self._profiles_for_observations(
                    connection, request, member_id, person_obs,
                    window_ids=window_ids, matches=matches, total=len(person_obs),
                )
                members.append(person)
                member_retrieval.append(retrieval)
            missing = [] if request.member_ids is None else [member for member in request.member_ids if member not in by_member]
            candidate_count = len(by_member)
        return QQGroupContext(
            group_id=request.group_id, participants=selected, members=members,
            retrieval=QQGroupRetrieval(
                query=request.query, terms=terms, terms_truncated=terms_truncated,
                history_selection="chronological_append",
                member_selection="verified_numeric_member_id_order" if request.member_ids is None else "explicit_member_id_order",
                group_known_member_count=group_count, candidate_member_count=candidate_count,
                missing_member_ids=missing, members_truncated=candidate_count > len(selected),
                omitted_member_count=max(0, candidate_count - len(selected)), max_members=request.max_members,
                recent_limit_per_member=request.recent_limit_per_member,
                history_limit_per_member=request.history_limit_per_member,
                window_mode="chronological_append",
            ),
            member_retrieval=member_retrieval,
        )

    def _profiles_for_observations(
        self,
        connection: sqlite3.Connection,
        request: _GroupContextQuery,
        member_id: str,
        observations: list[QQVisualObservation],
        *,
        window_ids: set[str],
        matches: list[QQHistoryMatch],
        total: int,
    ) -> tuple[QQPersonContext, QQMemberRetrieval]:
        latest_profile_row = connection.execute(
            """SELECT record_json FROM qq_visual_profiles WHERE group_id = ? AND member_id = ?
               ORDER BY observed_at DESC,
               CASE WHEN json_valid(record_json)
                    THEN REPLACE(json_extract(record_json, '$.stored_at'), 'Z', '+00:00') ELSE '' END DESC,
               record_id DESC LIMIT 1""",
            (request.group_id, member_id),
        ).fetchone()
        if latest_profile_row is None:
            raise QQVisualMemoryStorageError("selected member has no verified profile")
        profiles = [self._decode(QQVerifiedProfile, latest_profile_row["record_json"])]
        present = {profiles[0].record_id}
        for record_id in sorted({item.profile_record_id for item in observations} - present):
            row = connection.execute(
                "SELECT record_json FROM qq_visual_profiles WHERE record_id = ? AND group_id = ? AND member_id = ?",
                (record_id, request.group_id, member_id),
            ).fetchone()
            if row is None:
                raise QQVisualMemoryStorageError("observation identity evidence is missing or outside its scope")
            profiles.append(self._decode(QQVerifiedProfile, row["record_json"]))
        recent_ids = [item.record_id for item in observations if item.record_id in window_ids]
        history_ids = [item.record_id for item in observations if item.record_id not in window_ids]
        return (
            QQPersonContext(group_id=request.group_id, member_id=member_id, profiles=profiles, observations=observations),
            QQMemberRetrieval(
                member_id=member_id, total_observations=total,
                recent_record_ids=recent_ids, history_record_ids=history_ids,
                history_matches=matches, history_candidate_count=len(history_ids),
                history_match_count=len(matches), history_candidates_scanned=len(history_ids),
                recent_window_truncated=False, history_truncated=False,
                observations_omitted_count=0,
            ),
        )

    def _group_member_context(
        self,
        connection: sqlite3.Connection,
        request: _GroupContextQuery,
        member_id: str,
        terms: list[str],
    ) -> tuple[QQPersonContext, QQMemberRetrieval]:
        recent: list[QQVisualObservation] = []
        history: list[tuple[QQVisualObservation, QQHistoryMatch]] = []
        total = candidate_count = match_count = 0
        rows = connection.execute(
            """SELECT record_json FROM qq_visual_observations
               WHERE group_id = ? AND member_id = ?
               ORDER BY observed_at DESC,
               CASE WHEN json_valid(record_json)
                    THEN REPLACE(json_extract(record_json, '$.stored_at'), 'Z', '+00:00') ELSE '' END DESC,
               record_id DESC""",
            (request.group_id, member_id),
        )
        for row in rows:
            observation = self._decode(QQVisualObservation, row["record_json"])
            if observation.group_id != request.group_id or observation.member_id != member_id:
                raise QQVisualMemoryStorageError("persisted observation disagrees with its group/member scope")
            total += 1
            if len(recent) < request.recent_limit_per_member:
                recent.append(observation)
                continue
            candidate_count += 1
            if request.query is not None:
                match = _history_match(observation, terms)
                if not match.matched_terms:
                    continue
            else:
                match = QQHistoryMatch(
                    record_id=observation.record_id, reason="earliest_history", score=0,
                    matched_terms=[], matched_fields=[],
                )
            match_count += 1
            if request.history_limit_per_member:
                history.append((observation, match))
                if request.query is not None:
                    history.sort(key=lambda item: (
                        item[1].score, item[0].observed_at, item[0].stored_at, item[0].record_id
                    ), reverse=True)
                else:
                    history.sort(key=lambda item: (item[0].observed_at, item[0].stored_at, item[0].record_id))
                del history[request.history_limit_per_member:]
        observations = [*recent, *(item[0] for item in history)]
        latest_profile_row = connection.execute(
            """SELECT record_json FROM qq_visual_profiles WHERE group_id = ? AND member_id = ?
               ORDER BY observed_at DESC,
               CASE WHEN json_valid(record_json)
                    THEN REPLACE(json_extract(record_json, '$.stored_at'), 'Z', '+00:00') ELSE '' END DESC,
               record_id DESC LIMIT 1""",
            (request.group_id, member_id),
        ).fetchone()
        if latest_profile_row is None:
            raise QQVisualMemoryStorageError("selected member has no verified profile")
        profiles = [self._decode(QQVerifiedProfile, latest_profile_row["record_json"])]
        present = {profiles[0].record_id}
        for record_id in sorted({item.profile_record_id for item in observations} - present):
            row = connection.execute(
                "SELECT record_json FROM qq_visual_profiles WHERE record_id = ? AND group_id = ? AND member_id = ?",
                (record_id, request.group_id, member_id),
            ).fetchone()
            if row is None:
                raise QQVisualMemoryStorageError("observation identity evidence is missing or outside its scope")
            profiles.append(self._decode(QQVerifiedProfile, row["record_json"]))
        if any(profile.group_id != request.group_id or profile.member_id != member_id for profile in profiles):
            raise QQVisualMemoryStorageError("persisted profile disagrees with its group/member scope")
        return (
            QQPersonContext(group_id=request.group_id, member_id=member_id, profiles=profiles, observations=observations),
            QQMemberRetrieval(
                member_id=member_id, total_observations=total,
                recent_record_ids=[item.record_id for item in recent],
                history_record_ids=[item[0].record_id for item in history],
                history_matches=[item[1] for item in history], history_candidate_count=candidate_count,
                history_match_count=match_count, history_candidates_scanned=candidate_count,
                recent_window_truncated=total > len(recent), history_truncated=match_count > len(history),
                observations_omitted_count=total - len(observations),
            ),
        )
