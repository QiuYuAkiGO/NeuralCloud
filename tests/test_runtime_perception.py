from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from harness.runtime.models import RuntimeEventType
from harness.runtime.perception import PerceptionGuard

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _observation(
    sequence: int,
    *,
    digest: str | None,
    freshness_seconds: float | None = None,
) -> dict[str, Any]:
    captured_at = NOW + timedelta(milliseconds=sequence * 200)
    return {
        "schema_version": "observation.v1",
        "provider_id": "mistyai",
        "body_instance_id": "body-1",
        "revision": 7,
        "captured_at": captured_at.isoformat(),
        "summary": "desktop observation revision 7",
        "confidence": 1.0,
        "freshness_seconds": (
            sequence * 0.01 if freshness_seconds is None else freshness_seconds
        ),
        "evidence_refs": [
            {
                "provider_id": "mistyai",
                "ref": f"evidence://frame/{sequence}",
                "digest": digest,
                "media_type": "image/png",
                "created_at": captured_at.isoformat(),
            }
        ],
        "extensions": {
            "mistyai.desktop.v1": {
                "observation_id": f"obs-{sequence}",
                "captured_at": captured_at.isoformat(),
                "environment_revision": 7,
                "active_window": {"window_id": "main", "title": "NeuralCloud"},
                "windows": [{"window_id": "main", "title": "NeuralCloud"}],
                "a11y_ref": "a11y://stable",
                "fs_events": [],
                "backend_health": {"screen": "healthy"},
            }
        },
    }


class _SequenceBackend:
    def __init__(self, payloads: list[dict[str, Any]], complete: asyncio.Event) -> None:
        self.payloads = payloads
        self.complete = complete
        self.calls = 0

    async def observe(self, *, now: datetime | None = None) -> dict[str, Any]:
        del now
        payload = self.payloads[self.calls]
        self.calls += 1
        if self.calls == len(self.payloads):
            self.complete.set()
        return payload


def test_perception_guard_suppresses_timestamp_and_identifier_only_changes() -> None:
    async def scenario() -> None:
        complete = asyncio.Event()
        submitted: list[dict[str, Any]] = []

        async def submit(**kwargs: Any):
            submitted.append(kwargs)
            return None

        backend = _SequenceBackend(
            [_observation(sequence, digest="same") for sequence in range(1, 4)],
            complete,
        )
        guard = PerceptionGuard(backend, submit, interval_seconds=0.001)
        guard.start()
        await asyncio.wait_for(complete.wait(), timeout=1.0)
        await guard.stop()

        assert backend.calls == 3
        assert len(submitted) == 1
        assert guard.submitted_observations == 1
        assert guard.suppressed_observations == 2

    asyncio.run(scenario())


def test_perception_guard_emits_when_screen_digest_changes() -> None:
    async def scenario() -> None:
        complete = asyncio.Event()
        submitted: list[dict[str, Any]] = []

        async def submit(**kwargs: Any):
            submitted.append(kwargs)
            return None

        backend = _SequenceBackend(
            [
                _observation(1, digest="first"),
                _observation(2, digest="first"),
                _observation(3, digest="second"),
            ],
            complete,
        )
        guard = PerceptionGuard(backend, submit, interval_seconds=0.001)
        guard.start()
        await asyncio.wait_for(complete.wait(), timeout=1.0)
        await guard.stop()

        assert len(submitted) == 2
        assert guard.submitted_observations == 2
        assert guard.suppressed_observations == 1

    asyncio.run(scenario())


def test_perception_guard_keeps_reference_when_digest_is_missing() -> None:
    async def scenario() -> None:
        complete = asyncio.Event()
        submitted: list[dict[str, Any]] = []

        async def submit(**kwargs: Any):
            submitted.append(kwargs)
            return None

        backend = _SequenceBackend(
            [_observation(1, digest=None), _observation(2, digest=None)],
            complete,
        )
        guard = PerceptionGuard(backend, submit, interval_seconds=0.001)
        guard.start()
        await asyncio.wait_for(complete.wait(), timeout=1.0)
        await guard.stop()

        assert [item["event_type"] for item in submitted] == [
            RuntimeEventType.PERCEPTION,
            RuntimeEventType.PERCEPTION,
        ]

    asyncio.run(scenario())


def test_perception_guard_suppresses_stale_frames_and_reports_transitions() -> None:
    async def scenario() -> None:
        complete = asyncio.Event()
        submitted: list[dict[str, Any]] = []

        async def submit(**kwargs: Any):
            submitted.append(kwargs)
            return None

        backend = _SequenceBackend(
            [
                _observation(1, digest="same", freshness_seconds=1.0),
                _observation(2, digest="same", freshness_seconds=11.0),
                _observation(3, digest="same", freshness_seconds=12.0),
                _observation(4, digest="same", freshness_seconds=1.0),
            ],
            complete,
        )
        guard = PerceptionGuard(
            backend,
            submit,
            interval_seconds=0.001,
            max_state_age_seconds=10.0,
        )
        guard.start()
        await asyncio.wait_for(complete.wait(), timeout=1.0)
        await guard.stop()

        assert [item["event_type"] for item in submitted] == [
            RuntimeEventType.PERCEPTION,
            RuntimeEventType.DIAGNOSTIC,
            RuntimeEventType.DIAGNOSTIC,
            RuntimeEventType.PERCEPTION,
        ]
        assert submitted[1]["payload"]["perception_stale"] is True
        assert submitted[2]["payload"]["perception_stale"] is False
        assert guard.submitted_observations == 2
        assert guard.suppressed_observations == 2

    asyncio.run(scenario())
