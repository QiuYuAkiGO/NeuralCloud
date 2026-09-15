"""Authenticated Linux body service; never constructs a mind or calls a model."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from harness.board.store import BoardStore, BoardStoreError
from harness.embodiment.board_contracts import (
    BoardAppendRequest, BoardEvidenceRequest, BoardReadRequest, BoardReconcileRequest, BoardScope,
)
from harness.embodiment.capabilities import (
    CapabilityDescriptor, CapabilityRequirement, CapabilityStatus, HealthStatus, ProviderCapabilities, ProviderHealth,
)
from harness.embodiment.providers.board import BOARD_CAPABILITIES, PROVIDER_ID, BoardError, BoardProvider
from harness.research.manifest import EmbodimentCondition


class LocalBoardDriver:
    """SQLite is the physical actuator; all HTTP writes pass its negotiated provider."""

    def __init__(self, store: BoardStore):
        self.store = store

    async def capabilities(self):
        scope = self.store.scope
        return ProviderCapabilities(provider_id=PROVIDER_ID, provider_kind="linux_persistent_thought_board",
            provider_revision="board-v1:" + hashlib.sha256(scope.model_dump_json().encode()).hexdigest(),
            body_instance_id=scope.body_instance_id, observed_at=datetime.now(UTC),
            capabilities=[CapabilityDescriptor(capability=name, status=CapabilityStatus.AVAILABLE,
                implementation_version="1", limits={"board_id": scope.board_id, "owner_id": scope.owner_id,
                    "max_text_characters": 8000, "max_read_entries": 100, "max_request_seconds": 60,
                    "max_reconcile_seconds": 15, "durable_receipt_required": True, "append_only": True})
                for name in BOARD_CAPABILITIES])

    async def health(self):
        healthy = await asyncio.to_thread(self.store.healthy)
        state = HealthStatus.HEALTHY if healthy else HealthStatus.UNAVAILABLE
        return ProviderHealth(provider_id=PROVIDER_ID, body_instance_id=self.store.scope.body_instance_id,
            status=state, observed_at=datetime.now(UTC), capability_health=dict.fromkeys(BOARD_CAPABILITIES, state),
            detail={"reason_code": "board_ready" if healthy else "board_store_unavailable", "storage": "sqlite_full_wal"})

    async def read(self, request):
        return await asyncio.to_thread(self.store.read, request)

    async def append(self, request):
        return await asyncio.to_thread(self.store.append, request)

    async def reconcile(self, request):
        return await asyncio.to_thread(self.store.reconcile, request)

    async def evidence(self, request):
        return await asyncio.to_thread(self.store.evidence, request)

    async def close(self):
        await asyncio.to_thread(self.store.close)


def _token(value: str | None, path: str | Path | None) -> str:
    if (value is None) == (path is None):
        raise ValueError("board service requires exactly one credential source")
    if path is not None:
        token_path = Path(path)
        if os.name != "nt" and token_path.stat().st_mode & 0o077:
            raise ValueError("board_token_file_requires_private_permissions")
        value = token_path.read_text(encoding="utf-8").strip()
    if not isinstance(value, str) or not 32 <= len(value) <= 4096 or any(not 33 <= ord(char) <= 126 for char in value):
        raise ValueError("secure_board_token_required")
    return value


def create_app(*, store: BoardStore, token: str | None = None, token_file: str | Path | None = None,
               condition: EmbodimentCondition | None = None) -> FastAPI:
    secret = _token(token, token_file)
    condition = condition or EmbodimentCondition(mode="active", body_instance_id=store.scope.body_instance_id,
        capability_requirements=[CapabilityRequirement(capability=name, required=True) for name in BOARD_CAPABILITIES])
    provider = BoardProvider(condition, LocalBoardDriver(store), store.scope)

    async def authenticated(authorization: str | None = Header(default=None)):
        expected = ("Bearer " + secret).encode("ascii")
        presented = (authorization or "").encode("utf-8")
        if not hmac.compare_digest(presented, expected):
            raise HTTPException(status_code=401, detail="board_authentication_required",
                                headers={"WWW-Authenticate": "Bearer"})

    @asynccontextmanager
    async def lifespan(app):
        await provider.activate()
        try:
            yield
        finally:
            await provider.close()

    app = FastAPI(title="Misty Linux thought board", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(authenticated)], lifespan=lifespan)
    app.state.provider = provider
    app.state.store = store

    async def body_error(request, exc):
        code = getattr(exc, "code", "board_store_unavailable")
        statuses = {"scope_not_allowed": 403, "board_identity_mismatch": 403,
                    "board_idempotency_conflict": 409, "board_entry_not_found": 404,
                    "request_expired": 408, "request_time_invalid": 408}
        return JSONResponse(status_code=statuses.get(code, 503), content={"detail": code})

    app.add_exception_handler(BoardError, body_error)
    app.add_exception_handler(BoardStoreError, body_error)
    app.add_exception_handler(sqlite3.Error, body_error)

    @app.get("/health")
    async def health():
        return await provider.health()

    @app.get("/v1/capabilities")
    async def capabilities():
        return await provider.capabilities()

    @app.get("/v1/status")
    async def status():
        return await provider.status()

    @app.post("/v1/board/read")
    async def read(request: BoardReadRequest):
        return await provider.read(request)

    @app.post("/v1/board/append")
    async def append(request: BoardAppendRequest):
        return await provider.append(request)

    @app.post("/v1/board/reconcile")
    async def reconcile(request: BoardReconcileRequest):
        return await provider.reconcile(request)

    @app.post("/v1/board/evidence")
    async def evidence(request: BoardEvidenceRequest):
        return await provider.evidence(request)

    return app


def main():
    parser = argparse.ArgumentParser(description="Private Linux thought-board body")
    parser.add_argument("--state-dir", default=os.environ.get("NC_BOARD_STATE_DIR", str(Path.home() / ".local/share/neuralcloud/board")))
    parser.add_argument("--token-file", default=os.environ.get("NC_BOARD_TOKEN_FILE"))
    parser.add_argument("--host", default=os.environ.get("NC_BOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("NC_BOARD_PORT", "8793")))
    parser.add_argument("--body-instance-id", default=os.environ.get("NC_BOARD_BODY_INSTANCE_ID", "misty-linux-board-01"))
    parser.add_argument("--board-id", default=os.environ.get("NC_BOARD_ID", "misty-thoughts"))
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("board service must bind to a private loopback address")
    if args.token_file is None:
        parser.error("--token-file or NC_BOARD_TOKEN_FILE is required")
    _token(None, args.token_file)  # Refuse insecure startup before touching the database.
    scope = BoardScope(body_instance_id=args.body_instance_id, board_id=args.board_id)
    store = BoardStore(Path(args.state_dir) / "board.sqlite3", scope)
    app = create_app(store=store, token_file=args.token_file)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
