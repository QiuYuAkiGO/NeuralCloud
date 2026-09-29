"""Production QQ stages share the immutable, frozen reply window."""
from __future__ import annotations

from datetime import datetime, timedelta

from harness.qq.policy import QQLatencyOptions


def is_production_qq_command(command) -> bool:
    return (command.command_type in {"PrepareQQTurn", "ResumeQQResponse"}
            and isinstance(command.payload.get("production_context"), dict))


def downstream_reserve(command) -> float:
    """Reserves already removed from a production command's absolute deadline."""
    if not is_production_qq_command(command):
        return 0.0
    policy = QQLatencyOptions.model_validate(command.payload.get("qq_frozen_policy") or {})
    return policy.qq_delivery_reserve_seconds + (
        policy.qq_body_reserve_seconds if command.command_type == "PrepareQQTurn" else 0.0)


def stage_deadline(command) -> datetime:
    """Use the journaled request/policy, never a new window or current GUI policy."""
    original = datetime.fromisoformat(command.payload["deadline_at"])
    if original.tzinfo is None or original.utcoffset() is None:
        raise ValueError("qq_turn_deadline_timezone_required")
    return original - timedelta(seconds=downstream_reserve(command))
