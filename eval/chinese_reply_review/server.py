"""A deliberately narrow loopback HTTP surface; never serves project files."""
from __future__ import annotations

import html
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .review import analyze, export_review, import_ratings

MAX_SUBMISSION_BYTES = 2 * 1024 * 1024


def create_server(run_dir: Path, host: str = "127.0.0.1", port: int = 8795) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("Review server only permits host 127.0.0.1")
    run_dir = Path(run_dir).resolve()
    export_review(run_dir)
    token = secrets.token_urlsafe(32)
    mutation_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            # Avoid logging arbitrary user-provided paths or rating bodies.
            pass

        def _send(self, status: int, content: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(content)

        def _json(self, status: int, value: dict) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            authority = f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host") == authority

        def do_GET(self) -> None:
            if not self._host_ok():
                self._json(403, {"error": "仅允许通过本机 127.0.0.1 地址访问"})
                return
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                page = (run_dir / "review" / "index.html").read_text(encoding="utf-8")
                self._send(200, page.replace("__REVIEW_CSRF_TOKEN__", token).encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/samples.json":
                self._send(200, (run_dir / "review" / "samples.json").read_bytes(), "application/json; charset=utf-8")
            elif path == "/report":
                if not (run_dir / "private" / "ratings" / "first-seal.json").exists():
                    self._json(403, {"error": "请先完成全部有效样本的人工评分并封存，再揭盲"})
                    return
                try:
                    with mutation_lock:
                        report = analyze(run_dir)
                    markdown = (run_dir / "private" / "report.md").read_text(encoding="utf-8")
                    page = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>揭盲报告</title>"
                            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                            "<style>body{margin:0;background:#f5f3ed;color:#22352c;font:16px/1.85 system-ui,sans-serif}"
                            "main{max-width:980px;margin:auto;padding:48px 24px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}"
                            "a{color:#37694e}</style><main><a href='/'>返回评分页</a><h1>首轮人工评审 · 揭盲报告</h1>"
                            "<p>原始评分已封存。修订另存，默认报告继续引用首轮评分。</p><pre>"
                            + html.escape(markdown) + "</pre><p>有效样本：" + str(report["eligible_sample_count"]) + "</p></main></html>")
                    self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                except (ValueError, OSError, KeyError, TypeError):
                    self._json(409, {"error": "报告校验失败；请从命令行检查封存文件"})
            else:
                self._json(404, {"error": "此服务只提供本轮匿名评分页"})

        def do_POST(self) -> None:
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self._host_ok() or self.headers.get("Origin") != expected_origin
                    or self.headers.get("X-Review-Token") != token):
                self._json(403, {"error": "请从本机评分页提交"})
                return
            if urlsplit(self.path).path != "/submit":
                self._json(404, {"error": "未知接口"})
                return
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._json(415, {"error": "只接受 application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= MAX_SUBMISSION_BYTES:
                self._json(413, {"error": "评分文件为空或超过 2 MiB"})
                return
            try:
                self.connection.settimeout(15)
                ratings = json.loads(self.rfile.read(length).decode("utf-8"))
                with mutation_lock:
                    seal = import_ratings(run_dir, ratings)
                    analyze(run_dir)
                self._json(200, {"status": seal["status"], "message": seal["message"], "ratings_sha256": seal["ratings_sha256"], "report_url": "/report"})
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                self._json(400, {"error": str(exc)[:300]})
            except OSError:
                self._json(500, {"error": "评分保存失败；请先下载评分文件并检查本机磁盘"})

    return ThreadingHTTPServer((host, port), Handler)


def serve(run_dir: Path, host: str = "127.0.0.1", port: int = 8795) -> None:
    server = create_server(run_dir, host, port)
    print(f"人工评审：http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
