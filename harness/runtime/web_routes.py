"""Operator web configuration routes; mounted only on the authenticated Runtime router."""

from __future__ import annotations

from fastapi import HTTPException, Request

from harness.runtime.web_config import WebConfigurationError


def add_web_routes(router, supervisor):
    def store():
        value = getattr(supervisor, "web_config_store", None)
        if value is None:
            value = getattr(getattr(supervisor, "web", None), "config_store", None)
        if value is None:
            raise HTTPException(status_code=503, detail="web_config_unavailable")
        return value

    def controller():
        value = getattr(supervisor, "web", None)
        if value is None:
            raise HTTPException(status_code=503, detail="web_not_configured")
        return value

    async def body(request):
        try:
            # Credentials and request bodies must not appear in validation errors.
            if len(await request.body()) > 65536:
                raise ValueError()
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except ValueError:
            raise HTTPException(status_code=422, detail="web_config_request_invalid") from None

    @router.get("/v1/web/config")
    async def read_config():
        try:
            return store().snapshot()
        except WebConfigurationError:
            raise HTTPException(status_code=503, detail="web_config_unavailable") from None

    @router.put("/v1/web/config")
    async def save_config(request: Request):
        try:
            return store().stage(await body(request))
        except WebConfigurationError as exc:
            code = str(exc)
            status = 409 if code == "web_config_revision_conflict" else 422
            if code == "web_config_write_failed":
                status = 503
            raise HTTPException(status_code=status, detail=code) from None

    @router.post("/v1/web/test")
    async def test_connection(request: Request):
        try:
            settings, key = store().test_configuration(await body(request))
            return await controller().test(settings, key)
        except WebConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except HTTPException:
            raise
        except Exception:
            # Provider messages can contain endpoint credentials or response bodies.
            raise HTTPException(status_code=503, detail="web_connection_test_failed") from None

    @router.get("/v1/web/status")
    async def status():
        return await controller().status()
