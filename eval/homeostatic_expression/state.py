"""Fresh, authoritative HomeostasisEngine state for release soaks."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from harness.contracts.envelope import DomainCommandEnvelope, VersionVector
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.homeostasis.models import (
    HomeostaticInteroceptiveSummary,
    HomeostaticSnapshot,
)

from .production import json_hash


def snapshot_evidence(snapshot: HomeostaticSnapshot) -> dict[str, Any]:
    summary = snapshot.interoceptive_summary
    if summary is None:
        raise RuntimeError("authoritative homeostasis snapshot has no interoceptive summary")
    return {
        "schema_version": snapshot.schema_version,
        "profile_ref": f"{snapshot.profile_id}@{snapshot.profile_revision}",
        "revision": snapshot.revision,
        "snapshot_sha256": json_hash(snapshot.model_dump(mode="json")),
        "summary_sha256": json_hash(summary.model_dump(mode="json")),
        "summary": summary.model_dump(mode="json"),
        "salience": summary.salience,
    }


def initialize_fresh_homeostasis_state(
    state_dir: Path,
    *,
    profile_path: Path,
    now: datetime | None = None,
) -> str:
    """Create and durably persist a never-reused profile-revision-3 state."""

    if state_dir.exists() and any(state_dir.iterdir()):
        raise FileExistsError(f"fresh state directory is not empty: {state_dir}")
    state_dir.mkdir(parents=True, exist_ok=True)
    profile = load_profile(profile_path)
    if profile.revision != 3:
        raise ValueError(f"soak requires profile revision 3, got {profile.profile_ref}")
    started = now or datetime.now(UTC)
    engine = HomeostasisEngine(profile, state_dir=state_dir, now=started)
    asyncio.run(engine.close())
    evidence = snapshot_evidence(engine.snapshot)
    marker = {
        "schema_version": "homeostatic-expression-soak-state-v2",
        "created_at": started.isoformat(),
        "profile_ref": profile.profile_ref,
        "initial_revision": evidence["revision"],
        "initial_snapshot_sha256": evidence["snapshot_sha256"],
        "initial_summary_sha256": evidence["summary_sha256"],
        "snapshot_path": str((state_dir / "snapshot.json").resolve()),
    }
    (state_dir / "soak-state.json").write_text(
        json.dumps(marker, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return f"{profile.profile_ref}:snapshot:{engine.snapshot.revision}"


class FreshHomeostasisSoak:
    """Advance a fresh engine and expose only authoritative nominal summaries."""

    def __init__(
        self,
        state_dir: Path,
        *,
        profile_path: Path,
        clock: Callable[[], datetime],
    ) -> None:
        self.state_dir = state_dir
        self.profile = load_profile(profile_path)
        if self.profile.revision != 3:
            raise ValueError(f"soak requires profile revision 3, got {self.profile.profile_ref}")
        marker_path = state_dir / "soak-state.json"
        if not marker_path.is_file():
            raise FileNotFoundError("release soak requires state created by initialize_fresh_homeostasis_state")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("schema_version") != "homeostatic-expression-soak-state-v2":
            raise ValueError("fresh state marker schema is invalid")
        if marker.get("profile_ref") != self.profile.profile_ref:
            raise ValueError("fresh state marker profile differs from the selected profile")
        if marker.get("initial_revision") != 0:
            raise ValueError("release soak fresh state must begin at revision 0")
        self.clock = clock
        self.engine = HomeostasisEngine(
            self.profile,
            state_dir=state_dir,
            clock=clock,
        )
        self.initial = snapshot_evidence(self.engine.snapshot)
        marker_matches = (
            self.initial["revision"] == marker.get("initial_revision")
            and self.initial["snapshot_sha256"] == marker.get("initial_snapshot_sha256")
            and self.initial["summary_sha256"] == marker.get("initial_summary_sha256")
        )
        if not marker_matches:
            asyncio.run(self.engine.close())
            raise ValueError("fresh homeostasis state was already advanced or differs from its creation marker")
        if self.initial["salience"] != "nominal":
            asyncio.run(self.engine.close())
            raise ValueError("release soak must start from an authoritative nominal snapshot")
        self._receive_seq = max(0, self.engine.snapshot.acknowledged_receive_seq)

    def advance(
        self,
        *,
        sequence: int,
    ) -> tuple[HomeostaticInteroceptiveSummary, dict[str, Any]]:
        """Commit one real heartbeat and read the authoritative result back."""

        issued_at = self.clock()
        if issued_at.tzinfo is None:
            issued_at = issued_at.replace(tzinfo=UTC)
        self._receive_seq += 1
        command = DomainCommandEnvelope[dict[str, Any]](
            command_type="ApplyHeartbeat",
            event_id=f"eval_soak_heartbeat_{sequence}",
            receive_seq=self._receive_seq,
            correlation_id=f"eval_soak_{sequence}",
            runtime_instance_id="homeostatic-expression-soak",
            manifest_revision=3,
            idempotency_key=f"homeostatic-expression-soak:{sequence}",
            deadline=issued_at + timedelta(minutes=5),
            issued_at=issued_at,
            expected_revision=self.engine.snapshot.revision,
            version_vector=VersionVector(homeostasis_state=self.engine.snapshot.revision),
            payload={
                "event": {
                    "event_type": "heartbeat",
                    "source": "scheduler",
                    "source_time": issued_at.isoformat(),
                    "payload": {},
                }
            },
        )
        result = asyncio.run(self.engine.apply_heartbeat(command))
        if result.status not in {"applied", "idempotent_replay"}:
            raise RuntimeError(f"homeostasis soak heartbeat failed: {result.status}: {result.error}")
        authoritative = asyncio.run(self.engine.get_snapshot())
        if authoritative.revision != result.committed_revision:
            raise AssertionError("authoritative homeostasis revision does not match heartbeat result")
        snapshot_payload = dict(authoritative.payload)
        snapshot_payload.pop("profile", None)
        snapshot = HomeostaticSnapshot.model_validate(snapshot_payload)
        summary = snapshot.interoceptive_summary
        if summary is None:
            raise RuntimeError("advanced homeostasis state has no semantic summary")
        if summary.salience != "nominal":
            raise RuntimeError("fresh soak nominal baseline left nominal salience; aborting paired evaluation")
        return summary, snapshot_evidence(snapshot)

    def finish(self) -> dict[str, Any]:
        """Persist, reopen the mirror, and prove recorded/disk identity."""

        asyncio.run(self.engine.close())
        authoritative = snapshot_evidence(self.engine.snapshot)
        snapshot_path = self.state_dir / "snapshot.json"
        disk_snapshot = HomeostaticSnapshot.model_validate_json(snapshot_path.read_text(encoding="utf-8"))
        disk = snapshot_evidence(disk_snapshot)
        matches = (
            authoritative["revision"] == disk["revision"]
            and authoritative["snapshot_sha256"] == disk["snapshot_sha256"]
            and authoritative["summary_sha256"] == disk["summary_sha256"]
        )
        final_evidence = {
            "initial": self.initial,
            "final": authoritative,
            "disk": disk,
            "disk_matches_recorded_final": matches,
            "advanced_revisions": authoritative["revision"] - self.initial["revision"],
            "snapshot_path": str(snapshot_path.resolve()),
        }
        (self.state_dir / "soak-final-evidence.json").write_text(
            json.dumps(final_evidence, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return final_evidence
