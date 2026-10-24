"""Pinned official DeepSeek Flash encoding for cloud Agent context accounting.

Reference: deepseek-ai/DeepSeek-V4.1-Flash-1017, revision
1bae0e1088993b9b82de08f59de5be5a34e42170. Assets never load from the network
at runtime. Keep provider usage as the independent live acceptance check.
"""
from copy import deepcopy
from functools import lru_cache
import hashlib
from pathlib import Path
from urllib.parse import urlsplit


REVISION = "1bae0e1088993b9b82de08f59de5be5a34e42170"
TOKENIZER_SHA256 = "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b"
ENCODER_SHA256 = "13231c592e958fb39e2af6001dae69d90e7a36b265702b9b390236fb387599e2"
PROVIDER_CONTEXT_TOKENS = 1_048_576


def supported(settings):
    """Match the explicit official route; never send a key to lookalike hosts."""
    try:
        url = urlsplit(settings.llm_base_url)
        return (url.scheme == "https" and url.hostname == "api.deepseek.com"
                and url.port in (None, 443) and not url.username and not url.password
                and not url.query and not url.fragment and url.path.rstrip("/") in ("", "/v1")
                and settings.llm_model in {"deepseek-flash", "deepseek-v4-flash",
                                           "deepseek-v4-flash-vision-exp"})
    except ValueError:
        return False


def native_model_supported(settings):
    # Local-only remains a real boundary. Cloud use requires an explicit opt-in.
    from harness.agentic.context import local_endpoint
    return (settings.agentic_enabled and
            ((settings.agentic_local_only and local_endpoint(settings.llm_base_url))
             or (not settings.agentic_local_only and supported(settings))))


@lru_cache(maxsize=1)
def _assets():
    from tokenizers import Tokenizer
    folder = Path(__file__).resolve().parent
    content = (folder.parent / "llm/data/deepseek-v4.1-flash-1017-tokenizer.json").read_bytes()
    encoder = (folder / "_deepseek_encoding.py").read_bytes()
    if hashlib.sha256(content).hexdigest() != TOKENIZER_SHA256 or hashlib.sha256(encoder).hexdigest() != ENCODER_SHA256:
        raise ValueError("deepseek_agent_tokenizer_or_encoder_hash_mismatch")
    from harness.agentic._deepseek_encoding import encode_messages
    return Tokenizer.from_str(content.decode("utf-8")), encode_messages


class DeepSeekTokenCounter:
    def __init__(self, settings):
        if not supported(settings) or settings.agentic_local_only:
            raise ValueError("deepseek_agent_requires_explicit_cloud_route")
        if settings.agentic_tokenizer_path is not None:
            raise ValueError("deepseek_agent_requires_pinned_tokenizer")
        self.settings = settings
        self.tokenizer, self.encode_messages = _assets()

    @lru_cache(maxsize=8192)
    def text(self, value):
        return len(self.tokenizer.encode(value, add_special_tokens=False).ids)

    def prompt(self, messages, tools):
        # Images stay in the existing inspect_qq_images tool. Counting only an
        # image placeholder would not prove the capacity of a multimodal request.
        if any(not isinstance(message.get("content", ""), (str, type(None))) for message in messages):
            raise ValueError("deepseek_agent_text_context_required")
        projected = deepcopy(messages)
        if tools:
            if not projected or projected[0]["role"] != "system":
                projected.insert(0, {"role": "system", "content": ""})
            projected[0]["tools"] = deepcopy(tools)
        prompt = self.encode_messages(projected,
            thinking_mode="thinking" if self.settings.agentic_thinking else "chat",
            reasoning_effort="low" if self.settings.agentic_thinking else None)
        return self.text(prompt)

    def context_capacity(self):
        return min(self.settings.agentic_context_tokens, PROVIDER_CONTEXT_TOKENS)
