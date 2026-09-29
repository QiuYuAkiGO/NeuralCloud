"""Bounded QQ rejection diagnostics; exception messages are never public data."""
from __future__ import annotations

import httpx

from harness.qq.telemetry import QQDeadlineExpired


_CODES = {
    "timeout": {"qq_turn_expired", "qq_model_timeout", "qq_stage_deadline_exhausted"},
    "evidence_rejected": {"production_memory_proposals_rejected"},
    "model_invalid": {"qq_model_response_invalid", "production_response_contract_invalid",
                      "production_memory_group_envelope_invalid"},
    "source_invalid": {
        "qq_turn_binding_conflict", "qq_turn_source_changed", "qq_turn_used_source_changed",
        "qq_turn_snapshot_corrupt", "qq_turn_snapshot_conflict", "qq_turn_resume_conflict",
        "qq_turn_configuration_changed", "qq_required_reference_missing", "qq_required_reference_invalid",
        "qq_current_source_missing", "qq_current_authority_invalid", "qq_turn_runtime_binding_missing",
        "qq_turn_runtime_binding_changed", "qq_turn_resume_binding_changed", "qq_turn_request_changed",
        "production_affect_unavailable_evidence", "production_orientation_unavailable_evidence",
        "production_memory_manifest_required", "production_memory_superseded", "production_snapshot_superseded",
    },
    "cancelled": {"qq_turn_cancelled", "qq_turn_interrupted", "qq_turn_in_progress_or_interrupted",
                  "production_runtime_fenced", "production_runtime_fence_missing"},
    "model_error": {"qq_model_http_error", "qq_model_error"},
    "internal_error": {"qq_turn_internal_error"},
    "transport_error": {"qq_transport_error", "shared_cognitive_unavailable"},
}
_CATEGORY_BY_CODE = {code: category for category, codes in _CODES.items() for code in codes}


def safe_failure_detail(value) -> dict[str, str]:
    """Only a known category/code pair may cross domain or report boundaries."""
    if not isinstance(value, dict):
        return {}
    category, code = value.get("failure_category"), value.get("error_code")
    if type(category) is not str or type(code) is not str or _CATEGORY_BY_CODE.get(code) != category:
        return {}
    return {"failure_category": category, "error_code": code}


def failure_detail(error: BaseException) -> dict[str, str]:
    """Classify known exceptions without copying bodies, URLs, or validation input."""
    if isinstance(error, QQDeadlineExpired):
        code = "qq_turn_expired"
    elif isinstance(error, (TimeoutError, httpx.TimeoutException)):
        code = "qq_model_timeout"
    elif isinstance(error, httpx.HTTPError):
        code = "qq_model_http_error"
    elif isinstance(error, (ValueError, TypeError)):
        # Recognize one exact machine code, never substrings or chained errors.
        candidate = str(error)
        code = candidate if candidate in _CATEGORY_BY_CODE else "qq_model_response_invalid"
    else:
        code = "qq_turn_internal_error"
    return {"failure_category": _CATEGORY_BY_CODE[code], "error_code": code}
