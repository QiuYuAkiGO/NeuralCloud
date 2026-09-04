from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from harness.contracts.decisions import GoalProposal, PreparedResponse
from harness.contracts.envelope import DomainCommandResult, VersionVector
from harness.embodiment.capabilities import (
    ACTION_EXECUTE,
    ACTION_RECONCILE,
    ACTION_SAFE_POINT,
    ACTION_VERIFY,
    EVIDENCE_REFERENCE,
    OBSERVATION_SNAPSHOT,
    PHYSIOLOGY_BODY_STATE,
    CapabilityRequirement,
)
from harness.embodiment.contracts import ActionIntent, EmbodiedActionSpec, IntentionalNoopCriterion
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.providers.mistyai import (
    MistyAIActionBackend,
    MistyAIBodyStateProvider,
    MistyAICapabilityProvider,
    MistyAIClient,
    MistyAIError,
    MistyAIPerceptionBackend,
)
from harness.embodiment.providers.simulator import (
    DeterministicEmbodimentSimulator,
    SimulatorConfig,
)
from harness.runtime.action import ActionCoordinator
from harness.research.manifest import EmbodimentCondition
from harness.runtime.adapters import DomainSnapshot, PassiveHomeostasisAdapter, ServiceHealth
from harness.runtime.models import ActionExecution, ActionExecutionState, RuntimeEventType
from harness.runtime.supervisor import RuntimeSupervisor
from tests.test_runtime_supervisor import _manifest

NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)
EVIDENCE_BYTES = b"mistyai evidence fixture"
EVIDENCE_DIGEST = hashlib.sha256(EVIDENCE_BYTES).hexdigest()
EVIDENCE = f"evidence://sha256/{EVIDENCE_DIGEST}"


def _execution(action_id: str = "action-1") -> ActionExecution:
    return ActionExecution(
        action_id=action_id,
        goal_id="goal-1",
        goal="Wait briefly",
        idempotency_key=f"idem-{action_id}",
        state=ActionExecutionState.RUNNING,
        risk_class="low",
        reversible=True,
        verification_method="intentional_noop",
        version_vector=VersionVector(),
        deadline=NOW + timedelta(minutes=1),
        embodied_action=EmbodiedActionSpec(
            intent=ActionIntent(intent_id="control.wait.v1", arguments={"duration_ms": 1}),
            success_criteria=[IntentionalNoopCriterion(condition="waited")],
        ),
    )


def _native_result(*, success: bool = True, unknown: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "body-contract-v1",
        "success": success,
        "summary": "fixture result",
        "evidence_refs": [EVIDENCE] if success else [],
        "external_effect_ref": None,
        "safe_point": "action-1:complete",
        "continuation": None,
        "unknown": unknown,
        "retry_safe": not success and not unknown,
    }


