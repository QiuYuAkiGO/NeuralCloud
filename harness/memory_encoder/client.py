"""Explicit opt-in client; no changes to production model routing."""
from __future__ import annotations

import httpx
from .contracts import ChatRequest, MemoryQuery


class MemoryChatClient:
    def __init__(self, base_url, *, api_key, provider="neuralcloud", timeout=120):
        self.provider = provider
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                 headers={"Authorization": "Bearer " + api_key})

    def chat(self, messages, *, query=None, limit=8, max_new_tokens=256):
        if query is None:
            query = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        request = ChatRequest(messages=messages,
            memory=MemoryQuery(provider=self.provider, query=query, limit=limit), max_new_tokens=max_new_tokens)
        response = self.http.post("/v1/memory/chat", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return response.json()

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
