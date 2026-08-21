"""LLM client protocol: OpenAI-compatible HTTP + deterministic mock."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

import httpx

from harness.config import Settings, get_settings

VIOLATING_CARE_REPLY = "因为我还是担心你啊。昨天只是生气了，说的都是气话。对不起。"
REVISED_IN_CHARACTER_REPLY = "……我说的是不想管。\n\n她把视线挪回书上。\n\n又没说真的会不管。"
GREETING_REPLY = "嗯。"
DEFAULT_REPLY = "……知道了。"


class LLMClient(Protocol):
    def complete_json(self, *, system: str, user: str) -> dict[str, Any]: ...

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str: ...


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).removesuffix("```").strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError("LLM response did not contain a JSON object")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM JSON was not an object")
    return value


class OpenAICompatClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        content = self._chat_content(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
        )
        return parse_json_object(content)

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str:
        if stream:
            raise NotImplementedError("streaming is reserved for a later milestone")
        return self._chat_content(messages, temperature=temperature)

    def _chat_content(self, messages: list[dict[str, str]], *, temperature: float) -> str:
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature,
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])


class MockLLM:
    """Keyword-routed deterministic JSON / chat. Tests never need a GPU."""

    def __init__(
        self,
        routes: dict[str, dict[str, Any]] | None = None,
        chat_routes: dict[str, str] | None = None,
        chat_script: list[str] | None = None,
    ) -> None:
        self.routes = routes or {}
        self.chat_routes = chat_routes or {}
        self.chat_script = list(chat_script or [])
        self._script_i = 0
        self.calls: list[tuple[str, str]] = []
        self.chat_calls: list[list[dict[str, str]]] = []

    def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        blob = system + "\n" + user
        for key, value in self.routes.items():
            if key in blob:
                return dict(value)
        if "retrieval_plan" in blob or "time_scope" in blob:
            return {
                "time_scope": None,
                "entities": ["user", "character"],
                "event_type": None,
                "emotional_relevance": None,
                "channels": ["chat"],
            }
        if "persona_conditioned_insight" in blob:
            return {"persona_conditioned_insight": None, "insight_confidence": 0.0}
        if '"ops"' in blob or "curator" in blob.lower():
            return {"ops": [{"op": "NO_OP", "reason": "mock_default"}]}
        return {}

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str:
        if stream:
            raise NotImplementedError("streaming is reserved for a later milestone")
        self.chat_calls.append(messages)
        blob = "\n".join(message.get("content", "") for message in messages)
        for key, value in self.chat_routes.items():
            if key in blob:
                return value
        if any(
            token in blob
            for token in (
                "Revision instruction",
                "嘴硬",
                "不要道歉",
                "迂回",
                "premature_apology",
                "direct_care",
            )
        ):
            return REVISED_IN_CHARACTER_REPLY
        if self.chat_script:
            idx = min(self._script_i, len(self.chat_script) - 1)
            self._script_i += 1
            return self.chat_script[idx]
        if "不会再管" in blob or "昨天不是说" in blob:
            return VIOLATING_CARE_REPLY
        if any(token in blob for token in ("早上好", "晚上好", "你在干嘛", "一起吃饭")):
            return GREETING_REPLY
        return DEFAULT_REPLY