def _misty_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/capabilities":
        return httpx.Response(
            200,
            json={
                "schema_version": "body-contract-v1",
                "body_instance_id": "body-1",
                "contract_versions": ["body-contract-v1"],
                "desktop_backend": "fixture",
                "display": {"name": ":1", "width": 640, "height": 480, "scale": 1.0},
                "actuators": ["gui"],
                "skills": ["control.wait.v1"],
                "network_phase": 0,
                "policy_profile": "fixture",
                "extensions": {
                    "body_state_v1": True,
                    "virtual_resource_ledger": True,
                    "social_observation_v1": False,
                    "experiment_control": False,
                },
            },
        )
    if path == "/v1/health":
        component = {"status": "healthy", "detail": None}
        return httpx.Response(
            200,
            json={
                "schema_version": "body-contract-v1",
                "body_instance_id": "body-1",
                "status": "healthy",
                "database": component,
                "environment": component,
                "observation": component,
                "agent_backends": {"screen": "healthy"},
                "action": component,
                "evidence_store": component,
                "body_state": component,
                "physiology": component,
                "agency_state": "AVAILABLE",
                "active_action_id": None,
                "manual_takeover_active": False,
                "checked_at": NOW.isoformat(),
            },
        )
    if path == "/v1/observations:capture":
        return httpx.Response(
            200,
            json={
                "schema_version": "body-contract-v1",
                "observation_id": "obs-1",
                "body_instance_id": "body-1",
                "environment_revision": 3,
                "captured_at": NOW.isoformat(),
                "frame": {
                    "evidence_ref": EVIDENCE,
                    "media_type": "image/png",
                    "sha256": EVIDENCE_DIGEST,
                    "width": 640,
                    "height": 480,
                },
                "display": {"name": ":1", "width": 640, "height": 480, "scale": 1.0},
                "pointer": {"x": 1, "y": 2},
                "active_window": None,
                "windows": [],
                "a11y_ref": None,
                "fs_events": [],
                "running_apps": [],
                "backend_health": {"screen": "healthy"},
            },
        )
    if path == f"/v1/evidence/{EVIDENCE_DIGEST}":
        return httpx.Response(
            200,
            content=EVIDENCE_BYTES,
            headers={"Content-Type": "image/png"},
        )
    if path == "/v1/actions" and request.method == "POST":
        action_id = json.loads(request.content)["execution"]["action_id"]
        return httpx.Response(
            202,
            json={
                "schema_version": "body-contract-v1",
                "action_id": action_id,
                "state": "accepted",
                "current_step": 0,
                "result": None,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
            },
        )
    if path.startswith("/v1/actions/") and ":" not in path and request.method == "GET":
        action_id = path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "schema_version": "body-contract-v1",
                "action_id": action_id,
                "state": "execution_succeeded",
                "current_step": 1,
                "result": _native_result(),
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
            },
        )
    if path.endswith(":request-safe-point"):
        result = _native_result(success=False)
        result["safe_point"] = "action-1:safe"
        return httpx.Response(200, json=result)
    if path.endswith(":verify") or path.endswith(":reconcile"):
        return httpx.Response(200, json=_native_result())
    if path == "/v1/body-state:capture":
        energy = {
            "capacity_units": 100,
            "available_units": 75,
            "available_ratio": 0.75,
            "delta_units_per_minute": -0.1,
            "charging": False,
            "estimated_time_to_terminal_seconds": 45000,
            "terminal": False,
        }
        body_state = {
            "schema_version": "body-state-v1",
            "body_instance_id": "body-1",
            "episode_id": "episode-1",
            "revision": 4,
            "captured_at": NOW.isoformat(),
            "energy": energy,
            "thermal": None,
            "compute": None,
            "integrity": None,
            "evidence_refs": [EVIDENCE],
        }
        return httpx.Response(
            200,
            json={
                "schema_version": "body-contract-v1",
                "request_id": "body-state-request-1",
                "body_state": body_state,
                "body_instance_id": "body-1",
                "episode_id": "episode-1",
                "revision": 4,
                "captured_at": NOW.isoformat(),
                "energy": energy,
                "evidence_refs": [EVIDENCE],
            },
        )
    return httpx.Response(404, json={"detail": "not found"})


@pytest.mark.parametrize("provider_kind", ["simulator", "mistyai"])
def test_provider_neutral_contract_suite(provider_kind: str) -> None:
    async def scenario() -> None:
        if provider_kind == "simulator":
            provider = DeterministicEmbodimentSimulator(
                SimulatorConfig(body_instance_id="body-1", physiology=True),
                clock=lambda: NOW,
            )
            action_backend = provider
            perception_backend = provider
            capability_provider = provider
            body_state_provider = provider
            http_client = None
        else:
            http_client = httpx.AsyncClient(
                transport=httpx.MockTransport(_misty_handler),
                base_url="http://misty.test",
            )
            client = MistyAIClient("http://misty.test", token="test-token", client=http_client)
            action_backend = MistyAIActionBackend(
                client,
                poll_interval_seconds=0.001,
                clock=lambda: NOW,
            )
            perception_backend = MistyAIPerceptionBackend(client)
            capability_provider = MistyAICapabilityProvider(client)
            body_state_provider = MistyAIBodyStateProvider(client)

        capabilities = await capability_provider.capabilities()
        names = {item.capability for item in capabilities.capabilities}
        assert {
            ACTION_EXECUTE,
            ACTION_SAFE_POINT,
            ACTION_VERIFY,
            ACTION_RECONCILE,
            OBSERVATION_SNAPSHOT,
            EVIDENCE_REFERENCE,
            PHYSIOLOGY_BODY_STATE,
        }.issubset(names)
        health = await capability_provider.health()
        assert health.provider_id == capabilities.provider_id
        assert health.body_instance_id == "body-1"
        observation = await perception_backend.observe(now=NOW)
        assert observation.body_instance_id == "body-1"
        assert observation.evidence_refs
        execution = _execution()
        result = await action_backend.execute(execution)
        assert result.success and result.evidence_refs
        verified = await action_backend.verify(execution)
        assert verified.success and verified.evidence_refs
        safe = await action_backend.request_safe_point(execution)
        assert safe.safe_point
        reconciled = await action_backend.reconcile(execution)
        assert reconciled.success
        body = await body_state_provider.snapshot()
        assert body.revision > 0 and body.evidence_refs

        if http_client is not None:
            assert body.energy.estimated_time_to_empty_seconds == 45000
            await http_client.aclose()

    asyncio.run(scenario())


