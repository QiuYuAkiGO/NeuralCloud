"""Read-only observer tests: synthetic trees, mock HTTP, temporary token files."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading

import httpx
import pytest

from harness.qq.accessibility import ChatChangeSignature
from harness.qq.observer import (
    MAX_TREE_BYTES, ObserverReadError, QQObserverEvidence, QQScopedAccessibilityClient, ScopedQQObserver,
)


NOW = datetime(2026, 9, 11, 6, tzinfo=timezone.utc)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def wall(self):
        return NOW + timedelta(seconds=self.value - 100)


def signature(tree):
    return ChatChangeSignature(tree.get("visible", True), digest(tree["chat"]) if tree.get("visible", True) else None, 1)


def response_tree(**updates):
    value = {
        "schema_version": "a11y-tree-v1", "body_instance_id": "synthetic-instance", "environment_revision": 2,
        "started_at": NOW.isoformat(), "finished_at": NOW.isoformat(), "duration_ms": 0,
        "semantic_digest": digest("baseline"), "changed": True, "truncated": False,
        "scope": {"bound": True, "active": True, "window_id": "0x123", "app_id": "org.mistyai.QQ",
                  "window_rect": {"x": 0, "y": 27, "width": 1280, "height": 693},
                  "top_node_id": "node:0", "overlays": []},
        "nodes": [{"node_id": "node:0"}],
    }
    value.update(updates)
    return value


@pytest.fixture
def http_case(tmp_path):
    token = tmp_path / "synthetic.token"
    token.write_text("synthetic-secret-token-123456", encoding="utf-8")
    clients = []

    def create(handler, **options):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)
        options.setdefault("wall_clock", lambda: NOW)
        return QQScopedAccessibilityClient(agent_url="http://127.0.0.1:28766", token_file=token,
            body_instance_id="synthetic-instance", environment_revision=2, window_id="0x123",
            client=client, **options)

    yield create
    for client in clients:
        client.close()


def test_baseline_is_not_a_change_and_unchanged_input_roster_animation_do_not_renew_firstseen():
    clock = Clock()
    source = {"chat": "old visible text", "input": "", "roster": "one", "animation": 0}
    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature, clock=clock, wall_clock=clock.wall)
    baseline = observer.poll_once()
    assert baseline.is_baseline and baseline.sequence == 0
    assert observer.changes_after(0).observations == ()
    source.update(input="draft typing", roster="two", animation=1)
    clock.value += 10
    same = observer.poll_once()
    assert same.first_seen_mono == baseline.first_seen_mono and same.first_seen_at == baseline.first_seen_at
    assert same.sequence == 0 and same.polled_mono == 110
    source["chat"] = "new incoming text"
    clock.value += 1
    arrival = observer.poll_once()
    source.update(input="more typing", animation=2)
    clock.value += 20
    latest = observer.poll_once()
    assert latest.sequence == arrival.sequence == 1
    assert latest.first_seen_mono == arrival.first_seen_mono == 111
    assert latest.first_seen_at == arrival.first_seen_at
    assert len(observer.changes_after(0).observations) == 1
    assert observer.status()["polls"] == 4 and observer.status()["changes"] == 1
    latest.tree["chat"] = "consumer mutation"
    assert observer.latest().tree["chat"] == "new incoming text"


def test_gap_hides_stale_latest_preserves_start_and_marks_recovered_change():
    clock = Clock()
    source = {"chat": "A", "visible": True}
    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature, clock=clock, wall_clock=clock.wall)
    observer.poll_once()
    source["visible"] = False
    clock.value += 1
    assert observer.poll_once() is None and observer.latest() is None
    clock.value += 5
    assert observer.poll_once() is None
    status = observer.status()
    assert status["gap_seconds"] == 5 and status["gap_started_at"] == (NOW + timedelta(seconds=1)).isoformat()
    assert status["last_error_type"] is None and observer.changes_after(0).gap
    source.update(chat="B", visible=True)
    recovered = observer.poll_once()
    assert recovered.after_gap and recovered.sequence == 1
    assert observer.status()["gap_started_at"] is None and not observer.changes_after(0).gap


def test_first_read_after_initial_gap_is_only_baseline():
    source = {"chat": "previous messages", "visible": False}
    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature)
    observer.poll_once()
    source["visible"] = True
    baseline = observer.poll_once()
    assert baseline.is_baseline and baseline.sequence == 0 and baseline.after_gap
    assert observer.changes_after(0).observations == ()


def test_change_buffer_reports_exact_cursor_overflow_and_retains_newest_changes():
    source = {"chat": "A"}
    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature, max_changes=2)
    for text in "ABCD":
        source["chat"] = text
        observer.poll_once()
    missed = observer.changes_after(0)
    assert missed.overflowed and missed.missed_changes == 1
    assert missed.oldest_sequence == 2 and missed.latest_sequence == 3
    assert [item.tree["chat"] for item in missed.observations] == ["C", "D"]
    assert not observer.changes_after(1).overflowed
    assert observer.changes_after(3).observations == ()
    assert observer.status()["overflows"] == 1


def test_byte_budget_is_bounded_independently_of_change_count():
    source = {"chat": "A", "synthetic_padding": "x" * (3 * 1024 * 1024)}
    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature, max_buffer_bytes=MAX_TREE_BYTES)
    for text in "ABC":
        source["chat"] = text
        observer.poll_once()
    batch = observer.changes_after(0)
    assert batch.overflowed and [item.tree["chat"] for item in batch.observations] == ["C"]
    assert observer.status()["overflows"] == 1


def test_background_collects_multiple_changes_while_consumer_is_busy():
    lock = threading.Lock()
    source = {"chat": "A"}
    baseline_read = threading.Event()
    reads = []

    def read():
        with lock:
            value = dict(source)
        reads.append(threading.current_thread().name)
        baseline_read.set()
        return value

    observer = ScopedQQObserver(read_fn=read, signature_fn=signature, poll_seconds=0.01).start()
    try:
        assert baseline_read.wait(1)
        # Ensure the first read has become the baseline, without consuming any
        # changes. No model or GUI call is made by this test.
        observer.wait_for_change(0, timeout=0.03)
        with lock:
            source["chat"] = "B"
        assert observer.wait_for_change(0, timeout=1).latest_sequence == 1
        with lock:
            source["chat"] = "C"
        assert observer.wait_for_change(1, timeout=1).latest_sequence == 2
        buffered = observer.changes_after(0)
        assert [item.tree["chat"] for item in buffered.observations] == ["B", "C"]
        assert all(name == "qq-scoped-observer" for name in reads)
        assert observer.start() is observer  # No second thread or second reader.
    finally:
        assert observer.close()
    assert not observer.status()["running"]


def test_close_does_not_lie_about_blocked_reader_and_discards_late_publish():
    entered, release = threading.Event(), threading.Event()
    closes = []

    def read():
        entered.set()
        assert release.wait(2)
        return {"chat": "late baseline"}

    observer = ScopedQQObserver(read_fn=read, signature_fn=signature, close_fn=lambda: closes.append(1)).start()
    try:
        assert entered.wait(1)
        assert observer.close(timeout=0.01) is False
        assert observer.status()["running"] and observer.status()["last_error_type"] == "close_timeout"
        assert not closes
    finally:
        release.set()
        assert observer.close(timeout=1)
    assert observer.latest() is None and observer.status()["changes"] == 0
    assert observer.close() and closes == [1]


def test_failed_read_becomes_gap_without_leaking_exception_text():
    def broken():
        raise httpx.ReadTimeout("PRIVATE_MEMBER_AND_MESSAGE synthetic-secret")

    observer = ScopedQQObserver(read_fn=broken, signature_fn=signature)
    assert observer.poll_once() is None
    status = observer.status()
    assert status["last_error_type"] == "request_timeout" and status["gap_started_at"]
    assert "PRIVATE" not in json.dumps(status) and "synthetic-secret" not in json.dumps(status)


@pytest.mark.parametrize("timeout", [-1, 1.1, float("nan"), True])
def test_wait_deadline_is_at_most_one_second(timeout):
    observer = ScopedQQObserver(read_fn=lambda: {"chat": "A"}, signature_fn=signature)
    with pytest.raises(ValueError):
        observer.wait_for_change(0, timeout=timeout)


@pytest.mark.parametrize("cursor", [-1, 1, True, "0"])
def test_cursors_from_another_run_or_bad_type_are_rejected(cursor):
    observer = ScopedQQObserver(read_fn=lambda: {"chat": "A"}, signature_fn=signature)
    with pytest.raises(ValueError):
        observer.changes_after(cursor)


def test_bound_reader_reuses_client_sends_only_scoped_read_and_previous_digest(http_case):
    requests = []

    def handler(request):
        assert request.method == "POST" and request.url.path == "/agent/v1/accessibility:read"
        assert request.headers["Authorization"] == "Bearer synthetic-secret-token-123456"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_tree())

    reader = http_case(handler)
    first = reader.read()
    second = reader.read()
    assert first == second
    assert requests[0]["scope"] == {"window_id": "0x123", "app_id": "org.mistyai.QQ",
        "window_rect": {"x": 0, "y": 27, "width": 1280, "height": 693}, "maximum_duration_ms": 1000}
    assert "previous_digest" not in requests[0]["scope"]
    assert requests[1]["scope"]["previous_digest"] == digest("baseline")
    assert requests[0]["body_instance_id"] == "synthetic-instance" and requests[0]["environment_revision"] == 2
    reader.close()
    with pytest.raises(ObserverReadError, match="client_closed"):
        reader.read()
    assert len(requests) == 2


@pytest.mark.parametrize("mutate,code", [
    (lambda tree: tree.update(body_instance_id="wrong"), "identity_mismatch"),
    (lambda tree: tree.update(environment_revision=3), "identity_mismatch"),
    (lambda tree: tree.update(environment_revision=True), "identity_mismatch"),
    (lambda tree: tree["scope"].update(window_id="0x999"), "scope_mismatch"),
    (lambda tree: tree["scope"].update(app_id="org.other.App"), "scope_mismatch"),
    (lambda tree: tree["scope"].update(active=False), "scope_mismatch"),
    (lambda tree: tree["scope"]["window_rect"].update(x=False), "scope_mismatch"),
    (lambda tree: tree["scope"]["window_rect"].update(width=1), "scope_mismatch"),
    (lambda tree: tree.update(truncated=True), "invalid_response"),
    (lambda tree: tree.update(semantic_digest="not-hash"), "invalid_response"),
    (lambda tree: tree.update(started_at=(NOW - timedelta(seconds=10)).isoformat(),
                             finished_at=(NOW - timedelta(seconds=10)).isoformat()), "stale_response"),
    (lambda tree: tree.update(started_at=(NOW + timedelta(seconds=10)).isoformat(),
                             finished_at=(NOW + timedelta(seconds=10)).isoformat()), "future_response"),
    (lambda tree: tree.update(started_at="2026-09-11T06:00:00"), "invalid_timing"),
    (lambda tree: tree.update(duration_ms=1001), "invalid_timing"),
    (lambda tree: tree.update(finished_at=(NOW - timedelta(seconds=1)).isoformat()), "invalid_timing"),
])
def test_reader_rejects_wrong_identity_scope_future_stale_and_bad_timing(http_case, mutate, code):
    tree = response_tree()
    mutate(tree)
    reader = http_case(lambda request: httpx.Response(200, json=tree))
    with pytest.raises(ObserverReadError) as error:
        reader.read()
    assert str(error.value) == code


def test_failed_response_does_not_poison_previous_digest(http_case):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        tree = response_tree()
        if len(requests) == 2:
            tree.update(semantic_digest=digest("untrusted"), body_instance_id="wrong")
        return httpx.Response(200, json=tree)

    reader = http_case(handler)
    reader.read()
    with pytest.raises(ObserverReadError):
        reader.read()
    reader.read()
    assert requests[2]["scope"]["previous_digest"] == digest("baseline")


def test_http_body_and_credentials_are_not_in_read_errors(http_case):
    reader = http_case(lambda request: httpx.Response(500, text="PRIVATE_CHAT synthetic-secret-token-123456"))
    with pytest.raises(ObserverReadError) as error:
        reader.read()
    assert str(error.value) == "http_error"


def test_response_size_and_total_elapsed_are_bounded(http_case):
    reader = http_case(lambda request: httpx.Response(200, content=b"x" * (MAX_TREE_BYTES + 1)))
    with pytest.raises(ObserverReadError, match="response_too_large"):
        reader.read()
    clock = Clock()

    def slow(request):
        clock.value += 2
        return httpx.Response(200, json=response_tree())

    reader = http_case(slow, clock=clock)
    with pytest.raises(ObserverReadError, match="request_timeout"):
        reader.read()


@pytest.mark.parametrize("url", ["https://127.0.0.1", "http://example.invalid", "http://user:secret@127.0.0.1",
                                 "http://127.0.0.1/path", "http://127.0.0.1/?token=secret"])
def test_non_loopback_or_credential_url_rejected_without_http(tmp_path, url):
    with pytest.raises(ValueError, match="loopback"):
        QQScopedAccessibilityClient(agent_url=url, token_file=tmp_path / "unused", body_instance_id="synthetic",
                                    environment_revision=2, window_id="0x123")


def test_observer_copies_the_source_tree_and_buffer_results():
    source = {"chat": "A", "nodes": [{"name": "synthetic old text"}]}
    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature)
    baseline = observer.poll_once()
    source["nodes"][0]["name"] = "new source text"
    assert baseline.tree["nodes"][0]["name"] == "synthetic old text"
    source["chat"] = "B"
    observer.poll_once()
    first = observer.changes_after(0)
    first.observations[0].tree["nodes"].clear()
    assert observer.changes_after(0).observations[0].tree["nodes"]
    assert deepcopy(observer.status()) == observer.status()


def test_unexpected_worker_failure_hides_stale_snapshot_and_reports_fixed_error():
    observer = ScopedQQObserver(read_fn=lambda: {"chat": "A"}, signature_fn=signature)
    assert observer.poll_once().is_baseline

    def broken_poll():
        raise RuntimeError("PRIVATE synthetic callback failure")

    observer.poll_once = broken_poll
    observer.start()
    observer._thread.join(1)
    assert not observer.status()["running"]
    assert observer.status()["last_error_type"] == "observer_error"
    assert observer.latest() is None
    assert observer.wait_for_change(0, timeout=0).observations == ()
    with pytest.raises(RuntimeError):
        observer.start()
    assert observer.close()


@pytest.mark.parametrize("skew,error", [(0.4, None), (2.0, "future_response"), (-5.0, "stale_response")])
def test_reader_records_paired_offset_even_when_freshness_rejects(http_case, skew, error):
    clock = Clock()

    def handler(request):
        start = clock.wall() + timedelta(seconds=skew + .05)
        clock.value += .4
        return httpx.Response(200, json=response_tree(started_at=start.isoformat(),
            finished_at=(start + timedelta(seconds=.3)).isoformat(), duration_ms=300,
            nodes=[{"name": "PRIVATE synthetic member"}]))

    reader = http_case(handler, clock=clock, wall_clock=clock.wall)
    assert reader.last_timing is None
    if error:
        with pytest.raises(ObserverReadError, match=error):
            reader.read()
    else:
        reader.read()
    timing = reader.last_timing
    assert set(timing) == {"local_started_at", "local_finished_at", "request_duration_ms", "clock_offset_ms", "clock_uncertainty_ms"}
    assert timing["request_duration_ms"] == pytest.approx(400)
    assert timing["clock_offset_ms"] == pytest.approx(skew * 1000)
    assert timing["clock_uncertainty_ms"] == pytest.approx(50)
    assert "PRIVATE" not in json.dumps(timing)
    timing["clock_offset_ms"] = 123456
    assert reader.last_timing["clock_offset_ms"] == pytest.approx(skew * 1000)
    # Diagnostics must never teach the next request to accept the same skew.
    if error:
        with pytest.raises(ObserverReadError, match=error):
            reader.read()


def test_observer_status_includes_only_safe_bound_reader_timing(http_case):
    clock = Clock()
    fail = [False]

    def handler(request):
        clock.value += .2
        if fail[0]:
            return httpx.Response(500, text="PRIVATE synthetic error text")
        return httpx.Response(200, json=response_tree())

    reader = http_case(handler, clock=clock, wall_clock=clock.wall)
    observer = ScopedQQObserver(read_fn=reader.read, signature_fn=lambda _: digest("chat"),
                                clock=clock, wall_clock=clock.wall)
    observer.poll_once()
    assert observer.status()["read_timing"]["request_duration_ms"] == 200
    fail[0] = True
    observer.poll_once()
    status = observer.status()
    assert status["last_error_type"] == "http_error"
    assert status["read_timing"]["clock_offset_ms"] is None
    assert status["read_timing"]["clock_uncertainty_ms"] is None
    assert "PRIVATE" not in json.dumps(status)


def causal_response(request, clock, *, skew=2.0, nonce=True, timestamp=None):
    started = timestamp or clock.wall() + timedelta(seconds=skew + .05)
    clock.value += .4
    tree = response_tree(started_at=started.isoformat(),
        finished_at=(started + timedelta(seconds=.3)).isoformat(), duration_ms=300)
    if nonce:
        tree["request_nonce"] = json.loads(request.content)["request_nonce"]
    return tree


@pytest.mark.parametrize("skew", [2.0, -4.0])
def test_optin_causal_freshness_accepts_bounded_skew_without_changing_legacy(http_case, skew):
    clock = Clock()
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json=causal_response(request, clock, skew=skew,
                                                       nonce="request_nonce" in payload))

    legacy = http_case(handler, clock=clock, wall_clock=clock.wall)
    with pytest.raises(ObserverReadError, match="future_response|stale_response"):
        legacy.read()
    assert "request_nonce" not in requests[0]
    reader = http_case(handler, clock=clock, wall_clock=clock.wall, causal_freshness=True)
    first = reader.read()
    clock.value += 1
    second = reader.read()
    assert first["request_nonce"] != second["request_nonce"]
    assert all(len(payload["request_nonce"]) == 64 for payload in requests[1:])
    assert reader.last_timing["clock_offset_ms"] == pytest.approx(skew * 1000)
    observer = ScopedQQObserver(read_fn=reader.read)
    assert observer.status()["read_freshness"] == "nonce_bound"
    assert "request_nonce" not in json.dumps(observer.status())


@pytest.mark.parametrize("bad_nonce", [None, "a" * 64, "非ASCII", 123])
def test_new_client_refuses_missing_wrong_or_malformed_nonce(http_case, bad_nonce):
    clock = Clock()

    def handler(request):
        tree = causal_response(request, clock, nonce=False)
        if bad_nonce is not None:
            tree["request_nonce"] = bad_nonce
        return httpx.Response(200, json=tree)

    reader = http_case(handler, clock=clock, wall_clock=clock.wall, causal_freshness=True)
    with pytest.raises(ObserverReadError, match="nonce_mismatch"):
        reader.read()
    assert reader.last_timing["clock_offset_ms"] == pytest.approx(2000)
    assert reader._last_causal_sample is None


@pytest.mark.parametrize("echo_new_nonce", [False, True])
def test_causal_reader_rejects_nonce_replay_but_diagnoses_repeated_wall_time(http_case, echo_new_nonce):
    clock = Clock()
    previous = []

    def handler(request):
        if not previous:
            previous.append(causal_response(request, clock))
            return httpx.Response(200, json=previous[0])
        tree = deepcopy(previous[0])
        if echo_new_nonce:
            tree["request_nonce"] = json.loads(request.content)["request_nonce"]
        clock.value += .4
        return httpx.Response(200, json=tree)

    reader = http_case(handler, clock=clock, wall_clock=clock.wall, causal_freshness=True)
    reader.read()
    baseline = reader._last_causal_sample
    clock.value += 1
    if echo_new_nonce:
        reader.read()
        assert reader._last_causal_sample != baseline
        assert reader.last_timing["clock_step_detected"] is True
        assert reader.last_timing["clock_step_count"] == 1
        assert reader.last_timing["progress_error_ms"] == pytest.approx(-1400)
    else:
        with pytest.raises(ObserverReadError, match="nonce_mismatch"):
            reader.read()
        assert reader._last_causal_sample == baseline


@pytest.mark.parametrize("skew", [5.1, -5.1, 10, -10])
def test_nonce_does_not_authorize_unbounded_clock_offset(http_case, skew):
    clock = Clock()
    reader = http_case(lambda request: httpx.Response(200, json=causal_response(request, clock, skew=skew)),
                       clock=clock, wall_clock=clock.wall, causal_freshness=True)
    with pytest.raises(ObserverReadError, match="clock_skew"):
        reader.read()
    assert reader._last_causal_sample is None


@pytest.mark.parametrize("corrected_skew", [4.0, -2.0])
def test_causal_clock_step_is_diagnostic_and_does_not_poison_next_fresh_read(http_case, corrected_skew):
    clock = Clock()
    skew = [1.0]
    reader = http_case(lambda request: httpx.Response(200, json=causal_response(request, clock, skew=skew[0])),
                       clock=clock, wall_clock=clock.wall, causal_freshness=True)
    reader.read()
    baseline = reader._last_causal_sample
    clock.value += 1
    skew[0] = corrected_skew
    reader.read()
    assert reader._last_causal_sample != baseline
    assert reader.last_timing["clock_step_detected"] is True
    assert reader.last_timing["clock_step_count"] == 1
    assert reader.last_timing["progress_error_ms"] == pytest.approx((corrected_skew - 1) * 1000)
    clock.value += 1
    reader.read()
    assert reader.last_timing["clock_step_detected"] is False
    assert reader.last_timing["clock_step_count"] == 1
    assert reader.last_timing["progress_error_ms"] == pytest.approx(0)
    observer = ScopedQQObserver(read_fn=reader.read)
    assert observer.status()["read_timing"]["clock_step_count"] == 1
    assert "request_nonce" not in json.dumps(observer.status())


def test_causal_mode_allows_measured_gradual_clock_slew_without_calibration(http_case):
    clock = Clock()
    skew = [2.4]
    reader = http_case(lambda request: httpx.Response(200, json=causal_response(request, clock, skew=skew[0])),
                       clock=clock, wall_clock=clock.wall, causal_freshness=True)
    for _ in range(10):
        reader.read()
        assert reader.last_timing["clock_offset_ms"] == pytest.approx(skew[0] * 1000)
        clock.value += 2
        skew[0] -= .2


class TimedReadLock:
    def __init__(self, clock, *, wait=.3, acquired=True):
        self.clock, self.wait, self.acquired = clock, wait, acquired
        self.timeouts = []
        self.held = False
        self.releases = 0

    def acquire(self, *, timeout):
        self.timeouts.append(timeout)
        self.clock.value += self.wait
        self.held = self.acquired
        return self.acquired

    def release(self):
        assert self.held
        self.held = False
        self.releases += 1


def test_read_lock_and_http_share_one_local_budget(http_case):
    clock = Clock()
    lock = TimedReadLock(clock)
    timeout = []

    def handler(request):
        assert lock.held
        timeout.append(request.extensions["timeout"])
        return httpx.Response(200, json=causal_response(request, clock, skew=0))

    reader = http_case(handler, clock=clock, wall_clock=clock.wall, causal_freshness=True, read_lock=lock)
    reader.read()
    assert lock.timeouts == [pytest.approx(.4)]
    assert all(seconds == pytest.approx(1.2) for seconds in timeout[0].values())
    assert not lock.held and lock.releases == 1
    assert reader.last_timing["request_duration_ms"] == pytest.approx(700)
    assert ScopedQQObserver(read_fn=reader.read).status()["read_freshness"] == "nonce_bound"


@pytest.mark.parametrize("acquired", [False, True])
def test_lock_wait_exhausts_budget_without_any_http_request(http_case, acquired):
    clock = Clock()
    lock = TimedReadLock(clock, wait=1.5, acquired=acquired)
    requests = []
    reader = http_case(lambda request: requests.append(request), clock=clock, wall_clock=clock.wall, read_lock=lock)
    with pytest.raises(ObserverReadError, match="request_timeout"):
        reader.read()
    assert not requests and not lock.held
    assert lock.releases == int(acquired)
    assert reader.last_timing["request_duration_ms"] == 1500


@pytest.mark.parametrize("fault,code", [("http", "http_error"), ("scope", "scope_mismatch"),
                                       ("slow", "request_timeout")])
def test_acquired_read_lock_releases_after_transport_validation_or_budget_failure(http_case, fault, code):
    clock = Clock()
    lock = TimedReadLock(clock)

    def handler(request):
        assert lock.held
        if fault == "http":
            return httpx.Response(503)
        tree = causal_response(request, clock, skew=0)
        if fault == "scope":
            tree["scope"]["active"] = False
        if fault == "slow":
            clock.value += .9
        return httpx.Response(200, json=tree)

    reader = http_case(handler, clock=clock, wall_clock=clock.wall, causal_freshness=True, read_lock=lock)
    with pytest.raises(ObserverReadError, match=code):
        reader.read()
    assert not lock.held and lock.releases == 1


def test_real_shared_lock_timeout_does_not_release_another_owners_lock(http_case):
    lock = threading.Lock()
    lock.acquire()
    requests = []
    reader = http_case(lambda request: requests.append(request), read_lock=lock, timeout_seconds=.05)
    try:
        with pytest.raises(ObserverReadError, match="request_timeout"):
            reader.read()
        assert lock.locked() and not requests
    finally:
        lock.release()


def test_read_lock_requires_the_lock_protocol(http_case):
    with pytest.raises(ValueError, match="acquire and release"):
        http_case(lambda _request: httpx.Response(200), read_lock=object())


def test_contended_lock_never_starts_remote_read_without_full_scope_budget(http_case):
    clock = Clock()
    lock = TimedReadLock(clock, wait=.5)
    requests = []
    reader = http_case(lambda request: requests.append(request), clock=clock, wall_clock=clock.wall, read_lock=lock)
    with pytest.raises(ObserverReadError, match="request_timeout"):
        reader.read()
    assert lock.timeouts == [pytest.approx(.4)]
    assert not requests and lock.releases == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_obscured_content_option_is_optin_and_echo_bound(http_case, enabled):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        tree = response_tree()
        tree["scope"]["skip_obscured_content"] = enabled
        if enabled:
            tree.update(content_state="occluded", content_coverage="top_level_bindings_only")
        return httpx.Response(200, json=tree)

    reader = http_case(handler, skip_obscured_content=enabled)
    reader.read()
    if enabled:
        assert requests[0]["scope"]["skip_obscured_content"] is True
    else:
        assert "skip_obscured_content" not in requests[0]["scope"]


@pytest.mark.parametrize("echo", [None, False, "true", 1])
def test_optin_obscured_mode_rejects_missing_or_mismatched_echo(http_case, echo):
    def handler(_request):
        tree = response_tree()
        if echo is not None:
            tree["scope"]["skip_obscured_content"] = echo
        return httpx.Response(200, json=tree)

    reader = http_case(handler, skip_obscured_content=True)
    with pytest.raises(ObserverReadError, match="scope_mismatch"):
        reader.read()


@pytest.mark.parametrize("fault", ["remote_duration_mismatch", "request_too_slow"])
def test_causal_mode_keeps_processing_and_request_duration_bounds(http_case, fault):
    clock = Clock()

    def handler(request):
        tree = causal_response(request, clock)
        if fault == "remote_duration_mismatch":
            tree["duration_ms"] = 600
        else:
            clock.value += 2
        return httpx.Response(200, json=tree)

    reader = http_case(handler, clock=clock, wall_clock=clock.wall, causal_freshness=True)
    with pytest.raises(ObserverReadError, match="invalid_timing|request_timeout"):
        reader.read()


def capture_metadata(clock, number=1):
    return {"capture_id": f"synthetic-capture-{number}", "captured_at": clock.wall(),
            "evidence_ref": "evidence://sha256/" + digest(f"png-{number}"),
            "content_digest": digest(f"png-{number}"),
            "a11y_evidence_ref": "evidence://sha256/" + digest(f"ax-{number}"),
            "a11y_digest": digest(f"late-tree-{number}")}


def test_evidence_only_on_semantic_changes_keeps_independent_times_and_digests():
    clock = Clock()
    source = {"chat": "A"}
    captures = []

    def capture():
        clock.value += .3
        result = capture_metadata(clock, len(captures) + 1)
        captures.append(result)
        clock.value += .2
        return result

    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature, evidence_fn=capture,
                                clock=clock, wall_clock=clock.wall)
    assert observer.poll_once().evidence is None  # Baseline never imports old screenshots.
    clock.value += 1
    observer.poll_once()
    assert not captures
    source["chat"] = "B"
    changed = observer.poll_once()
    assert isinstance(changed.evidence, QQObserverEvidence)
    assert changed.first_seen_mono == changed.polled_mono == 101
    assert changed.evidence_started_mono == 101 and changed.evidence_finished_mono == 101.5
    assert changed.evidence.captured_at == NOW + timedelta(seconds=1.3)
    assert changed.evidence_started_at == NOW + timedelta(seconds=1)
    assert changed.evidence_finished_at == NOW + timedelta(seconds=1.5)
    assert changed.evidence.a11y_digest != changed.digest  # The two reads are explicitly not atomic.
    assert changed.evidence.evidence_ref == captures[0]["evidence_ref"]
    assert not changed.after_gap
    clock.value += 1
    latest = observer.poll_once()
    assert len(captures) == 1 and latest.evidence == changed.evidence
    assert latest.first_seen_mono == changed.first_seen_mono
    assert observer.status()["evidence_captures"] == 1
    assert observer.status()["last_evidence_duration_ms"] == 500
    assert observer.status()["max_poll_duration_ms"] == 500
    assert observer.changes_after(0).observations[0].evidence == changed.evidence


def test_evidence_captured_in_background_while_consumer_is_busy():
    source = {"chat": "A"}
    captured = threading.Event()
    callers = []
    clock = Clock()

    def capture():
        callers.append(threading.current_thread().name)
        captured.set()
        return capture_metadata(clock)

    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature, evidence_fn=capture,
                                poll_seconds=.01)
    observer.poll_once()
    observer.start()
    try:
        source["chat"] = "B"
        # The simulated model/consumer does not poll or drain during this wait.
        assert captured.wait(1)
        batch = observer.wait_for_change(0, timeout=1)
        assert len(batch.observations) == 1 and batch.observations[0].evidence
        assert callers == ["qq-scoped-observer"]
    finally:
        observer.close()


def test_in_progress_evidence_hides_old_latest_and_close_does_not_publish_late_result():
    source = {"chat": "A"}
    entered, release = threading.Event(), threading.Event()

    def capture():
        entered.set()
        assert release.wait(2)
        return capture_metadata(Clock())

    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature, evidence_fn=capture,
                                poll_seconds=.01)
    observer.poll_once()
    source["chat"] = "B"
    observer.start()
    try:
        assert entered.wait(1)
        assert observer.status()["evidence_in_progress"] is True
        assert observer.status()["pending_digest"] == digest("B")
        assert observer.latest() is None and observer.changes_after(0).gap
        assert observer.changes_after(0).observations == ()
        assert observer.close(timeout=.01) is False
    finally:
        release.set()
        assert observer.close(timeout=1)
    assert observer.latest() is None and observer.changes_after(0).observations == ()


def test_failed_evidence_stays_gap_and_retry_preserves_first_seen():
    clock = Clock()
    source = {"chat": "A"}
    attempts = []

    def capture():
        attempts.append(clock.value)
        if len(attempts) == 1:
            raise OSError("PRIVATE synthetic member and chat")
        return capture_metadata(clock)

    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature, evidence_fn=capture,
                                clock=clock, wall_clock=clock.wall)
    observer.poll_once()
    source["chat"] = "B"
    clock.value += 1
    assert observer.poll_once() is None
    status = observer.status()
    assert status["last_error_type"] == "evidence_capture_failed"
    assert status["evidence_failures"] == 1 and status["changes"] == 0
    assert observer.latest() is None and observer.changes_after(0).gap
    assert "PRIVATE" not in json.dumps(status)
    clock.value += 5
    result = observer.poll_once()
    assert result.sequence == 1 and result.after_gap
    assert result.first_seen_mono == 101 and result.polled_mono == 106
    assert result.evidence_started_mono == 106
    assert observer.status()["evidence_attempts"] == 2 and observer.status()["evidence_captures"] == 1
    assert observer.status()["pending_digest"] is None


@pytest.mark.parametrize("fault", ["bytes", "text", "invalid_ref", "bad_digest", "naive_time"])
def test_capture_metadata_rejects_bytes_chat_or_unbounded_untyped_fields(fault):
    clock = Clock()
    source = {"chat": "A"}
    result = capture_metadata(clock)
    if fault == "bytes":
        result["png"] = b"raw image"
    elif fault == "text":
        result["text"] = "PRIVATE chat body"
    elif fault == "invalid_ref":
        result["evidence_ref"] = "data:image/png;base64,PRIVATE"
    elif fault == "bad_digest":
        result["a11y_digest"] = "PRIVATE nickname"
    else:
        result["captured_at"] = datetime(2026, 9, 11)
    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature, evidence_fn=lambda: result)
    observer.poll_once()
    source["chat"] = "B"
    assert observer.poll_once() is None
    assert observer.status()["last_error_type"] == "evidence_metadata_invalid"
    assert observer.changes_after(0).observations == ()


def test_evidence_metadata_counts_toward_bounded_change_buffer():
    clock = Clock()
    source = {"chat": "A"}
    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature,
        evidence_fn=lambda: capture_metadata(clock), clock=clock, wall_clock=clock.wall, max_changes=1)
    observer.poll_once()
    source["chat"] = "B"
    observer.poll_once()
    tree_size = len(json.dumps(source, separators=(",", ":")).encode())
    assert observer._buffer_bytes > tree_size
    source["chat"] = "C"
    observer.poll_once()
    batch = observer.changes_after(0)
    assert batch.overflowed and batch.missed_changes == 1
    assert len(batch.observations) == 1 and batch.observations[0].evidence
    assert observer._buffer_bytes <= observer.max_buffer_bytes


def test_completed_gap_remains_visible_after_current_gap_clears():
    clock = Clock()
    source = {"chat": "A", "visible": True}
    saved = []
    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature, gap_fn=saved.append,
                                clock=clock, wall_clock=clock.wall)
    observer.poll_once()
    clock.value += 1
    source["visible"] = False
    observer.poll_once()
    clock.value += 4
    observer.poll_once()
    clock.value += 8
    source["visible"] = True
    observer.poll_once()
    status = observer.status()
    assert status["gap_seconds"] == 0
    assert status["gap_sequence"] == status["completed_gap_count"] == 1
    assert status["total_gap_seconds"] == status["max_gap_seconds"] == 12
    assert len(saved) == 1 and saved[0].as_dict() == status["recent_gaps"][0]
    assert saved[0].started_at == NOW + timedelta(seconds=1)
    assert saved[0].finished_at == NOW + timedelta(seconds=13)
    assert saved[0].reason == "chat_occluded"
    status["recent_gaps"].clear()
    assert len(observer.status()["recent_gaps"]) == 1


def test_closed_gap_ring_is_bounded_and_sequences_expose_evicted_history():
    clock = Clock()
    source = {"chat": "A", "visible": True}
    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature,
                                clock=clock, wall_clock=clock.wall)
    observer.poll_once()
    for _ in range(35):
        source["visible"] = False
        observer.poll_once()
        clock.value += 1
        source["visible"] = True
        observer.poll_once()
    status = observer.status()
    assert status["gap_sequence"] == status["completed_gap_count"] == 35
    assert status["total_gap_seconds"] == 35 and status["max_gap_seconds"] == 1
    assert len(status["recent_gaps"]) == 32
    assert [gap["sequence"] for gap in status["recent_gaps"]] == list(range(4, 36))


def test_gap_sink_error_is_redacted_and_does_not_remove_local_gap_evidence():
    clock = Clock()
    source = {"chat": "A", "visible": True}

    def failed_sink(gap):
        raise OSError("PRIVATE synthetic log path and token")

    observer = ScopedQQObserver(read_fn=lambda: source, signature_fn=signature, gap_fn=failed_sink,
                                clock=clock, wall_clock=clock.wall)
    observer.poll_once()
    source["visible"] = False
    observer.poll_once()
    clock.value += 2
    source["visible"] = True
    assert observer.poll_once() is not None
    status = observer.status()
    assert status["gap_sink_failures"] == 1 and len(status["recent_gaps"]) == 1
    assert "PRIVATE" not in json.dumps(status)
