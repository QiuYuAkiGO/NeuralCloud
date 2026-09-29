"""Authenticated, durable metering at the model transport boundary.

Only a trusted domain command opens a scope. Model text cannot supply a scope,
change an endpoint, reserve a budget, or acknowledge a body usage receipt.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import atexit
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field


class AttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(min_length=1, max_length=256)
    purpose: str = Field(min_length=1, max_length=256)
    autonomous: bool


class UsageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(min_length=1, max_length=256)
    usage: dict[str, Any]


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["owner", "local:main"] = "owner"
    autonomous: bool = True


_PROCESS_EPOCH = uuid4().hex


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ProductionUsageSpool:
    """Cognitive-owned receipts survive loss of the Runtime transport.

    In-flight rows are never published. Only a final outcome (or recovery after
    a process restart) freezes the payload that Runtime can acknowledge.
    """

    def __init__(self, path, *, process_epoch=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.process_epoch = process_epoch or _PROCESS_EPOCH
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS usage_spool(
                attempt_id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL,
                finalized INTEGER NOT NULL DEFAULT 0, acknowledged INTEGER NOT NULL DEFAULT 0)""")
            # Cognitive's existing owner lease excludes another live process.
            # Reopening a client in this process must not finalize a live call.
            for row in db.execute(
                "SELECT attempt_id,payload FROM usage_spool WHERE finalized=0 AND owner<>?", (self.process_epoch,)
            ).fetchall():
                usage = json.loads(row[1])
                usage.update(outcome="interrupted", error_type="ProcessInterrupted")
                db.execute("UPDATE usage_spool SET payload=?,finalized=1 WHERE attempt_id=?", (_json(usage), row[0]))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, attempt_id, usage):
        with self._db() as db:
            db.execute(
                "INSERT INTO usage_spool(attempt_id,owner,payload) VALUES(?,?,?)",
                (attempt_id, self.process_epoch, _json(usage)),
            )

    def checkpoint(self, attempt_id, usage):
        with self._db() as db:
            if (
                db.execute(
                    "UPDATE usage_spool SET payload=? WHERE attempt_id=? AND finalized=0 AND owner=?",
                    (_json(usage), attempt_id, self.process_epoch),
                ).rowcount
                != 1
            ):
                raise ValueError("production_usage_checkpoint_conflict")

    def finish(self, attempt_id, usage):
        payload = _json(usage)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload,finalized FROM usage_spool WHERE attempt_id=?", (attempt_id,)).fetchone()
            if old is not None and old[1] and old[0] != payload:
                raise ValueError("production_usage_identity_conflict")
            db.execute(
                """INSERT INTO usage_spool(attempt_id,owner,payload,finalized) VALUES(?,?,?,1)
                ON CONFLICT(attempt_id) DO UPDATE SET payload=excluded.payload,finalized=1""",
                (attempt_id, self.process_epoch, payload),
            )

    def pending(self, *, limit=32):
        with self._db() as db:
            return [
                (row[0], json.loads(row[1]))
                for row in db.execute(
                    "SELECT attempt_id,payload FROM usage_spool WHERE finalized=1 AND acknowledged=0 ORDER BY rowid LIMIT ?",
                    (limit,),
                )
            ]

    def acknowledge(self, attempt_id):
        with self._db() as db:
            db.execute("UPDATE usage_spool SET acknowledged=1 WHERE attempt_id=? AND finalized=1", (attempt_id,))

    def status(self):
        with self._db() as db:
            return {
                "pending": db.execute(
                    "SELECT count(*) FROM usage_spool WHERE finalized=1 AND acknowledged=0"
                ).fetchone()[0],
                "in_flight": db.execute("SELECT count(*) FROM usage_spool WHERE finalized=0").fetchone()[0],
            }


