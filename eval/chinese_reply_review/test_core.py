"""Offline acceptance tests: all HTTP traffic stays inside httpx.MockTransport."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import httpx
import pytest

from eval.chinese_reply_review import core


PROJECT = Path(__file__).resolve().parents[2]
FAKE_KEY = "offline-fixture-key-never-a-real-credential"
REASONING = "PRIVATE_REASONING_CANARY_MUST_NOT_BE_STORED"
ERROR_BODY = "UNTRUSTED_ERROR_BODY_CANARY_MUST_NOT_BE_STORED"


def model(model_id="mock-chat", provider="qwen", protocol="chat"):
    paths = {"chat": "/chat/completions", "responses": "/responses", "anthropic": "/messages"}
    caps = {"chat": "max_tokens", "responses": "max_output_tokens", "anthropic": "max_tokens"}
    return {
        "id": model_id,
        "provider": provider,
        "protocol": protocol,
        "path": paths[protocol],
        "options": {caps[protocol]: 8192},
        "reasoning_policy": "offline fixture; no thinking",
        "sources": [{"url": "https://example.invalid/offline", "kind": "test"}],
        "selection_reason": "Offline test fixture, never a live candidate.",
    }


def prepare(tmp_path, models=None):
    run_dir = tmp_path / "run"
    key_file = tmp_path / "dummy-key.txt"
    key_file.write_text(FAKE_KEY, encoding="utf-8")
    manifest = core.prepare_run(run_dir, PROJECT, models=models or [model()])
    return run_dir, key_file, manifest


def frozen_requests(run_dir):
    return json.loads((run_dir / "private" / "requests.json").read_text(encoding="utf-8"))


def ledger(run_dir, manifest):
    return core.Ledger(run_dir / "private" / "ledger.sqlite3", manifest)


def preflight(run_dir, key_file, models):
    def handle(request):
        assert request.method == "GET"
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": item["id"]} for item in models]})

    return core.preflight(run_dir, key_file, transport=httpx.MockTransport(handle))


def reply(protocol, model_id, *, text="我在听，你可以慢慢说。", truncated=False):
    if protocol == "chat":
        return {
            "model": model_id,
            "choices": [{"message": {"content": text, "reasoning_content": REASONING},
                         "finish_reason": "length" if truncated else "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 9, "total_tokens": 20},
        }
    if protocol == "responses":
        return {
            "model": model_id,
            "status": "incomplete" if truncated else "completed",
            "incomplete_details": {"reason": "max_output_tokens"} if truncated else None,
            "output": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": REASONING}]},
                       {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 11, "output_tokens": 9, "total_tokens": 20},
        }
    return {
        "model": model_id,
        "content": [{"type": "thinking", "thinking": REASONING}, {"type": "text", "text": text}],
        "stop_reason": "max_tokens" if truncated else "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 9},
    }


def persisted_bytes(run_dir):
    return b"\n".join(path.read_bytes() for path in run_dir.rglob("*") if path.is_file())


def test_preflight_is_get_only_and_reads_only_the_explicit_key(tmp_path, monkeypatch):
    run_dir, key_file, _ = prepare(tmp_path)
    monkeypatch.setenv("APIYI_API_KEY", "wrong-environment-key")
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer " + FAKE_KEY
        return httpx.Response(200, json={"data": [{"id": "mock-chat"}]})

    core.preflight(run_dir, key_file, transport=httpx.MockTransport(handle))
    assert len(requests) == 1
    assert FAKE_KEY.encode() not in persisted_bytes(run_dir)
    assert b"wrong-environment-key" not in persisted_bytes(run_dir)


def test_catalogue_payload_cannot_persist_an_echoed_key(tmp_path):
    run_dir, key_file, _ = prepare(tmp_path)
    core.preflight(run_dir, key_file, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "mock-chat"}, {"id": FAKE_KEY}]})))
    assert FAKE_KEY.encode() not in persisted_bytes(run_dir)


def test_all_protocols_extract_only_visible_text_and_second_run_sends_nothing(tmp_path):
    models = [model(), model("mock-responses", "openai", "responses"),
              model("mock-anthropic", "anthropic", "anthropic")]
    run_dir, key_file, manifest = prepare(tmp_path, models)
    preflight(run_dir, key_file, models)
    calls = []
    specs = {item["id"]: item for item in models}

    def handle(request):
        assert request.method == "POST"
        payload = json.loads(request.content)
        selected = specs[payload["model"]]
        assert request.url.path == "/v1" + selected["path"]
        assert payload.get("stream", False) is False
        assert payload.get("n", 1) == 1
        assert request.headers.get("authorization") == "Bearer " + FAKE_KEY or request.headers.get("x-api-key") == FAKE_KEY
        calls.append(payload["model"])
        return httpx.Response(200, json=reply(selected["protocol"], payload["model"]))

    transport = httpx.MockTransport(handle)
    core.run_samples(run_dir, key_file, transport=transport)
    assert Counter(calls) == {item["id"]: 2 for item in models}
    core.run_samples(run_dir, key_file, transport=transport)
    assert len(calls) == 6
    rows = ledger(run_dir, manifest).rows()
    assert len(rows) == 6
    assert all(row["status"] == "complete" and row["response_text"] == "我在听，你可以慢慢说。" for row in rows)
    stored = persisted_bytes(run_dir)
    assert "我在听，你可以慢慢说。".encode() in stored
    assert REASONING.encode() not in stored
    assert FAKE_KEY.encode() not in stored


@pytest.mark.parametrize("failure", ["http_error", "timeout", "redirect"])
def test_errors_are_counted_once_without_retry_or_redirect_and_error_body_is_not_saved(tmp_path, failure):
    models = [model()]
    run_dir, key_file, manifest = prepare(tmp_path, models)
    preflight(run_dir, key_file, models)
    calls = []

    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(ERROR_BODY + FAKE_KEY, request=request)
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://must-not-follow.invalid/"}, text=ERROR_BODY + FAKE_KEY)
        return httpx.Response(500, text=ERROR_BODY + FAKE_KEY)

    transport = httpx.MockTransport(handle)
    core.run_samples(run_dir, key_file, transport=transport)
    core.run_samples(run_dir, key_file, transport=transport)
    assert len(calls) == 2
    assert len(ledger(run_dir, manifest).rows()) == 2
    assert ERROR_BODY.encode() not in persisted_bytes(run_dir)
    assert FAKE_KEY.encode() not in persisted_bytes(run_dir)


def test_crash_after_send_is_never_replayed(tmp_path):
    models = [model()]
    run_dir, key_file, manifest = prepare(tmp_path, models)
    preflight(run_dir, key_file, models)
    calls = []

    def crash(request):
        calls.append(json.loads(request.content))
        raise KeyboardInterrupt("Simulated process interruption after a possibly accepted request")

    with pytest.raises(KeyboardInterrupt):
        core.run_samples(run_dir, key_file, transport=httpx.MockTransport(crash))
    assert len(ledger(run_dir, manifest).rows()) == 1

    def resume(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=reply("chat", "mock-chat"))

    core.run_samples(run_dir, key_file, transport=httpx.MockTransport(resume))
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert len(ledger(run_dir, manifest).rows()) == 2


def test_atomic_reservation_refuses_duplicate_slot_and_unknown_third_scenario(tmp_path):
    run_dir, _, manifest = prepare(tmp_path)
    req = frozen_requests(run_dir)[0]

    def reserve(_):
        return ledger(run_dir, manifest).reserve(req["model_id"], req["scenario_id"], req["request_sha256"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(16)))
    assert results.count(True) == 1
    assert results.count(False) == 15
    with pytest.raises((ValueError, RuntimeError)):
        ledger(run_dir, manifest).reserve(req["model_id"], "third-unapproved-scenario", req["request_sha256"])
    assert len(ledger(run_dir, manifest).rows()) == 1


def test_changed_candidate_list_cannot_overwrite_existing_freeze(tmp_path):
    run_dir, _, _ = prepare(tmp_path)
    before = (run_dir / "private" / "requests.json").read_bytes()
    with pytest.raises((ValueError, RuntimeError, FileExistsError)):
        core.prepare_run(run_dir, PROJECT, models=[model("replacement")])
    assert (run_dir / "private" / "requests.json").read_bytes() == before


@pytest.mark.parametrize("mutated_file", ["manifest.json", "private/requests.json"])
def test_tampered_frozen_inputs_block_generation_before_http(tmp_path, mutated_file):
    models = [model()]
    run_dir, key_file, _ = prepare(tmp_path, models)
    preflight(run_dir, key_file, models)
    path = run_dir / mutated_file
    text = path.read_text(encoding="utf-8")
    assert "mock-chat" in text
    path.write_text(text.replace("mock-chat", "tampered-model"), encoding="utf-8")

    def forbidden(request):
        pytest.fail("Tampered requests must be rejected before any HTTP call")

    with pytest.raises((ValueError, RuntimeError)):
        core.run_samples(run_dir, key_file, transport=httpx.MockTransport(forbidden))


def test_missing_preflight_or_catalogue_membership_blocks_generation(tmp_path):
    run_dir, key_file, _ = prepare(tmp_path)

    def forbidden(request):
        pytest.fail("Unverified candidates must not be called")

    with pytest.raises((ValueError, RuntimeError, FileNotFoundError)):
        core.run_samples(run_dir, key_file, transport=httpx.MockTransport(forbidden))
    core.preflight(run_dir, key_file, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "different-model"}]})))
    with pytest.raises(ValueError, match="preflight"):
        core.run_samples(run_dir, key_file, transport=httpx.MockTransport(forbidden))


@pytest.mark.parametrize("models", [
    [model(f"too-many-{i}") for i in range(4)],
    [model(f"too-many-{i}", f"provider-{i}") for i in range(22)],
    [model(), model()],
])
def test_freeze_rejects_provider_limit_global_limit_and_duplicate_models(tmp_path, models):
    with pytest.raises((ValueError, RuntimeError)):
        prepare(tmp_path, models)


def test_all_models_receive_identical_persona_and_scene_text_across_protocols(tmp_path):
    models = [model(), model("mock-responses", "openai", "responses"),
              model("mock-anthropic", "anthropic", "anthropic")]
    run_dir, _, manifest = prepare(tmp_path, models)
    assert manifest["persona"] == json.loads((PROJECT / "presets/builtin/misty.json").read_text(encoding="utf-8"))["persona"]
    normalized = {}
    for item in frozen_requests(run_dir):
        payload = item["payload"]
        if item["protocol"] == "chat":
            assert [message["role"] for message in payload["messages"]] == ["system", "user"]
            value = payload["messages"][0]["content"], payload["messages"][1]["content"]
        elif item["protocol"] == "anthropic":
            assert [message["role"] for message in payload["messages"]] == ["user"]
            value = payload["system"], payload["messages"][0]["content"]
        else:
            assert [message["role"] for message in payload["input"]] == ["user"]
            value = payload["instructions"], payload["input"][0]["content"]
        normalized.setdefault(item["scenario_id"], set()).add(value)
    assert len(normalized) == 2
    assert all(len(variants) == 1 for variants in normalized.values())
    emotion = next(iter(normalized["s08_strong_emotion"]))
    stance = next(iter(normalized["s04_stance_followups"]))
    assert emotion[1] == "@Misty 我努力准备了很久的比赛，结果第一轮就输了，真的很难受。"
    assert stance[1] == "@Misty 你更看重长寿还是活得有意思？先说你选哪边。"
    assert "20–60" not in emotion[0]
    assert "20–60" in stance[0] and "软目标" in stance[0]
    assert "先别给建议" not in emotion[0]


def test_full_capacity_is_exactly_42_sends_and_never_a_43rd(tmp_path):
    providers = ("openai", "anthropic", "deepseek", "qwen", "kimi", "glm", "minimax")
    models = [model(f"fixture-{provider}-{i}", provider) for provider in providers for i in range(3)]
    run_dir, key_file, manifest = prepare(tmp_path, models)
    assert manifest["limits"] == {"per_model": 2, "global": 42, "planned": 42}
    preflight(run_dir, key_file, models)
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload["model"])
        return httpx.Response(200, json=reply("chat", payload["model"]))

    transport = httpx.MockTransport(handle)
    first = core.run_samples(run_dir, key_file, transport=transport)
    second = core.run_samples(run_dir, key_file, transport=transport)
    assert first["attempted"] == second["attempted"] == 42
    assert first["remaining"] == second["remaining"] == 0
    assert first["human_review_status"] == "waiting_for_human"
    assert len(calls) == 42 and set(Counter(calls).values()) == {2}


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
def test_empty_and_truncated_replies_remain_technical_missingness(protocol):
    assert core.parse_response(reply(protocol, "fixture", text=" "), protocol)["status"] == "empty"
    partial = core.parse_response(reply(protocol, "fixture", text="还没说完", truncated=True), protocol)
    assert partial["status"] == "truncated"
    assert partial["response_text"] == "还没说完"


def test_inline_thinking_and_untrusted_usage_are_not_exported():
    raw = reply("chat", "fixture", text=f"<think>{REASONING}</think>我在听。")
    raw["usage"] = {"prompt_tokens": 11, "completion_tokens": 9, "cost": 0.001,
                    "secret": REASONING, "reasoning_tokens": REASONING,
                    "completion_tokens_details": {"reasoning_tokens": 2, "analysis": REASONING}}
    parsed = core.parse_response(raw, "chat")
    assert parsed["response_text"] == "我在听。" and parsed["status"] == "complete"
    assert REASONING not in json.dumps(parsed)
    assert parsed["usage"]["cost"] == 0.001
    assert parsed["usage"]["completion_tokens_details"] == {"reasoning_tokens": 2}
    raw["choices"][0]["message"]["content"] = f"<think>{REASONING}"
    parsed = core.parse_response(raw, "chat")
    assert parsed["status"] == "empty" and parsed["response_text"] == ""


def test_key_echo_in_successful_response_is_redacted_everywhere(tmp_path):
    models = [model()]
    run_dir, key_file, _ = prepare(tmp_path, models)
    preflight(run_dir, key_file, models)
    payload = reply("chat", FAKE_KEY, text="代理回显：" + FAKE_KEY)
    payload["id"] = FAKE_KEY
    core.run_samples(run_dir, key_file, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    stored = persisted_bytes(run_dir)
    assert FAKE_KEY.encode() not in stored and b"[REDACTED]" in stored


def test_changed_key_requires_a_new_catalogue_check_before_generation(tmp_path):
    models = [model()]
    run_dir, key_file, _ = prepare(tmp_path, models)
    preflight(run_dir, key_file, models)
    key_file.write_text("another-offline-key", encoding="utf-8")

    def forbidden(request):
        pytest.fail("Changed credentials must be rechecked before any generation request")

    with pytest.raises(ValueError, match="credentials_changed"):
        core.run_samples(run_dir, key_file, transport=httpx.MockTransport(forbidden))


def test_lock_rejects_overlapping_operations_and_releases_after_exit(tmp_path):
    run_dir, _, _ = prepare(tmp_path)
    with core.run_lock(run_dir):
        with pytest.raises(ValueError, match="another_review_operation"):
            with core.run_lock(run_dir):
                pytest.fail("The second operation must not enter the sampling region")
    with core.run_lock(run_dir):
        pass


def test_finished_attempts_cannot_be_rewritten_and_hashes_cannot_be_swapped(tmp_path):
    run_dir, _, manifest = prepare(tmp_path)
    attempt = frozen_requests(run_dir)[0]
    db = ledger(run_dir, manifest)
    with pytest.raises(ValueError, match="not_in_frozen_plan"):
        db.reserve(attempt["model_id"], attempt["scenario_id"], "incorrect-hash")
    assert db.rows() == []
    assert db.reserve(attempt["model_id"], attempt["scenario_id"], attempt["request_sha256"])
    db.finish(attempt["model_id"], attempt["scenario_id"], {"status": "complete", "response_text": "原始回复"})
    with pytest.raises(ValueError, match="immutable"):
        db.finish(attempt["model_id"], attempt["scenario_id"], {"status": "complete", "response_text": "修改后回复"})
    assert db.rows()[0]["response_text"] == "原始回复"
