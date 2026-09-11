"""Authenticated observation-only routes; no Runtime events are submitted."""

from __future__ import annotations

from collections.abc import Callable
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError

from harness.memory.qq_observations import (
    QQObservation,
    QQObservationConflict,
    QQObservationInput,
    QQObservationResult,
    QQObservationStorageError,
    QQObservationStore,
)


def create_qq_observation_router(store: QQObservationStore, *, authenticate: Callable) -> APIRouter:
    router = APIRouter(prefix="/v1/qq", dependencies=[Depends(authenticate)])

    @router.post("/observations", response_model=QQObservationResult)
    def observe(incoming: QQObservationInput) -> QQObservationResult:
        try:
            return store.observe(incoming)
        except QQObservationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (sqlite3.Error, OSError, QQObservationStorageError) as exc:
            raise HTTPException(status_code=503, detail="QQ observation persistence is unavailable") from exc

    @router.get("/observations", response_model=list[QQObservation])
    def find(
        group_id: str = Query(pattern=r"^[1-9][0-9]{0,19}$"),
        member_id: str | None = Query(default=None, pattern=r"^[1-9][0-9]{0,19}$"),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[QQObservation]:
        try:
            return store.find(group_id=group_id, member_id=member_id, limit=limit)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="invalid QQ observation query") from exc
        except (sqlite3.Error, OSError, QQObservationStorageError) as exc:
            raise HTTPException(status_code=503, detail="QQ observation persistence is unavailable") from exc

    return router
