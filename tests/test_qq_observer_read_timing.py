"""Synthetic transport clocks and HTTP only; no running QQ or Agent access."""
from copy import deepcopy
from datetime import timedelta
import inspect
import json
import time

import httpx
import pytest

from harness.qq.observer import ObserverReadError, ScopedQQObserver
from tests import test_qq_observer as fixtures

http_case = fixtures.http_case
Clock = fixtures.Clock
NOW = fixtures.NOW


def timed_response(request, clock, *, jump=0, elapsed=.55, envelope_ms=501):
    nonce = json.loads(request.content).get("request_nonce")
    start = clock.wall()
    clock.value += elapsed
    return fixtures.response_tree(
        started_at=start.isoformat(),
        finished_at=(start + timedelta(seconds=.49 + jump)).isoformat(),
        duration_ms=490,
        request_nonce=nonce,
        read_timing={
            "schema_version": "a11y-read-timing-v1", "basis": "agent_monotonic",
            "request_nonce": nonce, "read_started_offset_ms": 0,
            "read_finished_offset_ms": envelope_ms,
        },
    )


@pytest.mark.parametrize("jump", [-2.4, 2.4])
def test_same_request_wall_step_uses_nonce_monotonic_envelope(http_case, jump):
    clock = Clock()
    reader = http_case(
        lambda request: httpx.Response(200, json=timed_response(request, clock, jump=jump)),
        clock=clock, wall_clock=clock.wall, causal_freshness=True,
    )
    tree = reader.read()
    assert tree["finished_at"] != tree["started_at"]
    assert reader.last_timing["freshness_timing_basis"] == "agent_monotonic"
    assert reader.last_timing["intra_request_wall_step"] is True
    assert reader.last_timing["wall_duration_error_ms"] == pytest.approx(jump * 1000)
    assert reader.last_timing["request_duration_ms"] == pytest.approx(550)
    assert "request_nonce" not in json.dumps(reader.last_timing)


@pytest.mark.parametrize("fault,expected", [
    ("metadata_missing_field", "invalid_timing"), ("metadata_null", "invalid_timing"),
    ("schema", "invalid_timing"), ("basis", "invalid_timing"),
    ("nonce_missing", "nonce_mismatch"), ("nonce_wrong", "nonce_mismatch"),
    ("top_nonce_wrong", "nonce_mismatch"), ("instance", "identity_mismatch"),
    ("revision", "identity_mismatch"), ("window", "scope_mismatch"),
    ("nan", "invalid_timing"), ("infinity", "invalid_timing"),
    ("bool", "invalid_timing"), ("negative", "invalid_timing"),
    ("nonzero_start", "invalid_timing"), ("reversed", "invalid_timing"),
    ("ax_longer_than_envelope", "invalid_timing"), ("ax_over_budget", "invalid_timing"),
    ("envelope_longer_than_rtt", "invalid_timing"), ("too_late", "request_timeout"),
    ("hard_limit_even_with_larger_config", "invalid_timing"), ("clock_skew", "clock_skew"),
])
def test_invalid_new_evidence_never_falls_back_to_legacy_wall(http_case, fault, expected):
    clock = Clock()

    def handler(request):
        tree = timed_response(request, clock, elapsed=1.501 if fault in {
            "too_late", "hard_limit_even_with_larger_config"
        } else .55)
        timing = tree["read_timing"]
        if fault == "metadata_missing_field":
            del timing["read_finished_offset_ms"]
        elif fault == "metadata_null":
            tree["read_timing"] = None
        elif fault == "schema":
            timing["schema_version"] = "future-v2"
        elif fault == "basis":
            timing["basis"] = "wall_clock"
        elif fault == "nonce_missing":
            del timing["request_nonce"]
        elif fault == "nonce_wrong":
            timing["request_nonce"] = "f" * 64
        elif fault == "top_nonce_wrong":
            tree["request_nonce"] = "f" * 64
        elif fault == "instance":
            tree["body_instance_id"] = "other"
        elif fault == "revision":
            tree["environment_revision"] += 1
        elif fault == "window":
            tree["scope"]["window_id"] = "0x999"
        elif fault in {"nan", "infinity", "bool", "negative"}:
            timing["read_finished_offset_ms"] = {
                "nan": float("nan"), "infinity": float("inf"), "bool": True, "negative": -1,
            }[fault]
        elif fault == "nonzero_start":
            timing["read_started_offset_ms"] = 1
        elif fault == "reversed":
            timing["read_started_offset_ms"] = 600
        elif fault == "ax_longer_than_envelope":
            tree["duration_ms"] = 502
        elif fault == "ax_over_budget":
            tree["duration_ms"] = 1000.001
        elif fault == "envelope_longer_than_rtt":
            timing["read_finished_offset_ms"] = 551
        elif fault == "clock_skew":
            tree["started_at"] = (NOW + timedelta(seconds=6)).isoformat()
        return httpx.Response(200, content=json.dumps(tree), headers={"Content-Type": "application/json"})

    reader = http_case(handler, clock=clock, wall_clock=clock.wall, causal_freshness=True,
                       timeout_seconds=2 if fault == "hard_limit_even_with_larger_config" else 1.5)
    with pytest.raises(ObserverReadError, match=expected):
        reader.read()
    assert reader._previous_digest is None and reader._last_causal_sample is None


@pytest.mark.parametrize("rebind_top_nonce", [False, True])
def test_replayed_envelope_is_not_accepted_even_if_outer_nonce_is_replaced(http_case, rebind_top_nonce):
    clock, saved = Clock(), []

    def handler(request):
        if not saved:
            saved.append(timed_response(request, clock))
            return httpx.Response(200, json=saved[0])
        tree = deepcopy(saved[0])
        clock.value += .55
        if rebind_top_nonce:
            tree["request_nonce"] = json.loads(request.content)["request_nonce"]
        return httpx.Response(200, json=tree)

    reader = http_case(handler, clock=clock, wall_clock=clock.wall, causal_freshness=True)
    reader.read()
    prior = reader._last_causal_sample
    with pytest.raises(ObserverReadError, match="nonce_mismatch"):
        reader.read()
    assert reader._last_causal_sample == prior


@pytest.mark.parametrize("jump", [-2.4, 2.4])
def test_missing_new_metadata_retains_strict_legacy_interval(http_case, jump):
    clock = Clock()

    def handler(request):
        tree = timed_response(request, clock, jump=jump)
        del tree["read_timing"]
        return httpx.Response(200, json=tree)

    reader = http_case(handler, clock=clock, wall_clock=clock.wall, causal_freshness=True)
    with pytest.raises(ObserverReadError, match="invalid_timing"):
        reader.read()


def test_transport_default_is_high_resolution_without_changing_first_seen_clock(http_case):
    reader = http_case(lambda _request: httpx.Response(200, json=fixtures.response_tree()))
    assert reader.clock is time.perf_counter
    assert inspect.signature(ScopedQQObserver).parameters["clock"].default is time.monotonic


def test_sub_tick_transport_interval_is_preserved_with_injected_high_resolution_clock(http_case):
    clock = Clock()
    reader = http_case(
        lambda request: httpx.Response(200, json=timed_response(request, clock, elapsed=.503)),
        clock=clock, wall_clock=clock.wall, causal_freshness=True,
    )
    reader.read()  # 501ms Agent envelope inside true 503ms RTT, not a rounded 500ms RTT.
    assert reader.last_timing["request_duration_ms"] == pytest.approx(503)
