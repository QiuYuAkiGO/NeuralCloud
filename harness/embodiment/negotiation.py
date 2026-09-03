"""Deterministic major-version capability negotiation."""

from __future__ import annotations

from collections.abc import Iterable

from harness.embodiment.capabilities import (
    RUNTIME_CAPABILITIES,
    CapabilityRequirement,
    CapabilityStatus,
    NegotiatedCapabilitySet,
    ProviderCapabilities,
    parse_capability,
)


class CapabilityNegotiator:
    def __init__(self, understood: Iterable[str] = RUNTIME_CAPABILITIES) -> None:
        self.understood = frozenset(understood)
        for capability in self.understood:
            parse_capability(capability)

    def resolve(
        self,
        requirements: Iterable[CapabilityRequirement],
        provided: ProviderCapabilities,
    ) -> NegotiatedCapabilitySet:
        requested = list(requirements)
        names = [item.capability for item in requested]
        if len(names) != len(set(names)):
            raise ValueError("capability requirements must be unique")

        by_name = {item.capability: item for item in provided.capabilities}
        by_family: dict[str, set[int]] = {}
        for item in provided.capabilities:
            family, major = parse_capability(item.capability)
            by_family.setdefault(family, set()).add(major)

        enabled: set[str] = set()
        degraded: set[str] = set()
        unavailable_optional: set[str] = set()
        missing_required: set[str] = set()
        incompatible_required: set[str] = set()

        for requirement in requested:
            capability = requirement.capability
            descriptor = by_name.get(capability)
            if capability not in self.understood:
                if requirement.required:
                    incompatible_required.add(capability)
                else:
                    unavailable_optional.add(capability)
                continue
            if descriptor is None:
                family, major = parse_capability(capability)
                other_majors = by_family.get(family, set()) - {major}
                if requirement.required and other_majors:
                    incompatible_required.add(capability)
                elif requirement.required:
                    missing_required.add(capability)
                else:
                    unavailable_optional.add(capability)
                continue
            if descriptor.status == CapabilityStatus.AVAILABLE:
                enabled.add(capability)
            elif descriptor.status == CapabilityStatus.DEGRADED:
                degraded.add(capability)
            elif requirement.required:
                missing_required.add(capability)
            else:
                unavailable_optional.add(capability)

        return NegotiatedCapabilitySet(
            provider_id=provided.provider_id,
            provider_revision=provided.provider_revision,
            enabled=enabled,
            degraded=degraded,
            unavailable_optional=unavailable_optional,
            missing_required=missing_required,
            incompatible_required=incompatible_required,
            unknown_provided=set(by_name) - self.understood,
        )
