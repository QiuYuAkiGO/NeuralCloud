"""Record validator provenance beside a call without adding it to model input.

``original_evidence`` is the complete semantic evidence DTO before projection.
``projection`` is an audit-only DTO owned by the caller: it should include the
compact semantic projection and its alias/row-to-original-source mapping. Neither
object is used to construct ``system`` or ``user``; those are the exact strings
the caller sends to the model. Do not pass client settings or HTTP objects as
evidence. Snapshots follow CallLog's JSON serialization contract (no embeddings).
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from harness.config import Settings
from infra.trace.call_log import CallLog
from infra.trace.request_spans import current_trace_context, new_span_id
from infra.trace.serialize import dump_value


_SCHEMA = "validator-input-audit-v1"


def _text_metrics(text: str) -> dict[str, Any]:
    encoded = text.encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(),
        "characters": len(text), "utf8_bytes": len(encoded)}


def _json_metrics(value: Any) -> dict[str, Any]:
    # Object hashes/counts describe this canonical serialization, not a Python
    # repr or the pretty-printed surrounding call-log file.
    return {**_text_metrics(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        "serialization": "json:ensure_ascii=false,sort_keys=true,separators=(',',':')"}


@dataclass
class ValidationInputAudit:
    """Capture the model's raw validator output before caller-side parsing."""

    _enabled: bool = field(repr=False)
    _result: Any = field(default=None, repr=False)
    _result_recorded: bool = field(default=False, repr=False)

    def set_result(self, raw_validation: Any) -> None:
        if self._enabled:
            self._result = dump_value(raw_validation)
            self._result_recorded = True


@contextmanager
def validation_input_audit(
    settings: Settings,
    *,
    original_evidence: dict[str, Any] | None,
    projection: dict[str, Any] | None,
    system: str,
    user: str,
    original_persona_identity: dict[str, Any] | None = None,
) -> Iterator[ValidationInputAudit]:
    """Append before/after/output steps to an existing call, or open a validator call.

    Logging disabled means no snapshots, file writes, or additions to a parent
    call. The yielded object's ``set_result(raw_validation)`` is then a no-op.
    Exceptions propagate unchanged; only their type is recorded. CallLog retains
    its normal write-failure behavior and causal metadata.
    """
    log = CallLog(settings)
    audit = ValidationInputAudit(_enabled=log.enabled)
    if not log.enabled:
        yield audit
        return

    audit_id = new_span_id("validator_audit")
    original = dump_value(original_evidence)
    original_persona = dump_value(original_persona_identity)
    projected = dump_value(projection)
    model_input = {"system": system, "user": user}
    started = time.perf_counter()
    with log.scope("validator", {"audit_id": audit_id, "audit_schema": _SCHEMA}) as call:
        metadata = {**current_trace_context(), "audit_id": audit_id}
        log.add_step("validator_input_before", {
            "schema_version": _SCHEMA, "original_evidence": original,
            "original_evidence_metrics": _json_metrics(original),
            "original_persona_identity": original_persona,
            "original_persona_identity_metrics": _json_metrics(original_persona),
        }, extra=metadata)
        log.add_step("validator_input_after", {
            "schema_version": _SCHEMA, "projection": projected,
            "projection_metrics": _json_metrics(projected),
            "model_input": model_input, "model_input_metrics": _json_metrics(model_input),
            "system_metrics": _text_metrics(system), "user_metrics": _text_metrics(user),
        }, extra=metadata)
        error_type = None
        try:
            yield audit
        except BaseException as exc:
            error_type = type(exc).__name__
            raise
        finally:
            outcome = {"schema_version": _SCHEMA, "status": "error" if error_type else "success",
                "result_recorded": audit._result_recorded, "raw_validation": audit._result,
                "raw_validation_metrics": _json_metrics(audit._result) if audit._result_recorded else None,
                "error_type": error_type}
            log.add_step("validator_output", outcome, extra={**metadata,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3)})
            call.set_result(outcome)
