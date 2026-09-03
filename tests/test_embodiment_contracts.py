from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.embodiment.capabilities import (
    ACTION_EXECUTE,
    ACTION_SAFE_POINT,
    OBSERVATION_SNAPSHOT,
    CapabilityDescriptor,
    CapabilityRequirement,
    CapabilityStatus,
    ProviderCapabilities,
)
from harness.embodiment.negotiation import CapabilityNegotiator
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.providers.simulator import (
    DeterministicEmbodimentSimulator,
    SimulatorConfig,
)
from harness.research.manifest import EmbodimentCondition
from harness.embodiment.schema import schema_catalog
from harness.research.manifest import ExperimentManifest

NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def _provided(*descriptors: CapabilityDescriptor) -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_id="fixture",
        provider_kind="test",
        body_instance_id="body-1",
        capabilities=list(descriptors),
        observed_at=NOW,
    )


def _capability(name: str, status: CapabilityStatus = CapabilityStatus.AVAILABLE) -> CapabilityDescriptor:
    return CapabilityDescriptor(capability=name, status=status)


def test_capability_identifiers_and_duplicates_are_strict() -> None:
    with pytest.raises(ValidationError):
        _capability("execute")
    with pytest.raises(ValidationError):
        _provided(_capability(ACTION_EXECUTE), _capability(ACTION_EXECUTE))


def test_negotiation_exact_major_allowlist_and_unknown_diagnostics() -> None:
    provider = _provided(
        _capability(ACTION_EXECUTE),
        _capability(OBSERVATION_SNAPSHOT, CapabilityStatus.DEGRADED),
        _capability("action.safe-point.v2"),
        _capability("vendor.experimental.v1"),
    )
    result = CapabilityNegotiator().resolve(
        [
            CapabilityRequirement(capability=ACTION_EXECUTE, required=True),
            CapabilityRequirement(capability=OBSERVATION_SNAPSHOT),
            CapabilityRequirement(capability=ACTION_SAFE_POINT, required=True),
        ],
        provider,
    )
    assert result.enabled == {ACTION_EXECUTE}
    assert result.degraded == {OBSERVATION_SNAPSHOT}
    assert result.incompatible_required == {ACTION_SAFE_POINT}
    assert result.unknown_provided == {"action.safe-point.v2", "vendor.experimental.v1"}
    assert "vendor.experimental.v1" not in result.enabled


@pytest.mark.parametrize(
    ("provided", "requirements", "enabled", "missing"),
    [
        ([ACTION_EXECUTE, OBSERVATION_SNAPSHOT], [(ACTION_EXECUTE, True)], {ACTION_EXECUTE}, set()),
        ([OBSERVATION_SNAPSHOT], [(ACTION_EXECUTE, False)], set(), set()),
        ([OBSERVATION_SNAPSHOT], [(ACTION_EXECUTE, True)], set(), {ACTION_EXECUTE}),
        ([ACTION_EXECUTE], [(OBSERVATION_SNAPSHOT, False)], set(), set()),
        ([], [], set(), set()),
    ],
)
def test_documented_negotiation_matrix(
    provided: list[str],
    requirements: list[tuple[str, bool]],
    enabled: set[str],
    missing: set[str],
) -> None:
    result = CapabilityNegotiator().resolve(
        [CapabilityRequirement(capability=name, required=required) for name, required in requirements],
        _provided(*[_capability(name) for name in provided]),
    )
    assert result.enabled == enabled
    assert result.missing_required == missing


def test_manifest_without_embodiment_is_chat_only() -> None:
    payload = json.loads((ROOT / "presets" / "runtime" / "development-demo.json").read_text("utf-8"))
    payload.pop("embodiment", None)
    assert ExperimentManifest.model_validate(payload).embodiment.mode == "off"


def test_committed_json_schemas_are_current() -> None:
    generated = schema_catalog()
    directory = ROOT / "contracts" / "embodiment"
    assert {path.stem.removesuffix(".schema") for path in directory.glob("*.json")} == set(generated)
    for name, schema in generated.items():
        assert json.loads((directory / f"{name}.schema.json").read_text("utf-8")) == schema


def test_core_packages_do_not_import_provider_transport() -> None:
    forbidden = ("harness.embodiment.providers", "mistyai", "httpx")
    roots = [ROOT / "harness" / name for name in ("runtime", "cognitive", "homeostasis")]
    for package in roots:
        for path in package.rglob("*.py"):
            if path == ROOT / "harness" / "runtime" / "service.py":
                continue  # composition root
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            imports = [
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            ] + [
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            ]
            scoped_forbidden = forbidden[:2] if path.name == "rpc.py" else forbidden
            assert not any(name.startswith(scoped_forbidden) for name in imports), path


def test_activation_set_is_frozen_until_next_boot() -> None:
    async def scenario() -> None:
        simulator = DeterministicEmbodimentSimulator(
            SimulatorConfig(),
            clock=lambda: NOW,
        )
        simulator.set_capability_status(
            OBSERVATION_SNAPSHOT,
            CapabilityStatus.UNAVAILABLE,
        )
        condition = EmbodimentCondition(
            mode="observe",
            capability_requirements=[
                CapabilityRequirement(capability=OBSERVATION_SNAPSHOT, required=False)
            ],
        )
        manager = EmbodimentManager(condition, simulator)
        activated = await manager.activate()
        assert OBSERVATION_SNAPSHOT not in activated.activated_capabilities
        simulator.set_capability_status(OBSERVATION_SNAPSHOT, CapabilityStatus.AVAILABLE)
        refreshed = await manager.refresh()
        assert OBSERVATION_SNAPSHOT not in refreshed.effective_capabilities

    import asyncio

    asyncio.run(scenario())
