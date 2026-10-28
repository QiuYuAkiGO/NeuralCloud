"""Loopback-only metered transport; credentials never enter experiment artifacts."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import time

import httpx


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(canonical(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_key(path):
    values = set(re.findall(r"\bsk-[A-Za-z0-9_-]+", Path(path).read_text(encoding="utf-8-sig")))
    if len(values) != 1:
        raise ValueError("credential_file_must_contain_exactly_one_key")
    return values.pop()


def stored_response(body):
    body = deepcopy(body)
    for choice in body.get("choices", []):
        for key in ("reasoning", "reasoning_content", "reasoning_details"):
            choice.get("message", {}).pop(key, None)
    for row in body.get("data", []):
        if "embedding" in row:
            embedding = row.pop("embedding")
            row["embedding_dimensions"] = len(embedding) if isinstance(embedding, list) else None
            row["embedding_sha256"] = hashlib.sha256(canonical(embedding).encode()).hexdigest()
    return body


class Gateway:
    def __init__(self, root, repo, *, max_calls=800, max_total_tokens=1_500_000):
        self.root, self.repo = Path(root), Path(repo)
        self.lock = threading.Lock()
        self.calls = 0
        self.total_tokens = 0
        self.events = []
        self.blocked = False
        self.max_calls, self.max_total_tokens = max_calls, max_total_tokens
        self.routes = {
            "/writer/chat/completions": ("https://api.deepseek.com/v1/chat/completions", load_key(self.repo / "Deepseek-api-key-affect.txt")),
            "/embedding/embeddings": ("https://api.apiyi.com/v1/embeddings", load_key(self.repo / "apiyi-api-key.txt")),
            "/judge/chat/completions": ("https://api.moonshot.cn/v1/chat/completions", load_key(self.repo / "kimi-official-api-key.txt")),
        }
        self.client = httpx.Client(timeout=120, trust_env=False)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.path not in owner.routes:
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2_000_000:
                    self.send_error(413)
                    return
                with owner.lock:
                    if owner.blocked or owner.calls >= owner.max_calls or owner.total_tokens >= owner.max_total_tokens:
                        self.send_error(429, "experiment_request_limit")
                        return
                    owner.calls += 1
                    call_id = owner.calls
                payload = json.loads(self.rfile.read(length))
                phase = self.headers.get("X-Eval-Phase", "hy-memory")
                if self.path == "/writer/chat/completions":
                    payload.update(model="deepseek-flash", thinking={"type": "disabled"}, stream=False)
                    payload["temperature"] = 0.1 if phase in {"write", "hy-memory"} else 0
                    payload["max_tokens"] = 4096 if phase in {"write", "hy-memory"} else 512
                elif self.path == "/judge/chat/completions":
                    payload.update(model="kimi-k3", temperature=0.6, thinking={"type": "disabled"}, stream=False, max_tokens=3072)
                url, key = owner.routes[self.path]
                event = {"id": call_id, "case": self.headers.get("X-Eval-Case"),
                         "arm": self.headers.get("X-Eval-Arm"), "phase": phase,
                         "started_at": datetime.now(timezone.utc).isoformat(), "endpoint": url, "request": payload}
                start = time.monotonic()
                try:
                    response = owner.client.post(url, json=payload, headers={"Authorization": "Bearer " + key})
                    status = response.status_code
                    try:
                        body = response.json()
                    except ValueError:
                        body = {"error": {"type": "non_json_upstream_response"}}
                    event.update(status=status, response=stored_response(body))
                    with owner.lock:
                        owner.total_tokens += (body.get("usage") or {}).get("total_tokens", 0)
                        if status in {401, 402, 403}:
                            owner.blocked = True
                except Exception as exc:
                    status, body = 502, {"error": {"type": type(exc).__name__}}
                    event.update(status=status, response=body)
                event["seconds"] = time.monotonic() - start
                save(owner.root / "calls" / f"{call_id:05d}.json", event)
                with owner.lock:
                    owner.events.append({k: event.get(k) for k in ("id", "case", "arm", "phase", "status")})
                output = canonical(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(output)))
                self.end_headers()
                try:
                    self.wfile.write(output)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def errors_since(self, marker, case, arm):
        with self.lock:
            return [e for e in self.events if e["id"] > marker and e["case"] == case and e["arm"] == arm and e["status"] != 200]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.client.close()


class Chat:
    def __init__(self, gateway, *, case, arm, phase, judge=False):
        self.url = gateway.base + ("/judge/chat/completions" if judge else "/writer/chat/completions")
        self.headers = {"X-Eval-Case": case, "X-Eval-Arm": arm, "X-Eval-Phase": phase}
        self.last = None

    def call(self, messages, *, json_mode=False):
        payload = {"model": "transport-selected", "messages": messages}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        with httpx.Client(timeout=135, trust_env=False) as client:
            response = client.post(self.url, json=payload, headers=self.headers)
        response.raise_for_status()
        self.last = response.json()
        choice = self.last["choices"][0]
        if choice["finish_reason"] != "stop" or choice["message"].get("tool_calls") or choice["message"].get("refusal"):
            raise ValueError("generation_not_completed")
        text = choice["message"].get("content")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("generation_empty")
        return text

    def complete_chat_result(self, messages, **_kwargs):
        from types import SimpleNamespace
        text = self.call(messages, json_mode=True)
        return SimpleNamespace(text=text, finish_reason="stop", content_type="text", has_tool_calls=False, has_refusal=False)
