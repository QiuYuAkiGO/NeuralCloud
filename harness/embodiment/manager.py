"""Runtime-facing embodiment activation and health state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from harness.embodiment.capabilities import (
    ACTION_EXECUTE,
    EmbodimentState,
    EmbodimentStatus,
    HealthStatus,
    NegotiatedCapabilitySet,
    ProviderCapabilities,
    ProviderHealth,
)
from harness.embodiment.negotiation import CapabilityNegotiator

if TYPE_CHECKING:
    from harness.embodiment.ports import CapabilityProvider
    from harness.research.manifest import EmbodimentCondition


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EmbodimentManager:
    """Freeze activation permissions while allowing health to recover dynamically."""

    def __init__(
        self,
        condition: EmbodimentCondition,
        provider: CapabilityProvider | None = None,
        *,
        negotiator: CapabilityNegotiator | None = None,
    ) -> None:
        self.condition = condition
        self.provider = provider
        self.negotiator = negotiator or CapabilityNegotiator()
        self._provided: ProviderCapabilities | None = None
        self._negotiated: NegotiatedCapabilitySet | None = None
        self._health: ProviderHealth | None = None
        self._activated: set[str] = set()
        self._terminal = False
        self._reason: str | None = None

    async def activate(self) -> EmbodimentStatus:
        if self.condition.mode == "off":
            self._reason = "embodiment disabled by manifest"
            return self.status()
        if self.provider is None:
            self._reason = "no embodiment provider configured"
            return self.status()
        try:
            provided = await self.provider.capabilities()
            health = await self.provider.health()
            self._validate_identity(provided, health)
            negotiated = self.negotiator.resolve(
                self.condition.capability_requirements,
                provided,
            )
        except Exception as exc:
            self._reason = f"embodiment activation failed: {exc}"
            return self.status()
        self._provided = provided
        self._health = health
        self._negotiated = negotiated
        self._activated = set(negotiated.enabled | negotiated.degraded)
        self._reason = None
        return self.status()

    async def refresh(self) -> EmbodimentStatus:
        if self.condition.mode == "off" or self.provider is None:
            return self.status()
        try:
            provided = await self.provider.capabilities()
            health = await self.provider.health()
            self._validate_identity(provided, health)
            current = self.negotiator.resolve(self.condition.capability_requirements, provided)
            # A reconnect may restore an activated feature but cannot add one that
            # was absent at activation time.
            self._negotiated = current.model_copy(
                update={
                    "enabled": current.enabled & self._activated,
                    "degraded": current.degraded & self._activated,
                }
            )
            self._provided = provided
            self._health = health
            self._reason = None
        except Exception as exc:
            provider_id = self._provided.provider_id if self._provided else "unavailable"
            body_instance_id = self._provided.body_instance_id if self._provided else None
            self._health = ProviderHealth(
                provider_id=provider_id,
                body_instance_id=body_instance_id,
                status=HealthStatus.UNAVAILABLE,
                observed_at=_utc_now(),
                detail={"error": str(exc)},
            )
            self._reason = f"embodiment refresh failed: {exc}"
        return self.status()

    def supports(self, capability: str) -> bool:
        return capability in self._effective_capabilities()

    def set_terminal(self, reason: str = "body provider entered terminal state") -> None:
        self._terminal = True
        self._reason = reason

    def status(self) -> EmbodimentStatus:
        negotiated = self._negotiated
        health = self._health
        effective = self._effective_capabilities()
        if self.condition.mode == "off":
            state = EmbodimentState.DISABLED
        elif self._terminal:
            state = EmbodimentState.TERMINAL
        elif negotiated is None or not negotiated.required_satisfied:
            state = EmbodimentState.UNAVAILABLE
        elif health is None or health.status == HealthStatus.UNAVAILABLE:
            state = EmbodimentState.UNAVAILABLE
        else:
            required = {
                item.capability
                for item in self.condition.capability_requirements
                if item.required
            }
            required_unhealthy = required - effective
            if required_unhealthy:
                state = EmbodimentState.UNAVAILABLE
            elif (
                health.status == HealthStatus.DEGRADED
                or negotiated.degraded
                or set(health.capability_health.values()) & {HealthStatus.DEGRADED, HealthStatus.UNAVAILABLE}
            ):
                state = EmbodimentState.DEGRADED
            else:
                state = EmbodimentState.AVAILABLE
        return EmbodimentStatus(
            state=state,
            provider_id=self._provided.provider_id if self._provided else None,
            body_instance_id=self._provided.body_instance_id if self._provided else None,
            activated_capabilities=set(self._activated),
            effective_capabilities=effective,
            negotiated=negotiated,
            health=health,
            reason=self._reason,
        )

    def _effective_capabilities(self) -> set[str]:
        if self._negotiated is None or self._health is None:
            return set()
        if self._health.status == HealthStatus.UNAVAILABLE:
            return set()
        candidates = set(self._negotiated.enabled | self._negotiated.degraded)
        if self._terminal:
            candidates.discard(ACTION_EXECUTE)
        return {
            capability
            for capability in candidates
            if self._health.capability_health.get(capability, self._health.status)
            != HealthStatus.UNAVAILABLE
        }

    def _validate_identity(
        self,
        capabilities: ProviderCapabilities,
        health: ProviderHealth,
    ) -> None:
        if health.provider_id != capabilities.provider_id:
            raise ValueError("provider capability/health identity mismatch")
        if health.body_instance_id != capabilities.body_instance_id:
            raise ValueError("provider capability/health body instance mismatch")
        expected = self.condition.body_instance_id
        if expected is not None and capabilities.body_instance_id != expected:
            raise ValueError(
                f"expected body instance {expected}, received {capabilities.body_instance_id}"
            )
