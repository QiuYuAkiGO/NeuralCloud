"""The role API deliberately has no archive/restore/SQL/filesystem endpoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
import secrets
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import asyncio
import threading

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .contracts import (
    Correction,
    Recall,
    SourceEvent,
    SourceEventV2,
    DurableReceipt,
    MissingReceipt,
    RecordView,
    RecallResult,
)
from .storage import MemoryStore, Refusal
from .self_history import QqSelfHistoryQuery, QqSelfHistoryResult, recall_self_history

LOADED_IMPLEMENTATION = {
    "module": str(Path(__file__).resolve()),
    "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
}


def create_app(store: MemoryStore, token: str, *, close_on_shutdown=False, retriever=None):
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
        result = store.health()
        result["source_event_versions"] = [1, 2]
        result["qq_self_history"] = True
        result["implementation"] = LOADED_IMPLEMENTATION
        if retriever is not None:
            if hasattr(retriever, "status"):
                result["workflow"] = retriever.status()
            base = getattr(retriever, "base", retriever)
            result["source_event_versions"] = [1, 2]
            result["retrieval_v2"] = True
            result["hybrid_configured"] = base.hybrid
            result["identity_configured"] = base.identities is not None
            # Capability only; per-request diagnostics and worker health report actual readiness.
            result["semantic_search"] = base.hybrid and base.semantic is not None
        return result

    @app.post("/v1/recall/qq-self-history", response_model=QqSelfHistoryResult)
    def qq_self_history(request: QqSelfHistoryQuery):
        # The trusted channel supplies the peer. The store establishes authorship from original message metadata.
        return recall_self_history(store, request)

    @app.post("/v1/recall", response_model=RecallResult, response_model_exclude_none=True)
    async def recall(request: Recall, connection: Request):
        if retriever is not None:
            if hasattr(retriever, "plan"):
                if request.workflow == "compat":
                    if request.constraints or request.targets or request.intent != "auto":
                        raise Refusal("WORKFLOW_REQUIRED_FOR_CONSTRAINTS")
                    return await run_in_threadpool(
                        retriever.base.recall, request.query, request.limit, request.subject, request.mode
                    )
                cancel = threading.Event()
                pending = asyncio.create_task(
                    run_in_threadpool(
                        retriever.recall,
                        request.query,
                        request.limit,
                        request.subject,
                        request.mode,
                        request.intent,
                        request.targets,
                        request.constraints,
                        cancel=cancel,
                    )
                )
                try:
                    while not pending.done():
                        await asyncio.wait({pending}, timeout=0.025)
                        if await connection.is_disconnected():
                            cancel.set()
                    result = await pending
                finally:
                    cancel.set()
                logging.getLogger("uvicorn.error").info(
                    "MEMORY_WORKFLOW %s",
                    json.dumps(
                        {
                            "query_sha256": hashlib.sha256(request.query.encode()).hexdigest(),
                            "assessment": result.get("assessment"),
                            "diagnostics": result.get("diagnostics"),
                        }
                    ),
                )
                return result
            return await run_in_threadpool(
                retriever.recall, request.query, request.limit, request.subject, request.mode
            )
        return await run_in_threadpool(store.recall, request.query, request.limit)

    @app.get("/v1/records/{record_id}", response_model=RecordView, response_model_exclude_none=True)
    def read(record_id: str, offset: int = Query(0, ge=0), length: int = Query(4096, ge=1, le=16384)):
        return store.read(record_id, offset, length)

    @app.post("/v1/ingest", response_model=DurableReceipt)
    def ingest(event: SourceEvent | SourceEventV2):
        return store.ingest(event)

    @app.post("/v1/candidate", response_model=DurableReceipt)
    def candidate(event: SourceEvent):
        if event.kind not in {"summary", "relationship"} or not event.source_ids:
            raise Refusal("CANDIDATE_REQUIRES_SOURCES")
        return store.ingest(event)

    @app.post("/v1/corrections", response_model=DurableReceipt)
    def correct(command: Correction):
        return store.correct(command)

    @app.get("/v1/receipts", response_model=DurableReceipt | MissingReceipt)
    def receipt(source_instance: str = Query(max_length=128), event_id: str = Query(max_length=256)):
        return store.receipt(source_instance, event_id)

    @app.get("/v1/changes")
    def changes(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return store.changes(after, limit)

    return app
