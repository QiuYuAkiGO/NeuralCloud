"""Private homeostatic clock and durable factual feedback for the QQ driver.

No model, GUI operation or send authority lives here. The existing domain
envelopes and homeostasis command ledger remain authoritative for state.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from harness.contracts.envelope import DomainCommandEnvelope, VersionVector
from harness.homeostasis.engine import HomeostasisEngine
from harness.homeostasis.profiles import load_profile
from harness.homeostasis.models import HomeostaticProfile, HomeostaticSnapshot, QQFactLookup, QQFactStatus, QQHomeostaticFact
from harness.runtime.adapters import DomainSnapshot
from harness.runtime.ownership import require_development, require_service_token


class SharedHomeostasisUnavailable(ConnectionError):
    pass


class _SharedHomeostasis:
    def __init__(self, url, token_file, *, client=None):
        parsed = urlsplit(url or "")
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("qq_homeostasis_shared_runtime_required")
        path = Path(token_file) if token_file is not None else None
        if path is None or not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            raise ValueError("qq_homeostasis_shared_token_required")
        token = path.read_text(encoding="utf-8").strip()
        token = require_service_token(token, name="shared_control_token", control_mode="study_runtime")
        self.url, self.headers = url.rstrip("/"), {"Authorization": f"Bearer {token}"}
        self.client = client
        self.owns_client = client is None
        self._io = None
        self._runner = None
        self._io_lock = threading.Lock()
        self._closed = False

    def request(self, method, path, *, payload=None, timeout_seconds=3.0):
        # The provider invokes synchronous pre-click checks on its running loop.
        # Only HTTP moves off that actor; state validation stays with the caller.
        expires = time.monotonic() + timeout_seconds

        def run():
            if self._runner is None:
                self._runner = asyncio.Runner()
            if self.client is None:
                self.client = httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False)

            async def bounded():
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("qq_shared_homeostasis_request_expired")
                async with asyncio.timeout(remaining):
                    return await self._request(method, path, payload=payload)

            return self._runner.run(bounded())

        with self._io_lock:
            if self._closed:
                raise SharedHomeostasisUnavailable("qq_shared_homeostasis_closed")
            if self._io is None:
                self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qq-homeostasis-io")
            future = self._io.submit(run)
        return future.result()

    async def _request(self, method, path, *, payload=None):
        try:
            response = await self.client.request(method, self.url + path, headers=self.headers, json=payload)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (httpx.HTTPError, ValueError):
            raise SharedHomeostasisUnavailable("qq_shared_homeostasis_unavailable") from None

    def close(self):
        with self._io_lock:
            if self._closed:
                return
            self._closed = True
            executor = self._io
            future = executor.submit(self._close_io) if executor is not None else None
        try:
            if future is not None:
                future.result()
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            self._io = None

    def _close_io(self):
        if self._runner is not None:
            try:
                if self.owns_client and self.client is not None:
                    self._runner.run(self.client.aclose())
            finally:
                self._runner.close()
                self._runner = None


class QQHomeostaticCommandError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__("qq_homeostasis_command_failed")


class QQHomeostaticDriver:
    def __init__(self, *, state_dir: Path, profile_path: Path | None = None, group_id: str,
                 self_member_id: str, run_id: str, run_async, tick_seconds=5.0,
                 clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic,
                 shared_runtime_url: str | None = None, shared_control_token_file: Path | None = None,
                 development_mode: bool = False, shared_http_client=None):
        if (not isinstance(group_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", group_id)
                or not isinstance(self_member_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", self_member_id)):
            raise ValueError("qq_homeostasis_invalid_scope")
        if not math.isfinite(tick_seconds) or not 1 <= tick_seconds <= 300:
            raise ValueError("qq_homeostasis_invalid_tick")
        if type(development_mode) is not bool:
            raise ValueError("qq_homeostasis_invalid_mode")
        self._shared = None
        if development_mode:
            require_development("qq.local_homeostasis")
            if shared_runtime_url is not None or shared_control_token_file is not None:
                raise ValueError("qq_homeostasis_ambiguous_owner")
            if profile_path is None:
                raise ValueError("qq_homeostasis_profile_required")
            profile = load_profile(profile_path)
            if profile.qq_social_drive is None or not profile.qq_social_drive.enabled:
                raise ValueError("qq_homeostasis_profile_not_enabled")
        else:
            self._shared = _SharedHomeostasis(shared_runtime_url, shared_control_token_file, client=shared_http_client)
        self.engine = None
        self._shared_snapshot = None
        self._snapshot_mono = None
        self._tick_deadline = None
        now = self._aware(clock())
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "qq-feedback.sqlite3"
        self.group_id, self.self_member_id, self.run_id = group_id, self_member_id, run_id
        self.clock, self.monotonic, self.run_async = clock, monotonic, run_async
        self.tick_seconds, self.next_tick = tick_seconds, monotonic()
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scope(id INTEGER PRIMARY KEY CHECK(id=1),
                    group_id TEXT NOT NULL,self_member_id TEXT NOT NULL,armed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS facts(fact_id TEXT PRIMARY KEY,record_json TEXT NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS facts_pending ON facts(applied);
            """)
            db.execute("INSERT OR IGNORE INTO scope VALUES(1,?,?,?)",
                       (group_id, self_member_id, now.isoformat()))
            scope = db.execute("SELECT * FROM scope WHERE id=1").fetchone()
            if (scope["group_id"], scope["self_member_id"]) != (group_id, self_member_id):
                raise ValueError("qq_homeostasis_scope_mismatch")
            self.armed_at = self._aware(datetime.fromisoformat(scope["armed_at"]))
            if "terminal_reason" not in {row["name"] for row in db.execute("PRAGMA table_info(facts)")}:
                db.execute("ALTER TABLE facts ADD COLUMN terminal_reason TEXT")
        # Establish the group/account boundary before the domain constructor can
        # load, migrate or repair any persisted projections in that directory.
        if self._shared is None:
            self.engine = HomeostasisEngine(profile, state_dir=self.state_dir / "domain", clock=clock)
        else:
            try:
                self._refresh_shared_snapshot()
            except BaseException:
                self._shared.close()
                raise
        self.last_activity_at = max(self.armed_at, now)
        self._last_activity_mono = monotonic()
        self.last_error = None

    @staticmethod
    def _aware(moment):
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("qq_homeostasis_aware_time_required")
        return moment.astimezone(timezone.utc)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    @property
    def summary(self):
        return self.snapshot.interoceptive_summary

    @property
    def snapshot(self):
        return self.engine.snapshot if self.engine is not None else self._shared_snapshot

    def _shared_request(self, method, path, *, payload=None):
        remaining = 3.0 if self._tick_deadline is None else min(3.0, self._tick_deadline - self.monotonic())
        if remaining <= 0:
            raise SharedHomeostasisUnavailable("qq_shared_homeostasis_tick_expired")
        return self._shared.request(method, path, payload=payload, timeout_seconds=remaining)

    def _refresh_shared_snapshot(self):
        domain = DomainSnapshot.model_validate(self._shared_request("GET", "/v1/homeostasis/snapshot"))
        profile = HomeostaticProfile.model_validate(domain.payload.get("profile"))
        snapshot = HomeostaticSnapshot.model_validate({key: value for key, value in domain.payload.items() if key != "profile"})
        if (domain.service != "homeostasis" or domain.revision != snapshot.revision
                or (snapshot.profile_id, snapshot.profile_revision) != (profile.profile_id, profile.revision)
                or snapshot.interoceptive_summary is None
                or snapshot.interoceptive_summary.snapshot_revision != snapshot.revision
                or snapshot.interoceptive_summary.profile_ref != profile.profile_ref
                or profile.qq_social_drive is None or not profile.qq_social_drive.enabled
                or (self._shared_snapshot is not None and snapshot.revision < self._shared_snapshot.revision)):
            raise SharedHomeostasisUnavailable("qq_shared_homeostasis_snapshot_invalid")
        self._shared_snapshot = snapshot
        self._snapshot_mono = self.monotonic()

    def _known_fact_hash(self, fact):
        if self.engine is not None:
            return self.engine._command_ledger.qq_fact_hash(fact.fact_key)
        lookup = QQFactLookup(group_id=fact.group_id, fact_id=fact.fact_id)
        result = QQFactStatus.model_validate(self._shared_request("POST", "/v1/homeostasis/qq-facts:read",
            payload=lookup.model_dump(mode="json")))
        if result.fact_key != fact.fact_key:
            raise SharedHomeostasisUnavailable("qq_shared_fact_binding_invalid")
        return result.fact_hash

    def status(self):
        summary = self.summary
        with closing(self._connect()) as db:
            counts = {row["applied"]: row["count"] for row in db.execute(
                "SELECT applied,COUNT(*) AS count FROM facts GROUP BY applied")}
            reasons = {row["terminal_reason"]: row["count"] for row in db.execute(
                "SELECT terminal_reason,COUNT(*) AS count FROM facts WHERE applied=2 GROUP BY terminal_reason")}
        return {"enabled": True, "owner": "shared_runtime" if self._shared is not None else "isolated_development",
                "profile_ref": summary.profile_ref,
                "revision": summary.snapshot_revision, "salience": summary.salience,
                "episode_signature": summary.episode_signature,
                "tick_seconds": self.tick_seconds,
                "last_activity_at": self.last_activity_at.isoformat(),
                "feedback_pending": counts.get(0, 0), "feedback_applied": counts.get(1, 0),
                "feedback_retired": counts.get(2, 0), "feedback_retirement_reasons": reasons,
                "last_error_type": self.last_error}

    def _apply(self, command_type, payload, *, key):
        now = self._aware(self.clock())
        if self.engine is None:
            raise ValueError("qq_driver_cannot_write_shared_domain_commands")
        snapshot = self.engine.snapshot
        envelope = DomainCommandEnvelope(
            command_type=command_type, event_id=key, correlation_id=key,
            receive_seq=snapshot.acknowledged_receive_seq + 1,
            character_id="misty", runtime_instance_id=self.run_id,
            manifest_revision=1, idempotency_key=f"qq-domain:{uuid4().hex}",
            issued_at=now, deadline=now + timedelta(seconds=5),
            expected_revision=snapshot.revision,
            version_vector=VersionVector(homeostasis_state=snapshot.revision), payload=payload)
        method = self.engine.apply_heartbeat if command_type == "ApplyHeartbeat" else self.engine.apply_event
        result = self.run_async(method(envelope))
        if result.status not in {"applied", "idempotent_replay"}:
            raise QQHomeostaticCommandError(result.status)
        return result

    def enqueue_fact(self, *, fact_id, kind, occurred_at, evidence_refs, related_attempt_id=None):
        occurred_at = self._aware(occurred_at)
        if occurred_at < self.armed_at:
            return False
        fact = {"schema_version": "qq-homeostatic-fact-v1", "fact_id": fact_id,
                "group_id": self.group_id, "kind": kind, "occurred_at": occurred_at.isoformat(),
                "verification": "qq_ui_verified", "evidence_refs": sorted(set(evidence_refs))}
        if related_attempt_id is not None:
            fact["related_attempt_id"] = related_attempt_id
        validated = QQHomeostaticFact.model_validate(fact)
        fact = validated.model_dump(mode="json")
        encoded = json.dumps(fact, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as db, db:
            old = db.execute("SELECT record_json FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
            if old is not None:
                try:
                    same_fact = QQHomeostaticFact.model_validate_json(old[0]).fact_hash == validated.fact_hash
                except ValueError:
                    same_fact = False
                if not same_fact:
                    raise ValueError("qq_homeostatic_fact_conflict")
                return False
            db.execute("INSERT INTO facts(fact_id,record_json) VALUES(?,?)", (fact_id, encoded))
        age = (self._aware(self.clock()) - occurred_at).total_seconds()
        if kind != "no_response" and -5 <= age <= 300 and occurred_at >= self.last_activity_at:
            self.last_activity_at = occurred_at
            self._last_activity_mono = max(self._last_activity_mono, self.monotonic() - max(0, age))
        return True

    def _finish_fact(self, fact_id, *, applied, reason=None):
        # 0=pending, 1=applied (including a recovered domain commit), 2=retired.
        # Terminal rows stay in place so old feedback cannot be re-enqueued.
        with closing(self._connect()) as db, db:
            db.execute("UPDATE facts SET applied=?,terminal_reason=? WHERE fact_id=? AND applied=0",
                       (1 if applied else 2, reason, fact_id))

    def _deliver_fact(self, row):
        try:
            fact = QQHomeostaticFact.model_validate_json(row["record_json"])
        except ValueError:
            self._finish_fact(row["fact_id"], applied=False, reason="fact_invalid")
            return
        if fact.fact_id != row["fact_id"] or fact.group_id != self.group_id:
            self._finish_fact(row["fact_id"], applied=False, reason="fact_scope_mismatch")
            return
        # Recover the crash boundary after domain commit but before this queue's
        # acknowledgement. Query only; never reward a fact a second time.
        known = self._known_fact_hash(fact)
        if known is not None:
            self._finish_fact(fact.fact_id, applied=known == fact.fact_hash,
                              reason="domain_recovered" if known == fact.fact_hash else "fact_conflict")
            return
        age = (self._aware(self.clock()) - fact.occurred_at).total_seconds()
        if fact.occurred_at < self.armed_at or age > 300 or age < -5:
            reason = "fact_before_armed" if fact.occurred_at < self.armed_at else ("fact_expired" if age > 300 else "fact_future")
            self._finish_fact(fact.fact_id, applied=False, reason=reason)
            return
        if self._shared is not None:
            result = self._shared_request("POST", "/v1/homeostasis/qq-facts", payload=fact.model_dump(mode="json"))
            if (result.get("schema_version") != "qq-homeostatic-submit-v1"
                    or result.get("fact_key") != fact.fact_key or result.get("fact_hash") != fact.fact_hash
                    or result.get("status") not in {"queued", "applied"}):
                raise SharedHomeostasisUnavailable("qq_shared_fact_response_invalid")
            # Queue acceptance is not a domain commit. Query the original fact
            # on the next tick, including after a lost response or restart.
            if result["status"] == "applied":
                known = self._known_fact_hash(fact)
                if known != fact.fact_hash:
                    raise SharedHomeostasisUnavailable("qq_shared_fact_commit_invalid")
                self._finish_fact(fact.fact_id, applied=True)
            return
        try:
            self._apply("ApplyEvent", {"event": {"event_type": "qq_fact", "source": "runtime",
                        "source_time": fact.occurred_at.isoformat(), "payload": {"qq_fact": fact.model_dump(mode="json")}}},
                        key=fact.fact_id)
        except QQHomeostaticCommandError as exc:
            if exc.status == "rejected":
                self._finish_fact(fact.fact_id, applied=False, reason="domain_rejected")
                return
            if exc.status == "conflict":
                known = self._known_fact_hash(fact)
                if known is not None:
                    self._finish_fact(fact.fact_id, applied=known == fact.fact_hash,
                                      reason="domain_recovered" if known == fact.fact_hash else "fact_conflict")
                    return
            raise
        self._finish_fact(fact.fact_id, applied=True)

    def tick(self, *, allow_heartbeat=True):
        if self.monotonic() < self.next_tick:
            return
        self.next_tick = self.monotonic() + self.tick_seconds
        self._tick_deadline = self.monotonic() + min(5.0, self.tick_seconds)
        try:
            if self._shared is not None:
                self._refresh_shared_snapshot()
            with closing(self._connect()) as db:
                pending = db.execute("SELECT fact_id,record_json FROM facts WHERE applied=0 ORDER BY rowid LIMIT 16").fetchall()
            for row in pending:
                if self.monotonic() >= self._tick_deadline:
                    break
                self._deliver_fact(row)
            if self._shared is not None and pending:
                self._refresh_shared_snapshot()
            if allow_heartbeat and self.engine is not None:
                self._apply("ApplyHeartbeat", {"event": {"event_type": "heartbeat", "source": "scheduler",
                    "source_time": self.clock().isoformat(), "payload": {}}}, key=f"qq-heartbeat:{uuid4().hex}")
            self.last_error = None
        except Exception as exc:
            self.last_error = type(exc).__name__
            raise
        finally:
            self._tick_deadline = None

    def refresh(self):
        """Read current shared state for an already-authorized opportunity.

        This does not create a heartbeat, apply feedback or reset a deadline.
        Selection may finish between the driver's periodic snapshot polls.
        """
        if self._shared is not None:
            self._refresh_shared_snapshot()

    def eligible(self, *, quiet_seconds, shared_opportunity=False):
        if not math.isfinite(quiet_seconds) or quiet_seconds < 0:
            raise ValueError("qq_homeostasis_invalid_quiet_interval")
        summary = self.summary
        if self._shared is not None and (self._snapshot_mono is None or self.monotonic() - self._snapshot_mono > 2 * self.tick_seconds):
            return False
        factors = self.snapshot.dimensions
        if (factors["energy_reserve"].value <= factors["energy_reserve"].target_band[0]
                or factors["cognitive_load"].value >= factors["cognitive_load"].target_band[1]):
            return False
        salience = {"noticeable", "marked"} if shared_opportunity else {"marked"}
        if (self.last_error or self.snapshot.watchdog or summary.salience not in salience
                or (not shared_opportunity and self.monotonic() - self._last_activity_mono < quiet_seconds)):
            return False
        with closing(self._connect()) as db:
            if db.execute("SELECT 1 FROM facts WHERE applied=0 LIMIT 1").fetchone() is not None:
                return False
        # Shared cognition has already selected the capability and consumed the
        # quiet/cooldown opportunity. Keep factual readiness and body limits;
        # do not perform the legacy need-to-channel selection a second time.
        return shared_opportunity or any(need.dimension in {"social_connection", "novelty"} and need.direction == "low"
                   for need in summary.dominant_needs)

    def close(self):
        if self.engine is not None:
            self.run_async(self.engine.close())
        else:
            self._shared.close()
