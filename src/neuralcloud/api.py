"""The role API deliberately has no archive/restore/SQL/filesystem endpoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
import secrets
import sqlite3

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from .contracts import Correction, Recall, SourceEvent
from .storage import MemoryStore, Refusal


def create_app(store: MemoryStore, token: str, *, close_on_shutdown=False):
    if len(token) < 32:
        raise ValueError("service token must contain at least 32 characters")

    async def authenticate(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "UNAUTHORIZED")

    @asynccontextmanager
    async def lifespan(app):
        yield
        if close_on_shutdown:
            store.close()

    app = FastAPI(
        title="NeuralCloud resident memory",
        version="1.0.0",
        dependencies=[Depends(authenticate)],
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def bounded_request(request, call_next):
        # Read at most 128 KiB regardless of Content-Length or chunked transport.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 128 * 1024:
                return JSONResponse({"detail": "REQUEST_TOO_LARGE"}, status_code=413)
            body.extend(chunk)
        request._body = bytes(body)
        return await call_next(request)

    @app.exception_handler(Refusal)
    async def refused(request, error):
        return JSONResponse({"detail": error.code}, status_code=409)

    @app.exception_handler(sqlite3.Error)
    async def unavailable(request, error):
        # An interrupted/full transaction never produces a durable receipt.
        return JSONResponse({"detail": "STORAGE_UNAVAILABLE"}, status_code=503)

    @app.get("/v1/health")
    def health():
        return store.health()

    @app.post("/v1/recall")
    def recall(request: Recall):
        return store.recall(request.query, request.limit)

    @app.get("/v1/records/{record_id}")
    def read(record_id: str, offset: int = Query(0, ge=0), length: int = Query(4096, ge=1, le=16384)):
        return store.read(record_id, offset, length)

    @app.post("/v1/ingest")
    def ingest(event: SourceEvent):
        return store.ingest(event)

    @app.post("/v1/candidate")
    def candidate(event: SourceEvent):
        if event.kind not in {"summary", "relationship"} or not event.source_ids:
            raise Refusal("CANDIDATE_REQUIRES_SOURCES")
        return store.ingest(event)

    @app.post("/v1/corrections")
    def correct(command: Correction):
        return store.correct(command)

    @app.get("/v1/receipts")
    def receipt(source_instance: str = Query(max_length=128), event_id: str = Query(max_length=256)):
        return store.receipt(source_instance, event_id)

    @app.get("/v1/changes")
    def changes(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return store.changes(after, limit)

    return app
