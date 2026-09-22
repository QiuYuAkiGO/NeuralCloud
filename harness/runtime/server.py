"""Runtime authority, scoped evidence and durable budgets for remote Linux commands."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from harness.contracts.capability_calls import CapabilityOffer, SERVER_EXECUTE, SERVER_OBSERVE
from harness.embodiment.server_contracts import (
    ServerCommandArguments, ServerCommandResult, ServerError,
)
from harness.runtime.models import RuntimeLifecycle, utc_now
from harness.runtime.server_policy import classify_command


OBSERVE = SERVER_OBSERVE
EXECUTE = SERVER_EXECUTE


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ServerCallStore:
    """A request reservation survives failure, cancellation, approval and restart."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, capability TEXT NOT NULL,
                command TEXT NOT NULL, cwd TEXT, timeout_seconds INTEGER NOT NULL,
                risk_class TEXT NOT NULL, fingerprint TEXT NOT NULL, day TEXT NOT NULL,
                autonomous INTEGER NOT NULL, created_at TEXT NOT NULL, decided_at TEXT,
                started_at TEXT, status TEXT NOT NULL, result TEXT, error TEXT,
                offer_json TEXT, generation INTEGER NOT NULL
            );
        """)

    def command(self, identity):
        cursor = self.db.execute("SELECT * FROM commands WHERE id=?", (identity,))
        row = cursor.fetchone()
        return dict(zip([item[0] for item in cursor.description], row)) if row else None

    def reserve(self, identity, scope, capability, command, cwd, timeout_seconds, risk_class,
                fingerprint, day, autonomous, generation, offer_json, status, limit, autonomous_limit):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.command(identity)
            if previous:
                if (previous["fingerprint"], previous["scope"], previous["capability"]) != (fingerprint, scope, capability):
                    raise ServerError("server_call_conflict")
                self.db.rollback()
                return previous
            total, auto = self.usage(day)
            if total >= limit or autonomous and auto >= autonomous_limit:
                raise ServerError("server_budget_exhausted")
            self.db.execute(
                "INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,NULL,NULL,?,?)",
                (identity, scope, capability, command, cwd, timeout_seconds, risk_class, fingerprint, day,
                 int(autonomous), utc_now().isoformat(), status, offer_json, generation))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return None

    def finish(self, identity, *, status=None, result=None, error=None, decided=False, started=False):
        current = self.command(identity)
        if current is None:
            return
        now = utc_now().isoformat()
        with self.db:
            self.db.execute(
                "UPDATE commands SET status=?, result=?, error=?, decided_at=?, started_at=? WHERE id=?",
                (status or current["status"],
                 _json(result) if result is not None else current["result"],
                 error, now if decided or current["decided_at"] else current["decided_at"],
                 now if started or current["started_at"] else current["started_at"], identity))

    def mark_started(self, identity):
        with self.db:
            self.db.execute(
                "UPDATE commands SET status='started', started_at=?, decided_at=? WHERE id=? AND status='pending_approval'",
                (utc_now().isoformat(), utc_now().isoformat(), identity))
        row = self.command(identity)
        if row is None or row["status"] != "started":
            raise ServerError("server_command_not_pending")

    def usage(self, day):
        row = self.db.execute("SELECT COUNT(*),COALESCE(SUM(autonomous),0) FROM commands WHERE day=?", (day,)).fetchone()
        return int(row[0]), int(row[1])

    def list(self, *, status=None, scope=None, limit=40):
        query, args = "SELECT * FROM commands", []
        clauses = []
        if status:
            clauses.append("status=?")
            args.append(status)
        if scope:
            clauses.append("scope=?")
            args.append(scope)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY rowid DESC LIMIT ?"
        args.append(limit)
        cursor = self.db.execute(query, args)
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def close(self):
        self.db.close()


class ServerController:
    def __init__(self, supervisor, settings, state_dir, *, provider=None, config_store=None, test_provider_factory=None):
        self.supervisor = supervisor
        self.settings = settings
        self.provider = provider
        self.config_store = config_store
        self.test_provider_factory = test_provider_factory
        self.calls = ServerCallStore(Path(state_dir) / "server" / "calls.sqlite3")
        self._lock = asyncio.Lock()
        self._provider_identity = id(provider)
        self._revision = self._configuration()
        self._closed = False

    def _configuration(self):
        return _hash({"settings": self.settings.model_dump(mode="json") if self.settings else None,
            "authorization": self.supervisor.manifest.server_access.model_dump(mode="json"),
            "config_revision": self.config_store.snapshot()["active_revision"] if self.config_store else 0})

    @property
    def enabled(self):
        return bool(self.settings and self.provider and self.supervisor.manifest.server_access.enabled)

    def _day(self):
        return utc_now().astimezone(ZoneInfo(self.supervisor.manifest.timezone)).date().isoformat()

    def _policy(self):
        settings = self.settings
        access = self.supervisor.manifest.server_access
        extra = tuple(settings.extra_high_risk_patterns) if settings else ()
        forbidden = tuple(settings.forbidden_patterns) if settings else ()
        sudo = settings.sudo_requires_approval if settings else access.sudo_requires_approval
        return extra, forbidden, sudo

    def _permitted(self, capability, autonomous):
        if not self.enabled or self._configuration() != self._revision or id(self.provider) != self._provider_identity:
            return False
        if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
            return False
        if autonomous:
            config = self.supervisor.manifest.autonomous_capabilities
            return config.enabled and config.owners.get("server", "off") == "shared" and capability in config.allowed_capabilities
        return True

    def context(self, scope):
        pending = [self._summary(row, include_output=False) for row in self.calls.list(status="pending_approval", limit=8)]
        recent = [self._summary(row, include_output=False) for row in self.calls.list(scope=scope, limit=5)]
        return {
            "host": self.settings.host if self.settings else None,
            "host_id": self.settings.body_instance_id if self.settings else None,
            "username": self.settings.username if self.settings else None,
            "pending_approvals": pending,
            "recent": recent,
        }

    def _summary(self, row, *, include_output=False):
        result = json.loads(row["result"]) if row.get("result") else {}
        item = {
            "command_id": row["id"], "status": row["status"], "capability": row["capability"],
            "command": row["command"], "cwd": row["cwd"], "risk_class": row["risk_class"],
            "created_at": row["created_at"], "error": row["error"],
            "exit_code": result.get("exit_code"), "truncated": result.get("truncated", False),
            "evidence_refs": result.get("evidence_refs", []),
        }
        if include_output:
            item.update(stdout=result.get("stdout", ""), stderr=result.get("stderr", ""))
        return item

    async def catalog(self, scope="autonomous", *, autonomous=True, deadline=None):
        if not self.enabled:
            return []
        status = await self.provider.status()
        now = utc_now()
        total, auto = self.calls.usage(self._day())
        within_budget = total < self.settings.daily_commands and (not autonomous or auto < self.settings.autonomous_daily_commands)
        context = self.context(scope)
        offers = []
        for capability, model in ((OBSERVE, ServerCommandArguments), (EXECUTE, ServerCommandArguments)):
            supported = capability in status.activated_capabilities
            available = (supported and capability in status.effective_capabilities
                         and status.state.value in {"available", "degraded"} and self._permitted(capability, autonomous))
            ready = available and within_budget
            offers.append(CapabilityOffer(offer_id="server:" + capability, channel="server", capability_id=capability,
                provider_id=status.provider_id or "ssh-server", body_instance_id=self.settings.body_instance_id,
                scope={"host_id": self.settings.body_instance_id, "context_id": scope},
                activation_revision=self._revision, input_schema=model.model_json_schema(),
                observation=context, observation_refs=[ref for item in context["recent"] for ref in item.get("evidence_refs", [])][:16],
                supported=supported, available=available, ready=ready,
                valid_until=min(deadline, now + timedelta(seconds=30)) if deadline else now + timedelta(seconds=30)))
        return offers

    @staticmethod
    def _binding(offer):
        return (offer.provider_id, offer.body_instance_id, offer.scope, offer.activation_revision, offer.capability_id)

    def _replay_or_raise(self, previous, fingerprint, scope):
        if previous["fingerprint"] != fingerprint or previous["scope"] != scope:
            raise ServerError("server_call_conflict")
        if previous["status"] == "pending_approval":
            return ServerCommandResult(status="pending_approval", command_id=previous["id"],
                                       risk_class=previous["risk_class"])
        if previous["status"] != "completed":
            raise ServerError(previous["error"] or "server_result_uncertain")
        return ServerCommandResult.model_validate_json(previous["result"])

    def _classify(self, command):
        extra, forbidden, sudo = self._policy()
        return classify_command(command, extra_high_risk_patterns=extra, forbidden_patterns=forbidden,
                                sudo_requires_approval=sudo)

    async def execute(self, identity, offer, arguments, *, deadline, autonomous=False, guard=None, generation=0):
        async with self._lock:
            if not self.enabled:
                raise ServerError("server_not_enabled")
            if utc_now() >= deadline:
                raise ServerError("server_deadline_exceeded")
            scope = offer.scope.get("context_id")
            if not isinstance(scope, str):
                raise ServerError("server_scope_invalid")
            capability = offer.capability_id
            try:
                request = ServerCommandArguments.model_validate(arguments)
            except (TypeError, ValueError):
                raise ServerError("server_command_arguments_invalid") from None
            classification = self._classify(request.command)
            if capability == OBSERVE and classification.risk_class != "read_only":
                raise ServerError("server_observe_not_read_only")
            if capability not in {OBSERVE, EXECUTE}:
                raise ServerError("server_capability_unknown")
            fingerprint = _hash([request.model_dump(mode="json"), self._binding(offer), classification.risk_class])
            previous = self.calls.command(identity)
            if previous:
                return self._replay_or_raise(previous, fingerprint, scope)
            fresh = await self.catalog(scope, autonomous=autonomous, deadline=deadline)
            current = next((item for item in fresh if item.capability_id == capability), None)
            if (not current or not current.available or self._binding(current) != self._binding(offer)
                    or offer.valid_until <= utc_now() or guard and guard()):
                raise ServerError("server_call_superseded")
            status = "pending_approval" if classification.risk_class == "high_risk" else "started"
            reserved = self.calls.reserve(
                identity, scope, capability, request.command, request.cwd, request.timeout_seconds,
                classification.risk_class, fingerprint, self._day(), autonomous, generation,
                _json(offer.model_dump(mode="json")), status,
                self.settings.daily_commands, self.settings.autonomous_daily_commands)
            if reserved:
                return self._replay_or_raise(reserved, fingerprint, scope)
            if status == "pending_approval":
                return ServerCommandResult(status="pending_approval", command_id=identity,
                                           risk_class=classification.risk_class)
            return await self._run_reserved(identity, request, capability, classification.risk_class, deadline, guard)

    async def _run_reserved(self, identity, request, capability, risk_class, deadline, guard):
        try:
            timeout = min(request.timeout_seconds, (deadline - utc_now()).total_seconds())
            async with asyncio.timeout(max(0.1, timeout)):
                result = await self.provider.run(request, deadline=deadline, capability=capability)
            if utc_now() >= deadline or not self._permitted(capability, True) and not self._permitted(capability, False):
                raise ServerError("server_late_result")
            if guard and guard():
                raise ServerError("server_late_result")
            evidence = ["server-command:" + identity,
                        "server-output:sha256:" + _hash({"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code})]
            normalized = result.model_copy(update={
                "status": "completed", "command_id": identity, "risk_class": risk_class, "evidence_refs": evidence,
                "observation": {"exit_code": result.exit_code, "truncated": result.truncated,
                                "stdout": result.stdout[:4000], "stderr": result.stderr[:2000]},
            })
            self.calls.finish(identity, status="completed", result=normalized.model_dump(mode="json"), decided=True, started=True)
        except BaseException as exc:
            code = exc.code if isinstance(exc, ServerError) else "server_timeout" if isinstance(exc, TimeoutError) else "server_request_failed"
            self.calls.finish(identity, status="uncertain" if isinstance(exc, (TimeoutError, asyncio.CancelledError)) else "failed",
                              error=code, decided=True, started=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ServerError(code) from None
        self._journal(identity, capability, normalized)
        return normalized

    def _journal(self, identity, capability, result):
        append = getattr(self.supervisor, "_append_journal", None)
        if append:
            append("server_command_completed", data={"call_id": identity, "capability": capability,
                "risk_class": result.risk_class, "exit_code": result.exit_code, "truncated": result.truncated,
                "evidence_refs": result.evidence_refs})

    def approval_deadline(self, row):
        created = datetime.fromisoformat(row["created_at"])
        seconds = self.supervisor.manifest.server_access.approval_timeout_seconds
        if self.settings is not None:
            seconds = self.settings.approval_timeout_seconds
        return created + timedelta(seconds=seconds)

    async def expire_pending_approvals(self):
        now = utc_now()
        for row in self.calls.list(status="pending_approval", limit=200):
            if now >= self.approval_deadline(row):
                self.calls.finish(row["id"], status="cancelled", error="server_approval_expired", decided=True)

    async def approve(self, identity, *, guard: Callable[[], object] | None = None):
        async with self._lock:
            row = self.calls.command(identity)
            if row is None:
                raise ServerError("server_command_not_found")
            if row["status"] == "completed":
                return ServerCommandResult.model_validate_json(row["result"])
            if row["status"] != "pending_approval":
                raise ServerError(row["error"] or "server_result_uncertain")
            if utc_now() >= self.approval_deadline(row) or guard and guard():
                self.calls.finish(identity, status="cancelled", error="server_approval_expired", decided=True)
                raise ServerError("server_approval_expired" if utc_now() >= self.approval_deadline(row) else "server_call_superseded")
            if not self._permitted(row["capability"], bool(row["autonomous"])):
                self.calls.finish(identity, status="cancelled", error="server_call_superseded", decided=True)
                raise ServerError("server_call_superseded")
            self.calls.mark_started(identity)
            request = ServerCommandArguments(command=row["command"], cwd=row["cwd"], timeout_seconds=row["timeout_seconds"])
            deadline = utc_now() + timedelta(seconds=request.timeout_seconds)
            return await self._run_reserved(identity, request, row["capability"], row["risk_class"], deadline, guard)

    async def reject(self, identity, *, guard: Callable[[], object] | None = None):
        async with self._lock:
            row = self.calls.command(identity)
            if row is None:
                raise ServerError("server_command_not_found")
            if row["status"] == "completed":
                return ServerCommandResult.model_validate_json(row["result"])
            if row["status"] != "pending_approval":
                raise ServerError(row["error"] or "server_result_uncertain")
            self.calls.finish(identity, status="rejected", error="server_command_rejected", decided=True)
            return {"status": "rejected", "command_id": identity}

    def commands(self, *, status=None, limit=40):
        return [self._summary(row, include_output=row["status"] in {"completed", "failed", "pending_approval"})
                for row in self.calls.list(status=status, limit=limit)]

    async def status(self):
        body = await self.provider.status() if self.provider is not None else None
        total, auto = self.calls.usage(self._day())
        auto_config = self.supervisor.manifest.autonomous_capabilities
        auto_authorized = auto_config.owners.get("server", "off") == "shared" and bool(
            {OBSERVE, EXECUTE} & set(auto_config.allowed_capabilities))
        return {"enabled": self.enabled,
            "autonomous_enabled": self.enabled and auto_authorized and auto_config.enabled,
            "autonomous_global_enabled": auto_config.enabled,
            "authorization": {**self.supervisor.manifest.server_access.model_dump(mode="json"),
                              "autonomous_enabled": auto_authorized},
            "body": body.model_dump(mode="json") if body else None,
            "pending_approvals": len(self.calls.list(status="pending_approval", limit=200)),
            "usage": {"day": self._day(), "commands": total, "autonomous_commands": auto,
                "daily_limit": self.settings.daily_commands if self.settings else 200,
                "autonomous_daily_limit": self.settings.autonomous_daily_commands if self.settings else 40}}

    async def test(self, settings, private_key):
        if self.test_provider_factory is None:
            raise ServerError("server_test_unavailable")
        async with self._lock:
            provider = self.test_provider_factory(settings, private_key)
            identity = "test:" + uuid4().hex
            try:
                request = ServerCommandArguments(command="uname -a; id")
                self.calls.reserve(identity, identity, OBSERVE, request.command, None, request.timeout_seconds,
                    "read_only", _hash([identity]), self._day(), False, 0, "{}", "started",
                    settings.daily_commands, settings.autonomous_daily_commands)
                try:
                    async with asyncio.timeout(settings.default_timeout_seconds):
                        result = await provider.run(request, capability=OBSERVE)
                    result = result.model_copy(update={"command_id": identity, "risk_class": "read_only",
                        "evidence_refs": ["server-command:" + identity]})
                    self.calls.finish(identity, status="completed", result=result.model_dump(mode="json"), decided=True, started=True)
                    return {"status": "completed" if result.exit_code == 0 else "partial",
                            "checks": [{"capability": OBSERVE, "status": "completed" if result.exit_code == 0 else "failed",
                                        "exit_code": result.exit_code, "stdout": result.stdout[:500]}],
                            "activated": False}
                except BaseException as exc:
                    code = exc.code if isinstance(exc, ServerError) else "server_test_failed"
                    self.calls.finish(identity, error=code, status="failed", decided=True, started=True)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise ServerError(code) from None
            finally:
                await provider.close()

    async def close(self):
        if not self._closed:
            self._closed = True
            if self.provider is not None:
                await self.provider.close()
            self.calls.close()