def test_misty_evidence_verification_cache_is_shared_across_provider_surfaces() -> None:
    async def scenario() -> None:
        evidence_downloads = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal evidence_downloads
            if request.url.path.startswith("/v1/evidence/"):
                evidence_downloads += 1
            return _misty_handler(request)

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        await MistyAIPerceptionBackend(client).observe(now=NOW)
        backend = MistyAIActionBackend(client, poll_interval_seconds=0.001, clock=lambda: NOW)
        assert (await backend.execute(_execution())).success
        assert (await backend.verify(_execution())).success
        assert (await backend.reconcile(_execution())).success
        await MistyAIBodyStateProvider(client).snapshot()
        assert evidence_downloads == 1
        await http_client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("agency_state", "manual_takeover", "observation_available"),
    [
        ("TERMINAL", False, True),
        ("AVAILABLE", True, False),
    ],
)
def test_misty_health_blocks_new_actions_for_terminal_or_takeover(
    agency_state: str,
    manual_takeover: bool,
    observation_available: bool,
) -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            response = _misty_handler(request)
            if request.url.path != "/v1/health":
                return response
            payload = response.json()
            payload["agency_state"] = agency_state
            payload["manual_takeover_active"] = manual_takeover
            return httpx.Response(200, json=payload)

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        provider = MistyAICapabilityProvider(
            MistyAIClient("http://misty.test", token="token", client=http_client)
        )
        health = await provider.health()
        assert health.status.value == "degraded"
        assert health.capability_health[ACTION_EXECUTE].value == "unavailable"
        expected_observation = "healthy" if observation_available else "unavailable"
        assert health.capability_health[OBSERVATION_SNAPSHOT].value == expected_observation
        assert health.detail["agency_state"] == agency_state
        await http_client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "surface",
    ["observation", "execute", "verify", "reconcile", "body_state"],
)
def test_misty_provider_rejects_corrupt_evidence_on_every_trusted_surface(
    surface: str,
) -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/v1/evidence/"):
                return httpx.Response(
                    200,
                    content=b"corrupt evidence",
                    headers={"Content-Type": "image/png"},
                )
            return _misty_handler(request)

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        backend = MistyAIActionBackend(client, poll_interval_seconds=0.001, clock=lambda: NOW)
        if surface == "observation":
            with pytest.raises(MistyAIError, match="SHA-256") as captured:
                await MistyAIPerceptionBackend(client).observe(now=NOW)
            assert captured.value.code == "evidence_digest_mismatch"
        elif surface == "body_state":
            with pytest.raises(MistyAIError, match="SHA-256") as captured:
                await MistyAIBodyStateProvider(client).snapshot()
            assert captured.value.code == "evidence_digest_mismatch"
        else:
            result = await getattr(backend, surface)(_execution())
            assert result.success is False
            assert result.unknown is True
            assert "evidence_digest_mismatch" in result.summary
        await http_client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("response_headers", "maximum_bytes", "expected_code"),
    [
        ({"Content-Type": "application/json"}, 1024, "evidence_media_type_mismatch"),
        ({"Content-Type": "image/png"}, 4, "evidence_too_large"),
    ],
)
def test_misty_observation_rejects_wrong_media_type_and_oversized_evidence(
    response_headers: dict[str, str],
    maximum_bytes: int,
    expected_code: str,
) -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/v1/evidence/"):
                return httpx.Response(
                    200,
                    content=EVIDENCE_BYTES,
                    headers=response_headers,
                )
            return _misty_handler(request)

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient(
            "http://misty.test",
            token="token",
            max_evidence_bytes=maximum_bytes,
            client=http_client,
        )
        with pytest.raises(MistyAIError) as captured:
            await MistyAIPerceptionBackend(client).observe(now=NOW)
        assert captured.value.code == expected_code
        await http_client.aclose()

    asyncio.run(scenario())


