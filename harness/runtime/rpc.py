"""HTTP JSON transport for the Cognitive/Homeostasis command boundary."""

from __future__ import annotations

from typing import Any

import httpx

from harness.contracts.decisions import AutonomousAssessment, PreparedResponse
from harness.contracts.envelope import DomainCommandEnvelope, DomainCommandResult, VersionVector
from harness.runtime.adapters import DomainSnapshot, ServiceHealth
from harness.homeostasis.models import QQFactLookup, QQFactStatus


class DomainRpcError(ConnectionError):
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

    def _rpc_failure(
        self,
        method: str,
        path: str,
        exc: BaseException,
        response: httpx.Response | None = None,
    ) -> DomainRpcError:
        name = type(exc).__name__
        message = str(exc).strip()
        parts = [name] if not message else [f"{name}: {message}"]
        if response is not None:
            parts.append(f"status={response.status_code}")
            body = " ".join((response.text or "").split())
            if body:
                parts.append(body[:240])
        return DomainRpcError(
            f"{self.service_name} RPC {method} {path} failed: {'; '.join(parts)}"
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        # Transport blips (dead keep-alive, empty body) are retried once.
        # HTTP status errors are application outcomes and are not retried.
        last_response: httpx.Response | None = None
        for attempt in range(2):
            try:
                response = await self.client.request(method, path, **kwargs)
                last_response = response
                response.raise_for_status()
                decoded = response.json()
            except httpx.HTTPStatusError as exc:
                raise self._rpc_failure(method, path, exc, exc.response) from exc
            except (httpx.RequestError, ValueError) as exc:
                last_response = getattr(exc, "response", None) or last_response
                if attempt == 0:
                    continue
                raise self._rpc_failure(method, path, exc, last_response) from exc
            else:
                payload = decoded if isinstance(decoded, dict) else None
                if payload is None:
                    raise DomainRpcError(
                        f"{self.service_name} RPC returned a non-object payload"
                    )
                return payload
        raise self._rpc_failure(
            method,
            path,
            RuntimeError("domain RPC retry loop exited without a result"),
            last_response,
        )


class HttpCognitiveClient(_HttpDomainClient):
    def __init__(self, base_url: str, *, service_token: str, **kwargs: Any) -> None:
        super().__init__(
            base_url,
            service_token=service_token,
            service_name="cognitive",
            **kwargs,
        )

    async def observe_emotion(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def appraise_affect(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def read_affect_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/affect/snapshot", json=payload)

    async def prepare_response(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[PreparedResponse]:
        result = await self.command(command)
        return DomainCommandResult[PreparedResponse].model_validate(result.model_dump(mode="json"))

    async def prepare_qq_response(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def prepare_qq_turn(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def resume_qq_response(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def prepare_board_entry(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def prepare_capability_call(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def prepare_web_step(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def prepare_goal_decision(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def prepare_diary_entry(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def commit_board_entry(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def commit_diary_entry(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return DomainCommandResult[dict[str, Any]].model_validate((await self.command(command)).model_dump(mode="json"))

    async def assess_autonomous(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[AutonomousAssessment]:
        result = await self.command(command)
        return DomainCommandResult[AutonomousAssessment].model_validate(
            result.model_dump(mode="json")
        )

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

    async def read_qq_fact(self, lookup: QQFactLookup) -> QQFactStatus:
        result = QQFactStatus.model_validate(await self._request(
            "POST", "/v1/qq-facts:read", json=lookup.model_dump(mode="json")))
        if result.fact_key != lookup.fact_key:
            raise DomainRpcError("homeostasis fact lookup binding mismatch")
        return result

    async def apply_heartbeat(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self.apply_event(command)

    async def apply_outcome(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self.apply_event(command)

    async def assimilate_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self.apply_event(command)

    async def apply_appraisal(self, command: DomainCommandEnvelope[dict[str, Any]]) -> DomainCommandResult[dict[str, Any]]:
        return await self.apply_event(command)

    async def apply_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self.apply_event(command)

    async def propose_policies(self, *, version_vector: VersionVector) -> list[dict[str, Any]]:
        payload = await self._request(
            "POST", "/v1/policies", json={"version_vector": version_vector.model_dump(mode="json")}
        )
        policies = payload.get("policies") or []
        return [dict(policy) for policy in policies]

    async def read_traces(
        self, *, since: str | None = None, until: str | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "POST",
            "/v1/traces:read",
            json={"since": since, "until": until, "limit": limit},
        )
        traces = payload.get("traces") or []
        return [dict(item) for item in traces if isinstance(item, dict)]


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
            "ObserveEmotion": getattr(service, "observe_emotion", None),
            "AppraiseAffect": getattr(service, "appraise_affect", None),
            "AssessAutonomous": getattr(service, "assess_autonomous", None),
            "PrepareResponse": getattr(service, "prepare_response", None),
            "PrepareQQResponse": getattr(service, "prepare_qq_response", None),
            "PrepareQQTurn": getattr(service, "prepare_qq_turn", None),
            "ResumeQQResponse": getattr(service, "resume_qq_response", None),
            "PrepareBoardEntry": getattr(service, "prepare_board_entry", None),
            "PrepareCapabilityCall": getattr(service, "prepare_capability_call", None),
            "PrepareWebStep": getattr(service, "prepare_web_step", None),
            "PrepareGoalDecision": getattr(service, "prepare_goal_decision", None),
            "CommitBoardEntry": getattr(service, "commit_board_entry", None),
            "PrepareDiaryEntry": getattr(service, "prepare_diary_entry", None),
            "CommitDiaryEntry": getattr(service, "commit_diary_entry", None),
            "CommitInteraction": getattr(service, "commit_interaction", None),
            "ApplyEvent": getattr(service, "apply_event", None),
            "ApplyHeartbeat": getattr(service, "apply_heartbeat", None),
            "ApplyOutcome": getattr(service, "apply_outcome", None),
            "ApplyAppraisal": getattr(service, "apply_appraisal", None),
            "AssimilateObservation": getattr(service, "assimilate_observation", None),
            "ApplyObservation": getattr(service, "apply_observation", None),
        }
        method = methods.get(command_type)
        if method is None:
            raise HTTPException(status_code=404, detail="unsupported command")
        result = await method(envelope)
        return result.model_dump(mode="json")

    if service_name == "cognitive":
        @app.post("/v1/affect/snapshot", dependencies=[Depends(authenticate)])
        async def affect_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
            method = getattr(service, "read_affect_snapshot", None)
            if method is None:
                raise HTTPException(status_code=404, detail="unsupported command")
            return await method(payload)

        @app.post("/v1/maintenance/pause", dependencies=[Depends(authenticate)])
        async def pause_maintenance(payload: dict[str, Any]) -> dict[str, Any]:
            drained = await service.pause_maintenance(timeout=float(payload.get("timeout", 5.0)))
            return {"drained": drained}

        @app.post("/v1/maintenance/resume", dependencies=[Depends(authenticate)])
        async def resume_maintenance() -> dict[str, bool]:
            await service.resume_maintenance()
            return {"resumed": True}
    else:
        @app.post("/v1/qq-facts:read", dependencies=[Depends(authenticate)])
        async def read_qq_fact(payload: dict[str, Any]) -> dict[str, Any]:
            try:
                lookup = QQFactLookup.model_validate(payload)
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid QQ fact lookup") from None
            method = getattr(service, "read_qq_fact", None)
            if method is None:
                raise HTTPException(status_code=501, detail="QQ fact lookup unavailable")
            return (await method(lookup)).model_dump(mode="json")

        @app.post("/v1/policies", dependencies=[Depends(authenticate)])
        async def policies(payload: dict[str, Any]) -> dict[str, Any]:
            vector = VersionVector.model_validate(payload.get("version_vector") or {})
            return {"policies": await service.propose_policies(version_vector=vector)}

        @app.post("/v1/traces:read", dependencies=[Depends(authenticate)])
        async def read_traces(payload: dict[str, Any]) -> dict[str, Any]:
            method = getattr(service, "read_traces", None)
            if method is None:
                raise HTTPException(status_code=501, detail="homeostasis traces unavailable")
            traces = await method(
                since=payload.get("since"),
                until=payload.get("until"),
                limit=int(payload.get("limit") or 5000),
            )
            return {"traces": traces}

    return app
