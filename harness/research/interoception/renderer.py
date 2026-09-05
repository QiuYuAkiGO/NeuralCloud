"""Render the observation projection with a conservative, strict token budget."""

from __future__ import annotations

from collections.abc import Sequence

from harness.research.grounded_agency.neutrality import NeutralityScanner
from harness.research.interoception.contracts import OpaqueInteroceptiveObservation


def _text(observations: Sequence[OpaqueInteroceptiveObservation]) -> str:
    if len(observations) == 1:
        observation = observations[0]
        return (
            "Internal channel sample:\n"
            f"channel={observation.channel_id}\nreading={observation.reading}\n"
            f"change={observation.change}\nconfidence={observation.confidence_band}\n"
            f"sample={observation.sample_sequence}"
        )
    return "Internal channel samples:\n" + "\n".join(
        f"{item.channel_id}: {item.reading}/{item.change}/{item.confidence_band} @{item.sample_sequence}"
        for item in observations
    )


def bounded_observations(
    observations: Sequence[OpaqueInteroceptiveObservation], *, max_tokens: int = 256,
) -> tuple[OpaqueInteroceptiveObservation, ...]:
    """Keep the latest complete samples within a UTF-8-byte token upper bound.

    One UTF-8 byte per token is deliberately conservative for byte-level model
    tokenizers. No transform reference, timestamp, evidence, or truth is rendered.
    """
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("max_tokens must be a positive integer")
    if any(not isinstance(item, OpaqueInteroceptiveObservation) for item in observations):
        raise TypeError("renderer accepts only opaque observations, never envelopes")
    ordered = tuple(sorted(
        (OpaqueInteroceptiveObservation.model_validate(item.model_dump(mode="python")) for item in observations),
        key=lambda item: item.sample_sequence,
    ))
    if len({item.sample_sequence for item in ordered}) != len(ordered):
        raise ValueError("sample sequences must be unique")
    while ordered and len(_text(ordered).encode("utf-8")) > max_tokens:
        ordered = ordered[1:]
    if observations and not ordered:
        raise ValueError("token budget cannot fit one complete opaque sample")
    return ordered


def render_opaque_interoception(
    observations: Sequence[OpaqueInteroceptiveObservation], *, max_tokens: int = 256,
) -> str:
    selected = bounded_observations(observations, max_tokens=max_tokens)
    if not selected:
        return ""
    rendered = _text(selected)
    NeutralityScanner().assert_neutral([{"role": "system", "content": rendered}])
    return rendered
