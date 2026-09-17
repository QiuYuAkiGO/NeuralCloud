import json

import httpx

from harness.config import Settings
from harness.emotion.endpoint import build_emotion_client
from harness.emotion import EmotionService


def test_dedicated_key_and_json_mode_do_not_change_main_or_aux(tmp_path):
    key = tmp_path / "emotion.key"
    key.write_text("dedicated-test-key")
    config = tmp_path / "emotion.json"
    config.write_text(json.dumps({"api_key_file": str(key)}))
    settings = Settings(emotion_llm_config=config, llm_api_key="main-key",
                        aux_llm_api_key="aux-key", llm_model="deepseek-v4-flash")
    service = EmotionService(settings, object())
    assert service._distinct_aux()
    client = service._emotion_client
    def respond(request):
        payload = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer dedicated-test-key"
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["thinking"] == {"type": "disabled"}
        assert '"required":["events"]' in payload["messages"][0]["content"]
        assert "keep_alive" not in payload and "options" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"events":[]}'}}]})
    client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        assert client.complete_json(system="Appraise", user="hello", timeout_seconds=2,
            response_format={"type": "json_schema", "json_schema": {
                "schema": {"type": "object", "required": ["events"]}}}) == {"events": []}
        assert settings.llm_api_key == "main-key"
        assert settings.aux_llm_api_key == "aux-key"
    finally:
        client.close()


def test_unconfigured_keeps_existing_routing():
    assert build_emotion_client(Settings()) is None
