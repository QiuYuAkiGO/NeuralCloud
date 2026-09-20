"""Transactional private sensor journal with deterministic restart and deduplication."""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Self

from harness.contracts.body import BodyStateSnapshot as WireBodyState
from harness.embodiment.contracts import BodyStateSnapshot as ProviderBodyState
from harness.research.grounded_agency.hashes import canonical_json, content_hash
from harness.research.grounded_agency.manifest import GroundedAgencyCondition
from harness.research.interoception.contracts import (
    BodyTruthReference,
    OpaqueInteroceptionEnvelope,
    OpaqueInteroceptiveObservation,
    SensorTransformRecord,
)
from harness.research.interoception.renderer import bounded_observations
from harness.research.interoception.transforms import OpaqueSensorTransform, _aware


class OpaqueInteroceptionStore:
    """Keep truth references and replay records private; release symbols when due.

    A database is bound to one complete condition and sensor configuration. A
    duplicate revision returns its original envelope; conflicting truth or stale
    revisions fail before writing. SQLite transactions allocate sample sequences
    atomically, including when more than one store opens the same journal.
    """

    def __init__(
        self, path: str | Path, condition: GroundedAgencyCondition, *,
        history_limit: int = 6, max_tokens: int = 256, bin_count: int = 9,
    ) -> None:
        self.condition = GroundedAgencyCondition.model_validate(condition.model_dump(mode="python"))
        if self.condition.interoception_treatment not in {"opaque_current", "opaque_history", "semantic_summary"} or self.condition.sensor_transform is None:
            raise ValueError("opaque store requires an opaque condition with a sensor transform")
        if isinstance(history_limit, bool) or not isinstance(history_limit, int) or not 1 <= history_limit <= 64:
            raise ValueError("history_limit must be an integer from 1 to 64")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 128:
            raise ValueError("max_tokens must be an integer of at least 128")
        # Fail before creating a journal if the transform declaration is unsupported.
        OpaqueSensorTransform(self.condition.sensor_transform, bin_count=bin_count)
        self.history_limit = history_limit
        self.max_tokens = max_tokens
        self.bin_count = bin_count
        self._lock = RLock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        config = canonical_json({
            "version": "opaque-store-v1", "condition": self.condition,
            "history_limit": history_limit, "max_tokens": max_tokens, "bin_count": bin_count,
        })
        try:
            with self._db:
                self._db.execute("CREATE TABLE IF NOT EXISTS opaque_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS opaque_records (record_ref TEXT PRIMARY KEY, record_json TEXT NOT NULL)"
                )
                self._db.execute("""CREATE TABLE IF NOT EXISTS opaque_samples (
                    sequence INTEGER PRIMARY KEY,
                    body_instance_id TEXT NOT NULL,
                    episode_key TEXT NOT NULL,
                    body_revision INTEGER NOT NULL,
                    captured_at TEXT NOT NULL,
                    source_value REAL NOT NULL,
                    envelope_json TEXT NOT NULL,
                    record_ref TEXT NOT NULL REFERENCES opaque_records(record_ref),
                    UNIQUE(body_instance_id, episode_key, body_revision)
                )""")
                self._db.execute("INSERT OR IGNORE INTO opaque_meta VALUES ('config', ?)", (config,))
                stored = self._db.execute("SELECT value FROM opaque_meta WHERE key = 'config'").fetchone()
                if stored["value"] != config:
                    raise ValueError("opaque sensor condition/configuration drift: use a new journal")
            self._validate_replay()
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def recovery_cursor(self, identity: str):
        with self._lock:
            row = self._db.execute("SELECT value FROM opaque_meta WHERE key=?", ('runtime:'+identity,)).fetchone()
            try:
                return json.loads(row[0]) if row else None
            except (ValueError, TypeError):
                return None

    def set_recovery_cursor(self, identity: str, token: dict) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO opaque_meta VALUES (?,?)", ('runtime:'+identity, json.dumps(token)))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _validate_replay(self) -> None:
        previous: dict[tuple[str, str], float] = {}
        for row in self._db.execute("SELECT * FROM opaque_samples ORDER BY sequence"):
            envelope = OpaqueInteroceptionEnvelope.model_validate_json(row["envelope_json"])
            key = (row["body_instance_id"], row["episode_key"])
            truth = envelope.truth_ref
            if (
                truth.body_instance_id != row["body_instance_id"]
                or (truth.episode_id or "") != row["episode_key"]
                or truth.body_revision != row["body_revision"]
                or truth.captured_at.isoformat() != row["captured_at"]
                or envelope.observation.sample_sequence != row["sequence"]
            ):
                raise ValueError("opaque journal reference integrity failure")
            transform = OpaqueSensorTransform(
                self.condition.sensor_transform, episode_id=truth.episode_id, bin_count=self.bin_count,
            )
            observation, record = transform.encode(
                value=row["source_value"], previous_value=previous.get(key),
                captured_at=truth.captured_at, sample_sequence=row["sequence"],
            )
            saved_record = self._db.execute(
                "SELECT record_json FROM opaque_records WHERE record_ref = ?", (row["record_ref"],),
            ).fetchone()
            if (
                observation != envelope.observation or envelope.transform_record_ref != content_hash(record)
                or saved_record is None or SensorTransformRecord.model_validate_json(saved_record["record_json"]) != record
                or row["record_ref"] != envelope.transform_record_ref
            ):
                raise ValueError("opaque journal deterministic replay failure")
            previous[key] = row["source_value"]

    def ingest(
        self, body: WireBodyState | ProviderBodyState, *, episode_id: str | None = None,
        sensor_value: float | None = None,
    ) -> OpaqueInteroceptionEnvelope:
        if not isinstance(body, (WireBodyState, ProviderBodyState)):
            raise TypeError("opaque ingest requires a validated body snapshot")
        body = type(body).model_validate(body.model_dump(mode="python"))
        wire_episode = getattr(body, "episode_id", None)
        if wire_episode is not None and episode_id is not None and wire_episode != episode_id:
            raise ValueError("episode_id contradicts the body snapshot")
        episode = wire_episode if wire_episode is not None else episode_id
        transform = OpaqueSensorTransform(self.condition.sensor_transform, episode_id=episode, bin_count=self.bin_count)
        episode_key = episode or ""
        truth_hash = content_hash(body)
        value = body.energy.available_ratio if sensor_value is None else sensor_value
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            existing = self._db.execute(
                "SELECT envelope_json, source_value FROM opaque_samples WHERE body_instance_id = ? AND episode_key = ? AND body_revision = ?",
                (body.body_instance_id, episode_key, body.revision),
            ).fetchone()
            if existing is not None:
                envelope = OpaqueInteroceptionEnvelope.model_validate_json(existing["envelope_json"])
                if envelope.truth_ref.truth_hash != truth_hash:
                    raise ValueError("same body revision contains conflicting truth")
                if existing["source_value"] != value:
                    raise ValueError("same body revision contains a conflicting sensor intervention")
                return envelope
            previous = self._db.execute(
                "SELECT * FROM opaque_samples WHERE body_instance_id = ? AND episode_key = ? ORDER BY sequence DESC LIMIT 1",
                (body.body_instance_id, episode_key),
            ).fetchone()
            if previous is not None:
                if body.revision <= previous["body_revision"]:
                    raise ValueError("body revisions must increase within an episode")
                if body.captured_at < datetime.fromisoformat(previous["captured_at"]):
                    raise ValueError("body capture times must not move backwards")
            sequence = self._db.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM opaque_samples").fetchone()[0]
            observation, record = transform.encode(
                value=value,
                previous_value=previous["source_value"] if previous is not None else None,
                captured_at=body.captured_at, sample_sequence=sequence,
            )
            refs = tuple(ref if isinstance(ref, str) else ref.ref for ref in body.evidence_refs)
            envelope = OpaqueInteroceptionEnvelope(
                truth_ref=BodyTruthReference(
                    body_instance_id=body.body_instance_id, body_revision=body.revision, episode_id=episode,
                    captured_at=body.captured_at, evidence_refs=refs, truth_hash=truth_hash,
                ),
                observation=observation, transform_record_ref=content_hash(record),
            )
            self._db.execute(
                "INSERT OR IGNORE INTO opaque_records VALUES (?, ?)", (content_hash(record), canonical_json(record)),
            )
            self._db.execute(
                "INSERT INTO opaque_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sequence, body.body_instance_id, episode_key, body.revision, body.captured_at.isoformat(),
                 value, canonical_json(envelope), envelope.transform_record_ref),
            )
            return envelope

    def observations(self, *, now: datetime) -> tuple[OpaqueInteroceptiveObservation, ...]:
        _aware(now)
        with self._lock:
            latest = self._db.execute(
                "SELECT body_instance_id, episode_key FROM opaque_samples ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            if latest is None:
                return ()
            limit = 1 if self.condition.interoception_treatment == "opaque_current" else self.history_limit
            rows = self._db.execute(
                "SELECT envelope_json FROM opaque_samples WHERE body_instance_id = ? AND episode_key = ? ORDER BY sequence DESC",
                (latest["body_instance_id"], latest["episode_key"]),
            )
            visible: list[OpaqueInteroceptiveObservation] = []
            for row in rows:
                observation = OpaqueInteroceptionEnvelope.model_validate_json(row["envelope_json"]).observation
                if observation.observed_at <= now:
                    visible.append(observation)
                    if len(visible) == limit:
                        break
            return bounded_observations(visible, max_tokens=self.max_tokens)

    def envelopes(self) -> tuple[OpaqueInteroceptionEnvelope, ...]:
        """Private evaluation/journal projection, never pass to the model."""
        with self._lock:
            return tuple(
                OpaqueInteroceptionEnvelope.model_validate_json(row["envelope_json"])
                for row in self._db.execute("SELECT envelope_json FROM opaque_samples ORDER BY sequence")
            )

    def records(self) -> tuple[SensorTransformRecord, ...]:
        """Private replay records in first-seen order, deduplicated by content hash."""
        with self._lock:
            return tuple(
                SensorTransformRecord.model_validate_json(row["record_json"])
                for row in self._db.execute("SELECT record_json FROM opaque_records ORDER BY rowid")
            )
