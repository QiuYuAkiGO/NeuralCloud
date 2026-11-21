"""Serve the Misty v1 sample editor on loopback."""

from __future__ import annotations

import argparse
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

ROOT = Path(__file__).resolve().parent
DATASETS = {
    "relationships": ("Chat Worker · 关系分档", "chat_worker_relationship_sft.jsonl"),
    "chat": ("Chat Worker · 通用", "chat_worker_sft.jsonl"),
    "master-materials": ("Master · 纯材料", "master_materials_sft.jsonl"),
    "master-heart": ("Master · 心声提示", "master_heart_prompt_sft.jsonl"),
}


def revision(line: bytes) -> str:
    return sha256(line).hexdigest()


def validate_edit(original, prompt, completion):
    if isinstance(original, str):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt 必须是非空文本。")
        if not isinstance(completion, str) or not completion.strip():
            raise ValueError("回复必须是非空文本。")
        return

    for name, messages, roles in (
        ("Prompt", prompt, [message["role"] for message in original]),
        ("回复", completion, ["assistant"]),
    ):
        if not isinstance(messages, list) or len(messages) != len(roles):
            raise ValueError(f"{name}的消息数量与原样本不一致。")
        for message, role in zip(messages, roles, strict=True):
            if (
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message["role"] != role
                or not isinstance(message["content"], str)
                or not message["content"].strip()
            ):
                raise ValueError(f"{name}的 {role} 消息需保留原角色，并填写正文。")


def create_server(data_dir: Path, port: int = 8768) -> ThreadingHTTPServer:
    page = (ROOT / "editor.html").read_bytes()
    write_lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, content, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", (
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            ))
            self.end_headers()
            self.wfile.write(content)

        def send_json(self, status, value):
            self.send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def allowed_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def dataset_path(self):
            key = self.path.removeprefix("/api/datasets/")
            if self.path.startswith("/api/datasets/") and key in DATASETS:
                return data_dir / DATASETS[key][1]
            self.send_json(404, {"error": "找不到这个样本集。"})
            return None

        def do_GET(self):
            if not self.allowed_host():
                self.send_json(403, {"error": "请使用编辑器的本机地址访问。"})
                return
            if self.path == "/":
                self.send(200, page, "text/html; charset=utf-8")
                return
            if self.path == "/api/datasets":
                self.send_json(200, [{"key": key, "label": label} for key, (label, _) in DATASETS.items()])
                return
            path = self.dataset_path()
            if path is None:
                return
            try:
                rows = [{"row": json.loads(line), "revision": revision(line)} for line in path.read_bytes().splitlines()]
            except (OSError, ValueError):
                self.send_json(500, {"error": "无法读取样本文件，请检查 JSONL。"})
                return
            self.send_json(200, {"filename": path.name, "samples": rows})

        def do_POST(self):
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if not self.allowed_host() or self.headers.get("Origin") != expected_origin:
                self.send_json(403, {"error": "请从本机编辑器提交。"})
                return
            path = self.dataset_path()
            if path is None:
                return
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                self.send_json(415, {"error": "只接受 JSON 保存请求。"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("保存请求为空或超过 1 MiB。")
                self.connection.settimeout(10)
                edit = json.loads(self.rfile.read(length))
                if not isinstance(edit, dict) or set(edit) != {"id", "revision", "prompt", "completion"}:
                    raise ValueError("保存请求需要样本编号、版本、Prompt 和回复。")
                if not isinstance(edit["id"], str) or not isinstance(edit["revision"], str):
                    raise ValueError("样本编号和版本必须是文本。")
            except (ValueError, TimeoutError) as exc:
                self.send_json(400, {"error": str(exc)})
                return

            try:
                with write_lock:
                    lines = path.read_bytes().splitlines(keepends=True)
                    for index, line in enumerate(lines):
                        row = json.loads(line)
                        if row["id"] == edit["id"]:
                            break
                    else:
                        self.send_json(404, {"error": "原样本已不存在。"})
                        return
                    if revision(line.rstrip(b"\r\n")) != edit["revision"]:
                        self.send_json(409, {"error": "这条样本在磁盘上已有新版本。请先复制你的改动，再重新加载页面。"})
                        return
                    validate_edit(row["prompt"], edit["prompt"], edit["completion"])
                    row.update(prompt=edit["prompt"], completion=edit["completion"])
                    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    ending = line[len(line.rstrip(b"\r\n")):]
                    lines[index] = encoded + ending
                    temp = NamedTemporaryFile(dir=path.parent, delete=False)
                    temporary = Path(temp.name)
                    try:
                        with temp:
                            temp.writelines(lines)
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            except OSError:
                self.send_json(500, {"error": "无法保存样本，请检查文件是否被占用。"})
                return
            self.send_json(200, {"row": row, "revision": revision(encoded)})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    with create_server(ROOT / "v1", args.port) as server:
        print(f"Misty 样本编辑器 http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
