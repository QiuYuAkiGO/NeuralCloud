"""Operator SSH server configuration and approval routes."""

from __future__ import annotations

from fastapi import HTTPException, Request

from harness.embodiment.server_contracts import ServerError
from harness.runtime.server_config import ServerConfigurationError


def add_server_routes(router, supervisor):
    def store():
        value = getattr(supervisor, "server_config_store", None)
        if value is None:
            value = getattr(getattr(supervisor, "server", None), "config_store", None)
        if value is None:
            raise HTTPException(status_code=503, detail="server_config_unavailable")
        return value

    def controller():
        value = getattr(supervisor, "server", None)
        if value is None:
            raise HTTPException(status_code=503, detail="server_not_configured")
        return value

    def capability_row(identity):
        capabilities = getattr(supervisor, "capabilities", None)
        if capabilities is None:
            return None
        return next((row for row in capabilities.calls.all() if row.get("server_call_id") == identity), None)

    async def body(request):
        try:
            if len(await request.body()) > 65536:
                raise ValueError()
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except ValueError:
            raise HTTPException(status_code=422, detail="server_config_request_invalid") from None

    @router.get("/v1/server/config")
    async def read_config():
        try:
            return store().snapshot()
        except ServerConfigurationError:
            raise HTTPException(status_code=503, detail="server_config_unavailable") from None

    @router.put("/v1/server/config")
    async def save_config(request: Request):
        try:
            return store().stage(await body(request))
        except ServerConfigurationError as exc:
            code = str(exc)
            status = 409 if code == "server_config_revision_conflict" else 422
            if code == "server_config_write_failed":
                status = 503
            raise HTTPException(status_code=status, detail=code) from None

    @router.post("/v1/server/test")
    async def test_connection(request: Request):
        try:
            settings, key = store().test_configuration(await body(request))
            return await controller().test(settings, key)
        except ServerConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except HTTPException:
            raise
        except ServerError as exc:
            raise HTTPException(status_code=503, detail=exc.code) from None
        except Exception:
            raise HTTPException(status_code=503, detail="server_connection_test_failed") from None

    @router.post("/v1/server/keys:generate")
    async def generate_key():
        try:
            return store().generate_key()
        except ServerConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except Exception:
            raise HTTPException(status_code=503, detail="server_key_generation_failed") from None

    @router.get("/v1/server/status")
    async def status():
        return await controller().status()

    @router.get("/v1/server/commands")
    async def commands(status: str | None = None):
        if status is not None and status not in {
            "pending_approval", "completed", "failed", "cancelled", "rejected", "uncertain", "started",
        }:
            raise HTTPException(status_code=422, detail="server_command_status_invalid")
        return {"commands": controller().commands(status=status)}

    @router.post("/v1/server/commands/{command_id}/{operation}")
    async def operate(command_id: str, operation: str):
        if operation not in {"approve", "reject"}:
            raise HTTPException(status_code=404, detail="unknown server command operation")
        if not command_id or len(command_id) > 256 or any(c.isspace() for c in command_id):
            raise HTTPException(status_code=422, detail="server_command_id_invalid")
        row = capability_row(command_id)
        guard = (lambda: supervisor.capabilities._server_guard(row)) if row is not None else None
        try:
            if operation == "approve":
                result = await controller().approve(command_id, guard=guard)
            else:
                result = await controller().reject(command_id, guard=guard)
        except ServerError as exc:
            status_code = 404 if exc.code == "server_command_not_found" else 409
            raise HTTPException(status_code=status_code, detail=exc.code) from None
        capabilities = getattr(supervisor, "capabilities", None)
        if capabilities is not None:
            await capabilities.reconcile()
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json")
        return result
