"""LLM client protocol: OpenAI-compatible HTTP + deterministic mock."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from harness.config import Settings, get_settings
from harness.core.inference import TokenUsage

logger = logging.getLogger(__name__)

VIOLATING_CARE_REPLY = "因为我还是担心你啊。昨天只是生气了，说的都是气话。对不起。"
REVISED_IN_CHARACTER_REPLY = "……我说的是不想管。\n\n她把视线挪回书上。\n\n又没说真的会不管。"
GREETING_REPLY = "嗯。"
DEFAULT_REPLY = "……知道了。"


class ChatResult(BaseModel):
    text: str
    usage: TokenUsage = Field(default_factory=TokenUsage)


class LLMClient(Protocol):
    def complete_json(
        self, *, system: str, user: str, max_tokens: int | None = None
    ) -> dict[str, Any]: ...

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str: ...

    def complete_chat_result(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> ChatResult: ...


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


def approx_tokens(text: str) -> int:
    """Same CJK-friendly heuristic as the context compiler."""
    if not text:
        return 0
    return max(1, (len(text) + 1) // 2)


def estimate_chat_usage(messages: list[dict[str, str]], text: str) -> TokenUsage:
    prompt = sum(approx_tokens(message.get("content") or "") for message in messages)
    completion = approx_tokens(text)
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, source="estimate")


def usage_from_api_payload(raw: Any, messages: list[dict[str, str]], text: str) -> TokenUsage:
    if isinstance(raw, dict):
        prompt = int(raw.get("prompt_tokens") or 0)
        completion = int(raw.get("completion_tokens") or 0)
        total = int(raw.get("total_tokens") or 0)
        if prompt or completion or total:
            return TokenUsage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                source="api",
            )
    return estimate_chat_usage(messages, text)


class OpenAICompatClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._extra_payload = dict(extra_payload or {})
        # One long-lived client: keep-alive reuse across all pipeline stages
        # instead of a fresh TCP+TLS handshake per call.
        self._client = httpx.Client(timeout=self.settings.llm_timeout)

    def close(self) -> None:
        self._client.close()

    def complete_json(
        self, *, system: str, user: str, max_tokens: int | None = None
    ) -> dict[str, Any]:
        content = self.complete_chat_result(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            max_tokens=max_tokens,
        ).text
        return parse_json_object(content)

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str:
        return self.complete_chat_result(messages, temperature=temperature, stream=stream).text

    def complete_chat_result(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
        max_tokens: int | None = None,
    ) -> ChatResult:
        if stream:
            raise NotImplementedError("streaming is reserved for a later milestone")
        payload: dict[str, Any] = {
            **self._extra_payload,
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        response = self._client.post(
            f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        body = response.json()
        text = str(body["choices"][0]["message"]["content"])
        return ChatResult(text=text, usage=usage_from_api_payload(body.get("usage"), messages, text))


def _filled(value: str | None) -> bool:
    return bool((value or "").strip())


def resolve_aux_endpoint(settings: Settings) -> tuple[str, str, str]:
    """Resolve (base_url, model, api_key) for helper JSON calls, falling back to the character model."""
    base_url = (settings.aux_llm_base_url or "").strip() or settings.llm_base_url
    model = (settings.aux_llm_model or "").strip() or settings.llm_model
    api_key = (settings.aux_llm_api_key or "").strip() or settings.llm_api_key
    return base_url, model, api_key


def aux_explicitly_configured(settings: Settings) -> bool:
    return any(
        _filled(value)
        for value in (settings.aux_llm_base_url, settings.aux_llm_model, settings.aux_llm_api_key)
    )


def aux_endpoint_distinct(settings: Settings) -> bool:
    return resolve_aux_endpoint(settings) != (
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_api_key,
    )


def aux_model_distinct(settings: Settings) -> bool:
    aux_url, aux_model, _ = resolve_aux_endpoint(settings)
    return (aux_url.rstrip("/"), aux_model) != (settings.llm_base_url.rstrip("/"), settings.llm_model)


def keep_alive_value(raw: str | None) -> str | int:
    text = (raw or "").strip()
    if not text:
        return -1
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def aux_runtime_payload(settings: Settings) -> dict[str, Any]:
    """ollama extras so the helper stays on GPU with a bounded KV cache."""
    return {
        "keep_alive": keep_alive_value(settings.aux_llm_keep_alive),
        "options": {"num_ctx": settings.aux_llm_num_ctx, "num_gpu": 99},
    }


def ollama_native_root(base_url: str) -> str | None:
    stripped = base_url.rstrip("/")
    if stripped.endswith("/v1"):
        stripped = stripped[: -len("/v1")].rstrip("/")
    if stripped.endswith(":11434"):
        return stripped
    return None


class RoutingLLMClient:
    """Send complete_json to the aux client and complete_chat* to the character-model client."""

    def __init__(self, main: LLMClient, aux: LLMClient) -> None:
        self._main = main
        self._aux = aux

    def complete_json(
        self, *, system: str, user: str, max_tokens: int | None = None
    ) -> dict[str, Any]:
        return self._aux.complete_json(system=system, user=user, max_tokens=max_tokens)

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str:
        return self._main.complete_chat(messages, temperature=temperature, stream=stream)

    def complete_chat_result(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> ChatResult:
        return self._main.complete_chat_result(messages, temperature=temperature, stream=stream)

    def close(self) -> None:
        if self._aux is not self._main:
            closer = getattr(self._aux, "close", None)
            if callable(closer):
                closer()
        closer = getattr(self._main, "close", None)
        if callable(closer):
            closer()


def build_llm_client(settings: Settings) -> LLMClient:
    """Live-mode client: route helpers to aux when configured, otherwise a single endpoint."""
    main = OpenAICompatClient(settings)
    if not (aux_explicitly_configured(settings) or aux_endpoint_distinct(settings)):
        return main
    if not aux_endpoint_distinct(settings):
        return RoutingLLMClient(main, main)
    base_url, model, api_key = resolve_aux_endpoint(settings)
    aux_settings = settings.model_copy(
        update={"llm_base_url": base_url, "llm_model": model, "llm_api_key": api_key}
    )
    return RoutingLLMClient(
        main, OpenAICompatClient(aux_settings, extra_payload=aux_runtime_payload(settings))
    )


def probe_endpoint(base_url: str, api_key: str, *, timeout: float = 0.8) -> bool:
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers)
            return 200 <= response.status_code < 400
    except httpx.HTTPError:
        return False


def _model_ids_from_openai(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        mid = item.get("id") or item.get("name")
        if isinstance(mid, str) and mid.strip():
            names.append(mid.strip())
    return names


def _model_ids_from_ollama_tags(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    names: list[str] = []
    for item in models:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        mid = item.get("name") or item.get("model")
        if isinstance(mid, str) and mid.strip():
            names.append(mid.strip())
    return names


def list_endpoint_models(base_url: str, api_key: str = "not-needed", *, timeout: float = 2.0) -> list[str]:
    """Return installed model ids from OpenAI-compat `/models` or Ollama `/api/tags`."""
    url = (base_url or "").strip()
    if not url:
        return []
    headers = {"Authorization": f"Bearer {api_key or 'not-needed'}"}
    names: list[str] = []
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{url.rstrip('/')}/models", headers=headers)
            if 200 <= response.status_code < 400:
                try:
                    names.extend(_model_ids_from_openai(response.json()))
                except ValueError:
                    names = []
            if not names:
                native = ollama_native_root(url)
                if native:
                    tags = client.get(f"{native}/api/tags")
                    if 200 <= tags.status_code < 400:
                        try:
                            names.extend(_model_ids_from_ollama_tags(tags.json()))
                        except ValueError:
                            names = []
    except httpx.HTTPError:
        return []
    return list(dict.fromkeys(names))


def preload_aux_model(settings: Settings, *, timeout: float = 120.0) -> bool:
    """Issue a 1-token chat call so ollama/llama.cpp load the aux model onto GPU."""
    base_url, model, api_key = resolve_aux_endpoint(settings)
    extras = aux_runtime_payload(settings)
    headers = {"Authorization": f"Bearer {api_key}"}
    native_root = ollama_native_root(base_url)
    try:
        with httpx.Client(timeout=timeout) as client:
            if native_root:
                response = client.post(
                    f"{native_root}/api/chat",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "stream": False,
                        "keep_alive": extras["keep_alive"],
                        "options": {**extras["options"], "num_predict": 1},
                    },
                )
            else:
                response = client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        **extras,
                    },
                    headers=headers,
                )
            response.raise_for_status()
        return True
    except (httpx.HTTPError, OSError, TimeoutError, ValueError) as exc:
        logger.warning("aux model preload failed for %s @ %s: %s", model, base_url, exc)
        return False


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

    def complete_json(
        self, *, system: str, user: str, max_tokens: int | None = None
    ) -> dict[str, Any]:
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
                "channels": [],
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
        return self.complete_chat_result(messages, temperature=temperature, stream=stream).text

    def complete_chat_result(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> ChatResult:
        if stream:
            raise NotImplementedError("streaming is reserved for a later milestone")
        self.chat_calls.append(messages)
        blob = "\n".join(message.get("content", "") for message in messages)
        for key, value in self.chat_routes.items():
            if key in blob:
                text = value
                break
        else:
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
                text = REVISED_IN_CHARACTER_REPLY
            elif self.chat_script:
                idx = min(self._script_i, len(self.chat_script) - 1)
                self._script_i += 1
                text = self.chat_script[idx]
            elif "不会再管" in blob or "昨天不是说" in blob:
                text = VIOLATING_CARE_REPLY
            elif any(token in blob for token in ("早上好", "晚上好", "你在干嘛", "一起吃饭")):
                text = GREETING_REPLY
            else:
                text = DEFAULT_REPLY
        return ChatResult(text=text, usage=estimate_chat_usage(messages, text))
