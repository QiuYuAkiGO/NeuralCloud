"""Protect the human-report provenance boundary and incomplete-output labels."""
import json

import httpx
import pytest

from eval.emotional_expression_diagnostics.before_after import (
    ReplayMismatch, VerifiedAuxReplay, result_status,
)
from eval.homeostatic_expression.production import json_hash


def fixture_call():
    user = json.dumps({"frozen_input": "synthetic"}, separators=(",", ":"), sort_keys=True)
    response_format = {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}}
    response = {"events": []}
    source = {"system": "appraise", "user": json.loads(user), "max_tokens": 1536,
              "timeout_seconds": 8.0, "response_format": response_format,
              "request_sha256": json_hash(["appraise", user, response_format]),
              "response": response, "response_sha256": json_hash(response),
              "stage": "appraisal", "outcome": "returned"}
    kwargs = {"system": "appraise", "user": user, "max_tokens": 1536,
              "timeout_seconds": 8.0, "response_format": response_format}
    return source, kwargs


@pytest.mark.parametrize("changed", ["system", "user", "response_format", "max_tokens", "timeout_seconds"])
def test_replay_refuses_changed_auxiliary_request(changed):
    source, kwargs = fixture_call()
    replay = VerifiedAuxReplay()
    replay.bind([source])
    kwargs[changed] = '{"frozen_input":"other"}' if changed == "user" else None
    with pytest.raises(ReplayMismatch):
        replay.complete_json(**kwargs)
    # The caller may catch the first exception as a model failure. A subsequent
    # check must still reject the run instead of mislabelling it a valid replay.
    with pytest.raises(ReplayMismatch):
        replay.assert_complete()


def test_recorded_timeout_is_preserved_and_never_replaced_with_a_response():
    source, kwargs = fixture_call()
    source.update(outcome="error", error_type="ReadTimeout")
    replay = VerifiedAuxReplay()
    replay.bind([source])
    with pytest.raises(httpx.ReadTimeout):
        replay.complete_json(**kwargs)
    replay.assert_complete()
    assert replay.calls[0]["outcome"] == "error"
    assert "response" not in replay.calls[0]


def test_replay_response_is_copied_and_digest_checked():
    source, kwargs = fixture_call()
    replay = VerifiedAuxReplay()
    replay.bind([source])
    result = replay.complete_json(**kwargs)
    result["events"].append("local-mutation")
    assert source["response"] == {"events": []}
    replay.assert_complete()
    source["response"]["events"].append("tampered")
    replay.bind([source])
    with pytest.raises(ReplayMismatch):
        replay.complete_json(**kwargs)


def test_completion_status_preserves_truncation_and_missing_metadata():
    assert result_status("text", [{"response_metadata": {"finish_reason": "length"}},
                                  {"response_metadata": {"finish_reason": "stop"}}]) == "truncated"
    assert result_status("text", [{}]) == "completion_unverified"
    assert result_status("", [{"response_metadata": {"finish_reason": "stop"}}]) == "empty"
    assert result_status("text", [{"response_metadata": {"finish_reason": "stop"}}]) == "complete"
