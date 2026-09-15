"""Serial, evidence-backed roster reads on the watcher's existing QQ body."""
from __future__ import annotations

from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3

from harness.embodiment.qq_contracts import QQReadRequest, QQReadResult
from harness.qq.roster import configured_groups


class GroupObservationStore:
    """Latest visible page per numeric group, separate from attributed messages."""

    def __init__(self, path):
        self.path = Path(path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS qq_group_reads (
                group_id TEXT PRIMARY KEY, result_json TEXT, read_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT, last_error TEXT)""")

    def rows(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return {row["group_id"]: dict(row) for row in db.execute("SELECT * FROM qq_group_reads")}

    def record(self, result):
        result = QQReadResult.model_validate(result)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""INSERT INTO qq_group_reads VALUES(?,?,1,?,NULL)
                ON CONFLICT(group_id) DO UPDATE SET result_json=excluded.result_json,
                read_count=read_count+1,last_attempt_at=excluded.last_attempt_at,last_error=NULL""",
                (result.scope.group_id, result.model_dump_json(), result.observed_at.isoformat()))

    def failure(self, group_id, at, code):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""INSERT INTO qq_group_reads VALUES(?,NULL,0,?,?)
                ON CONFLICT(group_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                last_error=excluded.last_error""", (group_id, at.isoformat(), code))


class QQGroupObservationMixin:
    def _init_group_observation(self):
        self._group_roster = configured_groups(self.settings)
        self._roster_visiting = None
        self._roster_next_probe = 0.0
        self._roster_cursor = 0
        self._roster_store = (GroupObservationStore(self.settings.state_db.with_suffix(".roster.sqlite3"))
                              if self.settings.group_roster is not None else None)
        self._roster_status()

    @property
    def automatic_send_enabled(self):
        return self.settings.automatic_send and next(
            group.send for group in self._group_roster if group.group_id == self.settings.group_id)

    def _roster_status(self):
        saved = self._roster_store.rows() if self._roster_store is not None else {}
        rows = []
        for group in self._group_roster:
            state = saved.get(group.group_id, {})
            result = json.loads(state["result_json"]) if state.get("result_json") else {}
            rows.append({**group.model_dump(), "last_observed_at": result.get("observed_at"),
                "message_count": len(result.get("messages", [])), "read_count": state.get("read_count", 0),
                "last_error": state.get("last_error")})
        self.set_status(group_roster=rows, visiting_group_id=self._roster_visiting)

    def _remember_group_read(self, result):
        if self._roster_store is not None:
            if result.scope.group_id not in {g.group_id for g in self._group_roster if g.observe}:
                raise ValueError("qq_roster_read_scope_invalid")
            self._roster_store.record(result)
            self._roster_status()

    def _roster_read_refs(self, group_id):
        if self._roster_store is None:
            return []
        row = self._roster_store.rows().get(group_id, {})
        result = json.loads(row["result_json"]) if row.get("result_json") else {}
        return result.get("evidence_refs", [])[:16]

    def _prepare_roster_home(self):
        if len(self._group_roster) <= 1:
            return
        from harness.qq import watcher as runtime
        from harness.qq.telemetry import QQDeadline
        home = next(g for g in self._group_roster if g.group_id == self.settings.group_id)
        self.set_status(state="selecting_home_group", automatic_send=False)
        deadline = QQDeadline(runtime.time.monotonic() + 60)
        with self._observation_lock, self._desktop_turn(deadline):
            verification = self.desktop.select_group(home.group_id,
                allowed_group_ids=tuple(g.group_id for g in self._group_roster if g.observe),
                display_name=home.label or None)
            if verification.group_id != home.group_id:
                raise ValueError("qq_roster_home_group_mismatch")

    def _read_roster_group(self, group_id, *, deadline_at):
        from harness.qq import watcher as runtime
        from harness.qq.telemetry import QQDeadline, QQTurnTrace
        allowed = tuple(g.group_id for g in self._group_roster if g.observe)
        labels = {g.group_id: g.label or None for g in self._group_roster}
        if group_id not in allowed:
            raise ValueError("qq_roster_group_not_allowed")
        if (self._pending_turn is not None or self._qq_operation is not None
                or self.ledger.has_uncertain_send() or self._attribution_retry):
            raise ValueError("qq_roster_actor_busy")
        if not self._identity_prepared or self._qq_read_group_id != self.settings.group_id:
            raise ValueError("qq_roster_home_identity_required")
        self.check_control()
        remaining = (deadline_at - runtime.now()).total_seconds()
        if not 0 < remaining <= 60:
            raise ValueError("qq_roster_read_deadline_invalid")
        deadline = QQDeadline(runtime.time.monotonic() + remaining)
        trace = QQTurnTrace(first_seen_at=runtime.now(), first_seen_mono=runtime.time.monotonic())
        home = self.settings.group_id
        restored = group_id == home
        # The sensor cannot publish a foreign chat as a home-group arrival.
        # The same actor and observation gate own the visit and the return.
        with self._observation_lock:
            self._roster_visiting = group_id
            self.set_status(state="reading_group_roster", automatic_send=False)
            self._roster_status()
            try:
                with self._desktop_turn(deadline, trace):
                    deadline.check()
                    # Verify the signed-in account in the prepared home chat.
                    # The ensuing allowlisted navigation keeps the same body
                    # and window; a foreign group's visible members need not
                    # include our account or use the same group nickname.
                    account = self.desktop.verify_self()
                    verification = self.desktop.select_group(group_id, allowed_group_ids=allowed,
                        display_name=labels[group_id])
                    if verification.group_id != group_id:
                        raise ValueError("qq_roster_numeric_group_mismatch")
                    self._identity_prepared = True
                    self._qq_read_group_id = group_id
                    self._qq_read_group_evidence = tuple(dict.fromkeys([
                        *getattr(account, "evidence_refs", ()), *verification.evidence_refs]))[-30:]
                    self._qq_read_frame, self._qq_read_screen = verification.chat_frame, verification.chat_screen
                    self._qq_ensure_provider()
                    request = QQReadRequest(scope=self._qq_scope(group_id), requested_at=runtime.now(), deadline=deadline_at)
                    result = self._qq_loop.run_until_complete(self._qq_provider.read(request))
                    deadline.check()
                    self._remember_group_read(result)
                    return result
            finally:
                if group_id != home:
                    # Returning the view has its own bounded cleanup budget;
                    # this never extends a read request or a send candidate.
                    cleanup = QQDeadline(runtime.time.monotonic() + 45)
                    try:
                        with self._desktop_turn(cleanup, trace):
                            verification = self.desktop.select_group(home, allowed_group_ids=allowed,
                                display_name=labels[home])
                            if verification.group_id != home:
                                raise ValueError("qq_roster_home_group_mismatch")
                            self._qq_read_group_id = home
                            self._qq_read_group_evidence = tuple(verification.evidence_refs)
                            self._qq_read_frame, self._qq_read_screen = verification.chat_frame, verification.chat_screen
                            self._identity_prepared = False
                            restored = True
                    except Exception:
                        self._identity_prepared = False
                        self._qq_read_frame = self._qq_read_screen = None
                        runtime.atomic_json(self.settings.control_file,
                            {"paused": True, "reason": "qq_roster_home_restore_failed"})
                        self.set_status(state="roster_restore_failed", automatic_send=False,
                            last_error_type="qq_roster_home_restore_failed")
                self._roster_visiting = None
                self._roster_next_probe = runtime.time.monotonic() + self.settings.group_observation_seconds
                self._roster_status()
                trace.flush(self._trace_file)
                if restored:
                    self.set_status(state="watching", automatic_send=False)

    def _maybe_roster_observation(self):
        from harness.qq import watcher as runtime
        groups = [g for g in self._group_roster if g.observe and g.group_id != self.settings.group_id]
        if (not groups or not self.settings.qq_body_enabled or not self._identity_prepared
                or self._pending_turn is not None or self._attribution_retry
                or self._expired_observation is not None or self._carry_first_seen is not None
                or self.ledger.has_uncertain_send() or runtime.time.monotonic() < self._roster_next_probe):
            return
        group = groups[self._roster_cursor % len(groups)]
        self._roster_cursor += 1
        try:
            self._read_roster_group(group.group_id, deadline_at=runtime.now() + timedelta(seconds=60))
        except Exception as exc:
            if self._roster_store is not None:
                self._roster_store.failure(group.group_id, runtime.now(), runtime.error_code(exc))
            self._roster_next_probe = runtime.time.monotonic() + self.settings.group_observation_seconds
            self._roster_status()
