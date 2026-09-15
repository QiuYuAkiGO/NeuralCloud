"""Read-only Linux hardware/LLM inventory and synthetic Ollama JSON verification.

Run locally on the SSH target with Python 3. No credentials or production
configuration files are read, and no application services are restarted.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import time
import urllib.request


MODEL = "qwen3:4b-instruct-2507-q4_K_M"


def run(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        return {"code": result.returncode, "output": result.stdout.strip()[:6000]}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": type(error).__name__}


def request(base, path, payload=None, timeout=5):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def probe():
    result = {
        "platform": platform.platform(), "arch": platform.machine(),
        "memory": run(["free", "-m"]), "disk": run(["df", "-h", "/opt", "/var/lib"]),
        "gpu": run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free,driver_version", "--format=csv,noheader"]),
        "cpu": run(["lscpu"]), "listeners": run(["ss", "-ltn"]),
        "runtimes": {name: shutil.which(name) for name in ("ollama", "llama-server", "vllm", "docker", "zstd")},
        "services": run(["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--no-pager"]),
    }
    result["services"]["output"] = "\n".join(
        line for line in result["services"].get("output", "").splitlines()
        if any(name in line.lower() for name in ("neuralcloud", "ollama", "llama", "vllm", "napcat")))
    for port in (11434, 11435):
        base = f"http://127.0.0.1:{port}"
        try:
            result[f"ollama_{port}"] = {"version": request(base, "/api/version"),
                "models": [{"name": value["name"], "size": value.get("size"), "digest": value.get("digest")}
                           for value in request(base, "/api/tags").get("models", [])]}
        except (OSError, ValueError):
            result[f"ollama_{port}"] = {"reachable": False}
    return result


def verify(base, model):
    # Synthetic content only. The native warmup fixes the 8K context before
    # timing the exact OpenAI-compatible JSON route consumed by NeuralCloud.
    native = {"model": model, "stream": False, "keep_alive": -1, "format": "json",
        "options": {"num_ctx": 8192, "num_predict": 96, "temperature": 0},
        "messages": [{"role": "system", "content": "只输出JSON对象，字段ok的值为true。"},
                     {"role": "user", "content": "连通性测试"}]}
    warm_started = time.monotonic()
    warm = request(base, "/api/chat", native, timeout=180)
    warm_seconds = time.monotonic() - warm_started
    json.loads(warm["message"]["content"])
    payload = {"model": model, "temperature": 0.1, "max_tokens": 128, "stream": False,
        "response_format": {"type": "json_object"}, "messages": [
            {"role": "system", "content": "你是情绪评估器。只输出JSON，字段label和intensity。label只能为anger或calm，intensity为0至1数字。"},
            {"role": "user", "content": "人物通常温柔。有人当面说：你最珍惜的作品一文不值，都是垃圾。判断人物此刻情绪。"}]}
    started = time.monotonic()
    response = request(base, "/v1/chat/completions", payload, timeout=30)
    seconds = time.monotonic() - started
    content = response["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    assert parsed["label"] in {"anger", "calm"} and isinstance(parsed["intensity"], (int, float))
    assert not isinstance(parsed["intensity"], bool) and 0 <= parsed["intensity"] <= 1
    assert "<think>" not in content and response["choices"][0]["finish_reason"] == "stop"
    guidance_payload = {**payload, "messages": [
        {"role": "system", "content": "只输出JSON对象，字段text。为主模型生成一句中文情绪演绎提示，不代写回复，不输出计算参数。"},
        {"role": "user", "content": json.dumps({"personality": "通常温柔", "emotion": parsed,
            "needs": "精力正常", "instruction": "允许强情绪暂时突破通常的克制，但保持事实与身份。"}, ensure_ascii=False)}]}
    guidance_started = time.monotonic()
    guidance_response = request(base, "/v1/chat/completions", guidance_payload, timeout=30)
    guidance_seconds = time.monotonic() - guidance_started
    guidance = json.loads(guidance_response["choices"][0]["message"]["content"])
    assert isinstance(guidance.get("text"), str) and guidance["text"].strip()
    assert guidance_response["choices"][0]["finish_reason"] == "stop"
    return {"model": model, "endpoint": base + "/v1", "warmup_seconds": round(warm_seconds, 3),
        "json_seconds": round(seconds, 3), "json_result": parsed, "usage": response.get("usage"),
        "guidance_seconds": round(guidance_seconds, 3), "guidance_result": guidance,
        "within_appraisal_8_seconds": seconds <= 8, "within_guidance_4_seconds": guidance_seconds <= 4,
        "loaded_models": request(base, "/api/ps"),
        "gpu": run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free", "--format=csv,noheader"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["probe", "verify"])
    parser.add_argument("--base-url", default="http://127.0.0.1:11435")
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    result = probe() if args.action == "probe" else verify(args.base_url, args.model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
