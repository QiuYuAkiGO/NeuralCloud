"""Render the loopback authentication proxy behind an HTTPS FRP tunnel."""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urlsplit

from gui.origin import normalize_origin
from harness.deploy.config import load_config


def render_caddy(*, public_origin: str | None, password_hash: str, gui_port: int = 8790,
                 proxy_port: int = 8879, username: str = "admin", maintenance: bool = False) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", username):
        raise ValueError("invalid ingress username")
    if not re.fullmatch(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}", password_hash):
        raise ValueError("ingress requires a bcrypt password hash")
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in (gui_port, proxy_port)) or gui_port == proxy_port:
        raise ValueError("invalid ingress ports")
    # The site address must not imply Host=127.0.0.1: FRP preserves the
    # browser's public Host. bind restricts the listener independently.
    prefix = f"{{\n    admin off\n    auto_https off\n}}\n\nhttp://:{proxy_port} {{\n    bind 127.0.0.1\n"
    prefix += '''    header {
        Content-Security-Policy "frame-ancestors 'none'"
        X-Frame-Options DENY
    }
'''
    if maintenance or not public_origin:
        return prefix + '    respond "NeuralCloud maintenance" 503\n}\n'
    origin = normalize_origin(public_origin)
    if not origin.startswith("https://"):
        raise ValueError("public ingress requires HTTPS")
    host = urlsplit(origin).hostname
    if not host or not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
        raise ValueError("public ingress requires a DNS hostname")
    return prefix + f'''    @unexpected_host not host {host}
    respond @unexpected_host "Unexpected host" 421
    basic_auth {{
        {username} {password_hash}
    }}
    reverse_proxy 127.0.0.1:{gui_port} {{
        header_up -Authorization
        header_up X-Forwarded-Proto https
        header_up X-Forwarded-Host {{http.request.hostport}}
    }}
}}
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--hash-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--proxy-port", type=int, default=8879)
    parser.add_argument("--maintenance", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    path = Path(args.hash_file)
    if path.is_symlink() or path.stat().st_size > 1024:
        raise ValueError("invalid ingress hash file")
    output = Path(args.output)
    if output.is_symlink():
        raise ValueError("ingress output cannot be a symlink")
    output.write_text(render_caddy(public_origin=cfg.public_origin,
        password_hash=path.read_text().strip(), gui_port=cfg.ports["gui"], proxy_port=args.proxy_port,
        username=args.username, maintenance=args.maintenance), encoding="utf-8")


if __name__ == "__main__":
    main()