def test_misty_evidence_download_timeout_is_reported_as_unverified() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/v1/evidence/"):
                raise httpx.ReadTimeout("timed out", request=request)
            return _misty_handler(request)

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        result = await MistyAIActionBackend(
            client,
            poll_interval_seconds=0.001,
            clock=lambda: NOW,
        ).verify(_execution())
        assert result.success is False
        assert result.unknown is True
        assert "evidence_download_failed" in result.summary
        await http_client.aclose()

    asyncio.run(scenario())


def test_misty_evidence_download_has_a_wall_clock_deadline() -> None:
    class SlowEvidenceStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.05)
            yield EVIDENCE_BYTES

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/v1/evidence/"):
                return httpx.Response(
                    200,
                    stream=SlowEvidenceStream(),
                    headers={"Content-Type": "image/png"},
                )
            return _misty_handler(request)

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient(
            "http://misty.test",
            token="token",
            evidence_timeout_seconds=0.01,
            client=http_client,
        )
        result = await MistyAIActionBackend(
            client,
            poll_interval_seconds=0.001,
            clock=lambda: NOW,
        ).verify(_execution())
        assert result.success is False
        assert result.unknown is True
        assert "evidence_download_failed" in result.summary
        await http_client.aclose()

    asyncio.run(scenario())


def test_misty_evidence_reference_fanout_is_bounded() -> None:
    async def scenario() -> None:
        downloads = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal downloads
            if request.url.path.startswith("/v1/evidence/"):
                downloads += 1
            response = _misty_handler(request)
            if request.url.path.endswith(":verify"):
                payload = response.json()
                payload["evidence_refs"] = [EVIDENCE] * 3
                return httpx.Response(200, json=payload)
            return response

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient(
            "http://misty.test",
            token="token",
            max_evidence_refs=2,
            client=http_client,
        )
        result = await MistyAIActionBackend(client).verify(_execution())
        assert result.success is False
        assert result.unknown is True
        assert "evidence_fanout_exceeded" in result.summary
        assert downloads == 0
        await http_client.aclose()

    asyncio.run(scenario())


