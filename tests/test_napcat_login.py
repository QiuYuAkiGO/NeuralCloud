from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import types
from urllib.error import HTTPError

import pytest

from tools.deploy import napcat_login as login


TOKEN = "private-webui-token-" + "a" * 32
CREDENTIAL = "private-session-credential-" + "b" * 32
QR = "https://example.invalid/private-qr-value"


@pytest.fixture
def inputs(tmp_path):
    tmp_path.chmod(0o700)
    config = tmp_path / "webui.json"
    config.write_text(json.dumps({"host": "127.0.0.1", "port": 6799, "token": TOKEN}))
    config.chmod(0o600)
    return types.SimpleNamespace(config=str(config), webui_port=6799, refresh=False, qr_output=None)


@pytest.fixture
def client(monkeypatch):
    class FakeWebUI:
        calls = []
        loggedin = False
        phase = "expired"

        def __init__(self, port):
            assert port == 6799

        def post(self, endpoint, payload, **kwargs):
            self.calls.append((endpoint, payload, kwargs))
            if endpoint == "/auth/login":
                return {"Credential": CREDENTIAL, "untrusted_extra": TOKEN}
            if endpoint.endswith("CheckLoginStatus"):
                return {"isLogin": self.loggedin, "loginPhase": self.phase, "qrcodeurl": QR}
            if endpoint.endswith("GetQQLoginInfo"):
                return {"uin": "123456789", "nickname": TOKEN}
            if endpoint.endswith("RefreshQRcode"):
                return {"qrcodeurl": QR}
            pytest.fail("unexpected endpoint")

    monkeypatch.setattr(login, "WebUI", FakeWebUI)
    return FakeWebUI


def test_default_status_does_not_refresh_expired_or_export_qr(inputs, client, tmp_path):
    before = set(tmp_path.iterdir())
    assert login.query(inputs) == {"loggedin": False, "account": None, "phase": "expired"}
    assert [call[0] for call in client.calls] == ["/auth/login", "/QQLogin/CheckLoginStatus"]
    assert client.calls[0][1] == {"hash": hashlib.sha256((TOKEN + ".napcat").encode()).hexdigest()}
    assert client.calls[1][2]["credential"] == CREDENTIAL
    assert set(tmp_path.iterdir()) == before


def test_logged_in_status_reports_only_numeric_account(inputs, client):
    client.loggedin = True
    assert login.query(inputs) == {"loggedin": True, "account": "123456789", "phase": "logged_in"}
    assert client.calls[-1][0] == "/QQLogin/GetQQLoginInfo"


def test_untrusted_phase_cannot_echo_credential_or_other_response_fields(inputs, client):
    client.phase = TOKEN
    result = json.dumps(login.query(inputs))
    assert json.loads(result)["phase"] == "unknown"
    assert not any(secret in result for secret in (TOKEN, CREDENTIAL, QR))


def test_refresh_requires_output_and_output_requires_refresh_before_contact(inputs, client):
    inputs.refresh = True
    with pytest.raises(login.LoginError, match="qr_output_required_for_refresh"):
        login.query(inputs)
    inputs.refresh = False
    inputs.qr_output = str(Path(inputs.config).with_name("login.png"))
    with pytest.raises(login.LoginError, match="qr_output_requires_explicit_refresh"):
        login.query(inputs)
    assert not client.calls


@pytest.mark.skipif(os.name != "posix", reason="Linux QR private file permissions")
def test_explicit_refresh_writes_private_png_without_returning_qr(inputs, client):
    inputs.refresh = True
    output = Path(inputs.config).with_name("login.png")
    inputs.qr_output = str(output)
    result = login.query(inputs)
    assert result == {"loggedin": False, "account": None, "phase": "waiting_qrcode"}
    assert client.calls[-1][0] == "/QQLogin/RefreshQRcode"
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert output.stat().st_mode & 0o777 == 0o600
    assert QR not in json.dumps(result)


@pytest.mark.skipif(os.name != "posix", reason="Linux private file permissions")
def test_refuses_public_config_and_symlinked_qr(inputs, tmp_path):
    config = Path(inputs.config)
    config.chmod(0o644)
    with pytest.raises(login.LoginError, match="private_regular_file"):
        login.read_webui_token(config, 6799)
    linked = tmp_path / "login.png"
    linked.symlink_to(config)
    with pytest.raises(login.LoginError, match="symlink"):
        login.validate_qr_output(linked)


@pytest.mark.parametrize("change,reason", [
    ({"token": "short"}, "webui_token_invalid"),
    ({"token": TOKEN + "\n"}, "webui_token_invalid"),
    ({"host": "example.invalid"}, "loopback"),
    ({"port": 6099}, "port_mismatch"),
])
def test_rejects_bad_config_inputs(inputs, change, reason):
    path = Path(inputs.config)
    config = json.loads(path.read_text())
    config.update(change)
    path.write_text(json.dumps(config))
    with pytest.raises(login.LoginError, match=reason):
        login.read_webui_token(path, 6799)


def test_http_redirects_and_errors_cannot_leak_authentication_data(capsys, monkeypatch):
    client = login.WebUI(6799)

    def reject(*args, **kwargs):
        raise HTTPError(QR, 302, TOKEN + CREDENTIAL, {}, None)

    monkeypatch.setattr(client.opener, "open", reject)
    with pytest.raises(login.LoginError) as error:
        client.post("/auth/login", {"hash": "private-hash"}, reason="webui_auth_failed")
    assert str(error.value) == "webui_auth_failed"
    assert login.NoRedirect().redirect_request(None, None, 302, None, None, QR) is None
    assert capsys.readouterr().out == ""


def test_cli_never_echoes_sensitive_arguments_or_exception_text(monkeypatch, capsys):
    assert login.main(["--unexpected-private-option", TOKEN]) == 20
    assert json.loads(capsys.readouterr().out) == {"error": "arguments_invalid"}
    monkeypatch.setattr(login.sys, "platform", "linux")

    def failure(args):
        raise RuntimeError(TOKEN + CREDENTIAL + QR)

    monkeypatch.setattr(login, "query", failure)
    assert login.main([]) == 20
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": "napcat_login_helper_failed"}
    assert captured.err == ""
