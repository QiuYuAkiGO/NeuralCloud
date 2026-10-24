"""Strict reconstruction of one streamed assistant/tool response."""
from __future__ import annotations

import json


class NativeStream:
    def __init__(self):
        self.body = {}
        self.message = {"content": "", "reasoning_content": ""}
        self.calls = {}
        self.finish = None
        self.done = False
        self.characters = 0

    def line(self, line):
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if data == "[DONE]":
            self.done = True
            return
        chunk = json.loads(data)
        if not isinstance(chunk, dict) or "error" in chunk:
            raise ValueError("agentic_stream_provider_error")
        for key in ("id", "model", "usage"):
            if chunk.get(key) is not None:
                self.body[key] = chunk[key]
        choices = chunk.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("agentic_stream_choices_invalid")
        for choice in choices:
            if choice.get("index", 0) != 0:
                raise ValueError("agentic_stream_choices_invalid")
            if choice.get("finish_reason") is not None:
                self.finish = choice["finish_reason"]
            delta = choice.get("delta", {})
            for key in ("content", "reasoning_content", "refusal"):
                if delta.get(key) is not None:
                    self._append(self.message, key, delta[key])
            for update in delta.get("tool_calls", []):
                index = update.get("index")
                if type(index) is not int or not 0 <= index < 8:
                    raise ValueError("agentic_stream_tool_index_invalid")
                call = self.calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if update.get("type", "function") != "function":
                    raise ValueError("agentic_stream_tool_type_invalid")
                if update.get("id"):
                    if call["id"] and call["id"] != update["id"]:
                        raise ValueError("agentic_stream_tool_id_changed")
                    call["id"] = update["id"]
                for key in ("name", "arguments"):
                    value = update.get("function", {}).get(key)
                    if value is not None:
                        self._append(call["function"], key, value)

    def _append(self, target, key, value):
        if not isinstance(value, str):
            raise ValueError("agentic_stream_text_invalid")
        self.characters += len(value)
        if self.characters > 200_000:
            raise ValueError("agentic_stream_response_exceeds_limit")
        target[key] = target.get(key, "") + value

    def result(self):
        if not self.done or self.finish is None:
            raise ValueError("agentic_stream_incomplete")
        if self.calls:
            if sorted(self.calls) != list(range(len(self.calls))):
                raise ValueError("agentic_stream_tool_index_gap")
            self.message["tool_calls"] = [self.calls[index] for index in sorted(self.calls)]
        return {**self.body, "choices": [{"index": 0, "finish_reason": self.finish, "message": self.message}]}