def test_misty_missing_evidence_is_reported_as_unverified() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/v1/evidence/"):
                return httpx.Response(404, content=b"missing")
            return _misty_handler(request)

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        result = await MistyAIActionBackend(
            client,
            poll_interval_seconds=0.001,
            clock=lambda: NOW,
        ).verify(_execution())
        assert result.success is False
        assert result.unknown is True
        assert "evidence_download_failed" in result.summary
        await http_client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "code", "expected_unknown"),
    [
        (401, "authentication_failed", False),
        (422, "validation_failed", False),
        (409, "idempotency_conflict", False),
        (503, "agent_unavailable", True),
    ],
)
def test_misty_error_mapping_preserves_code_and_uncertainty(
    status: int,
    code: str,
    expected_unknown: bool,
) -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    404,
                    json={
                        "schema_version": "body-contract-v1",
                        "error": {"code": "action_not_found", "message": "missing", "retryable": False, "details": {}},
                    },
                )
            return httpx.Response(
                status,
                json={
                    "schema_version": "body-contract-v1",
                    "error": {"code": code, "message": "fixture", "retryable": status == 409, "details": {"x": 1}},
                },
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://misty.test")
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        result = await MistyAIActionBackend(client, clock=lambda: NOW).execute(_execution())
        assert result.unknown is expected_unknown
        assert code in result.summary or (status >= 500 and "could not be proven" in result.summary)
        if status == 409:
            assert result.retry_safe is True
            assert result.continuation is not None
            assert result.continuation["provider_error"]["code"] == code
        await http_client.aclose()

    asyncio.run(scenario())


def test_uncertain_post_timeout_is_queried_once_and_never_replayed() -> None:
    async def scenario() -> None:
        calls = {"post": 0, "get": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                calls["post"] += 1
                raise httpx.ReadTimeout("uncertain", request=request)
            calls["get"] += 1
            return httpx.Response(
                404,
                json={
                    "schema_version": "body-contract-v1",
                    "error": {"code": "action_not_found", "message": "missing", "retryable": False, "details": {}},
                },
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://misty.test")
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        result = await MistyAIActionBackend(client, clock=lambda: NOW).execute(_execution())
        assert result.unknown is True
        assert calls == {"post": 1, "get": 1}
        await http_client.aclose()

    asyncio.run(scenario())


def test_malformed_terminal_action_response_is_unknown_not_failed() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            response = _misty_handler(request)
            if request.method == "GET" and request.url.path.startswith("/v1/actions/"):
                payload = response.json()
                assert payload["result"] is not None
                del payload["result"]["evidence_refs"]
                return httpx.Response(200, json=payload)
            return response

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        result = await MistyAIActionBackend(
            client,
            poll_interval_seconds=0.001,
            clock=lambda: NOW,
        ).execute(_execution())
        assert result.success is False
        assert result.unknown is True
        assert "invalid_provider_response" in result.summary
        await http_client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda result: result.update(summary=""),
        lambda result: result.update(success=True, unknown=True),
    ],
)
def test_invalid_native_result_invariants_are_unknown(mutate: Any) -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            response = _misty_handler(request)
            if request.method == "GET" and request.url.path.startswith("/v1/actions/"):
                payload = response.json()
                assert payload["result"] is not None
                mutate(payload["result"])
                return httpx.Response(200, json=payload)
            return response

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        result = await MistyAIActionBackend(
            client,
            poll_interval_seconds=0.001,
            clock=lambda: NOW,
        ).execute(_execution())
        assert result.success is False
        assert result.unknown is True
        assert "invalid_provider_response" in result.summary
        await http_client.aclose()

    asyncio.run(scenario())


def test_naive_native_action_timestamp_is_unknown() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            response = _misty_handler(request)
            if request.method == "GET" and request.url.path.startswith("/v1/actions/"):
                payload = response.json()
                payload["updated_at"] = "2026-09-04T00:00:01"
                return httpx.Response(200, json=payload)
            return response

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        result = await MistyAIActionBackend(
            client,
            poll_interval_seconds=0.001,
            clock=lambda: NOW,
        ).execute(_execution())
        assert result.success is False
        assert result.unknown is True
        assert "invalid_provider_response" in result.summary
        await http_client.aclose()

    asyncio.run(scenario())


def test_mismatched_native_action_id_is_unknown() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            response = _misty_handler(request)
            if request.method == "GET" and request.url.path.startswith("/v1/actions/"):
                payload = response.json()
                payload["action_id"] = "different-action"
                return httpx.Response(200, json=payload)
            return response

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        result = await MistyAIActionBackend(
            client,
            poll_interval_seconds=0.001,
            clock=lambda: NOW,
        ).execute(_execution())
        assert result.success is False
        assert result.unknown is True
        assert "invalid_provider_response" in result.summary
        await http_client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("state", "native_result"),
    [
        ("unknown", _native_result(success=False, unknown=False)),
        ("execution_succeeded", _native_result(success=False, unknown=False)),
        ("failed", _native_result(success=True, unknown=False)),
    ],
)
def test_conflicting_terminal_action_state_is_unknown(
    state: str,
    native_result: dict[str, Any],
) -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            response = _misty_handler(request)
            if request.method == "GET" and request.url.path.startswith("/v1/actions/"):
                payload = response.json()
                payload["state"] = state
                payload["result"] = native_result
                return httpx.Response(200, json=payload)
            return response

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        result = await MistyAIActionBackend(
            client,
            poll_interval_seconds=0.001,
            clock=lambda: NOW,
        ).execute(_execution())
        assert result.success is False
        assert result.unknown is True
        assert "invalid_provider_response" in result.summary
        await http_client.aclose()

    asyncio.run(scenario())


def test_action_coordinator_never_fabricates_missing_provider_capabilities() -> None:
    async def scenario() -> None:
        simulator = DeterministicEmbodimentSimulator(
            SimulatorConfig(verify=False, safe_point=False, reconcile=False),
            clock=lambda: NOW,
        )
        supported = {
            item.capability
            for item in (await simulator.capabilities()).capabilities
        }
        coordinator = ActionCoordinator(
            simulator,
            capability_check=lambda capability: capability in supported,
        )
        proposal = GoalProposal(
            policy_id="fixture",
            goal="wait",
            success_criteria=["waited"],
            permission_class="low",
            reversible=True,
            verification_method="intentional_noop",
            embodied_action=_execution().embodied_action,
        )
        action = await coordinator.propose(
            proposal,
            idempotency_key="capability-test",
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            version_vector=VersionVector(),
        )
        reserved = await coordinator.reserve(action.action_id)
        preemption = await coordinator.request_preemption(reason="test")
        assert preemption is not None
        assert preemption.state == ActionExecutionState.RUNNING
        completed = await coordinator.execute(reserved.action_id)
        assert completed.state == ActionExecutionState.UNKNOWN
        assert "verification is unavailable" in (completed.error or "")
        reconciled = await coordinator.reconcile(completed.action_id)
        assert reconciled.state == ActionExecutionState.UNKNOWN

    asyncio.run(scenario())


