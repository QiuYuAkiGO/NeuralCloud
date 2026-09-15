"""Offline token counts for the verified two-message Qwen validator template.

Only the exact deployed model ID is supported. Other models, unavailable optional
dependencies, or invalid assets leave routing on its conservative byte bound.
This module never resolves a model or downloads a tokenizer during a request.
"""

from __future__ import annotations

from functools import lru_cache
import hashlib
from importlib.resources import files
import logging
import threading
from typing import Any


logger = logging.getLogger(__name__)

QWEN_VALIDATOR_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
_TOKENIZER_ASSET = "data/qwen3-4b-instruct-2507-tokenizer.json"
_TOKENIZER_SHA256 = "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
_LOAD_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _load_qwen_tokenizer() -> Any | None:
    """Called under _LOAD_LOCK so concurrent first requests load only once."""
    try:
        from tokenizers import Tokenizer

        content = files("harness.llm").joinpath(_TOKENIZER_ASSET).read_bytes()
        if hashlib.sha256(content).hexdigest() != _TOKENIZER_SHA256:
            raise ValueError("validator tokenizer checksum mismatch")
        # Decode the same bytes that were checked, avoiding a second file read.
        return Tokenizer.from_str(content.decode("utf-8"))
    except Exception as exc:
        logger.warning("exact validator token counting unavailable (%s); using byte bound", type(exc).__name__)
        return None


def count_validation_prompt_tokens(*, model: str | None, system: str, user: str) -> int | None:
    """Count the exact system/user prompt, including the assistant generation marker.

    The official Instruct-2507 template has no BOS or thinking prefix for these
    two text messages. Do not reuse this renderer for tools, images, assistant
    history, other Qwen variants, or arbitrary models with similar names.
    """
    if model != QWEN_VALIDATOR_MODEL:
        return None
    with _LOAD_LOCK:
        tokenizer = _load_qwen_tokenizer()
    if tokenizer is None:
        return None
    prompt = ("<|im_start|>system\n" + system + "<|im_end|>\n"
              "<|im_start|>user\n" + user + "<|im_end|>\n"
              "<|im_start|>assistant\n")
    try:
        return len(tokenizer.encode(prompt, add_special_tokens=False).ids)
    except Exception as exc:
        logger.warning("exact validator token counting failed (%s); using byte bound", type(exc).__name__)
        return None
