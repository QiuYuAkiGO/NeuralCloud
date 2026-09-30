"""Real MistyAI HTTP application backed by a deterministic desktop agent.

This module is launched in a separate process by the cross-repository CI job.
It deliberately uses MistyAI's real API, persistence, evidence store,
controller, grounder, verifier, and physiology implementation.  Only the
machine-specific desktop agent is replaced with a deterministic fixture.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from mistyai.api.app import create_app
from mistyai.contracts.agent import (
    AgentHealth,
    AgentObservationPayload,
    AgentPrimitiveRequest,
    AgentPrimitiveResult,
    AgentTakeoverRequest,
    AgentTakeoverResult,
    WaitPrimitive,
)
from mistyai.contracts.common import HealthStatus, utc_now
from mistyai.contracts.observation import DisplayInfo, PointerInfo
from mistyai.settings import AppSettings, AuthorizedRoot, MistyManifest, PhysiologySpec

MINIMAL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class DeterministicDesktopAgent:
    def __init__(self) -> None:
        self.capture_count = 0
        self.takeover_active = False
        self.results: dict[str, AgentPrimitiveResult] = {}

    async def health(self) -> AgentHealth:
        return AgentHealth(
            body_instance_id="misty-body-01",
            environment_revision=1,
            status=HealthStatus.HEALTHY,
            backends={
                "screen": HealthStatus.HEALTHY,
                "window": HealthStatus.HEALTHY,
                "input": HealthStatus.HEALTHY,
                "a11y": HealthStatus.HEALTHY,
                "process": HealthStatus.HEALTHY,
            },
            takeover_active=self.takeover_active,
            checked_at=utc_now(),
        )

    async def capture(self) -> AgentObservationPayload:
        self.capture_count += 1
        frame = MINIMAL_PNG + self.capture_count.to_bytes(4, "big")
        return AgentObservationPayload(
            body_instance_id="misty-body-01",
            environment_revision=1,
            captured_at=utc_now(),
            frame_png_base64=base64.b64encode(frame).decode("ascii"),
            display=DisplayInfo(name=":99", width=1280, height=720, scale=1.0),
            pointer=PointerInfo(x=0, y=0),
            windows=[],
            running_apps=[],
            backend_health={
                "screen": HealthStatus.HEALTHY,
                "window": HealthStatus.HEALTHY,
                "input": HealthStatus.HEALTHY,
                "a11y": HealthStatus.HEALTHY,
                "process": HealthStatus.HEALTHY,
            },
        )

    async def execute(self, request: AgentPrimitiveRequest) -> AgentPrimitiveResult:
        existing = self.results.get(request.step_id)
        if existing is not None:
            return existing
        if not isinstance(request.primitive, WaitPrimitive):
            raise RuntimeError("cross-process fixture only permits control.wait.v1")
        started_at = utc_now()
        result = AgentPrimitiveResult(
            action_id=request.action_id,
            step_id=request.step_id,
            success=True,
            started_at=started_at,
            finished_at=utc_now(),
            data={"waited_ms": request.primitive.duration_ms},
        )
        self.results[request.step_id] = result
        return result

    async def begin_takeover(self, _request: AgentTakeoverRequest) -> AgentTakeoverResult:
        self.takeover_active = True
        return AgentTakeoverResult(takeover_active=True, changed_at=utc_now())

    async def end_takeover(self, _request: AgentTakeoverRequest) -> AgentTakeoverResult:
        self.takeover_active = False
        return AgentTakeoverResult(takeover_active=False, changed_at=utc_now())

    async def close(self) -> None:
        return None


state_root = Path(os.environ["MISTYAI_FIXTURE_STATE_DIR"]).resolve()
workspace = state_root / "workspace"
workspace.mkdir(parents=True, exist_ok=True)

settings = AppSettings(
    manifest_path=state_root / "unused.yaml",
    database_url=f"sqlite+aiosqlite:///{(state_root / 'mistyai.db').as_posix()}",
    evidence_path=state_root / "evidence",
    service_token=os.environ["MISTYAI_SERVICE_TOKEN"],
    **({"accounting_token": os.environ["MISTYAI_ACCOUNTING_TOKEN"]}
       if os.environ.get("MISTYAI_FIXTURE_PRODUCTION") == "1" else {}),
    agent_url="http://fixture.invalid",
    agent_token="cross-process-agent-token",
    operator_socket=state_root / "operator.sock",
)
manifest = MistyManifest(
    body_instance_id="misty-body-01",
    environment_revision=1,
    authorized_roots=(
        AuthorizedRoot(
            id="workspace",
            container_path=workspace,
            permissions=frozenset({"read", "write", "create", "move", "delete"}),
        ),
    ),
    applications=(),
    commands=(),
    physiology=PhysiologySpec(enabled=True, **({"mode": "production_v1"}
        if os.environ.get("MISTYAI_FIXTURE_PRODUCTION") == "1" else {})),
)

app = create_app(settings=settings, manifest=manifest, agent=DeterministicDesktopAgent())
