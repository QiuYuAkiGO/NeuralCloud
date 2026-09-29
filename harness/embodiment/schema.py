"""Versioned JSON Schema catalog for the canonical embodiment boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from harness.embodiment.board_contracts import (
    BoardAppendRequest,
    BoardEntry,
    BoardEvidenceRequest,
    BoardReadRequest,
    BoardReadResult,
    BoardReceipt,
    BoardReconcileRequest,
    BoardReconcileResult,
    BoardStatus,
)
from harness.embodiment.capabilities import ProviderCapabilities
from harness.embodiment.web_contracts import WebReadArguments, WebResult, WebSearchArguments, WebSource
from harness.embodiment.voice_contracts import (
    VoiceJob,
    VoiceProfile,
    VoiceRenderRequest,
    VoiceScope,
    VoiceStatus,
)
from harness.embodiment.contracts import (
    BodyStateSnapshot,
    BodyRestCommand,
    BodyRestReceipt,
    BodyRestStatus,
    CognitiveUsage,
    CognitiveUsageReceipt,
    EmbodiedActionSpec,
    EvidenceRef,
    PerceptionSnapshot,
)


def schema_catalog() -> dict[str, dict[str, Any]]:
    return {
        "action.v1": TypeAdapter(EmbodiedActionSpec).json_schema(),
        "observation.v1": TypeAdapter(PerceptionSnapshot).json_schema(),
        "evidence.v1": TypeAdapter(EvidenceRef).json_schema(),
        "capability.v1": TypeAdapter(ProviderCapabilities).json_schema(),
        "web.v1": TypeAdapter(WebSearchArguments | WebReadArguments | WebSource | WebResult).json_schema(),
        "physiology.body-state.v1": TypeAdapter(BodyStateSnapshot).json_schema(),
        "physiology.rest.v1": TypeAdapter(BodyRestCommand | BodyRestReceipt | BodyRestStatus).json_schema(),
        "physiology.cognitive-usage.v1": TypeAdapter(CognitiveUsage | CognitiveUsageReceipt).json_schema(),
        "expression.voice.v1": TypeAdapter(
            VoiceScope | VoiceStatus | VoiceProfile | VoiceRenderRequest | VoiceJob
        ).json_schema(),
        "expression.board.v1": TypeAdapter(
            BoardReadRequest
            | BoardReadResult
            | BoardAppendRequest
            | BoardReceipt
            | BoardReconcileRequest
            | BoardReconcileResult
            | BoardEvidenceRequest
            | BoardEntry
            | BoardStatus
        ).json_schema(),
    }


def export_schemas(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, schema in schema_catalog().items():
        (output_dir / f"{name}.schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )


if __name__ == "__main__":
    export_schemas(Path(__file__).resolve().parents[2] / "contracts" / "embodiment")