def _connection_settings():
    from harness.runtime.ownership import require_service_token

    if os.environ.get("NC_SERVICE_TOKEN_FILE"):
        from harness.deploy.runner import read_token

        credential = read_token(os.environ["NC_SERVICE_TOKEN_FILE"])
    else:
        credential = require_service_token(os.environ.get("NC_SERVICE_TOKEN"), name="production-metering")
    host = os.environ.get("NC_RUNTIME_HOST", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    endpoint = os.environ.get("NC_RUNTIME_URL") or f"http://{host}:{os.environ.get('NC_RUNTIME_PORT', '8770')}"
    state = Path(
        os.environ.get(
            "NC_COGNITIVE_STATE_PATH", str(Path(__file__).resolve().parents[2] / "data/cognitive/state.json")
        )
    )
    return credential, endpoint, state.parent / "production-usage.sqlite3"


class ProductionMeteringClient:
    def __init__(self, *, base_url=None, token=None, transport=None, spool_path=None, background=True):
        defaults = (
            _connection_settings()
            if token is None
            else (
                token,
                os.environ.get("NC_RUNTIME_URL", "http://127.0.0.1:8770"),
                Path(
                    os.environ.get(
                        "NC_COGNITIVE_STATE_PATH",
                        str(Path(__file__).resolve().parents[2] / "data/cognitive/state.json"),
                    )
                ).parent
                / "production-usage.sqlite3",
            )
        )
        credential, endpoint, path = defaults
        self.spool = ProductionUsageSpool(spool_path or path)
        self._client = httpx.Client(
            base_url=base_url or endpoint,
            headers={"Authorization": "Bearer " + credential},
            timeout=5.0,
            transport=transport,
        )
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._flush_lock = threading.Lock()
        self._last_error = None
        self._publisher = None
        if background:
            self._publisher = threading.Thread(target=self._publish, name="production-usage-publisher", daemon=True)
            self._publisher.start()

    def _post(self, path, payload, **kwargs):
        # A transport failure may follow a commit. Retry the exact same identity.
        for attempt in range(2):
            try:
                response = self._client.post(path, json=payload, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.TransportError:
                if attempt:
                    raise

    def reserve_attempt(self, attempt_id, *, purpose, autonomous=True):
        receipt = self._post(
            "/v1/production/model-attempts", {"attempt_id": attempt_id, "purpose": purpose, "autonomous": autonomous}
        )
        if (
            not isinstance(receipt, dict)
            or receipt.get("attempt_id") != attempt_id
            or receipt.get("purpose") != purpose
            or receipt.get("autonomous") is not bool(autonomous)
        ):
            raise ValueError("production_attempt_receipt_mismatch")
        return receipt

    def context(self, scope="owner", *, autonomous=True):
        request = ContextRequest(scope=scope, autonomous=autonomous)
        return self._post("/v1/production/context", request.model_dump(mode="json"))

    def begin_attempt(self, attempt_id, usage):
        self.spool.begin(attempt_id, usage)

    def checkpoint_usage(self, attempt_id, usage):
        self.spool.checkpoint(attempt_id, usage)

    def record_usage(self, attempt_id, usage):
        # The completed model result does not wait for the Runtime network.
        self.spool.finish(attempt_id, usage)
        self._wake.set()
        return {"attempt_id": attempt_id, "durable": True, "delivery_pending": True}

    def flush_pending(self):
        with self._flush_lock:
            for attempt_id, usage in self.spool.pending():
                if self._stop.is_set():
                    break
                try:
                    receipt = self._post(
                        "/v1/production/model-usage", {"attempt_id": attempt_id, "usage": usage}, timeout=0.5
                    )
                    if (
                        not isinstance(receipt, dict)
                        or receipt.get("attempt_id") != attempt_id
                        or receipt.get("durable") is not True
                    ):
                        self._last_error = "UsageReceiptMismatch"
                        break
                    self.spool.acknowledge(attempt_id)
                    self._last_error = None
                except (httpx.HTTPError, ValueError) as exc:
                    self._last_error = type(exc).__name__
                    break

    def _publish(self):
        try:
            while not self._stop.is_set():
                try:
                    self.flush_pending()
                except Exception as exc:
                    # The durable queue survives a transient disk/transport
                    # error. Never stop the sole publisher silently.
                    self._last_error = type(exc).__name__
                self._wake.wait(1)
                self._wake.clear()
        finally:
            if self._stop.is_set():
                self._client.close()

    def status(self):
        return {**self.spool.status(), "error": self._last_error}

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._publisher is not None:
            self._publisher.join(timeout=2)
        else:
            self._client.close()


_default_meters = {}
_default_meter_lock = threading.Lock()


def get_production_meter():
    credential, endpoint, spool = _connection_settings()
    key = (str(spool.resolve()), endpoint, hashlib.sha256(credential.encode()).hexdigest())
    with _default_meter_lock:
        if key not in _default_meters or _default_meters[key]._stop.is_set():
            _default_meters[key] = ProductionMeteringClient(base_url=endpoint, token=credential, spool_path=spool)
        return _default_meters[key]


@atexit.register
def _close_default_meters():
    for meter in _default_meters.values():
        meter.close()


@dataclass
class CallScope:
    context: dict[str, Any]
    command_id: str
    autonomous: bool
    meter: Any
    on_dispatch: Any = None


_scope: ContextVar[CallScope | None] = ContextVar("production_model_call", default=None)


def current_production_context():
    current = _scope.get()
    return current.context if current else None


@contextmanager
def production_call_scope(context, *, command_id, autonomous=False, meter=None, on_dispatch=None):
    if on_dispatch is not None and not callable(on_dispatch):
        raise ValueError("production_dispatch_callback_invalid")
    if context is None:
        yield None
        return
    if (
        not isinstance(context, dict)
        or context.get("schema_version") != "production-context-v1"
        or not isinstance(context.get("configuration"), dict)
    ):
        raise ValueError("invalid_production_context")
    current = CallScope(context, command_id, autonomous, meter if meter is not None else get_production_meter(), on_dispatch)
    token = _scope.set(current)
    try:
        yield current
    finally:
        _scope.reset(token)


def mark_production_dispatch(metering):
    """Record exposure immediately before an actual POST/send attempt.

    Reservation has already succeeded. This marks attempted transmission, not
    receipt or attention by the provider. A failed audit callback prevents send.
    Response validation/transport failure never rolls an exposure record back.
    """
    callback = metering.get("on_dispatch")
    if callable(callback):
        callback()


@contextmanager
def production_attempt(*, model, defer_on_reserve_failure=False):
    current = _scope.get()
    evidence: dict[str, Any] = {}
    if current is None:
        yield evidence
        return
    attempt_id = "model-attempt-" + uuid4().hex
    try:
        current.meter.reserve_attempt(attempt_id, purpose=current.command_id, autonomous=current.autonomous)
    except (httpx.HTTPError, ValueError) as exc:
        from harness.production.store import ProductionBudgetExceeded

        if (defer_on_reserve_failure or current.context.get("maintenance") is True) and isinstance(
            exc, (httpx.HTTPError, ProductionBudgetExceeded)
        ):
            from harness.production.maintenance import ProductionMaintenanceDeferred

            raise ProductionMaintenanceDeferred("production_maintenance_reservation_unavailable") from exc
        raise
    usage = {"model": model, "outcome": "unknown", "source": "unavailable"}
    body = (current.context.get("body") or {}).get("snapshot") or current.context.get("last_known_body_identity") or {}
    usage.update({key: body[key] for key in ("body_instance_id", "episode_id") if body.get(key) is not None})
    begin = getattr(current.meter, "begin_attempt", None)
    if callable(begin):
        begin(attempt_id, usage)
    checkpoint = getattr(current.meter, "checkpoint_usage", None)
    if callable(checkpoint):
        evidence["checkpoint"] = lambda actual: checkpoint(attempt_id, {**usage, **actual})
    if current.on_dispatch is not None:
        dispatched = False

        def on_dispatch():
            nonlocal dispatched
            if not dispatched:
                current.on_dispatch()
                dispatched = True

        evidence["on_dispatch"] = on_dispatch
    try:
        yield evidence
        usage["outcome"] = "completed"
    except BaseException as exc:
        usage.update(outcome="failed", error_type=type(exc).__name__)
        raise
    finally:
        # Preserve whether tokens came from the provider or an estimate. No text,
        # messages, credentials or prompt is ever persisted through this API.
        if evidence.get("usage") is not None:
            usage.update(evidence["usage"])
        current.meter.record_usage(attempt_id, usage)


def install_metering_routes(app, supervisor, *, token):
    from fastapi import Header, HTTPException

    def authorize(authorization):
        if not hmac.compare_digest(authorization or "", "Bearer " + token):
            raise HTTPException(401, "invalid service token")
        runtime = getattr(supervisor, "production_runtime", None)
        if runtime is None:
            raise HTTPException(409, "production mechanism is not enabled")
        return runtime.store

    @app.post("/v1/production/context")
    async def context(request: ContextRequest, authorization: str | None = Header(default=None)):
        authorize(authorization)
        result = await supervisor.production_runtime.context(request.scope, autonomous=request.autonomous)
        return {**result, "runtime_lifecycle": str(getattr(supervisor, "lifecycle", "UNKNOWN"))}

    @app.post("/v1/production/model-attempts")
    def reserve(request: AttemptRequest, authorization: str | None = Header(default=None)):
        store = authorize(authorization)
        if request.autonomous and getattr(supervisor, "lifecycle", None) != "RUNNING":
            raise HTTPException(409, "autonomous model attempts require a running Runtime")
        try:
            return store.reserve_attempt(request.attempt_id, purpose=request.purpose, autonomous=request.autonomous)
        except Exception as exc:
            from harness.production.store import ProductionBudgetExceeded

            if isinstance(exc, ProductionBudgetExceeded):
                raise HTTPException(429, "autonomous model budget exhausted") from exc
            if isinstance(exc, ValueError):
                raise HTTPException(409, str(exc)) from exc
            raise

    @app.post("/v1/production/model-usage")
    def usage(request: UsageRequest, authorization: str | None = Header(default=None)):
        store = authorize(authorization)
        try:
            return store.record_usage(request.attempt_id, request.usage)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
