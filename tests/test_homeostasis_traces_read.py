from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from harness.homeostasis import HomeostasisEngine, load_profile
from harness.runtime.rpc import create_domain_service_app
from tests.test_homeostasis_engine import _heartbeat_command

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def test_engine_read_traces_filters_by_time(tmp_path) -> None:
    async def scenario() -> None:
        engine = HomeostasisEngine(load_profile("presets/homeostasis/functional-proxy-v1.json"),
                                   state_dir=tmp_path, now=NOW)
        result = await engine.apply_heartbeat(_heartbeat_command(sequence=1, expected_revision=0, at=NOW))
        assert result.status == "applied"
        traces = await engine.read_traces(
            since=(NOW - timedelta(minutes=1)).isoformat(),
            until=(NOW + timedelta(hours=1)).isoformat(),
        )
        assert traces and traces[0]["command_type"] == "ApplyHeartbeat"
        assert traces[0]["salience"] in {"nominal", "noticeable", "marked", "critical"}
        empty = await engine.read_traces(since="2020-01-01T00:00:00+00:00", until="2020-01-02T00:00:00+00:00")
        assert empty == []
        await engine.close()

    asyncio.run(scenario())


def test_traces_http_endpoint_is_authenticated(tmp_path) -> None:
    async def setup():
        engine = HomeostasisEngine(load_profile("presets/homeostasis/functional-proxy-v1.json"),
                                   state_dir=tmp_path, now=NOW)
        await engine.apply_heartbeat(_heartbeat_command(sequence=1, expected_revision=0, at=NOW))
        return engine

    engine = asyncio.run(setup())
    app = create_domain_service_app(engine, service_token="secret", service_name="homeostasis")
    with TestClient(app) as client:
        assert client.post("/v1/traces:read", json={}).status_code == 401
        payload = client.post(
            "/v1/traces:read",
            headers={"Authorization": "Bearer secret"},
            json={"since": (NOW - timedelta(minutes=1)).isoformat(), "until": (NOW + timedelta(hours=1)).isoformat()},
        )
        assert payload.status_code == 200
        assert payload.json()["traces"]
    asyncio.run(engine.close())
