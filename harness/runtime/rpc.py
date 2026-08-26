"""HTTP JSON transport for the Cognitive/Homeostasis command boundary."""

from __future__ import annotations

from typing import Any

import httpx

from harness.contracts.decisions import PreparedResponse
from harness.contracts.envelope import DomainCommandEnvelope, DomainCommandResult, VersionVector
from harness.runtime.adapters import DomainSnapshot, ServiceHealth


class DomainRpcError(RuntimeError):
    pass


class _HttpDomainClient:
    def __init__(
        self,
        base_url: str,
        *,
        service_token: str,
        service_name: str,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_name = service_name
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {service_token}"},
        )

    async def health(self) -> ServiceHealth:
        payload = await self._request("GET", "/v1/health")
        return ServiceHealth.model_validate(payload)

    async def get_snapshot(self) -> DomainSnapshot:
        payload = await self._request("GET", "/v1/snapshot")
        return DomainSnapshot.model_validate(payload)

    async def command(
        self, envelope: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[Any]:
        payload = await self._request(
            "POST",
            f"/v1/commands/{envelope.command_type}",
            json=envelope.model_dump(mode="json"),
        )
        return DomainCommandResult[Any].model_validate(payload)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainRpcError(f"{self.service_name} RPC {method} {path} failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise DomainRpcError(f"{self.service_name} RPC returned a non-object payload")
        return payload


class HttpCognitiveClient(_HttpDomainClient):
    def __init__(self, base_url: str, *, service_token: str, **kwargs: Any) -> None:
        super().__init__(
            base_url,
            service_token=service_token,
            service_name="cognitive",
            **kwargs,
        )

    async def prepare_response(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[PreparedResponse]:
        result = await self.command(command)
        return DomainCommandResult[PreparedResponse].model_validate(result.model_dump(mode="json"))

    async def commit_interaction(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        result = await self.command(command)
        return DomainCommandResult[dict[str, Any]].model_validate(result.model_dump(mode="json"))

    async def pause_maintenance(self, *, timeout: float) -> bool:
        payload = await self._request("POST", "/v1/maintenance/pause", json={"timeout": timeout})
        return bool(payload.get("drained"))

    async def resume_maintenance(self) -> None:
        await self._request("POST", "/v1/maintenance/resume")

    async def ui_snapshot(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/ui/snapshot")

    async def control(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/ui/control",
            json={"operation": operation, "payload": payload or {}},
        )


class HttpHomeostasisClient(_HttpDomainClient):
    def __init__(self, base_url: str, *, service_token: str, **kwargs: Any) -> None:
        super().__init__(
            base_url,
            service_token=service_token,
            service_name="homeostasis",
            **kwargs,
        )

    async def apply_event(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate(
            (await self.command(command)).model_dump(mode="json")
        )

    async def apply_heartbeat(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self.apply_event(command)

    async def apply_outcome(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self.apply_event(command)

    async def propose_policies(self, *, version_vector: VersionVector) -> list[dict[str, Any]]:
        payload = await self._request(
            "POST", "/v1/policies", json={"version_vector": version_vector.model_dump(mode="json")}
        )
        policies = payload.get("policies") or []
        return [dict(policy) for policy in policies]


def create_domain_service_app(service: Any, *, service_token: str, service_name: str):
    """Create the private FastAPI surface consumed by HTTP domain clients."""

    from fastapi import Depends, FastAPI, Header, HTTPException

    app = FastAPI(title=f"NeuralCloud {service_name} service")

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {service_token}":
            raise HTTPException(status_code=401, detail="invalid service token")

    @app.get("/v1/health", dependencies=[Depends(authenticate)])
    async def health() -> dict[str, Any]:
        return (await service.health()).model_dump(mode="json")

    @app.get("/v1/snapshot", dependencies=[Depends(authenticate)])
    async def snapshot() -> dict[str, Any]:
        return (await service.get_snapshot()).model_dump(mode="json")

    @app.post("/v1/commands/{command_type}", dependencies=[Depends(authenticate)])
    async def command(command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        envelope = DomainCommandEnvelope[dict[str, Any]].model_validate(payload)
        if command_type != envelope.command_type:
            raise HTTPException(status_code=400, detail="command type mismatch")
        methods = {
            "PrepareResponse": getattr(service, "prepare_response", None),
            "CommitInteraction": getattr(service, "commit_interaction", None),
            "ApplyEvent": getattr(service, "apply_event", None),
            "ApplyHeartbeat": getattr(service, "apply_heartbeat", None),
            "ApplyOutcome": getattr(service, "apply_outcome", None),
        }
        method = methods.get(command_type)
        if method is None:
            raise HTTPException(status_code=404, detail="unsupported command")
        result = await method(envelope)
        return result.model_dump(mode="json")

    if service_name == "cognitive":
        @app.post("/v1/maintenance/pause", dependencies=[Depends(authenticate)])
        async def pause_maintenance(payload: dict[str, Any]) -> dict[str, Any]:
            drained = await service.pause_maintenance(timeout=float(payload.get("timeout", 5.0)))
            return {"drained": drained}

        @app.post("/v1/maintenance/resume", dependencies=[Depends(authenticate)])
        async def resume_maintenance() -> dict[str, bool]:
            await service.resume_maintenance()
            return {"resumed": True}
    else:
        @app.post("/v1/policies", dependencies=[Depends(authenticate)])
        async def policies(payload: dict[str, Any]) -> dict[str, Any]:
            vector = VersionVector.model_validate(payload.get("version_vector") or {})
            return {"policies": await service.propose_policies(version_vector=vector)}

    return app
