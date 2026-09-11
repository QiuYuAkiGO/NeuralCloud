import httpx
import pytest

from harness.config import Settings
from harness.llm.client import OpenAICompatClient, RoutingLLMClient


def test_per_call_timeout_reaches_transport_without_changing_following_calls():
    received = []

    def handle(request):
        received.append(request.extensions["timeout"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    settings = Settings(llm_timeout=60)
    client = OpenAICompatClient(settings)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handle), timeout=60)
    routing = RoutingLLMClient(client, client)
    try:
        routing.complete_chat_result([], timeout_seconds=2.5)
        routing.complete_chat_result([])
    finally:
        routing.close()
    assert set(received[0].values()) == {2.5}
    assert set(received[1].values()) == {60}
    assert settings.llm_timeout == 60


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_invalid_request_deadline_fails_before_network(seconds):
    client = OpenAICompatClient(Settings())
    try:
        with pytest.raises(ValueError, match="timeout"):
            client.complete_chat_result([], timeout_seconds=seconds)
    finally:
        client.close()
