#!/usr/bin/env python3
"""Inspect private NapCat login state; refresh/export a QR only when requested."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DEFAULT_CONFIG = "/var/lib/neuralcloud-qq/napcat/config/webui.json"
PHASES = frozenset({"unknown", "initializing", "waiting_qrcode", "waiting_scan", "waiting_confirm",
                   "logging_in", "qrcode_expired", "qrcode_error", "offline", "expired", "error"})


class LoginError(RuntimeError):
    """Contains a fixed local reason code, never an upstream message."""


def fail(reason):
    raise LoginError(reason)


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unknown arguments and rejected values.
        fail("arguments_invalid")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WebUI:
    def __init__(self, port):
        if type(port) is not int or not 1024 <= port <= 65535:
            fail("webui_port_invalid")
        self.base_url = f"http://127.0.0.1:{port}/api"
        # A user's HTTP_PROXY must never receive the local login hash/credential.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def post(self, endpoint, payload, *, credential=None, reason):
        headers = {"Content-Type": "application/json"}
        if credential is not None:
            headers["Authorization"] = "Bearer " + credential
        request = Request(self.base_url + endpoint, data=json.dumps(payload).encode("utf-8"),
                          headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=15) as response:
                body = response.read(65537)
            if len(body) > 65536:
                fail(reason)
            value = json.loads(body)
            if (not isinstance(value, dict) or type(value.get("code")) is not int
                    or value["code"] != 0 or not isinstance(value.get("data"), dict)):
                fail(reason)
            return value["data"]
        except (HTTPError, URLError, OSError, UnicodeError, ValueError):
            fail(reason)


def absolute_private_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(ord(char) < 32 for char in str(value)):
        fail("private_path_invalid")
    if any(item.is_symlink() for item in (path, *path.parents)):
        fail("private_path_symlink")
    return path


def read_webui_token(path, port):
    path = absolute_private_path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 65536
                or (os.name == "posix" and info.st_mode & 0o077)):
            fail("webui_config_requires_private_regular_file")
        with os.fdopen(descriptor, "r", encoding="utf-8-sig") as stream:
            descriptor = -1
            config = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(config, dict):
        fail("webui_config_invalid")
    if config.get("host", "127.0.0.1") not in {"127.0.0.1", "localhost", "::1"}:
        fail("webui_config_requires_loopback")
    configured_port = config.get("port", port)
    if str(configured_port) != str(port):
        fail("webui_config_port_mismatch")
    token = config.get("token")
    if (not isinstance(token, str) or not 32 <= len(token) <= 4096
            or not token.isascii() or not token.isprintable() or any(c.isspace() for c in token)):
        fail("webui_token_invalid")
    return token


def validate_qr_output(value):
    target = absolute_private_path(value)
    if target.suffix.lower() != ".png":
        fail("qr_output_requires_png")
    parent = target.parent
    if not parent.is_dir():
        fail("qr_output_parent_missing")
    if os.name == "posix" and parent.stat().st_mode & 0o077:
        fail("qr_output_parent_must_be_private_0700")
    if target.exists():
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and info.st_mode & 0o077):
            fail("qr_output_requires_private_regular_file")
    return target


def qr_renderer():
    try:
        import qrcode
        from qrcode.image.pil import PilImage
    except ImportError:
        fail("qr_dependency_missing")

    def render(value, target):
        descriptor, temporary = tempfile.mkstemp(prefix=".qq-login-", suffix=".png", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
                qr.add_data(value)
                qr.make(fit=True)
                qr.make_image(image_factory=PilImage, fill_color="black", back_color="white").save(stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except Exception:
            fail("qr_render_failed")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    return render


def query(args):
    if type(args.webui_port) is not int or not 1024 <= args.webui_port <= 65535:
        fail("webui_port_invalid")
    if args.refresh and not args.qr_output:
        fail("qr_output_required_for_refresh")
    if args.qr_output and not args.refresh:
        fail("qr_output_requires_explicit_refresh")
    # Validate the destination and optional dependency before changing QR state.
    target = validate_qr_output(args.qr_output) if args.refresh else None
    render = qr_renderer() if args.refresh else None
    token = read_webui_token(args.config, args.webui_port)
    digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
    client = WebUI(args.webui_port)
    auth = client.post("/auth/login", {"hash": digest}, reason="webui_auth_failed")
    credential = auth.get("Credential")
    if (not isinstance(credential, str) or not 1 <= len(credential) <= 8192
            or not credential.isascii() or not credential.isprintable()
            or any(char.isspace() for char in credential)):
        fail("webui_auth_failed")
    data = client.post("/QQLogin/CheckLoginStatus", {}, credential=credential, reason="webui_status_failed")
    loggedin = data.get("isLogin")
    if type(loggedin) is not bool:
        fail("webui_status_invalid")
    raw_phase = data.get("loginPhase")
    phase = raw_phase if isinstance(raw_phase, str) and raw_phase in PHASES else "unknown"
    account = None
    if loggedin:
        data = client.post("/QQLogin/GetQQLoginInfo", {}, credential=credential, reason="qq_login_info_failed")
        for field in ("uin", "user_id", "qq"):
            value = data.get(field)
            if isinstance(value, (int, str)) and not isinstance(value, bool) and re.fullmatch(r"[1-9][0-9]{4,19}", str(value)):
                account = str(value)
                break
        phase = "logged_in"
    elif args.refresh:
        data = client.post("/QQLogin/RefreshQRcode", {}, credential=credential, reason="qq_qrcode_refresh_failed")
        value = data.get("qrcodeurl")
        if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(ord(char) < 32 for char in value):
            fail("qq_qrcode_unavailable")
        render(value, target)
        phase = "waiting_qrcode"
    return {"loggedin": loggedin, "account": account, "phase": phase}


def main(argv=None):
    parser = PrivateParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--webui-port", type=int, default=6799)
    parser.add_argument("--refresh", action="store_true", help="explicitly refresh a QR when logged out")
    parser.add_argument("--qr-output", help="absolute PNG path inside an existing private (0700) directory")
    try:
        args = parser.parse_args(argv)
        if sys.platform != "linux":
            fail("linux_required")
        print(json.dumps(query(args), ensure_ascii=True))
        return 0
    except LoginError as error:
        print(json.dumps({"error": str(error)}))
    except Exception:
        # Never print exception text, response bodies, headers, QR URLs or paths.
        print(json.dumps({"error": "napcat_login_helper_failed"}))
    return 20


if __name__ == "__main__":
    raise SystemExit(main())
