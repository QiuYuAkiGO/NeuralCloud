"""Exact Qwen counts must improve routing without widening any request budget."""

from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import pytest

from harness.config import Settings
from harness.llm import client as client_module
from harness.llm import validation_tokens as module
from harness.llm.client import ChatResult, RoutingLLMClient


@pytest.fixture(autouse=True)
def isolated_tokenizer_cache():
    module._load_qwen_tokenizer.cache_clear()
    yield
    module._load_qwen_tokenizer.cache_clear()


class _Client:
    def __init__(self, model, *, context=8192):
        self.settings = Settings(llm_model=model, aux_llm_num_ctx=context)
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return {"verdict": "pass"}

    def complete_chat_result(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return ChatResult(text='{"verdict": "pass"}')


def _routed(*, model=module.QWEN_VALIDATOR_MODEL, context=8192):
    main = _Client("other-main")
    aux = _Client(model, context=context)
    return RoutingLLMClient(main, aux), main, aux


@pytest.mark.parametrize("model", [None, "other-model", "qwen3", "qwen3:4b", "Qwen/Qwen3-4B-Instruct-2507",
                                  "qwen3:4b-instruct-2507-q8_0"])
def test_other_models_never_load_the_qwen_tokenizer(monkeypatch, model):
    def unexpected():
        pytest.fail("an unverified model requested the Qwen tokenizer")

    monkeypatch.setattr(module, "_load_qwen_tokenizer", unexpected)
    assert module.count_validation_prompt_tokens(model=model, system="critic", user="input") is None


@pytest.mark.parametrize(("system", "user", "expected"), [
    ("", "", 13),
    ("You are a critic.", '{"text": "你好，世界。", "evidence": ["🙂 café é"]}', 36),
    ("critic", "山" * 3000, 3015),
])
def test_pinned_official_tokenizer_and_two_message_template_counts(system, user, expected):
    pytest.importorskip("tokenizers")
    # Fixed values were independently obtained with the official Jinja template,
    # including the assistant generation marker and no extra BOS/thinking text.
    assert module.count_validation_prompt_tokens(model=module.QWEN_VALIDATOR_MODEL,
                                                system=system, user=user) == expected


def test_missing_optional_dependency_keeps_the_byte_bound(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "tokenizers", None)
    routed, main, aux = _routed()
    assert routed.complete_validation_json(system="critic", user="山" * 3000) == {"verdict": "pass"}
    assert len(main.calls) == 1 and not aux.calls
    assert "using byte bound" in caplog.text


@pytest.mark.parametrize("failure", ["checksum", "parse", "missing_asset"])
def test_invalid_assets_are_logged_and_cannot_enable_exact_routing(monkeypatch, caplog, failure):
    class Asset:
        def joinpath(self, name):
            return self

        def read_bytes(self):
            if failure == "missing_asset":
                raise FileNotFoundError()
            return b"{}"

    def parse(content):
        if failure == "checksum":
            pytest.fail("a tokenizer with a wrong hash was parsed")
        raise ValueError("invalid tokenizer")

    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=SimpleNamespace(from_str=parse)))
    monkeypatch.setattr(module, "files", lambda package: Asset())
    if failure == "parse":
        monkeypatch.setattr(module, "_TOKENIZER_SHA256", hashlib.sha256(b"{}").hexdigest())
    routed, main, aux = _routed()
    routed.complete_validation_json(system="critic", user="山" * 3000)
    assert len(main.calls) == 1 and not aux.calls
    assert "using byte bound" in caplog.text


def test_tokenizer_is_cached_and_does_not_reload_on_each_validation(monkeypatch):
    pytest.importorskip("tokenizers")
    assert module.count_validation_prompt_tokens(model=module.QWEN_VALIDATOR_MODEL, system="", user="") == 13

    def unexpected(package):
        pytest.fail("cached tokenizer was read from disk again")

    monkeypatch.setattr(module, "files", unexpected)
    assert module.count_validation_prompt_tokens(model=module.QWEN_VALIDATOR_MODEL, system="", user="") == 13


def test_chinese_that_fits_qwen_tokens_uses_aux_despite_larger_utf8_byte_count():
    pytest.importorskip("tokenizers")
    user = "山" * 3000
    assert len(user.encode("utf-8")) + 256 + 1024 > 8192
    routed, main, aux = _routed()
    assert routed.complete_validation_json(system="critic", user=user) == {"verdict": "pass"}
    assert not main.calls and len(aux.calls) == 1
    assert aux.calls[0] == {"system": "critic", "user": user, "max_tokens": 1024}


@pytest.mark.parametrize("remaining_capacity", [0, -1])
def test_exact_counts_keep_template_and_output_reserves_at_the_boundary(remaining_capacity):
    pytest.importorskip("tokenizers")
    required = 3015 + 256 + 1024
    routed, main, aux = _routed(context=required + remaining_capacity)
    routed.complete_validation_json(system="critic", user="山" * 3000)
    assert len(aux.calls) == (1 if remaining_capacity == 0 else 0)
    assert len(main.calls) == (0 if remaining_capacity == 0 else 1)


def test_other_models_keep_the_original_byte_based_routing():
    routed, main, aux = _routed(model="different-small-model")
    routed.complete_validation_json(system="critic", user="山" * 3000)
    assert len(main.calls) == 1 and not aux.calls


@pytest.mark.parametrize("timeout_seconds", [1.0, 0.1])
def test_counting_time_is_deducted_before_any_http_call(monkeypatch, timeout_seconds):
    monkeypatch.setattr(module, "count_validation_prompt_tokens", lambda **kwargs: 13)
    clock = iter([10.0, 10.25])
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    routed, main, aux = _routed()
    if timeout_seconds > .25:
        routed.complete_validation_json(system="critic", user="input", timeout_seconds=timeout_seconds)
        assert aux.calls[0]["timeout_seconds"] == .75
    else:
        with pytest.raises(TimeoutError, match="counting exceeded"):
            routed.complete_validation_json(system="critic", user="input", timeout_seconds=timeout_seconds)
        assert not aux.calls
    assert not main.calls