def test_misty_fixture_typed_goal_runs_execute_verify_evidence_and_journal(tmp_path) -> None:
    class DeterministicCognitive:
        def __init__(self) -> None:
            self.revision = 0
            self.cursor = 0
            self.prepared: set[str] = set()

        async def health(self):
            return ServiceHealth(service="cognitive", revision=self.revision)

        async def get_snapshot(self):
            return DomainSnapshot(
                service="cognitive",
                revision=self.revision,
                acknowledged_receive_seq=self.cursor,
                component_revisions={"memory": self.revision, "session": self.revision},
            )

        async def prepare_response(self, command):
            prepared = PreparedResponse(
                event_id=command.event_id,
                correlation_id=command.correlation_id,
                session_id="fixture",
                decision=GoalProposal(
                    policy_id="fixture-open",
                    goal="Open fixture application",
                    success_criteria=["fixture window is focused"],
                    permission_class="low",
                    reversible=True,
                    verification_method="window_state",
                    embodied_action=EmbodiedActionSpec(
                        intent=ActionIntent(
                            intent_id="app.open.v1",
                            arguments={"app_id": "org.mistyai.Fixture"},
                        ),
                        success_criteria=[IntentionalNoopCriterion(condition="observed")],
                    ),
                ),
                read_versions=command.version_vector or VersionVector(),
                prepared_at=datetime.now(UTC),
                expires_at=command.deadline,
                model_version="deterministic",
                prompt_version="fixture",
            )
            self.prepared.add(prepared.prepare_id)
            self.cursor = command.receive_seq
            return DomainCommandResult(
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=command.effective_request_hash(),
                committed_revision=self.revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=command.receive_seq,
                result=prepared,
                completed_at=datetime.now(UTC),
            )

        async def commit_interaction(self, command):
            assert command.payload["prepare_id"] in self.prepared
            self.revision += 1
            self.cursor = command.receive_seq
            return DomainCommandResult(
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=command.effective_request_hash(),
                committed_revision=self.revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=command.receive_seq,
                result={"revision": self.revision},
                completed_at=datetime.now(UTC),
            )

        async def pause_maintenance(self, *, timeout):
            return True

        async def resume_maintenance(self):
            return None

        async def ui_snapshot(self):
            return {}

        async def control(self, operation, payload=None):
            return {}

        async def close(self):
            return None

    async def scenario() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_misty_handler),
            base_url="http://misty.test",
        )
        client = MistyAIClient("http://misty.test", token="token", client=http_client)
        condition = EmbodimentCondition(
            mode="active",
            body_instance_id="body-1",
            capability_requirements=[
                CapabilityRequirement(capability=name, required=True)
                for name in (
                    ACTION_EXECUTE,
                    ACTION_VERIFY,
                    ACTION_SAFE_POINT,
                    ACTION_RECONCILE,
                    EVIDENCE_REFERENCE,
                )
            ],
        )
        manager = EmbodimentManager(condition, MistyAICapabilityProvider(client))
        coordinator = ActionCoordinator(
            MistyAIActionBackend(client, poll_interval_seconds=0.001),
            capability_check=manager.supports,
        )
        supervisor = RuntimeSupervisor(
            manifest=_manifest().model_copy(update={"embodiment": condition}),
            cognitive=DeterministicCognitive(),
            homeostasis=PassiveHomeostasisAdapter(),
            state_dir=tmp_path,
            embodiment_manager=manager,
            action_coordinator=coordinator,
        )
        await supervisor.boot_and_recover()
        await supervisor.submit_user_message("open the fixture")
        event = await supervisor.queue.get(timeout=0)
        assert event is not None
        result = await supervisor.dispatch(event)
        assert result.decision_kind == "goal"
        action_result = None
        for _ in range(100):
            action_result = await supervisor.queue.get(timeout=0.01)
            if action_result is not None:
                break
        assert action_result is not None
        assert action_result.event_type == RuntimeEventType.ACTION_RESULT
        await supervisor.dispatch(action_result)
        records = supervisor.journal.records()
        completed = [record for record in records if record.record_type == "action_completed"]
        assert completed
        action_payload = completed[-1].data["action"]
        assert action_payload["state"] == "SUCCEEDED"
        assert action_payload["outcome"]["evidence_refs"][0]["provider_id"] == "mistyai"
        assert any(record.record_type == "action_interaction_committed" for record in records)
        await supervisor.stop(reason="test complete")
        await http_client.aclose()

    asyncio.run(scenario())
