import httpx

from harness.config import Settings
from harness.llm.client import ChatResult, OpenAICompatClient
from harness.llm.health import ProviderHealth
from tests.test_main_reply_fallback import wrapper, MESSAGES, status_error


def monitor(handler, clock=lambda: 0):
    return ProviderHealth(Settings(_env_file=None, llm_model="qwen"),
                          transport=httpx.MockTransport(handler), clock=clock, start=False)


def good(request):
    return httpx.Response(200, json={"data": [{"id": "qwen"}]})


def test_unknown_stale_failure_and_recovery_never_wait_for_a_probe():
    now = [0]
    health = monitor(good, lambda: now[0])
    client, main, fallback = wrapper()
    client.health = health
    assert client.complete_chat(MESSAGES) == "备用回复"
    assert not main.calls
    health.probe()
    assert not health.snapshot()["ready"]
    health.probe()
    assert client.complete_chat(MESSAGES) == "主回复"
    main.result = status_error(503)
    assert client.complete_chat(MESSAGES) == "备用回复"
    assert not health.snapshot()["ready"]
    health.probe()
    assert not health.snapshot()["ready"]
    health.probe()
    assert not health.snapshot()["ready"]
    assert client.complete_chat(MESSAGES) == "备用回复"
    now[0] = 30
    health.probe()
    main.result = ChatResult(text="恢复回复")
    assert client.complete_chat(MESSAGES) == "恢复回复"
    assert health.snapshot()["ready"]
    now[0] = 42
    assert client.complete_chat(MESSAGES) == "备用回复"
    assert len(main.calls) == 3 and len(fallback.calls) == 4
    client.close()


def test_probe_cannot_override_a_newer_generation_failure():
    health = None
    def race(request):
        health.failed()
        return good(request)
    health = monitor(race)
    health.probe()
    assert not health.snapshot()["ready"]
    assert health.snapshot()["reason"] == "generation_unavailable"
    health.close()


def test_unexpected_model_or_malformed_response_is_not_ready():
    for body in [{"data": [{"id": "other"}]}, [], {"data": None}, {}]:
        health = monitor(lambda request: httpx.Response(200, json=body))
        health.probe()
        health.probe()
        assert not health.snapshot()["ready"]
        health.close()


def test_primary_connect_cap_survives_per_call_timeout():
    observed = []
    def serve(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    client = OpenAICompatClient(Settings(_env_file=None), connect_timeout=1)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(serve))
    try:
        client.complete_chat_result(MESSAGES, timeout_seconds=9)
        assert observed == [{"connect": 1, "read": 9, "write": 9, "pool": 9}]
    finally:
        client.close()


def test_model_listing_does_not_heal_generation_and_only_one_trial_is_leased():
    now = [0]
    health = monitor(good, lambda: now[0])
    health.probe()
    health.probe()
    old_epoch = health.begin_attempt(10)
    health.failed("ReadTimeout")
    health.succeeded(old_epoch)
    for tick in [5, 10, 20]:
        now[0] = tick
        health.probe()
        assert not health.snapshot()["ready"]
        assert health.begin_attempt(10) is None
    now[0] = 30
    health.probe()
    lease = health.begin_attempt(10)
    assert lease is not None
    assert health.begin_attempt(10) is None
    health.failed("ReadTimeout")
    health.succeeded(lease)
    assert not health.snapshot()["ready"]
    health.close()
