import asyncio
import hashlib
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from harness.embodiment.contracts import BodyRestCommand, CognitiveUsage
from harness.embodiment.providers.mistyai import MistyAIClient, MistyAIBodyStateProvider, MistyAIError
from harness.embodiment.providers.mistyai_dto import NativeBodyStateCaptureResponse


NOW = datetime(2026, 9, 22, tzinfo=UTC).isoformat()
PROOF = b'{"fixture":"verified-body"}'
DIGEST = hashlib.sha256(PROOF).hexdigest()
REF = "evidence://sha256/" + DIGEST


def body():
    return dict(
        schema_version="body-state-v1",
        body_instance_id="body-1",
        episode_id="episode-1",
        revision=3,
        sample_sequence=7,
        ledger_revision=3,
        captured_at=NOW,
        resting=True,
        exhausted=False,
        physiology_mode="production_v1",
        energy=dict(
            capacity_units=100,
            available_units=65,
            available_ratio=0.65,
            delta_units_per_minute=1.95,
            charging=True,
            estimated_time_to_terminal_seconds=None,
            terminal=False,
        ),
        thermal={"normalized_pressure": 0.2, "throttling": False},
        compute={"cpu_pressure": 0.1, "gpu_pressure": 0.2, "memory_pressure": 0.3},
        integrity={"overall": 0.9},
        evidence_refs=[REF],
    )


def capture(state):
    return dict(
        schema_version="body-contract-v1",
        request_id="request-1",
        body_state=state,
        **{
            key: state[key]
            for key in (
                "body_instance_id",
                "episode_id",
                "revision",
                "sample_sequence",
                "captured_at",
                "energy",
                "evidence_refs",
            )
        },
    )


def test_actual_body_contract_samples_and_full_fields_survive_provider_mapping():
    async def run():
        async def handler(request):
            if request.url.path.startswith("/v1/evidence/"):
                return httpx.Response(200, content=PROOF, headers={"Content-Type": "application/json"})
            return httpx.Response(200, json=capture(body()))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://body")
        client = MistyAIClient(
            base_url="http://body", token="fixture", accounting_token="fixture-accounting", client=http
        )
        try:
            state = await MistyAIBodyStateProvider(client).snapshot()
            assert (state.episode_id, state.sample_sequence, state.ledger_revision) == ("episode-1", 7, 3)
            assert state.resting and not state.exhausted and state.physiology_mode == "production_v1"
            assert state.thermal["normalized_pressure"] == 0.2
            assert state.compute["gpu_pressure"] == 0.2 and state.integrity["overall"] == 0.9
        finally:
            await http.aclose()

    asyncio.run(run())


def test_new_sample_projection_must_match_and_legacy_remains_readable():
    state = body()
    response = capture(state)
    with pytest.raises(ValidationError):
        NativeBodyStateCaptureResponse.model_validate({**response, "sample_sequence": 99})
    for key in ("sample_sequence", "ledger_revision", "resting", "exhausted", "physiology_mode"):
        state.pop(key)
    response["body_state"] = state
    response.pop("sample_sequence")
    assert NativeBodyStateCaptureResponse.model_validate(response).body_state.sample_sequence is None


def test_rest_and_usage_validate_identity_and_verify_evidence_before_returning():
    async def run():
        corrupt_identity = False

        async def handler(request):
            if request.url.path.startswith("/v1/evidence/"):
                return httpx.Response(200, content=PROOF, headers={"Content-Type": "application/json"})
            if request.url.path == "/v1/cognition/usage":
                return httpx.Response(
                    200,
                    json=dict(
                        schema_version="cognitive-usage-receipt-v1",
                        inference_id="wrong" if corrupt_identity else "attempt-1",
                        cost_id="cost-1",
                        units=0.001,
                        ledger_revision=3,
                        evidence_refs=[REF, REF],
                        body_instance_id="body-1",
                        episode_id="episode-1",
                        source="cognition",
                        metering="call",
                    ),
                )
            return httpx.Response(
                200,
                json=dict(
                    schema_version="body-rest-receipt-v1",
                    command_id="rest-1",
                    body_instance_id="body-1",
                    episode_id="episode-1",
                    resting=True,
                    ledger_revision=3,
                    evidence_refs=[REF],
                    body_state=body(),
                ),
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://body")
        client = MistyAIClient(
            base_url="http://body", token="fixture", accounting_token="fixture-accounting", client=http
        )
        provider = MistyAIBodyStateProvider(client)
        try:
            receipt = await provider.start_rest(
                BodyRestCommand(command_id="rest-1", body_instance_id="body-1", episode_id="episode-1")
            )
            assert receipt.resting and receipt.body_state.sample_sequence == 7
            request = CognitiveUsage(
                inference_id="attempt-1",
                body_instance_id="body-1",
                episode_id="episode-1",
                model_class="main",
                cost_class="other",
                quantity=1,
            )
            assert (await provider.record_usage(request)).units == 0.001
            corrupt_identity = True
            with pytest.raises(MistyAIError, match="identity"):
                await provider.record_usage(request)
        finally:
            await http.aclose()

    asyncio.run(run())
