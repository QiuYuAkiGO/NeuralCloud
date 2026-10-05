"""Verify pixels pass through the existing non-streaming project LLM client."""
from pathlib import Path
import argparse
import json
import os
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from harness.config import Settings
from harness.llm.client import OpenAICompatClient
from tools.production_acceptance import qq_vision_reply_benchmark as bench


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--providers", nargs="+", choices=[*bench.DEFAULT_PROVIDERS, "deepseek"],
                        default=bench.DEFAULT_PROVIDERS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    output = args.out or root / "adapter-smoke.json"
    if output.exists():
        raise ValueError("smoke evidence already exists")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    direct = json.loads((root / "direct.json").read_text(encoding="utf-8"))
    case = next(c for c in manifest["cases"] if c["id"] == "c05")
    messages, images = bench.make_messages(case, root, direct["system_prompt"])
    rows = []
    for provider in args.providers:
        spec = bench.MODELS[provider]
        key_path = (Path(os.environ["LOCALAPPDATA"]) / "NeuralCloud/llama.cpp/api-key.txt"
                    if provider == "local" else Path(os.environ["LOCALAPPDATA"]) / "NeuralCloud/llama.cpp/main-fallback.json"
                    if provider == "deepseek" else Path("kimi-k3-official-api-key.txt"))
        settings = Settings(_env_file=None, mode="live", llm_base_url=spec["url"], llm_model=spec["model"],
            llm_api_key=bench.key_from_file(key_path), llm_timeout=45,
            call_log_enabled=False, model_request_log_enabled=False)
        extra = {"max_completion_tokens": 2048} if provider == "kimi3" else {}
        client = OpenAICompatClient(settings, extra_payload=extra)
        started = time.perf_counter()
        try:
            result = client.complete_chat_result(messages,
                temperature={"local": 0.7, "kimi26": 0.6, "kimi3": 1.0, "deepseek": 0.7}[provider],
                max_tokens=None if provider == "kimi3" else 512,
                reasoning_effort="low" if provider == "kimi3" else None,
                thinking={"type": "disabled"} if provider in {"kimi26", "deepseek"} else None)
            row = {"provider": provider, "case_id": case["id"], "seconds": time.perf_counter() - started,
                   "text": result.text, "usage": result.usage.model_dump(mode="json"),
                   "metadata": result.response_metadata(), "images": images,
                   "input_sha256": bench.digest(messages), "success": bool(result.text.strip())}
        except Exception as exc:
            row = {"provider": provider, "case_id": case["id"], "seconds": time.perf_counter() - started,
                   "success": False, "error_type": type(exc).__name__}
        finally:
            client.close()
        rows.append(row)
        bench.save(output, {"scope": "OpenAICompatClient smoke; no QQ or memory writes", "results": rows})
        print(json.dumps({k: row.get(k) for k in ["provider", "seconds", "success", "text"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
